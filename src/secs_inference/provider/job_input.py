"""Bind one Job-input response to the identity previously advertised by the API."""

from __future__ import annotations

from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from dataclasses import dataclass, field
from hashlib import sha256
import re

from secs_inference.provider.canonical_json import (
    CanonicalJsonError,
    parse_canonical_json_bytes,
)


JOB_INPUT_SCHEMA_ID = "nmr.job.specification.text.v1"
JOB_INPUT_READ_RESPONSE_SCHEMA_ID = "nmr.provider.job_input.read.response.v1"
MAX_JOB_SPECIFICATION_BYTES = 65_536
MAX_JOB_INPUT_READ_RESPONSE_BYTES = 131_072

_BASE64 = re.compile(
    r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?",
    re.ASCII,
)
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_JOB_REF = re.compile(r"job:[A-Za-z0-9_.-]{1,124}", re.ASCII)
_RESPONSE_FIELDS = {
    "canonical_input_base64",
    "input_byte_length",
    "input_fingerprint",
    "input_schema_id",
    "job_ref",
    "schema_id",
}


class JobInputError(ValueError):
    """Untrusted API facts violate the released Job-input contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Cannot admit provider Job input: {reason}.")


@dataclass(frozen=True, slots=True)
class AdvertisedJobInput:
    """Input identity advertised by the feed but not yet bound to response bytes."""

    job_ref: str
    input_schema_id: str
    input_fingerprint: str
    input_byte_length: int

    def __post_init__(self) -> None:
        _require_job_ref(self.job_ref)
        _require_input_schema_id(self.input_schema_id)
        _require_fingerprint(self.input_fingerprint)
        _require_input_byte_length(self.input_byte_length)


@dataclass(frozen=True, slots=True)
class VerifiedJobInput:
    """Exact UTF-8 specification text bound to its advertised Job identity."""

    job_ref: str
    input_fingerprint: str
    input_byte_length: int
    specification_text: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_job_ref(self.job_ref)
        _require_fingerprint(self.input_fingerprint)
        _require_input_byte_length(self.input_byte_length)
        if type(self.specification_text) is not str:
            raise JobInputError("specification_text_type")
        try:
            exact_bytes = self.specification_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise JobInputError("specification_text_not_utf8") from None
        if len(exact_bytes) != self.input_byte_length:
            raise JobInputError("specification_text_byte_length_mismatch")
        if _fingerprint(exact_bytes) != self.input_fingerprint:
            raise JobInputError("specification_text_fingerprint_mismatch")


def parse_job_input_read_response(
    response_bytes: bytes,
    *,
    advertised: AdvertisedJobInput,
) -> VerifiedJobInput:
    """Verify response metadata and bytes before interpreting their meaning.

    The API feed and input-read response carry the same identity facts. This
    boundary requires both copies and the measured bytes to agree so a later
    interpreter cannot consume text belonging to another Job or revision.
    """

    if type(advertised) is not AdvertisedJobInput:
        raise JobInputError("advertised_input_type")
    if type(response_bytes) is not bytes:
        raise JobInputError("response_type")
    if len(response_bytes) > MAX_JOB_INPUT_READ_RESPONSE_BYTES:
        raise JobInputError("response_too_large")
    try:
        document = parse_canonical_json_bytes(response_bytes)
    except CanonicalJsonError:
        raise JobInputError("invalid_response_json") from None
    if type(document) is not dict or set(document) != _RESPONSE_FIELDS:
        raise JobInputError("invalid_response_shape")
    if document["schema_id"] != JOB_INPUT_READ_RESPONSE_SCHEMA_ID:
        raise JobInputError("wrong_response_schema_id")

    job_ref = document["job_ref"]
    input_schema_id = document["input_schema_id"]
    input_fingerprint = document["input_fingerprint"]
    input_byte_length = document["input_byte_length"]
    _require_job_ref(job_ref)
    _require_input_schema_id(input_schema_id)
    _require_fingerprint(input_fingerprint)
    _require_input_byte_length(input_byte_length)
    if job_ref != advertised.job_ref:
        raise JobInputError("job_ref_mismatch")
    if input_fingerprint != advertised.input_fingerprint:
        raise JobInputError("input_fingerprint_mismatch")
    if input_byte_length != advertised.input_byte_length:
        raise JobInputError("input_byte_length_mismatch")

    encoded = document["canonical_input_base64"]
    if type(encoded) is not str or not encoded or _BASE64.fullmatch(encoded) is None:
        raise JobInputError("invalid_canonical_input_base64")
    try:
        exact_input = b64decode(encoded, validate=True)
    except (Base64Error, ValueError):
        raise JobInputError("invalid_canonical_input_base64") from None
    if b64encode(exact_input).decode("ascii") != encoded:
        raise JobInputError("noncanonical_input_base64")
    if len(exact_input) != input_byte_length:
        raise JobInputError("decoded_input_byte_length_mismatch")
    if _fingerprint(exact_input) != input_fingerprint:
        raise JobInputError("decoded_input_fingerprint_mismatch")

    try:
        specification_text = exact_input.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise JobInputError("decoded_input_not_utf8") from None
    return VerifiedJobInput(
        job_ref=job_ref,
        input_fingerprint=input_fingerprint,
        input_byte_length=input_byte_length,
        specification_text=specification_text,
    )


def _fingerprint(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _require_job_ref(value: object) -> None:
    if type(value) is not str or _JOB_REF.fullmatch(value) is None:
        raise JobInputError("invalid_job_ref")


def _require_input_schema_id(value: object) -> None:
    if type(value) is not str or value != JOB_INPUT_SCHEMA_ID:
        raise JobInputError("invalid_input_schema_id")


def _require_fingerprint(value: object) -> None:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise JobInputError("invalid_input_fingerprint")


def _require_input_byte_length(value: object) -> None:
    if (
        type(value) is not int
        or value < 1
        or value > MAX_JOB_SPECIFICATION_BYTES
    ):
        raise JobInputError("invalid_input_byte_length")
