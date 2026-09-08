"""One-send HTTP envelopes for the provider operations used by this runner.

Adapted from NMRPeak's closed operation profiles. The frozen API release in
contracts/upstream is the review and conformance authority; it is not a runtime
schema loader. Request construction and retries belong to the API and lifecycle.
"""

from enum import Enum
import re


_COMMON_STATUSES = frozenset({200, 400, 401, 403, 408, 414, 431, 500, 503})


class Operation(Enum):
    """Exact route, body budget and response budget for one API operation."""

    HELLO = ("publish hello", "POST", "/provider/v1/hello", 524_288, 65_536, False, {404, 413})
    JOBS = ("list available Jobs", "GET", "/provider/v1/jobs", 0, 65_536, True, set())
    INPUT = ("read the Job specification", "GET", "/provider/v1/jobs/{job_ref}/input", 0, 131_072, True, {404})
    UPLOADS = ("list the Job's Uploads", "GET", "/provider/v1/jobs/{job_ref}/uploads", 0, 2_097_152, False, {404})
    CAPABILITY = (
        "obtain an Upload read grant", "POST", "/provider/v1/jobs/{job_ref}/uploads/{upload_ref}/read-capability",
        0, 65_536, False, {404, 409},
    )
    START = (
        "start an Attempt", "POST", "/provider/v1/execution-attempts/start",
        4096, 65_536, False, {404, 409, 413},
    )
    ATTEMPT = (
        "read the Attempt's state", "GET", "/provider/v1/execution-attempts/{execution_attempt_ref}",
        0, 65_536, False, {404},
    )
    COMPLETE = (
        "publish an analysis result", "POST", "/provider/v1/execution-attempts/complete",
        2_097_152, 65_536, False, {404, 409, 413},
    )
    FAIL = (
        "publish an Attempt failure", "POST", "/provider/v1/execution-attempts/fail",
        16_384, 65_536, False, {404, 409, 413},
    )

    def __init__(
        self, action: str, method: str, path: str, request_limit: int,
        response_limit: int, allows_query: bool, statuses: set[int],
    ) -> None:
        self.action = action
        self.method = method
        self.path = path
        self.request_limit = request_limit
        self.response_limit = response_limit
        self.allows_query = allows_query
        self.statuses = _COMMON_STATUSES | statuses
        self.path_pattern = re.compile(re.sub(r"\{[^}]+\}", "[^/]+", path))
