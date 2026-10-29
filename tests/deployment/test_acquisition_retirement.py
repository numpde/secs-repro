"""Exercise acquisition ownership when a Docker CID has not been published."""

import os
from pathlib import Path
import signal
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AcquisitionRetirementTests(unittest.TestCase):
    def test_term_during_proxy_pid_handoff_is_deferred_until_identity_is_owned(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "docker-started"
            docker = root / "docker"
            docker.write_text('#!/bin/bash\ntouch "$STARTED"\n')
            docker.chmod(0o700)
            proxy = root / "python3"
            proxy.write_text('#!/bin/bash\nwhile [[ $1 != --ready-file ]]; do shift; done\n'
                             'printf 12345 > "$2"\ntrap "" TERM; exec sleep 300\n')
            proxy.chmod(0o700)
            debug = root / "debug.sh"
            debug.write_text('trap \'if [[ $BASH_COMMAND == "proxy_pid=\\$!" ]]; then trap - DEBUG; kill -TERM "$$"; fi\' DEBUG\n')
            env = os.environ | {"PATH": f"{root}:/usr/bin:/bin", "SECS_WLAN_INTERFACE_INPUT": "lo",
                                "STARTED": str(marker), "BASH_ENV": str(debug)}
            result = subprocess.run(["bash", str(ROOT / "tools/docker.sh"), "--context", "default",
                                     "run", "--network", "bridge", "image"], cwd=ROOT, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    timeout=10)
            self.assertEqual(result.returncode, 143, result.stderr)
            self.assertFalse(marker.exists(), "Docker started after cancellation")

    def test_term_during_docker_pid_handoff_retains_uncertain_receipt(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / "docker"
            docker.write_text('#!/bin/bash\nexec sleep 300\n')
            docker.chmod(0o700)
            proxy = root / "python3"
            proxy.write_text('#!/bin/bash\nwhile [[ $1 != --ready-file ]]; do shift; done\n'
                             'printf 12345 > "$2"\nexec sleep 300\n')
            proxy.chmod(0o700)
            debug = root / "debug.sh"
            debug.write_text('trap \'if [[ $BASH_COMMAND == "docker_pid=\\$!" ]]; then trap - DEBUG; kill -TERM "$$"; fi\' DEBUG\n')
            errors = root / "errors"
            env = os.environ | {"PATH": f"{root}:/usr/bin:/bin", "SECS_WLAN_INTERFACE_INPUT": "lo",
                                "BASH_ENV": str(debug)}
            scratch = None
            with errors.open("w") as error_stream:
                result = subprocess.run(["bash", str(ROOT / "tools/docker.sh"), "--context", "default",
                                         "run", "--network", "bridge", "image"], cwd=ROOT, env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_stream,
                                        timeout=10)
            try:
                self.assertEqual(result.returncode, 143)
                text = errors.read_text()
                self.assertIn("Docker retirement is unconfirmed; retain ", text)
                scratch = Path(text.split("Docker retirement is unconfirmed; retain ", 1)[1].split(" and inspect", 1)[0])
                self.assertTrue(scratch.is_dir())
            finally:
                if scratch is not None and (scratch / "proxy.pid").is_file():
                    try:
                        os.kill(int((scratch / "proxy.pid").read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    for path in scratch.iterdir():
                        path.unlink()
                    scratch.rmdir()

    def run_cid_case(self, *, removal_fails, stop_signal=signal.SIGTERM):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker, calls, errors = root / "started", root / "calls", root / "errors"
            cid = "a" * 64
            docker = root / "docker"
            docker.write_text(
                '#!/bin/bash\nwhile [[ $1 != run && $1 != ps && $1 != rm ]]; do shift; done\n'
                'op=$1; shift\nprintf "%s %s\\n" "$op" "$*" >> "$CALLS"\n'
                'case "$op" in\n'
                'run) while [[ $1 != --cidfile ]]; do shift; done; printf "%s" "$CID" > "$2"; '
                'touch "$STARTED"; trap "" TERM; exec sleep 300 ;;\n'
                'ps) [[ -e $STARTED ]] && printf "%s\\n" "$CID"; exit 0 ;;\n'
                'rm) [[ $REMOVE_FAIL == 1 ]] && exit 1; rm "$STARTED" ;;\nesac\n'
            )
            docker.chmod(0o700)
            proxy = root / "python3"
            proxy.write_text('#!/bin/bash\nwhile [[ $1 != --ready-file ]]; do shift; done\n'
                             'printf 12345 > "$2"\nexec sleep 300\n')
            proxy.chmod(0o700)
            env = os.environ | {"PATH": f"{root}:/usr/bin:/bin", "SECS_WLAN_INTERFACE_INPUT": "lo",
                                "STARTED": str(marker), "CALLS": str(calls), "CID": cid,
                                "REMOVE_FAIL": "1" if removal_fails else "0"}
            scratch = None
            with errors.open("w") as error_stream:
                process = subprocess.Popen(["bash", str(ROOT / "tools/docker.sh"), "--context", "default",
                                            "run", "--network", "bridge", "image"], cwd=ROOT, env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_stream)
                try:
                    deadline = monotonic() + 10
                    while not marker.exists() and monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail("Wrapper exited before Docker started")
                        sleep(0.02)
                    self.assertTrue(marker.exists())
                    process.send_signal(stop_signal)
                    self.assertEqual(process.wait(timeout=10), 128 + stop_signal)
                    text = errors.read_text()
                    self.assertIn("rm --force " + cid, calls.read_text())
                    if removal_fails:
                        self.assertIn("Docker retirement is unconfirmed; retain ", text)
                        scratch = Path(text.split("Docker retirement is unconfirmed; retain ", 1)[1].split(" and inspect", 1)[0])
                        self.assertEqual((scratch / "worker.cid").read_text(), cid)
                        self.assertTrue(marker.exists())
                    else:
                        self.assertNotIn("Docker retirement is unconfirmed", text)
                        self.assertFalse(marker.exists())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    if scratch is not None and (scratch / "proxy.pid").is_file():
                        try:
                            os.kill(int((scratch / "proxy.pid").read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        for path in scratch.iterdir():
                            path.unlink()
                        scratch.rmdir()

    def test_term_kills_resistant_client_and_removes_only_recorded_id(self):
        self.run_cid_case(removal_fails=False)

    def test_failed_exact_removal_retains_cid_and_reports_uncertainty(self):
        self.run_cid_case(removal_fails=True)

    def test_hup_and_interrupt_retain_their_signal_status(self):
        for stop_signal in (signal.SIGHUP, signal.SIGINT):
            with self.subTest(stop_signal=stop_signal):
                self.run_cid_case(removal_fails=False, stop_signal=stop_signal)

    def test_term_before_cid_retains_receipt_and_does_not_guess_container(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker, calls, errors = root / "started", root / "calls", root / "errors"
            docker = root / "docker"
            docker.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\n'
                              'touch "$STARTED"\nexec sleep 300\n')
            docker.chmod(0o700)
            proxy = root / "python3"
            proxy.write_text('#!/bin/bash\nwhile [[ $1 != --ready-file ]]; do shift; done\n'
                             'printf 12345 > "$2"\nexec sleep 300\n')
            proxy.chmod(0o700)
            env = os.environ | {"PATH": f"{root}:/usr/bin:/bin", "SECS_WLAN_INTERFACE_INPUT": "lo",
                                "STARTED": str(marker), "CALLS": str(calls)}
            scratch = None
            with errors.open("w") as error_stream:
                process = subprocess.Popen(["bash", str(ROOT / "tools/docker.sh"), "--context", "default",
                                            "run", "--network", "bridge", "image"], cwd=ROOT, env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_stream)
                try:
                    deadline = monotonic() + 10
                    while not marker.exists() and monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail("Wrapper exited before Docker started")
                        sleep(0.02)
                    self.assertTrue(marker.exists())
                    process.terminate()
                    self.assertEqual(process.wait(timeout=10), 143)
                    text = errors.read_text()
                    self.assertIn("Docker retirement is unconfirmed; retain ", text)
                    scratch = Path(text.split("Docker retirement is unconfirmed; retain ", 1)[1].split(" and inspect", 1)[0])
                    self.assertTrue(scratch.is_dir())
                    self.assertNotIn("rm --force", calls.read_text())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    if scratch is not None and (scratch / "proxy.pid").is_file():
                        try:
                            os.kill(int((scratch / "proxy.pid").read_text()), signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        for path in scratch.iterdir():
                            path.unlink()
                        scratch.rmdir()


if __name__ == "__main__":
    unittest.main()
