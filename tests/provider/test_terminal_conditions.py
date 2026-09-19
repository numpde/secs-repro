"""Reporting observations never replace retained terminal recovery."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from test_execution import ACTIVE, REPORT, FakeApi
from secs_inference.provider.attempt_store import AttemptStore, _encode, _decode
from secs_inference.provider.execution import ExecutionLoop
from secs_inference.provider.job_api import ApiError, ApiUnavailable, complete_command

class TerminalConditions(unittest.TestCase):
    def test_legacy_phase_is_unknown_and_encoding_unchanged(self):
        raw = _encode(ACTIVE)
        self.assertNotIn('local_phase', raw)
        self.assertIsNone(_decode(raw).local_phase)
        self.assertEqual(_encode(_decode(raw)), raw)

    def test_truthful_phase_and_automation_preserve_original_terminal(self):
        for phase in ('preparing', 'running'):
            for error, automation in ((ApiUnavailable('lost response'), 'retrying'), (ApiError('refused'), 'held')):
                with self.subTest(phase=phase, automation=automation), TemporaryDirectory() as directory:
                    active = replace(ACTIVE, local_phase=phase)
                    terminal = complete_command(active, REPORT)
                    api = FakeApi()
                    calls = []
                    def publish(command):
                        calls.append(('terminal', command.body))
                        raise error
                    def progress(active, code):
                        calls.append(('progress', active.local_phase, code))
                        raise ApiError('secondary observation lost')
                    api.publish, api.progress = publish, progress
                    with AttemptStore(Path(directory)/'journal') as store:
                        store.save(terminal)
                        loop = ExecutionLoop(api, store, None, store.diagnose, retry_pending=lambda: True)
                        with self.assertRaises(type(error)) as caught:
                            loop.step()
                        self.assertIs(caught.exception, error)
                        self.assertEqual(store.load(), terminal)
                    self.assertEqual(calls, [('terminal', terminal.body), ('progress', phase, 'terminal_report_delivery_' + ('retryable' if automation=='retrying' else 'held'))])

    def test_execution_boundary_persists_phase_before_analysis_failure(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory)/'journal') as store:
            api=FakeApi()
            api.fail_publication=True
            def analyse(active):
                loop.mark_execution_entered()
                self.assertEqual(store.load().local_phase, 'running')
                raise ValueError('controlled worker failure')
            loop=ExecutionLoop(api, store, analyse, store.diagnose)
            with self.assertRaises(ApiUnavailable):
                loop.step()
            self.assertEqual(store.load().active.local_phase, 'running')

    def test_phase_storage_failure_never_becomes_computation_failure(self):
        from secs_inference.provider.attempt_store import JournalError
        from unittest.mock import patch
        with TemporaryDirectory() as directory, AttemptStore(Path(directory)/'journal') as store:
            api=FakeApi()
            error=JournalError('phase retention unconfirmed')
            def analyse(active):
                with patch.object(store, 'save', side_effect=error):
                    loop.mark_execution_entered()
            loop=ExecutionLoop(api, store, analyse, store.diagnose)
            with self.assertRaises(JournalError) as caught:
                loop.step()
            self.assertIs(caught.exception, error)
            self.assertFalse(any(isinstance(x,bytes) for x in api.calls))

    def test_progress_receipt_requires_correlated_facts_and_timestamp(self):
        import json
        from unittest.mock import Mock
        from secs_inference.provider.job_api import JobApi
        from secs_inference.provider.http import HttpResponse
        active=replace(ACTIVE,local_phase='running')
        code='terminal_report_delivery_held'
        receipt={'schema_id':'nmr.provider.execution_attempt_progress_response.v1',
                 'execution_attempt_ref':active.execution_attempt_ref,'phase':'running',
                 'condition_code':code,'updated_at':'2026-09-19T16:00:00Z'}
        transport=Mock(provider_ref=active.start.provider_ref)
        for field, value in [('updated_at',None),('updated_at','invalid'),('phase','preparing'),('condition_code','different')]:
            with self.subTest(field=field,value=value):
                transport.request.return_value=HttpResponse(200,'request-test',json.dumps(receipt|{field:value}).encode())
                with self.assertRaises(ApiError):
                    JobApi(transport).progress(active,code)
        transport.request.return_value=HttpResponse(200,'request-test',json.dumps(receipt).encode())
        self.assertEqual(JobApi(transport).progress(active,code),receipt['updated_at'])

    def test_read_reconciliation_restart_updates_condition_then_suppresses_closed(self):
        from secs_inference.provider.attempt_state import TerminalHold
        from secs_inference.provider.job_api import AttemptSnapshot
        from unittest.mock import Mock
        terminal=replace(complete_command(replace(ACTIVE,local_phase='running'),REPORT),
                         hold=TerminalHold('reconcile_state','operation_conflict','Read current state','Conflict','request-test'))
        api=FakeApi()
        api.snapshot=Mock(side_effect=[ApiUnavailable('read unavailable'),AttemptSnapshot('in_progress','open')])
        api.progress=Mock(return_value='2026-09-19T16:00:00Z')
        with TemporaryDirectory() as directory:
            for expected, code in ((ApiUnavailable,'reconciling'),(ApiError,'held')):
                with AttemptStore(Path(directory)/'journal') as store:
                    if store.load() is None: store.save(terminal)
                    with self.assertRaises(expected):
                        ExecutionLoop(api,store,None,store.diagnose,retry_pending=lambda:True).step()
                    self.assertEqual(store.load().body,terminal.body)
                    self.assertEqual(api.progress.call_args.args[1], 'terminal_report_delivery_'+code)
            with AttemptStore(Path(directory)/'journal') as store:
                latest=store.load()
                store.save(replace(latest,hold=replace(latest.hold,observed_state='expired')))
                api.progress.reset_mock()
                with self.assertRaises(ApiError):
                    ExecutionLoop(api,store,None,store.diagnose).step()
                api.progress.assert_not_called()
        self.assertFalse(any(isinstance(x,bytes) for x in api.calls))

    def test_shutdown_or_legacy_unknown_phase_does_not_claim_retry(self):
        from unittest.mock import Mock
        for phase,retry in [('running',False),(None,True)]:
            with self.subTest(phase=phase,retry=retry), TemporaryDirectory() as directory, AttemptStore(Path(directory)/'journal') as store:
                terminal=complete_command(replace(ACTIVE,local_phase=phase),REPORT)
                store.save(terminal)
                api=FakeApi();api.fail_publication=True;api.progress=Mock()
                with self.assertRaises(ApiUnavailable):
                    ExecutionLoop(api,store,None,store.diagnose,retry_pending=lambda:retry).step()
                api.progress.assert_not_called()
                self.assertEqual(store.load(),terminal)
