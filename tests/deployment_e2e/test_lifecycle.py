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
import unittest
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
                    {"mode": "forwarded-api", "host_address": gateway},
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
            credential = provider_mounts["/run/secrets/provider/signing.private.json"]
            self.assertTrue(credential["read_only"])
            self.assertIn("/secrets/deployments/example/", credential["source"])
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
                'mode = "forwarded-api"\nhost_address = "172.22.0.1"\n'
            )
            (config / "network.toml").chmod(0o600)
            env = config / "deployment.env"
            env.write_text(
                f"PROVIDER_IMAGE_REF={image}\nWORKER_IMAGE_REF={image}\n"
                f"CHECKPOINT_DIRECTORY={root}/checkpoint\nMOLFORMER_CACHE_DIRECTORY={root}/cache\n"
            )
            env.chmod(0o600)
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
                "entrypoint": ["python", "-c", "import time; time.sleep(3600)"],
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
            recipe.write_text(json.dumps({"services": {
                "provider": service | {"depends_on": ["worker"]}, "worker": service,
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
                stopped = project.stop()
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
