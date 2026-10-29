"""Real checkpoint science crosses shared native, HTTPS and durable IPC owners.

Eight candidate molecules and one GA generation establish composition, not
full-index quality or live interpreter behavior. A dropped local acknowledgement
after SQLite acceptance must replay the same report without repeating science.
"""

import asyncio
from base64 import b64encode, b64decode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import socket
import ssl
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from provider_core.api import ProviderAPI
from provider_core.attempt_record import RecoveryScope
from provider_core.chat import ChatEndpoint, ChatEndpointSpec
from provider_core.controller import AttemptController, LiveStartPolicy
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.config import ExecutionConfig, HelloPolicy, ProviderConfig
from secs_inference.provider.runtime import run_services
from secs_inference.provider.session import SecsSession
from secs_inference.provider.sources import AttemptSources
from secs_inference.provider.worker import WorkerClient
from tls_fixture import _write_test_certificates

UPLOAD = "upload:sha256:" + "a" * 64
DISTRACTOR = "upload:sha256:" + "0" * 64
ATTEMPT = "execution_attempt:sha256:" + "b" * 64
TEXT = b"Use the proton spectrum and formula C7H8ClN. The archive has several experiments."


class ProviderEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_upload_real_search_and_exact_durable_handoff_after_lost_ack(self):
        raw = Path("/fixtures/jcamp/4-chlorobenzylamine/4-chlorobenzylamine.jdx").read_bytes()
        archive = BytesIO()
        with ZipFile(archive, "w") as output:
            output.writestr("experiment1/carbon.jdx", raw.replace(b"^1H", b"^13C"))
            output.writestr("experiment2/proton.jdx", raw)
        wire = WireScenario(archive.getvalue())
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
                identity = SigningIdentity(wire.origin, "credential:test", Ed25519PrivateKey.from_private_bytes(bytes(range(32))))
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
                inbox = DeliveryInbox(queue, (endpoint,), request_seconds=2,
                                      on_accepted=lambda _endpoint: stop.set(), on_failure=lambda *_args: None,
                                      binding_status=lambda _endpoint: {})
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
                config = ProviderConfig(root / "unused-definition", "fixture",
                                        HelloPolicy("SECS test", "Local workflow proof", 3600, 1), execution)
                controllers = []
                def assemble(pending):
                    sources = AttemptSources(job_ref=pending.input.job_ref, api=new_api(), store=transport(),
                                             store_origin=wire.origin, directory=Path("/run/secs/sources/current"),
                                             max_upload_bytes=execution.max_upload_bytes,
                                             max_total_bytes=execution.max_total_bytes, retirement_seconds=2)
                    spec = ChatEndpointSpec(configuration_id="e2e", model="scripted-choice", url=wire.origin + "/chat",
                                            api_key="model-key", reasoning_effort=None)
                    chat = ChatEndpoint(spec, transport=transport(), request_bytes=2097152, response_bytes=262144)
                    science = SecsSession(worker=worker, sources=sources, chat=chat, work_seconds=120,
                                          interpretation_seconds=60, startup_seconds=300, max_turns=6, secrets=("model-key",))
                    client = DeliveryClient(endpoint, exchange_seconds=2, retirement_seconds=2)
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
                self.assertEqual(wire.turns, 3)
                self.assertEqual(wire.downloads, 1)
                self.assertEqual(wire.capabilities, 1)
                self.assertEqual(wire.hellos, 1)
                self.assertEqual(len(acknowledgements), 2)
                self.assertEqual(acknowledgements[0], acknowledgements[1])
                retained = queue.next_pending(wire.origin, "provider:test")
                self.assertIsNotNone(retained)
                self.assertEqual(retained.body_sha256, acknowledgements[1]["body_sha256"])
                report = json.loads(b64decode(json.loads(retained.body)["canonical_result_base64"]))
                self.assertEqual(report["outcome"], "analysed")
                self.assertEqual(len(report["input_choices"]), 2)
                self.assertIn("selected 13C spectrum", report["input_choices"][0]["reading_error"])
                self.assertIn("proton", report["input_choices"][-1]["explanation"])
                self.assertEqual(report["analysis"]["preparation"]["sources"], [{"upload_ref": UPLOAD, "member": "experiment2/proton.jdx"}])
                self.assertEqual(report["acquired_uploads"][UPLOAD]["content_hash"], "sha256:" + sha256(wire.archive).hexdigest())
                self.assertTrue(report["analysis"]["candidates"])
                retrieval = report["analysis"]["retrieval"]
                self.assertEqual(retrieval["index_size"], 8)
                self.assertEqual(retrieval["neighbours_returned"], 8)
                self.assertGreater(retrieval["starting_candidates"], 0)
                self.assertEqual(retrieval["starting_candidates"], retrieval["formula_matches"])
                self.assertEqual(report["analysis"]["search"]["generations"], 1)
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
            finally:
                reopened.close()


