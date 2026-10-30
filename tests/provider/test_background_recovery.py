"""Background retry timing respects server advice without changing admission."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.api import ApiFailure
from provider_core.transport import HttpResponse
from secs_inference.provider.config import HelloPolicy
from secs_inference.provider.main import _recovered_notice, _retry_notice
from secs_inference.provider.process import publish_hello_until_stopped, wait_until_stopped
from secs_inference.provider.runtime import run_execution


class BackgroundRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_failure(self, route, headers, *, admitted=True, status=429):
        stop = asyncio.Event()
        failure = ApiFailure('contact the provider API', 'fixture failure')
        api = type('API', (), {})()
        async def fail(**_kwargs):
            raise failure
        async def retire():
            # Transport retirement may reveal the original response head late.
            failure.response = HttpResponse(status, headers, b'')
        api.jobs = AsyncMock(side_effect=fail)
        api.publish_hello = AsyncMock(side_effect=fail)
        api.retire = AsyncMock(side_effect=retire)
        api.can_retry_job_discovery = lambda actual: actual is failure and admitted
        api.can_refresh_hello = lambda actual: actual is failure and admitted
        waits = []
        async def observe_wait(event, seconds):
            waits.append(seconds)
            event.set()
        if route == 'hello':
            target = 'secs_inference.provider.process.wait_until_stopped'
            run = lambda: publish_hello_until_stopped(api=api, provider_ref='provider:test',
                policy=HelloPolicy('SECS', 'Test', 60, 1), stop=stop)
        else:
            target = 'secs_inference.provider.runtime.wait_until_stopped'
            run = lambda: run_execution(api=api, scope=None, claims=None, stop=stop, poll_seconds=1,
                                        assemble=None, worker=None, startup_seconds=1)
        with patch(target, side_effect=observe_wait):
            if admitted:
                await run()
            else:
                with self.assertRaises(ApiFailure) as caught:
                    await run()
                self.assertIs(caught.exception, failure)
        api.retire.assert_awaited_once()
        return waits

    async def test_late_server_retry_after_is_the_minimum_for_both_loops(self):
        for route in ('hello', 'discovery'):
            with self.subTest(route=route):
                self.assertEqual(await self.run_failure(route, ((b'retry-after', b'600'),)), [600])
                self.assertEqual(await self.run_failure(route, ((b'retry-after', b'600'),), status=503), [600])
                self.assertEqual(await self.run_failure(route, ((b'retry-after', b'invalid'),)), [1])
                self.assertEqual(await self.run_failure(route, ()), [1])
                self.assertEqual(await self.run_failure(route, ((b'retry-after', b'600'),), admitted=False), [])

    async def test_stop_interrupts_a_long_wait(self):
        stop = asyncio.Event()
        task = asyncio.create_task(wait_until_stopped(stop, 600))
        await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, 1)

    def test_retry_and_recovery_have_distinct_safe_messages(self):
        with self.assertLogs('secs_inference.provider.main', level='INFO') as captured:
            _retry_notice('private sentinel')
            _recovered_notice('private sentinel')
        self.assertIn('retrying', captured.output[0])
        self.assertIn('recovered', captured.output[1])
        self.assertNotIn('private sentinel', ' '.join(captured.output))
