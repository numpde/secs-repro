"""Bind named SECS deployments to their configuration, secrets, and attempt state.

Maintained examples supply defaults; Compose owns the service recipe.
This adapter supplies SECS paths and serializes deployment mutations.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib

from deployment.compose import ComposeProject
from deployment.worker_target import worker_target
from deployment.templates import (
    configuration_directory, initialize_configuration, _ensure_private_parent,
    _locked_parent, _publish_new_file, _sync_directory,
)
from deployment.rootless_access import (
    grant_private_access, mapped_acl_permissions, mapped_ids, private_acl, private_owner_acl,
    set_mapped_access,
)


TEMPLATES = {
    "provider.toml": Path("config/provider.toml.example"),
    "worker.toml": Path("config/worker.toml.example"),
    "worker.mock-index.toml": Path("config/worker.mock-index.toml.example"),
    "deployment.env": Path("config/deployment.env.example"),
    "network.toml": Path("config/network.toml.example"),
}
_GPU_UUID = re.compile(
    r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
def _toml_document(path: Path) -> dict:
    """Read one strict private TOML table without exposing its contents."""
    try:
        document = tomllib.loads(_private_file(path).decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Deployment input is not valid TOML: {path}") from error
    return document


def _host_gpu_uuid(repository: Path) -> str:
    """Read the host's one GPU selection; deployments cannot override it."""
    path = repository.resolve(strict=True) / "config/host.toml"
    document = _toml_document(path)
    if set(document) != {"worker_gpu_uuid"}:
        raise ValueError("config/host.toml must contain only worker_gpu_uuid.")
    identifier = document["worker_gpu_uuid"]
    if type(identifier) is not str or _GPU_UUID.fullmatch(identifier) is None:
        raise ValueError("config/host.toml worker_gpu_uuid must be one GPU UUID, not an index or all GPUs.")
    return identifier


def _provider_network(repository: Path, name: str) -> dict:
    """Read one closed provider-network policy before contacting Docker."""
    path = configuration_directory(repository, name) / "network.toml"
    document = _toml_document(path)
    mode = document.get("mode")
    if mode == "standard" and set(document) == {"mode"}:
        return document
    if mode == "forwarded-services" and set(document) == {"mode", "host_address"}:
        address = document["host_address"]
        try:
            parsed = ipaddress.IPv4Address(address) if type(address) is str else None
        except ipaddress.AddressValueError:
            parsed = None
        if (parsed is not None and not parsed.is_unspecified and not parsed.is_loopback
                and not parsed.is_multicast and not parsed.is_link_local):
            return document
    raise ValueError(
        "network.toml must select exactly mode='standard', or mode='forwarded-services' "
        "with one unicast IPv4 host_address."
    )


def _apply_provider_network(plan: dict, name: str, network: dict) -> None:
    """Grant forwarded interpreter/store access independently of the shared API route."""
    if network["mode"] == "standard":
        return
    provider = plan["services"]["provider"]
    if "extra_hosts" in provider:
        raise ValueError("The base provider recipe already defines host aliases.")
    provider["extra_hosts"] = [f"nmr.localhost={network['host_address']}"]
    expected = {"provider-egress"}
    if set(plan.get("networks", {})) != expected:
        raise ValueError("The provider recipe network set does not match the controller contract.")
    plan["networks"]["provider-egress"] = {
        "external": True,
        "name": f"secs-{name}-egress",
    }


def _admit_provider_network(project: ComposeProject, repository: Path, name: str, network: dict) -> None:
    """Admit the externally owned bridge for configured interpreter/store access."""
    if network["mode"] == "standard":
        return
    expected_name = f"secs-{name}-egress"
    try:
        records = json.loads(project.command("network", "inspect", expected_name))
    except json.JSONDecodeError as error:
        raise ValueError("Docker returned malformed external network inspection.") from error
    if type(records) is not list or len(records) != 1 or type(records[0]) is not dict:
        raise ValueError("Docker did not inspect exactly one external provider network.")
    record = records[0]
    labels = record.get("Labels")
    if record.get("Name") != expected_name:
        raise ValueError(
            f"Cannot use the inspected external network named {record.get('Name')!r}; "
            f"this deployment requires {expected_name!r}."
        )
    if record.get("Driver") != "bridge" or record.get("Internal") is not False:
        raise ValueError(
            f"Cannot use external network {expected_name!r} with driver={record.get('Driver')!r} "
            f"and internal={record.get('Internal')!r}; a non-internal bridge is required."
        )
    required_labels = {
        "io.secs-repro.checkout": str(repository.resolve()),
        "io.secs-repro.deployment": name,
    }
    if type(labels) is not dict:
        raise ValueError(f"Cannot use external network {expected_name!r}; its ownership labels are malformed.")
    for key, expected in required_labels.items():
        if labels.get(key) != expected:
            raise ValueError(
                f"Cannot use external network {expected_name!r}; label {key!r} is "
                f"{labels.get(key)!r}, but this deployment requires {expected!r}."
            )
    ipam = record.get("IPAM")
    configurations = ipam.get("Config") if type(ipam) is dict else None
    if type(configurations) is not list or len(configurations) != 1 or type(configurations[0]) is not dict:
        raise ValueError("External provider network must have exactly one IPAM configuration.")
    configuration = configurations[0]
    try:
        subnet = ipaddress.IPv4Network(configuration.get("Subnet"))
        gateway = ipaddress.IPv4Address(configuration.get("Gateway"))
        host = ipaddress.IPv4Address(network["host_address"])
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, TypeError) as error:
        raise ValueError("External provider network has invalid IPv4 routing metadata.") from error
    if gateway != host:
        try:
            bridge = json.loads(project.command("network", "inspect", "bridge"))
            default = bridge[0]
            bridge_config = default["IPAM"]["Config"]
            valid_default = (type(bridge) is list and len(bridge) == 1 and default.get("Name") == "bridge"
                             and default.get("Driver") == "bridge" and type(bridge_config) is list
                             and len(bridge_config) == 1
                             and ipaddress.IPv4Address(bridge_config[0]["Gateway"]) == host)
        except (KeyError, TypeError, IndexError, ValueError, json.JSONDecodeError):
            valid_default = False
        if not valid_default:
            raise ValueError("Cannot use the forwarded-services host address: it must be this network's gateway or the inspected default bridge gateway.")
    elif host not in subnet:
        raise ValueError("Cannot use the forwarded-services host address outside its network subnet.")
    members = record.get("Containers")
    if type(members) is not dict:
        raise ValueError(f"Cannot use external network {expected_name!r}; its container membership is malformed.")
    inventory = project.inventory()
    allowed_members = ({inventory["provider"]["Id"]} if "provider" in inventory else set())
    foreign_members = set(members) - allowed_members
    if foreign_members:
        raise ValueError(
            f"Cannot use external network {expected_name!r}; it has {len(foreign_members)} "
            "container endpoint(s) not owned by this deployment."
        )


