import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from secs_inference.spectra.jcamp import read_jcamp_spectrum
from secs_inference.spectra.secs import prepare_secs_spectrum


FIXTURES = Path("/fixtures")
JCAMP_SPECTRUM = (
    FIXTURES / "jcamp/4-chlorobenzylamine/4-chlorobenzylamine.jdx"
)
FRONTEND_REFERENCE = FIXTURES / "frontend/4-chlorobenzylamine.json"
NTUPLES_SPECTRUM = FIXTURES / "jcamp/ethylvinylether/1h.jdx"
NTUPLES_FRONTEND_REFERENCE = FIXTURES / "frontend/ethylvinylether.json"


class JcampFrontendReferenceTest(unittest.TestCase):
    def test_processed_xydata_matches_frontend_float32_input(self):
        reference = json.loads(FRONTEND_REFERENCE.read_text())
        source = read_jcamp_spectrum(JCAMP_SPECTRUM)
        actual = prepare_secs_spectrum(source)
        expected = np.asarray(reference["intensities"], dtype=np.float32)

        # Independent JavaScript and NumPy resampling may round the final
        # Float32 value in opposite directions, but not by more than one ULP.
        np.testing.assert_array_max_ulp(actual, expected, maxulp=1)

    def test_processed_ntuples_matches_frontend_float32_input(self):
        reference = json.loads(NTUPLES_FRONTEND_REFERENCE.read_text())
        source = read_jcamp_spectrum(NTUPLES_SPECTRUM)
        actual = prepare_secs_spectrum(source)
        expected = np.asarray(reference["intensities"], dtype=np.float32)

        np.testing.assert_array_max_ulp(actual, expected, maxulp=1)

    def test_ntuples_hz_axis_requires_a_chemical_shift_reference(self):
        contents = NTUPLES_SPECTRUM.read_text()
        contents = contents.replace("##$OFFSET= 11.00659\n", "", 1)

        with self.assertRaisesRegex(ValueError, "chemical-shift reference"):
            read_jcamp_spectrum(self._write_variant(contents))

    def test_ntuples_uses_the_standard_shift_reference_when_present(self):
        contents = NTUPLES_SPECTRUM.read_text()
        contents = contents.replace("##$OFFSET= 11.00659\n", "", 1)
        contents = contents.replace(
            "##.OBSERVE FREQUENCY= 400.112\n",
            "##.OBSERVE FREQUENCY= 400.112\n"
            "##.SHIFT REFERENCE=INTERNAL, TMS, 16384, 0\n",
            1,
        )

        source = read_jcamp_spectrum(self._write_variant(contents))

        self.assertEqual(source.ppm[-1], 0)
        self.assertAlmostEqual(source.ppm[0], 4807.69230769231 / 400.112)

    def test_time_domain_fid_is_not_mistaken_for_a_processed_spectrum(self):
        contents = JCAMP_SPECTRUM.read_text().replace(
            "##DATA TYPE=NMR SPECTRUM",
            "##DATA TYPE=NMR FID",
            1,
        )

        with self.assertRaises(ValueError):
            read_jcamp_spectrum(self._write_variant(contents))

    def test_multiple_data_blocks_are_not_selected_silently(self):
        contents = JCAMP_SPECTRUM.read_text()

        with self.assertRaises(ValueError):
            read_jcamp_spectrum(self._write_variant(contents + contents))

    def test_xydata_checkpoint_must_agree_with_the_declared_axis(self):
        contents = JCAMP_SPECTRUM.read_text().replace(
            "12.000000 115",
            "999.000000 115",
            1,
        )

        with self.assertRaises(ValueError):
            read_jcamp_spectrum(self._write_variant(contents))

    def test_ntuples_is_not_admitted_as_xydata(self):
        contents = JCAMP_SPECTRUM.read_text().replace(
            "##DATA CLASS=XYDATA",
            "##DATA CLASS=NTUPLES",
            1,
        )

        with self.assertRaises(ValueError):
            read_jcamp_spectrum(self._write_variant(contents))

    def test_non_proton_spectrum_is_rejected(self):
        contents = JCAMP_SPECTRUM.read_text().replace(
            "##.OBSERVE NUCLEUS=^1H",
            "##.OBSERVE NUCLEUS=^13C",
            1,
        )

        with self.assertRaises(ValueError):
            read_jcamp_spectrum(self._write_variant(contents))

    def test_non_ppm_axis_is_rejected(self):
        contents = JCAMP_SPECTRUM.read_text().replace(
            "##XUNITS=PPM",
            "##XUNITS=HZ",
            1,
        )

        with self.assertRaises(ValueError):
            read_jcamp_spectrum(self._write_variant(contents))

    def _write_variant(self, contents: str) -> Path:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        path = Path(temporary_directory.name) / "spectrum.jdx"
        path.write_text(contents)
        return path
