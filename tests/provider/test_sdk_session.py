"""Attempt retirement preserves prepared ownership and the resident worker."""

import asyncio
import errno
import json
from datetime import datetime, timezone
from hashlib import sha256
import unittest

from provider_core.analysis import AnalysisFailure, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import RecoveryScope, StartPending, acknowledge_start
from provider_core.attempt_request import start_body
from provider_core.attempt_response import AttemptAdmission
from provider_core.job_input import FeedInputFacts, VerifiedJobInput

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.session import SecsSession
from secs_inference.provider.worker_protocol import ScientificWorkerFailed, WorkerError
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
    async def check_ready(self, *, deadline):
        self.events.append("worker-ready")
    async def request(self, command, *, deadline):
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


class SdkSessionTests(unittest.IsolatedAsyncioTestCase):
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
            {"outcome": "analysed", "analysis": {"candidates": []}},
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
        outcome = await self.prepare(session)
        self.assertIsInstance(outcome, AnalysisFailure)
        self.assertEqual(outcome.code, "scientific_execution_failed")
        await session.retire()
        self.assertLess(events.index("worker-stop"), events.index(("sources-retire", True)))