def render_deployment(repository: Path, name: str, mock_index_selection: bool | None = None) -> dict:
    """Project SECS paths and operator artifact selections into the existing recipe.

    Compose owns dotenv parsing. Framework-owned paths and the nonroot host
    identity take precedence over that file; the shell environment is ignored.
    No runtime directories are created by rendering.
    """
    config = configuration_directory(repository, name)
    _private_directory(config)
    _private_file(config / "deployment.env")
    network = _provider_network(repository, name)
    return _render_deployment(repository, name, config, network, mock_index_selection)


def _render_deployment(repository: Path, name: str, config: Path, network: dict, mock_index_selection: bool | None = None) -> dict:
    """Render one deployment from an already validated network policy."""
    worker_config = _toml_document(config / "worker.toml")
    worker_device = worker_config.get("device", "cuda:0")
    mock_index = worker_config.get("mock_index", False)
    mock_runner = worker_config.get("mock_runner", False)
    if type(mock_index) is not bool or type(mock_runner) is not bool:
        raise ValueError("Worker mock index and mock runner settings must be boolean.")
    worker_config_file = config / "worker.toml"
    if mock_index_selection is True:
        if worker_device != "cpu" or mock_runner is not False:
            raise ValueError("USE_MOCK_INDEX=yes requires a CPU scientific worker, not a mock runner.")
        if mock_index is not True:
            worker_config_file = config / "worker.mock-index.toml"
            worker_config = _toml_document(worker_config_file)
            worker_device = worker_config.get("device")
            mock_index = worker_config.get("mock_index")
            if worker_device != "cpu" or mock_index is not True:
                raise ValueError("USE_MOCK_INDEX=yes requires worker.mock-index.toml to select a CPU mock index.")
    elif mock_index_selection is False and mock_index is True:
        raise ValueError("USE_MOCK_INDEX=no conflicts with the saved mock-index worker configuration.")
    if type(mock_runner) is not bool or (mock_runner and (worker_device != "cpu" or mock_index is not False)):
        raise ValueError("A mock runner requires device=cpu and cannot use a mock index.")
    if mock_runner and set(worker_config) != {"device", "mock_runner"}:
        raise ValueError("The mock runner accepts only device and mock_runner.")
    if type(mock_index) is not bool or (mock_index and worker_device != "cpu"):
        raise ValueError("A mock index requires mock_index=true and device=cpu.")
    if mock_index and set(worker_config) != {"device", "mock_index"}:
        raise ValueError("The mock index worker accepts only device and mock_index; its search settings are fixed.")
    if worker_device not in ("cpu", "cuda:0"):
        raise ValueError("Worker device must be cpu or cuda:0 for this deployment.")
    gpu_uuid = _host_gpu_uuid(repository) if worker_device == "cuda:0" else None
    state = repository / "secrets/deployments" / name
    if os.getuid() == 0 or os.getgid() == 0:
        raise ValueError("SECS deployment requires a nonroot host UID and GID.")
    environment = {
        "PROVIDER_UID": str(os.getuid()), "PROVIDER_GID": str(os.getgid()),
        "SECS_DEPLOYMENT": name,
        "SECS_CHECKOUT": str(repository.resolve()),
        "PROVIDER_CONFIG_DIRECTORY": str(config),
        "INTERPRETER_KEY_FILE": str(state / "runtime/interpreter.key"),
        "WORKER_CONFIG_FILE": str(worker_config_file),
        "MOLFORMER_LOCK_FILE": str(repository / "molformer.lock.toml"),
        "CACHE_VERIFIER_FILE": str(repository / "tools/materialize_molformer_cache.py"),
    }
    if mock_runner:
        # Compose interpolation occurs before the unused scientific grants are removed.
        environment.update({"WORKER_IMAGE_REF": "sha256:" + "0" * 64,
                            "CHECKPOINT_DIRECTORY": "/", "MOLFORMER_CACHE_DIRECTORY": "/"})
    project = _project(repository, name)
    base_recipe = ComposeProject(repository, project.name, ("provider", "worker", "sources-init", "session-init"))
    plan = base_recipe.render(
        repository / "compose/provider.yml", config / "deployment.env", environment,
    )
    _apply_provider_network(plan, name, network)
    _apply_shared_grants(plan, project, config, state)
    worker = plan["services"]["worker"]
    worker.setdefault("environment", {}).update({"SECS_WORKER_DEVICE": worker_device,
                                                "SECS_MOCK_INDEX": "1" if mock_index else "0",
                                                "SECS_MOCK_RUNNER": "1" if mock_runner else "0"})
    provider = plan["services"]["provider"]
    provider.setdefault("environment", {}).update({"SECS_MOCK_INDEX": "1" if mock_index else "0",
                                                   "SECS_MOCK_RUNNER": "1" if mock_runner else "0"})
    if mock_runner:
        for role in ("provider", "worker"):
            plan["services"][role].setdefault("labels", {})["io.secs.mock-runner"] = "1"
        provider["volumes"] = [mount for mount in provider["volumes"]
                               if mount["target"] != "/run/secrets/provider/interpreter.key"]
        worker["image"] = provider["image"]
        worker["volumes"] = [mount for mount in worker["volumes"]
                             if mount["target"] not in {"/checkpoint", "/cache", "/input/molformer.lock.toml", "/opt/materialize.py"}]
        worker["command"] = ["mkdir -m 700 /tmp/native-config /tmp/native-scratch /tmp/native-status && "
                             "python -I -m secs_inference.provider.worker_config_snapshot && "
                             "exec python -I -m provider_core.runner --role secs "
                             "--socket /run/secs/worker/worker.sock --scratch /tmp/native-scratch "
                             "--status-directory /tmp/native-status --owner-session-lifetime "
                             "--startup-seconds 2 --terminate-seconds 2 --kill-seconds 2 --cleanup-seconds 2 "
                             "-- python -I -m secs_inference.provider.mock_runner_worker"]
    if mock_index:
        for role in ("provider", "worker"):
            plan["services"][role].setdefault("labels", {})["io.secs.mock-index"] = "eight-molecule"
        command = worker["command"]
        if type(command) is not list or len(command) != 1 or command[0].count("secs_inference.provider.worker_model") != 1:
            raise ValueError("Cannot select mock index: the worker command differs from the maintained recipe.")
        command[0] = command[0].replace("secs_inference.provider.worker_model", "secs_inference.provider.mock_index_worker")
    if gpu_uuid is not None:
        plan["services"]["worker"]["deploy"] = {"resources": {"reservations": {"devices": [
            {"driver": "nvidia", "device_ids": [gpu_uuid], "capabilities": ["gpu"]}
        ]}}}
    return plan


