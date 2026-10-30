"""Retire a disposable legacy API route while preserving a borrowed volume."""

import os
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import uuid4
import unittest

from deployment.compose import ComposeProject
from deployment.provider_deployment import _add_mount_grants, _sdk_projection, retire_old_api
from test_lifecycle import lifecycle_workspace


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


def _apply_api_route(plan, project, config, definition, binding_name):
    """Merge SDK-owned API routing while keeping the scientific service isolated."""
    provider = plan["services"]["provider"]
    route = _sdk_projection(
        project, provider["image"], definition, "provider_core.deployment_route", "render",
        "--definition", definition, "--binding", binding_name, "--image", provider["image"],
        "--interpreter", "/usr/local/bin/python", "--namespace", project.name, "--service-name", "api",
        "--uid", str(os.getuid()), "--gid", str(os.getgid()), "--named-volume",
    )
    if type(route) is not dict or set(route) != {"compose", "client_socket", "client_mount", "client_depends_on"}:
        raise ValueError("Installed SDK did not return its complete API route grants.")
    for section, grants in route["compose"].items():
        occupied = plan.setdefault(section, {})
        if set(occupied) & set(grants):
            raise ValueError("Shared API route roles overlap the existing deployment; no route grants were applied.")
    _add_mount_grants(provider, (route["client_mount"],))
    for section, grants in route["compose"].items():
        plan[section].update(grants)
    provider.setdefault("depends_on", {}).update(route["client_depends_on"])
    provider.setdefault("environment", {})["SECS_API_PROXY_SOCKET"] = route["client_socket"]


class OldRouteRetirementTests(unittest.TestCase):
    def test_legacy_route_retirement_retains_borrowed_storage(self):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        with lifecycle_workspace() as temporary:
            root = Path(temporary)
            name = "secs-old-" + uuid4().hex[:12]
            config = root / "config"
            config.mkdir(mode=0o700)
            project = ComposeProject(root, name, ("provider", "api", "api-init"), ("api-init",))
            binding_fixture(root, config, uid=os.getuid(), gid=os.getgid())
            definition = root / "bindings.toml"
            borrowed = name + "-borrowed"
            route_volume = None
            try:
                project.command("volume", "create", borrowed)
                provider = {"image": image, "pull_policy": "never", "init": True, "restart": "unless-stopped",
                            "user": f"{os.getuid()}:{os.getgid()}", "network_mode": "none", "read_only": True,
                            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                            "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777"],
                            "entrypoint": ["python", "-I", "-c",
                                           "import signal,time; signal.signal(signal.SIGTERM, lambda *_: exit(0)); open('/tmp/ready','w').close(); time.sleep(300)"],
                            "volumes": [{"type": "volume", "source": "borrowed", "target": "/borrowed",
                                         "read_only": True, "volume": {"nocopy": True}}]}
                plan = {"services": {"provider": provider},
                        "volumes": {"borrowed": {"name": borrowed, "external": True}}}
                _apply_api_route(plan, project, config, str(definition), "secs")
                route_volume = next(key for key in plan["volumes"] if key != "borrowed")
                records = project.start(plan)
                self.assertTrue(records["provider"]["State"]["Running"])
                deadline = monotonic() + 15
                while project.command("exec", records["provider"]["Id"], "python", "-I", "-c",
                                      "import os; print(os.path.isfile('/tmp/ready'))").strip() != b"True":
                    self.assertLess(monotonic(), deadline, "Legacy provider did not become ready")
                    sleep(0.05)
                retire_old_api(project, config)
                self.assertEqual(project.inventory(), {})
                existing = project.command("volume", "ls", "--quiet").decode().splitlines()
                self.assertNotIn(route_volume, existing)
                self.assertIn(borrowed, existing)
            finally:
                for record in project.inventory().values():
                    project.command("rm", "--force", record["Id"])
                existing = project.command("volume", "ls", "--quiet").decode().splitlines()
                for volume in (route_volume, borrowed):
                    if volume is not None and volume in existing:
                        project.command("volume", "rm", volume)
