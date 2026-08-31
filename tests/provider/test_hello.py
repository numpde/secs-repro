from pathlib import Path
from unittest.mock import patch
import unittest

from secs_inference.provider.analysis import (
    ADMISSIBLE_SPECTRUM_FORMATS,
    analysis_offering_description,
)
from secs_inference.provider.canonical_json import parse_canonical_json_bytes
from secs_inference.provider.config import decode_provider_config
from secs_inference.provider.hello import (
    AnalysisOffering,
    HelloReceiptRejected,
    HelloReceiptRejection,
    parse_hello_receipt,
    prepare_hello,
)
from secs_inference.provider.main import prepare_configured_hello, run_provider


CONFIG = Path(__file__).parents[2] / "config/provider.toml.example"


class ProviderHelloTests(unittest.TestCase):
    def test_offering_description_is_rendered_from_the_format_inventory(self):
        description = analysis_offering_description()

        self.assertEqual(
            ADMISSIBLE_SPECTRUM_FORMATS,
            (
                "a Bruker processed pdata directory",
                "a JCAMP-DX file containing one processed 1H NMR AFFN "
                "XYDATA block with a ppm axis",
            ),
        )
        for spectrum_format in ADMISSIBLE_SPECTRUM_FORMATS:
            self.assertIn(spectrum_format, description)
        self.assertIn("molecular formula", description)
        self.assertIn("not validated structure assignments", description)

    def test_configured_hello_contains_the_new_analysis_kind_and_format_text(self):
        config = decode_provider_config(CONFIG.read_bytes())

        prepared = prepare_configured_hello(config)
        document = parse_canonical_json_bytes(prepared.body)

        self.assertEqual(document["schema_id"], "nmr.provider.hello_request.v1")
        self.assertEqual(
            document["analysis_offerings"],
            [
                {
                    "analysis_kind_ref": "mol_from_1h_spectrum_formula",
                    "description": analysis_offering_description(),
                }
            ],
        )

    def test_config_rejects_a_non_text_api_topology(self):
        invalid = CONFIG.read_bytes().replace(
            b'topology = "web"',
            b'topology = ["web"]',
        )

        with self.assertRaises(ValueError):
            decode_provider_config(invalid)

    def test_runtime_rejects_invalid_presentation_before_reading_the_credential(self):
        invalid = CONFIG.read_bytes().replace(
            b'display_name = "SECS"',
            b'display_name = ""',
        )

        with (
            patch(
                "secs_inference.provider.main._read_regular_file",
                side_effect=(invalid, AssertionError("credential was read")),
            ) as read,
            self.assertRaisesRegex(ValueError, "provider display name"),
        ):
            run_provider(CONFIG)

        read.assert_called_once()

    def test_hello_rejects_a_non_text_analysis_kind_at_its_boundary(self):
        with self.assertRaisesRegex(TypeError, "analysis kind must be a string"):
            prepare_hello(
                display_name="Provider",
                description="Description",
                analysis_offerings=(AnalysisOffering(7, "Description"),),
            )

    def test_receipt_from_another_provider_is_rejected(self):
        receipt = parse_hello_receipt(
            b'{"accepted_at":"2026-08-31T12:34:56Z",'
            b'"provider_ref":"provider:other",'
            b'"schema_id":"nmr.provider.hello_response.v1"}',
            expected_provider_ref="provider:secs",
        )

        self.assertEqual(
            receipt,
            HelloReceiptRejected(HelloReceiptRejection.RESPONSE_DRIFT),
        )


if __name__ == "__main__":
    unittest.main()
