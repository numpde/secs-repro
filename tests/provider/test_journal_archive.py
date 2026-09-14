"""Closed Attempt archival preserves evidence while releasing local recovery."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from secs_inference.provider.attempt_store import AttemptStore, JournalError
from secs_inference.provider.attempt_state import TerminalHold
from secs_inference.provider.job_api import JobApi, complete_command
from secs_inference.provider.http import HttpResponse
from test_execution import ACTIVE, START, REPORT


class ClosedJournalArchiveTests(unittest.TestCase):
    def seed(self, root):
        record = replace(complete_command(ACTIVE, REPORT), hold=TerminalHold(
            "do_not_resend", "execution_attempt_outcome_expired", "Do not resend.",
            "Attempt expired.", "request:retained", "expired"))
        with AttemptStore(root) as store:
            store.save(record)
        raw = (root / "attempt.json").read_bytes()
        return raw, "sha256:" + sha256(raw).hexdigest()

    def api(self, state="expired", ref=ACTIVE.execution_attempt_ref):
        transport = Mock(provider_ref=START.provider_ref)
        transport.endpoint.origin = "https://api.example.test"
        transport.request.return_value = HttpResponse(200, "request:archive-read", json.dumps({
            "schema_id": "nmr.provider.execution_attempt_read_response.v1",
            "execution_attempt_ref": ref, "job_ref": START.selected.job_ref,
            "state": state, "job_state": "closed",
        }).encode())
        return JobApi(transport), transport

    def archive(self, store, api, digest):
        from secs_inference.provider.journal_archive import archive_closed
        return archive_closed(journal=store, api=api, owner={"origin": "https://api.example.test", "provider_ref": START.provider_ref},
            execution_attempt_ref=ACTIVE.execution_attempt_ref,
            expected_record_digest=digest, reason="Investigated expired Attempt; preserve completed result.")

    def test_closed_hold_is_archived_before_retirement_without_publication(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            raw, digest = self.seed(root)
            api, transport = self.api()
            with AttemptStore(root) as store, patch.object(api, "publish", side_effect=AssertionError("No report send")):
                result = self.archive(store, api, digest)
                self.assertIsNone(store.load())
            archive = json.loads(Path(result["archive_path"]).read_bytes())
            from base64 import b64decode
            self.assertEqual(b64decode(archive["original_record_base64"]), raw)
            self.assertEqual(archive["record_digest"], digest)
            self.assertEqual(archive["snapshot"]["state"], "expired")
            self.assertEqual(result["delivery"], "unconfirmed")
            self.assertEqual(result["next_actor"], "provider_operator")
            self.assertIn("restart", result["next_action"].lower())
            transport.request.assert_called_once()
            with AttemptStore(root) as store:
                self.assertIsNone(store.load())

    def test_stale_digest_does_not_read_or_mutate(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            raw, _ = self.seed(root)
            api, transport = self.api()
            with AttemptStore(root) as store, self.assertRaises((ValueError, JournalError)):
                self.archive(store, api, "sha256:" + "0" * 64)
            transport.request.assert_not_called()
            self.assertEqual((root / "attempt.json").read_bytes(), raw)

    def test_live_or_wrong_identity_read_preserves_retained_work(self):
        for state, ref in (("in_progress", ACTIVE.execution_attempt_ref),
                           ("expired", "execution_attempt:sha256:" + "c" * 64)):
            with self.subTest(state=state, ref=ref), TemporaryDirectory() as temporary:
                root = Path(temporary) / "journal"
                raw, digest = self.seed(root)
                api, _ = self.api(state, ref)
                with AttemptStore(root) as store, self.assertRaises((ValueError, RuntimeError)):
                    self.archive(store, api, digest)
                self.assertEqual((root / "attempt.json").read_bytes(), raw)
                self.assertFalse(list(root.glob("*.archive.json")))

    def test_unknown_record_fields_are_preserved_and_digest_covers_them(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            self.seed(root)
            document = json.loads((root / "attempt.json").read_bytes())
            document["future_evidence"] = {"uncertain": True}
            raw = json.dumps(document, indent=2).encode()
            (root / "attempt.json").write_bytes(raw)
            with AttemptStore(root) as store:
                result = self.archive(store, self.api()[0], "sha256:" + sha256(raw).hexdigest())
            from base64 import b64decode
            self.assertEqual(b64decode(json.loads(Path(result["archive_path"]).read_bytes())["original_record_base64"]), raw)

    def test_preflight_rejects_read_only_wrong_provider_and_nonheld(self):
        for variant in ("read_only", "provider", "nonheld", "reconciling"):
            with self.subTest(variant=variant), TemporaryDirectory() as temporary:
                root = Path(temporary) / "journal"
                self.seed(root)
                if variant in {"nonheld", "reconciling"}:
                    with AttemptStore(root) as store:
                        record = store.load()
                        hold = None if variant == "nonheld" else replace(record.hold, action="reconcile_state", observed_state=None)
                        store.save(replace(record, hold=hold))
                raw = (root / "attempt.json").read_bytes()
                api, transport = self.api()
                if variant == "provider":
                    api.provider_ref = "provider:someone-else"
                with AttemptStore(root, read_only=variant == "read_only") as store, self.assertRaises((ValueError, JournalError)):
                    self.archive(store, api, "sha256:" + sha256(raw).hexdigest())
                transport.request.assert_not_called()
                self.assertEqual((root / "attempt.json").read_bytes(), raw)

    def test_interrupted_retirement_can_retry_without_replacing_archive(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            raw, digest = self.seed(root)
            with AttemptStore(root) as store, patch.object(store, "clear", side_effect=JournalError("interrupted")):
                with self.assertRaises(JournalError):
                    self.archive(store, self.api()[0], digest)
            path, = root.glob("*.archive.json")
            archived = path.read_bytes()
            self.assertEqual((root / "attempt.json").read_bytes(), raw)
            with AttemptStore(root) as store:
                self.archive(store, self.api("failed")[0], digest)
                self.assertIsNone(store.load())
            self.assertEqual(path.read_bytes(), archived)

    def test_archive_publication_failure_preserves_active_record(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            raw, digest = self.seed(root)
            with AttemptStore(root) as store, patch("secs_inference.provider.attempt_store.os.link", side_effect=OSError("disk failure")):
                with self.assertRaises(JournalError):
                    self.archive(store, self.api()[0], digest)
            self.assertEqual((root / "attempt.json").read_bytes(), raw)
            self.assertFalse(list(root.glob("*.archive.json")))

    def test_invalid_reason_is_rejected_before_read(self):
        from secs_inference.provider.journal_archive import archive_closed
        for reason in ("", "   ", "x" * 2049, "bad\nreason"):
            with self.subTest(reason=reason[:20]), TemporaryDirectory() as temporary:
                root = Path(temporary) / "journal"
                raw, digest = self.seed(root)
                api, transport = self.api()
                with AttemptStore(root) as store, self.assertRaises(ValueError):
                    archive_closed(journal=store, api=api, owner={"origin": "https://api.example.test", "provider_ref": START.provider_ref}, execution_attempt_ref=ACTIVE.execution_attempt_ref,
                                   expected_record_digest=digest, reason=reason)
                transport.request.assert_not_called()
                self.assertEqual((root / "attempt.json").read_bytes(), raw)

    def test_archive_sync_failure_keeps_active_until_a_durable_retry(self):
        for failure_call in (1, 2):
            with self.subTest(failure_call=failure_call), TemporaryDirectory() as temporary:
                root = Path(temporary) / "journal"
                raw, digest = self.seed(root)
                import os
                original_fsync = os.fsync
                calls = 0
                def fail_once(fd):
                    nonlocal calls
                    calls += 1
                    if calls == failure_call:
                        raise OSError("sync failed")
                    return original_fsync(fd)
                with AttemptStore(root) as store, patch("secs_inference.provider.attempt_store.os.fsync", side_effect=fail_once):
                    with self.assertRaises(JournalError):
                        self.archive(store, self.api()[0], digest)
                self.assertEqual((root / "attempt.json").read_bytes(), raw)
                with AttemptStore(root) as store:
                    self.archive(store, self.api()[0], digest)
                    self.assertIsNone(store.load())

    def test_corrupt_existing_archive_is_not_overwritten_or_used(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            raw, digest = self.seed(root)
            with AttemptStore(root) as store, patch.object(store, "clear", side_effect=JournalError("interrupted")):
                with self.assertRaises(JournalError):
                    self.archive(store, self.api()[0], digest)
            path, = root.glob("*.archive.json")
            path.write_bytes(b'{"corrupt":true}')
            with AttemptStore(root) as store, self.assertRaises(JournalError):
                self.archive(store, self.api()[0], digest)
            self.assertEqual(path.read_bytes(), b'{"corrupt":true}')
            self.assertEqual((root / "attempt.json").read_bytes(), raw)

    def test_fresh_snapshot_outage_or_changed_record_does_not_retire(self):
        for changed in (False, True):
            with self.subTest(changed=changed), TemporaryDirectory() as temporary:
                root = Path(temporary) / "journal"
                raw, digest = self.seed(root)
                api, transport = self.api()
                response = transport.request.return_value
                def request(*args, **kwargs):
                    if not changed:
                        raise RuntimeError("API unavailable")
                    (root / "attempt.json").write_bytes(raw + b"\n")
                    return response
                transport.request.side_effect = request
                with AttemptStore(root) as store, self.assertRaises(RuntimeError):
                    self.archive(store, api, digest)
                self.assertEqual((root / "attempt.json").read_bytes(), raw + (b"\n" if changed else b""))
                self.assertFalse(list(root.glob("*.archive.json")))


    def test_cli_rejects_owner_drift_before_api_construction(self):
        from secs_inference.provider import journal_archive as module
        from types import SimpleNamespace
        config = SimpleNamespace(endpoint=SimpleNamespace(origin="https://other.example.test"))
        with patch.object(module, "_read_regular_file", return_value=b'{"origin":"https://api.example.test","provider_ref":"provider:test"}'), \
                patch.object(module, "decode_provider_config", return_value=config), \
                patch.object(module, "parse_provider_credential", return_value=SimpleNamespace(provider_ref="provider:test")), \
                patch.object(module, "ProviderApi") as api:
            with self.assertRaises(ValueError):
                module.configured_api()
            api.assert_not_called()

    def test_staging_cleanup_failure_preserves_primary_storage_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            raw, digest = self.seed(root)
            with AttemptStore(root) as store, \
                    patch("secs_inference.provider.attempt_store.os.link", side_effect=OSError("publication failed")), \
                    patch("secs_inference.provider.attempt_store.Path.unlink", side_effect=OSError("cleanup failed")), \
                    self.assertLogs("secs_inference.provider.attempt_store", level="ERROR") as logs:
                with self.assertRaisesRegex(JournalError, "durability") as caught:
                    self.archive(store, self.api()[0], digest)
            self.assertIn("publication failed", str(caught.exception.__cause__))
            self.assertIn("cleanup", " ".join(logs.output))
            self.assertEqual((root / "attempt.json").read_bytes(), raw)

    def test_cli_outputs_archival_facts_and_only_reads_api(self):
        from secs_inference.provider import journal_archive as module
        from contextlib import redirect_stdout
        from io import StringIO
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            _, digest = self.seed(root)
            api, transport = self.api()
            output = StringIO()
            owner = {"origin": "https://api.example.test", "provider_ref": START.provider_ref}
            with patch.object(module, "JOURNAL_PATH", root), patch.object(module, "configured_api", return_value=(api, owner)), redirect_stdout(output):
                self.assertEqual(module.main(["--execution-attempt-ref", ACTIVE.execution_attempt_ref,
                    "--expected-record-digest", digest, "--reason", "Reviewed closed Attempt."]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["delivery"], "unconfirmed")
            self.assertEqual(result["schema_id"], "secs.journal_archive_result.v1")
            self.assertEqual(json.loads(Path(result["archive_path"]).read_bytes())["owner"], owner)
            self.assertEqual(transport.request.call_args.args[0].name, "ATTEMPT")
            transport.request.assert_called_once()

    def test_each_closed_state_allows_archival_without_claiming_delivery(self):
        for state in ("succeeded", "failed", "expired"):
            with self.subTest(state=state), TemporaryDirectory() as temporary:
                root = Path(temporary) / "journal"
                _, digest = self.seed(root)
                with AttemptStore(root) as store:
                    result = self.archive(store, self.api(state)[0], digest)
                    self.assertIsNone(store.load())
                self.assertEqual(result["delivery"], "unconfirmed")

    def test_owner_origin_mismatch_refuses_before_read(self):
        from secs_inference.provider.journal_archive import archive_closed
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            raw, digest = self.seed(root)
            api, transport = self.api()
            with AttemptStore(root) as store, self.assertRaises(JournalError):
                archive_closed(journal=store, api=api,
                    owner={"origin": "https://wrong.example.test", "provider_ref": START.provider_ref},
                    execution_attempt_ref=ACTIVE.execution_attempt_ref, expected_record_digest=digest, reason="Investigated.")
            transport.request.assert_not_called()
            self.assertEqual((root / "attempt.json").read_bytes(), raw)
