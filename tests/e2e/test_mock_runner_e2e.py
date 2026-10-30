"""Exercise the no-science runner through the native owner and shared delivery."""

import asyncio
from base64 import b64decode, b64encode
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import ssl
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from provider_core.api import ProviderAPI
from provider_core.api_config import api_settings_with_material
from provider_core.attempt_record import RecoveryScope
from provider_core.controller import AttemptController, LiveStartPolicy
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.shared_delivery_ipc import SharedDeliveryInbox, SharedDeliverySocket
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from secs_inference.provider.config import ExecutionConfig, HelloPolicy, ProviderConfig
from secs_inference.provider.mock_runner_session import MockRunnerSession
from secs_inference.provider.runtime import run_services
from secs_inference.provider.worker import WorkerClient
from test_provider_e2e import WireScenario
from tls_fixture import _write_test_certificates


class MockRunnerEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_result_crosses_native_owner_and_shared_delivery_after_lost_ack(self):
        wire = WireScenario(b"unused upload")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_test_certificates(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), wire.handler())
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root / "server.pem", root / "server-key.pem")
            server.socket = context.wrap_socket(server.socket, server_side=True)
            wire.origin = f"https://localhost:{server.server_port}"
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            apis = []
            worker = inbox = queue = None
            try:
                trust = ssl.create_default_context(cafile=str(root / "ca.pem"))
                def transport():
                    return HttpTransport(trust=trust, exchange_seconds=10, retirement_seconds=2)
                key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
                identity = SigningIdentity(wire.origin, "credential:test", key)
                credential = json.dumps({"schema_id": "nmr.provider.private_signing_credential.v1",
                    "profile": "dev-local", "principal_ref": "provider:test", "credential_ref": "credential:test",
                    "algorithm": "ed25519", "public_key_spki_der_b64": b64encode(key.public_key().public_bytes(
                        Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode(),
                    "private_key_pkcs8_pem": key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()},
                    sort_keys=True, separators=(",", ":")).encode() + b"\n"
                ca_bytes = (root / "ca.pem").read_bytes()
                api_fields = {"origin": wire.origin, "profile": "dev-local", "topology": "dev-local",
                              "provider_ref": "provider:test", "credential_file": "/credential", "ca_file": "/ca",
                              "exchange_seconds": 10, "retirement_seconds": 2}
                _, _, publication = api_settings_with_material(api_fields,
                    lambda _path, **options: credential if options["label"] == "API signing credential" else ca_bytes)
                apis = []
                def new_api():
                    api = ProviderAPI(identity, transport(), expected_topology="dev-local")
                    apis.append(api)
                    return api
                stop, science_stop = asyncio.Event(), asyncio.Event()
                ipc, storage = root / "ipc", root / "queue"
                ipc.mkdir(mode=0o700)
                storage.mkdir(mode=0o700)
                endpoint = DeliveryEndpoint(wire.origin, "provider:test", ipc / "delivery.sock")
                limits = QueueLimits(1, 2097152, 1, 2097152)
                queue = DeliveryQueue(storage, limits=limits)
                inbox = SharedDeliveryInbox(queue, SharedDeliverySocket(endpoint.socket_path, None), request_seconds=2,
                                            on_accepted=lambda _outcome: stop.set(), on_failure=lambda *_args: None)
                await inbox.start()
                acknowledgements = []
                respond = inbox._respond
                async def lose_first_ack(connection, writer, status, document):
                    acknowledgements.append(document)
                    if len(acknowledgements) == 1:
                        writer.close()
                        return
                    await respond(connection, writer, status, document)
                claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152))
                scope = RecoveryScope(wire.origin, "provider:test", "secs", "scientific-e2e")
                worker = WorkerClient(Path("/run/secs/worker/worker.sock"), startup_seconds=300,
                                      retirement_seconds=10, observation_seconds=2)
                execution = ExecutionConfig(wire.origin + "/chat", "scripted-choice", wire.origin,
                                            work_seconds=120, interpretation_seconds=60, worker_startup_seconds=300,
                                            poll_seconds=0.1, max_turns=6)
                config = ProviderConfig("fixture", 1234, {}, 2, 0.1, 1,
                                        HelloPolicy("SECS test", "Local workflow proof", 3600, 1, mock_runner=True), execution)
                controllers = []
                def assemble(pending):
                    science = MockRunnerSession(worker=worker, work_seconds=30, startup_seconds=30)
                    client = SharedDeliveryClient(endpoint, publication, exchange_seconds=2, retirement_seconds=2)
                    controller = AttemptController.live(
                        new_api(), claims, science, scope=scope, stopping=science_stop, admission_stopping=stop,
                        start_policy=LiveStartPolicy(0.1, lambda *_args: None, lambda *_args: None), delivery=client,
                        on_delivery_retry=lambda *_args: None, delivery_retry_initial_seconds=0.01,
                        delivery_retry_max_seconds=0.1, allowed_job_states=frozenset({"open", "closed"}),
                        poll_seconds=0.1, task_settle_seconds=10)
                    controllers.append(controller)
                    return controller
                with patch.object(inbox, "_respond", side_effect=lose_first_ack):
                    await run_services(hello_api=new_api(), discovery_api=new_api(), provider_ref="provider:test",
                                       config=config, scope=scope, claims=claims, stop=stop, assemble=assemble, worker=worker)
                self.assertEqual(len(controllers), 1)
                self.assertTrue(controllers[0].shutdown_ready)
                self.assertFalse(claims.live_owners())
                self.assertFalse(Path("/run/secs/sources/current").exists())
                retained = queue.next_pending(wire.origin, "provider:test")
                self.assertEqual(wire.starts, 1)
                self.assertEqual(wire.turns, 0)
                self.assertEqual(wire.downloads, 0)
                self.assertEqual(wire.capabilities, 0)
                self.assertEqual(len(acknowledgements), 2)
                self.assertEqual(acknowledgements[0], acknowledgements[1])
                self.assertIsNotNone(retained)
                handoff = queue.next_pending_handoff(wire.origin, "provider:test")
                self.assertEqual(handoff.publication.credential_bytes, credential)
                self.assertEqual(handoff.publication.ca_bytes, ca_bytes)
                report = json.loads(b64decode(json.loads(retained.body)["canonical_result_base64"]))
                self.assertTrue(report["mock_runner"])
                self.assertIn("no model", report["message"].lower())
                self.assertNotIn(b"model-key", retained.body)
                self.assertNotIn(b"store-bearer", retained.body)
                self.assertTrue(wire.provider_signed and all(wire.provider_signed))
            except BaseException as failure:
                from provider_core.diagnostics import failure_evidence
                print(json.dumps(failure_evidence(failure, secrets=("model-key", "store-bearer"))), flush=True)
                raise
            finally:
                failures = []
                asynchronous = (() if worker is None else (worker.stop,)) + tuple(api.retire for api in apis)
                if inbox is not None:
                    asynchronous += (inbox.close,)
                for operation in asynchronous:
                    try:
                        await operation()
                    except BaseException as failure:
                        failures.append(failure)
                synchronous = (() if queue is None else (queue.close,)) + (server.shutdown, server.server_close, thread.join)
                for operation in synchronous:
                    try:
                        operation()
                    except BaseException as failure:
                        failures.append(failure)
                if failures:
                    raise BaseExceptionGroup("Scientific fixture cleanup failed", failures)
            reopened = DeliveryQueue(storage, limits=limits)
            try:
                self.assertEqual(reopened.next_pending(wire.origin, "provider:test").body, retained.body)
                reopened_handoff = reopened.next_pending_handoff(wire.origin, "provider:test")
                self.assertEqual(reopened_handoff.publication.credential_bytes, credential)
                self.assertEqual(reopened_handoff.publication.ca_bytes, ca_bytes)
            finally:
                reopened.close()

