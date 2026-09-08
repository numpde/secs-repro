"""Real HTTPS, ZIP decoding, warm model/FAISS/GA and durable result replay.

API and Chat peers are deterministic fixtures. The scientific child runs in a
separate networkless container with real checkpoint weights and eight candidate
molecules. This proves the workflow, not full-index quality or live LLM choice.
"""

from base64 import b64encode, b64decode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import socket
import ssl
from tempfile import TemporaryDirectory
from threading import Event, Thread, Timer
from time import monotonic, sleep
import unittest
from zipfile import ZipFile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.api import ProviderApi
from secs_inference.provider.chat import ChatEndpoint
from secs_inference.provider.config import ExecutionConfig, HelloPolicy, ProviderConfig
from secs_inference.provider.http import HttpsEndpoint
from secs_inference.provider.main import prepare_configured_hello
from secs_inference.provider.runtime import run_services
from secs_inference.provider.upload_download import UploadStore
from tls_fixture import _write_test_certificates


UPLOAD = "upload:sha256:" + "a" * 64
DISTRACTOR = "upload:sha256:" + "0" * 64
ATTEMPT = "execution_attempt:sha256:" + "b" * 64
TEXT = b"Use the proton spectrum and formula C7H8ClN. The archive has several experiments."


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
                response, media = scenario.reply(self.path, body, self.headers)
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

    def reply(self, path, body, headers):
        if path == "/chat":
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
        elif path == "/provider/v1/hello":
            self.hellos += 1
            document = {"schema_id": "nmr.provider.hello_response.v1", "provider_ref": "provider:test", "accepted_at": "2026-09-08T12:00:00Z"}
        elif path.startswith("/upload/"):
            assert headers["Authorization"] == "Bearer store-bearer"
            assert "Signature" not in headers
            self.downloads += 1
            return self.archive, "application/octet-stream"
        elif path.startswith("/provider/v1/jobs?"):
            document = {"schema_id": "nmr.provider.jobs.list.response.v1", "analysis_kind_ref": ANALYSIS_KIND_REF,
                        "has_provider_execution_attempt": False, "next_cursor": None, "jobs": [{"job_ref": "job:test", "analysis_kind_ref": ANALYSIS_KIND_REF,
                        "input_schema_id": "nmr.job.specification.text.v1", "input_fingerprint": "sha256:" + sha256(TEXT).hexdigest(), "input_byte_length": len(TEXT),
                        "created_at": "2026-09-08T12:00:00Z"}]}
        elif path.endswith("/start"):
            self.starts += 1
            document = {"schema_id": "nmr.provider.execution_attempt_start_response.v1", "execution_attempt_ref": ATTEMPT, "job_ref": "job:test",
                        "provider_ref": "provider:test", "analysis_kind_ref": ANALYSIS_KIND_REF, "state": "in_progress", "replayed": False, "started_at": "2026-09-08T12:00:00Z"}
        elif "/input?" in path:
            document = {"schema_id": "nmr.provider.job_input.read.response.v1", "job_ref": "job:test", "canonical_input_base64": b64encode(TEXT).decode(),
                        "input_schema_id": "nmr.job.specification.text.v1", "input_byte_length": len(TEXT), "input_fingerprint": "sha256:" + sha256(TEXT).hexdigest()}
        elif path.endswith("/uploads"):
            document = {"schema_id": "nmr.provider.job_upload_set.read.response.v1", "job_ref": "job:test", "uploads": [
                {"upload_ref": DISTRACTOR, "description": "irrelevant carbon attachment", "byte_length": 4, "content_hash": None},
                {"upload_ref": UPLOAD, "description": "Multiple experiments; find the proton spectrum.", "byte_length": len(self.archive), "content_hash": None}]}
        elif path.endswith("/read-capability"):
            self.capabilities += 1
            document = {"schema_id": "nmr.upload.read_capability.response.v1", "upload_ref": UPLOAD, "method": "GET", "download_url": self.origin + "/upload/v1/uploads/" + UPLOAD + "/bytes",
                        "byte_length": len(self.archive), "content_hash": "sha256:" + sha256(self.archive).hexdigest(), "expires_at": "2030-01-01T00:00:00Z", "capability": "store-bearer"}
        elif path.endswith("/complete"):
            retained = json.loads(Path("/state/journal/attempt.json").read_bytes())
            assert b64decode(retained["body_base64"]) == body
            self.complete_bodies.append(body)
            if len(self.complete_bodies) == 1:
                return None, None
            command = json.loads(body)
            self.stop.set()
            result = b64decode(command["canonical_result_base64"])
            document = {"schema_id": "nmr.provider.execution_attempt_complete_response.v1", "execution_attempt_ref": ATTEMPT,
                        "result_schema_id": command["result_schema_id"], "result_byte_length": len(result), "result_fingerprint": "sha256:" + sha256(result).hexdigest(),
                        "analysis_result_ref": "analysis_result:sha256:" + "c" * 64, "committed_at": "2026-09-08T12:30:00Z", "replayed": True}
        elif path.startswith("/provider/v1/execution-attempts/execution_attempt:"):
            document = {"schema_id": "nmr.provider.execution_attempt.read.response.v1", "execution_attempt_ref": ATTEMPT,
                        "job_ref": "job:test", "job_state": "closed", "state": "in_progress"}
        else:
            raise AssertionError("Unexpected test request: " + path)
        return json.dumps(document).encode(), "application/json"
