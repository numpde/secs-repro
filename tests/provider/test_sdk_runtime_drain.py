"""Provider task-group failure cannot abandon an SDK-selected durable report."""

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from provider_core.analysis import AnalysisSuccess, PreparedAnalysis
from provider_core.api import ProviderAPI
from provider_core.attempt_record import ProtectedEvidence, RecoveryScope
from provider_core.controller import AttemptController, LiveStartPolicy
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import DeliveryFull, DeliveryQueue, QueueLimits
from provider_core.job_feed import DiscoveredJob, JobPage
from provider_core.job_input import FeedInputFacts, VerifiedJobInput
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpResponse

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.config import HelloPolicy
from secs_inference.provider.runtime import _drain_controller, run_services


ORIGIN, PROVIDER = "https://api.test", "provider:secs-test"
ATTEMPT = "execution_attempt:sha256:" + "b" * 64
PAYLOAD = b"Find C2H6O"
FACTS = FeedInputFacts("job:chosen", "nmr.job.specification.text.v1",
                       "sha256:" + sha256(PAYLOAD).hexdigest(), len(PAYLOAD))


class LifecycleTransport:
    """Authored API replies; actual SDK signing and wire admission remain active."""
    exchange_seconds, retirement_seconds = 1, 1
    retirement_confirmed, pending_owner, last_failure = True, None, None

    async def retire(self, *, owner=None):
        pass

    async def exchange(self, request, *, maximum_bytes, owner=None):
        path = request.url.path
        if path.endswith("/start"):
            document = {"schema_id": "nmr.provider.execution_attempt_start_response.v1",
                        "execution_attempt_ref": ATTEMPT, "job_ref": FACTS.job_ref,
                        "analysis_kind_ref": ANALYSIS_KIND_REF, "provider_ref": PROVIDER,
                        "state": "in_progress", "started_at": "2026-10-29T00:00:00Z", "replayed": False}
        elif path.endswith("/progress"):
            command = json.loads(request.content)
            document = {"schema_id": "nmr.provider.execution_attempt_progress_response.v1",
                        "execution_attempt_ref": ATTEMPT, "phase": command["phase"],
                        "condition_code": command["condition_code"], "updated_at": "2026-10-29T00:00:00Z"}
        elif request.method == "GET" and path.endswith(ATTEMPT):
            document = {"schema_id": "nmr.provider.execution_attempt_read_response.v1",
                        "execution_attempt_ref": ATTEMPT, "job_ref": FACTS.job_ref,
                        "state": "in_progress", "job_state": "closed"}
        else:
            raise AssertionError(f"Unexpected API effect: {request.method} {path}")
        body = json.dumps(document).encode()
        if len(body) > maximum_bytes:
            raise AssertionError("Fixture response exceeds the production route ceiling")
        return HttpResponse(200, ((b"content-type", b"application/json"),
                                 (b"cache-control", b"no-store"), (b"nmr-api-topology", b"dev-local")), body)


class Science:
    """A completed scientific owner; this fixture proves task composition, not PID exit."""
    def __init__(self):
        self.handle = PreparedAnalysis(ProtectedEvidence("fixture.input.v1", b"chosen"))
        self.executions = self.retirements = 0

    async def prepare(self, acknowledged, instructions, *, report_condition):
        return self.handle

    async def execute(self, prepared, *, report_condition):
        if prepared is not self.handle:
            raise AssertionError("Substituted prepared handle")
        self.executions += 1
        return AnalysisSuccess("secs.elucidation.result.v1", b'{"outcome":"analysed"}',
                               ProtectedEvidence("fixture.evidence.v1", b"observed"))

    async def retire(self):
        self.retirements += 1


class Backpressure:
    """Withhold capacity, then use the real Unix/SQLite inbox acknowledgement."""
    def __init__(self, client):
        self.client, self.endpoint = client, client.endpoint
        self.selected, self.release = asyncio.Event(), asyncio.Event()
        self.items = []

    @property
    def retirement_confirmed(self):
        return self.client.retirement_confirmed

    async def submit(self, item):
        self.items.append(item)
        self.selected.set()
        if not self.release.is_set():
            raise DeliveryFull()
        return await self.client.submit(item)

    async def retire(self):
        await self.client.retire()


class Discovery:
    async def jobs(self, **query):
        return JobPage(ANALYSIS_KIND_REF, False, (DiscoveredJob(
            FACTS, ANALYSIS_KIND_REF, datetime.now(timezone.utc)),), None)


class SdkRuntimeDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_cancellation_during_a_blocked_drain_keeps_original_owners(self):
        blocked, release = asyncio.Event(), asyncio.Event()
        class Controller:
            shutdown_ready = False
            operation = None
            async def join(self):
                raise RuntimeError("fixture driver already failed")
            async def settle(self):
                if not release.is_set():
                    blocked.set()
                    raise RuntimeError("fixture original owner remains held")
                self.shutdown_ready = True
        controller = Controller()
        drain = asyncio.create_task(_drain_controller(controller, poll_seconds=0.1))
        try:
            await asyncio.wait_for(blocked.wait(), 1)
            await asyncio.sleep(0)
            drain.cancel()
            await asyncio.sleep(0)
            self.assertFalse(drain.done(), "The observer abandoned an unsettled owner")
            release.set()
            await asyncio.wait_for(drain, 1)
            self.assertTrue(controller.shutdown_ready)
        finally:
            release.set()
            await asyncio.gather(drain, return_exceptions=True)

    async def test_contained_driver_exit_still_settles_original_owners(self):
        failure = BaseExceptionGroup("contained driver exit", [SystemExit("private fixture detail")])
        class Controller:
            shutdown_ready = False
            async def join(self):
                raise failure
            async def settle(self):
                self.shutdown_ready = True
        controller = Controller()
        with self.assertLogs("secs_inference.provider.runtime", level="ERROR") as captured:
            with self.assertRaises(BaseExceptionGroup) as caught:
                await _drain_controller(controller, poll_seconds=0.01)
        self.assertIs(caught.exception, failure)
        self.assertTrue(controller.shutdown_ready)
        self.assertNotIn("private fixture detail", " ".join(captured.output))

    async def exercise_failure(self, fault):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            socket_directory = root / "ipc"
            socket_directory.mkdir(mode=0o700)
            endpoint = DeliveryEndpoint(ORIGIN, PROVIDER, socket_directory / "delivery.sock")
            limits = QueueLimits(1, 2097152, 1, 2097152)
            (root / "queue").mkdir(mode=0o700)
            queue = DeliveryQueue(root / "queue", limits=limits)
            inbox = DeliveryInbox(queue, (endpoint,), request_seconds=1,
                                  on_accepted=lambda _endpoint: None, on_failure=lambda *_args: None,
                                  binding_status=lambda _endpoint: {})
            await inbox.start()
            client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=1)
            delivery, science = Backpressure(client), Science()
            claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152))
            scope = RecoveryScope(ORIGIN, PROVIDER, "secs", "runtime-one")
            admission_stop, science_stop = asyncio.Event(), asyncio.Event()
            controllers = []
            def assemble(pending):
                identity = SigningIdentity(ORIGIN, "credential:test", Ed25519PrivateKey.generate())
                controller = AttemptController.live(
                    ProviderAPI(identity, LifecycleTransport(), expected_topology="dev-local"), claims, science,
                    scope=scope, stopping=science_stop, admission_stopping=admission_stop,
                    start_policy=LiveStartPolicy(0.001, lambda *_args: None, lambda *_args: None),
                    preverified_input=VerifiedJobInput(FACTS, PAYLOAD), delivery=delivery,
                    on_delivery_retry=lambda *_args: None,
                    delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.01,
                    allowed_job_states=frozenset({"open", "closed"}), poll_seconds=0.01, task_settle_seconds=1,
                )
                controllers.append(controller)
                return controller
            class Hello:
                async def publish_hello(self, **presentation):
                    if fault == "hello":
                        await delivery.selected.wait()
                        raise RuntimeError("hello sibling failed")
            config = SimpleNamespace(hello=HelloPolicy("SECS", "fixture", 3600, 1),
                                     execution=SimpleNamespace(poll_seconds=0.01, worker_startup_seconds=1))
            worker = SimpleNamespace(check_ready=AsyncMock())
            async def provider():
                await run_services(hello_api=Hello(), discovery_api=Discovery(), provider_ref=PROVIDER,
                                   config=config, scope=scope, claims=claims, stop=admission_stop, assemble=assemble, worker=worker)
            async def outer():
                async def sibling():
                    await delivery.selected.wait()
                    raise RuntimeError("outer service sibling failed")
                async with asyncio.TaskGroup() as group:
                    group.create_task(provider())
                    group.create_task(sibling())
            process = asyncio.create_task(provider() if fault == "hello" else outer())
            try:
                await asyncio.wait_for(delivery.selected.wait(), 2)
                await asyncio.wait_for(admission_stop.wait(), 2)
                self.assertFalse(process.done(), "Task group discarded its pending selected report")
                self.assertFalse(controllers[0].shutdown_ready)
                delivery.release.set()
                with self.assertRaises(ExceptionGroup):
                    await asyncio.wait_for(asyncio.shield(process), 3)
                self.assertTrue(controllers[0].shutdown_ready)
                self.assertEqual(science.executions, 1)
                self.assertEqual(science.retirements, 1)
                self.assertTrue(all(item is delivery.items[0] for item in delivery.items))
            finally:
                delivery.release.set()
                await asyncio.gather(process, return_exceptions=True)
                await client.retire()
                await inbox.close()
                queue.close()
            reopened = DeliveryQueue(root / "queue", limits=limits)
            try:
                retained = reopened.next_pending(ORIGIN, PROVIDER)
                self.assertEqual(retained.body, delivery.items[0].body)
                self.assertEqual(retained.body_sha256, delivery.items[0].body_sha256)
            finally:
                reopened.close()

    async def test_hello_sibling_failure_drains_the_original_selected_report(self):
        await self.exercise_failure("hello")

    async def test_outer_service_failure_drains_the_original_selected_report(self):
        await self.exercise_failure("outer")
