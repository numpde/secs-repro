"""Carry interpreter evidence to an injected operator-owned event sink."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class InterpreterEndpointFailed:
    """Explain why one endpoint yielded no usable interpretation."""

    configuration_id: str
    failure_kind: str
    failure_reason: str
    failure_state: str | None = None
