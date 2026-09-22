"""Test SECS-specific installation and attempt ownership without a model or engine."""

import json
import fcntl
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deployment.compose import ComposeProject
from deployment.provider_deployment import (
    _apply_provider_network,
    _admit_provider_network,
    _bind_attempt_owner,
    _host_gpu_uuid,
    _provider_network,
    _status,
    install_secret,
    main,
    render_deployment,
)
from deployment.templates import _locked_parent


class ProviderDeploymentTests(unittest.TestCase):
    def test_host_gpu_is_one_strict_shared_input(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            config.mkdir()
            host = config / "host.toml"
            host.write_text('worker_gpu_uuid = "GPU-00000000-0000-0000-0000-000000000000"\n')
            host.chmod(0o600)
            self.assertEqual(
                _host_gpu_uuid(root),
                "GPU-00000000-0000-0000-0000-000000000000",
            )
            for invalid in (
                "",
                'worker_gpu_uuid = "0"\n',
                'worker_gpu_uuid = "GPU-00000000-0000-0000-0000-000000000000"\nextra = true\n',
                'worker_gpu_uuid = ["GPU-00000000-0000-0000-0000-000000000000"]\n',
                'worker_gpu_uuid = "GPU-------------------------------------"\n',
                'worker_gpu_uuid = "GPU-000000000000000000000000000000000000"\n',
            ):
                with self.subTest(invalid=invalid):
                    host.write_text(invalid)
                    with self.assertRaises((ValueError, OSError)):
                        _host_gpu_uuid(root)

    def test_network_modes_have_one_finite_schema(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            network = config / "network.toml"
            network.write_text('mode = "standard"\n')
            network.chmod(0o600)
            self.assertEqual(_provider_network(root, "example"), {"mode": "standard"})
            network.write_text('mode = "forwarded-api"\nhost_address = "172.22.0.1"\n')
            self.assertEqual(
                _provider_network(root, "example"),
                {"mode": "forwarded-api", "host_address": "172.22.0.1"},
            )

    def test_network_configuration_rejects_ambiguous_or_unsafe_values(self):
        invalid_documents = (
            "",
            "not toml",
            'mode = "unknown"\n',
            'mode = "standard"\nhost_address = "172.22.0.1"\n',
            'mode = "standard"\nextra = true\n',
            'mode = "forwarded-api"\n',
            'mode = "forwarded-api"\nhost_address = "host-gateway"\n',
            'mode = "forwarded-api"\nhost_address = "::1"\n',
            'mode = "forwarded-api"\nhost_address = "127.0.0.1:443"\n',
            'mode = "forwarded-api"\nhost_address = "0.0.0.0"\n',
            'mode = ["standard"]\n',
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            network = config / "network.toml"
            network.touch()
            network.chmod(0o600)
            for document in invalid_documents:
                with self.subTest(document=document):
                    network.write_text(document)
                    with self.assertRaises((ValueError, OSError)):
                        _provider_network(root, "example")

    def test_forwarded_network_changes_only_provider_route_and_network_ownership(self):
        plan = {
            "services": {
                "provider": {"image": "sha256:" + "a" * 64, "networks": {"provider-egress": None}},
                "worker": {"image": "sha256:" + "b" * 64, "network_mode": "none"},
            },
            "networks": {"provider-egress": {"name": "generated", "driver": "bridge"}},
        }
        worker = dict(plan["services"]["worker"])
        _apply_provider_network(
            plan,
            "example",
            {"mode": "forwarded-api", "host_address": "172.22.0.1"},
        )
        self.assertEqual(plan["services"]["provider"]["extra_hosts"], ["nmr.localhost=172.22.0.1"])
        self.assertEqual(
            plan["networks"]["provider-egress"],
            {"external": True, "name": "secs-example-egress"},
        )
        self.assertEqual(plan["services"]["worker"], worker)

    def test_standard_network_does_not_change_the_rendered_plan(self):
        plan = {
            "services": {"provider": {"networks": {"provider-egress": None}}, "worker": {"network_mode": "none"}},
            "networks": {"provider-egress": {"name": "generated", "driver": "bridge"}},
        }
        original = json.loads(json.dumps(plan))
        _apply_provider_network(plan, "example", {"mode": "standard"})
        self.assertEqual(plan, original)

    def test_forwarded_network_must_be_the_owned_route_to_its_host_address(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = unittest.mock.Mock()
            project.command.return_value = json.dumps([{
                "Name": "secs-example-egress",
                "Driver": "bridge",
                "Internal": False,
                "Labels": {
                    "io.secs-repro.checkout": str(root),
                    "io.secs-repro.deployment": "example",
                },
                "IPAM": {"Config": [{"Subnet": "172.22.0.0/16", "Gateway": "172.22.0.1"}]},
                "Containers": {},
            }]).encode()
            project.inventory.return_value = {}
            _admit_provider_network(
                project,
                root,
                "example",
                {"mode": "forwarded-api", "host_address": "172.22.0.1"},
            )
            project.command.assert_called_once_with("network", "inspect", "secs-example-egress")

    def test_standard_network_never_inspects_external_networks(self):
        project = unittest.mock.Mock()
        _admit_provider_network(project, Path("/unused"), "example", {"mode": "standard"})
        project.command.assert_not_called()

    def test_foreign_or_misdirected_external_network_is_rejected(self):
        valid = {
            "Name": "secs-example-egress",
            "Driver": "bridge",
            "Internal": False,
            "Labels": {
                "io.secs-repro.checkout": "/repository",
                "io.secs-repro.deployment": "example",
            },
            "IPAM": {"Config": [{"Subnet": "172.22.0.0/16", "Gateway": "172.22.0.1"}]},
            "Containers": {},
        }
        changes = (
            ({"Name": "other"}, "requires 'secs-example-egress'"),
            ({"Driver": "overlay"}, "non-internal bridge"),
            ({"Internal": True}, "non-internal bridge"),
            ({"Labels": {}}, "io.secs-repro.checkout"),
            ({"IPAM": {"Config": [{"Subnet": "172.23.0.0/16", "Gateway": "172.23.0.1"}]}}, "does not match"),
            ({"Containers": {"c" * 64: {}}}, "not owned"),
        )
        for change, message in changes:
            with self.subTest(change=change):
                record = valid | change
                project = unittest.mock.Mock()
                project.command.return_value = json.dumps([record]).encode()
                project.inventory.return_value = {}
                with self.assertRaisesRegex(ValueError, message):
                    _admit_provider_network(
                        project,
                        Path("/repository"),
                        "example",
                        {"mode": "forwarded-api", "host_address": "172.22.0.1"},
                    )

    def test_start_rejects_foreign_network_before_runtime_state_or_containers(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            inputs = {
                "deployment.env": (
                    "PROVIDER_IMAGE_REF=a\nWORKER_IMAGE_REF=b\n"
                    "CHECKPOINT_DIRECTORY=/checkpoint\nMOLFORMER_CACHE_DIRECTORY=/cache\n"
                ),
                "network.toml": 'mode = "forwarded-api"\nhost_address = "172.22.0.1"\n',
                "provider.toml": '[api]\norigin = "https://api.example.test"\n',
                "worker.toml": "worker = true\n",
            }
            for name, content in inputs.items():
                path = config / name
                path.write_text(content)
                path.chmod(0o600)
            state = root / "secrets/deployments/example"
            state.mkdir(parents=True, mode=0o700)
            for name in ("provider.signing.private.json", "interpreter.key"):
                path = state / name
                path.write_text("test input")
                path.chmod(0o600)
            project = unittest.mock.Mock()
            project.command.return_value = json.dumps([{
                "Name": "secs-example-egress",
                "Driver": "bridge",
                "Internal": False,
                "Labels": {
                    "io.secs-repro.checkout": str(root.resolve()),
                    "io.secs-repro.deployment": "foreign",
                },
                "IPAM": {"Config": [{"Subnet": "172.22.0.0/16", "Gateway": "172.22.0.1"}]},
                "Containers": {},
            }]).encode()
            project.inventory.return_value = {}
            errors = StringIO()
            with patch("deployment.provider_deployment.__file__", str(root / "deployment/cli.py")), patch(
                "deployment.provider_deployment._project", return_value=project,
            ), patch(
                "deployment.provider_deployment._render_deployment", return_value={},
            ), redirect_stderr(errors):
                self.assertEqual(main(["up", "example"]), 1)
            self.assertIn("Deployment up failed", errors.getvalue())
            self.assertIn("io.secs-repro.deployment", errors.getvalue())
            self.assertIn("before this attempt changed", errors.getvalue())
            project.start.assert_not_called()
            for name in ("attempt-owner.json", "state", "sources", "socket"):
                self.assertFalse((state / name).exists())
            project.command.side_effect = OSError("Docker executable unavailable")
            errors = StringIO()
            with patch("deployment.provider_deployment.__file__", str(root / "deployment/cli.py")), patch(
                "deployment.provider_deployment._project", return_value=project,
            ), patch(
                "deployment.provider_deployment._render_deployment", return_value={},
            ), redirect_stderr(errors):
                self.assertEqual(main(["up", "example"]), 1)
            self.assertIn("Docker executable unavailable", errors.getvalue())
            self.assertIn("before this attempt changed", errors.getvalue())
            project.start.assert_not_called()

    def test_invalid_controller_inputs_fail_before_compose_render(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            deployment = config / "deployment.env"
            deployment.write_text(
                "PROVIDER_IMAGE_REF=a\nWORKER_IMAGE_REF=b\n"
                "CHECKPOINT_DIRECTORY=/checkpoint\nMOLFORMER_CACHE_DIRECTORY=/cache\n"
            )
            deployment.chmod(0o600)
            network = config / "network.toml"
            network.write_text('mode = "forwarded-api"\n')
            network.chmod(0o600)
            host = root / "config/host.toml"
            host.write_text('worker_gpu_uuid = "GPU-00000000-0000-0000-0000-000000000000"\n')
            host.chmod(0o600)
            project = unittest.mock.Mock()
            with patch("deployment.provider_deployment._project", return_value=project):
                with self.assertRaises(ValueError):
                    render_deployment(root, "example")
            project.render.assert_not_called()

    def test_status_exposes_restart_and_oom_evidence_without_private_metadata(self):
        for health in (None, "unhealthy"):
            with self.subTest(health=health):
                state = {"Status": "restarting", "ExitCode": 137, "OOMKilled": True}
                expected = {"id": "container-id", "image": "image-id", "status": "restarting",
                            "restart_count": 4, "exit_code": 137, "oom_killed": True}
                if health is not None:
                    state["Health"] = {"Status": health, "Log": ["private-output"]}
                    expected["health"] = health
                record = {"Id": "container-id", "Image": "image-id", "RestartCount": 4,
                          "State": state, "Config": {"Env": ["private-secret"]}}
                self.assertEqual(_status({"worker": record}), {"worker": expected})

    def test_installation_reports_publication_when_staging_cleanup_fails(self):
        for sync_fails in (False, True):
            with self.subTest(sync_fails=sync_fails), TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config/deployments/production"
                config.mkdir(parents=True, mode=0o700)
                source = root / "key"
                source.write_bytes(b"test-only secret")
                source.chmod(0o600)
                destination = root / "secrets/deployments/production/interpreter.key"
                output = StringIO()

                def sync_after_link(path):
                    """Inject a sync failure only after the complete key becomes visible."""
                    if sync_fails and destination.exists():
                        raise OSError("publication sync failed")

                with patch("deployment.provider_deployment.__file__", str(root / "deployment/cli.py")), patch(
                    "deployment.templates._sync_directory", side_effect=sync_after_link,
                ), patch.object(TemporaryDirectory, "_rmtree", side_effect=OSError("staging cleanup denied")), \
                        redirect_stderr(output):
                    self.assertEqual(main(["interpreter-key-install", "production", "--source", str(source)]), 1)
                self.assertEqual(destination.read_bytes(), b"test-only secret")
                self.assertIn(f"File is visible at {destination}", output.getvalue())
                self.assertIn("staging cleanup denied", output.getvalue())
                if sync_fails:
                    self.assertIn("publication sync failed", output.getvalue())
                    self.assertIn("durability is unconfirmed", output.getvalue())

    def test_lifecycle_mutations_still_hold_the_exclusive_lock(self):
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "production"
            config.mkdir(mode=0o700)

            def check_exclusion(*args):
                """A separate open cannot acquire the lock while a mutation owns it."""
                descriptor = os.open(config.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
                return {}

            with patch("deployment.provider_deployment.configuration_directory", return_value=config), patch(
                "deployment.provider_deployment.start_deployment", side_effect=check_exclusion,
            ), patch.object(ComposeProject, "stop", side_effect=check_exclusion), redirect_stdout(StringIO()):
                for operation in ("up", "down"):
                    with self.subTest(operation=operation):
                        self.assertEqual(main([operation, "production"]), 0)

    def test_observation_does_not_wait_for_a_lifecycle_lock(self):
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "production"
            config.mkdir(mode=0o700)
            with patch("deployment.provider_deployment.configuration_directory", return_value=config), patch(
                "deployment.provider_deployment.render_deployment", return_value={},
            ), patch.object(ComposeProject, "inventory", return_value={}), redirect_stdout(StringIO()), \
                    ThreadPoolExecutor(max_workers=1) as pool:
                for operation in ("status", "logs", "config"):
                    with self.subTest(operation=operation), _locked_parent(config.parent):
                        # A shutdown may hold this inode for its entire grace
                        # period. Observations must finish before it releases it.
                        observed = pool.submit(main, [operation, "production"])
                        self.assertEqual(observed.result(timeout=5), 0)

    def test_failed_owner_write_does_not_poison_the_next_start(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider, credential = root / "provider.toml", root / "provider.signing.private.json"
            provider.write_text('[api]\norigin="https://api.example.test"\n')
            credential.write_text('{"principal_ref": "provider:test"}')
            for path in (provider, credential):
                path.chmod(0o600)

            def interrupted_write(output, content):
                """Simulate a disk write failure after the first byte reaches the file."""
                output.write_bytes(content[:1])
                raise OSError("disk full")

            with patch("deployment.templates._write_new_file", side_effect=interrupted_write):
                with self.assertRaisesRegex(OSError, "disk full"):
                    _bind_attempt_owner(root, root)
            self.assertFalse((root / "attempt-owner.json").exists())
            _bind_attempt_owner(root, root)
            self.assertEqual(json.loads((root / "attempt-owner.json").read_bytes()),
                             {"origin": "https://api.example.test", "provider_ref": "provider:test"})

    def test_credential_install_is_private_and_never_replaces_existing_input(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/production"
            config.mkdir(parents=True, mode=0o700)
            source = root / "source"
            source.write_text("test-only credential bytes")
            source.chmod(0o600)
            installed = install_secret(root, "production", "provider.signing.private.json", source)
            self.assertEqual(installed.read_bytes(), source.read_bytes())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                install_secret(root, "production", "provider.signing.private.json", source)
            self.assertTrue(source.exists())

    def test_attempt_state_allows_key_rotation_but_not_another_api_or_provider(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, config = root / "state", root / "config"
            state.mkdir(mode=0o700)
            config.mkdir(mode=0o700)
            provider = config / "provider.toml"
            credential = state / "provider.signing.private.json"
            provider.write_text('[api]\norigin="https://api.example.test"\n')
            provider.chmod(0o600)
            credential.write_text(json.dumps({"principal_ref": "provider:test", "credential_ref": "first"}))
            credential.chmod(0o600)
            _bind_attempt_owner(state, config)
            credential.write_text(json.dumps({"principal_ref": "provider:test", "credential_ref": "rotated"}))
            _bind_attempt_owner(state, config)
            binding = (state / "attempt-owner.json").read_bytes()
            provider.write_text('[api]\norigin="https://another.example.test"\n')
            with self.assertRaisesRegex(ValueError, "another API or provider"):
                _bind_attempt_owner(state, config)
            provider.write_text('[api]\norigin="https://api.example.test"\n')
            credential.write_text(json.dumps({"principal_ref": "provider:other"}))
            with self.assertRaisesRegex(ValueError, "another API or provider"):
                _bind_attempt_owner(state, config)
            self.assertEqual((state / "attempt-owner.json").read_bytes(), binding)


if __name__ == "__main__":
    unittest.main()
