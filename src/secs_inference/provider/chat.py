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
_MAX_ERROR_BYTES = 16 * 1024


class InterpreterError(RuntimeError):
    """Interpretation did not produce a usable decision; this is not bad input."""

    def __init__(self, message: str, *, diagnostic: dict | None = None):
        """Carry endpoint-owned, redacted evidence separately from the public reason."""
        super().__init__(message)
        self.diagnostic = diagnostic


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
        """Return one assistant message; retain only selected rejection details on failure."""
        body = json.dumps({
            "model": self.model, "messages": messages, "tools": tools,
            "tool_choice": "required", "parallel_tool_calls": False, "stream": False,
        }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > _MAX_REQUEST_BYTES:
            raise InterpreterError(f"Cannot interpret this Job: its {len(body)}-byte input and inspection context exceeds this provider's {_MAX_REQUEST_BYTES}-byte model-request limit")
        prompt_text = tuple(m["content"] for m in messages if isinstance(m.get("content"), str) and m["content"])
        raw = self._post(body, deadline, prompt_text=prompt_text)
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

    def _post(self, body: bytes, deadline: float, *, prompt_text: tuple[str, ...]) -> bytes:
        """Send once under the turn deadline, preserving rejection evidence without replay."""
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
                        raise self._rejection(response, prompt_text)
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

    def _rejection(self, response, prompt_text: tuple[str, ...]) -> InterpreterError:
        """Preserve an observed HTTP rejection even when its optional detail cannot be read.

        The endpoint may echo request text. Keep only its error fields, redact
        known credentials and complete message echoes, and never store the raw
        request or response. This is not a detector for paraphrased input.
        """
        secrets = (self.api_key, *prompt_text)
        parsed = urlsplit(self.url)
        diagnostic = {
            "endpoint": _diagnostic_text(f"{parsed.scheme}://{parsed.netloc}", secrets, 256),
            "model": _diagnostic_text(self.model, secrets, 256),
            "status": response.status,
        }
        request_id = response.headers.get("x-request-id")
        if request_id:
            diagnostic["request_id"] = _diagnostic_text(request_id, secrets, 128)
        try:
            fields = _rejection_fields(response)
        except InterpreterError as error:
            diagnostic["detail_unavailable"] = str(error)
            fields = {}
        except (OSError, http.client.HTTPException, ValueError, RecursionError) as error:
            # Body delivery/parsing is secondary: the received status remains
            # the reason this turn failed, including when its deadline expires.
            diagnostic["detail_unavailable"] = f"Could not read the error response ({type(error).__name__})."
            fields = {}
        for name in ("message", "type", "code", "param"):
            if isinstance(fields.get(name), str):
                diagnostic[name] = _diagnostic_text(fields[name], secrets, 2048 if name == "message" else 256)
        message = f"Cannot interpret this Job: the model endpoint returned HTTP {response.status}."
        if diagnostic.get("message"):
            message += " Endpoint reason: " + diagnostic["message"][:700]
        else:
            diagnostic.setdefault("detail_unavailable", "The response did not contain a readable error.message.")
            message += " No readable error message was available; the operator can inspect this Attempt's diagnostics."
        return InterpreterError(message, diagnostic=diagnostic)


def _rejection_fields(response) -> dict:
    """Read one bounded JSON error object; never publish HTML or partial JSON."""
    if response.headers.get("Content-Encoding"):
        raise InterpreterError("The error response was encoded; its content was not decoded.")
    body = bytearray()
    while chunk := response.read1(min(4096, _MAX_ERROR_BYTES + 1 - len(body))):
        body.extend(chunk)
        if len(body) > _MAX_ERROR_BYTES:
            raise InterpreterError(f"The error response exceeded the {_MAX_ERROR_BYTES}-byte diagnostic limit.")
    if response.length not in {None, 0}:
        raise InterpreterError("The error response ended before its declared bytes arrived.")
    document = json.loads(body)
    return document["error"] if isinstance(document, dict) and isinstance(document.get("error"), dict) else {}


def _diagnostic_text(value: str, secrets: tuple[str, ...], limit: int) -> str:
    """Redact before truncation so a bound cannot expose a credential prefix."""
    # Match the original text: replacing a key first could otherwise prevent
    # redaction of a complete prompt echo containing that same key.
    spans = []
    for secret in secrets:
        start = value.find(secret)
        while start >= 0:
            spans.append((start, start + len(secret)))
            start = value.find(secret, start + 1)
    parts, end = [], 0
    for start, stop in sorted(spans):
        if start >= end:
            parts.extend((value[end:start], "[redacted]"))
        end = max(end, stop)
    parts.append(value[end:])
    value = "".join(parts)
    value = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+|\bsk-[A-Za-z0-9_*.-]+", "[redacted]", value)
    value = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    return value if len(value) <= limit else value[:limit - 1] + "…"
