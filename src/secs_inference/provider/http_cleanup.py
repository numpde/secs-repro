"""HTTP resource cleanup cannot revise an already observed exchange outcome."""

import http.client
import json
import logging

from secs_inference.provider.diagnostics import exception_evidence
from secs_inference.provider.network_errors import network_failure_reason


_LOG = logging.getLogger(__name__)


def close_http_resource(resource, *, operation: str, role: str) -> None:
    """Report close failures without turning a received reply into a failed send.

    Callers supply an owned, credential-free operation label. This policy is
    only for HTTP resources, not durable files or worker-stop confirmation.
    """
    try:
        resource.close()
    except (OSError, http.client.HTTPException) as error:
        _LOG.error("%s: could not close the HTTP %s: %s. The exchange outcome is unchanged. | %s",
                   operation, role, network_failure_reason(error), json.dumps(exception_evidence(error)))