def _admit_worker_image(project: ComposeProject, plan: dict) -> None:
    """Match the selected hardware grant to the built scientific image variant."""
    worker = plan["services"]["worker"]
    expected = "gpu" if "deploy" in worker else "cpu"
    records = json.loads(project.command("image", "inspect", worker["image"]))
    if (type(records) is not list or len(records) != 1 or type(records[0]) is not dict
            or records[0].get("Id") != worker["image"]):
        raise ValueError("Cannot start worker: Docker did not inspect the selected image identity.")
    config = records[0].get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    if worker.get("environment", {}).get("SECS_MOCK_RUNNER") == "1":
        if (worker["image"] != plan["services"]["provider"]["image"] or type(labels) is not dict
                or labels.get("io.secs-repro.provider.mock-runner") != "1"):
            raise ValueError("Cannot start mock runner: the provider image lacks its mock-runner contract.")
        return
    if (type(labels) is not dict or labels.get("io.secs-repro.packages.variant") != expected
            or labels.get("io.secs-repro.worker.device-contract") != "explicit-v1"):
        raise ValueError(f"Cannot start worker: its image must be the SECS {expected} packages variant.")
    if worker.get("environment", {}).get("SECS_MOCK_INDEX") == "1":
        if labels.get("io.secs-repro.worker.mock-index") != "1":
            raise ValueError("Cannot start worker: its image does not include the mock-index worker.")
        provider_image = plan["services"]["provider"]["image"]
        providers = json.loads(project.command("image", "inspect", provider_image))
        if (type(providers) is not list or len(providers) != 1 or type(providers[0]) is not dict
                or providers[0].get("Id") != provider_image
                or type(providers[0].get("Config")) is not dict
                or type(providers[0]["Config"].get("Labels")) is not dict
                or providers[0]["Config"]["Labels"].get("io.secs-repro.provider.mock-index") != "1"):
            raise ValueError("Cannot start mock-index deployment: the provider image cannot label its offering and results.")


def _project(repository: Path, name: str) -> ComposeProject:
    """SECS stops Job admission before its scientific worker."""
    configuration_directory(repository, name)
    return ComposeProject(repository, f"secs-{name}",
                          ("provider", "worker", "sources-init", "session-init"),
                          ("sources-init", "session-init"))


def _old_project(repository: Path, name: str) -> ComposeProject:
    """Admit the former owned API roles for their explicit retirement."""
    configuration_directory(repository, name)
    return ComposeProject(repository, f"secs-{name}",
                          ("provider", "worker", "api", "sources-init", "session-init", "api-init"),
                          ("sources-init", "session-init", "api-init"))


def _old_route_volume(project: ComposeProject, proxy: dict, initializer: dict) -> str:
    """Identify the old route from the inspected proxy and initializer grants."""
    labels = proxy["Config"]["Labels"]
    key = labels.get("org.nmr-provider-tools.binding")
    if (labels.get("org.nmr-provider-tools.route") != project.name
            or labels.get("org.nmr-provider-tools.route-role") != "proxy"
            or type(key) is not str or re.fullmatch(r"[0-9a-f]{64}", key) is None):
        raise ValueError("Cannot retire the old API route: the proxy ownership labels differ.")
    init_labels = initializer["Config"]["Labels"]
    if (init_labels.get("org.nmr-provider-tools.route") != project.name
            or init_labels.get("org.nmr-provider-tools.route-role") != "initializer"
            or init_labels.get("org.nmr-provider-tools.binding") != key):
        raise ValueError("Cannot retire the old API route: the initializer ownership labels differ.")
    prefix = project.name if len(project.name) <= 63 else "r-" + hashlib.sha256(project.name.encode("ascii")).hexdigest()
    volume = prefix + "-route-" + key if len(project.name) <= 63 else prefix + "-" + key
    for container in (proxy, initializer):
        mounts = container.get("Mounts")
        if type(mounts) is not list or sum(
            type(m) is dict and m.get("Type") == "volume" and m.get("Name") == volume
            and m.get("Destination") == "/route" and m.get("RW") is True for m in mounts
        ) != 1:
            raise ValueError("Cannot retire the old API route: its inspected volume grants differ.")
    return volume


def _check_old_route_borrowers(project: ComposeProject, volume: str, owners: set[str]) -> bool:
    present = set(project.command("volume", "ls", "--quiet").decode("utf-8").splitlines())
    if volume not in present:
        return False
    inspected = json.loads(project.command("volume", "inspect", volume))
    labels = inspected[0].get("Labels") if type(inspected) is list and len(inspected) == 1 and type(inspected[0]) is dict else None
    if (type(inspected) is not list or len(inspected) != 1 or type(inspected[0]) is not dict
            or inspected[0].get("Name") != volume or type(labels) is not dict
            or labels.get("com.docker.compose.project") != project.name
            or labels.get("com.docker.compose.volume") != volume):
        raise ValueError("Cannot retire the old API volume: its Compose ownership labels differ.")
    attached = project.command("ps", "--all", "--no-trunc", "--quiet", "--filter",
                               f"volume={volume}").decode("ascii").splitlines()
    if len(attached) != len(set(attached)) or not set(attached) <= owners:
        raise ValueError("Cannot retire the old API volume: another container still borrows it.")
    return True


