"""Retain only facts needed to recover one outstanding API obligation."""

from dataclasses import asdict, dataclass
from hashlib import sha256

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
    local_phase: str | None = None

    def __post_init__(self):
        if self.local_phase not in {None, "preparing", "running"}:
            raise ValueError("Invalid retained execution phase")


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


def terminal_recovery_facts(terminal):
    hold = terminal.hold
    facts = {**asdict(hold), "execution_attempt_ref": terminal.active.execution_attempt_ref,
             "operation": terminal.operation, "command_fingerprint": "sha256:" + sha256(terminal.body).hexdigest(),
             "command_retained": True, "delivery": "unconfirmed",
             "automatic_resends": "stopped_including_restart", "new_work": "stopped",
             "next_actor": "provider_operator",
             "next_action": "reconcile the original command and API outcome; involve the provider developer to investigate any mismatch"}

    if hold.reconciling:
        facts.update(automatic_reads="retry_with_backoff", next_actor="provider", next_action="retry only the Attempt read")
    else:
        facts["automatic_reads"] = "stopped"
    return facts
