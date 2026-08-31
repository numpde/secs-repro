"""Decode the transport, presentation, and cadence for provider hello."""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
import math
from pathlib import Path
import tomllib

from secs_inference.provider.http import HttpsEndpoint, validate_endpoint_config


SCHEMA_ID = "secs.provider.hello_config.v1"
CONFIG_PATH = Path("/run/config/provider/provider.toml")
CA_PATH = Path("/run/config/provider/api-ca.crt")
CREDENTIAL_PATH = Path("/run/secrets/provider/signing.private.json")


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
    """Complete local configuration needed by the hello-only process."""

    endpoint: EndpointConfig
    hello: HelloPolicy


def decode_provider_config(raw: bytes) -> ProviderConfig:
    """Decode one closed TOML document into hello-owned values."""

    if type(raw) is not bytes or len(raw) > 65_536:
        raise ValueError("Provider config must be bounded bytes")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("Provider config is not valid TOML") from error
    _require_fields("top level", document, {"api", "hello", "schema_id"})
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
    return ProviderConfig(endpoint=endpoint, hello=policy)


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
