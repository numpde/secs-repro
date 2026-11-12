"""A shared wheel pool is selected explicitly and never replaced wholesale."""

import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
            subprocess.run(['python3', str(ROOT / 'tools/packages_stream_inputs.py'), str(stage), str(ROOT), 'cpu', '--installer'],
                           capture_output=True, check=True)
            subprocess.run(['python3', str(ROOT / 'tools/packages_stream_inputs.py'), str(stage), str(repo), 'cpu'],
                           capture_output=True, check=True)
            destinations = {item['destination'] for item in json.loads((stage / 'sources.json').read_text())}
            self.assertIn('tools/bootstrap/acquisition/acquisition_tool/installer/wheels/pip-26.2.2-py3-none-any.whl', destinations)
            self.assertNotIn('pip-', (stage / 'packages.complete').read_text())
            self.assertNotIn('pip-', (stage / 'native.complete').read_text())
            self.assertEqual(json.loads((stage / 'manifest.json').read_text()), [])
            self.assertIn('src/module.py', destinations)
            self.assertIn('secs/src/module.py', destinations)
            self.assertNotIn('src/__pycache__/module.pyc', destinations)
            self.assertNotIn('secs/src/module.pyo', destinations)

    def test_provider_stream_binds_secret_copier_bytes_to_source_identity(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, repo = root / 'stage', root / 'repo'
            stage.mkdir()
            (stage / 'manifest.json').write_text('[]')
            (stage / 'provider.lock').write_text('lock')
            for name in ('src/secs_inference/__init__.py', 'src/secs_inference/model_failure.py',
                         'src/secs_inference/provider/main.py'):
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('content')
            helper = repo / 'tools/copy_deployment_secrets.py'
            helper.parent.mkdir()
            original = (ROOT / 'tools/copy_deployment_secrets.py').read_bytes()
            installer = subprocess.run(['python3', str(ROOT / 'tools/provider_stream_inputs.py'), str(stage), str(ROOT), '--installer'],
                                       capture_output=True, text=True, check=False)
            self.assertEqual(0, installer.returncode, msg=installer.stderr)
            identities = []
            for content in (original, original + b'\n# Changed image-owned copier.\n'):
                helper.write_bytes(content)
                result = subprocess.run(['python3', str(ROOT / 'tools/provider_stream_inputs.py'), str(stage), str(repo)],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(0, result.returncode, msg=result.stderr)
                identities.append(result.stdout.strip())
                entries = json.loads((stage / 'sources.json').read_text())
                selected = [entry for entry in entries if entry['destination'] == 'tools/copy_deployment_secrets.py']
                self.assertEqual(1, len(selected))
                self.assertEqual(hashlib.sha256(content).hexdigest(), selected[0]['sha256'])
                self.assertEqual(content, Path(selected[0]['path']).read_bytes())
            self.assertNotEqual(identities[0], identities[1])

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


if __name__ == '__main__':
    unittest.main()