def retire_old_api(project: ComposeProject, config: Path) -> None:
    """Stop the old graph and retire its inspected API route, retaining retry evidence."""
    receipt = config / "old-api-retirement.json"
    if receipt.exists():
        record = json.loads(_private_file(receipt))
    else:
        initial = project.inventory()
        if not {"provider", "api", "api-init"} <= set(initial):
            raise ValueError("Cannot retire the old API route: the owned provider and both old proxy roles must be present.")
        volume = _old_route_volume(project, initial["api"], initial["api-init"])
        provider = initial["provider"]
        if provider["State"]["Running"]:
            project.command("stop", "--signal", "SIGTERM", "--timeout", "-1", provider["Id"], timeout=None)
        observed = project.inventory().get("provider")
        if (observed is None or observed["Id"] != provider["Id"]
                or observed["State"]["Running"] or observed["State"].get("ExitCode") != 0
                or observed["State"].get("OOMKilled") is not False):
            raise ValueError("Cannot retire the old API route: provider shutdown did not confirm a clean drain; its dependencies remain available.")
        owners = {initial[role]["Id"] for role in ("provider", "api", "api-init")}
        if not _check_old_route_borrowers(project, volume, owners):
            raise ValueError("Cannot retire the old API route: its inspected volume is absent.")
        stopped = project.stop()
        if ("provider" not in stopped or stopped["provider"]["Id"] != provider["Id"]
                or stopped["provider"]["State"].get("ExitCode") != 0
                or stopped["provider"]["State"].get("OOMKilled") is not False):
            raise ValueError("Cannot retire the old API route: the drained provider changed or its final state is unconfirmed.")
        if any(role not in stopped or stopped[role]["Id"] != initial[role]["Id"]
               for role in ("api", "api-init")):
            raise ValueError("Cannot retire the old API route: its inspected proxy roles changed during drain.")
        owned = {role: stopped[role]["Id"] for role in ("provider", "api", "api-init") if role in stopped}
        record = {"project": project.name, "volume": volume, "containers": owned}
        _publish_new_file(receipt, json.dumps(record, sort_keys=True).encode())
    if (type(record) is not dict or set(record) != {"project", "volume", "containers"}
            or record["project"] != project.name or type(record["volume"]) is not str
            or type(record["containers"]) is not dict
            or not set(record["containers"]) <= {"provider", "api", "api-init"}
            or any(type(v) is not str or re.fullmatch(r"[0-9a-f]{64}", v) is None
                   for v in record["containers"].values())):
        raise ValueError("Cannot resume old API retirement: its retained ownership record is malformed.")
    prefix = project.name if len(project.name) <= 63 else "r-" + hashlib.sha256(project.name.encode("ascii")).hexdigest()
    separator = "-route-" if len(project.name) <= 63 else "-"
    if re.fullmatch(re.escape(prefix + separator) + r"[0-9a-f]{64}", record["volume"]) is None:
        raise ValueError("Cannot resume old API retirement: the retained volume name is not an owned old route.")
    current = project.inventory()
    if any(role in current and (current[role]["Id"] != identifier or current[role]["State"]["Running"])
           for role, identifier in record["containers"].items()):
        raise ValueError("Cannot retire the old API route: an owned container changed or is running.")
    present = _check_old_route_borrowers(project, record["volume"], set(record["containers"].values()))
    for role in ("provider", "api", "api-init"):
        if role in current and role in record["containers"]:
            project.command("rm", record["containers"][role])
    if present:
        project.command("volume", "rm", record["volume"])
    receipt.unlink()
    _sync_directory(config)


def _admit_runtime_volumes(project: ComposeProject, plan: dict, name: str) -> None:
    """Reuse only volumes owned by this deployment and its Compose project."""
    present = set(project.command("volume", "ls", "--quiet").decode("utf-8").splitlines())
    inventory = project.inventory()
    grants = {
        "lifetime": {"provider": ("/state", True)},
        "sources": {"provider": ("/run/secs/sources", True), "worker": ("/run/secs/sources", True),
                    "sources-init": ("/sources", True)},
        "session": {"provider": ("/run/secs/worker", False), "worker": ("/run/secs/worker", True),
                    "sources-init": ("/session", True), "session-init": ("/session", True)},
    }
    for role in ("lifetime", "sources", "session"):
        volume = plan["volumes"][role]["name"]
        if volume not in present:
            continue
        records = json.loads(project.command("volume", "inspect", volume))
        if type(records) is not list or len(records) != 1 or type(records[0]) is not dict:
            raise ValueError("Docker did not inspect exactly one SECS runtime volume.")
        record = records[0]
        labels = record.get("Labels")
        expected = {"com.docker.compose.project": project.name, "com.docker.compose.volume": role,
                    "io.secs.deployment": name, "io.secs.checkout": str(project.repository.resolve())}
        if record.get("Name") != volume or type(labels) is not dict or any(
                labels.get(key) != value for key, value in expected.items()):
            raise ValueError(f"Cannot reuse SECS runtime volume {volume!r}: its ownership labels differ.")
        options = record.get("Options")
        if options is None:
            options = {}
        if record.get("Driver") != "local" or type(options) is not dict or options != plan["volumes"][role].get("driver_opts", {}):
            raise ValueError(f"Cannot reuse SECS runtime volume {volume!r}: its driver or options differ.")
        attached = project.command("ps", "--all", "--no-trunc", "--quiet", "--filter", f"volume={volume}").decode("ascii").splitlines()
        allowed = {record["Id"]: (record, *grants[role][service]) for service, record in inventory.items()
                   if service in grants[role]}
        if len(attached) != len(set(attached)) or any(identifier not in allowed for identifier in attached):
            raise ValueError(f"Cannot reuse SECS runtime volume {volume!r}: a foreign or ambiguous container is attached.")
        for identifier in attached:
            container, target, writable = allowed[identifier]
            mounts = container.get("Mounts")
            if type(mounts) is not list or any(type(mount) is not dict for mount in mounts):
                raise ValueError(f"Cannot reuse SECS runtime volume {volume!r}: its container mounts are unavailable.")
            matches = [mount for mount in mounts if mount.get("Type") == "volume" and mount.get("Name") == volume]
            if len(matches) != 1 or matches[0].get("Destination") != target or matches[0].get("RW") is not writable:
                raise ValueError(f"Cannot reuse SECS runtime volume {volume!r}: its container grant differs.")


