"""Bind a no-computation runner to the ordinary Attempt lifecycle."""

import json
from time import monotonic

from provider_core.analysis import AnalysisFailure, AnalysisOutcomeUnresolved, AnalysisSuccess, ObservedWorkerCrash, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence
from provider_core.execution import ConditionRecoveryExpired, ObservationGapExpired

from secs_inference.provider.worker import NativeWorkerExited, WorkerDeadlineBeforeDispatch
from secs_inference.provider.worker_protocol import WorkerError, scientific_worker_failure


RESULT_SCHEMA_ID = "secs.mock_runner.result.v1"
_MESSAGE = "MOCK RUNNER: no model or scientific analysis was run."


class MockRunnerSession:
    """Keep the SDK's real Attempt and native ownership while doing no science."""

    def __init__(self, *, worker, work_seconds, startup_seconds):
        self.worker, self.work_seconds, self.startup_seconds = worker, work_seconds, startup_seconds
        self._acknowledged = self._prepared = self._deadline = None
        self._diagnostic = None
        self._quiescent, self._retired = True, False

    def _evidence(self):
        admission = self._acknowledged.admission
        content = json.dumps({"mock_runner": True, "job_ref": admission.job_ref,
                              "execution_attempt_ref": admission.execution_attempt_ref,
                              "scientific_worker": self._diagnostic},
                             sort_keys=True, separators=(",", ":")).encode()
        return ProtectedEvidence("secs.mock_runner.evidence.v1", content)

    def _crash(self, error, phase):
        return ObservedWorkerCrash.from_session(self._acknowledged, phase=phase, owner=self,
                                               runner=error.runner, evidence=self._evidence(), cause=error)

    async def _request(self, command, *, phase, require_execution):
        self._quiescent = False
        try:
            response = await self.worker.request(command, deadline=self._deadline, require_execution=require_execution)
        except NativeWorkerExited as error:
            raise self._crash(error, phase) from error
        except WorkerDeadlineBeforeDispatch as error:
            return AnalysisFailure("work_deadline_exceeded", _MESSAGE + " The request expired before dispatch.", self._evidence())
        except (ConditionRecoveryExpired, ObservationGapExpired):
            raise
        except (WorkerError, TimeoutError) as error:
            raise AnalysisOutcomeUnresolved("Cannot confirm the mock runner request outcome") from error
        if response.get("outcome") == "failed":
            try:
                self._diagnostic = scientific_worker_failure(response).diagnostic
            except WorkerError as error:
                raise AnalysisOutcomeUnresolved("Cannot confirm mock runner failure") from error
            return AnalysisFailure("mock_runner_failed", _MESSAGE + " The runner reported an error.", self._evidence())
        return response

    async def prepare(self, acknowledged, instructions, *, report_condition):
        if self._acknowledged is not None or self._retired:
            raise RuntimeError("Cannot prepare mock runner: the session was already used or retired")
        self._acknowledged = acknowledged
        self._quiescent = False
        try:
            await self.worker.check_ready(deadline=monotonic() + self.startup_seconds,
                                          require_execution=report_condition.require_execution)
        except NativeWorkerExited as error:
            raise self._crash(error, "preparing") from error
        except (ConditionRecoveryExpired, ObservationGapExpired):
            raise
        except (WorkerError, TimeoutError) as error:
            raise AnalysisOutcomeUnresolved("Cannot confirm mock runner readiness") from error
        self._quiescent = True
        self._deadline = monotonic() + self.work_seconds
        response = await self._request({"operation": "prepare", "attempt_ref": acknowledged.admission.execution_attempt_ref},
                                       phase="preparing", require_execution=report_condition.require_execution)
        if isinstance(response, AnalysisFailure):
            return response
        if response != {"outcome": "prepared", "prepared_handle": 1}:
            raise AnalysisOutcomeUnresolved("Cannot confirm mock runner preparation")
        self._prepared = PreparedAnalysis(self._evidence())
        self._quiescent = False
        return self._prepared

    async def execute(self, prepared, *, report_condition):
        if prepared is not self._prepared or prepared is None or self._retired:
            raise RuntimeError("Cannot execute mock runner: no matching prepared input is owned")
        self._prepared = None
        response = await self._request({"operation": "execute", "prepared_handle": 1,
                                        "attempt_ref": self._acknowledged.admission.execution_attempt_ref},
                                       phase="running", require_execution=report_condition.require_execution)
        if isinstance(response, AnalysisFailure):
            return response
        if response != {"outcome": "mock", "message": "No model or scientific analysis was run."}:
            raise AnalysisOutcomeUnresolved("Cannot confirm mock runner result")
        self._quiescent = True
        content = json.dumps({"schema_id": RESULT_SCHEMA_ID, "mock_runner": True, "message": _MESSAGE},
                             sort_keys=True, separators=(",", ":")).encode()
        return AnalysisSuccess(RESULT_SCHEMA_ID, content, self._evidence())

    async def retire(self):
        if self._retired:
            return
        if not self._quiescent:
            await self.worker.stop()
            self._quiescent = True
        self._retired = True
