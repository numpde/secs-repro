"""Bind execution API replies to the Job and exact command being handled."""

from base64 import b64decode, b64encode
from dataclasses import dataclass
from hashlib import sha256
import re

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.attempt_state import ActiveAttempt, StartPending, TerminalPending
from secs_inference.provider.canonical_json import canonical_json_bytes, parse_canonical_json_bytes
from secs_inference.provider.job_input import SelectedJobInput, parse_job_input_read_response, selected_job_input
from secs_inference.provider.job_upload import parse_job_upload_set_response, parse_upload_read_capability_response
from secs_inference.provider.operations import Operation
from secs_inference.provider.response_json import response_object


class ApiError(RuntimeError):
    """The API exchange needs reconciliation or operator correction."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class ApiUnavailable(ApiError):
    """A bounded retry can recover delivery without changing command facts."""


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    state: str
    job_state: str


class JobApi:
    """Execution-specific meaning over the existing freshly signed transport."""

    def __init__(self, provider):
        self.provider = provider
        self.provider_ref = provider.provider_ref

    def next_job(self) -> SelectedJobInput | None:
        """Read the current unattempted feed; no cursor is a durable work queue."""
        document = self._read(Operation.JOBS, query="analysis_kind_ref=" + ANALYSIS_KIND_REF)
        if (document.get("analysis_kind_ref") != ANALYSIS_KIND_REF
                or document.get("has_provider_execution_attempt") is not False
                or type(document.get("jobs")) is not list):
            raise ApiError("Cannot select a Job: the feed does not describe this offering's unattempted Jobs")
        if not document["jobs"]:
            return None
        item = document["jobs"][0]
        try:
            if item["analysis_kind_ref"] != ANALYSIS_KIND_REF:
                raise ValueError
            return selected_job_input(item)
        except (TypeError, ValueError, KeyError):
            raise ApiError("Cannot select a Job: the feed item has no usable input identity") from None

    def start(self, pending: StartPending) -> tuple[ActiveAttempt, str]:
        """Replay retained start facts; only the API may assign the Attempt."""
        body = canonical_json_bytes({
            "schema_id": "nmr.provider.execution_attempt_start_request.v1",
            "job_ref": pending.selected.job_ref,
            "provider_attempt_key": pending.provider_attempt_key,
        })
        document = self._read(Operation.START, body=body)
        if (document.get("job_ref") != pending.selected.job_ref
                or document.get("provider_ref") != pending.provider_ref
                or document.get("analysis_kind_ref") != ANALYSIS_KIND_REF):
            raise ApiError("Cannot confirm Attempt start: the receipt names different start facts")
        ref = document.get("execution_attempt_ref")
        if type(ref) is not str or re.fullmatch(r"execution_attempt:sha256:[0-9a-f]{64}", ref) is None:
            raise ApiError("Cannot confirm Attempt start: the receipt has no usable Attempt identity")
        return ActiveAttempt(pending, ref), _state(document)

    def snapshot(self, active: ActiveAttempt) -> AttemptSnapshot:
        document = self._read(Operation.ATTEMPT, path=Operation.ATTEMPT.path.format(execution_attempt_ref=active.execution_attempt_ref))
        if document.get("execution_attempt_ref") != active.execution_attempt_ref or document.get("job_ref") != active.start.selected.job_ref:
            raise ApiError("Cannot reconcile the Attempt: the snapshot names another Attempt or Job")
        job_state = document.get("job_state")
        if type(job_state) is not str or job_state not in {"open", "closed", "cancelled"}:
            raise ApiError("Cannot reconcile the Attempt: the Job lifecycle state is unreadable")
        return AttemptSnapshot(_state(document), job_state)

    def specification(self, active: ActiveAttempt):
        selected = active.start.selected
        raw = self.provider.request(Operation.INPUT, path=Operation.INPUT.path.format(job_ref=selected.job_ref), query="analysis_kind_ref=" + ANALYSIS_KIND_REF)
        return parse_job_input_read_response(raw, selected=selected)

    def uploads(self, active: ActiveAttempt):
        ref = active.start.selected.job_ref
        raw = self.provider.request(Operation.UPLOADS, path=Operation.UPLOADS.path.format(job_ref=ref))
        return parse_job_upload_set_response(raw, expected_job_ref=ref)

    def capability(self, active: ActiveAttempt, upload):
        path = Operation.CAPABILITY.path.format(job_ref=active.start.selected.job_ref, upload_ref=upload.upload_ref)
        raw = self.provider.request(Operation.CAPABILITY, path=path)
        return parse_upload_read_capability_response(raw, selected=upload)

    def publish(self, terminal: TerminalPending) -> None:
        """Confirm exact result/failure facts, not merely a matching terminal state."""
        command = parse_canonical_json_bytes(terminal.body)
        ref = terminal.active.execution_attempt_ref
        if type(command) is not dict or command.get("execution_attempt_ref") != ref:
            raise ApiError("Cannot publish the retained command: it names another Attempt")
        operation = Operation.COMPLETE if terminal.operation == "complete" else Operation.FAIL
        if command.get("schema_id") != "nmr.provider.execution_attempt_" + terminal.operation + "_request.v1":
            raise ApiError("Cannot publish the retained command: its schema does not match its operation")
        if operation is Operation.COMPLETE:
            result = b64decode(command["canonical_result_base64"], validate=True)
            expected = {"execution_attempt_ref": ref, "result_schema_id": command["result_schema_id"],
                        "result_byte_length": len(result), "result_fingerprint": "sha256:" + sha256(result).hexdigest()}
        else:
            expected = {name: command[name] for name in ("execution_attempt_ref", "failure_code", "failure_message")}
        receipt = self._read(operation, body=terminal.body)
        if any(type(receipt.get(key)) is not type(value) or receipt[key] != value for key, value in expected.items()):
            raise ApiError("Cannot confirm Attempt publication: the receipt does not match the retained terminal command")

    def _read(self, operation, **kwargs) -> dict:
        raw = self.provider.request(operation, **kwargs)
        schema = {
            Operation.JOBS: "nmr.provider.jobs.list.response.v1",
            Operation.ATTEMPT: "nmr.provider.execution_attempt_read_response.v1",
        }.get(operation, "nmr.provider.execution_attempt_" + operation.name.lower() + "_response.v1")
        try:
            document = response_object(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError(f"Cannot confirm the Provider API request to {operation.action}: its response JSON is unreadable") from None
        if document.get("schema_id") != schema:
            raise ApiError(f"Cannot confirm the Provider API request to {operation.action}: its response schema differs from the requested operation")
        return document


def complete_command(active: ActiveAttempt, report: dict) -> TerminalPending:
    """Freeze the exact result bytes before any delivery attempt."""
    raw = canonical_json_bytes(report)
    if len(raw) > 786432:
        raise ApiError("Cannot publish the analysis: its complete report exceeds the 786432-byte API result limit")
    body = canonical_json_bytes({
        "schema_id": "nmr.provider.execution_attempt_complete_request.v1",
        "execution_attempt_ref": active.execution_attempt_ref,
        "result_schema_id": report["schema_id"],
        "canonical_result_base64": b64encode(raw).decode("ascii"),
    })
    return TerminalPending(active, "complete", body)


def fail_command(active: ActiveAttempt, code: str, message: str) -> TerminalPending:
    """Retain bounded public diagnostics, without raw inputs or exception chains."""
    body = canonical_json_bytes({
        "schema_id": "nmr.provider.execution_attempt_fail_request.v1",
        "execution_attempt_ref": active.execution_attempt_ref,
        "failure_code": code, "failure_message": message[:1024],
    })
    return TerminalPending(active, "fail", body)


def _state(document: dict) -> str:
    state = document.get("state")
    if type(state) is not str or state not in {"in_progress", "succeeded", "failed", "expired"}:
        raise ApiError("Cannot reconcile the Attempt: its lifecycle state is unreadable")
    return state
