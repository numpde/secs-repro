"""Settle one durable Attempt before admitting another Job."""

import logging
import json
from dataclasses import replace
from secrets import token_hex

from secs_inference.provider.attempt_state import ActiveAttempt, StartPending, TerminalPending, TerminalHold, terminal_recovery_facts
from secs_inference.provider.chat import InterpreterError
from secs_inference.provider.job_api import ApiError, ApiUnavailable, complete_command, fail_command
from secs_inference.provider.job_input import JobInputError
from secs_inference.provider.job_upload import UploadResponseError
from secs_inference.provider.upload_download import UploadDownloadError
from secs_inference.provider.worker import WorkerError, WorkerStopUnconfirmed
from secs_inference.provider.diagnostics import exception_evidence
from secs_inference.provider.attempt_store import JournalError, provider_error_details


_LOG = logging.getLogger(__name__)


class ProviderStopping(RuntimeError):
    """Requested shutdown stops admission and reports any already admitted work."""


class AnalysisCancelled(RuntimeError):
    """An observed Job cancellation requests a policy stop, not a model failure."""


class WorkDeadlineExceeded(TimeoutError):
    """An execution boundary supplies a public-safe phase and stop outcome."""


class AttemptNoLongerActive(RuntimeError):
    """A point read already proved the Attempt terminal before work stopped."""

    def __init__(self, state: str):
        self.state = state
        super().__init__(f"Scientific work stopped because its Attempt is already {state}")


