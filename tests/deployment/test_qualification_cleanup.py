"""Qualification cleanup must never adopt a container by its intended name."""

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


class QualificationCleanupTests(unittest.TestCase):
    def run_cleanup(self, directory, receipt, *, inspect_status=0, inspect_message="", remove_status=0):
        script = ('source tools/qualification_cleanup.sh; '
                  'qualification_docker() { printf "%s\\n" "$*" >> "$RECEIPT"; '
                  'if [[ $1 == container ]]; then printf "%s" "$INSPECT_MESSAGE" >&2; return "$INSPECT_STATUS"; fi; '
                  'return "$REMOVE_STATUS"; }; '
                  'retire_qualification_containers "$1"')
        env = os.environ | {"RECEIPT": str(receipt), "INSPECT_STATUS": str(inspect_status),
                            "INSPECT_MESSAGE": inspect_message, "REMOVE_STATUS": str(remove_status)}
        return subprocess.run(["bash", "-c", script, "bash", str(directory)], cwd=ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_missing_cid_after_name_conflict_never_removes_foreign_container(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "extractor.started").touch()
            receipt = directory / "calls"
            result = self.run_cleanup(directory, receipt)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(receipt.exists())
            self.assertIn(b"identity is unconfirmed", result.stderr)

    def test_only_exact_issued_container_id_is_removed(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "extractor.started").touch()
            cid = "a" * 64
            (directory / "extractor.cid").write_text(cid + "\n")
            receipt = directory / "calls"
            result = self.run_cleanup(directory, receipt)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(receipt.read_text().splitlines(), ["container inspect " + cid, "rm -f " + cid])

    def test_inspection_failure_does_not_remove_container(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "extractor.started").touch()
            cid = "b" * 64
            (directory / "extractor.cid").write_text(cid)
            receipt = directory / "calls"
            result = self.run_cleanup(directory, receipt, inspect_status=1)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt.read_text().splitlines(), ["container inspect " + cid])

    def test_missing_container_is_already_retired(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "extractor.started").touch()
            cid = "c" * 64
            (directory / "extractor.cid").write_text(cid)
            receipt = directory / "calls"
            result = self.run_cleanup(directory, receipt, inspect_status=1, inspect_message="No such object")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(receipt.read_text().splitlines(), ["container inspect " + cid])

    def test_invalid_cid_and_failed_removal_remain_unconfirmed(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "extractor.started").touch()
            cid_file = directory / "extractor.cid"
            receipt = directory / "calls"
            cid_file.write_text("d" * 64 + "\nforeign\n")
            self.assertNotEqual(self.run_cleanup(directory, receipt).returncode, 0)
            self.assertFalse(receipt.exists())
            cid_file.write_text("d" * 64)
            self.assertNotEqual(self.run_cleanup(directory, receipt, remove_status=1).returncode, 0)


if __name__ == "__main__":
    unittest.main()
