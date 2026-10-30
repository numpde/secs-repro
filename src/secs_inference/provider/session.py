"""Bind one admitted Attempt to scientific preparation, search and source lifetime."""

from dataclasses import asdict
import json
from time import monotonic

from provider_core.analysis import AnalysisFailure, AnalysisOutcomeUnresolved, AnalysisSuccess, ObservedWorkerCrash, PreparedAnalysis
from provider_core.chat import ChatFailed
from provider_core.diagnostics import failure_evidence
from provider_core.execution import ConditionRecoveryExpired, ObservationGapExpired
from provider_core.attempt_record import ProtectedEvidence

from secs_inference.provider.input_operations import CannotAnalyse, SelectedRepresentation
from secs_inference.provider.interpreter import InterpretationFailed, InterpretationSession
from secs_inference.provider.outcomes import AnalysisOutcome, NO_STARTING_CANDIDATES_MESSAGE
from secs_inference.provider.source_access import InputReadError
from secs_inference.provider.worker import NativeWorkerExited, WorkerDeadlineBeforeDispatch
from secs_inference.provider.worker_protocol import ScientificWorkerFailed, WorkerError, scientific_worker_failure
from secs_inference.provider.sources import SourceAcquisitionFailed


RESULT_SCHEMA_ID = "secs.elucidation.result.v1"


