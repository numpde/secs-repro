"""Retain only facts needed to recover one outstanding API obligation."""

from dataclasses import dataclass

from secs_inference.provider.job_input import SelectedJobInput


@dataclass(frozen=True, slots=True)
class StartPending:
    provider_ref: str
    selected: SelectedJobInput
    provider_attempt_key: str


@dataclass(frozen=True, slots=True)
class ActiveAttempt:
    start: StartPending
    execution_attempt_ref: str


@dataclass(frozen=True, slots=True)
class TerminalHold:
    action: str
    code: str
    description: str
    detail: str
    request_id: str
    observed_state: str | None = None

    @property
    def reconciling(self) -> bool:
        return self.action == "reconcile_state" and self.observed_state is None


@dataclass(frozen=True, slots=True)
class TerminalPending:
    active: ActiveAttempt
    operation: str
    body: bytes
    hold: TerminalHold | None = None


AttemptState = StartPending | ActiveAttempt | TerminalPending
