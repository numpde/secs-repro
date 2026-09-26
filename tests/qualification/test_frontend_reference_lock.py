import json
from pathlib import Path
import tempfile
import unittest

from frontend_reference_lock import (
    IMAGE_INPUTS,
    load_lock,
    lock_id,
    producer_id,
    verify_oracle,
)


VALID_LOCK = {
    "schema_id": "secs.frontend-reference-lock.v1",
    "repository": "https://example.test/reference",
    "revision": "1" * 40,
    "node_image": f"registry.test/node@sha256:{'2' * 64}",
}


class FrontendReferenceLockTest(unittest.TestCase):
    def test_rejects_missing_unknown_and_malformed_authority(self):
        cases = (
            {key: value for key, value in VALID_LOCK.items() if key != "revision"},
            {**VALID_LOCK, "copied_source_hashes": {}},
            {**VALID_LOCK, "revision": "main"},
            {**VALID_LOCK, "node_image": "node:latest"},
            {**VALID_LOCK, "repository": "git@example.test:reference"},
            {**VALID_LOCK, "revision": None},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock.json"
            for document in cases:
                with self.subTest(document=document):
                    path.write_text(json.dumps(document))
                    with self.assertRaises(ValueError):
                        load_lock(path)

    def test_identity_covers_exact_lock_and_producer_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative_path in IMAGE_INPUTS:
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(VALID_LOCK) if path.name.endswith(".json") else relative_path.as_posix())
            document, contents = load_lock(root / IMAGE_INPUTS[0])
            self.assertEqual(document, VALID_LOCK)
            self.assertRegex(lock_id(contents), r"^sha256:[0-9a-f]{64}$")
            original = producer_id(root, "frontend")
            producer = root / "tools/generate_frontend_reference.ts"
            producer.write_text(producer.read_text() + "\nchanged")
            self.assertNotEqual(producer_id(root, "frontend"), original)

    def test_oracle_must_match_both_lock_and_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "lock.json"
            lock.write_text(json.dumps(VALID_LOCK))
            expected_producer = f"sha256:{'3' * 64}"
            oracle = root / "oracle.json"
            valid = {
                "reference_lock": lock_id(lock.read_bytes()),
                "reference_build": expected_producer,
            }
            verify_oracle(lock, expected_producer, self.write_json(oracle, valid))
            for field in ("reference_lock", "reference_build"):
                with self.subTest(field=field):
                    stale = dict(valid)
                    stale[field] = f"sha256:{'0' * 64}"
                    with self.assertRaisesRegex(ValueError, "different reference"):
                        verify_oracle(lock, expected_producer, self.write_json(oracle, stale))

    @staticmethod
    def write_json(path, document):
        path.write_text(json.dumps(document))
        return path


if __name__ == "__main__":
    unittest.main()
