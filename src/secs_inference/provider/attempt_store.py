"""One process owns one durable Attempt record; no work queue is hidden here.

Atomic replacement and fsync ordering follow nmrpeak-repro's Attempt store.
Durability uncertainty stops further effects; restart reads whichever complete
record survived instead of guessing whether the previous write committed.
"""

from base64 import b64decode, b64encode
from dataclasses import asdict
import fcntl
import json
import logging
import os
import re
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile

from secs_inference.provider.attempt_state import ActiveAttempt, AttemptState, StartPending, TerminalPending
from secs_inference.provider.canonical_json import canonical_json_bytes
from secs_inference.provider.chat import InterpreterError
from secs_inference.provider.job_input import selected_job_input
from secs_inference.provider.job_api import ApiError
from secs_inference.provider.upload_download import UploadDownloadError
from secs_inference.provider.response_json import response_object
from secs_inference.provider.diagnostics import exception_evidence
from secs_inference.provider.worker import WorkerError
from secs_inference.provider.configuration_error import ConfigurationError
from secs_inference.provider.analysis_evidence import AnalysisContext


_LOG = logging.getLogger(__name__)


class JournalError(RuntimeError):
    """An owned journal failure; no later API effect is authorized by uncertain storage."""


class AttemptStore:
    """Hold the controller's single-writer lock; retained state survives release."""

    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, exist_ok=True)
        parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        self.directory = directory
        self._directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._lock_fd = -1
        self._usable = True
        try:
            status = os.fstat(self._directory_fd)
            if status.st_uid != os.getuid() or status.st_mode & 0o077:
                raise JournalError("Cannot own the Attempt journal: its directory must be private to the provider user")
            self._lock_fd = os.open("owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self._directory_fd)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._close_after_failure()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, error, traceback):
        if error is None:
            self.close()
        else:
            self._close_after_failure()

    def close(self) -> None:
        """Release ownership without changing the retained obligation."""
        descriptors = (("ownership lock", self._lock_fd), ("journal directory", self._directory_fd))
        # Linux can release an fd even when close reports an error. Detach both
        # before closing either: retrying a stale number could close a new file.
        self._lock_fd = self._directory_fd = -1
        self._usable = False
        failures = []
        for role, descriptor in descriptors:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError as error:
                    failures.append((role, error))
        if failures:
            reasons = "; ".join(f"{role}: {os.strerror(error.errno) if error.errno is not None else 'unclassified operating-system error'}"
                                for role, error in failures)
            cause = failures[0][1] if len(failures) == 1 else ExceptionGroup("Journal descriptor release failures", [error for _, error in failures])
            raise JournalError(f"Cannot confirm release of the Attempt journal resources: {reasons}. "
                               "Previously confirmed record durability is unchanged; this owner cannot continue.") from cause

    def _close_after_failure(self) -> None:
        """Keep the initiating failure authoritative while reporting failed release."""
        try:
            self.close()
        except JournalError as error:
            _LOG.error("Attempt journal release also failed while stopping: %s",
                       json.dumps(exception_evidence(error, boundary_details=provider_error_details)))

    def load(self) -> AttemptState | None:
        """Read the last complete record; malformed state requires operator repair."""
        self._require_usable()
        try:
            descriptor = os.open("attempt.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._directory_fd)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise JournalError("Cannot recover the Attempt: the journal record is not a regular file")
            raw = stream.read(3 * 1024 * 1024 + 1)
        if len(raw) > 3 * 1024 * 1024:
            raise JournalError("Cannot recover the Attempt: the journal record exceeds its byte limit")
        try:
            return _decode(response_object(raw))
        except (ValueError, TypeError, KeyError, RecursionError):
            raise JournalError("Cannot recover the Attempt: the retained journal record is unreadable") from None

    def save(self, state: AttemptState) -> None:
        """Confirm file and directory durability before the next external effect."""
        self._require_usable()
        document = _encode(state)
        raw = canonical_json_bytes(document)
        staging = None
        phase = "creating the staging file"
        try:
            with NamedTemporaryFile(dir=self.directory, prefix=".attempt-", delete=False) as stream:
                staging = Path(stream.name)
                phase = "writing and syncing the staging file"
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            phase = "replacing the retained record"
            os.replace(staging, "attempt.json", dst_dir_fd=self._directory_fd)
            phase = "syncing the journal directory"
            os.fsync(self._directory_fd)
        except BaseException as error:
            self._usable = False
            if staging is not None:
                try:
                    staging.unlink(missing_ok=True)
                except OSError as cleanup:
                    _LOG.error("Cannot remove the staging file for journal %s after a failed %s write: %s",
                               self.directory, document["stage"], json.dumps(exception_evidence(cleanup)))
            if isinstance(error, OSError):
                reason = os.strerror(error.errno) if error.errno is not None else "an operating-system error occurred without a recorded reason"
                raise JournalError(f"Cannot retain Attempt journal state {document['stage']!r} while {phase}: {reason}. "
                                   "Durability is unconfirmed; restart and recover before further API effects.") from error
            raise

    def diagnose(self, active: ActiveAttempt, error: Exception) -> None:
        """Retain frames and selected boundary evidence under the Attempt identity."""
        document = {"execution_attempt_ref": active.execution_attempt_ref,
                    **exception_evidence(error, boundary_details=provider_error_details)}
        context = getattr(error, "analysis_context", None)
        if isinstance(context, AnalysisContext):
            document["analysis"] = asdict(context)
        self._write_evidence(active, "diagnostic", document)

    def record_report(self, active: ActiveAttempt, report: dict) -> None:
        """Retain the full inability report that the API failure cannot attach."""
        self._write_evidence(active, "report", report)

    def _write_evidence(self, active: ActiveAttempt, kind: str, document: dict) -> None:
        """Create private Attempt evidence once, without replacing earlier facts."""
        self._require_usable()
        name = active.execution_attempt_ref.removeprefix("execution_attempt:sha256:") + f".{kind}.json"
        raw = canonical_json_bytes(document)
        phase = "creating the evidence file"
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._directory_fd)
            with os.fdopen(descriptor, "wb") as stream:
                phase = "writing and syncing the evidence file"
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            phase = "syncing the journal directory"
            os.fsync(self._directory_fd)
        except OSError as error:
            self._usable = False
            reason = os.strerror(error.errno) if error.errno is not None else "an operating-system error occurred without a recorded reason"
            raise JournalError(f"Cannot confirm {kind} retention for Attempt {active.execution_attempt_ref} while {phase}: {reason}. "
                               "Evidence retention is unconfirmed; Attempt settlement has stopped.") from error

    def clear(self) -> None:
        """Retire only a reconciled obligation; this is not cancellation."""
        self._require_usable()
        phase = "removing the retained record"
        try:
            os.unlink("attempt.json", dir_fd=self._directory_fd)
            phase = "syncing the journal directory"
            os.fsync(self._directory_fd)
        except OSError as error:
            self._usable = False
            reason = os.strerror(error.errno) if error.errno is not None else "an operating-system error occurred without a recorded reason"
            raise JournalError(f"Cannot confirm Attempt journal retirement while {phase}: {reason}. "
                               "Retirement durability is unconfirmed; restart and reconcile before further API effects.") from error

    def _require_usable(self):
        if not self._usable or self._directory_fd < 0:
            raise JournalError("The Attempt journal has no confirmed writable state; restart and recover before further API effects")


def provider_error_details(error: BaseException) -> dict:
    """Select only diagnostics whose exception boundary owns their disclosure."""
    if isinstance(error, (ConfigurationError, JournalError)):
        return {"message": str(error)}
    for kind, name in ((InterpreterError, "interpreter"), (ApiError, "api"),
                       (UploadDownloadError, "upload"), (WorkerError, "worker")):
        if isinstance(error, kind):
            diagnostic = getattr(error, "diagnostic", None)
            return {"message": str(error), **({name: diagnostic} if diagnostic is not None else {})}
    return {}


def _encode(state: AttemptState) -> dict:
    if isinstance(state, StartPending):
        return {"stage": "start", **asdict(state)}
    if isinstance(state, ActiveAttempt):
        return {"stage": "active", "start": _encode(state.start), "execution_attempt_ref": state.execution_attempt_ref}
    return {"stage": "terminal", "active": _encode(state.active), "operation": state.operation,
            "body_base64": b64encode(state.body).decode("ascii")}


def _decode(document: dict) -> AttemptState:
    stage = document["stage"]
    if stage == "start":
        provider, key = document["provider_ref"], document["provider_attempt_key"]
        if re.fullmatch(r"provider:[A-Za-z0-9_.-]{1,119}", provider) is None or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key) is None:
            raise ValueError("Retained start identity is unreadable")
        return StartPending(provider, selected_job_input(document["selected"]), key)
    if stage == "active":
        start = _decode(document["start"])
        if not isinstance(start, StartPending):
            raise ValueError("Active record has no start facts")
        ref = document["execution_attempt_ref"]
        if re.fullmatch(r"execution_attempt:sha256:[0-9a-f]{64}", ref) is None:
            raise ValueError("Retained Attempt identity is unreadable")
        return ActiveAttempt(start, ref)
    if stage == "terminal":
        active = _decode(document["active"])
        if not isinstance(active, ActiveAttempt) or document["operation"] not in {"complete", "fail"}:
            raise ValueError("Terminal record has no active obligation")
        return TerminalPending(active, document["operation"], b64decode(document["body_base64"], validate=True))
    raise ValueError("Unknown Attempt journal stage")
