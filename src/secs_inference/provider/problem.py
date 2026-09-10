"""Describe API problems without treating diagnostics as operation receipts."""

import re

from secs_inference.provider.http import HttpResponse
from secs_inference.provider.response_json import response_object


_PROBLEMS = {
    400: ("bad-request", "Bad request"),
    401: ("authentication-failed", "Request authentication failed"),
    403: ("authorization-denied", "Authorization denied"),
    404: ("not-found", "Resource not found"),
    408: ("request-body-timeout", "Request body timeout"),
    409: ("operation-conflict", "Operation conflict"),
    413: ("request-content-too-large", "Request content too large"),
    414: ("uri-too-long", "URI too long"),
    431: ("request-header-fields-too-large", "Request header fields too large"),
    500: ("internal-error", "Internal server error"),
    503: ("service-unavailable", "Service unavailable"),
}
_EDGE_SPACE = re.compile(r"[ \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
_FORBIDDEN_DIAGNOSTIC = re.compile(
    r"[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u200b-\u200f"
    r"\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]"
)


def is_display_diagnostic(value: str) -> bool:
    """Check the API's bounded, single-line public diagnostic text contract."""
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if not value or len(encoded) > 1_024:
        return False
    if _EDGE_SPACE.fullmatch(value[0]) or _EDGE_SPACE.fullmatch(value[-1]):
        return False
    return _FORBIDDEN_DIAGNOSTIC.search(value) is None


def describe_problem(response: HttpResponse) -> tuple[str, dict]:
    """Project public problem fields only; never disclose bodies or extensions.

    The API permits input explanations only on 400/413/414/431. Server and
    authentication failures deliberately require request-ID-based diagnosis.
    This projection grants no retry, reconciliation, or retirement authority.
    """
    facts = {"status": response.status, "request_id": response.request_id}
    request = " without a request ID" if response.request_id is None else f" for request {response.request_id}"
    message = f"HTTP {response.status}{request}"
    try:
        problem = response_object(response.body)
    except (ValueError, UnicodeError, RecursionError):
        return message + "; the API problem explanation is unreadable", facts
    profile = _PROBLEMS.get(response.status)
    if (profile is None or type(problem.get("status")) is not int or problem["status"] != response.status
            or problem.get("type") != "urn:nmr-api:problem:" + profile[0]
            or problem.get("title") != profile[1] or problem.get("request_id") != response.request_id):
        return message + "; the API problem fields do not match this response", facts
    facts.update(problem_type=problem["type"], title=profile[1])
    message += ": " + profile[1]
    if response.status in {400, 413, 414, 431}:
        code, detail = problem.get("code"), problem.get("detail")
        if (type(code) is str and len(code) <= 128 and re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", code)
                and type(detail) is str and is_display_diagnostic(detail)):
            facts.update(code=code, detail=detail)
            return message + f"; {detail} (code: {code})", facts
        return message + "; the API input explanation is missing or invalid", facts
    return message + "; the API supplies no further public explanation; inspect API logs using the request ID", facts