class WireScenario:
    def __init__(self, archive):
        self.archive = archive
        self.downloads = self.capabilities = self.turns = 0
        self.provider_signed = []
        self.starts = self.hellos = 0

    def handler(self):
        scenario = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.handle_operation()
            def do_POST(self):
                self.handle_operation()
            def do_PUT(self):
                self.handle_operation()
            def log_message(self, *args):
                pass
            def handle_operation(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path.startswith("/provider/"):
                    scenario.provider_signed.append(bool(self.headers.get("Signature")))
                response, media = scenario.reply(self.command, self.path, body, self.headers)
                if response is None:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", media)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Nmr-Api-Topology", "dev-local")
                self.send_header("Content-Length", str(len(response)))
                if media == "application/octet-stream":
                    self.send_header("Content-Digest", "sha-256=:" + b64encode(sha256(response).digest()).decode() + ":")
                self.end_headers()
                self.wfile.write(response)
        return Handler

    def reply(self, method, path, body, headers):
        route = (method, path)
        if method == "GET":
            assert not body
        if route == ("POST", "/chat"):
            assert headers["Authorization"] == "Bearer model-key"
            prompt = json.loads(body)
            self.turns += 1
            if self.turns == 1:
                assert "irrelevant carbon attachment" in prompt["messages"][1]["content"]
                name, arguments = "inspect_source", {"source": {"upload_ref": UPLOAD, "member": None}}
            else:
                if self.turns == 3:
                    assert "13C" in prompt["messages"][-1]["content"]
                    assert "requires 1H" in prompt["messages"][-1]["content"]
                member = "experiment1/carbon.jdx" if self.turns == 2 else "experiment2/proton.jdx"
                inspection = next(json.loads(message["content"])["representations"]
                                  for message in prompt["messages"] if message["role"] == "tool"
                                  and "representations" in message["content"])
                identity = next(item["id"] for item in inspection
                                if item["sources"] == [{"upload_ref": UPLOAD, "member": member}])
                name, arguments = "select_representation", {
                    "representation_id": identity, "formula": "C7H8ClN",
                    "formula_evidence": {"kind": "job_specification", "quote": "C7H8ClN"}, "processing": "as_stored",
                    "explanation": "The selected experiment provides the proton spectrum for the supplied formula.",
                }
            document = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [{"id": f"call-{self.turns}", "type": "function",
                         "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}
        elif route == ("POST", "/provider/v1/hello"):
            assert json.loads(body)["schema_id"] == "nmr.provider.hello_request.v1"
            self.hellos += 1
            document = {"schema_id": "nmr.provider.hello_response.v1", "provider_ref": "provider:test", "accepted_at": "2026-09-08T12:00:00Z"}
        elif route == ("GET", "/upload/v1/uploads/" + UPLOAD + "/bytes"):
            assert headers["Authorization"] == "Bearer store-bearer"
            assert "Signature" not in headers
            self.downloads += 1
            return self.archive, "application/octet-stream"
        elif method == "GET" and path.startswith("/provider/v1/jobs?"):
            assert "analysis_kind_ref=" + ANALYSIS_KIND_REF in path
            assert "has_provider_execution_attempt=false" in path
            document = {"schema_id": "nmr.provider.jobs.list.response.v1", "analysis_kind_ref": ANALYSIS_KIND_REF,
                        "has_provider_execution_attempt": False, "next_cursor": None, "jobs": [{"job_ref": "job:test", "analysis_kind_ref": ANALYSIS_KIND_REF,
                        "input_schema_id": "nmr.job.specification.text.v1", "input_fingerprint": "sha256:" + sha256(TEXT).hexdigest(), "input_byte_length": len(TEXT),
                        "created_at": "2026-09-08T12:00:00Z"}]}
        elif route == ("POST", "/provider/v1/execution-attempts/start"):
            command = json.loads(body)
            assert command["schema_id"] == "nmr.provider.execution_attempt_start_request.v1"
            assert command["job_ref"] == "job:test"
            self.start_key = command["provider_attempt_key"]
            self.starts += 1
            document = {"schema_id": "nmr.provider.execution_attempt_start_response.v1", "execution_attempt_ref": ATTEMPT, "job_ref": "job:test",
                        "provider_ref": "provider:test", "analysis_kind_ref": ANALYSIS_KIND_REF, "state": "in_progress", "replayed": False, "started_at": "2026-09-08T12:00:00Z"}
        elif route == ("GET", "/provider/v1/jobs/job:test/input?analysis_kind_ref=" + ANALYSIS_KIND_REF):
            document = {"schema_id": "nmr.provider.job_input.read.response.v1", "job_ref": "job:test", "canonical_input_base64": b64encode(TEXT).decode(),
                        "input_schema_id": "nmr.job.specification.text.v1", "input_byte_length": len(TEXT), "input_fingerprint": "sha256:" + sha256(TEXT).hexdigest()}
        elif route == ("GET", "/provider/v1/jobs/job:test/uploads"):
            document = {"schema_id": "nmr.provider.job_upload_set.read.response.v1", "job_ref": "job:test", "uploads": [
                {"upload_ref": DISTRACTOR, "description": "irrelevant carbon attachment", "byte_length": 4, "content_hash": None},
                {"upload_ref": UPLOAD, "description": "Multiple experiments; find the proton spectrum.", "byte_length": len(self.archive), "content_hash": None}]}
        elif route == ("POST", "/provider/v1/jobs/job:test/uploads/" + UPLOAD + "/read-capability"):
            assert not body
            self.capabilities += 1
            document = {"schema_id": "nmr.upload.read_capability.response.v1", "upload_ref": UPLOAD, "method": "GET", "download_url": self.origin + "/upload/v1/uploads/" + UPLOAD + "/bytes",
                        "byte_length": len(self.archive), "content_hash": "sha256:" + sha256(self.archive).hexdigest(), "expires_at": "2030-01-01T00:00:00Z", "capability": "store-bearer"}
        elif route == ("PUT", "/provider/v1/execution-attempts/" + ATTEMPT + "/progress"):
            command = json.loads(body)
            document = {"schema_id": "nmr.provider.execution_attempt_progress_response.v1",
                        "execution_attempt_ref": ATTEMPT, "phase": command["phase"],
                        "condition_code": command["condition_code"], "updated_at": "2026-10-29T00:00:00Z"}
        elif route == ("GET", "/provider/v1/execution-attempts/" + ATTEMPT):
            document = {"schema_id": "nmr.provider.execution_attempt_read_response.v1", "execution_attempt_ref": ATTEMPT,
                        "job_ref": "job:test", "job_state": "closed",
                        "state": "in_progress"}
        else:
            raise AssertionError(f"Unexpected test request: {method} {path}")
        return json.dumps(document).encode(), "application/json"