def _private_directory(path: Path) -> None:
    """Require an existing owner-only directory, with no symlink in its resolved path."""
    metadata = path.lstat()
    if (path.resolve() != path or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or not private_acl(path, owner_permissions="rwx")):
        raise ValueError(f"Deployment directory must be operator-owned mode 0700: {path}")


def _private_input(path: Path) -> None:
    """Admit a bounded private mount without reading its secret contents."""
    metadata = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o700 not in {0o400, 0o600}
            or not private_acl(path, owner_permissions=("rw-" if stat.S_IMODE(metadata.st_mode) & 0o200 else "r--"))
            or not 0 < metadata.st_size <= 65536):
        raise ValueError(f"Deployment input must be a nonempty owner-only file of at most 64 KiB: {path}")


def _private_file(path: Path) -> bytes:
    """Read a bounded owner-only document for the operation that owns it."""
    _private_input(path)
    expected = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        observed = os.fstat(descriptor)
        if (not stat.S_ISREG(observed.st_mode) or observed.st_dev != expected.st_dev
                or observed.st_ino != expected.st_ino or observed.st_uid != os.getuid()
                or not 0 < observed.st_size <= 65536):
            raise ValueError(f"Deployment input changed while it was opened: {path}")
        content = bytearray()
        while len(content) <= 65536:
            part = os.read(descriptor, 65537 - len(content))
            if not part:
                break
            content.extend(part)
        if len(content) != observed.st_size or len(content) > 65536:
            raise ValueError(f"Deployment input changed length while it was read: {path}")
        return bytes(content)
    finally:
        os.close(descriptor)


def install_secret(repository: Path, name: str, filename: str, source: Path) -> Path:
    """Install an operator-supplied secret once; never replace a live credential or key."""
    config = configuration_directory(repository, name)
    _private_directory(config)
    if filename not in {"provider.signing.private.json", "interpreter.key"}:
        raise ValueError("Secret installation accepts only the provider credential or interpreter key.")
    content = _private_file(source.resolve(strict=True))
    state = _state_root(repository, name)
    destination = state / filename
    _publish_new_file(destination, content)
    return destination


def _state_root(repository: Path, name: str) -> Path:
    """Create only the named deployment's private storage, without adopting other layouts."""
    configuration_directory(repository, name)
    path = repository
    for part in ("secrets", "deployments", name):
        path = path / part
        _ensure_private_parent(path)
    return path


def start_deployment(repository: Path, name: str, mock_index_selection: bool | None = None) -> dict:
    """Render first, admit existing inputs, then create private workspaces and start.

    Runtime parsers remain authoritative for credential and model semantics.
    Startup never imports model code on the host, downloads, or rebuilds an index.
    """
    config = configuration_directory(repository, name)
    _private_directory(config)
    if os.path.lexists(config / "old-api-retirement.json"):
        raise ValueError("Cannot start deployment: old API retirement is incomplete; finish its retained cleanup first.")
    _private_file(config / "deployment.env")
    network = _provider_network(repository, name)
    plan = _render_deployment(repository, name, config, network, mock_index_selection)
    project = _project(repository, name)
    try:
        _admit_provider_network(project, repository, name, network)
        _admit_worker_image(project, plan)
    except (OSError, ValueError, RuntimeError) as error:
        error.add_note(
            "Startup stopped before this attempt changed attempt ownership, runtime workspaces, or containers."
        )
        raise
    state = repository / "secrets/deployments" / name
    required = [config / "provider.toml", config / "worker.toml", state / "provider.signing.private.json"]
    if plan["services"]["worker"].get("environment", {}).get("SECS_MOCK_RUNNER") != "1":
        required.append(state / "interpreter.key")
    for path in required:
        _private_input(path)
    if any(os.path.lexists(state / part) for part in ("state", "sources", "socket")):
        raise ValueError("Old host runtime directories remain; drain the old deployment and preserve its state before selecting the new volumes.")
    _admit_runtime_volumes(project, plan, name)
    access = _grant_runtime_inputs(project, plan, repository, name)
    runtime_directory = state / "runtime"
    runtime_required = [runtime_directory / "provider.signing.private.json"]
    if plan["services"]["worker"].get("environment", {}).get("SECS_MOCK_RUNNER") != "1":
        runtime_required.append(runtime_directory / "interpreter.key")
    for path in runtime_required:
        metadata = path.lstat()
        mapped = access["mapped_uid"]
        mapped_gid = access["mapped_gid"]
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != mapped or metadata.st_gid != mapped_gid
                or stat.S_IMODE(metadata.st_mode) != 0o400 or not 0 < metadata.st_size <= 65536
                or not private_acl(path, owner_permissions="r--")):
            raise ValueError(f"Cannot start deployment: mapped-UID secret copy {path} failed owner-only admission.")
    return project.start(plan)


