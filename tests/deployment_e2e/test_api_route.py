"""One actual signed provider process uses only its private shared API route."""

import json
import os
from pathlib import Path
import ssl
from time import monotonic, sleep
import unittest
from urllib.request import urlopen
from uuid import uuid4

from deployment.compose import ComposeProject
from deployment.provider_deployment import _apply_api_route, retire_old_api
from test_lifecycle import binding_fixture, lifecycle_workspace


class ApiRouteProcessTests(unittest.TestCase):
    def wait_for(self, predicate, message, *, seconds=15):
        deadline = monotonic() + seconds
        while True:
            result = predicate()
            if result:
                return result
            self.assertLess(monotonic(), deadline, message)
            sleep(0.05)

    def test_signed_main_uses_private_route_and_recovers_without_direct_fallback(self):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        source = Path(__file__).resolve().parents[2]
        with lifecycle_workspace() as temporary:
            root = Path(temporary)
            name = "route-" + uuid4().hex[:12]
            project = ComposeProject(root, "secs-" + name, ("provider", "api", "api-init"), ("api-init",))
            peer, config = root / "peer", root / "config/deployments" / name
            state = root / "state"
            for directory in (peer, config, state):
                directory.mkdir(mode=0o700, parents=True)
            peer_id = root / "peer.cid"
            route_volume = None
            shared_volume = project.name + "-shared-test"
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
                credential = binding_fixture(root, config, origin=origin, credential=peer / "credential.json",
                                             ca_file=peer / "ca.pem", uid=os.getuid(), gid=os.getgid())
                provider_config = config / "provider.toml"
                provider_config.write_text('schema_id="secs.provider.config.v2"\n' + provider_config.read_text() + '''
[hello]
display_name="SECS route qualification"
provider_description="Synthetic signed route proof"
publication_interval_seconds=0.2
retry_initial_seconds=0.1
''')
                definition = root / "bindings.toml"
                mounts = [{"type": "bind", "source": str(config), "target": "/run/config/provider", "read_only": True},
                          {"type": "bind", "source": str(state), "target": "/state"}]
                mounts.extend({"type": "bind", "source": str(path), "target": str(path), "read_only": True}
                              for path in (definition, credential, peer / "ca.pem"))
                provider = {"image": image, "pull_policy": "never", "init": True, "restart": "unless-stopped",
                            "user": f"{os.getuid()}:{os.getgid()}", "network_mode": "none", "read_only": True,
                            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                            "pids_limit": 32, "cpus": 1, "mem_limit": "256m", "memswap_limit": "256m",
                            "stop_grace_period": "1s", "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777"],
                            "volumes": mounts}
                plan = {"services": {"provider": provider}}
                _apply_api_route(plan, project, config, str(definition), "secs")
                self.assertEqual(len(plan["volumes"]), 1)
                route_volume = next(iter(plan["volumes"]))
                project.command("volume", "create", shared_volume)
                plan["volumes"]["shared-test"] = {"external": True, "name": shared_volume}
                provider["volumes"].append({"type": "volume", "source": "shared-test", "target": "/shared-test",
                                            "read_only": True, "volume": {"nocopy": True}})
                records = project.start(plan)
                self.assertEqual(records["api-init"]["State"]["ExitCode"], 0)
                self.assertEqual(records["api-init"]["State"]["Status"], "exited")
                provider_id = records["provider"]["Id"]
                proxy = records["api"]
                self.assertEqual(records["provider"]["HostConfig"]["NetworkMode"], "none")
                socket_path = provider["environment"]["SECS_API_PROXY_SOCKET"]
                mount = next(mount for mount in records["provider"]["Mounts"]
                             if mount["Destination"] == str(Path(socket_path).parent))
                self.assertFalse(mount["RW"])
                self.assertEqual(mount["Name"], route_volume)
                self.assertFalse(any(mount["Source"] == str(credential) for mount in proxy["Mounts"]))
                def observed():
                    captured = json.loads((peer / "capture.json").read_text())
                    self.assertIsNone(captured["error"], "Synthetic peer rejected the actual signature or SNI")
                    return captured
                self.wait_for(lambda: observed()["hellos"] > 0, "Provider did not send an authenticated Hello")
                wrong_uid = 65532 if os.getuid() != 65532 else 65533
                project.command(
                    "run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true", "--user", f"{wrong_uid}:{os.getgid()}",
                    "--pids-limit", "16", "--memory", "64m", "--memory-swap", "64m", "--cpus", "1",
                    "--mount", f"type=volume,src={route_volume},dst=/route,readonly", "--entrypoint", "python", image, "-I", "-c",
                    "import errno,socket; s=socket.socket(socket.AF_UNIX);\n"
                    "try: s.connect('/route/proxy.sock')\n"
                    "except PermissionError as e: assert e.errno in (errno.EACCES,errno.EPERM)\n"
                    "else: raise AssertionError('Wrong UID received a private route grant')",
                )
                project.command("stop", "--signal", "SIGTERM", "--timeout", "-1", proxy["Id"], timeout=None)
                context = ssl.create_default_context(cafile=str(peer / "ca.pem"))
                with urlopen(origin + "/health", context=context, timeout=2) as health:
                    self.assertEqual(health.status, 200)
                sleep(0.2)
                count = observed()["hellos"]
                sleep(0.7)
                self.assertEqual(observed()["hellos"], count, "Provider bypassed its unavailable proxy")
                held = project.inventory()["provider"]
                self.assertEqual(held["Id"], provider_id)
                self.assertTrue(held["State"]["Running"])
                self.assertEqual(held["RestartCount"], 0)
                project.command("start", proxy["Id"])
                self.wait_for(lambda: observed()["hellos"] > count, "Provider did not recover its existing route")
                retire_old_api(project, config)
                self.assertEqual(project.inventory(), {})
                volumes = project.command("volume", "ls", "--quiet").decode().splitlines()
                self.assertNotIn(route_volume, volumes)
                self.assertIn(shared_volume, volumes)
            except BaseException as failure:
                try:
                    project.logs()
                except BaseException as diagnostics:
                    raise BaseExceptionGroup("Route qualification and log capture failed", (failure, diagnostics)) from None
                raise
            finally:
                try:
                    for record in project.inventory().values():
                        project.command("rm", "--force", record["Id"])
                    volumes = project.command("volume", "ls", "--quiet").decode().splitlines()
                    if route_volume is not None and route_volume in volumes:
                        project.command("volume", "rm", route_volume)
                    if shared_volume in volumes:
                        project.command("volume", "rm", shared_volume)
                finally:
                    if peer_id.exists():
                        identity = peer_id.read_text().strip()
                        self.assertRegex(identity, r"^[a-f0-9]{64}$")
                        project.command("rm", "--force", identity)
