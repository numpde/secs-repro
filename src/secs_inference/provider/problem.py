"""Describe API problems without treating diagnostics as operation receipts."""

from secs_inference.provider.http import HttpResponse
from secs_inference.provider.operations import Operation
from secs_inference.provider._nmr_api_failures import interpret_problem


# Local transport operations map explicitly to the published API route identities.
_API_OPERATIONS = {
    Operation.HELLO: "provider_hello",
    Operation.JOBS: "jobs_list",
    Operation.INPUT: "job_input_read",
    Operation.UPLOADS: "job_upload_set_read",
    Operation.CAPABILITY: "job_upload_read_capability",
    Operation.START: "execution_attempt_start",
    Operation.ATTEMPT: "execution_attempt_read",
    Operation.COMPLETE: "execution_attempt_complete",
    Operation.FAIL: "execution_attempt_fail",
    Operation.PROGRESS: "execution_attempt_progress",
}
def describe_problem(response: HttpResponse, *, operation: Operation) -> tuple[str, dict]:
    """Retain shared validated evidence, explicitly marking unverified explanations."""
    # HttpResponse proves that the transport admitted application/problem+json
    # for a non-success status; malformed media uses ResponseRejected instead.
    problem = interpret_problem(operation=_API_OPERATIONS[operation], status=response.status,
                                content_type="application/problem+json",
                                header_request_id=response.request_id, body=response.body)
    facts = {"status": response.status, "request_id": problem.header_request_id,
             "problem_verified": problem.verified, "problem_rejection": problem.rejection,
             "header_request_id": problem.header_request_id, "body_request_id": problem.body_request_id}
    for name in ("problem_type", "title", "code", "detail", "upload_ref"):
        value = getattr(problem, name)
        if value is not None:
            facts[name] = value
    if problem.verified:
        for name in ("recovery_mode", "recovery_description", "current_send_effect", "conflict_action", "conflict_description"):
            facts[name] = getattr(problem, name)
    message = f"HTTP {response.status}; response request ID {problem.header_request_id or 'unavailable'}"
    if not problem.verified:
        message += (f"; unverified API explanation ({problem.rejection}); "
                    f"body request ID {problem.body_request_id or 'unavailable'}")
    if problem.title is not None:
        message += ": " + problem.title
    if problem.detail is not None:
        message += "; " + problem.detail
    if problem.code is not None:
        message += f" (code: {problem.code})"
    return message, facts
