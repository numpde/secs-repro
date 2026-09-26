"""Support projections and evidence accounting have one strict owner."""

import io
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


TOOLS = Path("/tools")
if not TOOLS.is_dir():
    TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

from check_support_catalog_boundary import unexpected_catalog_importers  # noqa: E402
from project_support_catalog import project_support_region  # noqa: E402
from secs_inference.provider.analysis import analysis_offering_description  # noqa: E402
from secs_inference.provider.support_catalog import (  # noqa: E402
    SUPPORT_CLAIMS,
    SupportCategory,
)
from support_evidence import qualification_evidence  # noqa: E402
from support_coverage import (  # noqa: E402
    EvidenceResult,
    compare_evidence,
    declared_evidence,
)


class SupportCatalogTests(unittest.TestCase):
    def test_claims_are_complete_plain_text_records(self):
        self.assertTrue(SUPPORT_CLAIMS)
        self.assertEqual({claim.category for claim in SUPPORT_CLAIMS}, set(SupportCategory))
        for claim in SUPPORT_CLAIMS:
            with self.subTest(claim=claim.title):
                for text in (claim.title, claim.public_summary, *claim.limits):
                    self.assertEqual(text, text.strip())
                    self.assertTrue(text.isprintable())
                    self.assertNotIn('<', text)
                    self.assertNotIn('>', text)
                self.assertTrue(claim.required_evidence)
                self.assertEqual(
                    len(claim.required_evidence), len(set(claim.required_evidence))
                )

    def test_offering_projects_every_advertised_claim(self):
        description = analysis_offering_description()

        for claim in SUPPORT_CLAIMS:
            if claim.advertise_in_offering:
                self.assertIn(claim.public_summary, description)
        self.assertLessEqual(len(description.encode("utf-8")), 1024)


class SupportProjectionTests(unittest.TestCase):
    def test_projection_replaces_only_one_bounded_region_and_is_idempotent(self):
        original = (
            "before\n"
            "<!-- support-catalog:start -->\nold\n<!-- support-catalog:end -->\n"
            "after\n"
        )

        projected = project_support_region(original)

        self.assertTrue(projected.startswith("before\n<!-- support-catalog:start -->\n"))
        self.assertTrue(projected.endswith("<!-- support-catalog:end -->\nafter\n"))
        self.assertEqual(project_support_region(projected), projected)

    def test_projection_requires_exactly_one_complete_region(self):
        for document in (
            "no markers",
            "<!-- support-catalog:start -->\nmissing end",
            "<!-- support-catalog:end -->\nmissing start",
            "<!-- support-catalog:start --><!-- support-catalog:end -->"
            "<!-- support-catalog:start --><!-- support-catalog:end -->",
        ):
            with self.subTest(document=document), self.assertRaises(ValueError):
                project_support_region(document)

    def test_production_catalog_imports_are_limited_to_the_presentation_owner(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "provider").mkdir()
            (root / "provider/analysis.py").write_text(
                "from secs_inference.provider.support_catalog import SUPPORT_CLAIMS\n"
            )
            (root / "provider/input_formats.py").write_text(
                "from .support_catalog import SUPPORT_CLAIMS\n"
            )
            (root / "spectra").mkdir()
            (root / "spectra/jcamp.py").write_text(
                "from ..provider.support_catalog import SUPPORT_CLAIMS\n"
            )

            self.assertEqual(
                unexpected_catalog_importers(root),
                [Path("provider/input_formats.py"), Path("spectra/jcamp.py")],
            )


class SupportEvidenceTests(unittest.TestCase):
    def test_only_successful_tests_contribute_semantic_evidence(self):
        class Cases(unittest.TestCase):
            @qualification_evidence("example.passes.v1")
            def test_passes(self):
                pass

            @qualification_evidence("example.fails.v1")
            def test_fails(self):
                self.fail("expected failure for the result probe")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Cases)
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=EvidenceResult).run(suite)

        self.assertEqual(result.passed_evidence, {"example.passes.v1"})

    def test_coverage_rejects_missing_stale_and_duplicate_evidence(self):
        required = "required.v1"
        stale = "stale.v1"
        cases = (
            ({required}, {required}, set(), "did not pass"),
            ({required}, {stale}, {stale}, "missing declaration"),
            (set(), {stale}, {stale}, "not required"),
        )
        for required, declared, passed, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                compare_evidence(required=required, declared=declared, passed=passed)

        class DuplicateCases(unittest.TestCase):
            @qualification_evidence("example.duplicate.v1")
            def test_first(self):
                pass

            @qualification_evidence("example.duplicate.v1")
            def test_second(self):
                pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(DuplicateCases)
        with self.assertRaisesRegex(ValueError, "more than one test"):
            declared_evidence(suite)


if __name__ == "__main__":
    unittest.main()