def _document(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


class SecsSession:
    """Borrow the resident worker; release sources only after this work settles.

    The controller owns START, phase observation, entry and terminal handoff.
    This session owns the scientific choice and its single-use prepared handle.
    Healthy completed searches leave the resident worker available for reuse.
    """

    def __init__(self, *, worker, sources, chat, work_seconds, interpretation_seconds,
                 startup_seconds, max_turns, secrets=(), mock_index=False):
        self.worker, self.sources, self.chat = worker, sources, chat
        self.work_seconds, self.interpretation_seconds = work_seconds, interpretation_seconds
        self.startup_seconds, self.max_turns = startup_seconds, max_turns
        self._secrets, self._diagnostic = secrets, None
        self._mock_index = mock_index
        self._acknowledged = self._conversation = self._prepared = None
        self._handle = self._deadline = None
        self._choices = []
        self._quiescent = True
        self._retired = False

    def _evidence(self):
        return ProtectedEvidence("secs.analysis.evidence.v1", _document({
            "input_choices": self._choices,
            "interpretation_rejections": [] if self._conversation is None else self._conversation.rejections,
            "acquired_uploads": self.sources.evidence(),
            "failure": self._diagnostic,
        }))

    def _failure(self, code, message, cause=None):
        if cause is not None:
            self._diagnostic = failure_evidence(cause, secrets=self._secrets)
            if isinstance(cause, ScientificWorkerFailed):
                # The child supplies only its structural exception projection;
                # it has no API/interpreter credentials or raw exception text.
                self._diagnostic["scientific_worker"] = cause.diagnostic
        if self._mock_index:
            prefix = "MOCK INDEX: the starting index contains eight demonstration molecules, not the full candidate set. "
            available = 1024 - len(prefix)
            if len(message) > available:
                message = message[:available - 3] + "..."
            message = prefix + message
        return AnalysisFailure(code, message, self._evidence())

    def _worker_crash(self, failure, phase):
        self._diagnostic = failure_evidence(failure, secrets=self._secrets)
        return ObservedWorkerCrash.from_session(
            self._acknowledged, phase=phase, owner=self, runner=failure.runner,
            evidence=self._evidence(), cause=failure,
        )

    async def prepare(self, acknowledged, instructions, *, report_condition):
        if self._acknowledged is not None or self._retired:
            raise RuntimeError("Cannot prepare SECS input: this session was already used or retired")
        self._acknowledged = acknowledged
        try:
            return await self._prepare(instructions, report_condition.require_execution)
        except NativeWorkerExited as cause:
            raise self._worker_crash(cause, "preparing") from cause
        except InterpretationFailed as cause:
            if not self._quiescent:
                raise AnalysisOutcomeUnresolved("Cannot confirm SECS scientific inspection outcome") from cause
            return self._failure("interpretation_failed", str(cause), cause)
        except ChatFailed as cause:
            return self._failure("interpretation_failed", str(cause), cause)
        except SourceAcquisitionFailed as cause:
            return self._failure("input_access_failed", str(cause), cause)
        except ScientificWorkerFailed as cause:
            return self._failure("scientific_execution_failed", str(cause), cause)
        except WorkerError as cause:
            raise AnalysisOutcomeUnresolved("Cannot confirm SECS scientific preparation outcome") from cause
        except WorkerDeadlineBeforeDispatch as cause:
            return self._failure("work_deadline_exceeded", "The scientific request deadline elapsed before the worker request was sent.", cause)
        except (ConditionRecoveryExpired, ObservationGapExpired):
            raise
        except TimeoutError as cause:
            return self._failure("work_deadline_exceeded", "Input preparation exceeded its operation deadline; no verified prepared input was returned.", cause)

    async def _prepare(self, instructions, require_execution):
        self._quiescent = False
        require_execution()
        try:
            await self.worker.check_ready(deadline=monotonic() + self.startup_seconds,
                                          require_execution=require_execution)
        except (ConditionRecoveryExpired, ObservationGapExpired):
            raise
        except TimeoutError as cause:
            raise AnalysisOutcomeUnresolved("Cannot confirm SECS scientific worker readiness") from cause
        self._quiescent = True
        require_execution()
        self._deadline = monotonic() + self.work_seconds
        await self.sources.open(deadline=self._deadline, readers_quiescent=self._quiescent)
        interpretation_deadline = min(self._deadline, monotonic() + self.interpretation_seconds)
        self._conversation = InterpretationSession(
            self.chat, instructions.specification_text, self.sources.members,
            lambda source: self._inspect(source, require_execution),
            deadline=interpretation_deadline, max_turns=self.max_turns,
        )
        return await self._prepare_choice(require_execution)

    async def _inspect(self, source, require_execution):
        await self.sources.acquire(source.upload_ref, deadline=self._conversation.deadline)
        response = await self._request({"operation": "inspect", "source": asdict(source)},
                                       self._conversation.deadline, require_execution=require_execution)
        if response["outcome"] == "input_rejected":
            raise InputReadError(response["reason"])
        return response["facts"]

    async def _prepare_choice(self, require_execution):
        while True:
            decision = await self._conversation.select()
            if isinstance(decision, CannotAnalyse):
                return self._failure("cannot_analyse", "No analysis was produced. Interpreter explanation: " + decision.explanation)
            if not isinstance(decision, SelectedRepresentation):
                raise TypeError("Cannot prepare SECS input: interpretation returned no representation selection")
            choice = asdict(decision)
            self._choices.append(choice)
            change = await self.sources.reconcile()
            if change is not None:
                choice["reading_error"] = str(change)
                self._conversation.reject(str(change))
                continue
            response = await self._request({"operation": "prepare", "selection": choice}, self._deadline,
                                           require_execution=require_execution)
            if response["outcome"] == "input_rejected":
                choice["reading_error"] = response["reason"]
                self._conversation.reject(response["reason"])
                continue
            self._handle = response["prepared_handle"]
            self._quiescent = False
            self._prepared = PreparedAnalysis(ProtectedEvidence("secs.prepared.input.v1", _document({
                "execution_attempt_ref": self._acknowledged.admission.execution_attempt_ref,
                "selection": choice, "acquired_uploads": self.sources.evidence(),
            })))
            return self._prepared

    async def execute(self, prepared, *, report_condition):
        if prepared is not self._prepared or prepared is None or self._retired:
            raise RuntimeError("Cannot execute SECS analysis: this session owns no matching unconsumed prepared input")
        self._prepared = None
        handle, self._handle = self._handle, None
        try:
            response = await self._request({"operation": "execute", "prepared_handle": handle}, self._deadline,
                                           require_execution=report_condition.require_execution)
        except NativeWorkerExited as cause:
            raise self._worker_crash(cause, "running") from cause
        except ScientificWorkerFailed as cause:
            return self._failure("scientific_execution_failed", str(cause), cause)
        except WorkerError as cause:
            raise AnalysisOutcomeUnresolved("Cannot confirm SECS scientific execution outcome") from cause
        except WorkerDeadlineBeforeDispatch as cause:
            return self._failure("work_deadline_exceeded", "The scientific request deadline elapsed before the worker request was sent.", cause)
        self._choices[-1]["used"] = True
        if response["outcome"] == AnalysisOutcome.NO_STARTING_CANDIDATES:
            return self._failure(AnalysisOutcome.NO_STARTING_CANDIDATES.value, NO_STARTING_CANDIDATES_MESSAGE)
        report = {"schema_id": RESULT_SCHEMA_ID, "outcome": response["outcome"],
                  "input_choices": self._choices,
                  "interpretation_rejections": self._conversation.rejections,
                  "acquired_uploads": self.sources.evidence(), "analysis": response["analysis"]}
        if self._mock_index:
            report["mock_index"] = {"description": "Eight starting molecules; this is not a full scientific search."}
        return AnalysisSuccess(RESULT_SCHEMA_ID, _document(report), self._evidence())

    async def _request(self, request, deadline, *, require_execution=None):
        command = request | {"attempt_ref": self._acknowledged.admission.execution_attempt_ref}
        if request["operation"] != "execute":
            command |= self.sources.worker_inputs()
        self._quiescent = False
        try:
            response = await self.worker.request(command, deadline=deadline, require_execution=require_execution)
        except WorkerDeadlineBeforeDispatch:
            raise
        except (ConditionRecoveryExpired, ObservationGapExpired):
            raise
        except TimeoutError as cause:
            raise AnalysisOutcomeUnresolved("Cannot confirm SECS scientific worker request outcome") from cause
        if response.get("outcome") == "failed":
            raise scientific_worker_failure(response)
        expected = {"inspect": {"inspected", "input_rejected"},
                    "prepare": {"prepared", "input_rejected"},
                    "execute": {AnalysisOutcome.ANALYSED, AnalysisOutcome.NO_STARTING_CANDIDATES}}
        if type(response.get("outcome")) is not str or response["outcome"] not in expected[request["operation"]]:
            raise WorkerError("Cannot confirm SECS scientific work: the worker returned an unrecognized operation outcome")
        if request["operation"] == "execute":
            analysis = response.get("analysis")
            if (type(analysis) is not dict or type(analysis.get("candidates")) is not list
                    or any(type(analysis.get(field)) is not dict
                           for field in ("retrieval", "search", "preparation", "inference"))):
                raise WorkerError("Cannot confirm SECS scientific work: the worker returned an incomplete analysis")
            search = analysis["search"].get("outcome")
            if (response["outcome"] == AnalysisOutcome.NO_STARTING_CANDIDATES
                    and (search != AnalysisOutcome.NO_STARTING_CANDIDATES or analysis["candidates"]
                         or type(analysis["retrieval"].get("starting_candidates")) is not int
                         or analysis["retrieval"]["starting_candidates"] != 0)):
                raise WorkerError("Cannot confirm SECS scientific work: the worker returned inconsistent candidate evidence")
            if (response["outcome"] == AnalysisOutcome.ANALYSED
                    and (search != "optimized" or not analysis["candidates"]
                         or type(analysis["retrieval"].get("starting_candidates")) is not int
                         or analysis["retrieval"]["starting_candidates"] <= 0)):
                raise WorkerError("Cannot confirm SECS scientific work: the worker returned inconsistent search evidence")
            if any(type(candidate) is not dict or type(candidate.get("smiles")) is not str
                   or type(candidate.get("score")) is not str for candidate in analysis["candidates"]):
                raise WorkerError("Cannot confirm SECS scientific work: the worker returned invalid candidate evidence")
        if response["outcome"] == "prepared":
            if type(response.get("prepared_handle")) is not int or response["prepared_handle"] <= 0:
                raise WorkerError("Cannot confirm SECS input preparation: the worker returned no valid prepared handle")
        else:
            self._quiescent = True
        return response

    async def retire(self):
        if self._retired:
            return
        failures = []
        cleanup_failed = False
        if not self._quiescent:
            try:
                await self.worker.stop()
                self._quiescent = True
            except (Exception, BaseExceptionGroup) as cause:
                if self.worker.stopped:
                    self._quiescent = True
                else:
                    cleanup_failed = True
                failures.append(cause)
        try:
            await self.chat.retire()
        except (Exception, BaseExceptionGroup) as cause:
            cleanup_failed = True
            failures.append(cause)
        try:
            await self.sources.retire(readers_stopped=self._quiescent)
        except (Exception, BaseExceptionGroup) as cause:
            cleanup_failed = True
            failures.append(cause)
        self._retired = not cleanup_failed
        if failures:
            raise BaseExceptionGroup("SECS Attempt retirement retained failures", failures)
