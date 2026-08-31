"""Prepare one provider hello and bind its API acceptance receipt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json
import re

from secs_inference.provider.canonical_json import JsonValue, canonical_json_bytes
from secs_inference.provider.credential import validate_provider_ref


HELLO_PATH = "/provider/v1/hello"
HELLO_REQUEST_SCHEMA_ID = "nmr.provider.hello_request.v1"
HELLO_RESPONSE_SCHEMA_ID = "nmr.provider.hello_response.v1"
_ANALYSIS_KIND = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_TIMESTAMP = re.compile(
    r"(?!0000)[0-9]{4}-(?:0[1-9]|1[0-2])-"
    r"(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):"
    r"[0-5][0-9]:[0-5][0-9](?:\.(?!000000)[0-9]{6})?Z"
)
_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]{1,128}")
_EDGE_SPACE = re.compile(r"[ \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
_FORBIDDEN_DIAGNOSTIC = re.compile(
    r"[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u200b-\u200f"
    r"\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]"
)
_FIXED_PROBLEM_PROFILES = {
    400: (
        "urn:nmr-api:problem:bad-request",
        "Bad request",
        frozenset({"provider_request_invalid", "request_query_not_supported"}),
    ),
    413: (
        "urn:nmr-api:problem:request-content-too-large",
        "Request content too large",
        frozenset({"request_content_too_large"}),
    ),
    414: (
        "urn:nmr-api:problem:uri-too-long",
        "URI too long",
        frozenset({"request_path_too_large", "request_query_too_large"}),
    ),
    431: (
        "urn:nmr-api:problem:request-header-fields-too-large",
        "Request header fields too large",
        frozenset(
            {"request_header_bytes_too_large", "request_header_count_too_large"}
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class AnalysisOffering:
    """One analysis description in the provider's complete hello snapshot."""

    analysis_kind_ref: str
    description: str


@dataclass(frozen=True, slots=True)
class PreparedHello:
    """The exact unsigned hello request target and canonical body."""

    method: str
    path: str
    query: str
    body: bytes


@dataclass(frozen=True, slots=True)
class HelloAccepted:
    """The API accepted the identity-bound hello snapshot."""


class HelloReceiptRejection(Enum):
    INVALID_JSON = "invalid_json"
    INVALID_SHAPE = "invalid_shape"
    INVALID_FIELD = "invalid_field"
    RESPONSE_DRIFT = "response_drift"


@dataclass(frozen=True, slots=True)
class HelloReceiptRejected:
    """A successful HTTP response that cannot prove hello acceptance."""

    reason: HelloReceiptRejection


def prepare_hello(
    *,
    display_name: str,
    description: str,
    analysis_offerings: tuple[AnalysisOffering, ...],
) -> PreparedHello:
    """Prepare one complete replacement snapshot of provider presentation."""

    _require_bounded_text(display_name, "provider display name", 128)
    _require_bounded_text(description, "provider description", 1_024)
    if type(analysis_offerings) is not tuple or len(analysis_offerings) > 64:
        raise ValueError("Provider hello requires a tuple of at most 64 offerings")
    offerings: list[JsonValue] = []
    refs: set[str] = set()
    for offering in analysis_offerings:
        if type(offering) is not AnalysisOffering:
            raise TypeError("Provider hello offerings must be exact offering facts")
        if type(offering.analysis_kind_ref) is not str:
            raise TypeError("analysis kind must be a string")
        if (
            len(offering.analysis_kind_ref) > 128
            or _ANALYSIS_KIND.fullmatch(offering.analysis_kind_ref) is None
        ):
            raise ValueError("analysis kind has an invalid format")
        if offering.analysis_kind_ref in refs:
            raise ValueError("Provider hello analysis kinds must be unique")
        refs.add(offering.analysis_kind_ref)
        _require_bounded_text(
            offering.description,
            "analysis offering description",
            1_024,
        )
        offerings.append(
            {
                "analysis_kind_ref": offering.analysis_kind_ref,
                "description": offering.description,
            }
        )
    return PreparedHello(
        method="POST",
        path=HELLO_PATH,
        query="",
        body=canonical_json_bytes(
            {
                "schema_id": HELLO_REQUEST_SCHEMA_ID,
                "display_name": display_name,
                "description": description,
                "analysis_offerings": offerings,
            }
        ),
    )


