"""Real HTTPS and durable replay, with separate scientific and failure proofs.

API and Chat peers are deterministic fixtures. The scientific child runs in a
separate networkless container with real checkpoint weights and eight candidate
molecules. This proves the workflow, not full-index quality or live LLM choice.
The failure-only lane uses a lightweight supervised child without scientific dependencies.
"""

from base64 import b64encode, b64decode
from contextlib import ExitStack, contextmanager
from functools import partial
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import errno
import os
from pathlib import Path
import socket
import shutil
import ssl
from tempfile import TemporaryDirectory
from threading import Event, Thread, Timer
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.api import ProviderApi
from secs_inference.provider.attempt_state import ActiveAttempt, StartPending
from secs_inference.provider.chat import ChatEndpoint
from secs_inference.provider.config import ExecutionConfig, HelloPolicy, ProviderConfig
from secs_inference.provider.http import HttpsEndpoint
from secs_inference.provider.job_api import AttemptSnapshot, JobApi
from secs_inference.provider.job_input import SelectedJobInput
from secs_inference.provider.main import prepare_configured_hello
from secs_inference.provider.runtime import run_services
from secs_inference.provider.upload_download import UploadStore
from secs_inference.provider.worker import _serve_session
from tls_fixture import _write_test_certificates


UPLOAD = "upload:sha256:" + "a" * 64
DISTRACTOR = "upload:sha256:" + "0" * 64
ATTEMPT = "execution_attempt:sha256:" + "b" * 64
TEXT = b"Use the proton spectrum and formula C7H8ClN. The archive has several experiments."


class WireScenarioTests(unittest.TestCase):
    def test_wrong_method_target_query_or_body_is_not_answered(self):
        wire = WireScenario(b"")
        for method, path, body in (
            ("POST", "/provider/v1/jobs/job:test/uploads", b""),
            ("GET", "/wrong/jobs/job:test/uploads", b""),
            ("GET", "/provider/v1/jobs/job:other/uploads", b""),
            ("GET", "/provider/v1/jobs?analysis_kind_ref=wrong", b""),
            ("GET", "/provider/v1/jobs/job:test/uploads", b"{}"),
            ("POST", "/provider/v1/execution-attempts/start", b'{"schema_id":"wrong"}'),
        ):
            with self.subTest(method=method, path=path, body=body), self.assertRaises(AssertionError):
                wire.reply(method, path, body, {})

    def test_attempt_read_reports_the_observed_completion_stage(self):
        wire = WireScenario(b"")
        for completed_sends, state in ((0, "in_progress"), (1, "succeeded"), (2, "succeeded")):
            with self.subTest(completed_sends=completed_sends):
                wire.complete_bodies = [b"retained command"] * completed_sends
                raw, media = wire.reply("GET", "/provider/v1/execution-attempts/" + ATTEMPT, b"", {})
                self.assertEqual(media, "application/json")
                self.assertEqual(json.loads(raw), {
                    "schema_id": "nmr.provider.execution_attempt_read_response.v1",
                    "execution_attempt_ref": ATTEMPT, "job_ref": "job:test",
                    "job_state": "closed", "state": state,
                })


