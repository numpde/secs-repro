"""Acquire the interpreter's inputs, execute its choice, and retain its reasoning."""

from dataclasses import asdict, dataclass, field
import json
import logging
import os
from pathlib import Path
import shutil
from time import monotonic, sleep

from secs_inference.provider.input_operations import BrukerSelection, CannotAnalyse, JcampSelection
from secs_inference.provider.interpreter import InterpretationSession
from secs_inference.provider.chat import InterpreterError
from secs_inference.provider.execution import AnalysisCancelled, AttemptNoLongerActive, ProviderStopping
from secs_inference.provider.job_api import ApiError, ApiUnavailable
from secs_inference.provider.source_access import InputReadError
from secs_inference.provider.upload_download import UploadDownloadError, UploadUnavailable, download_upload
from secs_inference.provider.worker import WorkerError, WorkerStopUnconfirmed


RESULT_SCHEMA_ID = "secs.elucidation.result.v1"
_LOG = logging.getLogger(__name__)


class UploadSetChanged(InputReadError):
    """Refresh the interpreter's evidence after a reconciled membership change."""

    def __init__(self, uploads):
        super().__init__("The available Uploads changed during input access; choose from the current list.")
        self.uploads = uploads


@dataclass(frozen=True, slots=True)
class AcquiredUpload:
    path: Path = field(repr=False)
    byte_length: int
    content_hash: str


class AttemptSources:
    """Own successful downloads until work finishes or shutdown is confirmed."""

    def __init__(self, api, active, uploads, store, directory: Path, *, deadline: float, max_total_bytes: int):
        self.api, self.active, self.store = api, active, store
        self.uploads = {upload.upload_ref: upload for upload in uploads}
        self.directory = directory
        self.deadline = deadline
        self.max_total_bytes = max_total_bytes
        self.acquired: dict[str, AcquiredUpload] = {}

    def __enter__(self):
        try:
            self.directory.mkdir(mode=0o700)
        except OSError as error:
            reason = os.strerror(error.errno) if error.errno is not None else type(error).__name__
            raise UploadDownloadError(f"the private source workspace could not be created ({reason})") from error
        return self

    def __exit__(self, exc_type, error, traceback):
        """Release sources without replacing the analysis outcome with cleanup."""
        if isinstance(error, WorkerStopUnconfirmed):
            return False
        try:
            shutil.rmtree(self.directory)
        except OSError as cleanup:
            # Admission retries leftovers before accepting another Attempt.
            # Cleanup cannot undo a completed analysis or explain its failure.
            _LOG.error("Cannot remove the private source workspace (%s); cleanup is required before another Attempt", type(cleanup).__name__)
        return False

    def acquire(self, ref: str, *, deadline: float | None = None) -> Path:
        """Retry byte delivery without revisiting an unchanged scientific choice."""
        deadline = self.deadline if deadline is None else min(self.deadline, deadline)
        upload = self.uploads.get(ref)
        if upload is None:
            raise InputReadError("Choose an Upload listed for this Job; the selected identity is not in that list.")
        if ref in self.acquired:
            return self.acquired[ref].path
        remaining_bytes = self.max_total_bytes - sum(item.byte_length for item in self.acquired.values())
        if upload.byte_length > remaining_bytes:
            raise InputReadError(
                f"The selected {upload.byte_length}-byte Upload was not downloaded: only "
                f"{remaining_bytes} bytes remain in this Attempt's {self.max_total_bytes}-byte allowance. "
                "Earlier inspection downloads use the same allowance and cannot be discarded to make room. "
                "Choosing a ZIP member still requires its whole Upload."
            )
        for attempt in range(3):
            if monotonic() >= deadline:
                raise TimeoutError("The input acquisition deadline has elapsed")
            try:
                grant = self._capability(upload)
                path = download_upload(store=self.store, capability=grant, directory=self.directory, deadline=deadline)
            except (ApiError, UploadDownloadError) as error:
                if isinstance(error, ApiError):
                    retryable = isinstance(error, ApiUnavailable) or error.status == 409
                else:
                    retryable = isinstance(error, UploadUnavailable) or error.status in {401, 503, 507}
                if not retryable:
                    raise
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("The input acquisition deadline has elapsed") from error
                if attempt == 2:
                    raise
                sleep(min(1, remaining))
            else:
                self.acquired[ref] = AcquiredUpload(path, grant.byte_length, grant.content_hash)
                return path
        raise AssertionError("Acquisition retries ended without a transfer outcome")

    def _capability(self, upload):
        """Reconcile visibility before treating a missing grant as changed input."""
        try:
            return self.api.capability(self.active, upload)
        except ApiError as error:
            if error.status == 404:
                current = self.api.uploads(self.active)
                if {item.upload_ref: item for item in current} != self.uploads:
                    self.uploads = {item.upload_ref: item for item in current}
                    raise UploadSetChanged(current) from None
            raise


