"""Operator retirement of held work after a fresh API-confirmed closure.

Closure is not a receipt for the retained command. The private archive preserves
that distinction and the exact record; this operation never publishes a report.
"""
from base64 import b64encode
from hashlib import sha256
import argparse
import json
from pathlib import Path

from secs_inference.provider.api import ProviderApi
from secs_inference.provider.config import CONFIG_PATH, CREDENTIAL_PATH, decode_provider_config
from secs_inference.provider.credential import PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES, parse_provider_credential
from secs_inference.provider.main import _read_regular_file, _CONFIG_MAX_BYTES
from secs_inference.provider.configuration_error import ConfigurationError
from secs_inference.provider.response_json import response_object
from secs_inference.provider.archive_document import validate_archive_document

from secs_inference.provider._nmr_api_failures import attempt_is_closed
from secs_inference.provider.attempt_state import TerminalPending
from secs_inference.provider.attempt_store import AttemptStore, JournalError
from secs_inference.provider.job_api import ApiError, JobApi, validate_snapshot


def archive_closed(*, journal, api, owner: dict, execution_attempt_ref: str,
                   expected_record_digest: str, reason: str) -> dict:
    journal._require_writable()
    if (type(reason) is not str or not reason.strip() or not reason.isprintable()
            or len(reason.encode("utf-8")) > 2048):
        raise ValueError("Archive reason must be nonempty printable text within 2048 UTF-8 bytes")
    expected_owner = {"origin": api.provider.endpoint.origin, "provider_ref": api.provider_ref}
    if type(owner) is not dict or owner != expected_owner or any(type(value) is not str or not value for value in owner.values()):
        raise JournalError("The authenticated API configuration differs from the journal ownership binding")
    record = journal.load_record()
    if record is None:
        raise JournalError("No retained Attempt exists to archive")
    raw, retained = record
    digest = "sha256:" + sha256(raw).hexdigest()
    if (not isinstance(retained, TerminalPending) or retained.hold is None
            or retained.hold.reconciling or digest != expected_record_digest
            or retained.active.execution_attempt_ref != execution_attempt_ref
            or retained.active.start.provider_ref != api.provider_ref):
        raise JournalError("Archive selection does not match a held Attempt owned by this provider; inspect again")
    snapshot = api.snapshot_document(retained.active)
    if not attempt_is_closed(snapshot["state"]):
        raise JournalError("The API Attempt remains in progress; held work has not been archived")
    document = {"schema_id": "secs.provider.closed_attempt_archive.v1",
                "execution_attempt_ref": execution_attempt_ref,
                "owner": owner, "provider_ref": api.provider_ref, "record_digest": digest,
                "original_record_base64": b64encode(raw).decode("ascii"),
                "reason": reason, "snapshot": snapshot, "delivery": "unconfirmed"}

    def validate_existing(existing):
        if (type(existing) is not dict or set(existing) != set(document)
                or any(existing[key] != value for key, value in document.items() if key not in {"reason", "snapshot"})
                or type(existing["reason"]) is not str or not existing["reason"].strip()
                or not existing["reason"].isprintable() or len(existing["reason"].encode("utf-8")) > 2048):
            raise JournalError("Existing archive differs from the retained Attempt; preserve both and investigate")
        validate_snapshot(existing["snapshot"], retained.active)
        if not attempt_is_closed(existing["snapshot"]["state"]):
            raise JournalError("Existing archive does not prove Attempt closure")

    path = journal.archive_record(raw, document, validate_existing)
    return validate_archive_document({"schema_id": "secs.journal_archive_result.v1", "archive_path": str(path), "record_digest": digest,
            "execution_attempt_ref": execution_attempt_ref, "delivery": "unconfirmed",
            "retained": "Exact original work, identity, hold and evidence remain in the private archive.",
            "api_effect": f"The API read confirmed Attempt state {snapshot['state']}. No API mutation occurred; closure does not prove report delivery.",
            "automatic": "This held record will no longer be retried; the provider remains stopped.",
            "next_actor": "provider_operator",
            "next_action": "Restart the provider to resume admission of eligible work; this does not resend the archived report."})


JOURNAL_PATH = Path("/state/journal")
OWNER_PATH = Path("/run/config/attempt-owner.json")


def configured_api():
    """Acquire only signed API-read authority from the frozen deployment inputs."""
    owner = response_object(_read_regular_file(OWNER_PATH, 4096))
    config = decode_provider_config(_read_regular_file(CONFIG_PATH, _CONFIG_MAX_BYTES))
    credential = parse_provider_credential(_read_regular_file(CREDENTIAL_PATH, PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES))
    if owner != {"origin": config.endpoint.origin, "provider_ref": credential.provider_ref}:
        raise ValueError("API origin or provider identity differs from the retained journal ownership binding")
    api = ProviderApi(endpoint=config.endpoint.materialize(), provider_ref=credential.provider_ref,
                      credential_ref=credential.credential_ref, private_key=credential.private_key)
    return JobApi(api), owner


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-attempt-ref", required=True)
    parser.add_argument("--expected-record-digest", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(arguments)
    try:
        with AttemptStore(JOURNAL_PATH) as journal:
            api, owner = configured_api()
            result = archive_closed(journal=journal, api=api, owner=owner,
                execution_attempt_ref=args.execution_attempt_ref,
                expected_record_digest=args.expected_record_digest, reason=args.reason)
        print(json.dumps(result, ensure_ascii=False))
    except (JournalError, ApiError, ConfigurationError, OSError, ValueError) as error:
        parser.exit(2, f"Cannot confirm closed-Attempt archival: {error}. Preserve the journal and any archive; inspect retained state before retrying. No report was sent.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
