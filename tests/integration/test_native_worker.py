"""The actual SECS adapter must settle physical readers before source release."""

import asyncio
import errno
import json
import os
from pathlib import Path
import select
import signal
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from secs_inference.provider.worker import NativeWorkerExited, WorkerClient, WorkerError, WorkerStopUnconfirmed


class WorkerDispatchTests(unittest.IsolatedAsyncioTestCase):
    def client(self, directory):
        worker = WorkerClient(Path(directory) / "worker.sock", startup_seconds=10, retirement_seconds=10)
        worker._reader = object()
        worker._writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
        return worker

    async def test_expired_deadline_does_not_start_another_scientific_exchange(self):
        with TemporaryDirectory() as temporary:
            worker = self.client(temporary)
            try:
                with self.assertRaises(TimeoutError):
                    await worker.request({"operation": "inspect"}, deadline=monotonic() - 1)
                worker._writer.write.assert_not_called()
            finally:
                await worker.stop()

    async def test_queued_scientific_task_cannot_write_after_its_deadline_and_stop(self):
        with TemporaryDirectory() as temporary:
            worker = self.client(temporary)
            entered, release = asyncio.Event(), asyncio.Event()
            own = worker._own
            def deferred(operation, *arguments):
                async def delayed():
                    entered.set()
                    await release.wait()
                    return await operation(*arguments)
                return own(delayed)
            worker._own = deferred
            try:
                request = asyncio.create_task(worker.request({"operation": "inspect"}, deadline=monotonic() + 0.2))
                await asyncio.wait_for(entered.wait(), 1)
                with self.assertRaises(TimeoutError):
                    await request
                release.set()
                await asyncio.sleep(0)
                worker._writer.write.assert_not_called()
            finally:
                release.set()
                await worker.stop()


class NativeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.workspace = TemporaryDirectory(prefix="secs-native-binding-")
        self.root = Path(self.workspace.name)
        for part in ("ipc", "scratch", "status"):
            (self.root / part).mkdir(mode=0o700)
        self.endpoint = self.root / "ipc/worker.sock"
        (self.endpoint.parent / ".owner-session.lock").touch(mode=0o600)
        self.pid_file = self.root / "worker.pid"
        self.owner = self.worker = None
        self.log = (self.root / "supervisor.log").open("wb")

    async def start(self, mode="normal", *, scratch="scratch"):
        self.owner = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "provider_core.runner", "--role", "secs",
            "--socket", str(self.endpoint), "--scratch", str(self.root / scratch),
            "--status-directory", str(self.root / "status"), "--owner-session-lifetime",
            "--startup-seconds", "2", "--terminate-seconds", "2", "--kill-seconds", "2",
            "--cleanup-seconds", "2", "--", sys.executable, "-I",
            str(Path(__file__).with_name("native_worker_fixture.py")), "--mode", mode,
            "--pid-file", str(self.pid_file), stdout=self.log, stderr=self.log,
        )
        if self.worker is None:
            self.worker = WorkerClient(self.endpoint, startup_seconds=10, retirement_seconds=10, observation_seconds=2)

    async def asyncTearDown(self):
        try:
            if self.worker is not None:
                try:
                    await asyncio.wait_for(self.worker.stop(), 12)
                except Exception:
                    # Post-assertion containment does not establish a test's proof.
                    pass
            if self.owner is not None and self.owner.returncode is None:
                self.owner.terminate()
                await asyncio.wait_for(self.owner.wait(), 20)
        finally:
            self.log.close()
            self.workspace.cleanup()

    def assert_retired(self, descriptor):
        self.assertTrue(select.select([descriptor], [], [], 0)[0], "The original PID remains alive")
        self.assertTrue(self.worker.native.session.retirement.scratch_released)
        self.assertEqual(list((self.root / "scratch").iterdir()), [])

    async def fatal_probe(self, mode):
        await self.start()
        probe = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("native_fatal_probe.py")), str(self.endpoint), mode,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(probe.communicate(), 20)
        self.assertEqual(probe.returncode, 0, stderr.decode())
        self.assertEqual(json.loads(stdout), {"retired": True, "fatal_observed": True})

    async def test_contained_scientific_task_exit_is_retained_through_physical_retirement(self):
        await self.fatal_probe("science")

    async def test_original_control_future_fatal_requires_physical_proof_under_the_same_deadline(self):
        await self.fatal_probe("control")

    async def test_healthy_serial_work_reuses_one_loaded_child(self):
        await self.start()
        await self.worker.check_ready(deadline=monotonic() + 10)
        first = await self.worker.request({"operation": "pid"}, deadline=monotonic() + 3)
        generation = self.worker.native.session.operation
        await self.worker.check_ready(deadline=monotonic() + 3)
        second = await self.worker.request({"operation": "pid"}, deadline=monotonic() + 3)
        self.assertEqual(first["pid"], second["pid"])
        self.assertEqual(second["calls"], 2)
        self.assertEqual(self.worker.native.session.operation, generation)
        descriptor = os.pidfd_open(first["pid"])
        try:
            await self.worker.stop()
            self.assert_retired(descriptor)
        finally:
            os.close(descriptor)

    async def test_deadline_stops_a_resistant_child_while_an_api_observer_is_blocked(self):
        await self.start()
        await self.worker.check_ready(deadline=monotonic() + 10)
        pid = (await self.worker.request({"operation": "pid"}, deadline=monotonic() + 3))["pid"]
        descriptor = os.pidfd_open(pid)
        release, waiting = asyncio.Event(), asyncio.Event()
        async def api_observer():
            waiting.set()
            await release.wait()
        observer = asyncio.create_task(api_observer())
        entered = self.root / "entered"
        deadline = monotonic() + 2
        request = asyncio.create_task(self.worker.request({"operation": "hang", "entered": str(entered)}, deadline=deadline))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            async with asyncio.timeout(2):
                while not entered.exists():
                    await asyncio.sleep(0.01)
            with self.assertRaises(TimeoutError):
                await request
            await self.worker.stop()
            self.assert_retired(descriptor)
            self.assertFalse(observer.done(), "Native cleanup depended on the blocked API observer")
            self.assertLess(monotonic() - deadline, 10)
            self.assertEqual(self.worker.native.session.retirement.returncode, -signal.SIGKILL)
            self.assertIsNone(self.worker.native.session.original_exit)
        finally:
            release.set()
            await asyncio.gather(observer, request, return_exceptions=True)
            os.close(descriptor)

    async def test_actual_child_failure_retains_structural_causes_without_private_messages(self):
        await self.start()
        await self.worker.check_ready(deadline=monotonic() + 10)
        pid = (await self.worker.request({"operation": "pid"}, deadline=monotonic() + 3))["pid"]
        descriptor = os.pidfd_open(pid)
        try:
            response = await self.worker.request({"operation": "fail"}, deadline=monotonic() + 3)
            self.assertEqual(response["outcome"], "failed")
            self.assertEqual(response["exception_type"], "RuntimeError")
            self.assertEqual(response["cause"]["errno"], errno.EIO)
            self.assertNotIn("private fixture", json.dumps(response))
            await self.worker.stop()
            self.assert_retired(descriptor)
        finally:
            os.close(descriptor)

    async def test_repeated_retired_generations_release_the_shared_control_threads(self):
        await self.start()
        tasks = Path("/proc/self/task")
        baseline = len(tuple(tasks.iterdir()))
        generations = []
        for _generation in range(5):
            await self.worker.check_ready(deadline=monotonic() + 10)
            generations.append(self.worker.native.session.operation)
            await self.worker.stop()
            async with asyncio.timeout(1):
                while len(tuple(tasks.iterdir())) > baseline:
                    await asyncio.sleep(0.01)
            self.assertEqual(len(tuple(tasks.iterdir())), baseline)
        self.assertEqual(len(set(generations)), 5)

    async def test_replacement_supervisor_cannot_prove_cleanup_of_the_original_scratch(self):
        await self.start()
        await self.worker.check_ready(deadline=monotonic() + 10)
        pid = (await self.worker.request({"operation": "pid"}, deadline=monotonic() + 3))["pid"]
        original = self.worker.native.session
        retained = tuple((self.root / "scratch").iterdir())
        self.assertTrue(retained)
        with self.assertRaises(TimeoutError):
            await self.worker.native.observe_retirement(wait_seconds=0.01)
        reader = self.worker.native.reader
        descriptor = os.pidfd_open(pid)
        try:
            self.owner.kill()
            await self.owner.wait()
            if not select.select([descriptor], [], [], 0)[0]:
                os.kill(pid, signal.SIGKILL)
            async with asyncio.timeout(3):
                while not reader.done() or not select.select([descriptor], [], [], 0)[0]:
                    await asyncio.sleep(0.01)
            self.assertIsNotNone(self.worker.native.retirement_failure)
            replacement = self.root / "replacement-scratch"
            replacement.mkdir(mode=0o700)
            await self.start(scratch=replacement.name)
            with self.assertRaises(WorkerStopUnconfirmed):
                await self.worker.stop()
            self.assertFalse(self.worker.stopped)
            self.assertIs(self.worker.native.reader, reader)
            self.assertEqual(original.expected_lock_identity, self.worker.proof.session.lock_identity)
            self.assertNotEqual(original.generation, self.worker.proof.session.generation)
            self.assertEqual(tuple((self.root / "scratch").iterdir()), retained)
            self.assertTrue(self.worker.proof.session.retirement.scratch_released)
        finally:
            os.close(descriptor)

    async def test_startup_exit_is_observed_before_local_interruption_and_survives_cleanup(self):
        await self.start("startup_exit")
        opening = asyncio.create_task(self.worker.check_ready(deadline=monotonic() + 10))
        async with asyncio.timeout(3):
            while not self.pid_file.exists():
                await asyncio.sleep(0.01)
        descriptor = os.pidfd_open(int(self.pid_file.read_text()))
        try:
            Path(str(self.pid_file) + ".exit").touch()
            with self.assertRaises(NativeWorkerExited) as caught:
                await opening
            original = caught.exception.runner.original_exit
            self.assertEqual(original.returncode, 23)
            await self.worker.stop()
            self.assertIs(caught.exception.runner.original_exit, original)
            self.assert_retired(descriptor)
        finally:
            os.close(descriptor)

    async def test_startup_science_eof_with_a_live_child_joins_one_control_reader_after_stop(self):
        await self.start("startup_eof_alive")
        with self.assertRaises(WorkerError):
            await self.worker.check_ready(deadline=monotonic() + 10)
        original = self.worker.native.reader
        self.assertFalse(original.done())
        descriptor = os.pidfd_open(int(self.pid_file.read_text()))
        try:
            await self.worker.stop()
            self.assertIs(self.worker.native.reader, original)
            self.assert_retired(descriptor)
            self.assertIsNone(self.worker.native.session.original_exit)
        finally:
            os.close(descriptor)
