"""Upload admission preserves the choice and binds its later byte grant."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
import unittest

from secs_inference.provider.job_upload import (
    JobUpload,
    parse_job_upload_set_response,
    parse_upload_read_capability_response,
)


UPLOAD = JobUpload("upload:sha256:" + "a" * 64, "  Proton café\n", 4, "sha256:" + "b" * 64)


def upload_set(*uploads, job_ref="job:chosen"):
    return json.dumps({
        "schema_id": "nmr.provider.job_upload_set.read.response.v1",
        "job_ref": job_ref,
        "uploads": list(uploads),
    }).encode()


def grant(**changes):
    return json.dumps({
        "schema_id": "nmr.upload.read_capability.response.v1",
        "upload_ref": UPLOAD.upload_ref,
        "byte_length": UPLOAD.byte_length,
        "content_hash": UPLOAD.content_hash,
        "expires_at": "2026-09-08T12:00:00Z",
        "method": "GET",
        "download_url": "https://store.example/private",
        "capability": "secret-bearer",
    } | changes).encode()


class JobUploadTests(unittest.TestCase):
    def test_current_set_preserves_description_and_may_be_empty(self):
        for expected in ((), (UPLOAD,)):
            raw = upload_set(*(asdict(item) for item in expected))
            self.assertEqual(parse_job_upload_set_response(raw, expected_job_ref="job:chosen"), expected)

    def test_wrong_job_and_ambiguous_identities_are_rejected(self):
        for raw in (
            upload_set(asdict(UPLOAD), job_ref="job:other"),
            upload_set(asdict(UPLOAD), asdict(UPLOAD)),
            b'{"schema_id":"ignored","schema_id":"nmr.provider.job_upload_set.read.response.v1","job_ref":"job:chosen","uploads":[]}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_job_upload_set_response(raw, expected_job_ref="job:chosen")

    def test_scalar_rejections_do_not_echo_untrusted_metadata(self):
        for change in ({"byte_length": True}, {"description": "private\x00text"}, {"content_hash": 42}):
            with self.subTest(change=change), self.assertRaises(ValueError) as caught:
                parse_job_upload_set_response(upload_set(asdict(UPLOAD) | change), expected_job_ref="job:chosen")
            self.assertNotIn("private", str(caught.exception))

    def test_grant_binds_selection_and_can_finalize_a_pending_hash(self):
        for selected in (UPLOAD, replace(UPLOAD, content_hash=None)):
            admitted = parse_upload_read_capability_response(grant(), selected=selected)
            self.assertEqual(admitted.content_hash, UPLOAD.content_hash)
            self.assertEqual(admitted.expires_at, datetime(2026, 9, 8, 12, tzinfo=UTC))
            self.assertNotIn("secret-bearer", repr(admitted))
            self.assertNotIn("store.example", repr(admitted))

    def test_grant_cannot_switch_identity_length_or_finalized_hash(self):
        for change in (
            {"upload_ref": "upload:sha256:" + "c" * 64},
            {"byte_length": True}, {"byte_length": 5},
            {"content_hash": "sha256:" + "c" * 64},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_upload_read_capability_response(grant(**change), selected=UPLOAD)

    def test_expiry_is_parsed_but_not_an_execution_freshness_gate(self):
        for expiry in ("2000-01-01T00:00:00Z", "2026-09-08T12:00:00.123456Z"):
            parse_upload_read_capability_response(grant(expires_at=expiry), selected=UPLOAD)
        for expiry in (None, "2026-02-31T00:00:00Z", "2026-09-08T12:00:00.000000Z", "2026-09-08T12:00:00+00:00"):
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                parse_upload_read_capability_response(grant(expires_at=expiry), selected=UPLOAD)
