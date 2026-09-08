"""The interpreter's explicit source choice reaches the existing real readers."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import numpy as np

from secs_inference.provider.input_operations import BrukerSelection, JcampSelection, SourceRef
from secs_inference.provider.source_access import SourceAccess
from secs_inference.provider.spectrum_input import prepare_selected_spectrum
from secs_inference.spectra.bruker import read_bruker_pdata
from secs_inference.spectra.errors import SpectrumReadError
from secs_inference.spectra.jcamp import read_jcamp_spectrum
from secs_inference.spectra.secs import prepare_secs_spectrum
from secs_inference.spectra.source import SourceSpectrum


FIXTURES = Path("/fixtures")
PDATA = FIXTURES / "bruker/F3697/1/pdata/1"
JCAMP = FIXTURES / "jcamp/4-chlorobenzylamine/4-chlorobenzylamine.jdx"


class SelectedSpectrumTests(unittest.TestCase):
    def test_explicit_jcamp_member_matches_the_direct_reader(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "upload"
            with ZipFile(path, "w") as archive:
                archive.writestr("distractor.txt", "not a spectrum")
                archive.write(JCAMP, "chosen/spectrum.jdx")
            access = SourceAccess({"upload:chosen": path}, root)
            selection = JcampSelection(SourceRef("upload:chosen", "chosen/spectrum.jdx"), "C7H8ClN", "The chosen member is the proton spectrum.")
            actual = prepare_selected_spectrum(access, selection)
            np.testing.assert_array_equal(actual, prepare_secs_spectrum(read_jcamp_spectrum(JCAMP)))

    def test_bruker_reader_uses_only_the_chosen_processed_pair(self):
        for prefix in ("", "chosen/./pdata/7", "chosen//pdata/7"):
            with self.subTest(prefix=prefix), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "upload"
                with ZipFile(path, "w") as archive:
                    archive.writestr("other/pdata/1/procs", "not selected")
                    for name in ("1r", "procs"):
                        archive.writestr(prefix + "/" + name if prefix else name, (PDATA / name).read_bytes())
                access = SourceAccess({"upload:chosen": path}, root)
                selection = BrukerSelection("upload:chosen", prefix, "C2H6O", "The selected experiment is proton.")
                np.testing.assert_array_equal(
                    prepare_selected_spectrum(access, selection),
                    prepare_secs_spectrum(read_bruker_pdata(PDATA)),
                )

    def test_bruker_decoding_establishes_nucleus_dimension_and_point_count(self):
        for substitution in (("##$AXNUC= <1H>", "##$AXNUC= <13C>"), ("##$PPARMOD= 0", "##$PPARMOD= 1"),
                             ("##$OFFSET= 14.01234", "##$OFFSET= nan"), None):
            with self.subTest(substitution=substitution), TemporaryDirectory() as directory:
                root = Path(directory)
                parameters = (PDATA / "procs").read_text()
                if substitution is not None:
                    self.assertIn(substitution[0], parameters)
                    parameters = parameters.replace(*substitution)
                (root / "procs").write_text(parameters)
                data = (PDATA / "1r").read_bytes()
                (root / "1r").write_bytes(data if substitution else data[:32])
                with self.assertRaises(SpectrumReadError) as caught:
                    read_bruker_pdata(root)
                if substitution and "OFFSET" in substitution[0]:
                    self.assertIn("OFFSET must be finite", str(caught.exception))

    def test_unusable_spectrum_feedback_names_the_window_that_needs_variation(self):
        source = SourceSpectrum(np.array([-2., 10.]), np.zeros(2))
        with self.assertRaises(SpectrumReadError) as caught:
            prepare_secs_spectrum(source)
        self.assertIn("from -2 to 10 ppm", str(caught.exception))
        self.assertIn("variation", str(caught.exception))

    def test_preparation_bug_stays_operational(self):
        with TemporaryDirectory() as directory:
            access = SourceAccess({"upload:chosen": JCAMP}, Path(directory))
            selection = JcampSelection(SourceRef("upload:chosen"), "C7H8ClN", "Direct proton spectrum.")
            with patch("secs_inference.provider.spectrum_input.prepare_secs_spectrum", side_effect=ValueError("program fault")):
                with self.assertRaisesRegex(ValueError, "program fault") as caught:
                    prepare_selected_spectrum(access, selection)
            self.assertNotIsInstance(caught.exception, SpectrumReadError)
