"""Serve the resident scientific handler over bounded framed JSON messages.

This child-side protocol has no dependency on the process supervisor or SDK.
Only the process owner may attest that scientific execution has stopped.
"""

import json
import socket
import struct

from secs_inference.provider.diagnostics import exception_evidence


_MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class WorkerError(RuntimeError):
    """The scientific worker did not complete its requested operation."""


def serve_scientific_session(connection: socket.socket, load_handler) -> None:
    """Load scientific state once, then execute requests without network inputs."""
    with connection:
        try:
            handler = load_handler()
        except Exception as error:
            diagnostic = {"outcome": "failed", **exception_evidence(error)}
            print(json.dumps(diagnostic), flush=True)
            send(connection, diagnostic)
            return
        send(connection, {"outcome": "ready"})
        while True:
            command = receive(connection)
            # Answer in the loaded child, not the supervisor: a listening
            # socket alone cannot establish that scientific state is usable.
            if command.get("operation") == "ready":
                send(connection, {"outcome": "ready"})
                continue
            try:
                response = handler(command)
            except Exception as error:
                # The process boundary owns unexpected-failure serialization,
                # including imports or setup outside the scientific reader's catches.
                response = {"outcome": "failed", **exception_evidence(error)}
            send(connection, response)


class FrameBuffer:
    """Retain partial scientific messages until a complete bounded frame arrives."""

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


def send(connection: socket.socket, document: dict) -> None:
    raw = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > _MAX_MESSAGE_BYTES:
        raise WorkerError("Scientific worker message exceeds its framing limit")
    connection.sendall(struct.pack("!I", len(raw)) + raw)


def receive(connection: socket.socket) -> dict:
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
