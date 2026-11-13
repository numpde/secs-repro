"""Prove narrow rootless access grants and runtime-owned secret publication."""

import json
import io
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from deployment import rootless_access
from deployment.provider_deployment import _ensure_mapped_directory, _ensure_mapped_tree, _grant_runtime_inputs
from tools.copy_deployment_secrets import main as copy_deployment_secrets


class RootlessAccessTests(unittest.TestCase):
    def test_shared_probe_receives_selected_engine_and_service_namespace(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        with patch.object(rootless_access.subprocess, "run", side_effect=(
                Mock(returncode=0), Mock(returncode=0, stdout=b"100999 101999\n"))) as run:
            self.assertEqual(rootless_access.mapped_ids(project, "sha256:" + "a" * 64, 1000, 2000, "host"),
                             (100999, 101999))
        self.assertIn("verify", run.call_args_list[0].args[0])
        args = run.call_args_list[1].args[0]
        self.assertIn("unix:///run/engine.sock", args)
        self.assertEqual(args[args.index("--userns") + 1], "host")
        self.assertNotIn("--inverse", args)

    def test_wheelhouse_selection_uses_inverse_shared_probe(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        with patch.object(rootless_access.subprocess, "run", side_effect=(
                Mock(returncode=0), Mock(returncode=0, stdout=b"0 0\n"))) as run:
            self.assertEqual(rootless_access.container_ids_for_host(project, "image", 1000, 1000), (0, 0))
        self.assertIn("--inverse", run.call_args_list[1].args[0])

    def test_probe_failure_cannot_grant_access(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        with patch.object(rootless_access.subprocess, "run", side_effect=(
                Mock(returncode=0), Mock(returncode=3, stdout=b""))):
            with self.assertRaisesRegex(RuntimeError, "shared probe failed"):
                rootless_access.mapped_ids(project, "image", 1000, 1000)

    def test_unverified_snapshot_cannot_run_probe(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        with patch.object(rootless_access.subprocess, "run", return_value=Mock(returncode=1)) as run:
            with self.assertRaisesRegex(RuntimeError, "source verification failed"):
                rootless_access.mapped_ids(project, "image", 1000, 1000)
        run.assert_called_once()

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
            (config / "provider.toml").write_text("[api]\n[execution]\n")
            (config / "provider.toml").chmod(0o600)
            endpoint_directory = config / "interpreter.d"
            endpoint_directory.mkdir(mode=0o700)
            endpoint = endpoint_directory / "00-primary.toml"
            endpoint.write_text('url = "https://model.example/v1/chat/completions"\nmodel = "model"\n'
                                'key_file = "/run/secrets/provider/interpreters/interpreter.key"\n')
            endpoint.chmod(0o600)
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
                self.assertEqual(("--entrypoint", "python", plan["services"]["provider"]["image"],
                                  "-I", "-B", "/opt/provider/copy_deployment_secrets.py", "/run/output", name), call.args[-8:])
                self.assertNotIn(content.decode(), " ".join(call.args))
                mounts = [call.args[index + 1] for index, value in enumerate(call.args) if value == "--mount"]
                self.assertEqual([f"type=bind,src={state / 'runtime'},dst=/run/output"], mounts)
                self.assertTrue(rootless_access.private_owner_acl(state / name, owner_permissions="rw-"))


if __name__ == "__main__":
    unittest.main()
