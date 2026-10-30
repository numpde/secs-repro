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
from uuid import uuid4

from deployment.compose import ComposeProject
from deployment.provider_deployment import _admit_provider_network, _admit_runtime_volumes, _status, render_deployment


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


def shared_fixture(root, config, *, origin="https://api.example", credential=None, ca_file=None, gid=65532,
                   project="secs-render-fixture"):
    """Project synthetic shared grants through the installed SDK."""
    if credential is None:
        credential = root / "synthetic-signing.json"
        credential.write_text("fixture credential is projected, never opened")
        credential.chmod(0o600)
    (config / "provider.toml").write_text(f'''schema_id = "secs.provider.config.v4"
[shared]
project = "{project}"
gid = {gid}
[api]
origin = "{origin}"
provider_ref = "provider:secs-route"
profile = "dev-local"
topology = "dev-local"
credential_file = "/run/secrets/provider/provider.signing.private.json"
{'' if ca_file is None else 'ca_file = "/run/config/provider/api-ca.crt"'}
exchange_seconds = 1
retirement_seconds = 0.2
[delivery]
exchange_seconds = 1
retry_initial_seconds = 0.01
retry_max_seconds = 0.1
''')
    (config / "provider.toml").chmod(0o600)
    return credential


class LifecycleEndToEndTests(unittest.TestCase):
    def test_volume_backed_runtime_starts_and_retains_native_lock(self):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        source = Path(__file__).resolve().parents[2]
        with lifecycle_workspace() as temporary:
            root = Path(temporary)
            project = ComposeProject(root, "secs-volume-" + uuid4().hex[:12],
                                     ("provider", "worker", "sources-init", "session-init"),
                                     ("sources-init", "session-init"))
            env_file = root / "deployment.env"
            env_file.write_text(f"PROVIDER_IMAGE_REF={image}\nWORKER_IMAGE_REF={image}\n"
                                f"CHECKPOINT_DIRECTORY={root}\nMOLFORMER_CACHE_DIRECTORY={root}\n")
            environment = {"PROVIDER_UID": str(os.getuid()), "PROVIDER_GID": str(os.getgid()),
                           "SECS_DEPLOYMENT": "example",
                           "SECS_CHECKOUT": str(root),
                           "PROVIDER_CONFIG_DIRECTORY": str(root), "INTERPRETER_KEY_FILE": str(env_file),
                           "WORKER_CONFIG_FILE": str(env_file), "MOLFORMER_LOCK_FILE": str(env_file),
                           "CACHE_VERIFIER_FILE": str(env_file),
                           "SECS_HOST_WORKER_GPU_UUID": "GPU-00000000-0000-0000-0000-000000000000"}
            plan = project.render(source / "compose/provider.yml", env_file, environment)
            for role in ("provider", "worker"):
                service = plan["services"][role]
                service["entrypoint"] = ["python", "-I", "-c", "import time; time.sleep(300)"]
                service.pop("command", None)
                service.pop("deploy", None)
                service["network_mode"] = "none"
                service.pop("networks", None)
                service["volumes"] = [mount for mount in service["volumes"]
                                      if mount["type"] == "volume"]
            plan.pop("networks", None)
            volumes = [volume["name"] for volume in plan["volumes"].values()]
            try:
                records = project.start(plan)
                _admit_runtime_volumes(project, plan, "example")
                foreign = project.command(
                    "run", "--detach", "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--mount", f"type=volume,src={plan['volumes']['sources']['name']},dst=/foreign,readonly",
                    "--entrypoint", "python", image, "-I", "-c", "import time; time.sleep(300)",
                ).decode().strip()
                try:
                    with self.assertRaisesRegex(ValueError, "foreign or ambiguous container"):
                        _admit_runtime_volumes(project, plan, "example")
                finally:
                    project.command("rm", "--force", foreign)
                for role in project.completed_roles:
                    self.assertEqual(records[role]["State"]["ExitCode"], 0)
                script = "import os; print(os.stat('/run/secs/worker/.owner-session.lock').st_ino)"
                provider_inode = project.command("exec", records["provider"]["Id"], "python", "-I", "-c", script)
                worker_inode = project.command("exec", records["worker"]["Id"], "python", "-I", "-c", script)
                self.assertEqual(provider_inode, worker_inode)
                project.stop()
                records = project.start(plan)
                self.assertEqual(provider_inode, project.command("exec", records["worker"]["Id"], "python", "-I", "-c", script))
                project.stop()
            finally:
                for record in project.inventory().values():
                    project.command("rm", "--force", record["Id"])
                present = project.command("volume", "ls", "--quiet").decode().splitlines()
                for volume in volumes:
                    if volume in present:
                        project.command("volume", "rm", volume)

    def test_runtime_volume_initializers_preserve_owned_contents_and_lock(self):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        with lifecycle_workspace() as temporary:
            project = ComposeProject(Path(temporary), "volume-test-" + uuid4().hex[:12], ())
            sources = project.name + "-sources"
            session = project.name + "-session"
            created = []
            try:
                for volume in (sources, session):
                    project.command("volume", "create", volume)
                    created.append(volume)
                mounts = ("--mount", f"type=volume,src={sources},dst=/sources",
                          "--mount", f"type=volume,src={session},dst=/session")
                root = ("run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                        "--cap-add", "CHOWN", "--user", "0:0", *mounts, "--entrypoint", "python", image,
                        "-P", "-m", "secs_inference.provider.volume_init", "sources", str(os.getuid()), str(os.getgid()))
                owner = ("run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                         "--user", f"{os.getuid()}:{os.getgid()}", *mounts, "--entrypoint", "python", image)
                project.command(*root)
                project.command(*owner, "-P", "-m", "secs_inference.provider.volume_init", "session")
                script = "import os; from pathlib import Path; p=Path('/session/.owner-session.lock'); print(p.stat().st_ino); Path('/sources/retained').touch()"
                inode = project.command(*owner, "-I", "-c", script).strip()
                project.command(*root)
                project.command(*owner, "-P", "-m", "secs_inference.provider.volume_init", "session")
                self.assertEqual(inode, project.command(*owner, "-I", "-c", script).strip())
                project.command(*owner, "-I", "-c", "import os; os.chmod('/sources', 0o755)")
                with self.assertRaises(RuntimeError):
                    project.command(*root)
            finally:
                for volume in created:
                    project.command("volume", "rm", volume)

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
            (config / "worker.toml").write_text('device = "cuda:0"\n')
            (config / "worker.toml").chmod(0o600)
            (config / "network.toml").write_text('mode = "standard"\n')
            (config / "network.toml").chmod(0o600)
            env = config / "deployment.env"
            env.write_text(f"PROVIDER_IMAGE_REF={image}\nWORKER_IMAGE_REF={image}\n"
                           f"CHECKPOINT_DIRECTORY={root}/checkpoint\n"
                           f"MOLFORMER_CACHE_DIRECTORY={root}/cache\n")
            env.chmod(0o600)
            shared_fixture(root, config)
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
            credential = provider_mounts["/run/secrets/provider/provider.signing.private.json"]
            self.assertTrue(credential["read_only"])
            self.assertNotIn("/run/secrets/provider/provider.signing.private.json", worker_mounts)
            self.assertTrue(provider_mounts["/run/secs/worker"]["read_only"])
            self.assertTrue(provider_mounts["/run/nmr-api-proxy"]["read_only"])
            self.assertTrue(provider_mounts["/run/nmr-api-outbox"]["read_only"])
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
            (config / "worker.toml").write_text('device = "cpu"\n')
            (root / "config/host.toml").unlink()
            cpu_plan = render_deployment(root, "example")
            self.assertNotIn("deploy", cpu_plan["services"]["worker"])
            self.assertEqual(cpu_plan["services"]["worker"]["network_mode"], "none")
            (config / "worker.toml").write_text('device = "cpu"\nmock_index = true\n')
            mock_plan = render_deployment(root, "example")
            self.assertEqual(mock_plan["services"]["provider"]["environment"]["SECS_MOCK_INDEX"], "1")
            self.assertEqual(mock_plan["services"]["worker"]["environment"]["SECS_MOCK_INDEX"], "1")
            self.assertIn("secs_inference.provider.mock_index_worker", " ".join(mock_plan["services"]["worker"]["command"]))

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
            (config / "worker.toml").write_text('device = "cuda:0"\n')
            (config / "worker.toml").chmod(0o600)
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
            shared_fixture(root, config)
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
