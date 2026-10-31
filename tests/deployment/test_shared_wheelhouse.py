"""A shared wheel pool is selected explicitly and never replaced wholesale."""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.publish_shared_wheels import publish
from tools.link_cached_wheels import link


ROOT = Path(__file__).resolve().parents[2]


class SharedWheelhouseTests(unittest.TestCase):
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
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WHEELHOUSE=', result.stderr)

    def test_pool_cannot_overlap_the_checkout(self):
        with TemporaryDirectory(dir=ROOT) as nested:
            result = subprocess.run(['make', '--no-print-directory', 'provider/image', 'DOCKER_CONTEXT=default', f'WHEELHOUSE={nested}'],
                                    cwd=ROOT, capture_output=True, text=True, timeout=10)
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