def _apply_shared_grants(plan, project, config, state):
    """Consume the SDK's shared socket grants alongside SECS-owned inputs."""
    document = _toml_document(config / "provider.toml")
    if document.get("schema_id") != "secs.provider.config.v4":
        raise ValueError("Provider configuration requires schema_id secs.provider.config.v4.")
    selection = document.get("shared")
    if type(selection) is not dict or set(selection) != {"project", "gid"}:
        raise ValueError("Provider configuration requires shared project and gid.")
    project_name, gid = selection["project"], selection["gid"]
    if (type(project_name) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", project_name) is None
            or type(gid) is not int or not 1 <= gid <= 4294967294):
        raise ValueError("Provider configuration requires a lowercase shared project name and nonzero socket group ID.")
    projection = _sdk_grants(project, plan["services"]["provider"]["image"], project_name, gid)
    if type(projection) is not dict or set(projection) != {"group_add", "volumes", "mounts", "proxy_socket", "outbox_socket", "socket_group_id"}:
        raise ValueError("Installed SDK did not return its complete shared socket grants.")
    provider = plan["services"]["provider"]
    mounts = list(projection["mounts"])
    mounts.append(
        {"type": "bind", "source": str(state / "runtime/provider.signing.private.json"),
         "target": "/run/secrets/provider/provider.signing.private.json",
         "read_only": True, "bind": {"create_host_path": False}},
    )
    if set(plan.setdefault("volumes", {})) & set(projection["volumes"]):
        raise ValueError("Shared socket volumes overlap SECS-owned volumes.")
    _add_mount_grants(provider, mounts)
    provider.setdefault("group_add", []).extend(projection["group_add"])
    plan["volumes"].update(projection["volumes"])


def _add_mount_grants(provider, mounts):
    """Refuse a projection that hides another owner's already granted path."""
    targets = [Path(mount["target"]) for mount in provider["volumes"]]
    for mount in mounts:
        target = Path(mount["target"])
        if any(target == occupied or target in occupied.parents or occupied in target.parents
               for occupied in targets):
            raise ValueError("Projected grants overlap a provider mount; select separate socket and input paths.")
        targets.append(target)
    provider["volumes"].extend(mounts)


def _sdk_grants(project, image, project_name, gid):
    """Construct the shared SDK grant from provider-owned project and group settings."""
    raw = project.command(
        "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--pids-limit", "16", "--cpus", "1", "--memory", "128m", "--memory-swap", "128m",
        "--user", f"{os.getuid()}:{os.getgid()}", "--env", "PROVIDER_CORE_IN_CONTAINER=1",
        "--entrypoint", "python", image, "-I", "-B", "-c",
        "import json, sys; from provider_core.shared_grants import SharedGrants; "
        "print(json.dumps(SharedGrants(sys.argv[1], int(sys.argv[2])).compose(), sort_keys=True))",
        project_name, str(gid),
    )
    return json.loads(raw)


def _ensure_mapped_directory(path: Path, mapped_id: int, permissions: str, *, owner_uid: int,
                             allow_other_access: bool = False) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.resolve() != path:
        raise ValueError(f"Cannot grant deployment access: {path} must be a real directory.")
    if metadata.st_uid != owner_uid:
        raise ValueError(f"Cannot grant deployment access: {path} is not controlled by its expected owner.")
    existing = mapped_acl_permissions(path, "rwx", mapped_id)
    if existing == permissions:
        return
    if existing is not None:
        set_mapped_access(path, mapped_id, permissions, owner_uid=owner_uid,
                          owner_permissions="rwx", directory=True)
        return
    mode = stat.S_IMODE(metadata.st_mode)
    if mapped_id == owner_uid:
        required = sum(value for value, bit in ((4, "r"), (2, "w"), (1, "x")) if bit in permissions)
        if ((mode >> 6) & required) == required:
            return
        raise ValueError(f"Cannot grant deployment access: {path} owner permissions do not cover the runtime access.")
    if allow_other_access and mode & 0o005 == 0o005:
        return
    grant_private_access(path, mapped_id, permissions, owner_uid=owner_uid,
                         owner_permissions="rwx", directory=True)


def _ensure_mapped_file(path: Path, mapped_id: int, *, owner_uid: int) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.resolve() != path:
        raise ValueError(f"Cannot grant deployment access: {path} must be a regular file.")
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid == mapped_id and mode in {0o400, 0o600}:
        return
    if metadata.st_uid != owner_uid:
        if mode & 0o004:
            return
        raise ValueError(f"Cannot grant deployment access: {path} is not controlled by its expected owner.")
    owner_permissions = "rw-" if mode & 0o200 else "r--"
    if mapped_acl_permissions(path, owner_permissions, mapped_id) == "r--":
        return
    if (mode & 0o700 not in {0o400, 0o600}
            or not private_acl(path, owner_permissions=owner_permissions)):
        if mode & 0o004:
            return
        raise ValueError(f"Cannot grant private deployment access to {path}: its current permissions are not owner-only.")
    grant_private_access(path, mapped_id, "r--", owner_uid=owner_uid,
                         owner_permissions=owner_permissions, directory=False)


def _ensure_mapped_tree(root: Path, mapped_id: int, *, owner_uid: int) -> None:
    """Grant a worker only read/traverse access to a private scientific input tree."""
    resolved_root = root.resolve(strict=True)
    _ensure_mapped_directory(root, mapped_id, "r-x", owner_uid=owner_uid, allow_other_access=True)
    for current, directories, filenames in os.walk(root, followlinks=False):
        parent = Path(current)
        for name in directories:
            path = parent / name
            if path.is_symlink():
                _validate_tree_link(path, resolved_root, directory=True)
            else:
                _ensure_mapped_directory(path, mapped_id, "r-x", owner_uid=owner_uid,
                                         allow_other_access=True)
        for name in filenames:
            path = parent / name
            if path.is_symlink():
                _validate_tree_link(path, resolved_root, directory=False)
            else:
                _ensure_mapped_file(path, mapped_id, owner_uid=owner_uid)


def _validate_tree_link(path: Path, root: Path, *, directory: bool) -> None:
    """Admit cache snapshot links only when their final target stays in the input tree."""
    try:
        target = path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"Cannot grant deployment access: scientific input link {path} is unresolved.") from error
    if not target.is_relative_to(root) or (stat.S_ISDIR(target.stat().st_mode) is not directory):
        raise ValueError(f"Cannot grant deployment access: scientific input link {path} escapes its tree or has the wrong target type.")


