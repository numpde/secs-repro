"""Attempt retirement preserves prepared ownership and the resident worker."""

import asyncio
import errno
import json
from pathlib import Path
import sys
import tempfile
from time import monotonic
from types import SimpleNamespace
from datetime import datetime, timezone
from hashlib import sha256
import unittest
from unittest.mock import Mock

from provider_core.analysis import AnalysisFailure, AnalysisOutcomeUnresolved, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import RecoveryScope, StartPending, acknowledge_start
from provider_core.attempt_request import fail_body, start_body
from provider_core.execution import ConditionRecoveryExpired, ObservationGapExpired
from provider_core.interpreter import InterpretationFailed as ModelsExhausted, ModelReportedInterpretationProblem, ModelWindowExpired
from provider_core.attempt_response import AttemptAdmission
from provider_core.job_input import FeedInputFacts, VerifiedJobInput

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.input_operations import SourceRef
from secs_inference.provider.session import SecsSession
from secs_inference.provider.interpreter import InterpretationFailed
from secs_inference.provider.sources import SourceAcquisitionFailed
from secs_inference.provider.worker import WorkerClient, WorkerDeadlineBeforeDispatch
from secs_inference.provider.worker_protocol import InvalidFailureEnvelope, ScientificWorkerFailed, WorkerError
from secs_inference.provider.diagnostics import exception_evidence, scientific_failure_evidence
from test_async_interpretation import ScriptedCarrier, choice, call


def admission():
    specification = b"Find C2H6O"
    facts = FeedInputFacts("job:chosen", "nmr.job.specification.text.v1",
                           "sha256:" + sha256(specification).hexdigest(), len(specification))
    provider = "provider:secs-test"
    start = StartPending(RecoveryScope("https://api.test", provider, "secs", "generation-one"),
                         ANALYSIS_KIND_REF, facts, start_body(facts.job_ref, "request-one"))
    acknowledged = acknowledge_start(start, AttemptAdmission(
        "execution_attempt:sha256:" + "b" * 64, facts.job_ref, ANALYSIS_KIND_REF,
        provider, "in_progress", datetime.now(timezone.utc), False,
    ))
    return acknowledged, VerifiedJobInput(facts, specification)


class Sources:
    members = ()
    def __init__(self, events):
        self.events = events
    async def open(self, *, deadline, readers_quiescent):
        if not readers_quiescent:
            raise AssertionError("Source admission preceded scientific quiescence")
        self.events.append("sources-open")
    async def acquire(self, ref, *, deadline):
        pass
    async def reconcile(self):
        return None
    def evidence(self):
        return {}
    def worker_inputs(self):
        return {"receipts": {}, "directory": "/sources", "max_upload_bytes": 134217728, "max_total_bytes": 268435456}
    async def retire(self, *, readers_stopped):
        self.events.append(("sources-retire", readers_stopped))
        if not readers_stopped:
            raise RuntimeError("Sources remain owned by an unconfirmed reader")


class Worker:
    def __init__(self, events, *responses):
        self.events, self.responses = events, iter(responses)
        self.commands = []
        self.stopping_failure = None
        self.stopped = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
    async def check_ready(self, *, deadline, require_execution=None):
        if require_execution is not None:
            require_execution()
        self.events.append("worker-ready")
    async def request(self, command, *, deadline, require_execution=None):
        if command["operation"] in ("inspect", "prepare", "execute"):
            require_execution()
        self.commands.append(command)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if response == "blocked":
            self.entered.set()
            await self.release.wait()
        return response
    async def stop(self):
        self.events.append("worker-stop")
        self.release.set()
        if self.stopping_failure is not None:
            raise self.stopping_failure
        self.stopped = True


class Carrier(ScriptedCarrier):
    def __init__(self, events, *turns):
        super().__init__(*turns)
        self.events = events
        self.retirement_failure = None
    async def retire(self):
        self.events.append("chat-retire")
        if self.retirement_failure is not None:
            raise self.retirement_failure


