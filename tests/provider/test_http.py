"""Exercise the hello transport against a real local TLS peer."""

from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import ssl
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from secs_inference.provider.http import (
    HttpResponse,
    HttpsEndpoint,
    RequestDelivery,
    RequestUnavailable,
    ResponseRejected,
    ResponseRejection,
    TlsRejected,
    send_hello_request,
)
from secs_inference.provider.signing import sign_request


PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


class ProviderHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = TemporaryDirectory()
        cls.certificate_directory = Path(cls.temporary_directory.name)
        _write_test_certificates(cls.certificate_directory)

    @classmethod
    def tearDownClass(cls):
        cls.temporary_directory.cleanup()

    def test_post_preserves_the_signed_target_body_and_digest(self):
        body = b'{"schema_id":"nmr.provider.hello_request.v1"}'
        with _tls_server(self.certificate_directory) as server:
            endpoint = self._endpoint(server.port)
            request = _signed_hello(endpoint.authority, body)

            outcome = send_hello_request(endpoint=endpoint, request=request)

        self.assertIs(type(outcome), HttpResponse, repr(outcome))
        captured = server.requests[0]
        self.assertEqual(
            captured["requestline"],
            "POST /provider/v1/hello HTTP/1.1",
        )
        self.assertEqual(captured["host"], endpoint.authority)
        self.assertEqual(captured["body"], body)
        self.assertEqual(captured["content-length"], str(len(body)))
        self.assertEqual(
            captured["content-digest"],
            request.headers["Content-Digest"],
        )
        self.assertEqual(
            captured["signature-input"],
            request.headers["Signature-Input"],
        )

    def test_untrusted_server_certificate_is_a_tls_rejection(self):
        with _tls_server(self.certificate_directory) as server:
            endpoint = HttpsEndpoint(
                origin=f"https://localhost:{server.port}",
                expected_topology="dev-local",
                connect_timeout_seconds=1,
                io_deadline_seconds=1,
            )

            outcome = send_hello_request(
                endpoint=endpoint,
                request=_signed_hello(endpoint.authority, b"{}"),
            )

        self.assertEqual(outcome, TlsRejected())
        self.assertIsInstance(outcome.cause, ssl.SSLError)
        self.assertEqual(server.requests, [])

    def test_connection_refusal_proves_the_request_was_not_sent(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        endpoint = self._endpoint(port)

        outcome = send_hello_request(
            endpoint=endpoint,
            request=_signed_hello(endpoint.authority, b"{}"),
        )

        self.assertEqual(outcome, RequestUnavailable(RequestDelivery.NOT_SENT))
        self.assertIsInstance(outcome.cause, ConnectionRefusedError)

    def test_response_rejects_each_untrusted_envelope_fact(self):
        cases = (
            ({"Content-Type": "text/plain"}, ResponseRejection.INVALID_CONTENT_TYPE),
            ({"Cache-Control": "private"}, ResponseRejection.INVALID_CACHE_CONTROL),
            ({"Nmr-Api-Topology": "web"}, ResponseRejection.INVALID_TOPOLOGY),
            (
                {"Content-Encoding": "gzip"},
                ResponseRejection.CONTENT_ENCODING_NOT_ADMITTED,
            ),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                headers = _valid_response_headers() | overrides
                with _tls_server(
                    self.certificate_directory,
                    response_headers=headers,
                ) as server:
                    outcome = self._send(server.port)

                self.assertEqual(outcome, ResponseRejected(reason, 200))

    def test_gateway_failures_are_unavailable_without_an_api_envelope(self):
        for status in (502, 504):
            with self.subTest(status=status):
                with _tls_server(
                    self.certificate_directory,
                    status=status,
                    response_headers={"Content-Type": "text/html"},
                    response_body=b"gateway failure",
                ) as server:
                    outcome = self._send(server.port)

                self.assertEqual(
                    outcome,
                    RequestUnavailable(
                        RequestDelivery.RESPONSE_RECEIVED,
                        status=status,
                    ),
                )

    def test_short_response_preserves_the_incomplete_exchange(self):
        with _tls_server(
            self.certificate_directory,
            response_body=b"four",
            declared_response_length=5,
        ) as server:
            outcome = self._send(server.port)

        self.assertEqual(
            outcome,
            RequestUnavailable(
                RequestDelivery.RESPONSE_RECEIVED,
                status=200,
            ),
        )
        self.assertEqual(
            str(outcome.cause),
            "HTTP response ended after 4 of 5 declared bytes",
        )

    def test_one_deadline_bounds_the_complete_response_read(self):
        with _tls_server(
            self.certificate_directory,
            response_body=b"four",
            drip_seconds=0.08,
        ) as server:
            endpoint = self._endpoint(server.port, io_deadline_seconds=0.12)
            outcome = send_hello_request(
                endpoint=endpoint,
                request=_signed_hello(endpoint.authority, b"{}"),
            )

        self.assertEqual(
            outcome,
            RequestUnavailable(
                RequestDelivery.RESPONSE_RECEIVED,
                status=200,
            ),
        )
        self.assertIsInstance(outcome.cause, TimeoutError)

    def test_response_body_limit_rejects_one_byte_over_the_cap(self):
        with _tls_server(
            self.certificate_directory,
            response_body=b"x" * 65_537,
        ) as server:
            outcome = self._send(server.port)

        self.assertEqual(
            outcome,
            ResponseRejected(ResponseRejection.RESPONSE_BODY_TOO_LARGE, 200),
        )

    def _endpoint(
        self,
        port: int,
        *,
        io_deadline_seconds: float = 1,
    ) -> HttpsEndpoint:
        return HttpsEndpoint(
            origin=f"https://localhost:{port}",
            expected_topology="dev-local",
            connect_timeout_seconds=1,
            io_deadline_seconds=io_deadline_seconds,
            ca_file=self.certificate_directory / "ca.pem",
        )

    def _send(self, port: int):
        endpoint = self._endpoint(port)
        return send_hello_request(
            endpoint=endpoint,
            request=_signed_hello(endpoint.authority, b"{}"),
        )


def _signed_hello(authority: str, body: bytes):
    return sign_request(
        private_key=PRIVATE_KEY,
        credential_ref="credential:provider:test",
        method="POST",
        authority=authority,
        path="/provider/v1/hello",
        query="",
        body=body,
        created=1_700_000_000,
        nonce=bytes(range(16)),
    )


def _valid_response_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "Nmr-Api-Topology": "dev-local",
    }


class _QuietThreadingHttpServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


@contextmanager
def _tls_server(
    certificate_directory: Path,
    *,
    status: int = 200,
    response_headers: dict[str, str] | None = None,
    response_body: bytes = b"{}",
    declared_response_length: int | None = None,
    drip_seconds: float | None = None,
):
    requests: list[dict[str, str | bytes]] = []
    headers = (
        _valid_response_headers()
        if response_headers is None
        else response_headers
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(
                {
                    "requestline": self.requestline,
                    "host": self.headers.get("Host", ""),
                    "signature-input": self.headers.get("Signature-Input", ""),
                    "content-length": self.headers.get("Content-Length", ""),
                    "content-digest": self.headers.get("Content-Digest", ""),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header(
                "Content-Length",
                str(
                    len(response_body)
                    if declared_response_length is None
                    else declared_response_length
                ),
            )
            self.send_header("Connection", "close")
            self.end_headers()
            if drip_seconds is None:
                self.wfile.write(response_body)
            else:
                for byte in response_body:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    sleep(drip_seconds)

        def log_message(self, format, *args):
            pass

    server = _QuietThreadingHttpServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certificate_directory / "server.pem",
        certificate_directory / "server-key.pem",
    )
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.requests = requests
    server.port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _write_test_certificates(directory: Path) -> None:
    valid_from = datetime(2020, 1, 1, tzinfo=UTC)
    valid_until = datetime(2035, 1, 1, tzinfo=UTC)
    ca_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NMR test CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(valid_from)
        .not_valid_after(valid_until)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(2)
        .not_valid_before(valid_from)
        .not_valid_after(valid_until)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (directory / "ca.pem").write_bytes(
        ca_certificate.public_bytes(serialization.Encoding.PEM)
    )
    (directory / "server.pem").write_bytes(
        server_certificate.public_bytes(serialization.Encoding.PEM)
    )
    (directory / "server-key.pem").write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


if __name__ == "__main__":
    unittest.main()
