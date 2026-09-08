"""A stopped wait must never masquerade as a stopped scientific process."""

from contextlib import contextmanager
import os
import json
from pathlib import Path
import socket
import struct
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import unittest

from secs_inference.provider.worker import WorkerClient, WorkerError, WorkerStopUnconfirmed, serve_worker, _serve_session, _send, _receive


def load_test_handler():
    calls = 0
    def handle(command):
        nonlocal calls
        calls += 1
        if command["operation"] == "crash":
            os._exit(1)
        if command["operation"] == "forge_stop":
            return {"outcome": "stopped"}
        if command["operation"] == "hang":
            while True:
                sleep(1)
        return {"outcome": "test_result", "pid": os.getpid(), "calls": calls}
    return handle


def load_broken_handler():
    raise FileNotFoundError("private model path must not cross the diagnostic boundary")


@contextmanager
def worker_connection(load_handler=load_test_handler):
    with TemporaryDirectory() as directory:
        path = Path(directory) / "worker.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        def serve():
            connection, _ = listener.accept()
            with connection:
                _serve_session(connection, load_handler)
        thread = Thread(target=serve, daemon=True)
        thread.start()
        client = None
        try:
            client = WorkerClient(path, startup_deadline=monotonic() + 5)
            yield client
        finally:
            if client is not None and not client.stopped:
                client.stop()
            thread.join(5)
            listener.close()
            if thread.is_alive():
                raise AssertionError("Worker supervisor did not finish")


class WorkerTests(unittest.TestCase):
    def test_non_regular_supervisor_lock_cannot_block_worker_startup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            os.mkfifo(root / "owner.lock", mode=0o600)
            with self.assertRaisesRegex(WorkerError, "lock is not a regular file"):
                serve_worker(root / "worker.sock", load_test_handler)
            self.assertFalse((root / "worker.sock").exists())

    def test_failed_startup_preserves_safe_diagnostics_and_confirms_child_exit(self):
        with self.assertRaises(WorkerError) as failure:
            with worker_connection(load_broken_handler):
                self.fail("Broken model was reported ready")
        self.assertNotIsInstance(failure.exception, WorkerStopUnconfirmed)
        diagnostic = failure.exception.diagnostic
        self.assertEqual(diagnostic["exception_type"], "FileNotFoundError")
        self.assertNotIn("private model path", json.dumps(diagnostic))

    def test_child_crash_is_acknowledged_after_reaping_not_mistaken_for_timeout(self):
        with worker_connection() as worker:
            first = worker.request({"operation": "count"}, deadline=monotonic() + 2)
            with self.assertRaisesRegex(WorkerError, "exit was confirmed") as failure:
                worker.request({"operation": "crash"}, deadline=monotonic() + 2)
            self.assertNotIsInstance(failure.exception, WorkerStopUnconfirmed)
            self.assertTrue(worker.stopped)
            with self.assertRaises(ProcessLookupError):
                os.kill(first["pid"], 0)

    def test_rejected_child_protocol_is_reaped_before_supervisor_stop_acknowledgement(self):
        with worker_connection() as worker:
            first = worker.request({"operation": "count"}, deadline=monotonic() + 2)
            with self.assertRaisesRegex(WorkerError, "communication failed; its analysis process was confirmed stopped") as failure:
                worker.request({"operation": "forge_stop"}, deadline=monotonic() + 2)
            self.assertNotIsInstance(failure.exception, WorkerStopUnconfirmed)
            self.assertTrue(worker.stopped)
            with self.assertRaises(ProcessLookupError):
                os.kill(first["pid"], 0)

    def _scripted_peer(self, script, exercise):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "peer.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.listen(1)
            def serve():
                connection, _ = listener.accept()
                with connection:
                    _send(connection, {"outcome": "ready"})
                    script(connection)
            thread = Thread(target=serve, daemon=True)
            thread.start()
            client = WorkerClient(path, startup_deadline=monotonic() + 5)
            try:
                exercise(client)
            finally:
                client.socket.close()
                listener.close()
                thread.join(5)
                self.assertFalse(thread.is_alive())

    def test_partial_raced_result_is_drained_before_stop_acknowledgement(self):
        def serve(connection):
            _receive(connection)
            raw = json.dumps({"outcome": "analysed", "padding": "x" * 100}).encode()
            frame = struct.pack("!I", len(raw)) + raw
            connection.sendall(frame[:8])
            self.assertEqual(_receive(connection)["operation"], "cancel")
            connection.sendall(frame[8:])
            _send(connection, {"outcome": "stopped"})
        def exercise(worker):
            with self.assertRaises(TimeoutError):
                worker.request({"operation": "analyse"}, deadline=monotonic() + 0.05)
            self.assertTrue(worker.stopped)
        self._scripted_peer(serve, exercise)

    def test_queued_stop_ack_is_read_even_when_cancel_can_no_longer_be_sent(self):
        closed = Event()
        def serve(connection):
            _send(connection, {"outcome": "stopped"})
            connection.close()
            closed.set()
        def exercise(worker):
            self.assertTrue(closed.wait(2))
            worker.stop()
            self.assertTrue(worker.stopped)
        self._scripted_peer(serve, exercise)

    def test_normal_requests_share_one_warm_child(self):
        with worker_connection() as worker:
            first = worker.request({"operation": "count"}, deadline=monotonic() + 2)
            second = worker.request({"operation": "count"}, deadline=monotonic() + 2)
            self.assertEqual(first["pid"], second["pid"])
            self.assertEqual(second["calls"], 2)
            worker.stop()
            with self.assertRaises(ProcessLookupError):
                os.kill(first["pid"], 0)

    def test_deadline_reaps_blocked_computation_before_releasing_the_wait(self):
        with worker_connection() as worker:
            first = worker.request({"operation": "count"}, deadline=monotonic() + 2)
            with self.assertRaises(TimeoutError):
                worker.request({"operation": "hang"}, deadline=monotonic() + 0.1)
            self.assertTrue(worker.stopped)
            with self.assertRaises(ProcessLookupError):
                os.kill(first["pid"], 0)
