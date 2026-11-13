"""Prove narrow rootless access grants and runtime-owned secret publication."""

import json
import io
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from deployment import rootless_access
from deployment.provider_deployment import _ensure_mapped_directory, _ensure_mapped_tree, _grant_runtime_inputs
from tools.copy_deployment_secrets import main as copy_deployment_secrets


class RootlessAccessTests(unittest.TestCase):
    @staticmethod
    def _owner(status=0, output="100999 101999\n", receipt=b"retired\n"):
        def run(arguments):
            if receipt is not None:
                Path(arguments[arguments.index("--retired-receipt") + 1]).write_bytes(receipt)
            print(output, end="")
            return status
        return Mock(side_effect=run)

    def test_shared_probe_receives_selected_engine_and_service_namespace(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        owner = self._owner()
        with patch.object(rootless_access, "_identity_owner", return_value=owner):
            self.assertEqual(rootless_access.mapped_ids(project, "sha256:" + "a" * 64, 1000, 2000, "host"),
                             (100999, 101999))
        args = owner.call_args.args[0]
        self.assertIn("unix:///run/engine.sock", args)
        self.assertEqual(args[args.index("--userns") + 1], "host")
        self.assertEqual(args[args.index("--map"):args.index("--map") + 4], ["--map", "uid", "forward", "1000"])
        self.assertEqual(args[args.index("--map") + 4:args.index("--map") + 8], ["--map", "gid", "forward", "2000"])
        self.assertIn("67108864", args)
        self.assertIn("16", args)
        self.assertIn("1000000000", args)

    def test_wheelhouse_selection_uses_inverse_shared_probe(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        owner = self._owner(output="0 0\n")
        with patch.object(rootless_access, "_identity_owner", return_value=owner):
            self.assertEqual(rootless_access.container_ids_for_host(project, "image", 1000, 1000), (0, 0))
        args = owner.call_args.args[0]
        self.assertEqual(args[args.index("--map"):args.index("--map") + 4], ["--map", "uid", "inverse", "1000"])

    def test_probe_failure_cannot_grant_access(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        for status, output, receipt, message in ((3, "", b"retired\n", "shared probe failed"),
                                                  (0, "bad", b"retired\n", "invalid values"),
                                                  (0, "1 2", b"retired\n", "invalid values"),
                                                  (0, "01 2\n", b"retired\n", "invalid values"),
                                                  (0, "1 2 3\n", b"retired\n", "invalid values"),
                                                  (0, "4294967295 2\n", b"retired\n", "invalid values"),
                                                  (143, "", None, "retirement is unconfirmed"),
                                                  (0, "1 2", b"wrong", "retirement is unconfirmed")):
            with self.subTest(status=status, output=output, receipt=receipt), patch.object(
                    rootless_access, "_identity_owner", return_value=self._owner(status, output, receipt)):
                with self.assertRaisesRegex((RuntimeError, ValueError), message):
                    rootless_access.mapped_ids(project, "image", 1000, 1000)

    def test_retired_cancelled_probe_propagates_signal_status(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        for status in (129, 130, 143):
            with self.subTest(status=status), patch.object(
                    rootless_access, "_identity_owner", return_value=self._owner(status, "")):
                with self.assertRaises(SystemExit) as result:
                    rootless_access.mapped_ids(project, "image", 1000, 1000)
            self.assertEqual(result.exception.code, status)

    def test_unconfirmed_retirement_reports_original_cancellation_status(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        for status in (129, 130, 143):
            with self.subTest(status=status), patch.object(
                    rootless_access, "_identity_owner", return_value=self._owner(status, "", None)):
                with self.assertRaisesRegex(RuntimeError, f"retirement is unconfirmed.*owner exit {status}"):
                    rootless_access.mapped_ids(project, "image", 1000, 1000)

    def test_cleanup_failure_does_not_replace_confirmed_cancellation(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        with TemporaryDirectory() as directory:
            temporary = Mock(name=directory)
            temporary.name = directory
            temporary.cleanup.side_effect = OSError("fixture cleanup failed")
            with patch.object(rootless_access, "TemporaryDirectory", return_value=temporary), patch.object(
                    rootless_access, "_identity_owner", return_value=self._owner(143, "")), patch(
                    "sys.stderr", new_callable=io.StringIO) as diagnostic:
                with self.assertRaises(SystemExit) as result:
                    rootless_access.mapped_ids(project, "image", 1000, 1000)
            self.assertEqual(result.exception.code, 143)
            self.assertIn("fixture cleanup failed", diagnostic.getvalue())

    def test_unconfirmed_retirement_retains_status_and_cleanup_failure(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        with TemporaryDirectory() as directory:
            temporary = Mock()
            temporary.name = directory
            temporary.cleanup.side_effect = OSError("fixture cleanup failed")
            with patch.object(rootless_access, "TemporaryDirectory", return_value=temporary), patch.object(
                    rootless_access, "_identity_owner", return_value=self._owner(130, "", None)):
                with self.assertRaisesRegex(RuntimeError, "retirement is unconfirmed.*owner exit 130") as result:
                    rootless_access.mapped_ids(project, "image", 1000, 1000)
            self.assertTrue(any("fixture cleanup failed" in note for note in result.exception.__notes__))

    def test_unverified_snapshot_cannot_run_probe(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        verifier = Mock(side_effect=ValueError("source verification failed"))
        with patch.object(rootless_access.runpy, "run_path", return_value={"verify": verifier}), patch.object(
                rootless_access.importlib, "import_module") as importer:
            with self.assertRaisesRegex(ValueError, "source verification failed"):
                rootless_access.mapped_ids(project, "image", 1000, 1000)
        importer.assert_not_called()

    def test_owner_rejects_a_previously_loaded_foreign_identity_package(self):
        bundle = Path(__file__).resolve().parents[2] / "tools/bootstrap/acquisition"
        foreign = ModuleType("linux_idmap")
        foreign.__file__ = "/tmp/foreign/linux_idmap/__init__.py"
        with patch.dict(sys.modules, {"linux_idmap": foreign}):
            with self.assertRaisesRegex(RuntimeError, "loaded before admission or outside"):
                rootless_access._identity_owner(bundle)

    def test_owner_rejects_a_preloaded_module_even_from_the_bundle(self):
        bundle = Path(__file__).resolve().parents[2] / "tools/bootstrap/acquisition"
        preload = ModuleType("linux_idmap")
        preload.__file__ = str(bundle / "linux_idmap/__init__.py")
        with patch.object(rootless_access, "_IDENTITY_MANIFEST", None), patch.dict(sys.modules, {"linux_idmap": preload}):
            with self.assertRaisesRegex(RuntimeError, "loaded before admission"):
                rootless_access._identity_owner(bundle)

    def test_owner_rejects_a_changed_verified_snapshot(self):
        bundle = Path(__file__).resolve().parents[2] / "tools/bootstrap/acquisition"
        with patch.object(rootless_access, "_IDENTITY_MANIFEST", {"previous": "snapshot"}):
            with self.assertRaisesRegex(RuntimeError, "changed during this process"):
                rootless_access._identity_owner(bundle)

    def test_owner_exception_without_retirement_is_unsettled(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        with patch.object(rootless_access, "_identity_owner", return_value=Mock(side_effect=KeyboardInterrupt)):
            with self.assertRaisesRegex(RuntimeError, "retirement is unconfirmed"):
                rootless_access.mapped_ids(project, "image", 1000, 1000)

    def test_unconfirmed_retirement_preserves_owner_diagnostic(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")

        def unsettled(_arguments):
            print("Probe operation fixture; retirement unresolved.", file=sys.stderr)
            return 143

        with patch.object(rootless_access, "_identity_owner", return_value=unsettled), patch(
                "sys.stderr", new_callable=io.StringIO) as diagnostic:
            with self.assertRaisesRegex(RuntimeError, "retirement is unconfirmed"):
                rootless_access.mapped_ids(project, "image", 1000, 1000)
        self.assertIn("Probe operation fixture; retirement unresolved.", diagnostic.getvalue())

    def test_unconfirmed_retirement_survives_a_failed_diagnostic_sink(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        sink = Mock()
        sink.write.side_effect = OSError("diagnostic sink failed")
        with patch.object(rootless_access, "_identity_owner", return_value=self._owner(143, "", None)), \
                patch("sys.stderr", sink):
            with self.assertRaisesRegex(RuntimeError, "retirement is unconfirmed"):
                rootless_access.mapped_ids(project, "image", 1000, 1000)

    def test_confirmed_cancellation_survives_a_failed_diagnostic_sink(self):
        project = Mock(repository=Path("/checkout"), docker_endpoint="unix:///run/engine.sock")
        sink = Mock()
        sink.write.side_effect = OSError("diagnostic sink failed")
        with patch.object(rootless_access, "_identity_owner", return_value=self._owner(143, "")), \
                patch("sys.stderr", sink):
            with self.assertRaises(SystemExit) as result:
                rootless_access.mapped_ids(project, "image", 1000, 1000)
        self.assertEqual(result.exception.code, 143)

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
