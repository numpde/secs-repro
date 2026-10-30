"""The mock runner returns a labeled result and retains native ownership."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from provider_core.analysis import AnalysisFailure, AnalysisOutcomeUnresolved, AnalysisSuccess
from provider_core.execution import ConditionRecoveryExpired, ObservationGapExpired
from secs_inference.provider.mock_runner_session import MockRunnerSession
from secs_inference.provider.worker import WorkerDeadlineBeforeDispatch
from test_sdk_session import admission, condition


class MockRunnerTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, replies):
        worker = type('Worker', (), {})()
        worker.check_ready = AsyncMock()
        worker.request = AsyncMock(side_effect=replies)
        worker.stop = AsyncMock()
        return worker

    async def test_mock_result_contains_no_scientific_claim(self):
        worker = self.worker([{'outcome': 'prepared', 'prepared_handle': 1},
                              {'outcome': 'mock', 'message': 'No model or scientific analysis was run.'}])
        session = MockRunnerSession(worker=worker, work_seconds=2, startup_seconds=1)
        acknowledged, instructions = admission()
        prepared = await session.prepare(acknowledged, instructions, report_condition=condition)
        result = await session.execute(prepared, report_condition=condition)
        self.assertIsInstance(result, AnalysisSuccess)
        self.assertEqual(result.result_schema_id, 'secs.mock_runner.result.v1')
        self.assertTrue(json.loads(result.content)['mock_runner'])
        self.assertIn('no model', json.loads(result.content)['message'].lower())
        await session.retire()
        worker.stop.assert_not_awaited()

    async def test_abandoned_preparation_retires_the_native_owner(self):
        worker = self.worker([{'outcome': 'prepared', 'prepared_handle': 1}])
        session = MockRunnerSession(worker=worker, work_seconds=2, startup_seconds=1)
        acknowledged, instructions = admission()
        await session.prepare(acknowledged, instructions, report_condition=condition)
        await session.retire()
        worker.stop.assert_awaited_once()

    async def test_predispatch_expiry_and_unconfirmed_readiness_retire_the_worker(self):
        acknowledged, instructions = admission()
        worker = self.worker([WorkerDeadlineBeforeDispatch('before dispatch')])
        session = MockRunnerSession(worker=worker, work_seconds=2, startup_seconds=1)
        failure = await session.prepare(acknowledged, instructions, report_condition=condition)
        self.assertIsInstance(failure, AnalysisFailure)
        self.assertIn('MOCK RUNNER', failure.message)
        await session.retire()
        worker.stop.assert_awaited_once()

    async def test_readiness_preserves_sdk_condition_expiries(self):
        for expiry in (ConditionRecoveryExpired(1, phase="preparing", condition_code=None, cause=None),
                       ObservationGapExpired(1, executor=SimpleNamespace(_observation_read_started=False))):
            worker = self.worker([])
            worker.check_ready.side_effect = expiry
            session = MockRunnerSession(worker=worker, work_seconds=2, startup_seconds=1)
            acknowledged, instructions = admission()
            with self.assertRaises(type(expiry)) as caught:
                await session.prepare(acknowledged, instructions, report_condition=condition)
            self.assertIs(caught.exception, expiry)
            await session.retire()
            worker.stop.assert_awaited_once()
        worker = self.worker([])
        worker.check_ready.side_effect = TimeoutError()
        session = MockRunnerSession(worker=worker, work_seconds=2, startup_seconds=1)
        with self.assertRaises(AnalysisOutcomeUnresolved):
            await session.prepare(acknowledged, instructions, report_condition=condition)
        await session.retire()
        worker.stop.assert_awaited_once()

    async def test_worker_failure_retains_structural_evidence(self):
        frame = {"outcome": "failed", "exception_id": 0, "exception_type": "ValueError", "frames": []}
        worker = self.worker([frame])
        session = MockRunnerSession(worker=worker, work_seconds=2, startup_seconds=1)
        acknowledged, instructions = admission()
        failure = await session.prepare(acknowledged, instructions, report_condition=condition)
        self.assertIsInstance(failure, AnalysisFailure)
        self.assertEqual(json.loads(failure.evidence.content)["scientific_worker"], frame)
        await session.retire()
