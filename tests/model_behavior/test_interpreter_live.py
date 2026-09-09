"""Opt-in live interpreter qualification, adapted from Magnet's model-behavior lane.

Ported from magnet-deploy/tests/model_behavior/run_e01_interpreter.py (26931fb):
real configured endpoint, semantic outcome checks, and forced conversational
repair. Use SECS's production configuration, tools, session and source inspector.
Only the supplied archive is a fixture. No Job API, GPU or scientific run occurs.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from zipfile import ZipFile

from secs_inference.provider.config import CONFIG_PATH, decode_provider_config
from secs_inference.provider.input_operations import CannotAnalyse, JcampSelection, SourceRef
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
        self.inspected = []
        self.uploads = (JobUpload("upload:fixture", "Archive of two NMR experiments; inspect their nuclei.", archive.stat().st_size, None),)

    def session(self, text, uploads):
        """Keep each observation within the deployment's own turn and time limits."""
        return InterpretationSession(self.chat, JobSpecification("job:fixture", text), uploads,
            self.inspect, deadline=monotonic() + min(120, self.execution.interpretation_seconds),
            max_turns=min(8, self.execution.max_turns))

    def inspect(self, source):
        """Observe successful production inspection without substituting its output."""
        result = self.access.inspect(source)
        self.inspected.append(source)
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

    def test_forced_reader_rejection_uses_the_same_conversation(self):
        """Port Magnet's forced-repair check; this is not a real reader failure."""
        session = self.session("Find the structure with molecular formula C7H8ClN.", self.uploads)
        self.assert_proton_selection(session.select())
        remaining = session.remaining_turns
        session.reject("The reader could not read the selected input because of a temporary reader failure. The input bytes have not changed.")
        outcome = session.select()
        self.assert_proton_selection(outcome)
        self.assertLess(session.remaining_turns, remaining)

    def assert_proton_selection(self, outcome):
        """The expected source and formula are evaluator facts, not tool instructions."""
        self.assertIsInstance(outcome, JcampSelection)
        self.assertEqual(outcome.source, SourceRef("upload:fixture", "experiment2/spectrum.jdx"))
        self.assertEqual(outcome.formula, "C7H8ClN")
        self.assertTrue(outcome.explanation.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
