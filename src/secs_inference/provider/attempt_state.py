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
class TerminalPending:
    active: ActiveAttempt
    operation: str
    body: bytes


AttemptState = StartPending | ActiveAttempt | TerminalPending
