"""A mock-index deployment advertises its limited starting candidate set."""

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, patch

from secs_inference.provider.config import HelloPolicy
from secs_inference.provider.process import publish_hello_until_stopped
from secs_inference.provider import mock_index_worker


class MockIndexPresentationTests(unittest.IsolatedAsyncioTestCase):
    def test_worker_requires_distinct_session_and_lease_descriptors(self):
        with patch.object(sys, 'argv', ['worker', '--session-fd', '1', '--lease-fd', '1', '--operation', 'start']):
            with self.assertRaisesRegex(ValueError, 'distinct open descriptors'):
                mock_index_worker.main()

    async def test_hello_offering_states_the_mock_index_limit(self):
        stop = asyncio.Event()
        async def published(**_presentation):
            stop.set()
        api = type('API', (), {})()
        api.publish_hello = AsyncMock(side_effect=published)
        policy = HelloPolicy('SECS mock', 'Mock index', 60, 1, mock_index=True)
        await publish_hello_until_stopped(api=api, provider_ref='provider:test', policy=policy, stop=stop)
        offering = api.publish_hello.await_args.kwargs['analysis_offerings'][0]
        self.assertIn('MOCK INDEX', offering.description)
        self.assertIn('eight', offering.description)
        self.assertLessEqual(len(offering.description), 1024)
