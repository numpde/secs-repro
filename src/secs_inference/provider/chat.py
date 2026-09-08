"""Call one configured Chat Completions endpoint without ambient HTTP authority.

The request and tool-message shape follow magnet-deploy's interpreter adapter.
The synchronous provider uses its existing stdlib TLS/deadline machinery;
endpoint discovery, fallback routing and an additional HTTP library are absent.
"""

from dataclasses import dataclass, field
import http.client
import json
import os
from pathlib import Path
import re
import ssl
from time import monotonic
from urllib.parse import urlsplit

from secs_inference.provider.socket_deadline import socket_deadline


_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024


class InterpreterError(RuntimeError):
    """Interpretation did not produce a usable decision; this is not bad input."""


@dataclass(frozen=True, slots=True)
class ChatEndpoint:
    url: str
    model: str
    api_key: str = field(repr=False)
    ca_file: Path | None = None
    tls_context: ssl.SSLContext = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.query or parsed.port == 0
                or re.search(r"[\x00-\x20\x7f]", self.url)):
            raise ValueError("Interpreter endpoint must be a credential-free HTTPS URL without query or fragment")
        if not self.model or re.fullmatch(r"[\x21-\x7e]+", self.api_key) is None:
            raise ValueError("Interpreter model and a header-safe API key are required")
        try:
            context = ssl.create_default_context(cafile=self.ca_file)
        except OSError as error:
            reason = type(error).__name__ if isinstance(error, ssl.SSLError) or error.errno is None else os.strerror(error.errno)
            raise ValueError(f"Cannot load interpreter TLS trust from {self.ca_file or 'the system CA store'}: {reason}") from error
        object.__setattr__(self, "tls_context", context)

    def complete(self, messages: list[dict], tools: list[dict], *, deadline: float) -> dict:
        """Return one assistant message; never follow redirects or log response text."""
        body = json.dumps({
            "model": self.model, "messages": messages, "tools": tools,
            "tool_choice": "required", "parallel_tool_calls": False, "stream": False,
        }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > _MAX_REQUEST_BYTES:
            raise InterpreterError(f"Cannot interpret this Job: its {len(body)}-byte input and inspection context exceeds this provider's {_MAX_REQUEST_BYTES}-byte model-request limit")
        raw = self._post(body, deadline)
        try:
            document = json.loads(raw)
            choices = document["choices"]
            if len(choices) != 1:
                raise ValueError("No single completion")
            message = choices[0]["message"]
            if type(message) is not dict or message.get("role") != "assistant":
                raise ValueError("No assistant message")
            return message
        except (UnicodeError, ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise InterpreterError("Cannot interpret this Job: the model endpoint returned an unreadable completion") from None

    def _post(self, body: bytes, deadline: float) -> bytes:
        parsed = urlsplit(self.url)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise InterpreterError("Cannot interpret this Job: the interpretation deadline has elapsed")
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=min(10, remaining),
            context=self.tls_context,
        )
        try:
            connection.connect()
            with socket_deadline(connection.sock, deadline):
                connection.request("POST", parsed.path or "/", body, {
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                })
                response = connection.getresponse()
                try:
                    if response.status != 200:
                        raise InterpreterError(f"Cannot interpret this Job: the model endpoint returned HTTP {response.status}")
                    if response.headers.get("Content-Encoding"):
                        raise InterpreterError("Cannot interpret this Job: the model endpoint returned encoded content")
                    result = bytearray()
                    while chunk := response.read1(min(64 * 1024, _MAX_RESPONSE_BYTES + 1 - len(result))):
                        result.extend(chunk)
                        if len(result) > _MAX_RESPONSE_BYTES:
                            raise InterpreterError(f"Cannot interpret this Job: the completion exceeds this provider's {_MAX_RESPONSE_BYTES}-byte model-response limit")
                    if response.length not in {None, 0}:
                        raise InterpreterError("Cannot interpret this Job: the model response ended before its declared bytes arrived")
                    return bytes(result)
                finally:
                    response.close()
        except (OSError, http.client.HTTPException) as error:
            raise InterpreterError(f"Cannot interpret this Job: model response delivery failed ({type(error).__name__})") from None
        finally:
            connection.close()
