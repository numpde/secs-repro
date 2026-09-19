"""Bind named SECS deployments to their configuration, secrets, and attempt state.

Maintained examples supply defaults; Compose owns the service recipe.
This adapter supplies SECS paths and serializes deployment mutations.
"""

from __future__ import annotations

import argparse
import json
import importlib.util
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
import uuid

from deployment.compose import ComposeProject
from deployment.templates import (
    configuration_directory, initialize_configuration, _ensure_private_parent,
    _locked_parent, _publish_new_file,
)


# Load the producer's dependency-free presentation contract from its owning source.
_inspection_spec = importlib.util.spec_from_file_location("secs_inspection_document", Path(__file__).resolve().parents[1] / "src/secs_inference/provider/inspection_document.py")
_inspection_contract = importlib.util.module_from_spec(_inspection_spec)
_inspection_spec.loader.exec_module(_inspection_contract)
_archive_spec = importlib.util.spec_from_file_location("secs_archive_document", Path(__file__).resolve().parents[1] / "src/secs_inference/provider/archive_document.py")
_archive_contract = importlib.util.module_from_spec(_archive_spec)
_archive_spec.loader.exec_module(_archive_contract)


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
    """SECS stops Job admission before its scientific worker."""
    configuration_directory(repository, name)
    return ComposeProject(repository, f"secs-{name}", ("provider", "worker"))


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
        _publish_new_file(binding, content)


def inspect_deployment_journal(repository: Path, name: str) -> dict:
    """Inspect a stopped deployment under its caller's lifecycle exclusion."""
    project = _project(repository, name)
    if any(record["State"].get("Running") is not False for record in project.inventory().values()):
        raise RuntimeError(f"Journal inspection requires a stopped deployment; run make provider/deployment/down DEPLOYMENT={name} first.")
    state = repository / "secrets/deployments" / name
    _private_directory(state)
    journal = state / "state/journal"
    _private_directory(journal)
    binding = json.loads(_private_file(state / "attempt-owner.json"))
    if type(binding) is not dict or set(binding) != {"origin", "provider_ref"} or any(type(value) is not str or not value for value in binding.values()):
        raise ValueError("Attempt ownership binding is unreadable.")
    image = render_deployment(repository, name)["services"]["provider"]["image"]
    raw = project.command("run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                          "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                          "--security-opt", "no-new-privileges:true", "--pids-limit", "32",
                          "--memory", "128m", "--memory-swap", "128m", "--log-driver", "none",
                          "--mount", f"type=bind,src={journal},dst=/state/journal,readonly",
                          "--entrypoint", "python", image, "-m", "secs_inference.provider.journal_inspect")
    document = _inspection_contract.parse_inspection_document(raw)
    if any(record.get("provider_ref") != binding["provider_ref"] for record in document["records"]):
        raise ValueError("Retained journal names another provider than this deployment's ownership binding.")
    return document | {"deployment": name, "owner": binding}


