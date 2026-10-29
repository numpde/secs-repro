"""Exercise real Compose lifecycle with offline, disposable services.

This checks startup, stop/restart, and retained state. The separate provider E2E
lane exercises scientific execution against API fixtures; neither proves live
API authentication.
"""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from contextlib import contextmanager
import shutil
from time import monotonic, sleep
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

from deployment.compose import ComposeProject
from deployment.provider_deployment import _admit_provider_network, _status, render_deployment


@contextmanager
def lifecycle_workspace():
    """Retain evidence if an assertion or container cleanup fails.

    The caller removes admitted containers before returning normally. On an
    uncertain shutdown, their bind-mounted data must not disappear underneath them.
    """
    temporary = mkdtemp(prefix="provider-lifecycle-")
    try:
        yield temporary
    except BaseException as error:
        error.add_note(f"Lifecycle test workspace retained at {temporary}")
        raise
    else:
        shutil.rmtree(temporary)


def binding_fixture(root, config, *, origin="https://api.example", credential=None, ca_file=None, uid=65532, gid=65532):
    """Grant synthetic paths through the actual installed SDK definition parser."""
    if credential is None:
        credential = root / "synthetic-signing.json"
        credential.write_text("fixture credential is projected, never opened")
        credential.chmod(0o600)
    definition = root / "bindings.toml"
    definition.write_text(f'''project = "secs-render-fixture"
uid = {uid}
gid = {gid}
request_seconds = 1
retry_initial_seconds = 0.01
retry_max_seconds = 0.1
[limits]
provider_items = 1
provider_bytes = 4096
total_items = 16
total_bytes = 65536
[[bindings]]
name = "secs"
socket_path = "/run/delivery/delivery.sock"
[bindings.api]
origin = "{origin}"
provider_ref = "provider:secs-route"
profile = "dev-local"
topology = "dev-local"
credential_file = "{credential}"
{'' if ca_file is None else f'ca_file = "{ca_file}"'}
exchange_seconds = 1
retirement_seconds = 0.2
[bindings.route]
mode = "mapped"
interface = "lo"
log_level = "INFO"
addresses = ["{urlsplit(origin).hostname}=127.0.0.1"]
''')
    definition.chmod(0o600)
    (config / "provider.toml").write_text(f'[binding]\ndefinition_file = "{definition}"\nname = "secs"\n')
    (config / "provider.toml").chmod(0o600)
    return credential


