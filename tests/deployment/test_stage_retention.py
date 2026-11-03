"""Do not delete a stage while an engine operation may still own it."""

from pathlib import Path
import os
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

    def test_stage_cleanup_failure_changes_success_status(self):
        script = ('source tools/retain_uncertain_stage.sh; rm() { return 1; }; '
                  'secs_engine_active=0; trap \'retain_uncertain_stage "$1"\' EXIT; exit 0')
        result = subprocess.run(['bash', '-c', script, 'bash', '/tmp/example-stage'], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'Could not remove staging input', result.stderr)

    def test_confirmed_worker_failure_cleans_stage_and_keeps_status(self):
        with TemporaryDirectory() as temporary:
            stage = Path(temporary) / 'stage'
            stage.mkdir()
            script = ('source tools/retain_uncertain_stage.sh; '
                      'trap \'retain_uncertain_stage "$1"\' EXIT; begin_retained_engine "$1"; '
                      'printf "retired\\n" > "$SECS_RETIREMENT_RECEIPT"; exit 143')
            result = subprocess.run(['bash', '-c', script, 'bash', str(stage)], cwd=ROOT, capture_output=True)
            self.assertEqual(result.returncode, 143)
            self.assertFalse(stage.exists())

    def test_receipt_from_prior_worker_cannot_settle_next_worker(self):
        with TemporaryDirectory() as temporary:
            stage = Path(temporary) / 'stage'
            stage.mkdir()
            script = ('source tools/retain_uncertain_stage.sh; '
                      'trap \'retain_uncertain_stage "$1"\' EXIT; begin_retained_engine "$1"; '
                      'printf "retired\\n" > "$SECS_RETIREMENT_RECEIPT"; '
                      'begin_retained_engine "$1"; printf "retired\\n" > "$1/.docker-retired"; exit 1')
            result = subprocess.run(['bash', '-c', script, 'bash', str(stage)], cwd=ROOT, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(stage.exists())

    def test_receipt_creation_failure_never_starts_wheel_worker(self):
        script = ('source tools/retain_uncertain_stage.sh; source tools/wheelhouse_selection.sh; '
                  'mktemp() { return 1; }; bash() { echo worker-started; }; '
                  'stage=/tmp; if stage_selected_wheels a b c d e; then exit 9; fi')
        result = subprocess.run(['bash', '-c', script], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(b'worker-started', result.stdout)

    def test_ambient_receipt_cannot_create_an_arbitrary_file(self):
        with TemporaryDirectory() as temporary:
            receipt = Path(temporary) / 'unexpected'
            env = os.environ | {'SECS_RETIREMENT_RECEIPT': str(receipt), 'SECS_DOCKER_ENDPOINT': ''}
            subprocess.run(['bash', str(ROOT / 'tools/docker.sh'), 'run', '--network', 'none', 'image'],
                           cwd=ROOT, env=env, capture_output=True)
            self.assertFalse(receipt.exists())

    def test_receipt_directory_must_be_private_and_direct(self):
        with TemporaryDirectory(prefix='.secs-retirement.', dir=ROOT) as temporary:
            directory = Path(temporary)
            directory.chmod(0o755)
            receipt = directory / 'retired'
            env = os.environ | {'SECS_RETIREMENT_RECEIPT': str(receipt), 'SECS_DOCKER_ENDPOINT': ''}
            subprocess.run(['bash', str(ROOT / 'tools/docker.sh'), 'run', '--network', 'none', 'image'],
                           cwd=ROOT, env=env, capture_output=True)
            self.assertFalse(receipt.exists())
            directory.chmod(0o700)
            env['SECS_RETIREMENT_RECEIPT'] = str(directory / '..' / directory.name / 'retired')
            subprocess.run(['bash', str(ROOT / 'tools/docker.sh'), 'run', '--network', 'none', 'image'],
                           cwd=ROOT, env=env, capture_output=True)
            self.assertFalse(receipt.exists())

    def test_completed_failed_build_retains_only_its_image_receipt(self):
        with TemporaryDirectory() as temporary:
            stage = Path(temporary) / 'stage'
            stage.mkdir()
            (stage / 'image-id').write_text('sha256:' + 'a' * 64)
            (stage / 'manifest.json').write_text('{}')
            (stage / 'capture').mkdir()
            (stage / 'capture' / 'log').write_text('temporary')
            script = ('source tools/retain_uncertain_stage.sh; secs_engine_active=1; '
                      'trap \'retain_uncertain_stage "$1"\' EXIT; retain_build_receipt "$1"; exit 1')
            result = subprocess.run(['bash', '-c', script, 'bash', str(stage)], cwd=ROOT, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual([path.name for path in stage.iterdir()], ['image-id'])


if __name__ == "__main__":
    unittest.main()
