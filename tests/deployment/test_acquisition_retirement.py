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
    def run_case(self, *, fail_removal=False, fail_proxy=False, missing_proxy=False, missing_worker=False, invalid_stream=False, proxy_start_fails=False, bad_proxy_settings=False, hang_proxy_start=False,
                 handoff=None, hang_proxy=False, stop_signal=signal.SIGTERM):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / 'docker'
            docker.write_text('''#!/bin/bash
arguments=("$@")
while [[ $1 != run && $1 != create && $1 != start && $1 != ps && $1 != rm && $1 != inspect ]]; do shift; done
op=$1; shift
printf '%s %s %s\\n' "${arguments[1]}" "$op" "$*" >> "$CALLS"
case "$op" in
run|create)
  cidfile=; receipt=
  while (($#)); do
    case $1 in
      --cidfile) cidfile=$2; shift ;;
      --mount) case $2 in *,dst=/receipt) receipt=${2#type=bind,src=}; receipt=${receipt%,dst=/receipt};; esac; shift ;;
    esac
    shift
  done
  if [[ -n $receipt ]]; then
    [[ $HANG_PROXY == 1 ]] && exec sleep 300
    [[ $MISSING == 1 ]] && exit 1
    printf '%s' "$PROXY" > "$cidfile"; touch "$PROXY_EXISTS"; printf '%s' "$receipt" > "$PROXY_RECEIPT"; printf '%s\\n' "$PROXY"
  else
    [[ $MISSING_WORKER == 1 ]] && exit 1
    printf '%s' "$WORKER" > "$cidfile"; touch "$WORKER_EXISTS"; printf '%s\\n' "$WORKER"
  fi ;;
start) case "$*" in *"$PROXY"*) touch "$PROXY_LIVE"; [[ $PROXY_START_HANG == 1 ]] && exec sleep 300; [[ $PROXY_START_FAIL == 1 ]] && exit 1; printf 12345 > "$(cat "$PROXY_RECEIPT")/proxy.port" ;; *) touch "$WORKER_LIVE"; trap '' TERM; exec sleep 300 ;; esac ;;
inspect) case "$*" in *"$PROXY"*) if [[ $* == *State.Status* ]]; then if [[ $BAD_PROXY_SETTINGS == 1 ]]; then printf 'running always\\n'; else printf 'created no\\n'; fi; else printf 'true\\n'; fi ;; *) if [[ $INVALID_STREAM == 1 ]]; then printf 'created false false true false no\\n'; else printf 'created false false true true no\\n'; fi ;; esac ;;
ps)
  case "$*" in *"$PROXY"*) [[ -e $PROXY_EXISTS ]] && printf '%s\\n' "$PROXY" ;; *"$WORKER"*) [[ -e $WORKER_EXISTS ]] && printf '%s\\n' "$WORKER" ;; esac
  exit 0 ;;
rm)
  case "$*" in *"$WORKER"*) [[ $FAIL == 1 ]] && exit 1; rm -f "$WORKER_LIVE" "$WORKER_EXISTS" ;; *"$PROXY"*) [[ $FAIL_PROXY == 1 ]] && exit 1; rm -f "$PROXY_LIVE" "$PROXY_EXISTS" ;; esac ;;
esac
''')
            docker.chmod(0o700)
            calls, errors = root / 'calls', root / 'errors'
            worker_live, proxy_live = root / 'worker', root / 'proxy'
            env = os.environ | {'PATH': f'{root}:/usr/bin:/bin', 'SECS_NETWORK_INTERFACE_INPUT': 'lo',
                'SECS_DOCKER_ENDPOINT': 'unix:///tmp/test-docker.sock',
                'SECS_NETWORK_INTERFACE_ORIGIN': 'command line',
                'SECS_BOOTSTRAP_IMAGE': 'pinned-test-image', 'CALLS': str(calls), 'PROXY': PROXY,
                'WORKER': WORKER, 'PROXY_LIVE': str(proxy_live), 'WORKER_LIVE': str(worker_live),
                'PROXY_EXISTS': str(root / 'proxy-created'), 'PROXY_RECEIPT': str(root / 'proxy-receipt-dir'),
                'WORKER_EXISTS': str(root / 'worker-created'), 'INVALID_STREAM': '1' if invalid_stream else '0',
                'PROXY_START_FAIL': '1' if proxy_start_fails else '0',
                'PROXY_START_HANG': '1' if hang_proxy_start else '0',
                'BAD_PROXY_SETTINGS': '1' if bad_proxy_settings else '0',
                'FAIL': '1' if fail_removal else '0', 'MISSING': '1' if missing_proxy else '0',
                'FAIL_PROXY': '1' if fail_proxy else '0',
                'MISSING_WORKER': '1' if missing_worker or handoff == 'docker' else '0',
                'HANG_PROXY': '1' if hang_proxy else '0'}
            if hang_proxy or hang_proxy_start:
                timeout = root / 'timeout'
                timeout.write_text('#!/bin/bash\n[[ $3 == 30 ]] && set -- "$1" "$2" 0.2 "${@:4}"\nexec /usr/bin/timeout "$@"\n')
                timeout.chmod(0o700)
            if handoff:
                debug = root / 'debug.sh'
                if handoff == 'start':
                    debug.write_text('''count=0
trap 'if [[ $BASH_COMMAND == "docker_pid=\\$!" ]]; then count=$((count+1)); if [[ $count == 2 ]]; then trap - DEBUG; kill -TERM "$$"; fi; fi' DEBUG
''')
                elif handoff == 'proxy_start':
                    debug.write_text('''count=0
trap 'if [[ $BASH_COMMAND == "proxy_pid=\\$!" ]]; then count=$((count+1)); if [[ $count == 2 ]]; then trap - DEBUG; kill -TERM "$$"; fi; fi' DEBUG
''')
                elif handoff == 'proxy_post_create':
                    debug.write_text('''count=0
trap 'if [[ $BASH_COMMAND == "proxy_starting=1" ]]; then count=$((count+1)); if [[ $count == 2 ]]; then trap - DEBUG; kill -TERM "$$"; fi; fi' DEBUG
''')
                elif handoff == 'post_create':
                    debug.write_text('''trap 'if [[ $BASH_COMMAND == "worker_cid=\\$(read_cid worker)" ]]; then trap - DEBUG; kill -TERM "$$"; fi' DEBUG
''')
                else:
                    debug.write_text(f'''trap 'if [[ $BASH_COMMAND == "{handoff}_pid=\\$!" ]]; then trap - DEBUG; kill -TERM "$$"; fi' DEBUG\n''')
                env['BASH_ENV'] = str(debug)
            scratch = None
            with errors.open('w') as error_stream:
                process = subprocess.Popen(['bash', str(ROOT / 'tools/docker.sh'),
                    'run', '--network', 'bridge', 'image'], cwd=ROOT, env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=error_stream)
                try:
                    if not missing_proxy and not missing_worker and not invalid_stream and not proxy_start_fails and not bad_proxy_settings and not handoff and not hang_proxy and not hang_proxy_start:
                        deadline = monotonic() + 10
                        while not worker_live.exists() and monotonic() < deadline and process.poll() is None:
                            sleep(0.02)
                        self.assertTrue(worker_live.exists(), errors.read_text())
                        process.send_signal(stop_signal)
                    self.assertEqual(process.wait(timeout=10), 124 if hang_proxy or hang_proxy_start else 2 if invalid_stream or bad_proxy_settings else 1 if missing_proxy or missing_worker or proxy_start_fails else 128 + stop_signal)
                    self.assertEqual(process.stdout.read(), b'')
                    output = errors.read_text()
                    commands = calls.read_text() if calls.exists() else ''
                    for invocation in commands.splitlines():
                        self.assertTrue(invocation.startswith('unix:///tmp/test-docker.sock '), invocation)
                    if handoff:
                        if 'Docker retirement is unconfirmed; retain ' in output:
                            scratch = Path(output.split('Docker retirement is unconfirmed; retain ', 1)[1].split(' and inspect', 1)[0])
                            self.assertTrue(scratch.is_dir())
                        else:
                            self.assertIn(handoff, ('proxy_start', 'proxy_post_create', 'start'))
                            self.assertFalse(proxy_live.exists())
                        if handoff in ('docker', 'post_create'):
                            self.assertIsNotNone(scratch)
                            self.assertTrue(proxy_live.exists())
                        if handoff != 'start':
                            self.assertNotIn('rm --force ' + WORKER, commands)
                        if handoff in ('proxy_start', 'proxy_post_create') and scratch is None:
                            self.assertIn('rm --force ' + PROXY, commands)
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
                    elif proxy_start_fails or bad_proxy_settings or hang_proxy_start:
                        self.assertIn('rm --force ' + PROXY, commands)
                        self.assertNotIn('start --attach', commands)
                        self.assertFalse(proxy_live.exists())
                        if bad_proxy_settings:
                            self.assertNotIn('start ' + PROXY, commands)
                    else:
                        self.assertIn('rm --force ' + WORKER, commands)
                        self.assertIn('rm --force ' + PROXY, commands)
                        self.assertLess(commands.index('rm --force ' + WORKER), commands.index('rm --force ' + PROXY))
                        self.assertFalse(worker_live.exists())
                        self.assertFalse(proxy_live.exists())
                    if missing_proxy or hang_proxy:
                        self.assertFalse(worker_live.exists())
                        self.assertNotIn('rm --force', commands)
                    if missing_worker:
                        self.assertFalse(worker_live.exists())
                        self.assertNotIn('start --attach', commands)
                    if invalid_stream:
                        self.assertNotIn('start --attach', commands)
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

    def test_failed_proxy_start_retires_verified_proxy_before_worker(self):
        self.run_case(proxy_start_fails=True)

    def test_proxy_start_timeout_retires_verified_proxy_before_worker(self):
        self.run_case(hang_proxy_start=True)

    def test_running_or_restartable_proxy_is_rejected_before_start(self):
        self.run_case(bad_proxy_settings=True)

    def test_missing_worker_cid_retains_proxy_and_does_not_guess(self):
        self.run_case(missing_worker=True)

    def test_nondefault_stream_selection_is_rejected_before_start(self):
        self.run_case(invalid_stream=True)

    def test_signal_during_proxy_pid_handoff_never_starts_worker(self):
        self.run_case(handoff='proxy')

    def test_signal_after_proxy_create_retires_proxy_before_worker(self):
        self.run_case(handoff='proxy_post_create')

    def test_signal_during_proxy_start_handoff_never_starts_worker(self):
        self.run_case(handoff='proxy_start')

    def test_signal_during_worker_pid_handoff_never_guesses_worker(self):
        self.run_case(handoff='docker')

    def test_signal_during_start_handoff_retires_only_verified_worker(self):
        self.run_case(handoff='start')

    def test_signal_before_cid_validation_never_starts_worker(self):
        self.run_case(handoff='post_create')

    def test_hup_and_interrupt_preserve_status(self):
        for stop_signal in (signal.SIGHUP, signal.SIGINT):
            with self.subTest(stop_signal=stop_signal):
                self.run_case(stop_signal=stop_signal)