class LifecycleEndToEndTests(unittest.TestCase):
    def test_forwarded_network_admission_reads_the_real_engine_record(self):
        with TemporaryDirectory(prefix="secs-network-admission-") as temporary:
            root = Path(temporary).resolve()
            token = uuid4().hex
            deployment = "network-" + token[:12]
            network_name = f"secs-{deployment}-egress"
            octet = 20 + int(token[:2], 16) % 200
            gateway = f"10.253.{octet}.1"
            project = ComposeProject(root, f"secs-{deployment}", ("provider", "worker"))
            project.command(
                "network", "create", "--driver", "bridge",
                "--subnet", f"10.253.{octet}.0/24", "--gateway", gateway,
                "--label", f"io.secs-repro.checkout={root}",
                "--label", f"io.secs-repro.deployment={deployment}",
                network_name,
            )
            try:
                _admit_provider_network(
                    project,
                    root,
                    deployment,
                    {"mode": "forwarded-services", "host_address": gateway},
                )
            finally:
                project.command("network", "rm", network_name)

    def test_secs_recipe_routes_only_declared_inputs_to_their_consumers(self):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        source = Path(__file__).resolve().parents[2]
        with TemporaryDirectory(prefix="secs-render-") as temporary:
            root = Path(temporary)
            config = root / "config/deployments/example"
            config.mkdir(parents=True, mode=0o700)
            (root / "compose").mkdir()
            (root / "compose/provider.yml").write_bytes((source / "compose/provider.yml").read_bytes())
            (root / "config/host.toml").write_text(
                'worker_gpu_uuid = "GPU-00000000-0000-0000-0000-000000000000"\n'
            )
            (root / "config/host.toml").chmod(0o600)
            (config / "network.toml").write_text('mode = "standard"\n')
            (config / "network.toml").chmod(0o600)
            env = config / "deployment.env"
            env.write_text(f"PROVIDER_IMAGE_REF={image}\nWORKER_IMAGE_REF={image}\n"
                           f"CHECKPOINT_DIRECTORY={root}/checkpoint\n"
                           f"MOLFORMER_CACHE_DIRECTORY={root}/cache\n")
            env.chmod(0o600)
            credential_path = binding_fixture(root, config)
            plan = render_deployment(root, "example")
            provider, worker = plan["services"]["provider"], plan["services"]["worker"]
            self.assertEqual(provider["restart"], "unless-stopped")
            self.assertEqual(worker["restart"], "unless-stopped")
            self.assertEqual(worker["network_mode"], "none")
            devices = worker["deploy"]["resources"]["reservations"]["devices"]
            self.assertEqual(
                devices[0]["device_ids"],
                ["GPU-00000000-0000-0000-0000-000000000000"],
            )
            self.assertNotIn("ports", provider)
            self.assertNotIn("ports", worker)
            provider_mounts = {v["target"]: v for v in provider["volumes"]}
            worker_mounts = {v["target"]: v for v in worker["volumes"]}
            credential = provider_mounts[str(credential_path)]
            self.assertTrue(credential["read_only"])
            self.assertEqual(credential["source"], str(credential_path))
            self.assertNotIn(str(credential_path), worker_mounts)
            self.assertTrue(provider_mounts["/run/secs/worker"]["read_only"])
            self.assertTrue(provider_mounts["/run/delivery"]["read_only"])
            command = " ".join(worker["command"])
            self.assertIn("python -I -m secs_inference.provider.worker_config_snapshot", command)
            self.assertLess(command.index("secs_inference.provider.worker_config_snapshot"),
                            command.index("provider_core.runner"))
            self.assertIn("python -I -m provider_core.runner --role secs", command)
            self.assertIn("--owner-session-lifetime", command)
            self.assertIn("-- python -I -m secs_inference.provider.worker_model", command)
            self.assertFalse(any("/secrets/" in target for target in worker_mounts))
            self.assertTrue(worker_mounts["/checkpoint"]["read_only"])
            self.assertFalse((root / "secrets").exists())

    def test_forwarded_api_network_is_valid_compose_input(self):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        source = Path(__file__).resolve().parents[2]
        with TemporaryDirectory(prefix="secs-forwarded-render-") as temporary:
            root = Path(temporary)
            config = root / "config/deployments/fw-remote"
            config.mkdir(parents=True, mode=0o700)
            (root / "compose").mkdir()
            (root / "compose/provider.yml").write_bytes((source / "compose/provider.yml").read_bytes())
            (root / "config/host.toml").write_text(
                'worker_gpu_uuid = "GPU-00000000-0000-0000-0000-000000000000"\n'
            )
            (root / "config/host.toml").chmod(0o600)
            (config / "network.toml").write_text(
                'mode = "forwarded-services"\nhost_address = "172.22.0.1"\n'
            )
            (config / "network.toml").chmod(0o600)
            env = config / "deployment.env"
            env.write_text(
                f"PROVIDER_IMAGE_REF={image}\nWORKER_IMAGE_REF={image}\n"
                f"CHECKPOINT_DIRECTORY={root}/checkpoint\nMOLFORMER_CACHE_DIRECTORY={root}/cache\n"
            )
            env.chmod(0o600)
            binding_fixture(root, config)
            plan = render_deployment(root, "fw-remote")
            self.assertEqual(
                plan["services"]["provider"]["extra_hosts"],
                ["nmr.localhost=172.22.0.1"],
            )
            self.assertEqual(
                plan["networks"]["provider-egress"],
                {"external": True, "name": "secs-fw-remote-egress"},
            )
            self.assertEqual(plan["services"]["worker"]["network_mode"], "none")

    def test_render_start_inspect_stop_and_preserve_state(self):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        with lifecycle_workspace() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir(mode=0o700)
            marker = state / "retained-attempt"
            marker.write_text("must survive shutdown")
            project = ComposeProject(root, "provider-test-" + uuid4().hex[:12],
                                     ("provider", "worker"))
            service = {
                "image": "${TEST_IMAGE:?set the existing local image}",
                "init": True,
                "entrypoint": ["python", "-c", "import signal,time; signal.signal(signal.SIGTERM, lambda *_: exit(0)); time.sleep(3600)"],
                "network_mode": "none", "read_only": True,
                "user": f"{os.getuid()}:{os.getgid()}",
                "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                "pids_limit": 32, "mem_limit": "128m", "cpus": 0.5,
                "stop_grace_period": "5s",
                "logging": {"driver": "json-file", "options": {
                    "max-size": "1m", "max-file": "1", "compress": "false"}},
                "volumes": [{"type": "bind", "source": str(state), "target": "/state",
                             "bind": {"create_host_path": False}}],
            }
            recipe = root / "compose.json"
            draining_provider = service | {
                "depends_on": ["worker"], "restart": "unless-stopped", "stop_grace_period": "1s",
                "entrypoint": ["python", "-c", (
                    "import signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, lambda *_: (time.sleep(2), exit(0))); "
                    "Path('/state/ready').touch(); time.sleep(3600)"
                )],
            }
            recipe.write_text(json.dumps({"services": {
                "provider": draining_provider, "worker": service,
            }}))
            env = root / "deployment.env"
            env.write_text(f"TEST_IMAGE={image}\n")
            plan = project.render(recipe, env, {})
            self.assertEqual(project.inventory(), {})
            try:
                records = project.start(plan)
                self.assertEqual(set(records), {"provider", "worker"})
                for status in _status(records).values():
                    self.assertEqual(status["status"], "running")
                    self.assertEqual(status["restart_count"], 0)
                    self.assertFalse(status["oom_killed"])
                for record in records.values():
                    self.assertTrue(record["State"]["Running"])
                    self.assertEqual(record["HostConfig"]["NetworkMode"], "none")
                    self.assertTrue(record["HostConfig"]["ReadonlyRootfs"])
                    self.assertEqual(record["Image"], image)
                with self.assertRaisesRegex(ValueError, "already running"):
                    project.start(plan)
                deadline = monotonic() + 5
                while not (state / "ready").exists():
                    self.assertLess(monotonic(), deadline, "Provider did not install its drain handler")
                    sleep(0.02)
                started = monotonic()
                stopped = project.stop()
                self.assertGreaterEqual(monotonic() - started, 2)
                self.assertEqual(stopped["provider"]["State"]["ExitCode"], 0)
                self.assertEqual(stopped["provider"]["RestartCount"], 0)
                self.assertTrue(all(not r["State"]["Running"] for r in stopped.values()))
                self.assertEqual(marker.read_text(), "must survive shutdown")
                # Restart uses the same persistent input, not a new empty state.
                restarted = project.start(plan)
                self.assertTrue(all(r["State"]["Running"] for r in restarted.values()))
                self.assertEqual(marker.read_text(), "must survive shutdown")
            finally:
                # Only the disposable test owns removal. Production down keeps
                # containers and state; cleanup uses its admitted immutable IDs.
                records = project.stop()
                for record in records.values():
                    project.command("rm", record["Id"])


if __name__ == "__main__":
    unittest.main()