def archive_deployment_journal(repository: Path, name: str, *, execution_attempt_ref: str,
                               expected_record_digest: str, reason: str) -> dict:
    """Archive under lifecycle exclusion using current bound API credentials."""
    project = _project(repository, name)
    if any(record["State"].get("Running") is not False for record in project.inventory().values()):
        raise RuntimeError(f"Journal archival requires a stopped deployment; run make provider/deployment/down DEPLOYMENT={name} first.")
    state = repository / "secrets/deployments" / name
    config = configuration_directory(repository, name)
    journal = state / "state/journal"
    _private_directory(state)
    _private_directory(journal)
    # Unlike offline inspection, this operation uses credentials: existing
    # ownership must match current inputs, and missing binding cannot be created.
    _private_file(state / "attempt-owner.json")
    _bind_attempt_owner(state, config)
    image = render_deployment(repository, name)["services"]["provider"]["image"]
    reader_token = uuid.uuid4().hex
    reader_name = "secs-journal-archive-" + reader_token
    try:
        raw = project.command("run", "--rm", "--name", reader_name,
                              "--label", "io.secs.archive=" + reader_token, "--pull", "never", "--read-only",
                              "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                              "--security-opt", "no-new-privileges:true", "--pids-limit", "32",
                              "--memory", "128m", "--memory-swap", "128m", "--log-driver", "none",
                              "--mount", f"type=bind,src={journal},dst=/state/journal",
                              "--mount", f"type=bind,src={config},dst=/run/config/provider,readonly",
                              "--mount", f"type=bind,src={state / 'provider.signing.private.json'},dst=/run/secrets/provider/signing.private.json,readonly",
                              "--mount", f"type=bind,src={state / 'attempt-owner.json'},dst=/run/config/attempt-owner.json,readonly",
                              "--entrypoint", "python", image, "-m", "secs_inference.provider.journal_archive",
                              "--execution-attempt-ref", execution_attempt_ref,
                              "--expected-record-digest", expected_record_digest, "--reason", reason)
    except (Exception, KeyboardInterrupt) as original:
        try:
            _remove_archive_reader(project, reader_name, reader_token)
        except (Exception, KeyboardInterrupt) as cleanup:
            failure = RuntimeError(
                f"Archival result unconfirmed ({original}); reader cleanup unconfirmed ({cleanup}). "
                f"Provider operator must inspect name={reader_name}, label=io.secs.archive={reader_token}, "
                "verify both and remove only that container ID before inspecting retained journal/archive state. "
                "Preserve all retained work; do not restart until reader shutdown is confirmed."
            )
            for error in (original, cleanup):
                for note in getattr(error, "__notes__", ()):
                    failure.add_note(note)
            raise failure from original
        failure = RuntimeError(
            f"Archival result unconfirmed ({original}); invocation reader stopped. "
            "Provider operator must inspect the journal and archive before retrying or restarting; preserve both."
        )
        for note in getattr(original, "__notes__", ()):
            failure.add_note(note)
        raise failure from original
    document = _archive_contract.parse_archive_document(raw)
    if document["execution_attempt_ref"] != execution_attempt_ref or document["record_digest"] != expected_record_digest:
        raise ValueError("Archive result names a different selection; inspect retained state before retrying.")
    return document | {"deployment": name}


def _remove_archive_reader(project, name: str, token: str) -> None:
    """A timed-out Docker CLI does not prove its writable container stopped."""
    identifiers = project.command("ps", "-a", "--no-trunc", "--filter",
                                  "name=^/" + name + "$", "--format", "{{.ID}}").decode("ascii").splitlines()
    if not identifiers:
        return
    if len(identifiers) != 1 or re.fullmatch(r"[0-9a-f]{64}", identifiers[0]) is None:
        raise ValueError("Archive reader inventory is malformed")
    identifier = identifiers[0]
    records = json.loads(project.command("inspect", identifier))
    if type(records) is not list or len(records) != 1 or type(records[0]) is not dict:
        raise ValueError("Archive reader inspection is malformed")
    record = records[0]
    config = record.get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    if (record.get("Id") != identifier or record.get("Name") != "/" + name
            or type(labels) is not dict or labels.get("io.secs.archive") != token):
        raise ValueError("Archive reader ownership does not match this invocation")
    project.command("rm", "-f", identifier)


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
                                              "logs", "inspect", "archive-closed", "credential-install", "interpreter-key-install"))
    parser.add_argument("deployment")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--execution-attempt-ref")
    parser.add_argument("--expected-record-digest")
    parser.add_argument("--reason")
    options = parser.parse_args(arguments)
    archive_arguments = (options.execution_attempt_ref, options.expected_record_digest, options.reason)
    if (options.operation == "archive-closed" and not all(archive_arguments)
            or options.operation != "archive-closed" and any(value is not None for value in archive_arguments)):
        parser.error("--execution-attempt-ref, --expected-record-digest and --reason are required only for archive-closed")
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
            elif options.operation == "inspect":
                print(json.dumps(inspect_deployment_journal(repository, options.deployment), ensure_ascii=False))
            elif options.operation == "archive-closed":
                print(json.dumps(archive_deployment_journal(repository, options.deployment,
                      execution_attempt_ref=options.execution_attempt_ref,
                      expected_record_digest=options.expected_record_digest, reason=options.reason), ensure_ascii=False))
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
