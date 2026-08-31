from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
import json
import unittest

from secs_inference.provider.job_input import (
    JOB_SPECIFICATION_SCHEMA_ID,
    JobSpecification,
    JobInputError,
    MAX_JOB_INPUT_READ_RESPONSE_BYTES,
    SelectedJobInput,
    parse_job_input_read_response,
)


def _selected(exact: bytes) -> SelectedJobInput:
    return SelectedJobInput(
        job_ref="job:selected",
        input_schema_id=JOB_SPECIFICATION_SCHEMA_ID,
        input_fingerprint="sha256:" + sha256(exact).hexdigest(),
        input_byte_length=len(exact),
    )


def _response(exact: bytes, **changes: object) -> bytes:
    document = {
        "canonical_input_base64": b64encode(exact).decode("ascii"),
        "input_byte_length": len(exact),
        "input_fingerprint": "sha256:" + sha256(exact).hexdigest(),
        "input_schema_id": "nmr.job.specification.text.v1",
        "job_ref": "job:selected",
        "schema_id": "nmr.provider.job_input.read.response.v1",
    }
    return json.dumps(document | changes).encode()


class JobInputTests(unittest.TestCase):
    def assert_rejected(
        self,
        response: bytes,
        selected: SelectedJobInput,
    ) -> None:
        with self.assertRaises(JobInputError):
            parse_job_input_read_response(response, selected=selected)

    def test_returns_exact_text_when_response_bytes_match_the_feed(self) -> None:
        exact = "  Café\n\tFormula C2H6O  ".encode()
        selected = _selected(exact)

        specification = parse_job_input_read_response(
            _response(exact),
            selected=selected,
        )

        self.assertEqual(
            specification,
            JobSpecification("job:selected", exact.decode()),
        )
        self.assertNotIn("C2H6O", repr(specification))

    def test_rejects_the_wrong_job_or_measured_bytes(self) -> None:
        exact = b"Formula C2H6O"
        selected = _selected(exact)
        responses = (
            _response(exact, job_ref="job:other"),
            _response(exact + b" "),
            _response(b"x" * len(exact)),
        )
        for response in responses:
            with self.subTest(response=response):
                self.assert_rejected(response, selected)

    def test_the_feed_owns_how_input_bytes_are_interpreted(self) -> None:
        exact = b"Formula C2H6O"
        unsupported = SelectedJobInput(
            job_ref="job:selected",
            input_schema_id="nmr.job.binary.v1",
            input_fingerprint="sha256:" + sha256(exact).hexdigest(),
            input_byte_length=len(exact),
        )

        self.assert_rejected(_response(exact), unsupported)
        self.assertEqual(
            parse_job_input_read_response(
                _response(exact, input_schema_id="nmr.job.binary.v1"),
                selected=_selected(exact),
            ),
            JobSpecification("job:selected", exact.decode()),
        )

    def test_rejects_unreadable_response_or_input_text(self) -> None:
        exact = b"Formula C2H6O"
        selected = _selected(exact)
        self.assert_rejected(b"not JSON", selected)
        for hostile_json in (
            b"[" * 10_000 + b"]" * 10_000,
            b"1" + b"0" * 5_000,
        ):
            self.assert_rejected(hostile_json, selected)
        self.assert_rejected(
            _response(exact, schema_id="nmr.provider.other_response.v1"),
            selected,
        )
        self.assert_rejected(
            _response(exact, canonical_input_base64="a b"),
            selected,
        )

        non_utf8 = b"\xff"
        self.assert_rejected(
            _response(non_utf8),
            _selected(non_utf8),
        )

    def test_bounds_the_network_response(self) -> None:
        self.assert_rejected(
            b"x" * (MAX_JOB_INPUT_READ_RESPONSE_BYTES + 1),
            _selected(b"x"),
        )


if __name__ == "__main__":
    unittest.main()
