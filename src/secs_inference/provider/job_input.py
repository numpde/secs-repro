"""Measure one Job-input response against the identity advertised by the feed."""

from __future__ import annotations

from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass, field
from hashlib import sha256
import json


JOB_INPUT_READ_RESPONSE_SCHEMA_ID = "nmr.provider.job_input.read.response.v1"
MAX_JOB_INPUT_READ_RESPONSE_BYTES = 131_072


class JobInputError(ValueError):
    """The API response cannot be identified as the selected Job input."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Cannot read the selected Job input because {reason}.")


@dataclass(frozen=True, slots=True)
class SelectedJobInput:
    """The selected feed item's input identity."""

    job_ref: str
    input_fingerprint: str
    input_byte_length: int


@dataclass(frozen=True, slots=True)
class JobSpecification:
    """The selected Job's exact UTF-8 specification text."""

    job_ref: str
    text: str = field(repr=False)


def parse_job_input_read_response(
    response_bytes: bytes,
    *,
    selected: SelectedJobInput,
) -> JobSpecification:
    """Return text only when the measured bytes match the selected feed item."""

    if len(response_bytes) > MAX_JOB_INPUT_READ_RESPONSE_BYTES:
        raise JobInputError("the API response is too large")
    try:
        document = json.loads(response_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise JobInputError("the API response is not valid UTF-8 JSON") from None
    if type(document) is not dict:
        raise JobInputError("the API response is not a JSON object")
    if document.get("schema_id") != JOB_INPUT_READ_RESPONSE_SCHEMA_ID:
        raise JobInputError("the API response uses a different schema")
    if document.get("job_ref") != selected.job_ref:
        raise JobInputError("the API response names another Job")

    encoded = document.get("canonical_input_base64")
    if type(encoded) is not str:
        raise JobInputError("the API response does not contain Base64 input text")
    try:
        exact_input = b64decode(encoded, validate=True)
    except (Base64Error, ValueError):
        raise JobInputError("its Base64 encoding is invalid") from None
    # The feed owns input identity. Response copies and alternate JSON/Base64
    # spellings cannot change which decoded bytes are admitted here.
    if len(exact_input) != selected.input_byte_length:
        raise JobInputError("its byte length differs from the Job feed")
    measured = "sha256:" + sha256(exact_input).hexdigest()
    if measured != selected.input_fingerprint:
        raise JobInputError("its SHA-256 differs from the Job feed")
    try:
        specification_text = exact_input.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise JobInputError("its bytes are not UTF-8 text") from None
    return JobSpecification(selected.job_ref, specification_text)