async def condition(_code):
    raise AssertionError("No public condition is required by this scientific fixture")

condition.require_execution = lambda: None


class SdkSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_phase_refusal_precedes_expired_exchange_deadline(self):
        failures = (
            ObservationGapExpired(1, executor=SimpleNamespace(_observation_read_started=False)),
            ConditionRecoveryExpired(1, phase="preparing", condition_code=None, cause=None),
        )
        with tempfile.TemporaryDirectory() as directory:
            worker = WorkerClient(Path(directory) / "worker.sock", startup_seconds=10, retirement_seconds=10)
            worker._writer = SimpleNamespace(write=Mock())
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    def refuse():
                        raise failure
                    with self.assertRaises(type(failure)) as caught:
                        await worker._exchange({"operation": "inspect"}, monotonic() - 1, refuse)
                    self.assertIs(caught.exception, failure)
                    worker._writer.write.assert_not_called()

    async def test_proven_pre_dispatch_deadline_is_public_deadline(self):
        session, worker, _, events = self.make_session([])
        failure = WorkerDeadlineBeforeDispatch("fixture no-write deadline")
        async def refused(command, *, deadline, require_execution):
            raise failure
        worker.request = refused
        try:
            outcome = await self.prepare(session)
            self.assertIs(type(outcome), AnalysisFailure)
            self.assertEqual(outcome.code, "work_deadline_exceeded")
            self.assertNotIn(b"fixture no-write", outcome.evidence.content)
        finally:
            await session.retire()
        self.assertNotIn("worker-stop", events)
        self.assertIn(("sources-retire", True), events)

    async def test_inspection_deadline_before_dispatch_allows_model_handoff(self):
        session, worker, _, _ = self.make_session([])
        session._acknowledged, _ = admission()
        async def refused(command, *, deadline, require_execution):
            raise WorkerDeadlineBeforeDispatch("no request was sent")
        worker.request = refused
        with self.assertRaises(ModelWindowExpired):
            await session._inspect(SourceRef("upload:chosen"), monotonic() - 1, lambda: None)
        self.assertTrue(session._quiescent)

    async def test_execute_deadline_retains_prepared_handle_until_worker_stops(self):
        session, worker, _, events = self.make_session([{"outcome": "prepared", "prepared_handle": 7}])
        prepared = await self.prepare(session)
        async def refused(command, *, deadline, require_execution):
            raise WorkerDeadlineBeforeDispatch("no request was sent")
        worker.request = refused
        outcome = await session.execute(prepared, report_condition=condition)
        self.assertEqual(outcome.code, "work_deadline_exceeded")
        await session.retire()
        self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))

    async def test_unknown_preparation_reply_is_unresolved(self):
        session, _, _, events = self.make_session([{"outcome": "unexpected"}])
        try:
            with self.assertRaises(AnalysisOutcomeUnresolved) as caught:
                await self.prepare(session)
            self.assertIs(type(caught.exception.__cause__), WorkerError)
        finally:
            await session.retire()
        self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))

    async def test_worker_preparation_timeout_is_unresolved(self):
        session, worker, _, _ = self.make_session([])
        failure = TimeoutError("worker fixture timeout")
        async def interrupted(command, *, deadline, require_execution):
            raise failure
        worker.request = interrupted
        try:
            with self.assertRaises(AnalysisOutcomeUnresolved) as caught:
                await self.prepare(session)
            self.assertIs(caught.exception.__cause__, failure)
        finally:
            await session.retire()

    async def test_source_preparation_faults_propagate_original_errors_and_context(self):
        for failure in (TimeoutError("source fixture timeout"),
                        SourceAcquisitionFailed("the private source workspace could not be opened")):
            with self.subTest(failure=type(failure).__name__):
                session, worker, _, events = self.make_session([])
                original = PermissionError(errno.EACCES, "private source fixture")

                async def refused(*, deadline, readers_quiescent):
                    try:
                        raise original
                    except PermissionError:
                        raise failure from None

                session.sources.open = refused
                try:
                    with self.assertRaises(type(failure)) as caught:
                        await self.prepare(session)
                    self.assertIs(failure, caught.exception)
                    self.assertIs(original, caught.exception.__context__)
                    self.assertEqual([], worker.commands)
                finally:
                    await session.retire()
                self.assertIn(("sources-retire", True), events)

    async def test_real_worker_malformed_failure_frame_has_no_public_outcome(self):
        await self._assert_real_worker_unresolved("malformed_execute")

    async def test_real_dispatched_timeout_has_no_public_outcome(self):
        await self._assert_real_worker_unresolved("unconfirmed_execute")

    async def test_real_worker_invalid_json_has_no_public_outcome(self):
        await self._assert_real_worker_unresolved("invalid_json_execute")

    async def _assert_real_worker_unresolved(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for part in ("ipc", "scratch", "status"):
                (root / part).mkdir(mode=0o700)
            endpoint = root / "ipc/worker.sock"
            (endpoint.parent / ".owner-session.lock").touch(mode=0o600)
            fixture = Path(__file__).resolve().parents[1] / "integration/native_worker_fixture.py"
            owner = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "provider_core.runner", "--role", "secs",
                "--socket", str(endpoint), "--scratch", str(root / "scratch"),
                "--status-directory", str(root / "status"), "--owner-session-lifetime",
                "--startup-seconds", "2", "--terminate-seconds", "2", "--kill-seconds", "2",
                "--cleanup-seconds", "2", "--", sys.executable, "-I", str(fixture),
                "--mode", mode, "--pid-file", str(root / "pid"),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            session, _, _, events = self.make_session([{"outcome": "prepared", "prepared_handle": 7}])
            worker = WorkerClient(endpoint, startup_seconds=10, retirement_seconds=10)
            try:
                prepared = await self.prepare(session)
                session.worker = worker
                await worker.check_ready(deadline=monotonic() + 10)
                if mode == "unconfirmed_execute":
                    session._deadline = monotonic() + 1
                with self.assertRaises(AnalysisOutcomeUnresolved) as caught:
                    await session.execute(prepared, report_condition=condition)
                expected_cause = (TimeoutError if mode == "unconfirmed_execute" else
                                  InvalidFailureEnvelope if mode == "malformed_execute" else WorkerError)
                self.assertIs(type(caught.exception.__cause__), expected_cause)
                if mode == "invalid_json_execute":
                    self.assertIs(type(caught.exception.__cause__.__cause__), json.JSONDecodeError)
            finally:
                try:
                    await session.retire()
                finally:
                    if owner.returncode is None:
                        owner.terminate()
                    await asyncio.wait_for(owner.wait(), 20)
                if mode == "unconfirmed_execute":
                    self.assertTrue((root / "pid.entered").exists(), "The real child did not enter execution")
            self.assertTrue(worker.stopped)
            self.assertIn(("sources-retire", True), events)

    async def test_unconfirmed_execution_does_not_become_public_failure(self):
        failure = WorkerError("Scientific communication ended without an operation outcome")
        session, worker, _, events = self.make_session([{"outcome": "prepared", "prepared_handle": 7}])
        prepared = await self.prepare(session)
        async def interrupted(command, *, deadline, require_execution):
            worker.commands.append(command)
            raise failure
        worker.request = interrupted
        with self.assertRaises(AnalysisOutcomeUnresolved) as caught:
            await session.execute(prepared, report_condition=condition)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(worker.commands[-1]["operation"], "execute")
        await session.retire()
        self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))

    async def test_observation_expiry_at_dispatch_keeps_original_failure(self):
        session, _, _, _ = self.make_session([{"outcome": "prepared", "prepared_handle": 7}])
        prepared = await self.prepare(session)
        expiry = ObservationGapExpired(1, executor=SimpleNamespace(_observation_read_started=False))
        async def report(_code):
            raise AssertionError("No condition expected")
        def refuse():
            raise expiry
        report.require_execution = refuse
        try:
            with self.assertRaises(ObservationGapExpired) as caught:
                await session.execute(prepared, report_condition=report)
            self.assertIs(caught.exception, expiry)
        finally:
            await session.retire()

    async def test_condition_expiry_at_dispatch_keeps_original_failure(self):
        session, _, _, _ = self.make_session([{"outcome": "prepared", "prepared_handle": 7}])
        prepared = await self.prepare(session)
        expiry = ConditionRecoveryExpired(1, phase="running", condition_code=None, cause=None)
        async def report(_code):
            raise AssertionError("No condition expected")
        def refuse():
            raise expiry
        report.require_execution = refuse
        try:
            with self.assertRaises(ConditionRecoveryExpired) as caught:
                await session.execute(prepared, report_condition=report)
            self.assertIs(caught.exception, expiry)
        finally:
            await session.retire()

    async def test_observation_expiry_before_preparation_keeps_original_failure(self):
        session, _, _, _ = self.make_session([])
        expiry = ObservationGapExpired(1, executor=SimpleNamespace(_observation_read_started=False))
        async def report(_code):
            raise AssertionError("No condition expected")
        def refuse():
            raise expiry
        report.require_execution = refuse
        acknowledged, instructions = admission()
        try:
            with self.assertRaises(ObservationGapExpired) as caught:
                await session.prepare(acknowledged, instructions, report_condition=report)
            self.assertIs(caught.exception, expiry)
        finally:
            await session.retire()

    async def test_unrecognized_execution_reply_does_not_become_public_failure(self):
        session, _, _, events = self.make_session([
            {"outcome": "prepared", "prepared_handle": 7}, {"outcome": "unexpected"},
        ])
        prepared = await self.prepare(session)
        with self.assertRaises(AnalysisOutcomeUnresolved):
            await session.execute(prepared, report_condition=condition)
        await session.retire()
        self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))

    async def test_incomplete_analysis_does_not_become_public_outcome(self):
        for response in ({"outcome": "no_starting_candidates"},
                         {"outcome": "analysed", "analysis": None}):
            with self.subTest(response=response):
                session, _, _, events = self.make_session([
                    {"outcome": "prepared", "prepared_handle": 7}, response,
                ])
                prepared = await self.prepare(session)
                try:
                    with self.assertRaises(AnalysisOutcomeUnresolved) as caught:
                        await session.execute(prepared, report_condition=condition)
                    self.assertIs(type(caught.exception.__cause__), WorkerError)
                finally:
                    await session.retire()
                self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))

    async def test_scientific_execution_failure_propagates_its_original_exception_and_frame(self):
        frame = {"outcome": "failed", **exception_evidence(RuntimeError("private scientific detail"))}
        original = ScientificWorkerFailed(frame)
        session, worker, _, events = self.make_session([{"outcome": "prepared", "prepared_handle": 7}, original])
        prepared = await self.prepare(session)
        try:
            with self.assertRaises(ScientificWorkerFailed) as caught:
                await session.execute(prepared, report_condition=condition)
            self.assertIs(original, caught.exception)
            self.assertIs(frame, caught.exception.diagnostic)
            self.assertEqual(["prepare", "execute"], [command["operation"] for command in worker.commands])
        finally:
            await session.retire()
        self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))

    async def test_failure_envelope_requires_owned_root_fields(self):
        valid = {"outcome": "failed", "exception_id": 0, "exception_type": "RuntimeError", "frames": []}
        for field, value in (("exception_id", False), ("exception_id", 1),
                             ("exception_type", ""), ("exception_type", None), ("frames", None)):
            with self.subTest(field=field, value=value):
                session, _, _, _ = self.make_session([
                    {"outcome": "prepared", "prepared_handle": 7}, valid | {field: value},
                ])
                await self.prepare(session)
                try:
                    with self.assertRaises(WorkerError) as caught:
                        await session._request({"operation": "execute", "prepared_handle": 7}, session._deadline,
                                               require_execution=condition.require_execution)
                    self.assertIs(type(caught.exception), InvalidFailureEnvelope)
                finally:
                    await session.retire()

    async def test_non_string_worker_outcome_is_unrecognized(self):
        session, _, _, _ = self.make_session([
            {"outcome": "prepared", "prepared_handle": 7}, {"outcome": []},
        ])
        await self.prepare(session)
        try:
            with self.assertRaises(WorkerError):
                await session._request({"operation": "execute", "prepared_handle": 7}, session._deadline,
                                       require_execution=condition.require_execution)
        finally:
            await session.retire()

    async def test_incomplete_execution_replies_are_unconfirmed(self):
        analysis = {"candidates": [], "retrieval": {"starting_candidates": 1}, "search": {"outcome": "optimized"},
                    "preparation": {}, "inference": {}}
        for response in ({"outcome": "no_starting_candidates"},
                         {"outcome": "analysed", "analysis": None},
                         {"outcome": "analysed", "analysis": {}},
                         {"outcome": "analysed", "analysis": analysis},
                         {"outcome": "analysed", "analysis": analysis | {"candidates": [None]}},
                         {"outcome": "analysed", "analysis": analysis | {"candidates": [{"smiles": "C", "score": "1"}],
                                                                          "retrieval": {"starting_candidates": 0}}},
                         {"outcome": "no_starting_candidates", "analysis": analysis},
                         {"outcome": "no_starting_candidates", "analysis": analysis | {
                             "search": {"outcome": "no_starting_candidates"},
                             "retrieval": {"starting_candidates": 1}}}):
            with self.subTest(response=response):
                session, _, _, _ = self.make_session([
                    {"outcome": "prepared", "prepared_handle": 7}, response,
                ])
                await self.prepare(session)
                try:
                    with self.assertRaises(WorkerError):
                        await session._request({"operation": "execute", "prepared_handle": 7}, session._deadline,
                                               require_execution=condition.require_execution)
                finally:
                    await session.retire()

    async def test_complete_no_starting_candidates_reply_remains_public_failure(self):
        analysis = {"candidates": [], "retrieval": {"starting_candidates": 0}, "search": {"outcome": "no_starting_candidates"},
                    "preparation": {}, "inference": {}}
        session, _, _, _ = self.make_session([
            {"outcome": "prepared", "prepared_handle": 7},
            {"outcome": "no_starting_candidates", "analysis": analysis},
        ])
        prepared = await self.prepare(session)
        try:
            outcome = await session.execute(prepared, report_condition=condition)
            self.assertIsInstance(outcome, AnalysisFailure)
            self.assertEqual(outcome.code, "no_starting_candidates")
        finally:
            await session.retire()

    async def test_scientific_failure_retains_the_childs_safe_causal_graph(self):
        failure = RuntimeError("private fixture source content")
        failure.__cause__ = OSError(errno.EIO, "private fixture path")
        diagnostic = {"outcome": "failed", **exception_evidence(failure)}
        session, worker, _, _ = self.make_session([diagnostic])
        try:
            with self.assertRaises(ScientificWorkerFailed) as caught:
                await self.prepare(session)
            self.assertIs(diagnostic, caught.exception.diagnostic)
            evidence = scientific_failure_evidence(caught.exception.diagnostic)
            self.assertEqual("RuntimeError", evidence["exception_type"])
            self.assertEqual(errno.EIO, evidence["cause"]["errno"])
            self.assertNotIn("private fixture", json.dumps(evidence))
            self.assertEqual(["prepare"], [command["operation"] for command in worker.commands])
        finally:
            await session.retire()

    async def test_scientific_stop_observation_keeps_positive_retirement_and_independent_source_cleanup(self):
        for failure in (BaseExceptionGroup("contained SCI exit", [SystemExit(0)]),
                        ScientificWorkerFailed({"outcome": "failed", "exception_type": "RuntimeError"})):
            with self.subTest(failure=type(failure).__name__):
                session, worker, _, events = self.make_session([{"outcome": "prepared", "prepared_handle": 7}])
                await self.prepare(session)
                async def stopped_with_observation():
                    events.append("worker-stop")
                    worker.stopped = True
                    raise failure
                worker.stop = stopped_with_observation
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await session.retire()
                self.assertEqual(caught.exception.exceptions, (failure,))
                self.assertIn(("sources-retire", True), events)
                await session.retire()
                self.assertEqual(events.count("worker-stop"), 1)
                self.assertEqual(events.count("chat-retire"), 2)
                self.assertEqual(events.count(("sources-retire", True)), 1)

    def make_session(self, responses, turns=None):
        events = []
        worker = Worker(events, *responses)
        chat = Carrier(events, *(turns or [[choice()]]))
        session = SecsSession(worker=worker, sources=Sources(events), endpoint_factories=(lambda **_: chat,),
                              work_seconds=3, interpretation_seconds=1,
                              startup_seconds=1, context_bytes=1000000)
        return session, worker, chat, events

    async def prepare(self, session):
        acknowledged, instructions = admission()
        return await session.prepare(acknowledged, instructions, report_condition=condition)

    async def test_corrected_preparation_executes_once_and_success_retires_without_stopping_resident_worker(self):
        session, worker, _, events = self.make_session([
            {"outcome": "input_rejected", "reason": "Choose a proton representation."},
            {"outcome": "prepared", "prepared_handle": 7},
            {"outcome": "analysed", "analysis": {"candidates": [{"smiles": "CCO", "score": "1"}],
                "retrieval": {"starting_candidates": 1}, "search": {"outcome": "optimized"}, "preparation": {}, "inference": {}}},
        ], [[choice("first")], [choice("corrected")]])
        prepared = await self.prepare(session)
        self.assertIsInstance(prepared, PreparedAnalysis)
        foreign = PreparedAnalysis(prepared.identity)
        with self.assertRaises(RuntimeError):
            await session.execute(foreign, report_condition=condition)
        outcome = await session.execute(prepared, report_condition=condition)
        self.assertIsInstance(outcome, AnalysisSuccess)
        with self.assertRaises(RuntimeError):
            await session.execute(prepared, report_condition=condition)
        self.assertEqual([request["operation"] for request in worker.commands], ["prepare", "prepare", "execute"])
        self.assertEqual(set(worker.commands[-1]), {"operation", "attempt_ref", "prepared_handle"})
        await session.retire()
        self.assertNotIn("worker-stop", events)
        self.assertEqual(events[-1], ("sources-retire", True))

    async def test_preparation_can_finish_after_interpretation_deadline(self):
        session, worker, chat, _ = self.make_session([{"outcome": "prepared", "prepared_handle": 7}])
        session.interpretation_seconds = 0.05
        request = worker.request

        async def slow_prepare(command, **kwargs):
            if command["operation"] == "prepare":
                await asyncio.sleep(0.1)
            return await request(command, **kwargs)

        worker.request = slow_prepare
        self.assertIsInstance(await self.prepare(session), PreparedAnalysis)
        self.assertEqual(len(chat.requests), 1)
        await session.retire()

    async def test_late_preparation_rejection_does_not_start_another_model_turn(self):
        session, worker, chat, _ = self.make_session(
            [{"outcome": "input_rejected", "reason": "Choose another representation."}],
            [[choice("first")], [choice("second")]],
        )
        session.interpretation_seconds = 0.05
        request = worker.request

        async def slow_prepare(command, **kwargs):
            if command["operation"] == "prepare":
                await asyncio.sleep(0.1)
            return await request(command, **kwargs)

        worker.request = slow_prepare
        with self.assertRaises(InterpretationFailed):
            await self.prepare(session)
        self.assertEqual(len(chat.requests), 1)
        await session.retire()

    async def test_late_source_rejection_does_not_prepare_or_resume(self):
        session, worker, chat, _ = self.make_session([], [[choice("first")], [choice("second")]])
        session.interpretation_seconds = 0.05

        async def slow_reconcile():
            await asyncio.sleep(0.1)
            return ValueError("The selected source changed.")

        session.sources.reconcile = slow_reconcile
        with self.assertRaises(InterpretationFailed):
            await self.prepare(session)
        self.assertEqual(len(chat.requests), 1)
        self.assertEqual(worker.commands, [])
        await session.retire()

    async def test_mock_index_success_and_failure_identify_the_limited_search(self):
        session, _, _, _ = self.make_session([
            {"outcome": "prepared", "prepared_handle": 7},
            {"outcome": "analysed", "analysis": {"candidates": [{"smiles": "CCO", "score": "1"}],
                "retrieval": {"starting_candidates": 1}, "search": {"outcome": "optimized"},
                "preparation": {}, "inference": {}}},
        ])
        session._mock_index = True
        prepared = await self.prepare(session)
        outcome = await session.execute(prepared, report_condition=condition)
        self.assertIn("mock_index", json.loads(outcome.content))
        failure = session._failure("cannot_analyse", "x" * 1024)
        self.assertIn("MOCK INDEX", failure.message)
        self.assertLessEqual(len(failure.message), 1024)
        fail_body("execution_attempt:sha256:" + "b" * 64, failure.code, failure.message)
        await session.retire()

    async def test_unconsumed_preparation_stops_before_source_retirement(self):
        session, _, _, events = self.make_session([{"outcome": "prepared", "prepared_handle": 7}])
        await self.prepare(session)
        await session.retire()
        self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))

    async def test_cancelled_execution_still_requires_physical_stop_before_source_retirement(self):
        session, worker, _, events = self.make_session([
            {"outcome": "prepared", "prepared_handle": 7}, "blocked",
        ])
        prepared = await self.prepare(session)
        task = asyncio.create_task(session.execute(prepared, report_condition=condition))
        await asyncio.wait_for(worker.entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await session.retire()
        self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))
        with self.assertRaises(RuntimeError):
            await session.execute(prepared, report_condition=condition)

    async def test_failed_stop_holds_sources_and_preserves_independent_cleanup_causes(self):
        session, worker, chat, events = self.make_session([{"outcome": "prepared", "prepared_handle": 7}])
        await self.prepare(session)
        failure = RuntimeError("native retirement unconfirmed")
        worker.stopping_failure = failure
        chat.retirement_failure = RuntimeError("chat retirement unconfirmed")
        with self.assertRaises(ExceptionGroup) as caught:
            await session.retire()
        self.assertIn(failure, caught.exception.exceptions)
        self.assertIn("chat-retire", events)
        self.assertIn(("sources-retire", False), events)
        self.assertNotIn(("sources-retire", True), events)

    async def test_model_report_propagates_without_search_or_forced_worker_restart(self):
        session, worker, _, events = self.make_session([], [[call(
            "report_interpretation_problem", {"explanation": "No supported spectrum."}, "refusal")]])
        with self.assertRaises(InterpretationFailed) as caught:
            await self.prepare(session)
        aggregate = caught.exception.__cause__
        self.assertIs(ModelsExhausted, type(aggregate))
        self.assertEqual(1, len(aggregate.failures))
        report = aggregate.failures[0][1]
        self.assertIs(ModelReportedInterpretationProblem, type(report))
        self.assertIs(report, session._conversation.conversation.failures[0][1])
        await session.retire()
        self.assertEqual(worker.commands, [])
        self.assertNotIn("worker-stop", events)

    async def test_invalid_prepared_handle_poisons_worker_before_releasing_sources(self):
        session, _, _, events = self.make_session([{"outcome": "prepared", "prepared_handle": True}])
        with self.assertRaises(AnalysisOutcomeUnresolved) as caught:
            await self.prepare(session)
        self.assertIs(type(caught.exception.__cause__), WorkerError)
        await session.retire()
        self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))
