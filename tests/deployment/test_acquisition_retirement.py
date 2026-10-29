"""Exercise exact ownership of acquisition worker and SDK proxy containers."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

ROOT = Path(__file__).resolve().parents[2]
PROXY = 'a' * 64
WORKER = 'b' * 64


class AcquisitionRetirementTests(unittest.TestCase):
    def run_case(self, *, fail_removal=False, fail_proxy=False, missing_proxy=False, missing_worker=False,
                 handoff=None, hang_proxy=False, stop_signal=signal.SIGTERM):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / 'docker'
            docker.write_text('''#!/bin/bash
while [[ $1 != run && $1 != ps && $1 != rm && $1 != inspect ]]; do shift; done
op=$1; shift
printf '%s %s\\n' "$op" "$*" >> "$CALLS"
case "$op" in
run)
  cidfile=; receipt=; detached=0
  while (($#)); do
    case $1 in
      --detach) detached=1 ;;
      --cidfile) cidfile=$2; shift ;;
      --mount) case $2 in *,dst=/receipt) receipt=${2#type=bind,src=}; receipt=${receipt%,dst=/receipt};; esac; shift ;;
    esac
    shift
  done
  if ((detached)); then
    [[ $HANG_PROXY == 1 ]] && exec sleep 300
    [[ $MISSING == 1 ]] && exit 1
    printf '%s' "$PROXY" > "$cidfile"; touch "$PROXY_LIVE"; printf 12345 > "$receipt/proxy.port"
  else
    if [[ $MISSING_WORKER == 1 ]]; then touch "$WORKER_LIVE"; exec sleep 300; fi
    printf '%s' "$WORKER" > "$cidfile"; touch "$WORKER_LIVE"; trap '' TERM; exec sleep 300
  fi ;;
inspect) printf 'true\\n' ;;
ps)
  case "$*" in *"$PROXY"*) [[ -e $PROXY_LIVE ]] && printf '%s\\n' "$PROXY" ;; *"$WORKER"*) [[ -e $WORKER_LIVE ]] && printf '%s\\n' "$WORKER" ;; esac
  exit 0 ;;
rm)
  case "$*" in *"$WORKER"*) [[ $FAIL == 1 ]] && exit 1; rm -f "$WORKER_LIVE" ;; *"$PROXY"*) [[ $FAIL_PROXY == 1 ]] && exit 1; rm -f "$PROXY_LIVE" ;; esac ;;
esac
''')
            docker.chmod(0o700)
            calls, errors = root / 'calls', root / 'errors'
            worker_live, proxy_live = root / 'worker', root / 'proxy'
            env = os.environ | {'PATH': f'{root}:/usr/bin:/bin', 'SECS_NETWORK_INTERFACE_INPUT': 'lo',
                'SECS_NETWORK_INTERFACE_ORIGIN': 'command line',
                'SECS_BOOTSTRAP_IMAGE': 'pinned-test-image', 'CALLS': str(calls), 'PROXY': PROXY,
                'WORKER': WORKER, 'PROXY_LIVE': str(proxy_live), 'WORKER_LIVE': str(worker_live),
                'FAIL': '1' if fail_removal else '0', 'MISSING': '1' if missing_proxy else '0',
                'FAIL_PROXY': '1' if fail_proxy else '0',
                'MISSING_WORKER': '1' if missing_worker or handoff == 'docker' else '0',
                'HANG_PROXY': '1' if hang_proxy else '0'}
            if hang_proxy:
                timeout = root / 'timeout'
                timeout.write_text('#!/bin/bash\n[[ $3 == 30 ]] && set -- "$1" "$2" 0.2 "${@:4}"\nexec /usr/bin/timeout "$@"\n')
                timeout.chmod(0o700)
            if handoff:
                debug = root / 'debug.sh'
                debug.write_text(f'''trap 'if [[ $BASH_COMMAND == "{handoff}_pid=\\$!" ]]; then trap - DEBUG; kill -TERM "$$"; fi' DEBUG\n''')
                env['BASH_ENV'] = str(debug)
            scratch = None
            with errors.open('w') as error_stream:
                process = subprocess.Popen(['bash', str(ROOT / 'tools/docker.sh'), '--context', 'default',
                    'run', '--network', 'bridge', 'image'], cwd=ROOT, env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=error_stream)
                try:
                    if not missing_proxy and not handoff and not hang_proxy:
                        deadline = monotonic() + 10
                        while not worker_live.exists() and monotonic() < deadline and process.poll() is None:
                            sleep(0.02)
                        self.assertTrue(worker_live.exists(), errors.read_text())
                        process.send_signal(stop_signal)
                    self.assertEqual(process.wait(timeout=10), 124 if hang_proxy else 1 if missing_proxy else 128 + stop_signal)
                    self.assertEqual(process.stdout.read(), b'')
                    output = errors.read_text()
                    commands = calls.read_text() if calls.exists() else ''
                    if handoff:
                        if 'Docker retirement is unconfirmed; retain ' in output:
                            scratch = Path(output.split('Docker retirement is unconfirmed; retain ', 1)[1].split(' and inspect', 1)[0])
                            self.assertTrue(scratch.is_dir())
                        else:
                            self.assertEqual(handoff, 'proxy')
                            self.assertFalse(proxy_live.exists())
                        if handoff == 'docker':
                            self.assertIsNotNone(scratch)
                            self.assertTrue(proxy_live.exists())
                        self.assertNotIn('rm --force ' + WORKER, commands)
                    elif fail_removal or fail_proxy or missing_proxy or missing_worker or hang_proxy:
                        self.assertIn('Docker retirement is unconfirmed; retain ', output)
                        scratch = Path(output.split('Docker retirement is unconfirmed; retain ', 1)[1].split(' and inspect', 1)[0])
                        self.assertTrue(scratch.is_dir())
                        if fail_proxy:
                            self.assertIn('rm --force ' + WORKER, commands)
                            self.assertIn('rm --force ' + PROXY, commands)
                            self.assertTrue(proxy_live.exists())
                        else:
                            self.assertNotIn('rm --force ' + PROXY, commands)
                    else:
                        self.assertIn('rm --force ' + WORKER, commands)
                        self.assertIn('rm --force ' + PROXY, commands)
                        self.assertLess(commands.index('rm --force ' + WORKER), commands.index('rm --force ' + PROXY))
                        self.assertFalse(worker_live.exists())
                        self.assertFalse(proxy_live.exists())
                    if missing_proxy or hang_proxy:
                        self.assertFalse(worker_live.exists())
                        self.assertNotIn('rm --force', commands)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    if scratch is not None:
                        shutil.rmtree(scratch)
                    process.stdout.close()

    def test_term_retires_worker_then_proxy_by_exact_id(self):
        self.run_case()

    def test_failed_worker_removal_retains_proxy_and_receipts(self):
        self.run_case(fail_removal=True)

    def test_failed_proxy_removal_retains_its_receipt(self):
        self.run_case(fail_proxy=True)

    def test_missing_proxy_cid_does_not_start_worker_or_guess(self):
        self.run_case(missing_proxy=True)

    def test_proxy_start_timeout_does_not_start_worker_or_guess(self):
        self.run_case(hang_proxy=True)

    def test_missing_worker_cid_retains_proxy_and_does_not_guess(self):
        self.run_case(missing_worker=True)

    def test_signal_during_proxy_pid_handoff_never_starts_worker(self):
        self.run_case(handoff='proxy')

    def test_signal_during_worker_pid_handoff_never_guesses_worker(self):
        self.run_case(handoff='docker')

    def test_hup_and_interrupt_preserve_status(self):
        for stop_signal in (signal.SIGHUP, signal.SIGINT):
            with self.subTest(stop_signal=stop_signal):
                self.run_case(stop_signal=stop_signal)
