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

    def test_ntuples_encoded_rows_preserve_the_axis_and_values(self):
        encodings = {
            "AFFN": "10 1 2 3\n7 4 5 6",
            "PAC": "10+1+2+3\n7+4+5+6",
            "SQZ": "10ABC\n7DEF",
            "DIF": "10AJJ\n8CJJJ\n5F",
            "DIFDUP": "10AJT\n8CJU\n5F",
            "DIF to SQZ": "10AJJ\n8CDE\n5F",
            "SQZ to DIF": "10ABC\n7DJJ\n5F",
        }
        for encoding, rows in encodings.items():
            with self.subTest(encoding=encoding):
                source = read_jcamp_spectrum(self._ntuples(rows))
                np.testing.assert_array_equal(source.ppm, [5, 4.5, 4, 3.5, 3, 2.5])
                np.testing.assert_array_equal(source.intensities, [1, 2, 3, 4, 5, 6])

    def test_ntuples_rejects_wrong_row_positions_and_difference_checkpoints(self):
        corruptions = {
            "first X": "99AJT\n8CJU\n5F",
            "later X": "10AJT\n9CJU\n5F",
            "final X": "10AJT\n8CJU\n4F",
            "repeated Y": "10AJT\n8DJU\n5F",
            "final Y": "10AJT\n8CJU\n5G",
            "missing final checkpoint": "10AJT\n8CJU",
        }
        for fault, rows in corruptions.items():
            for channel in ("R", "I"):
                with self.subTest(fault=fault, channel=channel):
                    with self.assertRaisesRegex(ValueError, "checkpoint"):
                        read_jcamp_spectrum(self._ntuples(rows, channel=channel))

    def test_ntuples_does_not_silently_drop_numeric_row_text(self):
        with self.assertRaisesRegex(ValueError, "text the decoder would ignore"):
            read_jcamp_spectrum(self._ntuples("10 1 garbage 2 3\n7 4 5 6"))

    def test_ntuples_does_not_misread_a_later_change_to_compressed_rows(self):
        # nmrglue's numeric decoder would turn the later SQZ values 44, 65, 86
        # into 4, 5, 6 without changing the point count.
        with self.assertRaisesRegex(ValueError, "text the decoder would ignore"):
            read_jcamp_spectrum(self._ntuples("10 1 2 3\n7D4F5H6"))

    def test_compressed_checkpoint_is_not_mistaken_for_an_exponent(self):
        for rows, expected in (
            ("10D9JJ\n8E1JJJ\n5E4", [49, 50, 51, 52, 53, 54]),
            ("10d9jj\n8e1jjj\n5e4", [-49, -50, -51, -52, -53, -54]),
        ):
            with self.subTest(rows=rows):
                source = read_jcamp_spectrum(self._ntuples(rows))
                np.testing.assert_array_equal(source.intensities, expected)

    def test_fractional_dif_checkpoint_allows_only_arithmetic_roundoff(self):
        path = self._ntuples("10A.1%.1%.1\n8A.3%.1%.1%.1\n5A.6")
        source = read_jcamp_spectrum(path)
        np.testing.assert_allclose(
            source.intensities, [1.1, 1.2, 1.3, 1.4, 1.5, 1.6],
            rtol=0, atol=1e-14,
        )

        contents = path.read_text().replace("8A.3", "8A.30001", 1)
        with self.assertRaisesRegex(ValueError, "Y checkpoint"):
            read_jcamp_spectrum(self._write_variant(contents))

    def test_dif_checkpoint_roundoff_handles_cancellation_near_zero(self):
        source = read_jcamp_spectrum(self._ntuples("10A.1J.1J.1l.3\n7@JJ\n5B"))
        np.testing.assert_allclose(
            source.intensities, [1.1, 2.2, 3.3, 0, 1, 2], rtol=0, atol=1e-14,
        )

    def test_large_dif_ordinates_do_not_hide_a_one_unit_checkpoint_error(self):
        path = self._ntuples("10E00000000JJ\n8E00000002JJJ\n5E00000005")
        source = read_jcamp_spectrum(path)
        np.testing.assert_array_equal(source.intensities, np.arange(500000000, 500000006))
        contents = path.read_text().replace("8E00000002", "8E00000003", 1)
        with self.assertRaisesRegex(ValueError, "Y checkpoint"):
            read_jcamp_spectrum(self._write_variant(contents))

    def test_extreme_x_exponents_cannot_disable_checkpoint_checks(self):
        for token in ("0e500", "1e999999999", "0e999999999999999999999"):
            xydata = JCAMP_SPECTRUM.read_text().replace(
                "12.000000 115", f"{token} 115", 1,
            )
            for path in (self._write_variant(xydata), self._ntuples(f"{token} 1 2 3\n7 4 5 6")):
                with self.subTest(token=token, path=path):
                    with self.assertRaisesRegex(ValueError, "X checkpoint"):
                        read_jcamp_spectrum(path)

    def test_ntuples_factor_cannot_overflow_checkpoint_value_or_precision(self):
        for token in ("10", "0e100"):
            path = self._ntuples(f"{token} 1 2 3\n7 4 5 6")
            contents = path.read_text().replace("##FACTOR=0.5,", "##FACTOR=1e308,", 1)
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "FACTOR scaling"):
                    read_jcamp_spectrum(self._write_variant(contents))

    def _ntuples(self, rows: str, *, channel: str = "R") -> Path:
        real_rows = rows if channel == "R" else "10ABC\n7DEF"
        imaginary_rows = rows if channel == "I" else "10ABC\n7DEF"
        return self._write_variant(
            "##TITLE=Checkpoint example\n"
            "##JCAMP-DX=5.00\n"
            "##DATA TYPE=NMR SPECTRUM\n"
            "##DATA CLASS=NTUPLES\n"
            "##.OBSERVE NUCLEUS=^1H\n"
            "##NTUPLES=NMR SPECTRUM\n"
            "##SYMBOL=X,R,I,N\n"
            "##VAR_DIM=6,6,6,2\n"
            "##UNITS=PPM,ARBITRARY UNITS,ARBITRARY UNITS,\n"
            "##FIRST=5,1,1,1\n"
            "##LAST=2.5,6,6,2\n"
            "##FACTOR=0.5,1,1,1\n"
            "##PAGE=N=1\n"
            f"##DATA TABLE=(X++(R..R)), XYDATA\n{real_rows}\n"
            "##PAGE=N=2\n"
            f"##DATA TABLE=(X++(I..I)), XYDATA\n{imaginary_rows}\n"
            "##END NTUPLES=NMR SPECTRUM\n"
            "##END=\n",
        )

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
