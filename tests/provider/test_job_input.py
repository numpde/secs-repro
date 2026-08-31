from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
import unittest

from secs_inference.provider.canonical_json import canonical_json_bytes
from secs_inference.provider.job_input import (
    AdvertisedJobInput,
    JobInputError,
    MAX_JOB_INPUT_READ_RESPONSE_BYTES,
    MAX_JOB_SPECIFICATION_BYTES,
    VerifiedJobInput,
    parse_job_input_read_response,
)


def _advertised(exact: bytes, *, job_ref: str = "job:private_001") -> AdvertisedJobInput:
    return AdvertisedJobInput(
        job_ref=job_ref,
        input_schema_id="nmr.job.specification.text.v1",
        input_fingerprint="sha256:" + sha256(exact).hexdigest(),
        input_byte_length=len(exact),
    )


def _response(
    advertised: AdvertisedJobInput,
    exact: bytes,
    **changes: object,
) -> bytes:
    return canonical_json_bytes(
        {
            "canonical_input_base64": b64encode(exact).decode("ascii"),
            "input_byte_length": advertised.input_byte_length,
            "input_fingerprint": advertised.input_fingerprint,
            "input_schema_id": advertised.input_schema_id,
            "job_ref": advertised.job_ref,
            "schema_id": "nmr.provider.job_input.read.response.v1",
        }
        | changes
    )


class JobInputTests(unittest.TestCase):
    def assert_rejected(
        self,
        response: bytes,
        advertised: AdvertisedJobInput,
        reason: str,
    ) -> None:
        with self.assertRaises(JobInputError) as raised:
            parse_job_input_read_response(response, advertised=advertised)
        self.assertEqual(raised.exception.reason, reason)

    def test_binds_exact_utf8_text_to_the_advertised_identity(self) -> None:
        exact = "  Café\n\tFormula C2H6O  ".encode()
        advertised = _advertised(exact)

        verified = parse_job_input_read_response(
            _response(advertised, exact),
            advertised=advertised,
        )

        self.assertEqual(
            verified,
            VerifiedJobInput(
                job_ref=advertised.job_ref,
                input_fingerprint=advertised.input_fingerprint,
                input_byte_length=advertised.input_byte_length,
                specification_text=exact.decode(),
            ),
        )
        self.assertNotIn("C2H6O", repr(verified))

    def test_advertised_schema_id_requires_an_exact_string(self) -> None:
        class Text(str):
            pass

        exact = b"Formula C2H6O"
        with self.assertRaises(JobInputError) as raised:
            AdvertisedJobInput(
                job_ref="job:test",
                input_schema_id=Text("nmr.job.specification.text.v1"),
                input_fingerprint="sha256:" + sha256(exact).hexdigest(),
                input_byte_length=len(exact),
            )

        self.assertEqual(raised.exception.reason, "invalid_input_schema_id")

    def test_rejects_response_identity_drift(self) -> None:
        exact = b"Formula C2H6O"
        advertised = _advertised(exact)
        cases = (
            ({"job_ref": "job:other"}, "job_ref_mismatch"),
            (
                {"input_fingerprint": "sha256:" + "0" * 64},
                "input_fingerprint_mismatch",
            ),
            ({"input_byte_length": len(exact) - 1}, "input_byte_length_mismatch"),
        )
        for change, reason in cases:
            with self.subTest(reason=reason):
                self.assert_rejected(
                    _response(advertised, exact, **change),
                    advertised,
                    reason,
                )

    def test_rejects_bytes_that_do_not_match_the_advertised_input(self) -> None:
        exact = b"Formula C2H6O"
        advertised = _advertised(exact)
        cases = (
            (
                b64encode(exact + b" ").decode(),
                "decoded_input_byte_length_mismatch",
            ),
            (
                b64encode(b"x" * len(exact)).decode(),
                "decoded_input_fingerprint_mismatch",
            ),
        )
        for encoded, reason in cases:
            with self.subTest(reason=reason):
                self.assert_rejected(
                    _response(
                        advertised,
                        exact,
                        canonical_input_base64=encoded,
                    ),
                    advertised,
                    reason,
                )

    def test_rejects_malformed_envelopes_and_non_utf8_input(self) -> None:
        exact = b"Formula C2H6O"
        advertised = _advertised(exact)
        cases = (
            (b'{"schema_id": "x"}', "invalid_response_json"),
            (b"[" * 10_000 + b"]" * 10_000, "invalid_response_json"),
            (canonical_json_bytes({"schema_id": "x"}), "invalid_response_shape"),
            (
                _response(
                    advertised,
                    exact,
                    schema_id="nmr.provider.other_response.v1",
                ),
                "wrong_response_schema_id",
            ),
            (
                _response(advertised, exact, canonical_input_base64="a b"),
                "invalid_canonical_input_base64",
            ),
            (
                _response(advertised, exact, canonical_input_base64="AB=="),
                "noncanonical_input_base64",
            ),
        )
        for response, reason in cases:
            with self.subTest(reason=reason):
                self.assert_rejected(response, advertised, reason)

        non_utf8 = b"\xff"
        non_utf8_advertised = _advertised(non_utf8)
        self.assert_rejected(
            _response(non_utf8_advertised, non_utf8),
            non_utf8_advertised,
            "decoded_input_not_utf8",
        )

    def test_response_bound_contains_the_largest_released_input(self) -> None:
        self.assertEqual(MAX_JOB_SPECIFICATION_BYTES, 65_536)
        exact = b"a" * MAX_JOB_SPECIFICATION_BYTES
        advertised = _advertised(exact, job_ref="job:" + "a" * 124)
        response = _response(advertised, exact)

        self.assertLess(len(response), MAX_JOB_INPUT_READ_RESPONSE_BYTES)
        self.assertEqual(
            parse_job_input_read_response(
                response,
                advertised=advertised,
            ).specification_text,
            exact.decode(),
        )
        self.assert_rejected(
            b"x" * (MAX_JOB_INPUT_READ_RESPONSE_BYTES + 1),
            advertised,
            "response_too_large",
        )

    def test_verified_value_cannot_be_forged_with_different_text(self) -> None:
        exact = b"Formula C2H6O"
        advertised = _advertised(exact)

        with self.assertRaises(JobInputError) as raised:
            VerifiedJobInput(
                job_ref=advertised.job_ref,
                input_fingerprint=advertised.input_fingerprint,
                input_byte_length=advertised.input_byte_length,
                specification_text="Formula C3H8O",
            )

        self.assertEqual(
            raised.exception.reason,
            "specification_text_fingerprint_mismatch",
        )

    def test_errors_do_not_echo_private_input_or_identity(self) -> None:
        exact = b"private formula"
        advertised = _advertised(exact)
        with self.assertRaises(JobInputError) as raised:
            parse_job_input_read_response(
                _response(advertised, exact, job_ref="job:private_secret"),
                advertised=advertised,
            )

        self.assertNotIn("private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
