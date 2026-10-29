"""A disposable TLS peer verifies the actual provider's Hello signature and SNI.

Signature-base verification follows provider-core e0d6b301 tests/fakes/api_tls.py;
credential fields follow its API-issued artifact fixture. These keys authorize
only this synthetic peer and never leave the private test workspace.
"""

from base64 import b64encode
from datetime import UTC, datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import sys
from threading import Lock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from http_sf import parse
from tls_fixture import _write_test_certificates


root = Path(sys.argv[1])
_write_test_certificates(root)
(root / "server-key.pem").chmod(0o600)
key = Ed25519PrivateKey.generate()
credential = {
    "schema_id": "nmr.provider.private_signing_credential.v1", "profile": "dev-local",
    "principal_ref": "provider:secs-route", "credential_ref": "credential:secs-route", "algorithm": "ed25519",
    "public_key_spki_der_b64": b64encode(key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode(),
    "private_key_pkcs8_pem": key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
}
(root / "credential.json").write_text(json.dumps(credential, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
(root / "credential.json").chmod(0o600)
state, lock = {"hellos": 0, "sni": [], "error": None}, Lock()


def capture():
    temporary = root / "capture.next"
    temporary.write_text(json.dumps(state))
    temporary.replace(root / "capture.json")


def verify(method, target, headers, body, origin):
    assert method == "POST" and target == "/provider/v1/hello"
    assert headers["Content-Type"] == "application/json"
    assert parse(headers["Content-Digest"].encode(), tltype="dictionary")["sha-256"][0] == sha256(body).digest()
    _members, parameters = parse(headers["Signature-Input"].encode(), tltype="dictionary")["sig1"]
    assert parameters["keyid"] == "credential:secs-route" and parameters["tag"] == "nmr-api-v1"
    base = [b'"@method": POST', b'"@authority": ' + origin.removeprefix("https://").encode(),
            b'"@path": /provider/v1/hello', b'"@query": ?', b'"content-type": application/json',
            b'"content-digest": ' + headers["Content-Digest"].encode(),
            b'"@signature-params": ' + headers["Signature-Input"].split("=", 1)[1].encode()]
    signature = parse(headers["Signature"].encode(), tltype="dictionary")["sig1"][0]
    key.public_key().verify(signature, b"\n".join(base))
    assert json.loads(body)["schema_id"] == "nmr.provider.hello_request.v1"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_arguments):
        pass

    def reply(self, status, document):
        body = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Nmr-Api-Topology", "dev-local")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.path == "/health"
        self.reply(200, {"healthy": True})

    def do_POST(self):
        try:
            length = int(self.headers["Content-Length"])
            assert 0 < length <= 65536
            body = self.rfile.read(length)
            verify(self.command, self.path, self.headers, body, self.server.origin)
            assert self.connection.route_sni == "localhost"
            with lock:
                state["hellos"] += 1
                state["sni"].append(self.connection.route_sni)
                capture()
            self.reply(200, {"schema_id": "nmr.provider.hello_response.v1", "provider_ref": "provider:secs-route",
                             "accepted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")})
        except Exception as failure:
            with lock:
                state["error"] = type(failure).__name__
                capture()
            self.reply(500, {"fixture_error": type(failure).__name__})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.origin = f"https://localhost:{server.server_port}"
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(root / "server.pem", root / "server-key.pem")
def record_sni(connection, name, _context):
    connection.route_sni = name
context.set_servername_callback(record_sni)
server.socket = context.wrap_socket(server.socket, server_side=True)
capture()
(root / "ready.next").write_text(json.dumps({"origin": server.origin}))
(root / "ready.next").replace(root / "ready.json")
server.serve_forever()
