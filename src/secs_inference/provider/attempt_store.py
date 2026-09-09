"""One process owns one durable Attempt record; no work queue is hidden here.

Atomic replacement and fsync ordering follow nmrpeak-repro's Attempt store.
Durability uncertainty stops further effects; restart reads whichever complete
record survived instead of guessing whether the previous write committed.
"""

from base64 import b64decode, b64encode
from dataclasses import asdict
import fcntl
import os
import re
from pathlib import Path
import stat
import traceback
from tempfile import NamedTemporaryFile

from secs_inference.provider.attempt_state import ActiveAttempt, AttemptState, StartPending, TerminalPending
from secs_inference.provider.canonical_json import canonical_json_bytes
from secs_inference.provider.chat import InterpreterError
from secs_inference.provider.job_input import selected_job_input
from secs_inference.provider.response_json import response_object


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
                raise RuntimeError("Cannot own the Attempt journal: its directory must be private to the provider user")
            self._lock_fd = os.open("owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self._directory_fd)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self) -> None:
        """Release ownership without changing the retained obligation."""
        for descriptor in (self._lock_fd, self._directory_fd):
            if descriptor >= 0:
                os.close(descriptor)
        self._lock_fd = self._directory_fd = -1

    def load(self) -> AttemptState | None:
        """Read the last complete record; malformed state requires operator repair."""
        self._require_usable()
        try:
            descriptor = os.open("attempt.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._directory_fd)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RuntimeError("Cannot recover the Attempt: the journal record is not a regular file")
            raw = stream.read(3 * 1024 * 1024 + 1)
        if len(raw) > 3 * 1024 * 1024:
            raise RuntimeError("Cannot recover the Attempt: the journal record exceeds its byte limit")
        try:
            return _decode(response_object(raw))
        except (ValueError, TypeError, KeyError, RecursionError):
            raise RuntimeError("Cannot recover the Attempt: the retained journal record is unreadable") from None

    def save(self, state: AttemptState) -> None:
        """Confirm file and directory durability before the next external effect."""
        self._require_usable()
        raw = canonical_json_bytes(_encode(state))
        staging = None
        try:
            with NamedTemporaryFile(dir=self.directory, prefix=".attempt-", delete=False) as stream:
                staging = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, "attempt.json", dst_dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
        except BaseException as error:
            self._usable = False
            if staging is not None:
                try:
                    staging.unlink(missing_ok=True)
                except OSError as cleanup:
                    error.add_note(f"The incomplete journal staging file could not be removed ({type(cleanup).__name__}).")
            raise

    def diagnose(self, active: ActiveAttempt, error: Exception) -> None:
        """Retain frames and selected boundary evidence under the Attempt identity."""
        document = {
            "execution_attempt_ref": active.execution_attempt_ref,
            "exception_type": type(error).__name__,
            "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                       for frame in traceback.extract_tb(error.__traceback__)],
        }
        if isinstance(error, InterpreterError):
            if error.diagnostic is not None:
                document["interpreter"] = error.diagnostic
        elif hasattr(error, "diagnostic"):
            document["worker"] = error.diagnostic
        self._write_evidence(active, "diagnostic", document)

    def record_report(self, active: ActiveAttempt, report: dict) -> None:
        """Retain the full inability report that the API failure cannot attach."""
        self._write_evidence(active, "report", report)

    def _write_evidence(self, active: ActiveAttempt, kind: str, document: dict) -> None:
        """Create private Attempt evidence once, without replacing earlier facts."""
        self._require_usable()
        name = active.execution_attempt_ref.removeprefix("execution_attempt:sha256:") + f".{kind}.json"
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._directory_fd)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(self._directory_fd)

    def clear(self) -> None:
        """Retire only a reconciled obligation; this is not cancellation."""
        self._require_usable()
        try:
            os.unlink("attempt.json", dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
        except OSError:
            self._usable = False
            raise

    def _require_usable(self):
        if not self._usable or self._directory_fd < 0:
            raise RuntimeError("The Attempt journal has no confirmed writable state; restart and recover before further API effects")


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
