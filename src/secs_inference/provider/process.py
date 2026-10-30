"""Refresh the SECS offering through its independent shared API owner."""

import asyncio
import logging

from provider_core.api import ApiFailure
from provider_core.hello import AnalysisOffering

from secs_inference.provider.analysis import ANALYSIS_KIND_REF, analysis_offering_description


_LOG = logging.getLogger(__name__)


async def wait_until_stopped(stop, seconds):
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            await stop.wait()
    except TimeoutError:
        if not deadline.expired():
            raise


async def publish_hello_until_stopped(*, api, provider_ref, policy, stop):
    """Publish current presentation; a failed send never replays an old Hello."""
    retry_seconds = min(policy.retry_initial_seconds, 300)
    while not stop.is_set():
        try:
            offering = analysis_offering_description()
            if policy.mock_runner:
                offering = "MOCK RUNNER: returns a mock result without running a model or scientific analysis."
            elif policy.mock_index:
                offering = "MOCK INDEX: starts from eight demonstration molecules. Results do not represent a full candidate search."
            await api.publish_hello(
                provider_ref=provider_ref, display_name=policy.display_name,
                description=policy.provider_description,
                analysis_offerings=(AnalysisOffering(ANALYSIS_KIND_REF, offering),),
            )
        except ApiFailure as cause:
            await api.retire()
            if not api.can_refresh_hello(cause):
                raise
            _LOG.warning("Provider hello unavailable; refreshing current presentation in %g seconds (HTTP %s; request %s)",
                         retry_seconds, cause.status, cause.request_id)
            await wait_until_stopped(stop, retry_seconds)
            retry_seconds = min(retry_seconds * 2, 300)
        else:
            _LOG.info("Provider hello published")
            retry_seconds = min(policy.retry_initial_seconds, 300)
            await wait_until_stopped(stop, policy.publication_interval_seconds)
