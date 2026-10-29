"""Networked acquisition requires an explicit route selection."""

import os
from pathlib import Path
import pty
import select
import subprocess
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[2]


class NetworkInterfaceTests(unittest.TestCase):
    def test_terminal_requires_an_explicit_choice(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.execve('/bin/bash', ['bash', '-c', 'source tools/network_interface.sh; select_network_interface; printf "SELECTED=%s\\n" "$SECS_SELECTED_INTERFACE"'],
                      os.environ | {'SECS_NETWORK_INTERFACE_ORIGIN': 'undefined'})
        try:
            output = b''
            while b'Choice [' not in output:
                ready, _, _ = select.select([fd], [], [], 5)
                self.assertTrue(ready, 'Selection prompt did not appear')
                output += os.read(fd, 4096)
            self.assertIn(b'no default', output)
            os.write(fd, b'1\n')
            while b'SELECTED=system' not in output:
                ready, _, _ = select.select([fd], [], [], 5)
                self.assertTrue(ready, 'Selection did not finish')
                output += os.read(fd, 4096)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        finally:
            os.close(fd)

    def invoke(self, arguments, *, selection='', origin='undefined', old='undefined'):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / 'docker'
            docker.write_text('''#!/bin/bash
args=("$@")
while [[ $1 != create && $1 != start && $1 != inspect && $1 != run && $1 != build && $1 != pull ]]; do shift; done
case $1 in
  create)
    printf '%s\\n' "${args[@]}" > "$RECEIPT"
    while [[ $1 != --cidfile ]]; do shift; done
    printf '%064d' 0 > "$2"; printf '%064d\\n' 0 ;;
  inspect) printf 'created false false true true no\\n' ;;
  start) exit 0 ;;
  *) printf '%s\\n' "${args[@]}" > "$RECEIPT" ;;
esac
''')
            docker.chmod(0o700)
            receipt = root / 'receipt'
            env = os.environ | {'PATH': f'{root}:/usr/bin:/bin', 'RECEIPT': str(receipt),
                'SECS_NETWORK_INTERFACE_INPUT': selection, 'SECS_NETWORK_INTERFACE_ORIGIN': origin,
                'SECS_OLD_WLAN_ORIGIN': old}
            result = subprocess.run(['bash', str(ROOT / 'tools/docker.sh'), '--context', 'default', *arguments],
                                    env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            return result, receipt.read_text() if receipt.exists() else None

    def test_noninteractive_download_requires_selection_before_docker(self):
        result, receipt = self.invoke(['run', '--network', 'bridge', 'image'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(receipt)
        self.assertIn(b'NETWORK_INTERFACE', result.stderr)

    def test_system_route_clears_proxy_overrides(self):
        for op, origin, network in (('run', 'command line', '--network=bridge'),
                                    ('build', 'environment', '--network=default')):
            with self.subTest(op=op, origin=origin):
                result, receipt = self.invoke([op, network, 'image'], selection='system', origin=origin)
                self.assertEqual(result.returncode, 0, result.stderr)
                for variable in ('HTTP_PROXY=', 'HTTPS_PROXY=', 'http_proxy=', 'https_proxy=', 'NO_PROXY=',
                                 'no_proxy=', 'ALL_PROXY=', 'all_proxy=', 'FTP_PROXY=', 'ftp_proxy='):
                    self.assertIn(variable + '\n', receipt)

    def test_offline_and_pull_do_not_require_selection(self):
        for args in (['run', '--network', 'none', 'image'], ['pull', 'image']):
            with self.subTest(args=args):
                result, receipt = self.invoke(args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNotNone(receipt)

    def test_offline_run_owns_only_missing_cid_and_keeps_proxy_environment(self):
        result, receipt = self.invoke(['run', '--network', 'none', 'image'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt.count('--cidfile\n'), 1)
        self.assertNotIn('HTTP_PROXY=', receipt)
        for option in (['--cidfile', '/tmp/caller.cid'], ['--cidfile=/tmp/caller.cid']):
            with self.subTest(option=option):
                result, receipt = self.invoke(['run', '--network', 'none', *option, 'image'])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(receipt.splitlines(), ['--context', 'default', 'run', '--network', 'none', *option, 'image'])

    def test_container_command_pull_argument_remains_data(self):
        result, receipt = self.invoke(['run', '--network', 'bridge', 'image', 'command', '--pull=always'],
                                      selection='system', origin='command line')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt.splitlines()[-3:], ['image', 'command', '--pull=always'])

    def test_invalid_and_legacy_choices_fail_before_docker(self):
        for selection, origin, old in [('missing-device', 'command line', 'undefined'), ('', 'environment', 'undefined'),
                                       ('system', 'command line', 'command line')]:
            with self.subTest(selection=selection, old=old):
                result, receipt = self.invoke(['run', '--network', 'bridge', 'image'], selection=selection, origin=origin, old=old)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(receipt)
        result, receipt = self.invoke(['pull', 'image'], old='command line')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(receipt)

    def test_make_passes_interface_as_literal_data(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker, receipt, recipe = root / 'executed', root / 'receipt', root / 'probe.mk'
            value = f'$(shell touch {marker})'
            recipe.write_text(f"probe-interface:\n\t@printf '%s' \"$$SECS_NETWORK_INTERFACE_INPUT\" > {receipt}\n")
            result = subprocess.run(['make', '-f', 'Makefile', '-f', str(recipe), 'probe-interface',
                                     f'NETWORK_INTERFACE={value}'], cwd=ROOT, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(receipt.read_text(), value)
            self.assertFalse(marker.exists())

    def test_environment_selection_survives_recursive_make(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt, recipe = root / 'receipt', root / 'probe.mk'
            recipe.write_text(f'probe-parent:\n\t@$(MAKE) --no-print-directory -f Makefile -f {recipe} probe-child\n'
                              f'probe-child:\n\t@printf "%s|%s" "$$SECS_NETWORK_INTERFACE_INPUT" '
                              f'"$$SECS_NETWORK_INTERFACE_ORIGIN" > {receipt}\n')
            result = subprocess.run(['make', '-f', 'Makefile', '-f', str(recipe), 'probe-parent'],
                                    cwd=ROOT, env=os.environ | {'NETWORK_INTERFACE': 'system'}, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(receipt.read_text(), 'system|environment')
            marker = root / 'executed'
            value = f'$(shell touch {marker})'
            result = subprocess.run(['make', '-f', 'Makefile', '-f', str(recipe), 'probe-parent'],
                                    cwd=ROOT, env=os.environ | {'NETWORK_INTERFACE': value}, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(receipt.read_text(), value + '|environment')
            self.assertFalse(marker.exists())