class ProviderEndToEndTests(unittest.TestCase):
    def test_verified_upload_to_real_scientific_result_and_lost_delivery_replay(self):
        raw = Path("/fixtures/jcamp/4-chlorobenzylamine/4-chlorobenzylamine.jdx").read_bytes()
        archive_bytes = BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            archive.writestr("experiment1/carbon.jdx", raw.replace(b"^1H", b"^13C"))
            archive.writestr("experiment2/proton.jdx", raw)
        archive_bytes = archive_bytes.getvalue()
        wire = WireScenario(archive_bytes)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_test_certificates(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), wire.handler())
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root / "server.pem", root / "server-key.pem")
            server.socket = context.wrap_socket(server.socket, server_side=True)
            wire.origin = f"https://localhost:{server.server_port}"
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            watchdog = Timer(120, wire.stop.set)
            watchdog.start()
            try:
                socket_path = Path("/run/secs/worker/worker.sock")
                deadline = monotonic() + 300
                while not socket_path.exists() and monotonic() < deadline:
                    sleep(0.05)
                provider = ProviderApi(HttpsEndpoint(wire.origin, "dev-local", 2, 5, root / "ca.pem"),
                                       "provider:test", "credential:test", Ed25519PrivateKey.from_private_bytes(bytes(range(32))))
                chat = ChatEndpoint(wire.origin + "/chat", "scripted-choice", "model-key", root / "ca.pem")
                store = UploadStore(wire.origin, 128 * 1024 * 1024, ca_file=root / "ca.pem")
                config = ProviderConfig(None, HelloPolicy("SECS test", "Local workflow proof", 3600, 1),
                                        ExecutionConfig(chat.url, chat.model, store.origin, work_seconds=60,
                                                        worker_startup_seconds=60, poll_seconds=0.01, max_turns=6))
                run_services(api=provider, prepared=prepare_configured_hello(config), config=config,
                             chat=chat, upload_store=store, stop=wire.stop)
                self.assertFalse(Path("/state/journal/attempt.json").exists())
                self.assertFalse(Path("/run/secs/sources/current").exists())
                self.assertEqual(wire.starts, 1)
                self.assertEqual(wire.turns, 3)
                self.assertEqual(wire.hellos, 1)
                self.assertEqual(len(wire.complete_bodies), 2)
                retained = wire.complete_bodies[0]
                self.assertEqual(wire.complete_bodies, [retained, retained])
                # Exercise the wire read even when science finishes before the
                # worker's periodic state check. No artificial model delay needed.
                active = ActiveAttempt(StartPending("provider:test", SelectedJobInput(
                    "job:test", "nmr.job.specification.text.v1",
                    "sha256:" + sha256(TEXT).hexdigest(), len(TEXT),
                ), wire.start_key), ATTEMPT)
                self.assertEqual(JobApi(provider).snapshot(active), AttemptSnapshot("succeeded", "closed"))
                self.assertEqual(wire.downloads, 1)
                self.assertEqual(wire.capabilities, 1)
                report = json.loads(b64decode(json.loads(retained)["canonical_result_base64"]))
                self.assertEqual(report["outcome"], "analysed")
                self.assertIn("proton", report["explanation"])
                self.assertEqual(len(report["input_choices"]), 2)
                self.assertIn("not 1H", report["input_choices"][0]["reading_error"])
                self.assertEqual(report["input_choices"][-1]["source"]["member"], "experiment2/proton.jdx")
                self.assertEqual(report["acquired_uploads"][UPLOAD]["content_hash"], "sha256:" + sha256(archive_bytes).hexdigest())
                self.assertTrue(report["analysis"]["candidates"])
                self.assertEqual(report["analysis"]["search"]["generations"], 1)
                self.assertEqual(report["analysis"]["inference"]["smiles_batch_size"], 8)
                self.assertEqual(report["analysis"]["inference"]["retrieval_neighbours"], 8)
                self.assertNotIn("model-key", retained.decode())
                self.assertNotIn("store-bearer", retained.decode())
                self.assertTrue(all(wire.provider_signed))
            finally:
                watchdog.cancel()
                watchdog.join()
                server.shutdown()
                server.server_close()
                thread.join()


class WireScenario:
    def __init__(self, archive):
        self.archive = archive
        self.complete_bodies = []
        self.downloads = self.capabilities = self.turns = 0
        self.provider_signed = []
        self.stop = Event()
        self.starts = self.hellos = 0

    def handler(self):
        scenario = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.handle_operation()
            def do_POST(self):
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
                    assert "not 1H" in prompt["messages"][-1]["content"]
                member = "experiment1/carbon.jdx" if self.turns == 2 else "experiment2/proton.jdx"
                name, arguments = "read_jcamp", {"source": {"upload_ref": UPLOAD, "member": member}, "formula": "C7H8ClN",
                                                "explanation": "The selected experiment provides the proton spectrum for the supplied formula."}
            document = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": f"call-{self.turns}", "type": "function",
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
        elif route == ("GET", "/provider/v1/jobs?analysis_kind_ref=" + ANALYSIS_KIND_REF):
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
        elif route == ("POST", "/provider/v1/execution-attempts/complete"):
            command = json.loads(body)
            assert command["schema_id"] == "nmr.provider.execution_attempt_complete_request.v1"
            assert command["execution_attempt_ref"] == ATTEMPT
            retained = json.loads(Path("/state/journal/attempt.json").read_bytes())
            assert b64decode(retained["body_base64"]) == body
            self.complete_bodies.append(body)
            if len(self.complete_bodies) == 1:
                return None, None
            self.stop.set()
            result = b64decode(command["canonical_result_base64"])
            document = {"schema_id": "nmr.provider.execution_attempt_complete_response.v1", "execution_attempt_ref": ATTEMPT,
                        "result_schema_id": command["result_schema_id"], "result_byte_length": len(result), "result_fingerprint": "sha256:" + sha256(result).hexdigest(),
                        "analysis_result_ref": "analysis_result:sha256:" + "c" * 64, "committed_at": "2026-09-08T12:30:00Z", "replayed": True}
        elif route == ("GET", "/provider/v1/execution-attempts/" + ATTEMPT):
            document = {"schema_id": "nmr.provider.execution_attempt_read_response.v1", "execution_attempt_ref": ATTEMPT,
                        "job_ref": "job:test", "job_state": "closed",
                        "state": "succeeded" if self.complete_bodies else "in_progress"}
        else:
            raise AssertionError(f"Unexpected test request: {method} {path}")
        return json.dumps(document).encode(), "application/json"


