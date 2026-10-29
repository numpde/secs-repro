"""Do not delete a stage while an engine operation may still own it."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


class StageRetentionTests(unittest.TestCase):
    def run_owner(self, active, status):
        with TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            script = ('source tools/retain_uncertain_stage.sh; '
                      f'secs_engine_active={active}; trap \'retain_uncertain_stage "$1"\' EXIT; exit {status}')
            result = subprocess.run(["bash", "-c", script, "bash", str(stage)], cwd=ROOT, capture_output=True)
            return result, stage.exists(), str(stage).encode() in result.stderr

    def test_uncertain_engine_retains_stage_and_reports_path(self):
        result, retained, reported = self.run_owner(1, 143)
        self.assertEqual(result.returncode, 143)
        self.assertTrue(retained)
        self.assertTrue(reported)

    def test_failure_before_engine_cleans_stage(self):
        result, retained, _ = self.run_owner(0, 2)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(retained)


if __name__ == "__main__":
    unittest.main()
