"""A discovered representation reaches the real reader and scientific search."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import numpy as np

from secs_inference.provider.input_adapter import InputAdapter
from secs_inference.provider.input_operations import SourceRef
from secs_inference.provider.source_access import SourceAccess
from secs_inference.provider.worker_model import ScientificHandler, ScientificWorkerConfig
from secs_inference.spectra.bruker import read_bruker_pdata
from secs_inference.spectra.errors import SpectrumReadError
from secs_inference.spectra.secs import prepare_secs_spectrum
from secs_inference.spectra.source import SourceSpectrum


FIXTURES = Path("/fixtures")
PDATA = FIXTURES / "bruker/F3697/1/pdata/1"
JCAMP = FIXTURES / "jcamp/4-chlorobenzylamine/4-chlorobenzylamine.jdx"
ATTEMPT = "execution_attempt:sha256:" + "1" * 64


def _selection(handler, files, directory, formula="C7H8ClN"):
    inspected = handler({"operation": "inspect", "files": files, "directory": str(directory),
                         "attempt_ref": ATTEMPT,
                         "source": {"upload_ref": "upload:chosen", "member": None}})
    choices = [item for item in inspected["facts"]["representations"]
               if item["kind"] == "spectrum" and item["metadata"].get("nucleus") == "1H"]
    if len(choices) != 1:
        raise AssertionError(f"Expected one proton spectrum, found {len(choices)}")
    return {"representation_id": choices[0]["id"], "formula": formula,
            "formula_evidence": {"kind": "job_specification"}, "processing": "as_stored",
            "explanation": "The discovered representation is the requested proton spectrum."}


class SelectedSpectrumTests(unittest.TestCase):
    def handler(self, candidates=None):
        inference = Mock(embed_spectrum=Mock(return_value=np.array([1., 0.], dtype=np.float32)))
        if candidates is None:
            from secs.elucidation import StaticCandidateSource
            candidates = StaticCandidateSource(["NCc1ccc(Cl)cc1"])
        return ScientificHandler(inference, candidates,
            ScientificWorkerConfig("unused", "unused", device="cpu", neighbours=8,
                                   initial_population_size=2))

    def request(self, handler, directory, selection):
        return handler({"operation": "analyse", "files": {"upload:chosen": str(JCAMP)},
                        "directory": str(directory), "attempt_ref": ATTEMPT,
                        "selection": selection})

    def test_empty_or_nonfinite_optimizer_output_is_a_scientific_fault(self):
        from secs.elucidation import OptimizerResult

        for population, reason in (([], "empty population"), ([("CCO", float("nan"))], "nonfinite"),
                                   ([("CCO", float("inf"))], "nonfinite"),
                                   ([("CCO", float("-inf"))], "nonfinite")):
            with self.subTest(reason=reason), TemporaryDirectory() as directory:
                handler = self.handler()
                files = {"upload:chosen": str(JCAMP)}
                selection = _selection(handler, files, directory)
                with patch("secs.elucidation.GraphGAOptimizer.run",
                           return_value=OptimizerResult(population=population)):
                    with self.assertRaisesRegex(RuntimeError, reason):
                        self.request(handler, directory, selection)

    def test_candidate_retrieval_bug_remains_an_operational_failure(self):
        failure = ValueError("private retrieval detail")
        handler = self.handler(Mock(propose=Mock(side_effect=failure)))
        with TemporaryDirectory() as directory:
            files = {"upload:chosen": str(JCAMP)}
            selection = _selection(handler, files, directory)
            with self.assertRaises(ValueError) as caught:
                self.request(handler, directory, selection)
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
                index = faiss.IndexFlatIP(2)
                index.add(np.array([[1., 0.]] * len(formulas), dtype=np.float32).reshape(-1, 2))
                candidates = FaissCandidateSource(index, ["NCc1ccc(Cl)cc1"] * len(formulas), formulas,
                                                   n_neighbours=8)
                handler = self.handler(candidates)
                files = {"upload:chosen": str(JCAMP)}
                selection = _selection(handler, files, directory)
                optimized = OptimizerResult(population=[("NCc1ccc(Cl)cc1", 1.0)], generations=1, n_evaluated=2)
                with patch("secs.elucidation.GraphGAOptimizer.run", return_value=optimized) as refine:
                    response = self.request(handler, directory, selection)
                self.assertEqual(response["outcome"], outcome)
                analysis = response["analysis"]
                self.assertEqual(analysis["retrieval"], {
                    "index_size": len(formulas), "neighbours_returned": len(formulas),
                    "formula_matches": matches, "starting_candidates": starting,
                })
                if starting:
                    refine.assert_called_once()
                    self.assertEqual(len(refine.call_args.args[0]), starting)
                else:
                    refine.assert_not_called()
                    self.assertIn("Graph GA was not run", analysis["search"]["explanation"])

    def test_explicit_jcamp_member_and_bruker_pair_prepare_real_spectra(self):
        adapter = InputAdapter(token_key=b"test" * 8)
        for kind in ("jcamp", "bruker"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                root = Path(directory)
                upload = root / "upload"
                with ZipFile(upload, "w") as archive:
                    if kind == "jcamp":
                        archive.write(JCAMP, "chosen/spectrum.jdx")
                        member = "chosen/spectrum.jdx"
                    else:
                        for name in ("1r", "procs"):
                            archive.write(PDATA / name, "chosen/pdata/7/" + name)
                        member = "chosen/pdata/7/1r"
                access = SourceAccess({"upload:chosen": upload}, root)
                facts = adapter.discover(access, ATTEMPT, SourceRef("upload:chosen", member))
                choice = next(item for item in facts["representations"] if item["kind"] == "spectrum")
                formula = "C7H8ClN" if kind == "jcamp" else "C2H6O"
                prepared = adapter.prepare(access, ATTEMPT, {
                    "representation_id": choice["id"], "formula": formula,
                    "formula_evidence": {"kind": "job_specification"}, "processing": "as_stored",
                    "explanation": "Explicit integration choice.",
                })
                self.assertEqual(prepared.values.shape, (10000,))
                self.assertTrue(np.isfinite(prepared.values).all())

    def test_bruker_decoding_establishes_nucleus_dimension_and_point_count(self):
        for substitution in (("##$AXNUC= <1H>", "##$AXNUC= <13C>"),
                             ("##$PPARMOD= 0", "##$PPARMOD= 1"),
                             ("##$OFFSET= 14.01234", "##$OFFSET= nan"), None):
            with self.subTest(substitution=substitution), TemporaryDirectory() as directory:
                root = Path(directory)
                parameters = (PDATA / "procs").read_text()
                if substitution is not None:
                    parameters = parameters.replace(*substitution)
                (root / "procs").write_text(parameters)
                data = (PDATA / "1r").read_bytes()
                (root / "1r").write_bytes(data if substitution else data[:32])
                with self.assertRaises(SpectrumReadError):
                    read_bruker_pdata(root)

    def test_unusable_spectrum_feedback_names_the_window_that_needs_variation(self):
        with self.assertRaisesRegex(SpectrumReadError, "from -2 to 10 ppm.*variation"):
            prepare_secs_spectrum(SourceSpectrum(np.array([-2., 10.]), np.zeros(2)))

    def test_preparation_bug_stays_operational(self):
        adapter = InputAdapter(token_key=b"test" * 8)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            access = SourceAccess({"upload:chosen": JCAMP}, root)
            choice = adapter.discover(access, ATTEMPT, SourceRef("upload:chosen"))["representations"][0]
            with patch("secs_inference.provider.input_adapter.prepare_secs_spectrum",
                       side_effect=ValueError("program fault")):
                with self.assertRaisesRegex(ValueError, "program fault"):
                    adapter.prepare(access, ATTEMPT, {
                        "representation_id": choice["id"], "formula": "C7H8ClN",
                        "formula_evidence": {"kind": "job_specification"},
                        "processing": "as_stored", "explanation": "Direct proton spectrum.",
                    })


if __name__ == "__main__":
    unittest.main()
