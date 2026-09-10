"""Keep scientific work in a supervised, warm, offline process.

Only the supervisor acknowledges a stop, after reaping its child. Socket EOF
or a cancelled controller wait is never proof that parsing or GPU work stopped.
Deployment owns the container and its devices; this module cannot launch Docker.
"""

import json
import fcntl
import math
import os
import multiprocessing
from multiprocessing.connection import wait
from pathlib import Path
import socket
import struct
import stat
from time import monotonic

from secs_inference.provider.socket_deadline import socket_deadline


_MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class WorkerError(RuntimeError):
    """The scientific worker did not complete its requested operation."""


class WorkerStopUnconfirmed(WorkerError):
    """Keep source files and stop admission until the supervisor confirms exit."""


class WorkerClient:
    """Wait for a loaded child, then share it across serial healthy Attempts.

    Deadlines are absolute monotonic seconds, shared by the controller and
    supervisor on the same host, not wall-clock timestamps or durations.
    """

    def __init__(self, path: Path, *, startup_deadline: float, check_running=None):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(max(0.01, startup_deadline - monotonic()))
        try:
            self.socket.connect(str(path))
        except BaseException:
            self.socket.close()
            raise
        self.stopped = False
        self._incoming = _FrameBuffer()
        self._messages = []
        try:
            ready = self._receive_next(startup_deadline, check_running)
            self._accept_stop(ready)
            if ready.get("outcome") != "ready":
                error = WorkerError("The SECS scientific worker could not finish loading its configured model and candidate index; inspect worker diagnostics")
                error.diagnostic = ready
                raise error
        except BaseException:
            self.stop()
            raise

    def request(self, command: dict, *, deadline: float, check_active=None) -> dict:
        """On deadline/failure, require stop confirmation before unwinding files."""
        if self.stopped:
            raise WorkerError("The scientific worker has stopped; reconnect before accepting work")
        try:
            self.socket.settimeout(_remaining(deadline))
            _send(self.socket, command | {"deadline": deadline})
            response = self._receive_next(deadline, check_active)
            self._accept_stop(response)
            return response
        except BaseException:
            self.stop()
            raise

    def _accept_stop(self, response):
        if response.get("outcome") != "stopped":
            return
        self.stopped = True
        self.socket.close()
        if response.get("reason") == "deadline":
            raise TimeoutError("The scientific work deadline elapsed and its child was stopped")
        if response.get("reason") == "relay_failure":
            raise WorkerError("Scientific worker communication failed; its analysis process was confirmed stopped")
        raise WorkerError("The scientific analysis process exited before completing its operation; its exit was confirmed")

    def stop(self) -> None:
        """Discard a raced result; only a supervisor acknowledgement permits cleanup."""
        if self.stopped:
            return
        try:
            deadline = monotonic() + 10
            self.socket.settimeout(10)
            try:
                _send(self.socket, {"operation": "cancel"})
            except OSError:
                # The supervisor may already have stopped and closed its write
                # side. Its queued acknowledgement is still the needed proof.
                pass
            while self._receive_next(deadline).get("outcome") != "stopped":
                pass
        except BaseException:
            raise WorkerStopUnconfirmed("Scientific worker exit could not be confirmed; retain its source files and stop accepting Jobs") from None
        finally:
            self.socket.close()
        self.stopped = True

    def _receive_next(self, deadline, check_active=None):
        """Keep partial frames across timeout so a raced result cannot hide stop."""
        next_check = monotonic() + 5
        while True:
            remaining = _remaining(deadline)
            if self._messages:
                return self._messages.pop(0)
            ready = wait([self.socket], timeout=min(5, remaining))
            if ready:
                self.socket.settimeout(_remaining(deadline))
                chunk = self.socket.recv(64 * 1024)
                if not chunk:
                    raise WorkerError("Scientific worker disconnected before completing its operation")
                self._messages.extend(self._incoming.feed(chunk))
            if check_active is not None and monotonic() >= next_check:
                check_active()
                next_check = monotonic() + 5


def serve_worker(path: Path, load_handler) -> None:
    """Serve serial sessions; the container must kill children if its owner dies."""
    owner = os.open(path.parent / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(owner).st_mode):
            raise WorkerError("Cannot start the worker: its ownership lock is not a regular file")
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Only the sole supervisor may remove a socket left by its stopped
        # container. A second supervisor must not displace a live listener.
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise WorkerError("Cannot start the worker: its socket path contains a non-socket file")
            path.unlink()
        _listen(path, load_handler)
    finally:
        os.close(owner)


def _listen(path, load_handler):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)
    try:
        while True:
            client, _ = listener.accept()
            with client:
                _serve_session(client, load_handler)
    finally:
        listener.close()
        path.unlink(missing_ok=True)


