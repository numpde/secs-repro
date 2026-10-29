"""An idle native exit is settled before the serial runtime admits more work."""

import asyncio
from hashlib import sha256
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest

from provider_core.attempt_record import RecoveryScope
from provider_core.job_input import FeedInputFacts
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from secs_inference.provider.runtime import run_execution
from secs_inference.provider.worker import WorkerClient


class PreStartReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_exit_is_retired_and_replaced_before_controller_admission(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for part in ("ipc", "scratch", "status"):
                (root / part).mkdir(mode=0o700)
            endpoint = root / "ipc/worker.sock"
            (endpoint.parent / ".owner-session.lock").touch(mode=0o600)
            pid_file = root / "worker.pid"
            supervisor = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "provider_core.runner", "--role", "secs",
                "--socket", str(endpoint), "--scratch", str(root / "scratch"),
                "--status-directory", str(root / "status"), "--owner-session-lifetime",
                "--startup-seconds", "2", "--terminate-seconds", "2", "--kill-seconds", "2",
                "--cleanup-seconds", "2", "--", sys.executable, "-P",
                "/workspace/tests/integration/native_worker_fixture.py", "--mode", "normal",
                "--pid-file", str(pid_file), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            worker = WorkerClient(endpoint, startup_seconds=5, retirement_seconds=5, observation_seconds=1)
            descriptor = None
            try:
                await worker.check_ready(deadline=monotonic() + 5)
                idle_pid = (await worker.request({"operation": "pid"}, deadline=monotonic() + 2))["pid"]
                descriptor = os.pidfd_open(idle_pid)
                original_operation = worker.native.session.operation
                original = worker.native
                os.kill(idle_pid, signal.SIGKILL)
                async with asyncio.timeout(2):
                    while not select.select([descriptor], [], [], 0)[0]:
                        await asyncio.sleep(0.01)
                stop, admitted = asyncio.Event(), []
                facts = FeedInputFacts("job:chosen", "nmr.job.specification.text.v1",
                                       "sha256:" + sha256(b"C2H6O").hexdigest(), 5)
                class Discovery:
                    async def jobs(self, **_query):
                        return SimpleNamespace(jobs=(SimpleNamespace(input=facts),))
                class Controller:
                    shutdown_ready = True
                    async def run(self, _pending):
                        admitted.append((int(pid_file.read_text()), worker.native.session.operation))
                        stop.set()
                claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152))
                await run_execution(api=Discovery(), scope=RecoveryScope("https://api.test", "provider:test", "secs", "idle"),
                                    claims=claims, stop=stop, poll_seconds=0.01, assemble=lambda _pending: Controller(),
                                    worker=worker, startup_seconds=5)
                self.assertEqual(len(admitted), 1)
                self.assertNotEqual(admitted[0][0], idle_pid, "Controller admission borrowed an already-dead idle child")
                self.assertNotEqual(admitted[0][1], original_operation)
                self.assertTrue(original.reader.done())
                self.assertTrue(original.session.retirement.scratch_released)
                self.assertEqual(original.session.original_exit.returncode, -signal.SIGKILL)
                self.assertFalse(claims.live_owners())
            finally:
                try:
                    await worker.stop()
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    if supervisor.returncode is None:
                        supervisor.terminate()
                    await asyncio.wait_for(supervisor.wait(), 10)
