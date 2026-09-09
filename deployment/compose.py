"""Operate one owned Compose project without ambient Docker/Compose settings.

Adapted from numpde/nmrpeak-repro deployment/provider_deployment.py at
ae53376b9bb1dc572d3d7bce6592358080e08b18: explicit rendering, local immutable
images, inspected ownership, and ordered shutdown. Recipes own the service
graph; callers supply the shutdown order. This module never loads credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory


_ID = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
_ENV = {"PATH": "/usr/bin:/bin"}


@dataclass(frozen=True)
class ComposeProject:
    """One checkout's project and the order in which its services must stop.

    Use the default local Docker engine. Rootless/remote engines are not part
    of this deployment contract. The caller serializes lifecycle operations;
    other processes with engine access remain trusted administrators.
    """

    repository: Path
    name: str
    stop_order: tuple[str, ...]

    def render(self, recipe: Path, env_file: Path, environment: dict[str, str]) -> dict:
        """Normalize the maintained recipe; do not build, pull, or start services."""
        raw = self.command(
            "compose", "--project-name", self.name,
            "--project-directory", str(self.repository),
            "--env-file", str(env_file), "--file", str(recipe),
            "config", "--format", "json", environment=environment,
        )
        plan = json.loads(raw)
        if not isinstance(plan, dict) or set(plan.get("services", {})) != set(self.stop_order):
            raise ValueError("Compose rendered a different service set from this provider's recipe.")
        for role, service in plan["services"].items():
            if not _IMAGE.fullmatch(service.get("image", "")) or "build" in service:
                raise ValueError(f"Service {role} needs a local sha256 image ID, not a tag or build.")
        return plan

    def inventory(self) -> dict[str, dict]:
        """Inspect all project containers, including stopped ones; reject foreign ownership."""
        identifiers = self.command(
            "ps", "--all", "--no-trunc", "--quiet", "--filter",
            f"label=com.docker.compose.project={self.name}",
        ).decode("ascii").splitlines()
        if not identifiers:
            return {}
        if any(not _ID.fullmatch(value) for value in identifiers):
            raise ValueError("Docker returned malformed project container IDs.")
        records = json.loads(self.command("inspect", *identifiers))
        if not isinstance(records, list) or len(records) != len(identifiers):
            raise ValueError("Docker did not inspect every project container.")
        services = {}
        seen = set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("Config"), dict):
                raise ValueError("Docker returned a malformed container record.")
            labels = record["Config"].get("Labels")
            if not isinstance(labels, dict) or not isinstance(record.get("State"), dict):
                raise ValueError("Docker returned malformed container labels or state.")
            role = labels.get("com.docker.compose.service")
            identity = record.get("Id")
            if (
                identity not in identifiers or identity in seen
                or role not in self.stop_order or role in services
                or labels.get("com.docker.compose.project") != self.name
                or labels.get("com.docker.compose.oneoff") != "False"
                or labels.get("com.docker.compose.project.working_dir") != str(self.repository)
                or not isinstance(record.get("State", {}).get("Running"), bool)
            ):
                raise ValueError("Project contains a foreign or ambiguous container; no changes made.")
            seen.add(identity)
            services[role] = record
        return services

    def start(self, plan: dict) -> dict[str, dict]:
        """Start the exact rendered plan, refusing to replace any running service.

        Explicit down/up is required for reconfiguration. A failed startup may
        leave containers; retain them for status/log inspection, not rollback.
        Running containers alone do not prove API registration or model readiness.
        """
        if any(record["State"]["Running"] for record in self.inventory().values()):
            raise ValueError("Deployment is already running; use down before applying configuration.")
        for service in plan["services"].values():
            self.command("image", "inspect", service["image"])
        with TemporaryDirectory(prefix="provider-compose-") as temporary:
            path = Path(temporary) / "compose.json"
            # Compose will parse this normalized document again. Preserve
            # literal dollars in paths instead of interpolating them twice.
            path.write_text(json.dumps(plan).replace("$", "$$"))
            try:
                self.command(
                    "compose", "--env-file", "/dev/null", "--project-name", self.name,
                    "--project-directory", str(self.repository), "--file", str(path),
                    "up", "--detach", "--wait", "--wait-timeout", "60",
                    "--no-build", "--pull", "never", "--force-recreate", timeout=90,
                )
            except (OSError, RuntimeError) as error:
                error.add_note("Startup is unconfirmed; containers may remain. Inspect status and logs.")
                raise
        records = self.inventory()
        if set(records) != set(plan["services"]) or not all(
            record["State"]["Running"] and record["Image"] == plan["services"][role]["image"]
            for role, record in records.items()
        ):
            raise RuntimeError("Startup is unconfirmed: expected containers are not running with the selected images.")
        return records

    def stop(self) -> dict[str, dict]:
        """Stop admitted IDs in dependency order, preserving containers and durable data.

        Stop the job-accepting provider first so it can finish/cancel work while
        workers still exist. If stopping it fails, do not proceed to the worker.
        This operation needs neither deployment config nor credential contents.
        """
        records = self.inventory()
        for record in records.values():
            grace = record["Config"].get("StopTimeout")
            if record["State"]["Running"] and (type(grace) is not int or grace < 0):
                raise ValueError(
                    f"Container {record['Id']} has no finite shutdown grace period; "
                    "no containers stopped."
                )
        for role in self.stop_order:
            record = records.get(role)
            if record is not None and record["State"]["Running"]:
                # The running container owns its grace period, even if the
                # recipe has since changed. Let Docker apply that same value.
                self.command("stop", record["Id"], timeout=record["Config"]["StopTimeout"] + 30)
        stopped = self.inventory()
        if any(record["State"]["Running"] for record in stopped.values()):
            raise RuntimeError("Shutdown is unconfirmed: a project container is still running.")
        return stopped

    def logs(self) -> None:
        """Show the last 80 log lines per admitted service, without reading secret files."""
        for role, record in self.inventory().items():
            print(f"{role}:", flush=True)
            self.command("logs", "--tail", "80", record["Id"], capture=False)

    def command(self, *arguments: str, environment=None, timeout=60, capture=True) -> bytes:
        """Execute fixed argv against the local engine with no inherited configuration."""
        try:
            result = subprocess.run(
                ("/usr/bin/docker", "--context", "default", *arguments),
                cwd=self.repository, env=_ENV | (environment or {}), timeout=timeout,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Docker {arguments[0]} did not finish within {timeout} seconds.") from error
        if result.returncode:
            # Engine diagnostics can contain interpolated config. Keep them out
            # of exceptions; normal provider logs remain a separate operator action.
            missing = re.search(rb"required variable ([A-Z][A-Z0-9_]*) is missing", result.stderr or b"")
            if arguments[0] == "compose" and missing:
                variable = missing[1].decode("ascii")
                raise RuntimeError(f"Cannot render deployment: set {variable} in deployment.env.")
            raise RuntimeError(f"Docker {arguments[0]} failed (exit {result.returncode}).")
        return result.stdout or b""
