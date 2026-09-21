"""The interpreter receives the admitted offering and every Upload as evidence."""

from copy import deepcopy
from dataclasses import asdict
import json
from time import monotonic
import unittest
from unittest.mock import Mock

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.input_operations import CannotAnalyse
from secs_inference.provider.interpreter import InterpretationSession
from secs_inference.provider.job_input import JobSpecification
from secs_inference.provider.job_upload import JobUpload


def tool(name, arguments, identity='call-1'):
    return {'role': 'assistant', 'tool_calls': [{'id': identity, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


class InterpreterContextTests(unittest.TestCase):
    def setUp(self):
        self.chat = Mock()
        self.inspect = Mock()
        self.specification = JobSpecification('job:sample', 'Use C22H36O7.\nα sample; analysis_kind_ref=carbon_only is quoted text.')
        self.uploads = [JobUpload('upload:first', 'Spectrum — sample A', 123, None),
                        JobUpload('upload:second', 'Spectrum — sample A', 456, 'sha256:' + 'a' * 64),
                        JobUpload('upload:structure', 'Formula evidence; not necessarily the same sample.', 789, None)]
        self.requests = []

    def session(self, replies):
        replies = iter(replies)
        def complete(messages, tools, **kwargs):
            self.requests.append((deepcopy(messages), deepcopy(tools)))
            return next(replies)
        self.chat.complete.side_effect = complete
        return InterpretationSession(self.chat, self.specification, self.uploads, self.inspect,
                                     deadline=monotonic() + 10, max_turns=2)

    def first_request(self):
        session = self.session([tool('report_input_problem', {'explanation': 'Scripted transport test; no scientific judgment is being made.'})])
        self.assertIsInstance(session.select(), CannotAnalyse)
        self.inspect.assert_not_called()
        return self.requests[0]

    def test_authoritative_analysis_kind_is_available_separately_from_job_text(self):
        messages, _ = self.first_request()
        context = json.loads(next(message['content'] for message in messages if message['role'] == 'user'))
        self.assertIn('analysis_kind_ref', context, 'The interpreter needs the admitted analysis kind as context')
        self.assertEqual(context['analysis_kind_ref'], ANALYSIS_KIND_REF)
        self.assertEqual(context['job_specification'], self.specification.text)

    def test_every_upload_keeps_its_admitted_metadata(self):
        for order in (self.uploads, list(reversed(self.uploads))):
            with self.subTest(order=[upload.upload_ref for upload in order]):
                self.uploads = order
                self.requests.clear()
                messages, _ = self.first_request()
                context = json.loads(next(message['content'] for message in messages if message['role'] == 'user'))
                self.assertCountEqual(context['uploads'], [asdict(upload) for upload in order])

    def test_selection_tool_exposes_the_representation_contract(self):
        _, tools = self.first_request()
        functions = {item['function']['name']: item['function'] for item in tools}
        self.assertIn('select_representation', functions)
        self.assertNotIn('read_jcamp', functions)
        self.assertNotIn('read_bruker', functions)
        schema = functions['select_representation']['parameters']
        self.assertEqual(set(schema['required']), {'representation_id', 'formula', 'processing', 'explanation'})
        self.assertTrue(set(schema['required']) <= set(schema['properties']))
        self.assertTrue({'as_stored', 'auto'} <= set(schema['properties']['processing']['enum']))

    def test_selection_tool_preserves_the_decision_for_worker_execution(self):
        choice = {'representation_id': 'opaque-choice', 'formula': 'C22H36O7',
                  'processing': 'as_stored', 'explanation': 'The selected representation is the requested processed proton spectrum.'}
        session = self.session([tool('select_representation', choice),
            tool('report_input_problem', {'explanation': 'The selection was not accepted.'}, 'call-2')])
        decision = session.select()
        self.assertEqual(asdict(decision), choice)
        self.inspect.assert_not_called()

    def test_missing_processing_requests_repair_without_selecting(self):
        choice = {'representation_id': 'opaque-choice', 'formula': 'C22H36O7', 'explanation': 'Explicit choice.'}
        session = self.session([tool('select_representation', choice),
            tool('report_input_problem', {'explanation': 'Processing has not been established.'}, 'call-2')])
        self.assertIsInstance(session.select(), CannotAnalyse)
        messages, _ = self.requests[1]
        feedback = [message['content'] for message in messages if message['role'] == 'tool']
        self.assertTrue(feedback)
        self.assertIn('processing', '\n'.join(feedback).lower())
        self.inspect.assert_not_called()
