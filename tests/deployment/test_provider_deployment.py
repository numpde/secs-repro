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
from unittest.mock import Mock, mock_open, patch

from deployment.compose import ComposeProject
from deployment.provider_deployment import (
    _apply_provider_network,
    _admit_provider_network,
    _admit_runtime_volumes,
    _apply_shared_grants,
    _admit_worker_image,
    _host_gpu_uuid,
    _provider_network,
    _status,
    install_secret,
    main,
    render_deployment,
    retire_old_api,
    start_deployment,
)
from deployment.templates import _locked_parent
from deployment.worker_target import worker_target


class ProviderDeploymentTests(unittest.TestCase):
    def test_retirement_cli_reports_possible_partial_shutdown(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            errors = StringIO()
            with patch("deployment.provider_deployment.__file__", str(root / "deployment/provider_deployment.py")), \
                 patch("deployment.provider_deployment.retire_old_api", side_effect=ValueError("volume ownership differs")), \
                 redirect_stderr(errors):
                self.assertEqual(main(["retire-api", "example"]), 1)
            self.assertIn("volume ownership differs", errors.getvalue())
            self.assertIn("may already be stopped", errors.getvalue())
            self.assertIn("route resources may remain", errors.getvalue())

    def test_retirement_interrupt_keeps_signal_and_partial_effect_context(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            interruption = KeyboardInterrupt()
            with patch("deployment.provider_deployment.__file__", str(root / "deployment/provider_deployment.py")), \
                 patch("deployment.provider_deployment.retire_old_api", side_effect=interruption):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    main(["retire-api", "example"])
            self.assertIs(caught.exception, interruption)
            self.assertIn("may already be stopped", " ".join(interruption.__notes__))

    def test_start_refuses_incomplete_old_api_retirement(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            (config / "old-api-retirement.json").write_text("{}")
            with patch("deployment.provider_deployment._render_deployment") as render:
                with self.assertRaisesRegex(ValueError, "retirement is incomplete"):
                    start_deployment(root, "example")
            render.assert_not_called()

    def test_old_api_retirement_does_not_stop_a_new_topology(self):
        with TemporaryDirectory() as temporary:
            project = Mock()
            project.inventory.return_value = {"provider": {"Id": "1" * 64, "State": {"Running": True}}}
            with self.assertRaisesRegex(ValueError, "both old proxy roles"):
                retire_old_api(project, Path(temporary))
            project.stop.assert_not_called()
            project.command.assert_not_called()

    def test_old_api_retirement_preserves_dependencies_after_failed_provider_drain(self):
        with TemporaryDirectory() as temporary:
            project = Mock()
            project.name = "secs-example"
            key = "a" * 64
            mount = {"Type": "volume", "Name": project.name + "-route-" + key,
                     "Destination": "/route", "RW": True}
            def route(role):
                return {"Config": {"Labels": {"org.nmr-provider-tools.route": project.name,
                         "org.nmr-provider-tools.binding": key,
                         "org.nmr-provider-tools.route-role": role}}, "Mounts": [mount]}
            provider = {"Id": "1" * 64, "State": {"Running": False, "ExitCode": 1, "OOMKilled": False}}
            project.inventory.return_value = {"provider": provider, "api": route("proxy"),
                                              "api-init": route("initializer")}
            with self.assertRaisesRegex(ValueError, "clean drain"):
                retire_old_api(project, Path(temporary))
            project.stop.assert_not_called()
            project.command.assert_not_called()

    def test_old_api_retirement_rejects_foreign_route_before_stopping_provider(self):
        with TemporaryDirectory() as temporary:
            project = Mock()
            project.name = "secs-example"
            project.inventory.return_value = {"provider": {"State": {"Running": True}},
                                               "api": {"Config": {"Labels": {}}}, "api-init": {}}
            with self.assertRaisesRegex(ValueError, "proxy ownership"):
                retire_old_api(project, Path(temporary))
            project.stop.assert_not_called()
            project.command.assert_not_called()

    def test_old_api_retirement_refuses_foreign_volume_borrower(self):
        with TemporaryDirectory() as temporary:
            config = Path(temporary)
            project = Mock()
            project.name = "secs-example"
            volume = project.name + "-route-" + "a" * 64
            receipt = config / "old-api-retirement.json"
            receipt.write_text(json.dumps({"project": project.name, "volume": volume,
                                           "containers": {"api": "1" * 64}}))
            receipt.chmod(0o600)
            project.inventory.return_value = {}
            inspected = {"Name": volume, "Labels": {"com.docker.compose.project": project.name,
                         "com.docker.compose.volume": volume}}
            project.command.side_effect = ((volume + "\n").encode(),
                                           json.dumps([inspected]).encode(), b"2" * 64 + b"\n")
            with self.assertRaisesRegex(ValueError, "another container"):
                retire_old_api(project, config)
            self.assertTrue(receipt.exists())
            self.assertFalse(any(call.args[:2] == ("volume", "rm") for call in project.command.call_args_list))

    def test_old_api_retirement_checks_borrowers_before_stopping_proxy(self):
        with TemporaryDirectory() as temporary:
            project = Mock()
            project.name = "secs-example"
            key = "a" * 64
            volume = project.name + "-route-" + key
            mount = {"Type": "volume", "Name": volume, "Destination": "/route", "RW": True}
            def route(role, identifier):
                return {"Id": identifier, "Config": {"Labels": {
                    "org.nmr-provider-tools.route": project.name,
                    "org.nmr-provider-tools.binding": key,
                    "org.nmr-provider-tools.route-role": role}}, "Mounts": [mount]}
            provider = {"Id": "1" * 64, "State": {"Running": False, "ExitCode": 0, "OOMKilled": False}}
            project.inventory.return_value = {"provider": provider, "api": route("proxy", "2" * 64),
                                              "api-init": route("initializer", "3" * 64)}
            inspected = {"Name": volume, "Labels": {"com.docker.compose.project": project.name,
                         "com.docker.compose.volume": volume}}
            project.command.side_effect = ((volume + "\n").encode(), json.dumps([inspected]).encode(), b"4" * 64 + b"\n")
            with self.assertRaisesRegex(ValueError, "another container"):
                retire_old_api(project, Path(temporary))
            project.stop.assert_not_called()
            self.assertFalse((Path(temporary) / "old-api-retirement.json").exists())

    def test_old_api_retirement_retains_receipt_after_volume_removal_failure(self):
        with TemporaryDirectory() as temporary:
            config = Path(temporary)
            project = Mock()
            project.name = "secs-example"
            key = "a" * 64
            volume = project.name + "-route-" + key
            identifiers = {"provider": "1" * 64, "api": "2" * 64, "api-init": "3" * 64}
            records = {}
            for role, identifier in identifiers.items():
                labels = {"org.nmr-provider-tools.route": project.name,
                          "org.nmr-provider-tools.binding": key,
                          "org.nmr-provider-tools.route-role": "proxy" if role == "api" else "initializer"}
                records[role] = {"Id": identifier, "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
                                 "Config": {"Labels": labels}, "Mounts": [
                                     {"Type": "volume", "Name": volume, "Destination": "/route", "RW": True}]}
            project.stop.return_value = records
            project.inventory.return_value = records
            volume_record = {"Name": volume, "Labels": {"com.docker.compose.project": project.name,
                            "com.docker.compose.volume": volume}}
            def command(*args):
                if args[:2] == ("volume", "ls"):
                    return (volume + "\n").encode()
                if args[:2] == ("volume", "inspect"):
                    return json.dumps([volume_record]).encode()
                if args[0] == "ps":
                    return ("\n".join(identifiers.values()) + "\n").encode()
                if args[:2] == ("volume", "rm"):
                    raise RuntimeError("volume removal failed")
                return b""
            project.command.side_effect = command
            with self.assertRaisesRegex(RuntimeError, "volume removal failed"):
                retire_old_api(project, config)
            self.assertTrue((config / "old-api-retirement.json").exists())
            project.inventory.return_value = {}
            def resume(*args):
                if args[:2] == ("volume", "ls"):
                    return (volume + "\n").encode()
                if args[:2] == ("volume", "inspect"):
                    return json.dumps([volume_record]).encode()
                return b""
            project.command.side_effect = resume
            retire_old_api(project, config)
            self.assertFalse((config / "old-api-retirement.json").exists())
            self.assertEqual(project.stop.call_count, 1)

    def test_runtime_volume_reuse_requires_peer_style_ownership_labels(self):
        project = Mock(name="project")
        project.name = "secs-example"
        project.repository = Path("/workspace")
        project.inventory.return_value = {}
        plan = {"volumes": {role: {"name": "secs-example_" + role}
                            for role in ("lifetime", "sources", "session")}}
        labels = {"com.docker.compose.project": project.name,
                  "com.docker.compose.volume": "sources", "io.secs.deployment": "example",
                  "io.secs.checkout": "/workspace"}
        volume = {"Name": "secs-example_sources", "Labels": labels, "Driver": "local", "Options": None}
        project.command.side_effect = (b"secs-example_sources\n", json.dumps([volume]).encode(), b"")
        _admit_runtime_volumes(project, plan, "example")
        labels["io.secs.deployment"] = "other"
        project.command.side_effect = (b"secs-example_sources\n", json.dumps([volume]).encode())
        with self.assertRaisesRegex(ValueError, "ownership labels differ"):
            _admit_runtime_volumes(project, plan, "example")
        labels["io.secs.deployment"] = "example"
        labels["io.secs.checkout"] = "/other-checkout"
        project.command.side_effect = (b"secs-example_sources\n", json.dumps([volume]).encode())
        with self.assertRaisesRegex(ValueError, "ownership labels differ"):
            _admit_runtime_volumes(project, plan, "example")
        labels["io.secs.checkout"] = "/workspace"
        volume["Options"] = {"type": "tmpfs"}
        project.command.side_effect = (b"secs-example_sources\n", json.dumps([volume]).encode())
        with self.assertRaisesRegex(ValueError, "driver or options differ"):
            _admit_runtime_volumes(project, plan, "example")
        volume["Options"] = None
        project.command.side_effect = (b"secs-example_sources\n", json.dumps([volume]).encode(), b"f" * 64 + b"\n")
        with self.assertRaisesRegex(ValueError, "foreign or ambiguous container"):
            _admit_runtime_volumes(project, plan, "example")

    def test_session_volume_reuse_checks_owned_container_grants(self):
        project = Mock(name="project")
        project.name = "secs-example"
        project.repository = Path("/workspace")
        plan = {"volumes": {role: {"name": "secs-example_" + role}
                            for role in ("lifetime", "sources", "session")}}
        name = "secs-example_session"
        labels = {"com.docker.compose.project": project.name, "com.docker.compose.volume": "session",
                  "io.secs.deployment": "example", "io.secs.checkout": "/workspace"}
        volume = {"Name": name, "Labels": labels, "Driver": "local", "Options": None}
        identifier = "a" * 64
        mount = {"Type": "volume", "Name": name, "Destination": "/run/secs/worker", "RW": False}
        container = {"Id": identifier, "Mounts": [mount]}
        project.inventory.return_value = {"provider": container}

        def inspect():
            project.command.side_effect = (name.encode() + b"\n", json.dumps([volume]).encode(), identifier.encode() + b"\n")
            _admit_runtime_volumes(project, plan, "example")

        inspect()
        mount["RW"] = True
        with self.assertRaisesRegex(ValueError, "container grant differs"):
            inspect()
        mount["RW"] = False
        container["Mounts"].append(mount.copy())
        with self.assertRaisesRegex(ValueError, "container grant differs"):
            inspect()

    def test_start_refuses_old_host_runtime_before_starting_volumes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            for name in ("provider.toml", "worker.toml", "deployment.env"):
                path = config / name
                path.write_text("test")
                path.chmod(0o600)
            state = root / "secrets/deployments/example"
            state.mkdir(parents=True, mode=0o700)
            (root / "secrets").chmod(0o700)
            (root / "secrets/deployments").chmod(0o700)
            (state / "interpreter.key").write_text("test")
            (state / "interpreter.key").chmod(0o600)
            (state / "provider.signing.private.json").write_text("test")
            (state / "provider.signing.private.json").chmod(0o600)
            project = Mock()
            with patch("deployment.provider_deployment._provider_network", return_value={}), patch(
                "deployment.provider_deployment._render_deployment", return_value={"services": {"worker": {"environment": {}}}}
            ), patch("deployment.provider_deployment._admit_provider_network"), patch(
                "deployment.provider_deployment._admit_worker_image"
            ), patch(
                "deployment.provider_deployment._project", return_value=project
            ):
                for name in ("state", "sources", "socket"):
                    with self.subTest(name=name):
                        old = state / name
                        old.mkdir()
                        with self.assertRaisesRegex(ValueError, "Old host runtime directories remain"):
                            start_deployment(root, "example")
                        old.rmdir()
            project.start.assert_not_called()

    def test_worker_target_and_matching_image_are_explicit(self):
        self.assertEqual(worker_target("cpu"), "cpu")
        self.assertEqual(worker_target("gpu"), "gpu")
        with self.assertRaisesRegex(ValueError, "cpu or gpu"):
            worker_target("automatic")
        project = Mock()
        image = "sha256:" + "1" * 64
        plan = {"services": {"worker": {"image": image}}}
        project.command.return_value = json.dumps([{"Id": image, "Config": {"Labels": {
            "io.secs-repro.packages.variant": "cpu", "io.secs-repro.worker.device-contract": "explicit-v1"}}}]).encode()
        _admit_worker_image(project, plan)
        plan["services"]["worker"]["deploy"] = {"resources": {}}
        with self.assertRaisesRegex(ValueError, "gpu packages variant"):
            _admit_worker_image(project, plan)

    def test_worker_target_prompts_or_requires_an_explicit_choice(self):
        for answer, expected in (("1\n", "cpu"), ("2\n", "gpu")):
            with self.subTest(answer=answer), patch("builtins.open", mock_open(read_data=answer)):
                self.assertEqual(worker_target(""), expected)
        with patch("builtins.open", mock_open(read_data="")), self.assertRaisesRegex(ValueError, "cpu or gpu"):
            worker_target("")
        with patch("builtins.open", side_effect=OSError("no tty")), self.assertRaisesRegex(ValueError, "no interactive terminal"):
            worker_target("")

    def test_mock_index_requires_a_provider_image_that_can_label_results(self):
        project = Mock()
        worker_image = "sha256:" + "1" * 64
        provider_image = "sha256:" + "2" * 64
        plan = {"services": {"worker": {"image": worker_image, "environment": {"SECS_MOCK_INDEX": "1"}},
                             "provider": {"image": provider_image}}}
        worker = {"Id": worker_image, "Config": {"Labels": {
            "io.secs-repro.packages.variant": "cpu", "io.secs-repro.worker.device-contract": "explicit-v1",
            "io.secs-repro.worker.mock-index": "1"}}}
        old_provider = {"Id": provider_image, "Config": {"Labels": {}}}
        project.command.side_effect = [json.dumps([worker]).encode(), json.dumps([old_provider]).encode()]
        with self.assertRaisesRegex(ValueError, "cannot label"):
            _admit_worker_image(project, plan)

    def test_shared_grants_cannot_shadow_scientific_or_configuration_mounts(self):
        for socket_path in ("/run/secs/sources/delivery.sock", "/run/secs/delivery.sock"):
            with self.subTest(socket=socket_path), TemporaryDirectory() as temporary:
                config = Path(temporary)
                (config / "provider.toml").write_text('[shared]\nsettings_file="/private/shared.toml"\n')
                (config / "provider.toml").chmod(0o600)
                projection = {"group_add": ["1000"], "volumes": {"fixture-ipc": {"external": True}},
                              "mounts": [{"type": "volume", "source": "fixture-ipc", "target": str(Path(socket_path).parent)}],
                              "proxy_socket": "/route/proxy.sock", "outbox_socket": socket_path, "socket_group_id": 1000}
                project = Mock()
                project.command.return_value = json.dumps(projection).encode()
                mounts = [{"type": "bind", "target": "/run/secs/sources", "source": "/owned/sources"}]
                plan = {"services": {"provider": {"image": "sha256:" + "a" * 64, "volumes": mounts.copy()}}}
                with self.assertRaisesRegex(ValueError, "overlap"):
                    _apply_shared_grants(plan, project, config, config)
                self.assertEqual(plan["services"]["provider"]["volumes"], mounts)

    def test_shared_grants_preserve_existing_groups_and_scientific_mounts(self):
        with TemporaryDirectory() as temporary:
            config = Path(temporary)
            (config / "provider.toml").write_text('[shared]\nsettings_file="/private/shared.toml"\n')
            (config / "provider.toml").chmod(0o600)
            projection = {
                "group_add": ["1234"],
                "volumes": {name: {"name": name, "external": True} for name in ("proxy", "outbox")},
                "mounts": [{"type": "volume", "source": name, "target": "/run/" + name,
                            "read_only": True, "volume": {"nocopy": True}} for name in ("proxy", "outbox")],
                "proxy_socket": "/run/proxy/proxy.sock", "outbox_socket": "/run/outbox/http.sock",
                "socket_group_id": 1234,
            }
            project = Mock()
            project.command.return_value = json.dumps(projection).encode()
            mount = {"type": "volume", "source": "sources", "target": "/run/secs/sources"}
            plan = {"services": {"provider": {"image": "image", "volumes": [mount], "group_add": ["42"]}},
                    "volumes": {"sources": {}}}
            _apply_shared_grants(plan, project, config, config)
            provider = plan["services"]["provider"]
            self.assertEqual(provider["group_add"], ["42", "1234"])
            self.assertIn(mount, provider["volumes"])
            self.assertEqual(plan["volumes"]["proxy"], {"name": "proxy", "external": True})
            self.assertEqual(plan["volumes"]["outbox"], {"name": "outbox", "external": True})
            self.assertEqual(provider["volumes"][1:3], projection["mounts"])

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
            network.write_text('mode = "forwarded-services"\nhost_address = "172.22.0.1"\n')
            self.assertEqual(
                _provider_network(root, "example"),
                {"mode": "forwarded-services", "host_address": "172.22.0.1"},
            )

    def test_network_configuration_rejects_ambiguous_or_unsafe_values(self):
        invalid_documents = (
            "",
            "not toml",
            'mode = "unknown"\n',
            'mode = "standard"\nhost_address = "172.22.0.1"\n',
            'mode = "standard"\nextra = true\n',
            'mode = "forwarded-services"\n',
            'mode = "forwarded-services"\nhost_address = "host-gateway"\n',
            'mode = "forwarded-services"\nhost_address = "::1"\n',
            'mode = "forwarded-services"\nhost_address = "127.0.0.1:443"\n',
            'mode = "forwarded-services"\nhost_address = "0.0.0.0"\n',
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
            {"mode": "forwarded-services", "host_address": "172.22.0.1"},
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
                {"mode": "forwarded-services", "host_address": "172.22.0.1"},
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
            ({"IPAM": {"Config": [{"Subnet": "172.23.0.0/16", "Gateway": "172.23.0.1"}]}}, "inspected default bridge gateway"),
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
                        {"mode": "forwarded-services", "host_address": "172.22.0.1"},
                    )

    def test_forwarded_services_can_use_inspected_default_bridge_gateway(self):
        project = unittest.mock.Mock()
        external = {"Name": "secs-example-egress", "Driver": "bridge", "Internal": False,
                    "Labels": {"io.secs-repro.checkout": "/repository", "io.secs-repro.deployment": "example"},
                    "IPAM": {"Config": [{"Subnet": "172.22.0.0/16", "Gateway": "172.22.0.1"}]},
                    "Containers": {}}
        bridge = {"Name": "bridge", "Driver": "bridge",
                  "IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]}}
        project.command.side_effect = [json.dumps([external]).encode(), json.dumps([bridge]).encode()]
        project.inventory.return_value = {}
        _admit_provider_network(project, Path("/repository"), "example",
                                {"mode": "forwarded-services", "host_address": "172.17.0.1"})

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
                "network.toml": 'mode = "forwarded-services"\nhost_address = "172.22.0.1"\n',
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
            network.write_text('mode = "forwarded-services"\n')
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



if __name__ == "__main__":
    unittest.main()
