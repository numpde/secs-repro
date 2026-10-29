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
from provider_core.attempt_request import start_body
from provider_core.execution import ConditionRecoveryExpired, ObservationGapExpired
from provider_core.attempt_response import AttemptAdmission
from provider_core.job_input import FeedInputFacts, VerifiedJobInput

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.session import SecsSession
from secs_inference.provider.worker import WorkerClient, WorkerDeadlineBeforeDispatch
from secs_inference.provider.worker_protocol import InvalidFailureEnvelope, ScientificWorkerFailed, WorkerError
from secs_inference.provider.diagnostics import exception_evidence
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

    async def test_source_preparation_timeout_remains_public_deadline(self):
        session, _, _, _ = self.make_session([])
        async def timed_out(*, deadline, readers_quiescent):
            raise TimeoutError("source fixture timeout")
        session.sources.open = timed_out
        try:
            outcome = await self.prepare(session)
            self.assertIs(type(outcome), AnalysisFailure)
            self.assertEqual(outcome.code, "work_deadline_exceeded")
        finally:
            await session.retire()

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

    async def test_received_scientific_failure_remains_a_safe_public_failure(self):
        frame = {"outcome": "failed", **exception_evidence(RuntimeError("private scientific detail"))}
        session, _, _, events = self.make_session([{"outcome": "prepared", "prepared_handle": 7}, frame])
        prepared = await self.prepare(session)
        outcome = await session.execute(prepared, report_condition=condition)
        self.assertIsInstance(outcome, AnalysisFailure)
        self.assertEqual(outcome.code, "scientific_execution_failed")
        self.assertNotIn(b"private scientific detail", outcome.evidence.content)
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
        session, _, _, _ = self.make_session([diagnostic])
        outcome = await self.prepare(session)
        self.assertIsInstance(outcome, AnalysisFailure)
        evidence = json.loads(outcome.evidence.content)
        self.assertEqual(evidence["failure"]["scientific_worker"]["exception_type"], "RuntimeError")
        self.assertEqual(evidence["failure"]["scientific_worker"]["cause"]["errno"], errno.EIO)
        self.assertNotIn(b"private fixture", outcome.evidence.content)
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
                self.assertEqual(events.count("chat-retire"), 1)
                self.assertEqual(events.count(("sources-retire", True)), 1)

    def make_session(self, responses, turns=None):
        events = []
        worker = Worker(events, *responses)
        chat = Carrier(events, *(turns or [[choice()]]))
        session = SecsSession(worker=worker, sources=Sources(events), chat=chat,
                              work_seconds=3, interpretation_seconds=1,
                              startup_seconds=1, max_turns=3)
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

    async def test_explicit_inability_is_a_failure_without_search_or_forced_worker_restart(self):
        session, worker, _, events = self.make_session([], [[call(
            "report_input_problem", {"explanation": "No supported spectrum."}, "refusal")]])
        outcome = await self.prepare(session)
        self.assertIsInstance(outcome, AnalysisFailure)
        self.assertEqual(outcome.code, "cannot_analyse")
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