def parse_hello_receipt(
    raw: bytes,
    *,
    expected_provider_ref: str,
) -> HelloAccepted | HelloReceiptRejected:
    """Bind one successful hello receipt to the configured provider identity."""

    try:
        document = _decode_response_object(raw)
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        return HelloReceiptRejected(HelloReceiptRejection.INVALID_JSON)
    if set(document) != {"schema_id", "provider_ref", "accepted_at"}:
        return HelloReceiptRejected(HelloReceiptRejection.INVALID_SHAPE)
    if document["schema_id"] != HELLO_RESPONSE_SCHEMA_ID:
        return HelloReceiptRejected(HelloReceiptRejection.INVALID_FIELD)
    provider_ref = document["provider_ref"]
    accepted_at = document["accepted_at"]
    if (
        type(provider_ref) is not str
        or not _is_timestamp(accepted_at)
    ):
        return HelloReceiptRejected(HelloReceiptRejection.INVALID_FIELD)
    try:
        validate_provider_ref(provider_ref)
    except ValueError:
        return HelloReceiptRejected(HelloReceiptRejection.INVALID_FIELD)
    if provider_ref != expected_provider_ref:
        return HelloReceiptRejected(HelloReceiptRejection.RESPONSE_DRIFT)
    return HelloAccepted()


def is_fixed_hello_problem(
    raw: bytes,
    *,
    status: int,
) -> bool:
    """Return whether a problem proves that the fixed hello request must change."""

    profile = _FIXED_PROBLEM_PROFILES.get(status)
    if profile is None:
        return False
    try:
        document = _decode_response_object(raw)
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        return False
    if set(document) != {
        "type",
        "title",
        "status",
        "instance",
        "request_id",
        "code",
        "detail",
    }:
        return False
    problem_type, title, diagnostic_codes = profile
    if (document["type"], document["title"], document["status"]) != (
        problem_type,
        title,
        status,
    ):
        return False
    instance = document["instance"]
    request_id = document["request_id"]
    code = document["code"]
    detail = document["detail"]
    return (
        type(instance) is str
        and 1 <= len(instance) <= 404
        and type(request_id) is str
        and _VISIBLE_ASCII.fullmatch(request_id) is not None
        and type(code) is str
        and code in diagnostic_codes
        and type(detail) is str
        and _is_safe_diagnostic(detail)
    )


def _require_bounded_text(value: object, name: str, maximum_characters: int) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must contain Unicode scalar text") from error
    if not value or len(value) > maximum_characters or "\0" in value:
        raise ValueError(f"{name} must be non-empty bounded text without NUL")


def _is_timestamp(value: object) -> bool:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _is_safe_diagnostic(value: str) -> bool:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if not value or len(value) > 1_024 or len(encoded) > 1_024:
        return False
    if _EDGE_SPACE.fullmatch(value[0]) or _EDGE_SPACE.fullmatch(value[-1]):
        return False
    return _FORBIDDEN_DIAGNOSTIC.search(value) is None


def _decode_response_object(raw: bytes) -> dict[str, object]:
    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=_object_without_duplicates,
        parse_float=_reject_json_number,
        parse_constant=_reject_json_number,
    )
    if type(value) is not dict:
        raise TypeError("Provider response JSON root is not an object")
    return value


class _RejectedJson(ValueError):
    pass


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise _RejectedJson
        value[name] = item
    return value


def _reject_json_number(_value: str) -> None:
    raise _RejectedJson