def _grant_runtime_inputs(project: ComposeProject, plan: dict, repository: Path, name: str) -> dict:
    """Grant bind access and publish secret copies as the selected runtime identity."""
    provider = plan["services"]["provider"]
    worker = plan["services"]["worker"]
    try:
        container_uid, container_gid = (int(value) for value in provider["user"].split(":"))
    except (KeyError, ValueError, AttributeError) as error:
        raise ValueError("Cannot grant deployment access: provider user identity is missing or malformed.") from error
    if worker.get("user") != provider["user"] or worker.get("userns_mode") != provider.get("userns_mode"):
        raise ValueError("Cannot grant deployment access: provider and worker identity namespaces must match.")
    image = provider["image"]
    mapped, mapped_gid = mapped_ids(project, image, container_uid, container_gid, provider.get("userns_mode"))
    config = configuration_directory(repository, name)
    state = repository / "secrets/deployments" / name
    _state_root(repository, name)

    for directory in (config.parent, config):
        _ensure_mapped_directory(directory, mapped, "--x", owner_uid=os.getuid())
    _ensure_mapped_directory(state.parent.parent, mapped, "--x", owner_uid=os.getuid())
    _ensure_mapped_directory(state.parent, mapped, "--x", owner_uid=os.getuid())
    _ensure_mapped_directory(state, mapped, "--x", owner_uid=os.getuid())

    provider_config = config / "provider.toml"
    _ensure_mapped_file(provider_config, mapped, owner_uid=os.getuid())
    provider_document = _toml_document(provider_config)
    ca_filenames = set()
    api = provider_document.get("api")
    if type(api) is dict and api.get("ca_file") is not None:
        ca = Path(api["ca_file"])
        if type(api["ca_file"]) is not str or str(ca) != api["ca_file"] or ca.parent != Path("/run/config/provider"):
            raise ValueError("Cannot grant deployment access: API CA path must select a file in the provider config mount.")
        ca_filenames.add(ca.name)
    execution = provider_document.get("execution")
    if type(execution) is dict:
        if execution.get("interpreter_use_private_ca") is True:
            ca_filenames.add("interpreter-ca.crt")
        if execution.get("upload_store_use_private_ca") is True:
            ca_filenames.add("upload-store-ca.crt")
    for ca_filename in ca_filenames:
        _ensure_mapped_file(config / ca_filename, mapped, owner_uid=os.getuid())
    worker_config = next(
        Path(mount["source"]) for mount in worker["volumes"]
        if mount.get("target") == "/run/config/worker/worker.toml"
    )
    _ensure_mapped_file(worker_config, mapped, owner_uid=os.getuid())

    for service in (provider, worker):
        for mount in service.get("volumes", []):
            if mount.get("type") != "bind":
                continue
            target = mount.get("target")
            source = Path(mount["source"])
            if target in {"/run/config/provider", "/run/config/worker/worker.toml"}:
                continue
            if target in {"/checkpoint", "/cache"}:
                _ensure_mapped_tree(source, mapped, owner_uid=os.getuid())
            elif target not in {"/run/secrets/provider/provider.signing.private.json",
                                "/run/secrets/provider/interpreter.key"}:
                _ensure_mapped_file(source, mapped, owner_uid=os.getuid())

    secret_paths = [state / "provider.signing.private.json"]
    if worker.get("environment", {}).get("SECS_MOCK_RUNNER") != "1":
        secret_paths.append(state / "interpreter.key")
    for source in secret_paths:
        _private_input(source)
        source_mode = stat.S_IMODE(source.lstat().st_mode)
        owner_permissions = "rw-" if source_mode & 0o200 else "r--"
        if not private_owner_acl(source, owner_permissions=owner_permissions):
            raise ValueError(f"Cannot grant deployment access: installed secret {source.name} must remain owner-only.")
    runtime_directory = state / "runtime"
    try:
        runtime_directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _private_directory(runtime_directory)
    script = repository / "tools/copy_deployment_secrets.py"
    script_metadata = script.lstat()
    if not stat.S_ISREG(script_metadata.st_mode) or script.resolve() != script:
        raise ValueError("Cannot grant deployment access: the secret-copy helper must be a regular repository file.")
    script_source = script.read_text(encoding="utf-8")
    if any(character in str(runtime_directory) for character in (",", "\n", "\r")):
        raise ValueError("Cannot grant deployment access: runtime paths must be safe Docker bind sources.")
    copied = {}
    primary_error = None
    try:
        _ensure_mapped_directory(runtime_directory, mapped, "rwx", owner_uid=os.getuid())
        for source in secret_paths:
            arguments = [
                "run", "--rm", "--interactive", "--pull", "never", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--pids-limit", "16", "--cpus", "1", "--memory", "128m", "--memory-swap", "128m",
                "--user", f"{container_uid}:{container_gid}",
                "--mount", f"type=bind,src={runtime_directory},dst=/run/output",
            ]
            if provider.get("userns_mode") is not None:
                arguments.extend(("--userns", provider["userns_mode"]))
            try:
                raw = project.command(
                    *arguments, "--entrypoint", "python", image, "-I", "-B", "-c", script_source,
                    "/run/output", source.name, input=_private_file(source),
                )
                result = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError("Cannot publish mapped-UID deployment secret copy: helper status is invalid.") from error
            if type(result) is not dict or set(result) != {source.name} or result[source.name] not in {"created", "reused"}:
                raise ValueError("Cannot publish mapped-UID deployment secret copy: helper status is incomplete.")
            copied.update(result)
        for source in secret_paths:
            destination = runtime_directory / source.name
            metadata = destination.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != mapped or metadata.st_gid != mapped_gid
                    or stat.S_IMODE(metadata.st_mode) != 0o400 or not 0 < metadata.st_size <= 65536
                    or not private_acl(destination, owner_permissions="r--")):
                raise ValueError(f"Cannot use mapped-UID deployment secret copy {destination}: owner-only mode 0400 is required.")
    except BaseException as error:
        primary_error = error
        error.add_note("Startup remains blocked; a private runtime secret copy may have been published before this failure.")
        raise
    finally:
        if mapped != os.getuid():
            try:
                if mapped_acl_permissions(runtime_directory, "rwx", mapped) == "rwx":
                    set_mapped_access(runtime_directory, mapped, "r-x", owner_uid=os.getuid(),
                                      owner_permissions="rwx", directory=True)
            except BaseException as cleanup_error:
                if primary_error is not None:
                    primary_error.add_note("The runtime secret directory ACL could not be narrowed; inspect it before retrying.")
                else:
                    raise ValueError("Cannot grant deployment access: runtime secret directory ACL could not be narrowed.") from cleanup_error
    return {"mapped_uid": mapped, "mapped_gid": mapped_gid, "secret_copies": copied}