class ExecutionLoop:
    """The journal owns recovery facts; work never restarts from ActiveAttempt."""

    def __init__(self, api, journal, analyse, diagnose, before_start=lambda start: None):
        self.api, self.journal = api, journal
        self.analyse, self.diagnose = analyse, diagnose
        self.before_start = before_start

    def step(self) -> bool:
        """Settle retained state or admit one Job; return False for an empty feed."""
        retained = self.journal.load()
        if retained is not None:
            start = retained if isinstance(retained, StartPending) else (
                retained.start if isinstance(retained, ActiveAttempt) else retained.active.start
            )
            if start.provider_ref != self.api.provider_ref:
                raise JournalError("Cannot recover the retained Attempt with another provider identity; restore the owning provider credential")
            if isinstance(retained, TerminalPending):
                self._publish(retained)
                return True
            if isinstance(retained, ActiveAttempt):
                self._recover_active(retained)
                return True
            _LOG.info("Job %s: recovering retained start intent %s; prior admission may be unconfirmed",
                      retained.selected.job_ref, retained.provider_attempt_key)
        else:
            selected = self.api.next_job()
            if selected is None:
                return False
            retained = StartPending(self.api.provider_ref, selected, token_hex(16))
            self.journal.save(retained)
            _LOG.info("Job %s: start intent %s retained; preparing for admission", selected.job_ref, retained.provider_attempt_key)
        # Readiness and leftover-source cleanup precede remote admission. If
        # either fails, the retained start key still identifies the same retry.
        self.before_start(retained)
        _LOG.info("Job %s: sending retained start intent %s", retained.selected.job_ref, retained.provider_attempt_key)
        try:
            active, state = self.api.start(retained)
        except ApiError as error:
            if error.status == 404:
                _LOG.info("Job %s is no longer available for Attempt admission", retained.selected.job_ref)
                self.journal.clear()
                return True
            raise
        if state != "in_progress":
            _LOG.info("Start replay found Attempt %s already %s", active.execution_attempt_ref, state)
            self.journal.clear()
            return True
        _LOG.info("Job %s: API confirmed Attempt %s in progress; local active-state retention is pending",
                  retained.selected.job_ref, active.execution_attempt_ref)
        self.journal.save(active)
        _LOG.info("Attempt %s: active state retained; beginning analysis", active.execution_attempt_ref)
        # Journal uncertainty must escape, never become an Attempt failure.
        # Only analysis and report construction belong to this translation.
        failed_report = None
        try:
            report = self.analyse(active)
            if report.get("outcome") == "cannot_analyse":
                terminal = fail_command(active, "cannot_analyse",
                    "No analysis was produced. Interpreter explanation: " + report["explanation"])
                failed_report = report
            else:
                terminal = complete_command(active, report)
        except WorkerStopUnconfirmed as stopped:
            # The source workspace and active record survive for recovery.
            try:
                self.diagnose(active, stopped)
            except Exception as retention_error:
                # Keep the lifecycle-controlling exception and its original
                # cause. The secondary storage failure remains in the log.
                _LOG.error("Cannot retain diagnostics for Attempt %s; worker stop remains unconfirmed and settlement is halted: %s",
                           active.execution_attempt_ref, json.dumps(exception_evidence(retention_error, boundary_details=provider_error_details)))
            raise
        except ProviderStopping:
            terminal = fail_command(active, "provider_stopping", "Analysis stopped because the provider is shutting down.")
        except AnalysisCancelled:
            terminal = fail_command(active, "job_cancelled", "Analysis stopped because the Job was cancelled.")
        except AttemptNoLongerActive as ended:
            _LOG.info("Scientific work stopped after observing Attempt %s already %s", active.execution_attempt_ref, ended.state)
            self.journal.clear()
            return True
        except Exception as error:
            self.diagnose(active, error)
            terminal = fail_command(active, *_public_failure(error))
        # The API failure endpoint cannot attach a result. Keep the full report
        # privately before publication; storage uncertainty must escape here.
        if failed_report is not None:
            self.journal.record_report(active, failed_report)
        self.journal.save(terminal)
        self._publish(terminal)
        return True

    def _recover_active(self, active):
        """Interrupted work can be reported, but cannot be replayed as inference."""
        try:
            snapshot = self.api.snapshot(active)
        except ApiError as error:
            if error.status != 404:
                raise
            _LOG.warning("Retained Attempt %s is no longer visible; no publication is claimed", active.execution_attempt_ref)
            self.journal.clear()
            return
        if snapshot.state != "in_progress":
            _LOG.info("Interrupted Attempt %s is already %s", active.execution_attempt_ref, snapshot.state)
            self.journal.clear()
            return
        terminal = fail_command(active, "provider_interrupted", "Analysis was interrupted by a provider restart; it was not rerun.")
        self.journal.save(terminal)
        self._publish(terminal)

    def _publish(self, terminal):
        """Retain exact bytes across outages, even after the work deadline."""
        if terminal.hold is not None:
            if terminal.hold.reconciling:
                self._reconcile_terminal(terminal)
            self._held(terminal)
        _LOG.info("Attempt %s: sending retained %s command", terminal.active.execution_attempt_ref, terminal.operation)
        try:
            self.api.publish(terminal)
        except ApiError as error:
            if error.status not in {404, 409}:
                raise
            facts = error.diagnostic or {}
            action = facts.get("conflict_action") if facts.get("problem_verified") else None
            if action not in {"do_not_resend", "reconcile_original"}:
                action = "reconcile_state"
            hold = TerminalHold(action, facts.get("code") or "unavailable",
                                facts.get("conflict_description") or "Reconcile the original retained command against the observed Attempt state.",
                                facts.get("detail") or "The API refused this terminal command.",
                                facts.get("request_id") or "unavailable")
            held = replace(terminal, hold=hold)
            self.journal.save(held)
            if held.hold.reconciling:
                self._reconcile_terminal(held)
            self._held(held)
        _LOG.info("Attempt %s: %s publication confirmed; journal retirement is pending",
                  terminal.active.execution_attempt_ref, terminal.operation)
        self.journal.clear()
        _LOG.info("Attempt %s: journal retirement confirmed", terminal.active.execution_attempt_ref)

    def _reconcile_terminal(self, terminal):
        """A retained read obligation cannot turn back into publication on restart."""
        try:
            observed = self.api.snapshot(terminal.active).state
        except ApiError as error:
            if error.status == 404:
                observed = "not_visible"
            else:
                automatic = isinstance(error, ApiUnavailable)
                facts = terminal_recovery_facts(terminal) | {
                    "automatic_reads": "retry_with_backoff" if automatic else "stopped",
                    "next_actor": "provider" if automatic else "provider_operator",
                    "next_action": "retry only the Attempt read" if automatic else "investigate the rejected Attempt read before restarting",
                }
                kind = ApiUnavailable if automatic else ApiError
                raise kind(
                    f"Attempt {facts['execution_attempt_ref']}: exact {facts['operation']} command retained; delivery is unconfirmed. "
                    "Publication remains paused across restart while current state is unverified. "
                    f"API code {terminal.hold.code}; request ID {terminal.hold.request_id}: {terminal.hold.detail} "
                    + ("The provider will automatically retry only the read with backoff. " if automatic
                       else "Automatic reads stopped; the provider operator must investigate the rejected read before restarting. ")
                    + f"Read failure: {error}",
                    diagnostic={"terminal_reconciliation": facts, "read": error.diagnostic},
                ) from error
        held = replace(terminal, hold=replace(terminal.hold, observed_state=observed))
        self.journal.save(held)
        self._held(held)

    def _held(self, terminal):
        hold = terminal.hold
        facts = terminal_recovery_facts(terminal)
        raise ApiError(
            f"Attempt {facts['execution_attempt_ref']}: exact {facts['operation']} command retained; "
            "delivery is not confirmed. Automatic resends and new work are stopped, including after restart. "
            f"API code {hold.code}; action {hold.action}; request ID {hold.request_id}. "
            + (f"Observed Attempt state: {hold.observed_state}. " if hold.observed_state else "")
            + hold.detail + " " + hold.description + f" The provider operator must {facts['next_action']}. "
            "This hold does not report a new computation outcome.",
            diagnostic={"terminal_hold": facts},
        )


def _public_failure(error: Exception) -> tuple[str, str]:
    """Publish boundary-owned reasons, including redacted interpreter rejection text."""
    if isinstance(error, ApiError) and error.diagnostic and error.diagnostic.get("problem_verified") is False:
        evidence = error.diagnostic
        return "api_access_failed", (
            "This Attempt could not finish because the provider could not verify a required API response "
            f"(HTTP {evidence['status']} for request {evidence.get('request_id') or 'unavailable'}). "
            "Ask the provider operator to investigate using this Attempt's reference."
        )
    for error_type, code in ((InterpreterError, "interpretation_failed"), (UploadDownloadError, "input_access_failed"),
                             (ApiError, "api_access_failed"), (JobInputError, "api_access_failed"),
                             (UploadResponseError, "api_access_failed"), (WorkerError, "scientific_execution_failed")):
        if isinstance(error, error_type):
            return code, str(error)
    if isinstance(error, WorkDeadlineExceeded):
        return "work_deadline_exceeded", str(error)
    if isinstance(error, TimeoutError):
        return "provider_execution_failed", "Analysis stopped after an internal operation timed out; the operator can inspect diagnostics recorded for this Attempt."
    return "provider_execution_failed", "Analysis could not finish because of an internal provider error; the operator can inspect diagnostics recorded for this Attempt."
