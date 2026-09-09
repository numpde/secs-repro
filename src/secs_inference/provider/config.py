"""Decode fixed deployment facts for hello and optional Job execution."""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
import math
from pathlib import Path
import tomllib

from secs_inference.provider.http import HttpsEndpoint, validate_endpoint_config


SCHEMA_ID = "secs.provider.config.v1"
CONFIG_PATH = Path("/run/config/provider/provider.toml")
CA_PATH = Path("/run/config/provider/api-ca.crt")
CREDENTIAL_PATH = Path("/run/secrets/provider/signing.private.json")
INTERPRETER_KEY_PATH = Path("/run/secrets/provider/interpreter.key")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Execution is enabled by this table, never by finding ambient credentials."""

    interpreter_url: str
    interpreter_model: str
    upload_store_origin: str
    work_seconds: float = 1800
    interpretation_seconds: float = 120
    worker_startup_seconds: float = 600
    poll_seconds: float = 5
    max_turns: int = 12
    max_upload_bytes: int = 128 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    interpreter_use_private_ca: bool = False
    upload_store_use_private_ca: bool = False
    interpreter_reasoning_effort: str | None = None

    def __post_init__(self):
        if self.interpreter_reasoning_effort is not None and (
                not isinstance(self.interpreter_reasoning_effort, str) or not self.interpreter_reasoning_effort.strip()):
            raise ValueError("Interpreter reasoning effort must be nonempty text when configured")
        for name in ("interpreter_use_private_ca", "upload_store_use_private_ca"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"Execution {name} must be a boolean")
        for name in ("work_seconds", "interpretation_seconds", "worker_startup_seconds", "poll_seconds"):
            _require_positive_seconds(getattr(self, name), "execution " + name)
        for name in ("max_turns", "max_upload_bytes", "max_total_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"Execution {name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """Validated API facts that do not acquire TLS trust material."""

    origin: str
    expected_topology: str
    connect_timeout_seconds: float
    io_deadline_seconds: float
    ca_file: Path | None

    def __post_init__(self) -> None:
        validate_endpoint_config(
            self.origin,
            self.expected_topology,
            self.connect_timeout_seconds,
            self.io_deadline_seconds,
        )

    def materialize(self) -> HttpsEndpoint:
        """Load configured TLS trust when the process acquires transport."""

        return HttpsEndpoint(
            origin=self.origin,
            expected_topology=self.expected_topology,
            connect_timeout_seconds=self.connect_timeout_seconds,
            io_deadline_seconds=self.io_deadline_seconds,
            ca_file=self.ca_file,
        )


@dataclass(frozen=True, slots=True)
class HelloPolicy:
    """Configured provider presentation and publication cadence."""

    display_name: str
    provider_description: str
    publication_interval_seconds: float
    retry_initial_seconds: float

    def __post_init__(self) -> None:
        _require_positive_seconds(
            self.publication_interval_seconds,
            "hello publication interval",
        )
        _require_positive_seconds(
            self.retry_initial_seconds,
            "hello initial retry delay",
        )


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """The execution table is absent for an explicitly hello-only deployment."""

    endpoint: EndpointConfig
    hello: HelloPolicy
    execution: ExecutionConfig | None = None


def decode_provider_config(raw: bytes) -> ProviderConfig:
    """Decode one closed TOML document; endpoint owners admit their URLs."""

    if type(raw) is not bytes or len(raw) > 65_536:
        raise ValueError("Provider config must be bounded bytes")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("Provider config is not valid TOML") from error
    _require_fields("top level", document, {"api", "hello", "schema_id"}, {"execution"})
    if document["schema_id"] != SCHEMA_ID:
        raise ValueError("Provider config schema is unsupported")

    api = _require_table(
        document,
        "api",
        {"connect_timeout_seconds", "io_deadline_seconds", "origin", "topology"},
        {"use_private_ca"},
    )
    use_private_ca = api.get("use_private_ca", False)
    if type(use_private_ca) is not bool:
        raise ValueError("Provider API private-CA selection must be a boolean")
    endpoint = EndpointConfig(
        origin=api["origin"],
        expected_topology=api["topology"],
        connect_timeout_seconds=api["connect_timeout_seconds"],
        io_deadline_seconds=api["io_deadline_seconds"],
        ca_file=CA_PATH if use_private_ca else None,
    )

    hello = _require_table(
        document,
        "hello",
        {
            "display_name",
            "provider_description",
            "publication_interval_seconds",
            "retry_initial_seconds",
        },
    )
    policy = HelloPolicy(
        display_name=hello["display_name"],
        provider_description=hello["provider_description"],
        publication_interval_seconds=hello["publication_interval_seconds"],
        retry_initial_seconds=hello["retry_initial_seconds"],
    )
    execution = None
    if "execution" in document:
        required = {"interpreter_url", "interpreter_model", "upload_store_origin"}
        table = _require_table(document, "execution", required,
                               set(ExecutionConfig.__dataclass_fields__) - required)
        execution = ExecutionConfig(**table)
    return ProviderConfig(endpoint=endpoint, hello=policy, execution=execution)


def _require_table(
    document: dict[str, object],
    name: str,
    required: Set[str],
    optional: Set[str] = frozenset(),
) -> dict[str, object]:
    value = document[name]
    if type(value) is not dict:
        raise ValueError(f"Provider config [{name}] must be a table")
    _require_fields(name, value, required, optional)
    return value


def _require_fields(
    name: str,
    value: dict[str, object],
    required: Set[str],
    optional: Set[str] = frozenset(),
) -> None:
    actual = set(value)
    if not required <= actual or actual - required - optional:
        raise ValueError(f"Provider config {name} has invalid fields")


def _require_positive_seconds(value: object, name: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive finite seconds")
