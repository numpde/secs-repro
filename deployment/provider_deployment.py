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


TEMPLATES = {
    "provider.toml": Path("config/provider.toml.example"),
    "worker.toml": Path("config/worker.toml.example"),
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


def render_deployment(repository: Path, name: str) -> dict:
    """Project SECS paths and operator artifact selections into the existing recipe.

    Compose owns dotenv parsing. Framework-owned paths and the nonroot host
    identity take precedence over that file; the shell environment is ignored.
    No runtime directories are created by rendering.
    """
    config = configuration_directory(repository, name)
    _private_directory(config)
    _private_file(config / "deployment.env")
    network = _provider_network(repository, name)
    return _render_deployment(repository, name, config, network)


def _render_deployment(repository: Path, name: str, config: Path, network: dict) -> dict:
    """Render one deployment from an already validated network policy."""
    worker_config = _toml_document(config / "worker.toml")
    worker_device = worker_config.get("device", "cuda:0")
    mock_index = worker_config.get("mock_index", False)
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
        "INTERPRETER_KEY_FILE": str(state / "interpreter.key"),
        "WORKER_CONFIG_FILE": str(config / "worker.toml"),
        "MOLFORMER_LOCK_FILE": str(repository / "molformer.lock.toml"),
        "CACHE_VERIFIER_FILE": str(repository / "tools/materialize_molformer_cache.py"),
    }
    project = _project(repository, name)
    base_recipe = ComposeProject(repository, project.name, ("provider", "worker", "sources-init", "session-init"))
    plan = base_recipe.render(
        repository / "compose/provider.yml", config / "deployment.env", environment,
    )
    _apply_provider_network(plan, name, network)
    _apply_shared_grants(plan, project, config, state)
    worker = plan["services"]["worker"]
    worker.setdefault("environment", {}).update({"SECS_WORKER_DEVICE": worker_device,
                                                "SECS_MOCK_INDEX": "1" if mock_index else "0"})
    plan["services"]["provider"].setdefault("environment", {})["SECS_MOCK_INDEX"] = "1" if mock_index else "0"
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
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError(f"Deployment directory must be operator-owned mode 0700: {path}")


def _private_input(path: Path) -> None:
    """Admit a bounded private mount without reading its secret contents."""
    metadata = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
            or not 0 < metadata.st_size <= 65536):
        raise ValueError(f"Deployment input must be a nonempty owner-only file of at most 64 KiB: {path}")


def _private_file(path: Path) -> bytes:
    """Read a bounded owner-only document for the operation that owns it."""
    _private_input(path)
    return path.read_bytes()


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


def start_deployment(repository: Path, name: str) -> dict:
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
    plan = _render_deployment(repository, name, config, network)
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
    for path in (config / "provider.toml", config / "worker.toml", state / "interpreter.key",
                 state / "provider.signing.private.json"):
        _private_input(path)
    state = _state_root(repository, name)
    if any(os.path.lexists(state / part) for part in ("state", "sources", "socket")):
        raise ValueError("Old host runtime directories remain; drain the old deployment and preserve its state before selecting the new volumes.")
    _admit_runtime_volumes(project, plan, name)
    return project.start(plan)


def _apply_shared_grants(plan, project, config, state):
    """Consume the SDK's shared socket grants alongside SECS-owned inputs."""
    document = _toml_document(config / "provider.toml")
    selection = document.get("shared")
    if type(selection) is not dict or set(selection) != {"settings_file"}:
        raise ValueError("Provider configuration requires shared settings_file.")
    definition = selection["settings_file"]
    if (type(definition) is not str or not Path(definition).is_absolute()
            or str(Path(definition)) != definition or ".." in Path(definition).parts
            or not Path(definition).name or any(value in definition for value in (",", "\0", "\r", "\n"))):
        raise ValueError("Shared settings_file must be a normalized absolute path.")
    projection = _sdk_projection(project, plan["services"]["provider"]["image"], definition,
                                 "provider_core.shared_grants", "--config", definition)
    if type(projection) is not dict or set(projection) != {"group_add", "volumes", "mounts", "proxy_socket", "outbox_socket", "socket_group_id"}:
        raise ValueError("Installed SDK did not return its complete shared socket grants.")
    provider = plan["services"]["provider"]
    mounts = list(projection["mounts"])
    mounts.extend([
        {"type": "bind", "source": definition, "target": definition,
         "read_only": True, "bind": {"create_host_path": False}},
        {"type": "bind", "source": str(state / "provider.signing.private.json"),
         "target": "/run/secrets/provider/provider.signing.private.json",
         "read_only": True, "bind": {"create_host_path": False}},
    ])
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


def _sdk_projection(project, image, definition, module, *arguments):
    """Run the installed SDK with only its explicit neutral definition mounted."""
    raw = project.command(
        "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--pids-limit", "16", "--cpus", "1", "--memory", "128m", "--memory-swap", "128m",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={definition},dst={definition},readonly",
        "--entrypoint", "python", image, "-I", "-B", "-m", module, *arguments,
    )
    return json.loads(raw)


def _status(records: dict) -> dict:
    """Expose lifecycle evidence, not Docker environment or credential-bearing metadata."""
    return {role: {"id": record["Id"], "status": record["State"]["Status"],
                   "image": record["Image"], "restart_count": record["RestartCount"],
                   "exit_code": record["State"]["ExitCode"],
                   "oom_killed": record["State"]["OOMKilled"],
                   **({"mock_index": "eight molecules"}
                      if record.get("Config", {}).get("Labels", {}).get("io.secs.mock-index") == "eight-molecule" else {}),
                   **({"health": record["State"]["Health"]["Status"]}
                      if "Health" in record["State"] else {})}
            for role, record in records.items()}


def main(arguments: list[str] | None = None) -> int:
    """Dispatch one named operation; report partial effects without implying rollback."""
    parser = argparse.ArgumentParser(description="Operate a named SECS deployment.")
    parser.add_argument("operation", choices=("init", "config", "up", "status", "down", "retire-api",
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
            if selection not in ("0", "no", "1", "yes") or (selection in ("1", "yes") and target != "cpu"):
                raise ValueError("Select USE_MOCK_INDEX=yes or no; yes requires TARGET=cpu.")
            mock_index = selection in ("1", "yes")
            suffix = ".mock-index" if mock_index else (".cpu" if target == "cpu" else "")
            templates = TEMPLATES | {"worker.toml": Path(f"config/worker{suffix}.toml.example")}
            destination = initialize_configuration(repository, options.deployment, templates)
            print(f"Configuration created: {destination}")
            print(f"Worker target: {target}; mock index: {'yes (eight molecules)' if mock_index else 'no'}. Build and select the matching worker image.")
            print("Edit these files before deployment. No credentials installed or services started.")
            return 0
        config = configuration_directory(repository, options.deployment)
        _private_directory(config)
        project = _project(repository, options.deployment)
        # Observations must remain available during a long shutdown. Inventory
        # checks ownership without promising a snapshot across lifecycle changes.
        if options.operation == "config":
            print(json.dumps(render_deployment(repository, options.deployment), indent=2))
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
            elif options.operation == "up":
                print(json.dumps(_status(start_deployment(repository, options.deployment)), indent=2))
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
