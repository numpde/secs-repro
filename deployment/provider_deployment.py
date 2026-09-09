"""Bind the common deployment initializer to SECS's maintained configuration.

The model's examples remain authoritative. This adapter selects files, not
copied settings; changing a worker default requires no framework change.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
from tempfile import TemporaryDirectory

from deployment.compose import ComposeProject
from deployment.templates import (
    configuration_directory, initialize_configuration, _ensure_private_parent,
    _locked_parent, _write_new_file, _sync_directory,
)


TEMPLATES = {
    "provider.toml": Path("config/provider.toml.example"),
    "worker.toml": Path("config/worker.toml.example"),
    "deployment.env": Path("config/deployment.env.example"),
}


def render_deployment(repository: Path, name: str) -> dict:
    """Project SECS paths and operator artifact selections into the existing recipe.

    Compose owns dotenv parsing. Framework-owned paths and the nonroot host
    identity take precedence over that file; the shell environment is ignored.
    No runtime directories are created by rendering.
    """
    config = configuration_directory(repository, name)
    _private_directory(config)
    _private_file(config / "deployment.env")
    state = repository / "secrets/deployments" / name
    if os.getuid() == 0 or os.getgid() == 0:
        raise ValueError("SECS deployment requires a nonroot host UID and GID.")
    environment = {
        "PROVIDER_UID": str(os.getuid()), "PROVIDER_GID": str(os.getgid()),
        "PROVIDER_CONFIG_DIRECTORY": str(config),
        "PROVIDER_CREDENTIAL_FILE": str(state / "provider.signing.private.json"),
        "INTERPRETER_KEY_FILE": str(state / "interpreter.key"),
        "ATTEMPT_STATE_DIRECTORY": str(state / "state"),
        "SOURCE_DIRECTORY": str(state / "sources"),
        "WORKER_SOCKET_DIRECTORY": str(state / "socket"),
        "WORKER_CONFIG_FILE": str(config / "worker.toml"),
        "MOLFORMER_LOCK_FILE": str(repository / "molformer.lock.toml"),
        "CACHE_VERIFIER_FILE": str(repository / "tools/materialize_molformer_cache.py"),
    }
    plan = _project(repository, name).render(
        repository / "compose/provider.yml", config / "deployment.env", environment,
    )
    devices = plan["services"]["worker"]["deploy"]["resources"]["reservations"]["devices"]
    identifiers = devices[0]["device_ids"]
    if len(identifiers) != 1 or not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", identifiers[0]):
        raise ValueError("WORKER_GPU_UUID must select one GPU UUID, not an index or all GPUs.")
    return plan


def _project(repository: Path, name: str) -> ComposeProject:
    """SECS stops Job admission before its scientific worker, retaining the current grace periods."""
    configuration_directory(repository, name)
    return ComposeProject(repository, f"secs-{name}", (("provider", 1900), ("worker", 20)))


def _private_directory(path: Path) -> None:
    """Require an existing owner-only directory, with no symlink in its resolved path."""
    metadata = path.lstat()
    if (path.resolve() != path or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError(f"Deployment directory must be operator-owned mode 0700: {path}")


def _private_file(path: Path) -> bytes:
    """Read bounded owner-only input; never include its contents in diagnostics."""
    metadata = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
            or not 0 < metadata.st_size <= 65536):
        raise ValueError(f"Deployment input must be a nonempty owner-only file of at most 64 KiB: {path}")
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
    with TemporaryDirectory(prefix=".install-", dir=state) as temporary:
        staged = Path(temporary) / filename
        _write_new_file(staged, content)
        # Link a complete private file into place without replacing an existing
        # name. A failed or interrupted write never installs a partial key.
        os.link(staged, destination)
        try:
            _sync_directory(state)
        except OSError as error:
            error.add_note(f"Secret is visible at {destination}; crash durability is unconfirmed.")
            raise
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
    plan = render_deployment(repository, name)
    config = configuration_directory(repository, name)
    state = repository / "secrets/deployments" / name
    for path in (config / "provider.toml", config / "worker.toml",
                 state / "provider.signing.private.json", state / "interpreter.key"):
        _private_file(path)
    state = _state_root(repository, name)
    _bind_attempt_owner(state, config)
    for part in ("state", "sources", "socket"):
        _ensure_private_parent(state / part)
    return _project(repository, name).start(plan)


def _bind_attempt_owner(state: Path, config: Path) -> None:
    """Keep retained attempts bound to the same API origin and provider identity.

    Runtime parsers validate the full documents. Here only the facts needed to
    prevent cross-deployment replay are read. Credential rotation is allowed;
    changing the API or provider requires a different named deployment.
    """
    provider = tomllib.loads(_private_file(config / "provider.toml").decode("utf-8"))
    credential = json.loads(_private_file(state / "provider.signing.private.json"))
    try:
        identity = {"origin": provider["api"]["origin"], "provider_ref": credential["principal_ref"]}
    except (KeyError, TypeError) as error:
        raise ValueError("Cannot bind attempt state: provider.toml needs api.origin and the credential needs principal_ref.") from error
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise ValueError("Cannot bind attempt state: API origin and provider identity must be nonempty text.")
    binding = state / "attempt-owner.json"
    content = (json.dumps(identity, sort_keys=True) + "\n").encode()
    if binding.exists() or binding.is_symlink():
        if _private_file(binding) != content:
            raise ValueError("This attempt state belongs to another API or provider; use another deployment name.")
    else:
        journal = state / "state"
        if journal.exists() and any(journal.iterdir()):
            raise ValueError("Cannot adopt existing attempt files without their API/provider ownership record.")
        _write_new_file(binding, content)
        _sync_directory(state)


def _status(records: dict) -> dict:
    """Expose lifecycle evidence, not Docker environment or credential-bearing metadata."""
    return {role: {"id": record["Id"], "status": record["State"]["Status"],
                   "image": record["Image"]} for role, record in records.items()}


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
        # Serialize all named lifecycle operations in this checkout. Directory
        # locking also works when credentials or runtime config are unreadable.
        with _locked_parent(config.parent):
            project = _project(repository, options.deployment)
            if installing:
                filename = "provider.signing.private.json" if options.operation == "credential-install" else "interpreter.key"
                print(install_secret(repository, options.deployment, filename, options.source))
            elif options.operation == "config":
                print(json.dumps(render_deployment(repository, options.deployment), indent=2))
            elif options.operation == "up":
                print(json.dumps(_status(start_deployment(repository, options.deployment)), indent=2))
                print("Containers started; verify provider hello and Job polling in logs.")
            elif options.operation == "down":
                print(json.dumps(_status(project.stop()), indent=2))
            elif options.operation == "status":
                print(json.dumps(_status(project.inventory()), indent=2))
            else:
                project.logs()
    except (OSError, ValueError, RuntimeError) as error:
        headline = "initialization" if options.operation == "init" else options.operation
        print(f"Deployment {headline} failed: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
