"""An unrecorded create must not dispatch a worker."""

import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[2]


class CreateRetirementTests(unittest.TestCase):
    def test_mismatched_proxy_receipt_is_not_used_to_start_or_remove(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = root / 'calls'
            docker = root / 'docker'
            docker.write_text('''#!/bin/bash
while [[ $1 != create && $1 != start && $1 != ps && $1 != rm ]]; do shift; done
printf '%s\\n' "$1" >> "$CALLS"
if [[ $1 == create ]]; then
  while [[ $1 != --cidfile ]]; do shift; done
  printf '%064d' 0 > "$2"; printf '%064d\\n' 1
fi
''')
            docker.chmod(0o700)
            env = os.environ | {'PATH': f'{root}:/usr/bin:/bin', 'CALLS': str(calls),
                'SECS_NETWORK_INTERFACE_ORIGIN': 'command line', 'SECS_NETWORK_INTERFACE_INPUT': 'lo',
                'SECS_OLD_WLAN_ORIGIN': 'undefined', 'SECS_BOOTSTRAP_IMAGE': 'test-image'}
            result = subprocess.run(['bash', str(ROOT / 'tools/docker.sh'), 'run', '--rm', '--network', 'bridge', 'image'],
                                    env=env, cwd=ROOT, capture_output=True, timeout=10)
            try:
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls.read_text().splitlines(), ['create'])
                self.assertIn(b'output does not match', result.stderr)
                self.assertIn(b'Docker retirement is unconfirmed; retain ', result.stderr)
            finally:
                text = result.stderr.decode()
                if 'Docker retirement is unconfirmed; retain ' in text:
                    scratch = Path(text.split('Docker retirement is unconfirmed; retain ', 1)[1].split(' and inspect', 1)[0])
                    shutil.rmtree(scratch)

    def test_missing_proxy_create_receipt_never_starts_proxy(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / 'proxy-started'
            docker = root / 'docker'
            docker.write_text('''#!/bin/bash
while [[ $1 != run && $1 != create && $1 != start ]]; do shift; done
case $1 in
  run|start) touch "$MARKER"; exit 1 ;;
  create) exit 1 ;;
esac
''')
            docker.chmod(0o700)
            env = os.environ | {'PATH': f'{root}:/usr/bin:/bin', 'MARKER': str(marker),
                'SECS_NETWORK_INTERFACE_ORIGIN': 'command line', 'SECS_NETWORK_INTERFACE_INPUT': 'lo',
                'SECS_OLD_WLAN_ORIGIN': 'undefined', 'SECS_BOOTSTRAP_IMAGE': 'test-image'}
            result = subprocess.run(['bash', str(ROOT / 'tools/docker.sh'), 'run', '--rm',
                                     '--network', 'bridge', 'image'], env=env, cwd=ROOT,
                                    capture_output=True, timeout=10)
            try:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists(), 'Proxy started without a recorded identity')
                self.assertIn(b'Docker retirement is unconfirmed; retain ', result.stderr)
            finally:
                text = result.stderr.decode()
                if 'Docker retirement is unconfirmed; retain ' in text:
                    scratch = Path(text.split('Docker retirement is unconfirmed; retain ', 1)[1].split(' and inspect', 1)[0])
                    shutil.rmtree(scratch)

    def test_mismatched_create_receipt_is_not_used_to_start_or_remove(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = root / 'calls'
            docker = root / 'docker'
            docker.write_text('''#!/bin/bash
while [[ $1 != create && $1 != start && $1 != ps && $1 != rm ]]; do shift; done
printf '%s\\n' "$1" >> "$CALLS"
if [[ $1 == create ]]; then
  while [[ $1 != --cidfile ]]; do shift; done
  printf '%064d' 0 > "$2"; printf '%064d\\n' 1
fi
''')
            docker.chmod(0o700)
            env = os.environ | {'PATH': f'{root}:/usr/bin:/bin', 'CALLS': str(calls),
                'SECS_NETWORK_INTERFACE_ORIGIN': 'command line', 'SECS_NETWORK_INTERFACE_INPUT': 'system',
                'SECS_OLD_WLAN_ORIGIN': 'undefined'}
            result = subprocess.run(['bash', str(ROOT / 'tools/docker.sh'), 'run', '--rm', '--network', 'bridge', 'image'],
                                    env=env, cwd=ROOT, capture_output=True, timeout=10)
            try:
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls.read_text().splitlines(), ['create'])
                self.assertIn(b'output does not match', result.stderr)
                self.assertIn(b'Docker retirement is unconfirmed; retain ', result.stderr)
            finally:
                text = result.stderr.decode()
                if 'Docker retirement is unconfirmed; retain ' in text:
                    scratch = Path(text.split('Docker retirement is unconfirmed; retain ', 1)[1].split(' and inspect', 1)[0])
                    shutil.rmtree(scratch)

    def test_missing_create_receipt_never_starts_worker(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / 'started'
            docker = root / 'docker'
            docker.write_text('''#!/bin/bash
while [[ $1 != run && $1 != create && $1 != start ]]; do shift; done
case $1 in
  run|start) touch "$MARKER"; exit 1 ;;
  create) exit 1 ;;
esac
''')
            docker.chmod(0o700)
            env = os.environ | {'PATH': f'{root}:/usr/bin:/bin', 'MARKER': str(marker),
                'SECS_NETWORK_INTERFACE_ORIGIN': 'command line', 'SECS_NETWORK_INTERFACE_INPUT': 'system',
                'SECS_OLD_WLAN_ORIGIN': 'undefined'}
            result = subprocess.run(['bash', str(ROOT / 'tools/docker.sh'), '--context', 'default',
                                     'run', '--rm', '--network', 'bridge', '--pull', 'never', 'image'],
                                    env=env, cwd=ROOT, capture_output=True, timeout=10)
            try:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists(), 'Worker started without a recorded identity')
                self.assertIn(b'Docker retirement is unconfirmed; retain ', result.stderr)
            finally:
                text = result.stderr.decode()
                if 'Docker retirement is unconfirmed; retain ' in text:
                    scratch = Path(text.split('Docker retirement is unconfirmed; retain ', 1)[1].split(' and inspect', 1)[0])
                    shutil.rmtree(scratch)
