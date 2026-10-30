"""Decode fixed deployment facts for hello and optional Job execution."""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
import math
from pathlib import Path
import tomllib

from secs_inference.provider.configuration_error import ConfigurationError


SCHEMA_ID = "secs.provider.config.v3"
CONFIG_PATH = Path("/run/config/provider/provider.toml")
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
            raise ConfigurationError("Interpreter reasoning effort must be nonempty text when configured")
        for name in ("interpreter_use_private_ca", "upload_store_use_private_ca"):
            if type(getattr(self, name)) is not bool:
                raise ConfigurationError(f"Execution {name} must be a boolean")
        for name in ("work_seconds", "interpretation_seconds", "worker_startup_seconds", "poll_seconds"):
            _require_positive_seconds(getattr(self, name), "execution " + name)
        for name in ("max_turns", "max_upload_bytes", "max_total_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ConfigurationError(f"Execution {name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class HelloPolicy:
    """Configured provider presentation and publication cadence."""

    display_name: str
    provider_description: str
    publication_interval_seconds: float
    retry_initial_seconds: float
    mock_index: bool = False
    mock_runner: bool = False

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

    shared_settings: Path
    api: dict
    delivery_exchange_seconds: float
    delivery_retry_initial_seconds: float
    delivery_retry_max_seconds: float
    hello: HelloPolicy
    execution: ExecutionConfig | None = None


def decode_provider_config(raw: bytes) -> ProviderConfig:
    """Decode one closed TOML document; endpoint owners admit their URLs."""

    if type(raw) is not bytes or len(raw) > 65_536:
        raise ConfigurationError("Cannot load provider configuration: supply a TOML file of at most 65536 bytes")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError("Provider config is not valid TOML") from error
    _require_fields("top level", document, {"shared", "api", "delivery", "hello", "schema_id"}, {"execution"})
    if document["schema_id"] != SCHEMA_ID:
        raise ConfigurationError(f"Cannot load provider configuration: schema_id must be {SCHEMA_ID!r}")

    shared = _require_table(document, "shared", {"settings_file"})
    definition_file = shared["settings_file"]
    if (type(definition_file) is not str or not definition_file.startswith("/")
            or any(char in definition_file for char in ("\0", "\r", "\n", ","))
            or not Path(definition_file).name or ".." in Path(definition_file).parts
            or str(Path(definition_file)) != definition_file):
        raise ConfigurationError("Shared settings_file must be one normalized absolute path")
    api = document["api"]
    if type(api) is not dict:
        raise ConfigurationError("Provider config [api] must be a table")
    if api.get("credential_file") != "/run/secrets/provider/provider.signing.private.json":
        raise ConfigurationError("API credential_file must select the installed provider credential")
    ca = api.get("ca_file")
    if ca is not None and (type(ca) is not str or str(Path(ca)) != ca
                           or Path(ca).parent != Path("/run/config/provider")):
        raise ConfigurationError("API ca_file must select a file in /run/config/provider")
    delivery = _require_table(document, "delivery", {"exchange_seconds", "retry_initial_seconds", "retry_max_seconds"})
    for key, value in delivery.items():
        _require_positive_seconds(value, "delivery " + key)
    if delivery["retry_initial_seconds"] > delivery["retry_max_seconds"]:
        raise ConfigurationError("Delivery initial retry delay cannot exceed its maximum")

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
    return ProviderConfig(shared_settings=Path(definition_file), api=api,
                          delivery_exchange_seconds=delivery["exchange_seconds"],
                          delivery_retry_initial_seconds=delivery["retry_initial_seconds"],
                          delivery_retry_max_seconds=delivery["retry_max_seconds"], hello=policy, execution=execution)


def _require_table(
    document: dict[str, object],
    name: str,
    required: Set[str],
    optional: Set[str] = frozenset(),
) -> dict[str, object]:
    value = document[name]
    if type(value) is not dict:
        raise ConfigurationError(f"Provider config [{name}] must be a table")
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
        problems = []
        if required - actual:
            problems.append("missing required fields: " + ", ".join(sorted(required - actual)))
        if actual - required - optional:
            # Only known field names are safe to print: a mistyped TOML key
            # could itself be a pasted credential or other private value.
            problems.append(f"{len(actual - required - optional)} unrecognized field(s); allowed fields: "
                            + ", ".join(sorted(required | optional)))
        raise ConfigurationError(f"Cannot load provider config {name}: " + "; ".join(problems))


def _require_positive_seconds(value: object, name: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{name} must be positive finite seconds")
