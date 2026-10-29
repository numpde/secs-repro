"""Bind named SECS deployments to their configuration, secrets, and attempt state.

Maintained examples supply defaults; Compose owns the service recipe.
This adapter supplies SECS paths and serializes deployment mutations.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib

from deployment.compose import ComposeProject
from deployment.templates import (
    configuration_directory, initialize_configuration, _ensure_private_parent,
    _locked_parent, _publish_new_file,
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
        raise ValueError(
            f"Cannot use external network {expected_name!r}; gateway {str(gateway)!r} does not "
            f"match network.toml host_address {str(host)!r}."
        )
    if host not in subnet:
        raise ValueError(
            f"Cannot use external network {expected_name!r}; host address {str(host)!r} is outside "
            f"subnet {str(subnet)!r}."
        )
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
    gpu_uuid = _host_gpu_uuid(repository)
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
        "SECS_HOST_WORKER_GPU_UUID": gpu_uuid,
    }
    project = _project(repository, name)
    base_recipe = ComposeProject(repository, project.name, ("provider", "worker", "sources-init", "session-init"))
    plan = base_recipe.render(
        repository / "compose/provider.yml", config / "deployment.env", environment,
    )
    _apply_provider_network(plan, name, network)
    _apply_delivery_binding(plan, project, config)
    devices = plan["services"]["worker"]["deploy"]["resources"]["reservations"]["devices"]
    identifiers = devices[0]["device_ids"]
    if identifiers != [gpu_uuid]:
        raise ValueError("The rendered worker GPU differs from config/host.toml.")
    return plan


def _project(repository: Path, name: str) -> ComposeProject:
    """SECS stops Job admission before its scientific worker."""
    configuration_directory(repository, name)
    return ComposeProject(repository, f"secs-{name}",
                          ("provider", "worker", "api", "sources-init", "session-init", "api-init"),
                          ("sources-init", "session-init", "api-init"))


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
    _private_file(config / "deployment.env")
    network = _provider_network(repository, name)
    plan = _render_deployment(repository, name, config, network)
    project = _project(repository, name)
    try:
        _admit_provider_network(project, repository, name, network)
    except (OSError, ValueError, RuntimeError) as error:
        error.add_note(
            "Startup stopped before this attempt changed attempt ownership, runtime workspaces, or containers."
        )
        raise
    state = repository / "secrets/deployments" / name
    for path in (config / "provider.toml", config / "worker.toml", state / "interpreter.key"):
        _private_input(path)
    state = _state_root(repository, name)
    if any(os.path.lexists(state / part) for part in ("state", "sources", "socket")):
        raise ValueError("Old host runtime directories remain; drain the old deployment and preserve its state before selecting the new volumes.")
    _admit_runtime_volumes(project, plan, name)
    return project.start(plan)


def _apply_delivery_binding(plan, project, config):
    """Consume the installed SDK's projection, not a second binding parser."""
    document = _toml_document(config / "provider.toml")
    selection = document.get("binding")
    if type(selection) is not dict or set(selection) != {"definition_file", "name"}:
        raise ValueError("Provider configuration requires its shared binding definition_file and name.")
    definition, name = selection["definition_file"], selection["name"]
    if (type(definition) is not str or not Path(definition).is_absolute()
            or not Path(definition).name or any(value in definition for value in (",", "\0", "\r", "\n"))
            or type(name) is not str):
        raise ValueError("Delivery selection requires an absolute definition file and a text binding name.")
    projection = _sdk_projection(project, plan["services"]["provider"]["image"], definition,
                                 "provider_core.deployment_binding", "describe-binding",
                                 "--definition", definition, "--binding", name)
    if type(projection) is not dict or set(projection) != {"ipc_volume", "socket_path", "socket_group_id", "input_paths"}:
        raise ValueError("Installed SDK did not return its complete delivery-binding projection.")
    volume, socket_path, group, inputs = (projection[key] for key in (
        "ipc_volume", "socket_path", "socket_group_id", "input_paths"))
    if (type(volume) is not str or not volume or type(socket_path) is not str
            or not Path(socket_path).is_absolute() or Path(socket_path).parent == Path("/")
            or type(group) is not int or group <= 0 or type(inputs) is not list
            or any(type(path) is not str or not Path(path).is_absolute() for path in inputs)):
        raise ValueError("Installed SDK delivery projection contains invalid container grants.")
    provider = plan["services"]["provider"]
    mounts = [{"type": "volume", "source": volume, "target": str(Path(socket_path).parent),
               "read_only": True, "volume": {"nocopy": True}}]
    mounts.extend({"type": "bind", "source": filename, "target": filename,
                   "read_only": True, "bind": {"create_host_path": False}}
                  for filename in (definition, *inputs))
    _add_mount_grants(provider, mounts)
    provider["group_add"] = [str(group)]
    plan.setdefault("volumes", {})[volume] = {"name": volume, "external": True}
    _apply_api_route(plan, project, config, definition, name)


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


def _status(records: dict) -> dict:
    """Expose lifecycle evidence, not Docker environment or credential-bearing metadata."""
    return {role: {"id": record["Id"], "status": record["State"]["Status"],
                   "image": record["Image"], "restart_count": record["RestartCount"],
                   "exit_code": record["State"]["ExitCode"],
                   "oom_killed": record["State"]["OOMKilled"],
                   **({"health": record["State"]["Health"]["Status"]}
                      if "Health" in record["State"] else {})}
            for role, record in records.items()}


def main(arguments: list[str] | None = None) -> int:
    """Dispatch one named operation; report partial effects without implying rollback."""
    parser = argparse.ArgumentParser(description="Operate a named SECS deployment.")
    parser.add_argument("operation", choices=("init", "config", "up", "status", "down",
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
            destination = initialize_configuration(repository, options.deployment, TEMPLATES)
            print(f"Configuration created: {destination}")
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
            print(json.dumps(_status(project.inventory()), indent=2))
            return 0
        if options.operation == "logs":
            project.logs()
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
    except (OSError, ValueError, RuntimeError) as error:
        headline = "initialization" if options.operation == "init" else options.operation
        print(f"Deployment {headline} failed: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