def load_no_science_handler(root):
    """The real child proves readiness, but any scientific call fails this test."""
    (root / "child.pid").write_text(str(os.getpid()))
    def unexpected(command):
        (root / "science-called").touch()
        raise AssertionError("No scientific operation is allowed after model rejection")
    return unexpected


def load_failed_inspection_handler(root):
    """Raise an operation failure for the production child boundary to serialize."""
    (root / "child.pid").write_text(str(os.getpid()))
    def inspect(command):
        assert command["operation"] == "inspect"
        assert Path(command["files"][UPLOAD]).read_bytes() == b"unread fixture bytes"
        # Exclusive creation makes an accidental second inspection fail.
        with (root / "inspection-called").open("x"):
            pass
        try:
            raise OSError(errno.EIO, "private-worker-source")
        except OSError as cause:
            raise RuntimeError("private-worker-wrapper") from cause
    return inspect


class FailedAttemptScenario(WireScenario):
    """The API loses its first failure receipt after retaining the command."""

    def __init__(self, journal):
        super().__init__(b"unread fixture bytes")
        self.journal = journal
        self.failure_bodies = []

    def reply(self, method, path, body, headers):
        if (method, path) == ("GET", "/provider/v1/execution-attempts/" + ATTEMPT):
            raw, media = super().reply(method, path, body, headers)
            document = json.loads(raw)
            document["state"] = "failed" if self.failure_bodies else "in_progress"
            return json.dumps(document).encode(), media
        if (method, path) != ("POST", "/provider/v1/execution-attempts/fail"):
            return super().reply(method, path, body, headers)
        command = json.loads(body)
        assert command["schema_id"] == "nmr.provider.execution_attempt_fail_request.v1"
        assert command["execution_attempt_ref"] == ATTEMPT
        retained = json.loads((self.journal / "attempt.json").read_bytes())
        assert b64decode(retained["body_base64"]) == body
        self.failure_bodies.append(body)
        if len(self.failure_bodies) == 1:
            return None, None
        self.stop.set()
        receipt = {name: command[name] for name in ("execution_attempt_ref", "failure_code", "failure_message")}
        receipt.update(schema_id="nmr.provider.execution_attempt_fail_response.v1", replayed=True,
                       committed_at="2026-09-10T00:00:00Z")
        return json.dumps(receipt).encode(), "application/json"


class RejectedInterpretationScenario(FailedAttemptScenario):
    """Reject interpretation before the provider acquires or inspects any Upload."""

    def handler(self):
        scenario = self
        class Handler(super().handler()):
            def do_POST(self):
                if self.path != "/chat":
                    return super().do_POST()
                assert self.headers["Authorization"] == "Bearer model-key"
                body = self.rfile.read(int(self.headers["Content-Length"]))
                assert json.loads(body)["model"] == "scripted-failure"
                scenario.turns += 1
                reply = b'{"error":{"message":"private incomplete model detail"}}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("x-request-id", "model-rejection-request")
                self.send_header("Content-Length", str(len(reply) + 1))
                self.end_headers()
                self.wfile.write(reply)
        return Handler


