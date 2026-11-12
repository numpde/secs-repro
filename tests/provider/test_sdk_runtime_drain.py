"""Provider task-group failure cannot abandon an SDK-selected durable report."""

import asyncio
import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from provider_core.analysis import AnalysisFailure, AnalysisSuccess, PreparedAnalysis
from provider_core.api import ProviderAPI
from provider_core.api_config import api_settings_with_material
from provider_core.attempt_record import ProtectedEvidence, RecoveryScope, StartPending, TerminalOperation
from provider_core.attempt_request import start_body
from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import DeliveryFull, DeliveryQueue, QueueLimits
from provider_core.execution import ExecutionStopRequested
from provider_core.job_feed import DiscoveredJob, JobPage
from provider_core.job_input import FeedInputFacts, VerifiedJobInput
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.signing import SigningIdentity
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.shared_delivery_ipc import SharedDeliveryInbox, SharedDeliverySocket
from provider_core.transport import HttpResponse

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.config import HelloPolicy
from secs_inference.provider.interpreter import InterpretationFailed
from secs_inference.provider.runtime import _drain_controller, run_execution, run_services
from secs_inference.provider.session import SecsSession
from secs_inference.provider.worker import WorkerClient
from secs_inference.provider.worker_protocol import ScientificWorkerFailed
from test_async_interpretation import call, choice
from test_sdk_session import Carrier, Sources, Worker


ORIGIN, PROVIDER = "https://api.test", "provider:secs-test"
ATTEMPT = "execution_attempt:sha256:" + "b" * 64
PAYLOAD = b"Find C2H6O"
FACTS = FeedInputFacts("job:chosen", "nmr.job.specification.text.v1",
                       "sha256:" + sha256(PAYLOAD).hexdigest(), len(PAYLOAD))


