"""Own terminal analysis policy and public failure reasons.

Only an explicit analysed report may complete an Attempt. Input correction,
settlement safety, and delivery uncertainty remain with their execution owners.
"""

from enum import StrEnum


class AnalysisOutcome(StrEnum):
    ANALYSED = "analysed"
    CANNOT_ANALYSE = "cannot_analyse"
    NO_STARTING_CANDIDATES = "no_starting_candidates"


NO_STARTING_CANDIDATES_MESSAGE = (
    "No elucidation was produced. Candidate retrieval returned no starting molecules under the configured search. "
    "Graph GA was not run. This does not establish that the formula is invalid "
    "or that no matching structure exists."
)
PROVIDER_STOPPING = ("provider_stopping", "Analysis stopped because the provider is shutting down.")
JOB_CANCELLED = ("job_cancelled", "Analysis stopped because the Job was cancelled.")
PROVIDER_INTERRUPTED = ("provider_interrupted", "Analysis was interrupted by a provider restart; it was not rerun.")


class WorkDeadlineExceeded(TimeoutError):
    """An execution boundary supplies a public-safe phase and stop outcome."""


def report_failure(report: dict) -> tuple[str, str] | None:
    """Return a public failure code and message, or None for an analysed report.

    Missing or unknown outcomes are classification errors.
    """
    outcome = report.get("outcome")
    if outcome == AnalysisOutcome.ANALYSED:
        return None
    if outcome == AnalysisOutcome.CANNOT_ANALYSE:
        return outcome, "No analysis was produced. Interpreter explanation: " + report["explanation"]
    if outcome == AnalysisOutcome.NO_STARTING_CANDIDATES:
        return outcome, NO_STARTING_CANDIDATES_MESSAGE
    raise ValueError("Cannot classify the analysis report: its outcome is missing or unrecognized")


def exception_failure(error: Exception) -> tuple[str, str]:
    """Return a public failure code and message from boundary-owned evidence."""
    # Scientific workers share report vocabulary without loading controller dependencies.
    from secs_inference.provider.chat import InterpreterError
    from secs_inference.provider.job_api import ApiError
    from secs_inference.provider.job_input import JobInputError
    from secs_inference.provider.job_upload import UploadResponseError
    from secs_inference.provider.upload_download import UploadDownloadError
    from secs_inference.provider.worker import WorkerError

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
