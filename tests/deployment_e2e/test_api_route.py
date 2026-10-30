"""Exercise the provider through a disposable shared proxy and socket grants."""

import json
import os
from pathlib import Path
import shutil
from time import monotonic, sleep
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

from deployment.compose import ComposeProject
from deployment.provider_deployment import _apply_shared_grants
from test_lifecycle import lifecycle_workspace, shared_fixture


class ApiRouteProcessTests(unittest.TestCase):
    def wait_for(self, predicate, message, *, seconds=15):
        deadline = monotonic() + seconds
        while True:
            result = predicate()
            if result:
                return result
            self.assertLess(monotonic(), deadline, message)
            sleep(0.05)

    def test_signed_main_uses_shared_route_without_direct_fallback(self):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        source = Path(__file__).resolve().parents[2]
        with lifecycle_workspace() as temporary:
            root = Path(temporary)
            name = "secs-route-" + uuid4().hex[:12]
            project = ComposeProject(root, name, ("provider",))
            peer, config, state = root / "peer", root / "config", root / "state"
            for directory in (peer, config, state):
                directory.mkdir(mode=0o700)
            peer_id, proxy_id = root / "peer.cid", root / "proxy.cid"
            volumes = [name + "-proxy-socket", name + "-outbox-socket"]
            try:
                project.command(
                    "run", "--detach", "--init", "--pull", "never", "--cidfile", str(peer_id),
                    "--network", "host", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true", "--user", f"{os.getuid()}:{os.getgid()}",
                    "--pids-limit", "32", "--cpus", "1", "--memory", "256m", "--memory-swap", "256m",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777", "--env", "PYTHONPATH=/tests",
                    "--mount", f"type=bind,src={peer},dst=/fixture",
                    "--mount", f"type=bind,src={source}/tests/deployment_e2e/route_peer.py,dst=/tests/route_peer.py,readonly",
                    "--mount", f"type=bind,src={source}/tests/e2e/tls_fixture.py,dst=/tests/tls_fixture.py,readonly",
                    "--entrypoint", "python", image, "/tests/route_peer.py", "/fixture",
                )
                ready = self.wait_for(lambda: (peer / "ready.json").exists() and json.loads(
                    (peer / "ready.json").read_text()), "Synthetic TLS peer did not become ready")
                origin = ready["origin"]
                shared_fixture(root, config, origin=origin, credential=peer / "credential.json",
                               ca_file=peer / "ca.pem", gid=os.getgid(), project=name)
                shutil.copyfile(peer / "credential.json", state / "provider.signing.private.json")
                (state / "provider.signing.private.json").chmod(0o600)
                shutil.copyfile(peer / "ca.pem", config / "api-ca.crt")
                (config / "api-ca.crt").chmod(0o600)
                provider_config = config / "provider.toml"
                with provider_config.open("a") as stream:
                    stream.write('''\n[hello]
display_name="SECS route qualification"
provider_description="Synthetic signed route proof"
publication_interval_seconds=0.2
retry_initial_seconds=0.1
''')
                for volume in volumes:
                    project.command("volume", "create", volume)
                project.command(
                    "run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--cap-add", "CHOWN", "--user", "0:0", "--mount", f"type=volume,src={volumes[0]},dst=/route",
                    "--entrypoint", "python", image, "-I", "-m", "provider_core.deployment_route", "initialize",
                    "--uid", str(os.getuid()), "--gid", str(os.getgid()), "--shared-group",
                )
                parsed = urlsplit(origin)
                project.command(
                    "run", "--detach", "--init", "--pull", "never", "--cidfile", str(proxy_id), "--network", "host",
                    "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                    "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", f"/run:rw,nosuid,nodev,noexec,size=16m,mode=0700,uid={os.getuid()},gid={os.getgid()}",
                    "--mount", f"type=volume,src={volumes[0]},dst=/route", "--entrypoint", "python", image,
                    "-I", "-m", "provider_core.provider_proxy", "--mode", "mapped", "--interface", "lo",
                    "--authority", f"{parsed.hostname}:{parsed.port or 443}", "--address", f"{parsed.hostname}=127.0.0.1",
                    "--log-level", "INFO", "--socket", "/route/proxy.sock", "--ready-file", "/route/ready",
                    "--status-directory", "/run", "--socket-group-id", str(os.getgid()),
                )
                proxy = proxy_id.read_text().strip()
                def proxy_ready():
                    return project.command("exec", proxy, "python", "-I", "-c",
                                           "import os; print(os.path.isfile('/route/ready'))").strip() == b"True"
                self.wait_for(proxy_ready, "Synthetic shared proxy did not become ready")
                provider = {"image": image, "pull_policy": "never", "init": True, "restart": "unless-stopped",
                            "user": f"{os.getuid()}:{os.getgid()}", "network_mode": "none", "read_only": True,
                            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                            "pids_limit": 32, "cpus": 1, "mem_limit": "256m", "memswap_limit": "256m",
                            "stop_grace_period": "1s", "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777"],
                            "volumes": [{"type": "bind", "source": str(config), "target": "/run/config/provider", "read_only": True},
                                        {"type": "bind", "source": str(state), "target": "/state"}]}
                plan = {"services": {"provider": provider}}
                _apply_shared_grants(plan, project, config, state)
                records = project.start(plan)
                provider_id = records["provider"]["Id"]
                self.assertEqual(records["provider"]["HostConfig"]["NetworkMode"], "none")
                for target, volume in zip(("/run/nmr-api-proxy", "/run/nmr-api-outbox"), volumes):
                    mount = next(item for item in records["provider"]["Mounts"] if item["Destination"] == target)
                    self.assertFalse(mount["RW"])
                    self.assertEqual(mount["Name"], volume)
                def observed():
                    captured = json.loads((peer / "capture.json").read_text())
                    self.assertIsNone(captured["error"], "Synthetic peer rejected the actual signature or SNI")
                    return captured
                self.wait_for(lambda: observed()["hellos"] > 0, "Provider did not send an authenticated Hello")
                project.command("stop", "--signal", "SIGTERM", "--timeout", "15", proxy)
                sleep(0.2)
                count = observed()["hellos"]
                sleep(0.7)
                self.assertEqual(observed()["hellos"], count, "Provider bypassed its unavailable proxy")
                held = project.inventory()["provider"]
                self.assertEqual(held["Id"], provider_id)
                self.assertTrue(held["State"]["Running"])
                self.assertEqual(held["RestartCount"], 0)
                project.command("start", proxy)
                self.wait_for(lambda: observed()["hellos"] > count, "Provider did not recover its shared route")
                project.stop()
                existing = project.command("volume", "ls", "--quiet").decode().splitlines()
                self.assertTrue(all(volume in existing for volume in volumes))
            except BaseException as failure:
                try:
                    project.logs()
                except BaseException as diagnostics:
                    raise BaseExceptionGroup("Shared route qualification and log capture failed", (failure, diagnostics)) from None
                raise
            finally:
                try:
                    for record in project.inventory().values():
                        project.command("rm", "--force", record["Id"])
                finally:
                    try:
                        if proxy_id.exists():
                            identity = proxy_id.read_text().strip()
                            self.assertRegex(identity, r"^[a-f0-9]{64}$")
                            project.command("rm", "--force", identity)
                    finally:
                        if peer_id.exists():
                            identity = peer_id.read_text().strip()
                            self.assertRegex(identity, r"^[a-f0-9]{64}$")
                            project.command("rm", "--force", identity)
                existing = project.command("volume", "ls", "--quiet").decode().splitlines()
                for volume in volumes:
                    if volume in existing:
                        project.command("volume", "rm", volume)
