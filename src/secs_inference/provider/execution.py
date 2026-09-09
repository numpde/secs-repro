"""Settle one durable Attempt before admitting another Job."""

import logging
from secrets import token_hex

from secs_inference.provider.attempt_state import ActiveAttempt, StartPending, TerminalPending
from secs_inference.provider.chat import InterpreterError
from secs_inference.provider.job_api import ApiError, complete_command, fail_command
from secs_inference.provider.job_input import JobInputError
from secs_inference.provider.job_upload import UploadResponseError
from secs_inference.provider.upload_download import UploadDownloadError
from secs_inference.provider.worker import WorkerError, WorkerStopUnconfirmed


_LOG = logging.getLogger(__name__)


class ProviderStopping(RuntimeError):
    """Requested shutdown stops admission and reports any already admitted work."""


class AnalysisCancelled(RuntimeError):
    """An observed Job cancellation requests a policy stop, not a model failure."""


class AttemptNoLongerActive(RuntimeError):
    """A point read already proved the Attempt terminal before work stopped."""

    def __init__(self, state: str):
        self.state = state
        super().__init__(f"Scientific work stopped because its Attempt is already {state}")


class ExecutionLoop:
    """The journal owns recovery facts; work never restarts from ActiveAttempt."""

    def __init__(self, api, journal, analyse, diagnose, before_start=lambda: None):
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
                raise RuntimeError("Cannot recover the retained Attempt with another provider identity; restore the owning provider credential")
            if isinstance(retained, TerminalPending):
                self._publish(retained)
                return True
            if isinstance(retained, ActiveAttempt):
                self._recover_active(retained)
                return True
        else:
            selected = self.api.next_job()
            if selected is None:
                return False
            retained = StartPending(self.api.provider_ref, selected, token_hex(16))
            self.journal.save(retained)
        # Readiness and leftover-source cleanup precede remote admission. If
        # either fails, the retained start key still identifies the same retry.
        self.before_start()
        try:
            active, state = self.api.start(retained)
        except ApiError as error:
            if error.status == 404:
                self.journal.clear()
                _LOG.info("Job %s is no longer available for Attempt admission", retained.selected.job_ref)
                return True
            raise
        if state != "in_progress":
            self.journal.clear()
            _LOG.info("Start replay found Attempt %s already %s", active.execution_attempt_ref, state)
            return True
        self.journal.save(active)
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
        except WorkerStopUnconfirmed:
            # The source workspace and active record survive for recovery.
            raise
        except ProviderStopping:
            terminal = fail_command(active, "provider_stopping", "Analysis stopped because the provider is shutting down.")
        except AnalysisCancelled:
            terminal = fail_command(active, "job_cancelled", "Analysis stopped because the Job was cancelled.")
        except AttemptNoLongerActive as ended:
            self.journal.clear()
            _LOG.info("Scientific work stopped after observing Attempt %s already %s", active.execution_attempt_ref, ended.state)
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
            self.journal.clear()
            _LOG.warning("Retained Attempt %s is no longer visible; no publication is claimed", active.execution_attempt_ref)
            return
        if snapshot.state != "in_progress":
            self.journal.clear()
            _LOG.info("Interrupted Attempt %s is already %s", active.execution_attempt_ref, snapshot.state)
            return
        terminal = fail_command(active, "provider_interrupted", "Analysis was interrupted by a provider restart; it was not rerun.")
        self.journal.save(terminal)
        self._publish(terminal)

    def _publish(self, terminal):
        """Retain exact bytes across outages, even after the work deadline."""
        try:
            self.api.publish(terminal)
        except ApiError as error:
            if error.status not in {404, 409}:
                raise
            try:
                snapshot = self.api.snapshot(terminal.active)
            except ApiError as read_error:
                if read_error.status != 404:
                    raise
                reason = "the API no longer exposes this Attempt"
            else:
                if snapshot.state != "expired":
                    raise ApiError(
                        f"Cannot settle the retained command for Attempt {terminal.active.execution_attempt_ref}: "
                        f"its snapshot is {snapshot.state}, not expired. {error}. "
                        "Inspect this Attempt before continuing; its command remains retained."
                    ) from error
                reason = "the Attempt expired"
            self.journal.clear()
            _LOG.warning("Stopped retrying %s publication for Attempt %s: %s. Delivery was not confirmed.",
                         "result" if terminal.operation == "complete" else "failure report",
                         terminal.active.execution_attempt_ref, reason)
            return
        self.journal.clear()


def _public_failure(error: Exception) -> tuple[str, str]:
    """Publish boundary-owned reasons, including redacted interpreter rejection text."""
    for error_type, code in ((InterpreterError, "interpretation_failed"), (UploadDownloadError, "input_access_failed"),
                             (ApiError, "api_access_failed"), (JobInputError, "api_access_failed"),
                             (UploadResponseError, "api_access_failed"), (WorkerError, "scientific_execution_failed")):
        if isinstance(error, error_type):
            return code, str(error)
    if isinstance(error, TimeoutError):
        return "work_deadline_exceeded", "Analysis stopped because the provider's work deadline elapsed."
    return "provider_execution_failed", "Analysis could not finish because of an internal provider error; the operator can inspect diagnostics recorded for this Attempt."
