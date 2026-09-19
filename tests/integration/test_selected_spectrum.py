"""The interpreter's explicit source choice reaches the existing real readers."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import numpy as np

from secs_inference.provider.input_operations import BrukerSelection, JcampSelection, SourceRef
from secs_inference.provider.source_access import SourceAccess
from secs_inference.provider.spectrum_input import prepare_selected_spectrum
from secs_inference.provider.worker_model import ScientificHandler, ScientificWorkerConfig
from secs_inference.spectra.bruker import read_bruker_pdata
from secs_inference.spectra.errors import SpectrumReadError
from secs_inference.spectra.jcamp import read_jcamp_spectrum
from secs_inference.spectra.secs import prepare_secs_spectrum
from secs_inference.spectra.source import SourceSpectrum


FIXTURES = Path("/fixtures")
PDATA = FIXTURES / "bruker/F3697/1/pdata/1"
JCAMP = FIXTURES / "jcamp/4-chlorobenzylamine/4-chlorobenzylamine.jdx"


class SelectedSpectrumTests(unittest.TestCase):
    def test_empty_or_nonfinite_optimizer_output_is_a_scientific_fault(self):
        from secs.elucidation import OptimizerResult, StaticCandidateSource

        for population, reason in (([], "empty population"), ([("CCO", float("nan"))], "nonfinite"),
                                   ([("CCO", float("inf"))], "nonfinite"), ([("CCO", float("-inf"))], "nonfinite")):
            with self.subTest(reason=reason, population=population), TemporaryDirectory() as directory:
                inference = Mock(embed_spectrum=Mock(return_value=np.array([1., 0.], dtype=np.float32)))
                worker = ScientificHandler(inference, StaticCandidateSource(["CCO"]),
                    ScientificWorkerConfig("unused", "unused", device="cpu"))
                with patch("secs.elucidation.GraphGAOptimizer.run", return_value=OptimizerResult(population=population)):
                    with self.assertRaisesRegex(RuntimeError, reason):
                        worker({"operation": "analyse", "files": {"upload:chosen": str(JCAMP)},
                            "directory": directory, "selection": {"reader": "jcamp",
                            "source": {"upload_ref": "upload:chosen", "member": None}, "formula": "C2H6O",
                            "explanation": "The selected file is the proton spectrum."}})

    def test_candidate_retrieval_bug_remains_an_operational_failure(self):
        inference = Mock(embed_spectrum=Mock(return_value=np.array([1., 0.], dtype=np.float32)))
        failure = ValueError("private retrieval detail")
        candidates = Mock(propose=Mock(side_effect=failure))
        worker = ScientificHandler(inference, candidates, ScientificWorkerConfig("unused", "unused", device="cpu"))
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError) as caught:
                worker({"operation": "analyse", "files": {"upload:chosen": str(JCAMP)},
                    "directory": directory, "selection": {"reader": "jcamp",
                    "source": {"upload_ref": "upload:chosen", "member": None}, "formula": "C7H8ClN",
                    "explanation": "The selected file is the proton spectrum."}})
        self.assertIs(caught.exception, failure)

    def test_retrieval_counts_distinguish_empty_index_formula_rejection_and_population_limit(self):
        import faiss
        from secs.elucidation import FaissCandidateSource, OptimizerResult

        for formulas, outcome, matches, starting in (
            ([], "no_starting_candidates", 0, 0),
            (["C30H62"], "no_starting_candidates", 0, 0),
            (["C7H8ClN"] * 3, "analysed", 3, 2),
        ):
            with self.subTest(formulas=formulas), TemporaryDirectory() as directory:
                inference = Mock(embed_spectrum=Mock(return_value=np.array([1., 0.], dtype=np.float32)))
                index = faiss.IndexFlatIP(2)
                index.add(np.array([[1., 0.]] * len(formulas), dtype=np.float32).reshape(-1, 2))
                candidates = FaissCandidateSource(index, ["NCc1ccc(Cl)cc1"] * len(formulas), formulas, n_neighbours=8)
                worker = ScientificHandler(inference, candidates,
                    ScientificWorkerConfig("unused", "unused", device="cpu", neighbours=8, initial_population_size=2))
                optimized = OptimizerResult(population=[("NCc1ccc(Cl)cc1", 1.0)], generations=1, n_evaluated=2)
                with patch("secs.elucidation.GraphGAOptimizer.run", return_value=optimized) as refine:
                    response = worker({"operation": "analyse", "files": {"upload:chosen": str(JCAMP)},
                        "directory": directory, "selection": {"reader": "jcamp",
                        "source": {"upload_ref": "upload:chosen", "member": None}, "formula": "C7H8ClN",
                        "explanation": "The selected file is the proton spectrum."}})
                self.assertEqual(response["outcome"], outcome)
                analysis = response["analysis"]
                self.assertEqual(analysis["retrieval"], {"index_size": len(formulas), "neighbours_returned": len(formulas),
                    "formula_matches": matches, "starting_candidates": starting})
                search = analysis["search"]
                if starting:
                    refine.assert_called_once()
                    self.assertEqual(len(refine.call_args.args[0]), starting)
                    self.assertEqual(search["outcome"], "optimized")
                    self.assertEqual(analysis["candidates"], [{"smiles": "NCc1ccc(Cl)cc1", "score": "1.0"}])
                else:
                    refine.assert_not_called()
                    self.assertEqual(analysis["candidates"], [])
                    self.assertEqual(search["outcome"], "no_starting_candidates")
                    self.assertEqual((search["generations"], search["evaluated"]), (0, 0))
                    self.assertIn("Graph GA was not run", search["explanation"])
                    self.assertIn("does not establish", search["explanation"])

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
