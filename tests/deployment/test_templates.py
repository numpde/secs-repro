"""Port NMRPeak's initialization proofs without its two-model release fixtures.

Real temporary Git repositories and concurrent callers exercise publication.
No engine, credentials, model assets, or external network are involved.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr
from io import StringIO
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from deployment import templates
from deployment.provider_deployment import TEMPLATES, main


def git(root, *arguments):
    """Operate only the temporary repository, independent of operator Git settings."""
    return subprocess.run(
        ("git", "-C", str(root), *arguments), check=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


@contextmanager
def repository():
    """Provide committed examples and an untracked-output ignore rule."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "config").mkdir()
        for destination, source in TEMPLATES.items():
            (root / source).write_text(f"example for {destination}\n")
        (root / ".gitignore").write_text("/config/deployments/\n")
        git(root, "init", "--quiet")
        git(root, "add", ".")
        git(root, "-c", "user.name=Test", "-c", "user.email=test@example.test",
            "commit", "--quiet", "-m", "examples")
        yield root


class InitializationTests(unittest.TestCase):
    def test_initialization_copies_committed_examples_once(self):
        with repository() as root:
            destination = templates.initialize_configuration(root, "production-1", TEMPLATES)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)
            for name, source in TEMPLATES.items():
                self.assertEqual((destination / name).read_bytes(), (root / source).read_bytes())
                self.assertEqual(stat.S_IMODE((destination / name).stat().st_mode), 0o600)
            (destination / "provider.toml").write_text("operator edit\n")
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                templates.initialize_configuration(root, "production-1", TEMPLATES)
            self.assertEqual((destination / "provider.toml").read_text(), "operator edit\n")
            self.assertEqual(git(root, "status", "--porcelain"), b"")

    def test_invalid_names_publish_nothing(self):
        with repository() as root:
            for name in ("", "../escape", "UPPER", "-leading", "trailing-", "a" * 65):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    templates.initialize_configuration(root, name, TEMPLATES)
            self.assertFalse((root / "config/deployments").exists())

    def test_worktree_and_ambient_git_do_not_change_committed_templates(self):
        with repository() as root, repository() as other:
            (root / TEMPLATES["provider.toml"]).write_text("uncommitted setting\n")
            (other / TEMPLATES["provider.toml"]).write_text("another repository\n")
            git(other, "add", ".")
            git(other, "-c", "user.name=Test", "-c", "user.email=test@example.test",
                "commit", "--quiet", "-m", "other settings")
            with patch.dict(os.environ, {"GIT_DIR": str(other / ".git"),
                                         "GIT_WORK_TREE": str(other)}):
                destination = templates.initialize_configuration(root, "production", TEMPLATES)
            self.assertEqual((destination / "provider.toml").read_text(),
                             "example for provider.toml\n")

    def test_head_is_pinned_before_reading_any_template(self):
        with repository() as root:
            original = templates._committed_template

            def move_head(repository_root, revision, relative):
                """Simulate a commit between snapshot selection and template reads."""
                (root / TEMPLATES["worker.toml"]).write_text("new worker settings\n")
                git(root, "add", ".")
                git(root, "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "commit", "--quiet", "--allow-empty", "-m", "next")
                return original(repository_root, revision, relative)

            with patch.object(templates, "_committed_template", side_effect=move_head):
                destination = templates.initialize_configuration(root, "production", TEMPLATES)
            self.assertEqual((destination / "worker.toml").read_text(),
                             "example for worker.toml\n")

    def test_concurrent_initializers_cannot_replace_the_winner(self):
        with repository() as root:
            barrier = threading.Barrier(2)
            original = templates._ensure_private_parent

            def meet_at_publication(parent):
                """Make both callers compete for the same empty destination."""
                original(parent)
                barrier.wait(timeout=5)

            with patch.object(templates, "_ensure_private_parent", side_effect=meet_at_publication):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(templates.initialize_configuration,
                                           root, "production", TEMPLATES) for _ in range(2)]
                    outcomes = [future.exception() for future in futures]
            self.assertEqual(outcomes.count(None), 1)
            self.assertEqual(sum(isinstance(error, FileExistsError) for error in outcomes), 1)
            self.assertEqual([p.name for p in (root / "config/deployments").iterdir()],
                             ["production"])

    def test_symlink_or_shared_parent_is_not_adopted(self):
        with repository() as root:
            parent = root / "config/deployments"
            parent.symlink_to(root / "config", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "0700"):
                templates.initialize_configuration(root, "production", TEMPLATES)
            parent.unlink()
            parent.mkdir(mode=0o755)
            # mkdir applies the caller's umask; this scenario specifically
            # requires a shared directory even under an owner-only umask.
            parent.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "0700"):
                templates.initialize_configuration(root, "production", TEMPLATES)

    def test_dangling_destination_and_tracked_symlink_are_not_followed(self):
        with repository() as root:
            parent = root / "config/deployments"
            parent.mkdir(mode=0o700)
            (parent / "production").symlink_to(root / "missing")
            with self.assertRaises(FileExistsError):
                templates.initialize_configuration(root, "production", TEMPLATES)
            source = root / TEMPLATES["provider.toml"]
            source.unlink()
            source.symlink_to("worker.toml.example")
            git(root, "add", ".")
            git(root, "-c", "user.name=Test", "-c", "user.email=test@example.test",
                "commit", "--quiet", "-m", "symlink")
            with self.assertRaisesRegex(ValueError, "committed regular file"):
                templates.initialize_configuration(root, "another", TEMPLATES)
            self.assertFalse((parent / "another").exists())

    def test_failed_write_does_not_publish_partial_configuration(self):
        with repository() as root:
            original = templates._write_new_file

            def fail_second_file(path, content):
                """Leave a completed first file for staging cleanup to remove."""
                if path.name == "worker.toml":
                    raise OSError("disk full")
                original(path, content)

            with patch.object(templates, "_write_new_file", side_effect=fail_second_file):
                with self.assertRaisesRegex(OSError, "disk full"):
                    templates.initialize_configuration(root, "production", TEMPLATES)
            self.assertEqual(list((root / "config/deployments").iterdir()), [])

    def test_failed_sync_after_rename_preserves_visible_configuration(self):
        with repository() as root:
            original = os.fsync

            def fail_after_publication(fd):
                """Fail only once the destination has become visible."""
                if (root / "config/deployments/production").exists():
                    raise OSError("sync failed")
                original(fd)

            with patch.object(templates.os, "fsync", side_effect=fail_after_publication):
                with self.assertRaisesRegex(OSError, "sync failed") as failure:
                    templates.initialize_configuration(root, "production", TEMPLATES)
            self.assertIn("has not been removed", failure.exception.__notes__[0])
            self.assertEqual((root / "config/deployments/production/worker.toml").read_text(),
                             "example for worker.toml\n")

    def test_cli_reports_uncertain_durability_without_claiming_rollback(self):
        error = OSError("sync failed")
        error.add_note("Configuration is visible; crash durability is unconfirmed.")
        output = StringIO()
        with patch("deployment.provider_deployment.initialize_configuration", side_effect=error):
            with redirect_stderr(output):
                self.assertEqual(main(["init", "production"]), 1)
        self.assertIn("initialization failed", output.getvalue())
        self.assertIn("Configuration is visible", output.getvalue())


if __name__ == "__main__":
    unittest.main()