def _serve_session(client: socket.socket, load_handler) -> None:
    """Read frames incrementally so a partial child response cannot block cancel."""
    parent, child_socket = socket.socketpair()
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_child_loop, args=(child_socket, load_handler))
    child.start()
    child_socket.close()
    reason = None
    try:
        reason = _relay(client, parent, child)
    except (OSError, ValueError, WorkerError):
        reason = "relay_failure"
    finally:
        try:
            _reap(child)
        finally:
            parent.close()
            if not child.is_alive():
                child.close()
    if reason is not None:
        try:
            with socket_deadline(client, monotonic() + 2):
                _send(client, {"outcome": "stopped", "reason": reason})
        except OSError:
            pass


def _relay(client, parent, child):
    """Return a stop reason; only the enclosing process owner may acknowledge it."""
    buffers = {client: _FrameBuffer(), parent: _FrameBuffer()}
    deadline = None
    while True:
        timeout = None if deadline is None else max(0, deadline - monotonic())
        ready = wait([client, parent, child.sentinel], timeout=timeout)
        if deadline is not None and monotonic() >= deadline:
            return "deadline"
        # Read cancellation before forwarding a simultaneously ready result.
        for incoming in (client, parent):
            if incoming not in ready:
                continue
            chunk = incoming.recv(64 * 1024)
            if not chunk:
                return "child_exit" if incoming is parent else None
            for message in buffers[incoming].feed(chunk):
                if incoming is parent and message.get("outcome") == "stopped":
                    raise WorkerError("Only the supervisor may acknowledge child termination")
                if incoming is client and message.get("operation") == "cancel":
                    return "cancel"
                if incoming is client:
                    deadline = message.get("deadline")
                    if type(deadline) not in {int, float} or not math.isfinite(deadline):
                        raise WorkerError("The scientific request has no finite work deadline")
                else:
                    deadline = None
                outgoing = parent if incoming is client else client
                with socket_deadline(outgoing, monotonic() + 2):
                    _send(outgoing, message)
        if child.sentinel in ready:
            return "child_exit"


def _reap(child) -> None:
    """Use the sibling supervisor's terminate/kill/join ordering."""
    if child.is_alive():
        child.terminate()
        child.join(2)
    if child.is_alive():
        child.kill()
        child.join(2)
    if child.is_alive():
        raise WorkerStopUnconfirmed("The scientific child has not exited after termination; the supervisor cannot accept another session")
    child.join()


def _child_loop(connection: socket.socket, load_handler) -> None:
    """Load scientific state once, then execute requests without network inputs."""
    with connection:
        try:
            handler = load_handler()
        except Exception as error:
            import traceback
            diagnostic = {"outcome": "failed", "exception_type": type(error).__name__,
                          "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                                     for frame in traceback.extract_tb(error.__traceback__)]}
            print(json.dumps(diagnostic), flush=True)
            _send(connection, diagnostic)
            return
        _send(connection, {"outcome": "ready"})
        while True:
            command = _receive(connection)
            _send(connection, handler(command))


class _FrameBuffer:
    """A bounded frame accumulator shared by supervisor and blocking readers."""

    def __init__(self):
        self.bytes = bytearray()

    def feed(self, chunk: bytes) -> list[dict]:
        self.bytes.extend(chunk)
        messages = []
        while len(self.bytes) >= 4:
            length = struct.unpack("!I", self.bytes[:4])[0]
            if not 1 <= length <= _MAX_MESSAGE_BYTES:
                raise WorkerError("Scientific worker message exceeds its framing limit")
            if len(self.bytes) < length + 4:
                break
            document = json.loads(self.bytes[4:length + 4])
            del self.bytes[:length + 4]
            if type(document) is not dict:
                raise WorkerError("Scientific worker message is not an object")
            messages.append(document)
        return messages


def _send(connection: socket.socket, document: dict) -> None:
    raw = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > _MAX_MESSAGE_BYTES:
        raise WorkerError("Scientific worker message exceeds its framing limit")
    connection.sendall(struct.pack("!I", len(raw)) + raw)


def _receive(connection: socket.socket) -> dict:
    header = _exact(connection, 4)
    length = struct.unpack("!I", header)[0]
    if not 1 <= length <= _MAX_MESSAGE_BYTES:
        raise WorkerError("Scientific worker message exceeds its framing limit")
    document = json.loads(_exact(connection, length))
    if type(document) is not dict:
        raise WorkerError("Scientific worker message is not an object")
    return document


def _exact(connection: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = connection.recv(min(length - len(result), 64 * 1024))
        if not chunk:
            raise WorkerError("The scientific worker connection ended before its message was complete")
        result.extend(chunk)
    return bytes(result)


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("The deadline for the scientific worker response has elapsed")
    return remaining
