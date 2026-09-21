"""Opt-in live interpreter qualification, adapted from Magnet's model-behavior lane.

Ported from magnet-deploy/tests/model_behavior/run_e01_interpreter.py (26931fb):
real configured endpoint, semantic outcome checks, and retained-conversation
feedback. Use SECS's production configuration, tools, session and source inspector.
Only the supplied archive is a fixture. No Job API, GPU or scientific run occurs.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from zipfile import ZipFile

from secs_inference.provider.config import CONFIG_PATH, decode_provider_config
from secs_inference.provider.input_adapter import InputAdapter
from secs_inference.provider.input_operations import CannotAnalyse, SelectedRepresentation, SourceRef
from secs_inference.provider.interpreter import InterpretationSession
from secs_inference.provider.job_input import JobSpecification
from secs_inference.provider.job_upload import JobUpload
from secs_inference.provider.main import _read_regular_file, load_chat_endpoint
from secs_inference.provider.source_access import SourceAccess


class LiveInterpreterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.execution = decode_provider_config(_read_regular_file(CONFIG_PATH, 65536)).execution
        if cls.execution is None:
            raise ValueError("Live interpreter tests require an execution configuration.")
        cls.chat = load_chat_endpoint(cls.execution)
        print(f"Live interpreter: model={cls.chat.model!r}, reasoning_effort={cls.chat.reasoning_effort!r}", flush=True)

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        spectrum = Path("/fixtures/proton.jdx").read_bytes()
        spectrum = b"##TITLE=NMR experiment\n" + spectrum.split(b"\n", 1)[1]
        archive = root / "experiments.zip"
        with ZipFile(archive, "w") as output:
            output.writestr("experiment1/spectrum.jdx", Path(__file__).with_name("fixtures").joinpath("carbon.jdx").read_bytes())
            output.writestr("experiment2/spectrum.jdx", spectrum)
        self.access = SourceAccess({"upload:fixture": archive}, root)
        self.adapter = InputAdapter(token_key=b"model-behavior" * 2)
        self.inspected = []
        self.representations = {}
        self.uploads = (JobUpload("upload:fixture", "Archive of two NMR experiments; inspect their nuclei.", archive.stat().st_size, None),)

    def session(self, text, uploads):
        """Keep each observation within the deployment's own turn and time limits."""
        return InterpretationSession(self.chat, JobSpecification("job:fixture", text), uploads,
            self.inspect, deadline=monotonic() + min(120, self.execution.interpretation_seconds),
            max_turns=min(8, self.execution.max_turns))

    def inspect(self, source):
        """Observe successful production inspection without substituting its output."""
        result = self.adapter.discover(self.access, "execution_attempt:model-behavior", source)
        self.inspected.append(source)
        self.representations.update({item["id"]: item for item in result["representations"]})
        return result

    def test_missing_input_is_explained_not_invented(self):
        outcome = self.session("Please elucidate this molecule.", ()).select()
        self.assertIsInstance(outcome, CannotAnalyse)
        self.assertTrue(outcome.explanation.strip())
        self.assertIn("formula", outcome.explanation.lower())
        self.assertTrue(any(word in outcome.explanation.lower() for word in ("spectrum", "spectra", "nmr")))

    def test_selects_proton_experiment_among_distractors(self):
        session = self.session("Find the structure with molecular formula C7H8ClN.", self.uploads)
        self.assert_proton_selection(session.select())
        self.assertIn(SourceRef("upload:fixture", "experiment2/spectrum.jdx"), self.inspected)

    def test_selects_bruker_processed_pair(self):
        """Require real Bruker tool arguments; the separate reader tests decode it."""
        archive = Path(self.temporary.name) / "bruker.zip"
        with ZipFile(archive, "w") as output:
            for name in ("1r", "procs"):
                output.write(Path("/fixtures/bruker") / name, "NMR-test-1/pdata/1/" + name)
        self.access.files["upload:bruker"] = archive
        uploads = (JobUpload("upload:bruker", "Archive of an NMR experiment; inspect the processed data.", archive.stat().st_size, None),)
        session = self.session("Find the structure with molecular formula C21H22N2O2.", uploads)
        outcome = session.select()
        self.assertIsInstance(outcome, SelectedRepresentation, session.rejections)
        sources = {source["member"] for source in self.representations[outcome.representation_id]["sources"]}
        self.assertEqual(sources, {"NMR-test-1/pdata/1/1r", "NMR-test-1/pdata/1/procs"})
        self.assertEqual(outcome.formula, "C21H22N2O2")
        self.assertTrue(outcome.explanation.strip())
        self.assertIn(SourceRef("upload:bruker", "NMR-test-1/pdata/1/procs"), self.inspected)

    def test_reader_rejection_is_explained_in_the_same_conversation(self):
        """Inject a structural rejection, not an outage that the worker would fail.

        SECS may explain that no usable input remains; unlike Magnet's
        argument-repair case, an unreadable spectrum cannot be corrected by
        resubmitting the same selection. No scientific reader runs here.
        """
        session = self.session("Find the structure with molecular formula C7H8ClN.", self.uploads)
        self.assert_proton_selection(session.select())
        remaining = session.remaining_turns
        session.reject("Cannot read the processed JCAMP-DX spectrum because the file does not contain an XYDATA table.")
        outcome = session.select()
        self.assertIsInstance(outcome, CannotAnalyse)
        self.assertIn("xydata", outcome.explanation.lower())
        self.assertLess(session.remaining_turns, remaining)

    def assert_proton_selection(self, outcome):
        """The expected source and formula are evaluator facts, not tool instructions."""
        self.assertIsInstance(outcome, SelectedRepresentation)
        self.assertEqual(self.representations[outcome.representation_id]["sources"], [
            {"upload_ref": "upload:fixture", "member": "experiment2/spectrum.jdx"},
        ])
        self.assertEqual(outcome.formula, "C7H8ClN")
        self.assertTrue(outcome.explanation.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
