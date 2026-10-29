"""Science parses checked snapshots; transfer receipt drift remains operational."""

from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from provider_core.upload_file import UploadFileReadFailed, VerifiedUploadFile
from secs_inference.provider.worker_model import _read_upload_snapshots


class UploadSnapshotTests(unittest.TestCase):
    def command(self, root, contents):
        path = root / "upload"
        path.write_bytes(contents)
        receipt = VerifiedUploadFile("job:fixture", "upload:fixture", len(contents),
                                     "sha256:" + sha256(contents).hexdigest(), path.name)
        return {"directory": str(root), "receipts": {receipt.upload_ref: asdict(receipt)},
                "max_upload_bytes": 1024, "max_total_bytes": 1024}

    def read_and_assert_closed(self, command):
        opened = []
        original_open = os.open
        def record_open(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor
        try:
            with patch("os.open", side_effect=record_open):
                return _read_upload_snapshots(command)
        finally:
            self.assertEqual(len(opened), 2, "Expected one directory and one source open")
            for descriptor in opened:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_returned_bytes_remain_immutable_after_path_changes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = self.command(root, b"original")
            snapshots = self.read_and_assert_closed(command)
            (root / "upload").write_bytes(b"replacement")
            self.assertEqual(snapshots, {"upload:fixture": b"original"})

    def test_changed_length_and_same_length_hash_fail_before_parsing_and_close_readers(self):
        for replacement in (b"short", b"modified"):
            with self.subTest(replacement=replacement), TemporaryDirectory() as temporary:
                root = Path(temporary)
                command = self.command(root, b"original")
                (root / "upload").write_bytes(replacement)
                with self.assertRaises(UploadFileReadFailed):
                    self.read_and_assert_closed(command)

    def test_receipt_identity_and_aggregate_limit_fail_before_opening_sources(self):
        with TemporaryDirectory() as temporary:
            command = self.command(Path(temporary), b"original")
            for change in ({"max_total_bytes": 1}, {"receipts": {"upload:other": command["receipts"]["upload:fixture"]}}):
                with self.subTest(change=change), patch("os.open") as opened:
                    with self.assertRaises(ValueError):
                        _read_upload_snapshots(command | change)
                    opened.assert_not_called()
