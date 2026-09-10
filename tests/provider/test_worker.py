"""A stopped wait must never masquerade as a stopped scientific process."""

from contextlib import contextmanager, nullcontext
import errno
import os
import json
from pathlib import Path
import socket
import struct
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import unittest
from unittest.mock import Mock, patch

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
    def test_supervisor_releases_acquired_resources_when_child_startup_fails(self):
        for stage in ("construction", "start"):
            with self.subTest(stage=stage):
                parent, child_socket = Mock(), Mock()
                child = Mock(pid=None)
                child.is_alive.return_value = False
                context = Mock()
                context.Process.return_value = child
                original = RuntimeError("private-startup")
                if stage == "construction":
                    context.Process.side_effect = original
                else:
                    child.start.side_effect = original
                with patch("secs_inference.provider.worker.socket.socketpair", return_value=(parent, child_socket)), \
                     patch("secs_inference.provider.worker.multiprocessing.get_context", return_value=context), \
                     patch("secs_inference.provider.worker._reap") as reaping, \
                     patch("secs_inference.provider.worker._send") as sending:
                    with self.assertRaises(RuntimeError) as caught:
                        _serve_session(Mock(), Mock())
                self.assertIs(caught.exception, original)
                parent.close.assert_called_once()
                child_socket.close.assert_called_once()
                reaping.assert_not_called()
                sending.assert_not_called()
                if stage == "start":
                    child.close.assert_called_once()

    def test_resource_release_errors_do_not_prevent_reaping_or_rewrite_its_acknowledgement(self):
        parent, child_socket = Mock(), Mock()
        child = Mock(pid=123)
        child.is_alive.return_value = False
        context = Mock()
        context.Process.return_value = child
        for resource in (parent, child_socket, child):
            resource.close.side_effect = OSError(errno.EIO, "private-close")
        with patch("secs_inference.provider.worker.socket.socketpair", return_value=(parent, child_socket)), \
             patch("secs_inference.provider.worker.multiprocessing.get_context", return_value=context), \
             patch("secs_inference.provider.worker._relay", return_value="cancel"), \
             patch("secs_inference.provider.worker._reap") as reaping, \
             patch("secs_inference.provider.worker._send") as sending, \
             patch("secs_inference.provider.worker.socket_deadline", return_value=nullcontext()), \
             self.assertLogs("secs_inference.provider.worker", level="ERROR") as captured:
            _serve_session(Mock(), Mock())
        reaping.assert_called_once_with(child)
        self.assertEqual(sending.call_args.args[1], {"outcome": "stopped", "reason": "cancel"})
        for resource in (parent, child_socket, child):
            resource.close.assert_called_once()
        self.assertEqual(len(captured.output), 3)
        self.assertNotIn("private-close", "\n".join(captured.output))

    def test_relay_evidence_survives_failed_reaping_or_acknowledgement_delivery(self):
        for stage in ("reap", "delivery"):
            with self.subTest(stage=stage):
                child = Mock()
                child.is_alive.return_value = stage == "reap"
                context = Mock()
                context.Process.return_value = child
                parent, child_socket = Mock(), Mock()
                if stage == "reap":
                    parent.close.side_effect = OSError(errno.EIO, "private-close")
                stop_failure = WorkerStopUnconfirmed("Child exit could not be confirmed")
                with patch("secs_inference.provider.worker.multiprocessing.get_context", return_value=context), \
                     patch("secs_inference.provider.worker.socket.socketpair", return_value=(parent, child_socket)), \
                     patch("secs_inference.provider.worker._relay", side_effect=ValueError("private-relay")), \
                     patch("secs_inference.provider.worker._reap", side_effect=stop_failure if stage == "reap" else None), \
                     patch("secs_inference.provider.worker._send", side_effect=OSError(errno.EPIPE, "private-delivery")) as sending, \
                     patch("secs_inference.provider.worker.socket_deadline", return_value=nullcontext()), \
                     self.assertLogs("secs_inference.provider.worker", level="ERROR") as captured:
                    if stage == "reap":
                        with self.assertRaises(WorkerStopUnconfirmed) as caught:
                            _serve_session(Mock(), Mock())
                        self.assertIs(caught.exception, stop_failure)
                        sending.assert_not_called()
                    else:
                        _serve_session(Mock(), Mock())
                        sending.assert_called_once()
                text = "\n".join(captured.output)
                self.assertIn('"relay"', text)
                self.assertIn("ValueError", text)
                if stage == "delivery":
                    self.assertIn('"delivery"', text)
                    self.assertIn(f'"errno": {errno.EPIPE}', text)
                self.assertNotIn("private-", text)
                parent.close.assert_called_once()

    def test_failed_cancel_send_is_retained_only_when_exit_cannot_be_confirmed(self):
        for acknowledged in (False, True):
            with self.subTest(acknowledged=acknowledged):
                transport = Mock()
                with patch("secs_inference.provider.worker.socket.socket", return_value=transport), \
                     patch.object(WorkerClient, "_receive_next", return_value={"outcome": "ready"}):
                    worker = WorkerClient(Path("/worker.sock"), startup_deadline=monotonic() + 5)
                receive_failure = EOFError("private-receive")
                receive = {"return_value": {"outcome": "stopped"}} if acknowledged else {"side_effect": receive_failure}
                with patch("secs_inference.provider.worker._send", side_effect=OSError(errno.EPIPE, "private-send")), \
                     patch.object(worker, "_receive_next", **receive), \
                     self.assertNoLogs("secs_inference.provider.worker", level="WARNING"):
                    if acknowledged:
                        worker.stop()
                    else:
                        with self.assertRaises(WorkerStopUnconfirmed) as caught:
                            worker.stop()
                        self.assertIs(caught.exception.__context__, receive_failure)
                        self.assertEqual(caught.exception.diagnostic["cancel_request"]["errno"], errno.EPIPE)
                        self.assertNotIn("private-", json.dumps(caught.exception.diagnostic))
                self.assertEqual(worker.stopped, acknowledged)

    def test_socket_cleanup_cannot_change_supervisor_exit_confirmation(self):
        for acknowledged in (False, True):
            with self.subTest(acknowledged=acknowledged):
                transport = Mock()
                with patch("secs_inference.provider.worker.socket.socket", return_value=transport), \
                     patch.object(WorkerClient, "_receive_next", return_value={"outcome": "ready"}):
                    worker = WorkerClient(Path("/worker.sock"), startup_deadline=monotonic() + 5)
                transport.close.side_effect = OSError(errno.EIO, "private-close")
                receive_error = EOFError("private-receive")
                receive = {"return_value": {"outcome": "stopped"}} if acknowledged else {"side_effect": receive_error}
                with patch.object(worker, "_receive_next", **receive), \
                     self.assertLogs("secs_inference.provider.worker", level="ERROR") as captured:
                    if acknowledged:
                        worker.stop()
                    else:
                        with self.assertRaises(WorkerStopUnconfirmed) as caught:
                            worker.stop()
                        self.assertIs(caught.exception.__context__, receive_error)
                self.assertEqual(worker.stopped, acknowledged)
                self.assertIn(f'"errno": {errno.EIO}', "\n".join(captured.output))
                self.assertNotIn("private-", "\n".join(captured.output))

    def test_deadline_acknowledgement_survives_socket_close_failure(self):
        transport = Mock()
        with patch("secs_inference.provider.worker.socket.socket", return_value=transport), \
             patch.object(WorkerClient, "_receive_next", return_value={"outcome": "ready"}):
            worker = WorkerClient(Path("/worker.sock"), startup_deadline=monotonic() + 5)
        transport.close.side_effect = OSError(errno.EIO, "private-close")
        with patch.object(worker, "_receive_next", return_value={"outcome": "stopped", "reason": "deadline"}), \
             self.assertLogs("secs_inference.provider.worker", level="ERROR"):
            with self.assertRaisesRegex(TimeoutError, "child was stopped"):
                worker.request({"operation": "analyse"}, deadline=monotonic() + 5)
        self.assertTrue(worker.stopped)
        transport.close.assert_called_once()

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
            self.assertEqual(failure.exception.diagnostic["relay"]["exception_type"], "WorkerError")
            self.assertTrue(failure.exception.diagnostic["relay"]["frames"])
            self.assertTrue(worker.stopped)
            with self.assertRaises(ProcessLookupError):
                os.kill(first["pid"], 0)

    def test_explicit_stop_reports_relay_evidence_without_claiming_unconfirmed_exit(self):
        transport = Mock()
        with patch("secs_inference.provider.worker.socket.socket", return_value=transport), \
             patch.object(WorkerClient, "_receive_next", return_value={"outcome": "ready"}):
            worker = WorkerClient(Path("/worker.sock"), startup_deadline=monotonic() + 5)
        diagnostic = {"relay": {"exception_type": "OSError", "errno": errno.EIO, "frames": []}}
        with patch.object(worker, "_receive_next", return_value={"outcome": "stopped", "reason": "relay_failure", "diagnostic": diagnostic}), \
             self.assertLogs("secs_inference.provider.worker", level="ERROR") as captured:
            worker.stop()
        self.assertTrue(worker.stopped)
        self.assertIn("exit was confirmed", captured.output[0])
        self.assertIn(f'"errno": {errno.EIO}', captured.output[0])

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
