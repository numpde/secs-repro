"""Inspection must explain retained work without changing it or starting services."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from dataclasses import replace
from secs_inference.provider.attempt_store import AttemptStore, JournalError
from secs_inference.provider.attempt_state import TerminalHold
from secs_inference.provider.job_api import complete_command
from test_execution import START, ACTIVE, REPORT

class JournalInspectionTests(unittest.TestCase):
    def test_readonly_owner_never_creates_syncs_or_changes_state(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)/'journal'
            with AttemptStore(root) as store: store.save(START)
            before={p.name:p.read_bytes() for p in root.iterdir()}
            with patch('secs_inference.provider.attempt_store.os.fsync', side_effect=AssertionError('Inspection cannot sync')):
                with AttemptStore(root, read_only=True) as store:
                    self.assertEqual(store.load(),START)
                    for mutate in (lambda:store.save(START), store.clear, lambda:store.record_report(ACTIVE,REPORT)):
                        with self.assertRaises(JournalError): mutate()
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})
            with self.assertRaises(JournalError): AttemptStore(root/'missing',read_only=True)
            self.assertFalse((root/'missing').exists())

    def test_module_inspects_all_stages_without_result_body(self):
        terminal=complete_command(ACTIVE,REPORT)
        held=replace(terminal,hold=TerminalHold('do_not_resend','execution_attempt_outcome_expired','Do not resend.','Attempt expired.','request:test'))
        pending=replace(terminal,hold=TerminalHold('reconcile_state','operation_conflict','Read current state.','Conflict.','request:test'))
        for state in (None,START,ACTIVE,terminal,held,pending):
            with self.subTest(state=type(state).__name__),TemporaryDirectory() as directory:
                root=Path(directory)/'journal'
                with AttemptStore(root) as store:
                    if state is not None: store.save(state)
                before={p.name:p.read_bytes() for p in root.iterdir()}
                result=subprocess.run([sys.executable,'-m','secs_inference.provider.journal_inspect','--journal',str(root)],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)
                document=json.loads(result.stdout)
                self.assertEqual(document['current_automation'],'stopped')
                self.assertEqual(len(document['records']),0 if state is None else 1)
                self.assertNotIn('body_base64',result.stdout.decode())
                self.assertNotIn('canonical_result_base64',result.stdout.decode())
                self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})

    def test_running_and_malformed_journal_are_not_inspection_authority(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)/'journal'
            command=[sys.executable,'-m','secs_inference.provider.journal_inspect','--journal',str(root)]
            with AttemptStore(root):
                result=subprocess.run(command,capture_output=True)
                self.assertNotEqual(result.returncode,0)
            (root/'attempt.json').write_bytes(b'not JSON')
            result=subprocess.run(command,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(result.stdout,b'')

    def test_private_ownership_and_existing_lock_are_required(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)/"journal"
            with AttemptStore(root): pass
            root.chmod(0o755)
            with self.assertRaises(JournalError): AttemptStore(root,read_only=True)
            root.chmod(0o700)
            (root/"owner.lock").unlink()
            with self.assertRaises(JournalError): AttemptStore(root,read_only=True)
            self.assertFalse((root/"owner.lock").exists())

    def test_plain_terminal_command_must_be_bound_before_inspection(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)/'journal'
            with AttemptStore(root) as store:
                store.save(replace(complete_command(ACTIVE,REPORT),body=b'not a terminal command'))
            result=subprocess.run([sys.executable,'-m','secs_inference.provider.journal_inspect','--journal',str(root)],capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(result.stdout,b'')
            self.assertNotIn(b'not a terminal command',result.stderr)

    def test_shared_output_contract_rejects_nested_raw_fields_and_malformed_records(self):
        from copy import deepcopy
        from secs_inference.provider.journal_inspect import inspect_journal
        from secs_inference.provider.inspection_document import parse_inspection_document, validate_inspection_document
        with TemporaryDirectory() as directory:
            root=Path(directory)/"journal"
            with AttemptStore(root) as store:
                terminal=replace(complete_command(ACTIVE,REPORT),hold=TerminalHold("do_not_resend","execution_attempt_outcome_expired","Do not resend.","Expired.","request:test"))
                store.save(terminal)
            document=inspect_journal(root)
            for location in ("envelope","record","recovery","stage","element"):
                changed=deepcopy(document)
                if location=="envelope": changed["raw_body"]="PRIVATE_REPORT_MARKER"
                elif location=="record": changed["records"][0]["body_base64"]="PRIVATE_REPORT_MARKER"
                elif location=="recovery": changed["records"][0]["recovery_on_restart"]["body"]="PRIVATE_REPORT_MARKER"
                elif location=="stage": changed["records"][0]["stage"]=[]
                else: changed["records"][0]=None
                with self.subTest(location=location),self.assertRaises(ValueError) as caught:
                    parse_inspection_document(json.dumps(changed).encode())
                self.assertNotIn("PRIVATE_REPORT_MARKER",str(caught.exception))
            with AttemptStore(root) as store: store.save(START)
            start_document=inspect_journal(root)
            self.assertEqual(validate_inspection_document(document),document)
            self.assertEqual(validate_inspection_document(start_document),start_document)
            with self.assertRaises(ValueError): parse_inspection_document(b" "*32769)