def _status(records: dict) -> dict:
    """Expose lifecycle evidence, not Docker environment or credential-bearing metadata."""
    return {role: {"id": record["Id"], "status": record["State"]["Status"],
                   "image": record["Image"], "restart_count": record["RestartCount"],
                   "exit_code": record["State"]["ExitCode"],
                   "oom_killed": record["State"]["OOMKilled"],
                   **({"mock_index": "eight molecules"}
                      if record.get("Config", {}).get("Labels", {}).get("io.secs.mock-index") == "eight-molecule" else {}),
                   **({"mock_runner": True}
                      if record.get("Config", {}).get("Labels", {}).get("io.secs.mock-runner") == "1" else {}),
                   **({"health": record["State"]["Health"]["Status"]}
                      if "Health" in record["State"] else {})}
            for role, record in records.items()}


def main(arguments: list[str] | None = None) -> int:
    """Dispatch one named operation; report partial effects without implying rollback."""
    parser = argparse.ArgumentParser(description="Operate a named SECS deployment.")
    parser.add_argument("operation", choices=("init", "config", "grant", "up", "status", "down", "retire-api",
                                              "logs", "credential-install", "interpreter-key-install"))
    parser.add_argument("deployment")
    parser.add_argument("--source", type=Path)
    options = parser.parse_args(arguments)
    installing = options.operation in {"credential-install", "interpreter-key-install"}
    if installing != (options.source is not None):
        parser.error("--source is required only for credential-install or interpreter-key-install")
    repository = Path(__file__).resolve().parents[1]
    try:
        if options.operation == "init":
            target = worker_target(os.environ.get("SECS_WORKER_TARGET", ""))
            selection = os.environ.get("SECS_USE_MOCK_INDEX", "0")
            runner_selection = os.environ.get("SECS_USE_MOCK_RUNNER", "0")
            if selection not in ("0", "no", "1", "yes") or (selection in ("1", "yes") and target != "cpu"):
                raise ValueError("Select USE_MOCK_INDEX=yes or no; yes requires TARGET=cpu.")
            if runner_selection not in ("0", "no", "1", "yes") or (runner_selection in ("1", "yes") and target != "cpu"):
                raise ValueError("Select USE_MOCK_RUNNER=yes or no; yes requires TARGET=cpu.")
            mock_index = selection in ("1", "yes")
            mock_runner = runner_selection in ("1", "yes")
            if mock_runner and mock_index:
                raise ValueError("USE_MOCK_RUNNER and USE_MOCK_INDEX cannot both be enabled.")
            suffix = ".mock-runner" if mock_runner else (".mock-index" if mock_index else (".cpu" if target == "cpu" else ""))
            templates = TEMPLATES | {"worker.toml": Path(f"config/worker{suffix}.toml.example")}
            destination = initialize_configuration(repository, options.deployment, templates)
            print(f"Configuration created: {destination}")
            if mock_runner:
                print("Mock runner selected: no interpreter or scientific model will run; select the provider image.")
            else:
                print(f"Worker target: {target}; mock index: {'yes (eight molecules)' if mock_index else 'no'}. Build and select the matching worker image.")
            if mock_index or mock_runner:
                print("Mock modes can consume queued Jobs for this provider; their results disclose the mock behavior.")
            print("Edit these files before deployment. No credentials installed or services started.")
            return 0
        config = configuration_directory(repository, options.deployment)
        _private_directory(config)
        project = _project(repository, options.deployment)
        index_flag = os.environ.get("SECS_USE_MOCK_INDEX", "")
        if index_flag not in ("", "0", "no", "1", "yes"):
            raise ValueError("Select USE_MOCK_INDEX=yes or no.")
        index_selection = None if not index_flag else index_flag in ("1", "yes")
        # Observations must remain available during a long shutdown. Inventory
        # checks ownership without promising a snapshot across lifecycle changes.
        if options.operation == "config":
            print(json.dumps(render_deployment(repository, options.deployment, index_selection), indent=2))
            return 0
        if options.operation == "status":
            print(json.dumps(_status(_old_project(repository, options.deployment).inventory()), indent=2))
            return 0
        if options.operation == "logs":
            _old_project(repository, options.deployment).logs()
            return 0
        # Serialize mutations, including their ownership admission. Acquiring
        # the lock needs no credential contents.
        with _locked_parent(config.parent):
            if installing:
                filename = "provider.signing.private.json" if options.operation == "credential-install" else "interpreter.key"
                print(install_secret(repository, options.deployment, filename, options.source))
            elif options.operation == "grant":
                plan = render_deployment(repository, options.deployment, index_selection)
                access = _grant_runtime_inputs(project, plan, repository, options.deployment)
                print(json.dumps(access, indent=2))
                print("Mapped container access is prepared; no deployment services started.")
            elif options.operation == "up":
                print(json.dumps(_status(start_deployment(repository, options.deployment, index_selection)), indent=2))
                print("Containers started. Check provider logs for hello publication or errors.")
                print("A successful hello confirms API acceptance, not model readiness.")
            elif options.operation == "down":
                print(json.dumps(_status(project.stop()), indent=2))
            elif options.operation == "retire-api":
                try:
                    retire_old_api(_old_project(repository, options.deployment), config)
                except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as error:
                    error.add_note("Retirement is incomplete; the provider or dependencies may already be stopped and route resources may remain. Inspect status before retrying.")
                    raise
                print("Old API route retired. Shared and scientific storage was retained.")
    except (OSError, ValueError, RuntimeError) as error:
        headline = "initialization" if options.operation == "init" else options.operation
        print(f"Deployment {headline} failed: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
