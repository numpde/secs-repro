"""Exercise missing-only shared wheel acquisition with a local offline feed."""

import hashlib
import os
from pathlib import Path
import subprocess
import unittest
import zipfile

from tools.link_cached_wheels import link
from tools.publish_shared_wheels import publish
from test_lifecycle import lifecycle_workspace


ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


class SharedWheelhouseProcessTests(unittest.TestCase):
    def test_partial_pool_reuses_existing_file_and_publishes_only_missing(self):
        image = os.environ['DEPLOYMENT_TEST_IMAGE']
        with lifecycle_workspace() as temporary:
            root = Path(temporary)
            pool, feed, stage = (root / name for name in ('pool', 'feed', 'stage'))
            for path in (pool, feed, stage):
                path.mkdir()
            first, second = 'fixture_a-1.0-py3-none-any.whl', 'fixture_b-1.0-py3-none-any.whl'
            for name, destination in (('fixture_a', pool / first), ('fixture_b', feed / second)):
                metadata = name + '-1.0.dist-info/'
                with zipfile.ZipFile(destination, 'w') as wheel:
                    wheel.writestr(metadata + 'METADATA', f'Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n')
                    wheel.writestr(metadata + 'WHEEL', 'Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                    wheel.writestr(metadata + 'RECORD', '')
            original_inode = (pool / first).stat().st_ino
            lock = root / 'requirements.lock'
            lock.write_text(f'fixture-a==1.0 --hash=sha256:{digest(pool / first)}\n'
                            f'fixture-b==1.0 --hash=sha256:{digest(feed / second)}\n')
            link(pool, stage)
            command = [str(ROOT / 'tools/docker.sh'), 'run', '--rm',
                       '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                       '--security-opt', 'no-new-privileges:true', '--user', f'{os.getuid()}:{os.getgid()}',
                       '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=32m',
                       '--mount', f'type=bind,src={pool},dst=/pool,readonly',
                       '--mount', f'type=bind,src={feed},dst=/feed,readonly',
                       '--mount', f'type=bind,src={stage},dst=/stage',
                       '--mount', f'type=bind,src={lock},dst=/input/requirements.lock,readonly',
                       '--entrypoint', 'python', image, '-m', 'pip', 'download', '--no-index', '--no-deps',
                       '--require-hashes', '--find-links', '/pool', '--find-links', '/feed',
                       '--dest', '/stage', '-r', '/input/requirements.lock']
            subprocess.run(command, cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertTrue((stage / first).is_symlink())
            self.assertTrue((stage / second).is_file())
            publish(stage, pool)
            self.assertEqual((pool / first).stat().st_ino, original_inode)
            self.assertEqual(digest(pool / second), digest(feed / second))
