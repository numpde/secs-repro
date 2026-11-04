"""SECS retries fresh access without charging failed downloads or losing its cache."""

from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
import ssl
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpcore
from provider_core.transport import HttpTransport
from provider_core.upload_response import ReadGrant, UploadMember, UploadSet
from secs_inference.provider.sources import AttemptSources, SourceDeadlineBeforeDispatch


class Stream:
    def __init__(self, body):
        self.body = body
    async def __aiter__(self):
        yield self.body
    async def aclose(self):
        pass


class Client:
    def __init__(self, status, body, headers):
        self.status, self.body, self.headers = status, body, headers
        self.requests = []
    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpcore.Response(self.status, headers=self.headers, content=Stream(self.body))
    async def aclose(self):
        pass


class SourceAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_model_window_does_not_start_upload_transfer(self):
        ref = "upload:fixture"
        sources = AttemptSources(job_ref="job:fixture", api=SimpleNamespace(), store=SimpleNamespace(),
                                 store_origin="https://store.example", directory=Path("/unused"),
                                 max_upload_bytes=10, max_total_bytes=10, retirement_seconds=1)
        sources._deadline = monotonic() + 5
        sources._uploads = {ref: UploadMember(ref, "fixture", 1, None)}
        sources.transfer.acquire_file = AsyncMock()
        with self.assertRaises(SourceDeadlineBeforeDispatch):
            await sources.acquire(ref, deadline=monotonic() - 1)
        sources.transfer.acquire_file.assert_not_awaited()

    async def test_store_refusal_gets_fresh_grant_then_one_success_is_cached_and_charged_once(self):
        ref = "upload:sha256:" + "a" * 64
        digest = "sha256:" + sha256(b"x").hexdigest()
        member = UploadMember(ref, "fixture", 1, digest)
        grants = [ReadGrant("job:fixture", ref, 1, digest,
                            "https://store.example/upload/v1/uploads/" + ref + "/bytes", capability,
                            datetime.now(UTC) + timedelta(minutes=1)) for capability in ("first", "fresh")]
        api = SimpleNamespace(uploads=AsyncMock(return_value=UploadSet("job:fixture", (member,))),
                              read_grant=AsyncMock(side_effect=grants), retire=AsyncMock())
        error_body = b'{"error":"temporarily unavailable"}'
        clients = [Client(503, error_body, [(b"content-length", str(len(error_body)).encode("ascii"))]),
                   Client(200, b"x", [(b"content-type", b"application/octet-stream"), (b"cache-control", b"no-store"),
                                      (b"content-length", b"1"), (b"content-digest", b"sha-256=:" + b64encode(sha256(b"x").digest()) + b":")])]
        store = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=1)
        with TemporaryDirectory() as temporary:
            directory = Path(temporary) / "sources"
            sources = AttemptSources(job_ref="job:fixture", api=api, store=store, store_origin="https://store.example",
                                     directory=directory, max_upload_bytes=1, max_total_bytes=1, retirement_seconds=1)
            await sources.open(deadline=monotonic() + 5, readers_quiescent=True)
            try:
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
                    receipt = await sources.acquire(ref, deadline=monotonic() + 5)
                    self.assertIs(await sources.acquire(ref, deadline=monotonic() + 5), receipt)
                self.assertEqual(api.read_grant.await_count, 2)
                self.assertEqual([{name.lower(): value for name, value in client.requests[0].headers}[b"authorization"]
                                  for client in clients],
                                 [b"Bearer first", b"Bearer fresh"])
                self.assertEqual(sources._spent_bytes, 1)
                self.assertEqual((directory / receipt.name).read_bytes(), b"x")
                self.assertEqual(len(tuple(directory.iterdir())), 1)
                self.assertTrue(store.retirement_confirmed)
                self.assertFalse(sources.transfer.file_cleanup_pending)
            finally:
                await sources.retire(readers_stopped=True)
            self.assertFalse(directory.exists())