class LifecycleTransport:
    """Authored API replies; actual SDK signing and wire admission remain active."""
    exchange_seconds, retirement_seconds = 1, 1
    retirement_confirmed, pending_owner, last_failure = True, None, None

    def __init__(self, *, fail_observations=0, fail_conditions=0, facts=FACTS, attempt=ATTEMPT):
        self.fail_observations = fail_observations
        self.fail_conditions = fail_conditions
        self.observation_requests = []
        self.condition_requests = []
        self.facts, self.attempt = facts, attempt

    @staticmethod
    def unavailable():
        body = json.dumps({"type": "urn:nmr-api:problem:service-unavailable", "title": "Unavailable",
                           "status": 503, "instance": "urn:nmr-api:request:fixture",
                           "request_id": "request:fixture", "code": "service_unavailable",
                           "detail": "Retry the request"}).encode()
        return HttpResponse(503, ((b"content-type", b"application/problem+json"),
                                  (b"cache-control", b"no-store"),
                                  (b"nmr-api-topology", b"dev-local"),
                                  (b"x-request-id", b"request:fixture")), body)

    async def retire(self, *, owner=None):
        pass

    async def exchange(self, request, *, maximum_bytes, owner=None):
        path = request.url.path
        if path.endswith("/start"):
            document = {"schema_id": "nmr.provider.execution_attempt_start_response.v1",
                        "execution_attempt_ref": self.attempt, "job_ref": self.facts.job_ref,
                        "analysis_kind_ref": ANALYSIS_KIND_REF, "provider_ref": PROVIDER,
                        "state": "in_progress", "started_at": "2026-10-29T00:00:00Z", "replayed": False}
        elif path.endswith("/progress"):
            command = json.loads(request.content)
            if command["condition_code"] is not None:
                self.condition_requests.append(request)
                if self.fail_conditions:
                    self.fail_conditions -= 1
                    return self.unavailable()
            document = {"schema_id": "nmr.provider.execution_attempt_progress_response.v1",
                        "execution_attempt_ref": self.attempt, "phase": command["phase"],
                        "condition_code": command["condition_code"], "updated_at": "2026-10-29T00:00:00Z"}
        elif request.method == "GET" and path.endswith(self.attempt):
            self.observation_requests.append(request)
            if self.fail_observations:
                self.fail_observations -= 1
                return self.unavailable()
            document = {"schema_id": "nmr.provider.execution_attempt_read_response.v1",
                        "execution_attempt_ref": self.attempt, "job_ref": self.facts.job_ref,
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
        self.preparations = self.executions = self.retirements = 0

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.preparations += 1
        return self.handle

    async def execute(self, prepared, *, report_condition):
        if prepared is not self.handle:
            raise AssertionError("Substituted prepared handle")
        self.executions += 1
        return AnalysisSuccess("secs.elucidation.result.v1", b'{"outcome":"analysed"}',
                               ProtectedEvidence("fixture.evidence.v1", b"observed"))

    async def retire(self):
        self.retirements += 1


class ReportingScience(Science):
    async def prepare(self, acknowledged, instructions, *, report_condition):
        await report_condition("source_waiting")
        return await super().prepare(acknowledged, instructions, report_condition=report_condition)

    async def execute(self, prepared, *, report_condition):
        await report_condition("interpretation_waiting")
        return await super().execute(prepared, report_condition=report_condition)


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
    async def test_settled_failure_is_durable_before_next_job_succeeds(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ipc").mkdir(mode=0o700)
            (root / "queue").mkdir(mode=0o700)
            endpoint = DeliveryEndpoint(ORIGIN, PROVIDER, root / "ipc/delivery.sock")
            limits = QueueLimits(2, 4194304, 2, 4194304)
            queue = DeliveryQueue(root / "queue", limits=limits)
            inbox = SharedDeliveryInbox(queue, SharedDeliverySocket(endpoint.socket_path, None), request_seconds=1,
                                        on_accepted=lambda _outcome: None, on_failure=lambda *_args: None)
            key = Ed25519PrivateKey.generate()
            credential = json.dumps({
                "schema_id": "nmr.provider.private_signing_credential.v1", "profile": "dev-local",
                "principal_ref": PROVIDER, "credential_ref": "credential:test", "algorithm": "ed25519",
                "public_key_spki_der_b64": base64.b64encode(key.public_key().public_bytes(
                    Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode(),
                "private_key_pkcs8_pem": key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
            }, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            fields = {"origin": ORIGIN, "profile": "dev-local", "topology": "dev-local", "provider_ref": PROVIDER,
                      "credential_file": "/fixture/credential", "exchange_seconds": 1, "retirement_seconds": 1}
            _, _, publication = api_settings_with_material(fields, lambda *_args, **_kwargs: credential)
            delivery = SharedDeliveryClient(endpoint, publication, exchange_seconds=1, retirement_seconds=1)
            second = FeedInputFacts("job:second", FACTS.input_schema_id, FACTS.input_fingerprint, FACTS.input_byte_length)
            jobs = (FACTS, second)
            attempts = (ATTEMPT, "execution_attempt:sha256:" + "c" * 64)
            stop = asyncio.Event()
            claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152))
            scope = RecoveryScope(ORIGIN, PROVIDER, "secs", "two-jobs")
            sciences, controllers = [], []

            class FailedScience(Science):
                async def prepare(self, acknowledged, instructions, *, report_condition):
                    self.preparations += 1
                    return AnalysisFailure("interpretation_failed", "fixture failure",
                                           ProtectedEvidence("fixture.evidence.v1", b"observed"))

            class TwoJobs:
                async def jobs(self, **_query):
                    if len(controllers) == len(jobs):
                        stop.set()
                        return SimpleNamespace(jobs=())
                    return SimpleNamespace(jobs=(SimpleNamespace(input=jobs[len(controllers)]),))

            def assemble(pending):
                index = len(controllers)
                if index == 1:
                    self.assertEqual(queue.status(ORIGIN, PROVIDER, attempts[0])["operation"], TerminalOperation.FAIL.value)
                    self.assertTrue(controllers[0].shutdown_ready)
                    self.assertEqual(claims.live_owners(), ())
                science = FailedScience() if index == 0 else Science()
                sciences.append(science)
                identity = SigningIdentity(ORIGIN, "credential:test", key)
                controller = AttemptController.live(
                    ProviderAPI(identity, LifecycleTransport(facts=jobs[index], attempt=attempts[index]), expected_topology="dev-local"),
                    claims, science, scope=scope, stopping=asyncio.Event(), admission_stopping=stop,
                    start_policy=LiveStartPolicy(0.001, lambda *_args: None, lambda *_args: None),
                    preverified_input=VerifiedJobInput(jobs[index], PAYLOAD), delivery=delivery,
                    on_delivery_retry=lambda *_args: None, delivery_retry_initial_seconds=0.01,
                    delivery_retry_max_seconds=0.01, allowed_job_states=frozenset({"open", "closed"}),
                    poll_seconds=0.01, task_settle_seconds=1,
                )
                controllers.append(controller)
                return controller

            try:
                await inbox.start()
                await asyncio.wait_for(run_execution(api=TwoJobs(), scope=scope, claims=claims, stop=stop,
                    poll_seconds=0.01, assemble=assemble, worker=SimpleNamespace(check_ready=AsyncMock()),
                    startup_seconds=1, prose_services=()), 5)
                self.assertEqual([(s.preparations, s.executions, s.retirements) for s in sciences], [(1, 0, 1), (1, 1, 1)])
                self.assertTrue(all(controller.shutdown_ready for controller in controllers))
                self.assertEqual(claims.live_owners(), ())
            finally:
                await delivery.retire()
                await inbox.close()
                queue.close()
            reopened = DeliveryQueue(root / "queue", limits=limits)
            try:
                items = {row["execution_attempt_ref"]: row for row in reopened.inventory(ORIGIN, PROVIDER)}
                self.assertEqual(set(items), set(attempts))
                self.assertEqual(items[attempts[0]]["operation"], TerminalOperation.FAIL.value)
                self.assertEqual(items[attempts[1]]["operation"], TerminalOperation.COMPLETE.value)
                self.assertEqual([items[a]["job_ref"] for a in attempts], [job.job_ref for job in jobs])
            finally:
                reopened.close()

    async def test_observation_and_condition_recover_without_restarting_science(self):
        from secs_inference.provider.main import _api_recovery_policies

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ipc").mkdir(mode=0o700)
            (root / "queue").mkdir(mode=0o700)
            endpoint = DeliveryEndpoint(ORIGIN, PROVIDER, root / "ipc/delivery.sock")
            queue = DeliveryQueue(root / "queue", limits=QueueLimits(1, 2097152, 1, 2097152))
            inbox = DeliveryInbox(queue, (endpoint,), request_seconds=1,
                                  on_accepted=lambda _endpoint: None, on_failure=lambda *_args: None,
                                  binding_status=lambda _endpoint: {})
            delivery = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=1)
            transport = LifecycleTransport(fail_observations=1, fail_conditions=1)
            identity = SigningIdentity(ORIGIN, "credential:test", Ed25519PrivateKey.generate())
            claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152))
            scope = RecoveryScope(ORIGIN, PROVIDER, "secs", "recovery-proof")
            pending = StartPending(scope, ANALYSIS_KIND_REF, FACTS, start_body(FACTS.job_ref, "recovery-proof"))
            science = ReportingScience()
            observation_recovery, condition_recovery = _api_recovery_policies(
                exchange_seconds=0.1, retirement_seconds=0.1, poll_seconds=0.001,
            )
            controller = AttemptController.live(
                ProviderAPI(identity, transport, expected_topology="dev-local"), claims, science,
                scope=scope, stopping=asyncio.Event(), admission_stopping=asyncio.Event(),
                start_policy=LiveStartPolicy(0.001, lambda *_args: None, lambda *_args: None),
                preverified_input=VerifiedJobInput(FACTS, PAYLOAD), delivery=delivery,
                on_delivery_retry=lambda *_args: None, delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.01, allowed_job_states=frozenset({"open", "closed"}),
                poll_seconds=0.001, task_settle_seconds=1,
                observation_recovery=observation_recovery, condition_recovery=condition_recovery,
            )
            try:
                await inbox.start()
                await controller.run(pending)
                self.assertGreaterEqual(len(transport.observation_requests), 2)
                first_observations = transport.observation_requests[:2]
                attempt_path = f"/provider/v1/execution-attempts/{ATTEMPT}"
                self.assertTrue(all(request.url.path == attempt_path for request in first_observations))
                self.assertEqual(len({request.headers["signature-input"] for request in first_observations}), 2)
                condition_requests = [request for request in transport.condition_requests
                                      if json.loads(request.content)["condition_code"] == "source_waiting"]
                self.assertEqual(len(condition_requests), 2)
                self.assertTrue(all(request.url.path == f"{attempt_path}/progress"
                                    for request in condition_requests))
                self.assertEqual(len({request.content for request in condition_requests}), 1)
                self.assertEqual(len({request.headers["signature-input"] for request in condition_requests}), 2)
                self.assertEqual(science.preparations, 1)
                self.assertEqual(science.executions, 1)
                self.assertEqual(science.retirements, 1)
                self.assertEqual(claims.live_owners(), ())
            finally:
                await delivery.retire()
                await inbox.close()
                queue.close()

    def test_recovery_windows_follow_transport_bounds_and_poll_pacing(self):
        from secs_inference.provider.main import _api_recovery_policies

        standard_observation, standard_condition = _api_recovery_policies(
            exchange_seconds=30, retirement_seconds=10, poll_seconds=5,
        )
        self.assertEqual((standard_observation.gap_seconds, standard_condition.window_seconds), (80, 90))
        shorter_observation, shorter_condition = _api_recovery_policies(
            exchange_seconds=8, retirement_seconds=3, poll_seconds=2,
        )
        self.assertEqual((shorter_observation.gap_seconds, shorter_condition.window_seconds), (23, 27))

    async def test_proven_prewrite_deadline_reports_without_worker_write(self):
        await self._assert_proven_prewrite_deadline(fail_retirement=False)

    async def test_prewrite_deadline_preserves_independent_retirement_failure(self):
        await self._assert_proven_prewrite_deadline(fail_retirement=True)

    async def _assert_proven_prewrite_deadline(self, *, fail_retirement):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ipc").mkdir(mode=0o700)
            (root / "queue").mkdir(mode=0o700)
            endpoint = DeliveryEndpoint(ORIGIN, PROVIDER, root / "ipc/delivery.sock")
            queue = DeliveryQueue(root / "queue", limits=QueueLimits(1, 2097152, 1, 2097152))
            inbox = DeliveryInbox(queue, (endpoint,), request_seconds=1,
                                  on_accepted=lambda _endpoint: None, on_failure=lambda *_args: None,
                                  binding_status=lambda _endpoint: {})
            delivery = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=1)
            events = []
            worker = Worker(events)
            cleanup = RuntimeError("fixture retirement failure")
            sources = Sources(events)
            if fail_retirement:
                async def failed_source_retirement(*, readers_stopped):
                    events.append(("sources-retire", readers_stopped))
                    raise cleanup
                sources.retire = failed_source_retirement
            exchange = WorkerClient(root / "ipc/worker.sock", startup_seconds=10, retirement_seconds=10)
            exchange._writer = SimpleNamespace(write=Mock())
            async def request(command, *, deadline, require_execution):
                return await exchange._exchange(command, monotonic() - 1, require_execution)
            worker.request = request
            session = SecsSession(worker=worker, sources=sources, endpoint_factories=(lambda **_: Carrier(events, [choice()]),),
                                  work_seconds=3, interpretation_seconds=1, startup_seconds=1, context_bytes=1000000)
            claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152))
            scope = RecoveryScope(ORIGIN, PROVIDER, "secs", "runtime-one")
            pending = StartPending(scope, ANALYSIS_KIND_REF, FACTS, start_body(FACTS.job_ref, "request-deadline"))
            identity = SigningIdentity(ORIGIN, "credential:test", Ed25519PrivateKey.generate())
            controller = AttemptController.live(
                ProviderAPI(identity, LifecycleTransport(), expected_topology="dev-local"), claims, session,
                scope=scope, stopping=asyncio.Event(), admission_stopping=asyncio.Event(),
                start_policy=LiveStartPolicy(0.001, lambda *_args: None, lambda *_args: None),
                preverified_input=VerifiedJobInput(FACTS, PAYLOAD), delivery=delivery,
                on_delivery_retry=lambda *_args: None, delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.01, allowed_job_states=frozenset({"open", "closed"}),
                poll_seconds=0.01, task_settle_seconds=1,
            )
            try:
                await inbox.start()
                if fail_retirement:
                    with self.assertRaises(ControllerFailed) as caught:
                        await controller.run(pending)
                    def contains(error):
                        return error is cleanup or (isinstance(error, BaseExceptionGroup)
                                                    and any(contains(child) for child in error.exceptions))
                    execution = caught.exception.cause
                    self.assertTrue(any(contains(error) for error in (execution.cause, *execution.cleanup_failures)))
                    self.assertIn(controller, claims.live_owners())
                else:
                    await controller.run(pending)
                exchange._writer.write.assert_not_called()
                self.assertEqual(queue.next_pending(ORIGIN, PROVIDER).operation, TerminalOperation.FAIL)
                self.assertFalse(worker.stopped)
                self.assertIn(("sources-retire", True), events)
            finally:
                await delivery.retire()
                await inbox.close()
                queue.close()

    async def test_controller_stop_before_worker_dispatch_blocks_first_execution(self):
        await self._assert_stop_before_worker_dispatch("execute")

    async def test_controller_stop_before_worker_dispatch_blocks_preparation(self):
        await self._assert_stop_before_worker_dispatch("prepare")

    async def _assert_stop_before_worker_dispatch(self, operation):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for part in ("ipc", "queue", "scratch", "status"):
                (root / part).mkdir(mode=0o700)
            worker_endpoint = root / "ipc/worker.sock"
            (root / "ipc/.owner-session.lock").touch(mode=0o600)
            fixture = Path(__file__).resolve().parents[1] / "integration/native_worker_fixture.py"
            owner = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "provider_core.runner", "--role", "secs",
                "--socket", str(worker_endpoint), "--scratch", str(root / "scratch"),
                "--status-directory", str(root / "status"), "--owner-session-lifetime",
                "--startup-seconds", "2", "--terminate-seconds", "2", "--kill-seconds", "2",
                "--cleanup-seconds", "2", "--", sys.executable, "-I", str(fixture),
                "--mode", "unconfirmed_execute", "--pid-file", str(root / "pid"),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            endpoint = DeliveryEndpoint(ORIGIN, PROVIDER, root / "ipc/delivery.sock")
            queue = DeliveryQueue(root / "queue", limits=QueueLimits(1, 2097152, 1, 2097152))
            inbox = DeliveryInbox(queue, (endpoint,), request_seconds=1,
                                  on_accepted=lambda _endpoint: None, on_failure=lambda *_args: None,
                                  binding_status=lambda _endpoint: {})
            delivery = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=1)
            entered, release = asyncio.Event(), asyncio.Event()
            events = []
            class PausedWorker(WorkerClient):
                async def _exchange(self, command, deadline, require_execution):
                    if command["operation"] == operation:
                        entered.set()
                        await release.wait()
                    return await super()._exchange(command, deadline, require_execution)
            worker = PausedWorker(worker_endpoint, startup_seconds=10, retirement_seconds=10)
            session = SecsSession(worker=worker, sources=Sources(events), endpoint_factories=(lambda **_: Carrier(events, [choice()]),),
                                  work_seconds=3, interpretation_seconds=1, startup_seconds=10, context_bytes=1000000)
            claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152))
            scope = RecoveryScope(ORIGIN, PROVIDER, "secs", "runtime-one")
            pending = StartPending(scope, ANALYSIS_KIND_REF, FACTS, start_body(FACTS.job_ref, "request-stop"))
            identity = SigningIdentity(ORIGIN, "credential:test", Ed25519PrivateKey.generate())
            controller = AttemptController.live(
                ProviderAPI(identity, LifecycleTransport(), expected_topology="dev-local"), claims, session,
                scope=scope, stopping=asyncio.Event(), admission_stopping=asyncio.Event(),
                start_policy=LiveStartPolicy(0.001, lambda *_args: None, lambda *_args: None),
                preverified_input=VerifiedJobInput(FACTS, PAYLOAD), delivery=delivery,
                on_delivery_retry=lambda *_args: None, delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.01, allowed_job_states=frozenset({"open", "closed"}),
                poll_seconds=1, task_settle_seconds=15,
            )
            task = None
            try:
                await inbox.start()
                task = asyncio.create_task(controller.run(pending))
                await asyncio.wait_for(entered.wait(), 5)
                controller.request_stop()
                release.set()
                result, = await asyncio.gather(task, return_exceptions=True)
                marker = "pid.entered" if operation == "execute" else "pid.prepare_entered"
                self.assertFalse((root / marker).exists(), f"The child entered {operation} after stop")
                self.assertIs(type(result), ControllerFailed)
                self.assertIs(type(result.cause.cause), ExecutionStopRequested)
                self.assertTrue(worker.stopped)
                self.assertIsNone(queue.next_pending(ORIGIN, PROVIDER))
            finally:
                release.set()
                if task is not None and not task.done():
                    controller.request_stop()
                    await asyncio.gather(task, return_exceptions=True)
                try:
                    await delivery.retire()
                    await inbox.close()
                    queue.close()
                finally:
                    if owner.returncode is None:
                        owner.terminate()
                    await asyncio.wait_for(owner.wait(), 20)

    async def test_real_dispatched_timeout_keeps_claim_without_terminal_handoff(self):
        await self._assert_real_timeout_keeps_claim("execute")

    async def test_inspection_deadline_keeps_claim_without_terminal_handoff(self):
        await self._assert_real_timeout_keeps_claim("inspect")

    async def _assert_real_timeout_keeps_claim(self, phase):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for part in ("ipc", "queue", "scratch", "status"):
                (root / part).mkdir(mode=0o700)
            worker_endpoint = root / "ipc/worker.sock"
            (root / "ipc/.owner-session.lock").touch(mode=0o600)
            fixture = Path(__file__).resolve().parents[1] / "integration/native_worker_fixture.py"
            owner = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "provider_core.runner", "--role", "secs",
                "--socket", str(worker_endpoint), "--scratch", str(root / "scratch"),
                "--status-directory", str(root / "status"), "--owner-session-lifetime",
                "--startup-seconds", "2", "--terminate-seconds", "2", "--kill-seconds", "2",
                "--cleanup-seconds", "2", "--", sys.executable, "-I", str(fixture),
                "--mode", "unconfirmed_execute" if phase == "execute" else "unconfirmed_inspect",
                "--pid-file", str(root / "pid"),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            endpoint = DeliveryEndpoint(ORIGIN, PROVIDER, root / "ipc/delivery.sock")
            queue = DeliveryQueue(root / "queue", limits=QueueLimits(1, 2097152, 1, 2097152))
            inbox = DeliveryInbox(queue, (endpoint,), request_seconds=1,
                                  on_accepted=lambda _endpoint: None, on_failure=lambda *_args: None,
                                  binding_status=lambda _endpoint: {})
            delivery = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=1)
            events = []
            worker = WorkerClient(worker_endpoint, startup_seconds=10, retirement_seconds=10)
            turn = ([choice()] if phase == "execute" else [call("inspect_source", {
                "source": {"upload_ref": "upload:chosen", "member": None}}, "inspect")])
            session = SecsSession(worker=worker, sources=Sources(events), endpoint_factories=(lambda **_: Carrier(events, turn),),
                                  work_seconds=1 if phase == "execute" else 3,
                                  interpretation_seconds=1 if phase == "execute" else 0.2,
                                  startup_seconds=10, context_bytes=1000000)
            claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152))
            scope = RecoveryScope(ORIGIN, PROVIDER, "secs", "runtime-one")
            pending = StartPending(scope, ANALYSIS_KIND_REF, FACTS, start_body(FACTS.job_ref, "request-held"))
            identity = SigningIdentity(ORIGIN, "credential:test", Ed25519PrivateKey.generate())
            controller = AttemptController.live(
                ProviderAPI(identity, LifecycleTransport(), expected_topology="dev-local"), claims, session,
                scope=scope, stopping=asyncio.Event(), admission_stopping=asyncio.Event(),
                start_policy=LiveStartPolicy(0.001, lambda *_args: None, lambda *_args: None),
                preverified_input=VerifiedJobInput(FACTS, PAYLOAD), delivery=delivery,
                on_delivery_retry=lambda *_args: None, delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.01, allowed_job_states=frozenset({"open", "closed"}),
                poll_seconds=0.01, task_settle_seconds=15,
            )
            try:
                await inbox.start()
                try:
                    await controller.run(pending)
                except ControllerFailed as failure:
                    from provider_core.analysis import AnalysisOutcomeUnresolved
                    reason = failure.cause.cause
                    self.assertIs(type(reason), AnalysisOutcomeUnresolved)
                    self.assertIs(type(reason.__cause__), TimeoutError)
                    self.assertIs(controller.require_held_failure(pending, failure), failure)
                else:
                    marker = "pid.entered" if phase == "execute" else "pid.inspect_entered"
                    self.assertTrue((root / marker).exists(), "The child did not enter the operation")
                    self.assertIsNone(queue.next_pending(ORIGIN, PROVIDER), "Unknown result entered terminal delivery")
                    self.fail("Unknown result did not retain a controller failure")
                marker = "pid.entered" if phase == "execute" else "pid.inspect_entered"
                self.assertTrue((root / marker).exists(), "The child did not enter the operation")
                self.assertIsNone(queue.next_pending(ORIGIN, PROVIDER))
                self.assertIn(controller, claims.live_owners())
                self.assertTrue(worker.stopped)
                self.assertIn(("sources-retire", True), events)
            finally:
                try:
                    await delivery.retire()
                    await inbox.close()
                    queue.close()
                finally:
                    if owner.returncode is None:
                        owner.terminate()
                    await asyncio.wait_for(owner.wait(), 20)

    async def test_unknown_science_keeps_claim_without_terminal_handoff(self):
        await self._assert_worker_failure_keeps_claim("execute")

    async def test_unknown_preparation_keeps_claim_without_terminal_handoff(self):
        await self._assert_worker_failure_keeps_claim("prepare")

    async def test_unknown_preparation_with_failed_retirement_stays_held(self):
        await self._assert_worker_failure_keeps_claim("prepare", fail_retirement=True)

    async def test_scientific_operator_failures_keep_original_cause_without_terminal_handoff(self):
        for phase in ("prepare", "execute"):
            for fail_retirement in (False, True):
                with self.subTest(phase=phase, fail_retirement=fail_retirement):
                    frame = {"outcome": "failed", "exception_id": 0, "exception_type": "RuntimeError",
                             "frames": [], "private_detail": "PRIVATE scientific frame"}
                    original = ScientificWorkerFailed(frame)
                    await self._assert_worker_failure_keeps_claim(
                        phase, fail_retirement=fail_retirement, scientific_failure=original,
                    )

    async def _assert_worker_failure_keeps_claim(self, phase, *, fail_retirement=False, scientific_failure=None):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ipc").mkdir(mode=0o700)
            (root / "queue").mkdir(mode=0o700)
            endpoint = DeliveryEndpoint(ORIGIN, PROVIDER, root / "ipc/delivery.sock")
            queue = DeliveryQueue(root / "queue", limits=QueueLimits(1, 2097152, 1, 2097152))
            inbox = DeliveryInbox(queue, (endpoint,), request_seconds=1,
                                  on_accepted=lambda _endpoint: None, on_failure=lambda *_args: None,
                                  binding_status=lambda _endpoint: {})
            await inbox.start()
            delivery = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=1)
            events = []
            response = scientific_failure if scientific_failure is not None else {"outcome": "unexpected"}
            if scientific_failure is not None:
                original_frame = scientific_failure.diagnostic
            replies = ([{"outcome": "prepared", "prepared_handle": 7}, response]
                       if phase == "execute" else [response])
            worker = Worker(events, *replies)
            cleanup = RuntimeError("fixture retirement failed")
            if fail_retirement:
                worker.stopping_failure = cleanup
            session = SecsSession(worker=worker, sources=Sources(events), endpoint_factories=(lambda **_: Carrier(events, [choice()]),),
                                  work_seconds=3, interpretation_seconds=1, startup_seconds=1, context_bytes=1000000)
            claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152))
            scope = RecoveryScope(ORIGIN, PROVIDER, "secs", "runtime-one")
            pending = StartPending(scope, ANALYSIS_KIND_REF, FACTS, start_body(FACTS.job_ref, "request-held"))
            identity = SigningIdentity(ORIGIN, "credential:test", Ed25519PrivateKey.generate())
            controller = AttemptController.live(
                ProviderAPI(identity, LifecycleTransport(), expected_topology="dev-local"), claims, session,
                scope=scope, stopping=asyncio.Event(), admission_stopping=asyncio.Event(),
                start_policy=LiveStartPolicy(0.001, lambda *_args: None, lambda *_args: None),
                preverified_input=VerifiedJobInput(FACTS, PAYLOAD), delivery=delivery,
                on_delivery_retry=lambda *_args: None, delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.01, allowed_job_states=frozenset({"open", "closed"}),
                poll_seconds=0.01, task_settle_seconds=1,
            )
            try:
                try:
                    await controller.run(pending)
                except ControllerFailed as failure:
                    from provider_core.analysis import AnalysisOutcomeUnresolved
                    if scientific_failure is None:
                        self.assertIs(AnalysisOutcomeUnresolved, type(failure.cause.cause))
                        if not fail_retirement:
                            self.assertIs(failure, controller.require_held_failure(pending, failure))
                    else:
                        self.assertIs(scientific_failure, failure.cause.cause)
                        self.assertIs(original_frame, failure.cause.cause.diagnostic)
                        self.assertNotIn("PRIVATE", str(failure))
                    if fail_retirement:
                        def contains(error):
                            return error is cleanup or (isinstance(error, BaseExceptionGroup)
                                                        and any(contains(child) for child in error.exceptions))
                        self.assertTrue(any(contains(error) for error in failure.cause.cleanup_failures))
                else:
                    self.assertIsNone(queue.next_pending(ORIGIN, PROVIDER), "Unknown result entered terminal delivery")
                    self.fail("Unknown result did not retain a controller failure")
                self.assertIsNone(queue.next_pending(ORIGIN, PROVIDER))
                self.assertIn(controller, claims.live_owners())
                expected_commands = ["prepare", "execute"] if phase == "execute" else ["prepare"]
                self.assertEqual(expected_commands, [command["operation"] for command in worker.commands])
                self.assertEqual(not fail_retirement, worker.stopped)
                self.assertIn(("sources-retire", not fail_retirement), events)
            finally:
                await delivery.retire()
                await inbox.close()
                queue.close()

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
            inbox = SharedDeliveryInbox(queue, SharedDeliverySocket(endpoint.socket_path, None), request_seconds=1,
                                        on_accepted=lambda _outcome: None, on_failure=lambda *_args: None)
            await inbox.start()
            key = Ed25519PrivateKey.generate()
            credential = json.dumps({
                "schema_id": "nmr.provider.private_signing_credential.v1", "profile": "dev-local",
                "principal_ref": PROVIDER, "credential_ref": "credential:test", "algorithm": "ed25519",
                "public_key_spki_der_b64": base64.b64encode(key.public_key().public_bytes(
                    Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode(),
                "private_key_pkcs8_pem": key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
            }, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            fields = {"origin": ORIGIN, "profile": "dev-local", "topology": "dev-local", "provider_ref": PROVIDER,
                      "credential_file": "/fixture/credential", "exchange_seconds": 1, "retirement_seconds": 1}
            _, _, publication = api_settings_with_material(fields, lambda *_args, **_kwargs: credential)
            client = SharedDeliveryClient(endpoint, publication, exchange_seconds=1, retirement_seconds=1)
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
                                   config=config, scope=scope, claims=claims, stop=admission_stop, assemble=assemble,
                                   worker=worker, prose_services=())
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
                retained = reopened.next_pending_handoff(ORIGIN, PROVIDER)
                self.assertEqual(retained.outcome.body, delivery.items[0].body)
                self.assertEqual(retained.outcome.body_sha256, delivery.items[0].body_sha256)
                self.assertEqual(retained.publication.credential_bytes, credential)
            finally:
                reopened.close()

    async def test_hello_sibling_failure_drains_the_original_selected_report(self):
        await self.exercise_failure("hello")

    async def test_outer_service_failure_drains_the_original_selected_report(self):
        await self.exercise_failure("outer")
