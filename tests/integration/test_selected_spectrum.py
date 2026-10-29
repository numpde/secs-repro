"""A discovered representation reaches the real reader and scientific search."""

from dataclasses import asdict
from hashlib import sha256
from tempfile import NamedTemporaryFile
from provider_core.upload_file import VerifiedUploadFile

from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import weakref
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


def _request(handler, document):
    if "files" not in document:
        return handler(document)
    directory = Path(document["directory"])
    receipts = {}
    for ref, filename in document["files"].items():
        path = Path(filename)
        if path.parent != directory:
            with NamedTemporaryFile(dir=directory, prefix="source-", delete=False) as output:
                destination = Path(output.name)
            shutil.copyfile(path, destination)
            path = destination
        contents = path.read_bytes()
        receipts[ref] = asdict(VerifiedUploadFile("job:fixture", ref, len(contents),
            "sha256:" + sha256(contents).hexdigest(), path.name))
    command = {key: value for key, value in document.items() if key != "files"}
    return handler(command | {"receipts": receipts, "max_upload_bytes": 128 * 1024 * 1024,
                              "max_total_bytes": 256 * 1024 * 1024})


def _selection(handler, files, directory, formula="C7H8ClN"):
    inspected = _request(handler, {"operation": "inspect", "files": files, "directory": str(directory),
                         "attempt_ref": ATTEMPT,
                         "source": {"upload_ref": "upload:chosen", "member": None}})
    choices = [item for item in inspected["facts"]["representations"]
               if item["kind"] == "spectrum" and item["metadata"].get("nucleus") == "1H"]
    if len(choices) != 1:
        raise AssertionError(f"Expected one proton spectrum, found {len(choices)}")
    return {"representation_id": choices[0]["id"], "formula": formula,
            "formula_evidence": {"kind": "job_specification", "quote": "C7H8ClN"}, "processing": "as_stored",
            "explanation": "The discovered representation is the requested proton spectrum."}


