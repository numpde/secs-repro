"""Serve the resident scientific handler over bounded framed JSON messages.

This child-side protocol has no dependency on the process supervisor or SDK.
Only the process owner may attest that scientific execution has stopped.
"""

import json
import socket
import struct

from secs_inference.provider.diagnostics import exception_evidence
from secs_inference.model_failure import ModelExecutionFailed


_MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class WorkerError(RuntimeError):
    """The scientific worker did not complete its requested operation."""


class ScientificWorkerFailed(WorkerError):
    """Retain the child's bounded structural failure frame for protected evidence."""

    def __init__(self, diagnostic):
        super().__init__("Cannot finish SECS scientific work: the worker encountered an internal error; inspect protected diagnostics")
        self.diagnostic = diagnostic


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
                if type(error) is ModelExecutionFailed and command.get("operation") == "execute":
                    response["model_failure"] = {"modality": error.modality, "stage": error.stage}
                send(connection, response)
                continue
            send(connection, response)


class FrameBuffer:
    """Retain partial scientific messages until a complete bounded frame arrives."""

    def __init__(self):
        self.bytes = bytearray()

    def feed(self, chunk: bytes) -> list[dict]:
        self.bytes.extend(chunk)
        messages = []
        while len(self.bytes) >= 4:
            length = _length(self.bytes[:4])
            if len(self.bytes) < length + 4:
                break
            document = _document(self.bytes[4:length + 4])
            del self.bytes[:length + 4]
            messages.append(document)
        return messages


def encode(document: dict) -> bytes:
    raw = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > _MAX_MESSAGE_BYTES:
        raise WorkerError("Scientific worker message exceeds its framing limit")
    return struct.pack("!I", len(raw)) + raw


def send(connection: socket.socket, document: dict) -> None:
    connection.sendall(encode(document))


def _length(header):
    length = struct.unpack("!I", header)[0]
    if not 1 <= length <= _MAX_MESSAGE_BYTES:
        raise WorkerError("Scientific worker message exceeds its framing limit")
    return length


def _document(raw):
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as cause:
        raise WorkerError("Cannot decode the scientific worker message as JSON") from cause
    if type(document) is not dict:
        raise WorkerError("Scientific worker message is not an object")
    return document


def receive(connection: socket.socket) -> dict:
    return _document(_exact(connection, _length(_exact(connection, 4))))


async def receive_async(reader) -> dict:
    return _document(await reader.readexactly(_length(await reader.readexactly(4))))


def _exact(connection: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = connection.recv(min(length - len(result), 64 * 1024))
        if not chunk:
            raise WorkerError("The scientific worker connection ended before its message was complete")
        result.extend(chunk)
    return bytes(result)