class ProviderFailureWireTests(unittest.TestCase):
    def test_model_rejection_is_failed_and_replayed_without_scientific_work(self):
        """Prove controller composition, not model inference or scientific quality."""
        with self.failure_lane(RejectedInterpretationScenario, load_no_science_handler) as (wire, root, evidence, failure):
            self.assertEqual((wire.downloads, wire.capabilities), (0, 0))
            self.assertEqual(failure["failure_code"], "interpretation_failed")
            self.assertIn("scripted-failure", failure["failure_message"])
            self.assertIn("HTTP 400", failure["failure_message"])
            self.assertFalse((root / "science-called").exists())
            self.assertEqual(evidence["interpreter"]["request_id"], "model-rejection-request")
            self.assertIn("declared bytes", evidence["interpreter"]["detail_unavailable"])

    def test_verified_upload_and_worker_failure_survive_lost_publication_receipt(self):
        """The child fails inspection; verified input identity and both causes survive."""
        remove = shutil.rmtree
        removals = []
        def remove_after_exit(path, *args, **kwargs):
            source = Path(path)
            if source.name == "current":
                self.assertTrue(source.is_dir())
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((source.parent / "child.pid").read_text()), 0)
                removals.append(source)
            return remove(path, *args, **kwargs)
        with patch("secs_inference.provider.analysis_run.shutil.rmtree", side_effect=remove_after_exit), \
             self.failure_lane(FailedAttemptScenario, load_failed_inspection_handler) as (wire, root, evidence, failure):
            self.assertEqual((wire.downloads, wire.capabilities), (1, 1))
            self.assertTrue((root / "inspection-called").exists())
            self.assertEqual(failure["failure_code"], "scientific_execution_failed")
            self.assertIn("input inspection", failure["failure_message"])
            self.assertEqual(evidence["worker"]["exception_type"], "RuntimeError")
            self.assertEqual(evidence["worker"]["cause"]["exception_type"], "OSError")
            self.assertEqual(evidence["worker"]["cause"]["errno"], errno.EIO)
            self.assertEqual(evidence["worker"]["cause"]["reason"], "Input/output error")
            self.assertEqual(evidence["analysis"]["acquired_uploads"], {
                UPLOAD: {"byte_length": len(wire.archive), "content_hash": "sha256:" + sha256(wire.archive).hexdigest()},
            })
            self.assertEqual(removals, [root / "current"])

    @contextmanager
    def failure_lane(self, scenario_type, load_handler):
        """Exercise real HTTPS, a spawned child, durable failure and exact replay."""
        with TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener, ExitStack() as cleanup:
            root = Path(directory)
            socket_path = root / "worker.sock"
            listener.bind(str(socket_path))
            listener.listen(1)
            listener.settimeout(5)
            worker_errors = []
            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        _serve_session(connection, partial(load_handler, root))
                except BaseException as error:
                    worker_errors.append(error)
            worker_thread = Thread(target=serve, daemon=True)
            worker_thread.start()
            cleanup.callback(worker_thread.join, 6)
            _write_test_certificates(root)
            wire = scenario_type(root / "journal")
            server = ThreadingHTTPServer(("127.0.0.1", 0), wire.handler())
            cleanup.callback(server.server_close)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root / "server.pem", root / "server-key.pem")
            server.socket = context.wrap_socket(server.socket, server_side=True)
            wire.origin = f"https://localhost:{server.server_port}"
            server_thread = Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            cleanup.callback(server_thread.join, 5)
            cleanup.callback(server.shutdown)
            watchdog = Timer(15, wire.stop.set)
            watchdog.start()
            try:
                provider = ProviderApi(HttpsEndpoint(wire.origin, "dev-local", 2, 3, root / "ca.pem"),
                    "provider:test", "credential:test", Ed25519PrivateKey.from_private_bytes(bytes(range(32))))
                chat = ChatEndpoint(wire.origin + "/chat", "scripted-failure", "model-key", root / "ca.pem")
                store = UploadStore(wire.origin, 1024, ca_file=root / "ca.pem")
                config = ProviderConfig(None, HelloPolicy("Test", "Controller failure proof", 3600, 1),
                    ExecutionConfig(chat.url, chat.model, store.origin, worker_startup_seconds=5, poll_seconds=0.01))
                with patch("secs_inference.provider.runtime.WORKER_SOCKET", socket_path), \
                     patch("secs_inference.provider.runtime.SOURCE_DIRECTORY", root / "current"), \
                     patch("secs_inference.provider.runtime.JOURNAL_DIRECTORY", wire.journal):
                    run_services(api=provider, prepared=prepare_configured_hello(config), config=config,
                                 chat=chat, upload_store=store, stop=wire.stop)
                self.assertEqual((wire.starts, wire.turns), (1, 1))
                self.assertEqual(len(wire.failure_bodies), 2)
                self.assertEqual(wire.failure_bodies[0], wire.failure_bodies[1])
                failure = json.loads(wire.failure_bodies[0])
                self.assertFalse((root / "current").exists())
                self.assertFalse((wire.journal / "attempt.json").exists())
                active = ActiveAttempt(StartPending("provider:test", SelectedJobInput(
                    "job:test", "nmr.job.specification.text.v1", "sha256:" + sha256(TEXT).hexdigest(), len(TEXT),
                ), wire.start_key), ATTEMPT)
                self.assertEqual(JobApi(provider).snapshot(active), AttemptSnapshot("failed", "closed"))
                evidence = json.loads(next(wire.journal.glob("*.diagnostic.json")).read_bytes())
                for secret in ("model-key", "store-bearer", "private incomplete model detail", "private-worker"):
                    self.assertNotIn(secret, json.dumps(evidence) + failure["failure_message"])
                worker_thread.join(5)
                self.assertFalse(worker_thread.is_alive())
                self.assertEqual(worker_errors, [])
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((root / "child.pid").read_text()), 0)
                yield wire, root, evidence, failure
            finally:
                watchdog.cancel()
                watchdog.join()
