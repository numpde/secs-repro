"""Inspect private retained obligations without changing their recovery state."""
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone
from .attempt_store import AttemptStore, JournalError
from .job_api import ApiError, terminal_receipt_facts
from .inspection_document import validate_inspection_document
from .attempt_state import StartPending, ActiveAttempt, TerminalPending, terminal_recovery_facts
from hashlib import sha256


def inspect_journal(directory: Path) -> dict:
    with AttemptStore(directory, read_only=True) as journal:
        loaded = journal.load_record()
        record = None if loaded is None else loaded[1]
        if isinstance(record, TerminalPending):
            try:
                terminal_receipt_facts(record)
            except (ValueError, TypeError, KeyError, RecursionError, ApiError):
                raise JournalError("The retained terminal command is unreadable or names another obligation") from None
        records = []
        if record is not None:
            start = record if isinstance(record, StartPending) else record.start if isinstance(record, ActiveAttempt) else record.active.start
            facts = {"record_digest": "sha256:" + sha256(loaded[0]).hexdigest(),
                     "provider_ref": start.provider_ref, "job_ref": start.selected.job_ref,
                     "provider_attempt_key": start.provider_attempt_key,
                     "current_automation": "stopped", "next_actor": "provider_operator",
                     "next_action": "Review the retained state before restarting the provider."}
            if isinstance(record, StartPending):
                facts.update(stage="start", restart_behavior="Recover the same start intent; earlier admission may be unconfirmed.")
            elif isinstance(record, ActiveAttempt):
                facts.update(stage="active", execution_attempt_ref=record.execution_attempt_ref,
                             restart_behavior="Read the Attempt; interrupted computation is not rerun.")
            else:
                facts.update(stage="terminal", execution_attempt_ref=record.active.execution_attempt_ref,
                             operation=record.operation, command_fingerprint="sha256:" + sha256(record.body).hexdigest(),
                             command_bytes=len(record.body), command_retained=True, delivery="unconfirmed",
                             restart_behavior="Retry the exact retained terminal command; computation is not rerun.")
                if record.hold is not None:
                    recovery = terminal_recovery_facts(record)
                    facts.update(stage="terminal_reconciling" if record.hold.reconciling else "terminal_held",
                                 restart_behavior="Retry only the Attempt read with backoff." if record.hold.reconciling else "Keep publication stopped; operator reconciliation is required.",
                                 recovery_on_restart=recovery,
                                 next_action="Reconcile the retained command and API outcome before deciding any further action." if not record.hold.reconciling else "Restart the provider to continue read-only reconciliation.")
            records.append(facts)
    return validate_inspection_document({"schema_id": "secs.journal_inspection.v1", "record_count": len(records), "observed_at": datetime.now(timezone.utc).isoformat(),
            "current_automation": "stopped", "records": records})


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path("/state/journal"))
    args = parser.parse_args(arguments)
    try:
        print(json.dumps(inspect_journal(args.journal), ensure_ascii=False))
    except (JournalError, OSError, ValueError) as error:
        parser.exit(2, f"Cannot inspect retained work: {error}. Stop the owning provider before inspection.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
