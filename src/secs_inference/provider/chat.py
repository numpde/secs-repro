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
from secs_inference.provider.network_errors import network_failure_reason, network_failure_evidence
from secs_inference.provider.connection import https_connection


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
    reasoning_effort: str | None = None
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
            reason = ("TLS could not load the CA certificates" if isinstance(error, ssl.SSLError) else
                      os.strerror(error.errno) if error.errno is not None else "an operating-system error occurred without a recorded reason")
            raise ValueError(f"Cannot load interpreter TLS trust from {self.ca_file or 'the system CA store'}: {reason}") from error
        object.__setattr__(self, "tls_context", context)

    def complete(self, messages: list[dict], tools: list[dict], *, deadline: float) -> dict:
        """Return one assistant message; retain only selected rejection details on failure."""
        request = {
            "model": self.model, "messages": messages, "tools": tools,
            "tool_choice": "required", "parallel_tool_calls": False, "stream": False,
        }
        # Ported from the sibling Chat adapters: omission preserves the
        # endpoint default; a configured effort is sent without model guessing.
        if self.reasoning_effort is not None:
            request["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > _MAX_REQUEST_BYTES:
            raise self.failure("preparing the interpretation request", f"its {len(body)}-byte input and inspection context exceeds this provider's {_MAX_REQUEST_BYTES}-byte model-request limit")
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
            raise self.failure("checking the model's reply", "the response does not contain one readable assistant message", prompt_text=prompt_text) from None

    def failure(self, phase: str, reason: str, *, prompt_text: tuple[str, ...] = (), **evidence) -> InterpreterError:
        """Name the selected model and operation without retaining request text.

        Callers supply owned reasons and scalar evidence, never arbitrary
        exception text. Model and endpoint configuration still need redaction.
        """
        secrets = (self.api_key, *prompt_text)
        parsed = urlsplit(self.url)
        model = _diagnostic_text(self.model, secrets, 128)
        diagnostic = {
            "endpoint": _diagnostic_text(f"{parsed.scheme}://{parsed.netloc}", secrets, 256),
            "model": model, "phase": phase, **evidence,
        }
        return InterpreterError(
            f"Job interpretation with model {model!r} failed while {phase}: {reason}.",
            diagnostic=diagnostic,
        )

    def _post(self, body: bytes, deadline: float, *, prompt_text: tuple[str, ...]) -> bytes:
        """Send once under the turn deadline, preserving rejection evidence without replay."""
        parsed = urlsplit(self.url)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise self.failure("preparing the interpretation request", "the interpretation deadline has elapsed; this request was not sent", prompt_text=prompt_text)
        connection = https_connection(
            parsed.hostname, parsed.port or 443, timeout=min(10, remaining),
            context=self.tls_context,
        )
        phase = "connecting to the model service"
        try:
            connection.connect()
            with socket_deadline(connection.sock, deadline):
                # The connect timeout must not cap model generation. The timer
                # now owns the remaining budget across sending and all reads.
                connection.sock.settimeout(None)
                phase = "sending the interpretation request"
                connection.request("POST", parsed.path or "/", body, {
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                })
                phase = "waiting for the model service to reply"
                response = connection.getresponse()
                phase = "reading the model's reply"
                try:
                    if response.status != 200:
                        raise self._rejection(response, prompt_text)
                    if response.headers.get("Content-Encoding"):
                        raise self.failure(phase, "the service returned a Content-Encoding that this provider cannot decode", prompt_text=prompt_text)
                    result = bytearray()
                    while chunk := response.read1(min(64 * 1024, _MAX_RESPONSE_BYTES + 1 - len(result))):
                        result.extend(chunk)
                        if len(result) > _MAX_RESPONSE_BYTES:
                            raise self.failure(phase, f"the completion exceeds this provider's {_MAX_RESPONSE_BYTES}-byte model-response limit", prompt_text=prompt_text)
                    if monotonic() >= deadline:
                        raise self.failure(phase, "the interpretation deadline elapsed before the reply could be accepted", prompt_text=prompt_text)
                    if response.length not in {None, 0}:
                        raise self.failure(phase, "the reply ended before all its declared bytes arrived", prompt_text=prompt_text)
                    return bytes(result)
                finally:
                    response.close()
        except (OSError, http.client.HTTPException) as error:
            if monotonic() >= deadline:
                reason = "the interpretation deadline elapsed; " + network_failure_reason(error)
            else:
                reason = network_failure_reason(error)
            if phase == "connecting to the model service":
                reason += "; this request was not sent"
            else:
                reason += "; whether the service finished processing this request is unknown"
            raise self.failure(phase, reason, prompt_text=prompt_text,
                               **network_failure_evidence(error)) from None
        finally:
            connection.close()

    def _rejection(self, response, prompt_text: tuple[str, ...]) -> InterpreterError:
        """Preserve an observed HTTP rejection even when its optional detail cannot be read.

        The endpoint may echo request text. Keep only its error fields, redact
        known credentials and complete message echoes, and never store the raw
        request or response. This is not a detector for paraphrased input.
        """
        secrets = (self.api_key, *prompt_text)
        rejection = self.failure("requesting an interpretation", f"the model endpoint returned HTTP {response.status}",
                             prompt_text=prompt_text, status=response.status)
        diagnostic = rejection.diagnostic
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
        message = str(rejection)
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