class SelectedSpectrumTests(unittest.TestCase):
    def test_prepared_input_survives_source_deletion_and_is_consumed_once(self):
        from secs.elucidation import StaticCandidateSource

        class Inference:
            def __init__(self):
                self.spectra = []

            def embed_spectrum(self, values):
                self.spectra.append(values.copy())
                return np.array([1., 0.], dtype=np.float32)

        inference = Inference()
        candidates = Mock(wraps=StaticCandidateSource([]))
        handler = ScientificHandler(inference, candidates,
            ScientificWorkerConfig("unused", "unused", device="cpu"))
        observed = {}
        original_prepare = handler.input_adapter.prepare

        def observe_prepare(*args):
            prepared = original_prepare(*args)
            observed["spectrum"] = prepared.values.copy()
            observed["prepared"] = weakref.ref(prepared.values)
            return prepared

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.jdx"
            shutil.copyfile(JCAMP, source)
            files = {"upload:chosen": str(source)}
            choice = _selection(handler, files, directory)
            with patch.object(handler.input_adapter, "prepare", side_effect=observe_prepare):
                prepared = _request(handler, {"operation": "prepare", "files": files,
                    "directory": directory, "attempt_ref": ATTEMPT, "selection": choice})
            self.assertEqual(prepared["outcome"], "prepared")
            self.assertEqual(inference.spectra, [])
            self.assertIsNotNone(observed["prepared"]())
            source.unlink()
            # Neither mutable selection facts nor the removed source can alter
            # the worker's admitted values and formula after preparation.
            choice["formula"] = "C30H62"
            command = {"operation": "execute", "attempt_ref": ATTEMPT,
                       "prepared_handle": prepared["prepared_handle"]}
            response = _request(handler, command)
            self.assertEqual(response["outcome"], "no_starting_candidates")
            self.assertEqual(len(inference.spectra), 1)
            self.assertEqual(candidates.propose.call_args.args[1], "C7H8ClN")
            np.testing.assert_array_equal(inference.spectra[0], observed["spectrum"])
            self.assertIsNone(observed["prepared"](), "Completed response retains prepared input")
            with self.assertRaisesRegex(ValueError, "prepared"):
                _request(handler, command)
            self.assertEqual(len(inference.spectra), 1)

    def test_wrong_attempt_cannot_consume_prepared_input(self):
        handler = self.handler()
        with TemporaryDirectory() as directory:
            files = {"upload:chosen": str(JCAMP)}
            choice = _selection(handler, files, directory)
            prepared = _request(handler, {"operation": "prepare", "files": files,
                "directory": directory, "attempt_ref": ATTEMPT, "selection": choice})
            with self.assertRaisesRegex(ValueError, "prepared"):
                _request(handler, {"operation": "execute", "attempt_ref": "another-attempt",
                         "prepared_handle": prepared["prepared_handle"]})
            handler.inference.embed_spectrum.assert_not_called()

    def test_failed_dispatch_cannot_reuse_its_consumed_preparation(self):
        failure = ValueError("retrieval fault")
        candidates = Mock(propose=Mock(side_effect=failure))
        handler = self.handler(candidates)
        with TemporaryDirectory() as directory:
            files = {"upload:chosen": str(JCAMP)}
            choice = _selection(handler, files, directory)
            prepared = _request(handler, {"operation": "prepare", "files": files,
                "directory": directory, "attempt_ref": ATTEMPT, "selection": choice})
            command = {"operation": "execute", "attempt_ref": ATTEMPT,
                       "prepared_handle": prepared["prepared_handle"]}
            with self.assertRaises(ValueError) as caught:
                _request(handler, command)
            self.assertIs(caught.exception, failure)
            with self.assertRaisesRegex(ValueError, "prepared"):
                _request(handler, command)
            handler.inference.embed_spectrum.assert_called_once()
            candidates.propose.assert_called_once()

    def test_formula_rejection_precedes_scientific_dispatch(self):
        handler = self.handler()
        with TemporaryDirectory() as directory:
            files = {"upload:chosen": str(JCAMP)}
            choice = _selection(handler, files, directory)
            choice["formula"] = "C7H8ClN followed by prose"
            choice["formula_evidence"]["quote"] = choice["formula"]
            response = _request(handler, {"operation": "prepare", "files": files,
                "directory": directory, "attempt_ref": ATTEMPT, "selection": choice})
            self.assertEqual(response["outcome"], "input_rejected")
            handler.inference.embed_spectrum.assert_not_called()

    def handler(self, candidates=None):
        inference = Mock(embed_spectrum=Mock(return_value=np.array([1., 0.], dtype=np.float32)))
        if candidates is None:
            from secs.elucidation import StaticCandidateSource
            candidates = StaticCandidateSource(["NCc1ccc(Cl)cc1"])
        return ScientificHandler(inference, candidates,
            ScientificWorkerConfig("unused", "unused", device="cpu", neighbours=8,
                                   initial_population_size=2))

    def request(self, handler, directory, selection):
        prepared = _request(handler, {"operation": "prepare", "files": {"upload:chosen": str(JCAMP)},
                        "directory": str(directory), "attempt_ref": ATTEMPT,
                        "selection": selection})
        if prepared["outcome"] == "input_rejected":
            return prepared
        return _request(handler, {"operation": "execute", "attempt_ref": ATTEMPT,
                        "prepared_handle": prepared["prepared_handle"]})

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
                access = SourceAccess({"upload:chosen": upload.read_bytes()}, root)
                facts = adapter.discover(access, ATTEMPT, SourceRef("upload:chosen", member))
                choice = next(item for item in facts["representations"] if item["kind"] == "spectrum")
                formula = "C7H8ClN" if kind == "jcamp" else "C2H6O"
                prepared = adapter.prepare(access, ATTEMPT, {
                    "representation_id": choice["id"], "formula": formula,
                    "formula_evidence": {"kind": "job_specification", "quote": formula}, "processing": "as_stored",
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
            access = SourceAccess({"upload:chosen": JCAMP.read_bytes()}, root)
            choice = adapter.discover(access, ATTEMPT, SourceRef("upload:chosen"))["representations"][0]
            with patch("secs_inference.provider.input_formats.prepare_secs_spectrum",
                       side_effect=ValueError("program fault")):
                with self.assertRaisesRegex(ValueError, "program fault"):
                    adapter.prepare(access, ATTEMPT, {
                        "representation_id": choice["id"], "formula": "C7H8ClN",
                        "formula_evidence": {"kind": "job_specification", "quote": "C7H8ClN"},
                        "processing": "as_stored", "explanation": "Direct proton spectrum.",
                    })


if __name__ == "__main__":
    unittest.main()
