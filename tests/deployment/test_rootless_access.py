"""Prove narrow rootless access grants and runtime-owned secret publication."""

import json
import io
import os
from pathlib import Path
import stat
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from deployment import rootless_access
from deployment.provider_deployment import _ensure_mapped_directory, _ensure_mapped_tree, _grant_runtime_inputs
from tools.copy_deployment_secrets import main as copy_deployment_secrets


class RootlessAccessTests(unittest.TestCase):
    def test_map_id_applies_the_host_observed_range_offset(self):
        rows = rootless_access.parse_id_map(b"0 1000 1\n1 100000 65536\n", "UID")
        self.assertEqual(rootless_access.map_id(rows, 1000, "UID"), 100999)

    def test_map_id_rejects_gaps_and_overlaps(self):
        rows = rootless_access.parse_id_map(b"0 1000 1\n1 100000 10\n", "UID")
        with self.assertRaisesRegex(ValueError, "not mapped exactly once"):
            rootless_access.map_id(rows, 11, "UID")
        with self.assertRaisesRegex(ValueError, "overlapping"):
            rootless_access.parse_id_map(b"0 1000 10\n5 2000 10\n", "UID")

    def test_selected_engine_host_pid_maps_are_read_and_probe_is_retired(self):
        project = Mock()
        identifier = "a" * 64
        inspection = json.dumps([{"State": {"Running": True, "Pid": 42}}]).encode()
        project.command.side_effect = (identifier.encode(), inspection, inspection, (identifier + "\n").encode(), b"")
        maps = ((0, 1000, 1), (1, 100000, 65536))
        with patch.object(rootless_access, "_host_user_namespace"), patch.object(
                rootless_access, "_host_process_maps", return_value=(maps, maps)) as read_maps:
            self.assertEqual(rootless_access.mapped_ids(
                project, "sha256:" + "a" * 64, 1000, 1000, "host"), (100999, 100999))
        read_maps.assert_called_once()
        self.assertEqual(read_maps.call_args.args[0], 42)
        self.assertIn("_secs_map_probe", read_maps.call_args.args[1])
        run_args = project.command.call_args_list[0].args
        self.assertIn("--network", run_args)
        self.assertEqual(run_args[run_args.index("--network") + 1], "none")
        self.assertIn(("--userns", "host"), [
            tuple(run_args[index:index + 2]) for index in range(len(run_args) - 1)
        ])
        self.assertEqual(project.command.call_args_list[-1].args[:2], ("rm", "--force"))

    def test_host_proc_pid_must_identify_the_unique_mapping_probe(self):
        with patch.object(Path, "read_bytes", return_value=b"python\0-c\0different process\0") as read:
            with self.assertRaisesRegex(ValueError, "does not identify"):
                rootless_access._host_process_maps(42, "import time; _secs_map_probe = 'token'")
        read.assert_called_once()

    def test_failed_probe_creation_does_not_remove_a_container_by_name(self):
        project = Mock()
        project.command.side_effect = RuntimeError("container name is already in use")
        with patch.object(rootless_access, "_host_user_namespace"):
            with self.assertRaisesRegex(RuntimeError, "already in use") as raised:
                rootless_access.mapped_ids(project, "sha256:" + "a" * 64, 1000, 1000)
        project.command.assert_called_once()
        self.assertTrue(any("without removing it by name" in note for note in raised.exception.__notes__))

    def test_host_mapping_reader_rejects_a_nested_user_namespace(self):
        nested = b"0 1000 1\n1 100000 65536\n"
        with patch.object(Path, "read_bytes", side_effect=(nested, nested)):
            with self.assertRaisesRegex(ValueError, "initial user namespace"):
                rootless_access._host_user_namespace()

    def test_unmapped_container_uid_is_rejected(self):
        project = Mock()
        identifier = "b" * 64
        inspection = json.dumps([{"State": {"Running": True, "Pid": 42}}]).encode()
        project.command.side_effect = (identifier.encode(), inspection, inspection, (identifier + "\n").encode(), b"")
        with patch.object(rootless_access, "_host_user_namespace"), patch.object(
                rootless_access, "_host_process_maps", return_value=(((0, 1000, 1),), ((0, 1000, 1),))):
            with self.assertRaisesRegex(ValueError, "not mapped exactly once"):
                rootless_access.mapped_ids(project, "sha256:" + "a" * 64, 1000, 1000)
        self.assertEqual(project.command.call_args_list[-1].args[:2], ("rm", "--force"))

    def test_private_acl_accepts_owner_only_base_mode(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "secret"
            path.write_text("test")
            path.chmod(0o600)
            self.assertTrue(rootless_access.private_acl(path, owner_permissions="rw-"))
            self.assertTrue(rootless_access.private_owner_acl(path, owner_permissions="rw-"))

    def test_private_owner_acl_rejects_a_named_runtime_user(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "secret"
            path.write_text("test")
            path.chmod(0o640)
            entries = ("user::rw-", "user:100999:r--", "group::---", "mask::r--", "other::---")
            with patch.object(rootless_access, "_acl", return_value=entries):
                self.assertFalse(rootless_access.private_owner_acl(path, owner_permissions="rw-"))

    def test_private_acl_accepts_only_group_and_other_empty_named_user_acl(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config"
            path.write_text("test")
            path.chmod(0o640)
            entries = ("user::rw-", "user:100999:r--", "group::---", "mask::r--", "other::---")
            with patch.object(rootless_access, "_acl", return_value=entries):
                self.assertTrue(rootless_access.private_acl(
                    path, owner_permissions="rw-", mapped_permissions="r--", mapped_id=100999,
                ))
                self.assertFalse(rootless_access.private_acl(
                    path, owner_permissions="rw-", mapped_permissions="r--", mapped_id=100998,
                ))

    def test_grant_adds_one_exact_named_user_acl(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config"
            path.write_text("test")
            path.chmod(0o600)
            base = ("user::rw-", "group::---", "other::---")
            extended = ("user::rw-", "user:100999:r--", "group::---", "mask::r--", "other::---")
            with patch.object(rootless_access, "_acl", side_effect=(base, extended)), patch.object(
                    rootless_access.subprocess, "run", return_value=Mock(returncode=0)) as run:
                rootless_access.grant_private_access(
                    path, 100999, "r--", owner_uid=os.getuid(), owner_permissions="rw-", directory=False,
                )
            self.assertEqual(run.call_args.args[0][-3], "u:100999:r--,m::r--")

    def test_grant_rejects_an_existing_foreign_or_broader_acl(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "private"
            path.mkdir(mode=0o700)
            foreign = ("user::rwx", "user:100998:--x", "group::---", "mask::--x", "other::---")
            with patch.object(rootless_access, "_acl", return_value=foreign), patch.object(
                    rootless_access.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "unexpected ACL"):
                    rootless_access.grant_private_access(
                        path, 100999, "--x", owner_uid=os.getuid(), owner_permissions="rwx", directory=True,
                    )
            run.assert_not_called()

    def test_traversal_grant_rejects_world_searchable_private_directory(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "private"
            path.mkdir(mode=0o755)
            with patch("deployment.provider_deployment.grant_private_access",
                       side_effect=ValueError("unexpected ACL")) as grant:
                with self.assertRaisesRegex(ValueError, "unexpected ACL"):
                    _ensure_mapped_directory(path, 100999, "--x", owner_uid=os.getuid())
            grant.assert_called_once()

    def test_scientific_tree_accepts_internal_snapshot_symlinks_and_rejects_escape(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            (root / "blobs").mkdir(parents=True)
            (root / "snapshots").mkdir()
            target = root / "blobs/model.py"
            target.write_text("model")
            link = root / "snapshots/model.py"
            link.symlink_to("../blobs/model.py")
            _ensure_mapped_tree(root, os.getuid(), owner_uid=os.getuid())
            self.assertTrue(link.is_symlink())
            outside = Path(temporary) / "outside"
            outside.write_text("outside")
            (root / "snapshots/outside.py").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "escapes its tree"):
                _ensure_mapped_tree(root, os.getuid(), owner_uid=os.getuid())

    def test_secret_copies_are_runtime_owned_private_and_idempotent(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "runtime"
            output.mkdir(mode=0o700)
            rendered = []
            with patch("sys.stdin", type("BinaryInput", (), {"buffer": io.BytesIO(b"test credential")})()), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(copy_deployment_secrets([str(output), "interpreter.key"]), 0)
                rendered.append(stdout.getvalue())
            installed = output / "interpreter.key"
            self.assertEqual(installed.read_text(), "test credential")
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o400)
            self.assertEqual(installed.stat().st_uid, os.getuid())
            with patch("sys.stdin", type("BinaryInput", (), {"buffer": io.BytesIO(b"test credential")})()), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(copy_deployment_secrets([str(output), "interpreter.key"]), 0)
                self.assertEqual(json.loads(stdout.getvalue()), {"interpreter.key": "reused"})
            self.assertEqual(json.loads(rendered[0]), {"interpreter.key": "created"})

    def test_secret_copier_rejects_empty_stream_without_publishing(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "runtime"
            output.mkdir(mode=0o700)
            with patch("sys.stdin", type("BinaryInput", (), {"buffer": io.BytesIO(b"")})()):
                with self.assertRaisesRegex(ValueError, "1 to 65536 bytes"):
                    copy_deployment_secrets([str(output), "interpreter.key"])
            self.assertEqual(list(output.iterdir()), [])

    def test_grant_streams_secrets_without_acl_on_sources(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            config.parent.chmod(0o700)
            config.parent.parent.chmod(0o700)
            (config / "provider.toml").write_text("[api]\n")
            (config / "provider.toml").chmod(0o600)
            worker_config = config / "worker.toml"
            worker_config.write_text('device = "cpu"\n')
            worker_config.chmod(0o600)
            state = root / "secrets/deployments/example"
            state.parent.parent.mkdir(mode=0o700)
            state.parent.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            sources = {
                "provider.signing.private.json": b"credential bytes",
                "interpreter.key": b"interpreter bytes",
            }
            for name, content in sources.items():
                path = state / name
                path.write_bytes(content)
                path.chmod(0o600)
            helper_directory = root / "tools"
            helper_directory.mkdir()
            repository_helper = Path(__file__).resolve().parents[2] / "tools/copy_deployment_secrets.py"
            shutil.copyfile(repository_helper, helper_directory / repository_helper.name)
            plan = {"services": {
                "provider": {"user": f"{os.getuid()}:{os.getgid()}", "image": "sha256:" + "a" * 64,
                             "volumes": []},
                "worker": {"user": f"{os.getuid()}:{os.getgid()}", "volumes": [{
                    "type": "bind", "source": str(worker_config), "target": "/run/config/worker/worker.toml",
                }], "environment": {}},
            }}
            project = Mock()

            def publish(*arguments, input=None, **_kwargs):
                name = arguments[-1]
                runtime = Path(next(value.split(",src=", 1)[1].split(",dst=", 1)[0]
                                    for value in arguments if value.startswith("type=bind,src=")))
                destination = runtime / name
                destination.write_bytes(input)
                destination.chmod(0o400)
                return json.dumps({name: "created"}).encode()

            project.command.side_effect = publish
            with patch("deployment.provider_deployment.mapped_ids", return_value=(os.getuid(), os.getgid())), \
                    patch("deployment.provider_deployment._ensure_mapped_directory"), \
                    patch("deployment.provider_deployment._ensure_mapped_file"):
                result = _grant_runtime_inputs(project, plan, root, "example")

            self.assertEqual(set(result["secret_copies"]), set(sources))
            self.assertEqual(project.command.call_count, 2)
            for call, (name, content) in zip(project.command.call_args_list, sources.items()):
                self.assertIn("--interactive", call.args)
                self.assertEqual(call.kwargs["input"], content)
                self.assertEqual(call.args[-1], name)
                self.assertTrue(rootless_access.private_owner_acl(state / name, owner_permissions="rw-"))


if __name__ == "__main__":
    unittest.main()