def run_analysis(
    *, api, active, chat, worker, store, directory: Path,
    work_deadline: float, interpretation_seconds: float, max_turns: int,
    max_total_bytes: int,
    check_running=lambda: None,
) -> dict:
    """Return an observed outcome; the execution owner maps it to API status."""
    specification = api.specification(active)
    uploads = api.uploads(active)
    with AttemptSources(api, active, uploads, store, directory,
                        deadline=work_deadline, max_total_bytes=max_total_bytes) as sources:
        interpretation_deadline = min(work_deadline, monotonic() + interpretation_seconds)
        def inspect(source):
            check_running()
            try:
                sources.acquire(source.upload_ref, deadline=interpretation_deadline)
                response = _worker_request(worker, sources, {"operation": "inspect", "source": asdict(source)}, interpretation_deadline, check_running)
            except UploadSetChanged as change:
                return {"access_change": str(change), "current_uploads": [asdict(item) for item in change.uploads]}
            except TimeoutError as error:
                raise InterpreterError("Cannot interpret this Job: the interpretation deadline elapsed during input inspection") from error
            if response["outcome"] == "input_rejected":
                raise InputReadError(response["reason"])
            return response["facts"]

        session = InterpretationSession(chat, specification, uploads, inspect,
                                        deadline=interpretation_deadline, max_turns=max_turns)
        choices = []
        try:
            while True:
                check_running()
                decision = session.select()
                check_running()
                if isinstance(decision, CannotAnalyse):
                    return {"schema_id": RESULT_SCHEMA_ID, "outcome": "cannot_analyse",
                            "explanation": decision.explanation, "input_choices": choices,
                            "interpretation_rejections": session.rejections,
                            "acquired_uploads": _upload_evidence(sources)}
                if isinstance(decision, JcampSelection):
                    reader, ref = "jcamp", decision.source.upload_ref
                elif isinstance(decision, BrukerSelection):
                    reader, ref = "bruker", decision.upload_ref
                else:
                    raise AssertionError("No scientific reader is bound to the interpreted selection")
                choice = {"reader": reader, **asdict(decision)}
                choices.append(choice)
                try:
                    sources.acquire(ref)
                except UploadSetChanged as change:
                    choice["reading_error"] = str(change)
                    session.reject(str(change) + "\n" + json.dumps({"current_uploads": [asdict(item) for item in change.uploads]}, ensure_ascii=False))
                    continue
                except InputReadError as error:
                    choice["reading_error"] = str(error)
                    session.reject(str(error))
                    continue
                response = _worker_request(worker, sources, {"operation": "analyse", "selection": choice}, work_deadline, check_running)
                if response["outcome"] == "input_rejected":
                    choice["reading_error"] = response["reason"]
                    session.reject(response["reason"])
                    continue
                choice["used"] = True
                return {"schema_id": RESULT_SCHEMA_ID, "outcome": "analysed", "explanation": decision.explanation,
                        "interpretation_rejections": session.rejections,
                        "input_choices": choices, "acquired_uploads": _upload_evidence(sources), "analysis": response["analysis"]}
        except (WorkerStopUnconfirmed, ProviderStopping, AnalysisCancelled, AttemptNoLongerActive):
            raise
        except Exception as error:
            # Selection and transfer facts belong to this run, regardless of
            # which component failed. Keep them separate from that component's
            # diagnostic, and leave exception classification unchanged.
            error.analysis_context = {
                "interpretation_rejections": session.rejections, "input_choices": choices,
                "acquired_uploads": _upload_evidence(sources),
            }
            raise


def _worker_request(worker, sources, request, deadline, check_running=lambda: None):
    def check_active():
        check_running()
        try:
            snapshot = sources.api.snapshot(sources.active)
        except ApiUnavailable:
            # A missed observation is not evidence of cancellation. The worker's
            # work deadline still bounds execution while the API recovers.
            return
        if snapshot.state != "in_progress":
            raise AttemptNoLongerActive(snapshot.state)
        if snapshot.job_state == "cancelled":
            # Cancellation stops work by provider policy; the API still permits
            # its failure report. Merely closing the Job does not stop this Attempt.
            raise AnalysisCancelled("Scientific work stopped because the Job was cancelled")
    response = worker.request(request | {"files": {ref: str(item.path) for ref, item in sources.acquired.items()},
                                        "directory": str(sources.directory)}, deadline=deadline,
                              check_active=check_active)
    if response.get("outcome") == "failed":
        error = WorkerError("Scientific analysis could not finish because the worker encountered an internal error; inspect this Attempt's operator diagnostics")
        error.diagnostic = response
        worker.stop()
        raise error
    if response.get("outcome") not in {"inspected", "input_rejected", "analysed"}:
        worker.stop()
        raise WorkerError("Scientific analysis returned an unrecognized operation outcome")
    return response


def _upload_evidence(sources):
    """Report measured transfer identity without another file read or bearer copy."""
    return {ref: {"byte_length": item.byte_length, "content_hash": item.content_hash}
            for ref, item in sources.acquired.items()}
