"""A shared wheel pool is selected explicitly and never replaced wholesale."""

import fcntl
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.publish_shared_wheels import publish
from tools.link_cached_wheels import link


ROOT = Path(__file__).resolve().parents[2]


def standalone_make_environment():
    environment = os.environ.copy()
    environment.pop("MAKELEVEL", None)
    environment.pop("MAKEFLAGS", None)
    return environment


class SharedWheelhouseTests(unittest.TestCase):
    def test_pylock_output_does_not_follow_a_dangling_symlink(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, target = root / 'pylock.toml', root / 'missing.toml'
            output.symlink_to(target)
            result = subprocess.run(['make', '--no-print-directory', 'packages/pylock/locate', 'VARIANT=cpu',
                                     f'OUTPUT={output}', f'WHEELHOUSE={root}', 'DOCKER_CONTEXT=generic'],
                                    cwd=ROOT, env=standalone_make_environment(), capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'Projected lock already exists', result.stderr)
            self.assertTrue(output.is_symlink())
            self.assertFalse(target.exists())

    def test_package_stream_excludes_generated_bytecode(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, repo = root / 'stage', root / 'repo'
            stage.mkdir()
            for role in ('packages', 'native'):
                (stage / f'{role}.json').write_text('[]')
                (stage / f'{role}.lock').write_text('lock')
            for name in ('containers/packages/Dockerfile', 'pyproject.toml', 'secs/pyproject.toml',
                         'secs/README.md', 'src/module.py', 'src/__pycache__/module.pyc',
                         'secs/src/module.py', 'secs/src/module.pyo'):
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('content')
            subprocess.run(['python3', str(ROOT / 'tools/packages_stream_inputs.py'), str(stage), str(repo), 'cpu'],
                           capture_output=True, check=True)
            destinations = {item['destination'] for item in json.loads((stage / 'sources.json').read_text())}
            self.assertIn('src/module.py', destinations)
            self.assertIn('secs/src/module.py', destinations)
            self.assertNotIn('src/__pycache__/module.pyc', destinations)
            self.assertNotIn('secs/src/module.pyo', destinations)

    def test_writer_waits_for_readers_and_readers_can_coexist(self):
        with TemporaryDirectory() as temporary:
            directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
            command = ('source tools/wheelhouse_selection.sh; wheelhouse_directory=$1; '
                       'lock_wheelhouse "$2"; printf acquired')
            writer = None
            try:
                fcntl.flock(directory, fcntl.LOCK_SH)
                shared = subprocess.run(['bash', '-c', command, 'wheelhouse-test', temporary, '-s'],
                                        cwd=ROOT, capture_output=True, timeout=5, check=True)
                self.assertEqual(shared.stdout, b'acquired')
                writer = subprocess.Popen(['bash', '-c', command, 'wheelhouse-test', temporary, '-x'],
                                          cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                with self.assertRaises(subprocess.TimeoutExpired):
                    writer.communicate(timeout=0.2)
                fcntl.flock(directory, fcntl.LOCK_UN)
                output, error = writer.communicate(timeout=5)
                self.assertEqual((writer.returncode, output, error), (0, b'acquired', b''))
            finally:
                if writer is not None and writer.poll() is None:
                    writer.kill()
                    writer.communicate()
                os.close(directory)

    def test_publication_keeps_existing_artifacts_and_refuses_conflicts(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, pool = root / 'stage', root / 'pool'
            stage.mkdir(); pool.mkdir()
            (stage / 'fixture.whl').write_bytes(b'expected')
            (pool / 'unrelated.whl').write_bytes(b'unrelated')
            publish(stage, pool)
            publish(stage, pool)
            self.assertEqual((pool / 'fixture.whl').read_bytes(), b'expected')
            (stage / 'fixture.whl').unlink()
            (stage / 'fixture.whl').write_bytes(b'different')
            with self.assertRaisesRegex(ValueError, 'different bytes'):
                publish(stage, pool)
            self.assertEqual((pool / 'fixture.whl').read_bytes(), b'expected')
            self.assertEqual((pool / 'unrelated.whl').read_bytes(), b'unrelated')

    def test_unattended_build_requires_an_explicit_pool(self):
        result = subprocess.run(['make', '--no-print-directory', 'provider/image', 'DOCKER_CONTEXT=default'], cwd=ROOT,
                                capture_output=True, text=True, timeout=10, env=standalone_make_environment())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WHEELHOUSE=', result.stderr)

    def test_pool_cannot_overlap_the_checkout(self):
        with TemporaryDirectory(dir=ROOT) as nested:
            result = subprocess.run(['make', '--no-print-directory', 'provider/image', 'DOCKER_CONTEXT=default', f'WHEELHOUSE={nested}'],
                                    cwd=ROOT, capture_output=True, text=True, timeout=10, env=standalone_make_environment())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('outside the repository', result.stderr)

    def test_cached_artifacts_are_aliases_and_only_new_files_are_published(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, pool = root / 'stage', root / 'pool'
            stage.mkdir(); pool.mkdir()
            (pool / 'present.whl').write_bytes(b'cached')
            link(pool, stage)
            self.assertTrue((stage / 'present.whl').is_symlink())
            self.assertEqual((stage / 'present.whl').readlink(), Path('/pool/present.whl'))
            (stage / 'missing.whl').write_bytes(b'new')
            publish(stage, pool)
            self.assertEqual((pool / 'present.whl').read_bytes(), b'cached')
            self.assertEqual((pool / 'missing.whl').read_bytes(), b'new')


if __name__ == '__main__':
    unittest.main()
