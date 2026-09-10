"""Admit Upload metadata and bind a read grant to the selected Upload."""

from dataclasses import dataclass, field
from datetime import datetime
import re

from secs_inference.provider.response_json import response_object


_UPLOAD_REF = re.compile(r"upload:sha256:[0-9a-f]{64}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


class UploadResponseError(ValueError):
    """The API reply was rejected; the reason is safe for an Attempt failure."""


@dataclass(frozen=True, slots=True)
class JobUpload:
    upload_ref: str
    description: str = field(repr=False)
    byte_length: int
    content_hash: str | None


@dataclass(frozen=True, slots=True)
class UploadReadCapability:
    """Keep the bearer and capability URL private, including its path and query.

    Operator connection diagnostics may identify the store's resolved TCP
    address, but never the URL that grants access to an Upload.
    """

    upload_ref: str
    byte_length: int
    content_hash: str
    expires_at: datetime
    download_url: str = field(repr=False)
    capability: str = field(repr=False)


def parse_job_upload_set_response(
    raw: bytes, *, expected_job_ref: str,
) -> tuple[JobUpload, ...]:
    """Preserve all current descriptions; the interpreter decides relevance."""
    document = _response(raw, "nmr.provider.job_upload_set.read.response.v1", "list the Job's Uploads")
    if document.get("job_ref") != expected_job_ref:
        raise UploadResponseError("Cannot read Uploads: the response names another Job")
    items = document.get("uploads")
    if type(items) is not list or len(items) > 64:
        raise UploadResponseError("Cannot read Uploads: the response has no bounded Upload list")
    uploads = tuple(_upload(item) for item in items)
    if len({upload.upload_ref for upload in uploads}) != len(uploads):
        raise UploadResponseError("Cannot read Uploads: the response repeats an identity")
    return uploads


def _upload(item: object) -> JobUpload:
    if type(item) is not dict:
        raise UploadResponseError("Cannot read Uploads: an entry is not an object")
    ref = item.get("upload_ref")
    description = item.get("description")
    length = item.get("byte_length")
    content_hash = item.get("content_hash")
    if type(ref) is not str or _UPLOAD_REF.fullmatch(ref) is None:
        raise UploadResponseError("Cannot read Uploads: an entry has an invalid identity")
    if (
        type(description) is not str or len(description) > 4096
        or re.search(r"[\x00\ud800-\udfff]", description)
    ):
        raise UploadResponseError("Cannot read Uploads: an entry has an invalid description")
    if type(length) is not int or not 1 <= length <= 2**63 - 1:
        raise UploadResponseError("Cannot read Uploads: an entry has an invalid byte length")
    if "content_hash" not in item or (
        content_hash is not None and not _is_hash(content_hash)
    ):
        raise UploadResponseError("Cannot read Uploads: an entry has an invalid content hash")
    return JobUpload(ref, description, length, content_hash)


def parse_upload_read_capability_response(
    raw: bytes, *, selected: JobUpload,
) -> UploadReadCapability:
    """Bind a grant once; a pending Upload may now have its finalized hash."""
    document = _response(raw, "nmr.upload.read_capability.response.v1", "obtain permission to download the selected Upload")
    if (
        document.get("method") != "GET"
        or document.get("upload_ref") != selected.upload_ref
        or type(document.get("byte_length")) is not int
        or document["byte_length"] != selected.byte_length
    ):
        raise UploadResponseError("Cannot obtain Upload bytes: the grant does not match the selection")
    content_hash = document.get("content_hash")
    if not _is_hash(content_hash) or (
        selected.content_hash is not None and content_hash != selected.content_hash
    ):
        raise UploadResponseError("Cannot obtain Upload bytes: the grant has a different content hash")
    bearer = document.get("capability")
    url = document.get("download_url")
    if type(bearer) is not str or not 1 <= len(bearer) <= 2048:
        raise UploadResponseError("Cannot obtain Upload bytes: the grant has no usable bearer")
    if type(url) is not str or not url.startswith("https://") or len(url) > 359:
        raise UploadResponseError("Cannot obtain Upload bytes: the grant has no HTTPS destination")
    expiry = _expiry(document.get("expires_at"))
    return UploadReadCapability(
        selected.upload_ref, selected.byte_length, content_hash, expiry, url, bearer,
    )


def _response(raw: bytes, schema: str, operation: str) -> dict:
    try:
        document = response_object(raw)
    except (UnicodeError, ValueError, RecursionError):
        raise UploadResponseError(f"Cannot {operation}: the API returned ambiguous or unreadable JSON") from None
    if document.get("schema_id") != schema:
        raise UploadResponseError(f"Cannot {operation}: the API response does not declare the required {schema!r} schema")
    return document


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH.fullmatch(value) is not None


def _expiry(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        precision = "microseconds" if parsed.microsecond else "seconds"
        canonical = parsed.isoformat(timespec=precision).replace("+00:00", "Z")
        if canonical == value and value.endswith("Z"):
            return parsed
    except (TypeError, ValueError):
        pass
    raise UploadResponseError("Cannot obtain Upload bytes: the grant expiry is not canonical UTC text")
