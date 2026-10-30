"""Select serial SECS Jobs and retain shared controllers through orderly drain."""

import asyncio
import json
import logging
from provider_core.task_boundary import contain_task_exit
from secrets import token_hex
from time import monotonic

from provider_core.api import ApiFailure
from provider_core.attempt_record import StartPending
from provider_core.attempt_request import start_body
from provider_core.execution import AttemptExecutor, ExecutionRefused
from provider_core.diagnostics import failure_evidence

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.process import publish_hello_until_stopped, wait_until_stopped
from secs_inference.provider.worker import NativeWorkerExited


_LOG = logging.getLogger(__name__)


async def _drain_controller(controller, *, poll_seconds):
    """Join the original driver before settlement; retain every unaccepted report."""
    fatal_failures = []
    while True:
        observer = asyncio.current_task()
        cancellations = observer.cancelling()
        try:
            await controller.join()
            break
        except asyncio.CancelledError:
            if observer.cancelling() > cancellations:
                # Further observer cancellation cannot abandon this drain's
                # original driver or transfer its report ownership elsewhere.
                continue
            _LOG.error("Attempt driver was interrupted; settling its retained original owners")
            break
        except (Exception, BaseExceptionGroup) as cause:
            if not isinstance(cause, Exception):
                fatal_failures.append(cause)
            _LOG.error("Attempt driver failed; settling its retained original owners and protected diagnostic evidence")
            break
    while not controller.shutdown_ready:
        try:
            await controller.settle()
        except (Exception, BaseExceptionGroup, asyncio.CancelledError) as cause:
            if (isinstance(cause, BaseExceptionGroup) and not isinstance(cause, Exception)
                    and all(cause is not retained for retained in fatal_failures)):
                fatal_failures.append(cause)
            operation = controller.operation
            if isinstance(operation, AttemptExecutor) and operation.work_settled and not operation.shutdown_ready:
                try:
                    await operation.retain_suppressed_disposition()
                except ExecutionRefused:
                    # A selected report cannot acquire a suppression disposition.
                    # Its unchanged handoff stays owned until delivery accepts it.
                    pass
            if not controller.shutdown_ready:
                _LOG.error("Provider drain is blocked by retained Attempt work or reporting; lifetime ownership remains held")
                try:
                    await asyncio.sleep(poll_seconds)
                except asyncio.CancelledError:
                    continue
    if len(fatal_failures) == 1:
        raise fatal_failures[0]
    if fatal_failures:
        raise BaseExceptionGroup("Attempt drain observed independent fatal failures", fatal_failures)


async def run_execution(*, api, scope, claims, stop, poll_seconds, assemble, worker, startup_seconds):
    """Discover fresh unattempted Jobs; the SDK alone admits and drives Attempts."""
    retry_seconds = poll_seconds
    while not stop.is_set():
        try:
            page = await api.jobs(analysis_kind_ref=ANALYSIS_KIND_REF,
                                  has_provider_execution_attempt=False, limit=1, cursor=None)
        except ApiFailure as cause:
            await api.retire()
            if not api.can_retry_job_discovery(cause):
                raise
            advised = cause.retry_after_seconds
            delay = max(retry_seconds, advised) if advised is not None else retry_seconds
            _LOG.warning("Job discovery unavailable; retrying in %g seconds (HTTP %s; request %s)",
                         delay, cause.status, cause.request_id)
            await wait_until_stopped(stop, delay)
            retry_seconds = min(300, retry_seconds * 2)
            continue
        retry_seconds = poll_seconds
        if not page.jobs:
            await wait_until_stopped(stop, poll_seconds)
            continue
        selected = page.jobs[0]
        if stop.is_set():
            break
        startup_deadline = monotonic() + startup_seconds
        try:
            await worker.check_ready(deadline=startup_deadline)
        except NativeWorkerExited as cause:
            _LOG.warning("Idle scientific worker exited; retiring it before a new Attempt is admitted; evidence=%s",
                         json.dumps({"returncode": cause.runner.original_exit.returncode,
                                     "failure": failure_evidence(cause, secrets=())}))
            await worker.stop()
            if stop.is_set():
                break
            await worker.check_ready(deadline=startup_deadline)
        if stop.is_set():
            break
        pending = StartPending(scope, ANALYSIS_KIND_REF, selected.input,
                               start_body(selected.input.job_ref, token_hex(16)))
        controller = assemble(pending)
        try:
            await controller.run(pending)
        finally:
            if not controller.shutdown_ready:
                stop.set()
                await _drain_controller(controller, poll_seconds=poll_seconds)
        if claims.live_owners():
            # A lost START reply can leave a passive owned Job after cleanup.
            # One resident scientific worker has no capacity for another owner.
            stop.set()
            _LOG.error("Admission stopped: the shared controller retains unresolved START or Attempt ownership")


async def run_services(*, hello_api, discovery_api, provider_ref, config, scope, claims, stop, assemble, worker):
    """Close admission on sibling failure and drain the original SDK driver."""
    async def hello():
        try:
            await publish_hello_until_stopped(api=hello_api, provider_ref=provider_ref,
                                             policy=config.hello, stop=stop)
        finally:
            stop.set()

    async def execution():
        try:
            await run_execution(api=discovery_api, scope=scope, claims=claims, stop=stop,
                                poll_seconds=config.execution.poll_seconds, assemble=assemble, worker=worker,
                                startup_seconds=config.execution.worker_startup_seconds)
        finally:
            stop.set()

    async with asyncio.TaskGroup() as group:
        group.create_task(contain_task_exit(hello))
        group.create_task(contain_task_exit(execution))
