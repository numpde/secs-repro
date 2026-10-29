"""Only admitted API refusals may prompt a new source-membership observation."""

from pathlib import Path
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from provider_core.api import ApiFailure
from provider_core.transport import HttpResponse
from provider_core.upload_response import UploadMember
from secs_inference.provider.sources import AttemptSources, SourceSetChanged


class SourceMembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_unadmitted_404_does_not_become_a_scientific_membership_correction(self):
        for code in (None, "resource_not_found"):
            with self.subTest(problem_code=code):
                refusal = ApiFailure("request Upload access", "fixture refusal", response=HttpResponse(404, (), b""))
                refusal.problem_code = code
                api = SimpleNamespace(read_grant=AsyncMock(side_effect=refusal), retire=AsyncMock())
                sources = AttemptSources(job_ref="job:fixture", api=api,
                                         store=SimpleNamespace(retirement_confirmed=True),
                                         store_origin="https://store.example", directory=Path("/unused"),
                                         max_upload_bytes=1024, max_total_bytes=1024, retirement_seconds=1)
                selected = UploadMember("upload:sha256:" + "a" * 64, "fixture", 8, None)
                sources._uploads = {selected.upload_ref: selected}
                sources._acquisition_deadline = monotonic() + 1
                sources._read_members = AsyncMock(return_value=[])
                expected = ApiFailure if code is None else SourceSetChanged
                with self.assertRaises(expected):
                    await sources._grant(selected)
                if code is None:
                    sources._read_members.assert_not_awaited()
                    api.retire.assert_not_awaited()
                else:
                    sources._read_members.assert_awaited_once()
                    api.retire.assert_awaited_once()
