"""An interrupted acquisition must retire its own Docker container."""

import os
from pathlib import Path
import subprocess
from time import monotonic, sleep
import unittest
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]


class AcquisitionRetirementTests(unittest.TestCase):
    def test_networked_run_preserves_input_output_and_exit_status(self):
        for interface in ("lo", ""):
            with self.subTest(interface=interface):
                self._check_run_input_output_and_exit_status(interface)

    def _check_run_input_output_and_exit_status(self, interface):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        environment = os.environ | {"SECS_NETWORK_INTERFACE_INPUT": interface or "system",
                                    "SECS_NETWORK_INTERFACE_ORIGIN": "command line"}
        result = subprocess.run(
            ["bash", str(ROOT / "tools/docker.sh"), "--context", "default", "run", "--rm", "-i",
             "--network", "bridge", "--pull", "never", "--entrypoint", "sh", image,
             "-c", "cat; printf failure >&2; exit 7"],
            cwd=ROOT, env=environment, input=b"exact-stream\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, b"exact-stream\n")
        self.assertIn(b"failure", result.stderr)

    def test_term_retires_the_running_container(self):
        for interface in ("lo", ""):
            with self.subTest(interface=interface):
                self._check_term_retires_container(interface)

    def _check_term_retires_container(self, interface):
        image = os.environ["DEPLOYMENT_TEST_IMAGE"]
        name = "secs-acquisition-proof-" + uuid4().hex
        environment = os.environ | {"SECS_NETWORK_INTERFACE_INPUT": interface or "system",
                                    "SECS_NETWORK_INTERFACE_ORIGIN": "command line"}
        process = subprocess.Popen(
            ["bash", str(ROOT / "tools/docker.sh"), "--context", "default", "run", "--rm",
             "--name", name, "--network", "bridge", "--pull", "never", "--entrypoint", "sh",
             image, "-c", "sleep 300"],
            cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = monotonic() + 15
            while monotonic() < deadline:
                observed = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", name],
                                          capture_output=True, text=True)
                if observed.returncode == 0 and observed.stdout.strip() == "true":
                    break
                if process.poll() is not None:
                    self.fail("Acquisition wrapper exited before its container started")
                sleep(0.1)
            else:
                self.fail("Disposable acquisition container did not start")
            process.terminate()
            try:
                status = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail("Acquisition wrapper did not retire its container after TERM")
            self.assertEqual(status, 143)
            observed = subprocess.run(["docker", "inspect", name], capture_output=True)
            self.assertNotEqual(observed.returncode, 0, "Interrupted acquisition container still exists")
        finally:
            subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
