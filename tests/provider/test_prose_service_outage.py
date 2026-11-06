"""The running SECS provider must honor one prose service's wait across Jobs.

These tests exercise SECS's real discovery/admission loop and exact SDK
StartPending construction. The model-service delay and scientific worker are
controlled test ports; the SDK owns HTTP request and Retry-After semantics.
"""
import asyncio
from hashlib import sha256
from time import monotonic
from types import SimpleNamespace
import unittest

from provider_core.attempt_record import RecoveryScope
from provider_core.job_input import FeedInputFacts
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from secs_inference.provider.runtime import run_execution


class ProseServiceOutageTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_second_job_waits_for_the_first_jobs_service_delay_then_runs_automatically(self):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        first_finished = asyncio.Event()
        starts = []
        discoveries = []
        delay_until = 0.0
        payload = b"1H NMR interpretation input"
        facts = tuple(
            FeedInputFacts(f"job:secs-outage-{index}", "nmr.job.specification.text.v1",
                           "sha256:" + sha256(payload).hexdigest(), len(payload))
            for index in range(2)
        )

        class Service:
            @property
            def retry_seconds(self):
                return max(0.0, delay_until - loop.time())

        class Discovery:
            async def jobs(self, **query):
                discoveries.append((loop.time(), query))
                return SimpleNamespace(jobs=(SimpleNamespace(input=facts[len(starts)]),))

        class Worker:
            async def check_ready(self, *, deadline):
                self_deadline = deadline
                if self_deadline < monotonic():
                    raise AssertionError("The original worker-readiness deadline elapsed")

        class Controller:
            shutdown_ready = True

            async def run(self, pending):
                nonlocal delay_until
                starts.append((pending.input.job_ref, loop.time()))
                if len(starts) == 1:
                    # The SDK already classified the preceding service refusal;
                    # a new Job must not spend that service's requested wait.
                    delay_until = loop.time() + 0.25
                    first_finished.set()
                else:
                    stop.set()

        async def run():
            await run_execution(
                api=Discovery(),
                scope=RecoveryScope("https://api.test", "provider:secs-test", "secs", "b4"),
                claims=AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152)),
                stop=stop, poll_seconds=0.01, assemble=lambda _pending: Controller(),
                worker=Worker(), startup_seconds=1, prose_services=(Service(),),
            )

        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(first_finished.wait(), 1)
            await asyncio.sleep(0.08)
            self.assertEqual(1, len(starts), "SECS started another Job during the previous Job's prose-service wait")
            self.assertEqual(1, len(discoveries), "SECS continued discovery while all prose services were unavailable")
            await asyncio.wait_for(task, 1)
            self.assertEqual([item.job_ref for item in facts], [ref for ref, _ in starts])
            self.assertGreaterEqual(starts[1][1] - starts[0][1], 0.25)
        finally:
            stop.set()
            await asyncio.wait_for(task, 2)

    async def test_stop_interrupts_a_long_service_delay_without_discovering_a_job(self):
        stop = asyncio.Event()
        selected = []
        class Service:
            retry_seconds = 600.0
        class Discovery:
            async def jobs(self, **_query):
                selected.append("discovered")
                raise AssertionError("No Job should be discovered while the sole prose service is postponed")
        task = asyncio.create_task(run_execution(
            api=Discovery(), scope=RecoveryScope("https://api.test", "provider:secs-test", "secs", "stop"),
            claims=AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152)),
            stop=stop, poll_seconds=0.01,
            assemble=lambda _pending: (_ for _ in ()).throw(AssertionError("No Attempt may be constructed")),
            worker=SimpleNamespace(check_ready=None), startup_seconds=1, prose_services=(Service(),),
        ))
        try:
            await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(task, 0.5)
            self.assertEqual([], selected)
        finally:
            stop.set()
            await asyncio.wait_for(task, 1)
