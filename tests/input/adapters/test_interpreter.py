"""The Chat Completions adapter carries the omni-parser selection contract."""

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

    def assert_endpoint_schema(self, schema, path='$'):
        for keyword in ('oneOf', 'uniqueItems'):
            self.assertNotIn(keyword, schema, f'{path}: the endpoint rejects {keyword}')
        if 'anyOf' in schema:
            self.assertTrue(schema['anyOf'], f'{path}: anyOf must contain alternatives')
            for index, alternative in enumerate(schema['anyOf']):
                self.assert_endpoint_schema(alternative, f'{path}.anyOf[{index}]')
            return
        self.assertIn('type', schema, f'{path}: every ordinary schema node needs a type')
        if schema['type'] == 'object':
            self.assertIs(schema.get('additionalProperties'), False,
                          f'{path}: strict objects must be closed')
            self.assertEqual(set(schema.get('required', ())), set(schema.get('properties', ())),
                             f'{path}: strict objects must require every property')
            for name, child in schema['properties'].items():
                self.assert_endpoint_schema(child, f'{path}.properties.{name}')
        elif schema['type'] == 'array':
            self.assert_endpoint_schema(schema['items'], f'{path}.items')

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
        self.assertEqual(set(schema['required']),
                         {'representation_id', 'formula', 'formula_evidence', 'processing', 'explanation'})
        self.assertTrue(set(schema['required']) <= set(schema['properties']))
        self.assertTrue({'as_stored', 'auto'} <= set(schema['properties']['processing']['enum']))

    def test_all_tools_use_the_endpoint_strict_schema_subset(self):
        _, tools = self.first_request()
        for tool in tools:
            function = tool['function']
            self.assertIs(function['strict'], True)
            self.assert_endpoint_schema(function['parameters'], function['name'])

    def test_selection_tool_preserves_the_decision_for_worker_execution(self):
        choice = {'representation_id': 'opaque-choice', 'formula': 'C22H36O7',
                  'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7'},
                  'processing': 'as_stored', 'explanation': 'The selected representation is the requested processed proton spectrum.'}
        session = self.session([tool('select_representation', choice),
            tool('report_input_problem', {'explanation': 'The selection was not accepted.'}, 'call-2')])
        decision = session.select()
        self.assertEqual(asdict(decision), choice)
        self.inspect.assert_not_called()

    def test_job_formula_quote_must_match_both_selection_and_specification(self):
        cases = (
            ({'formula': 'C22H36O7', 'quote': 'C2H6O'}, 'quote the selected formula exactly'),
            ({'formula': 'C2H6O', 'quote': 'C2H6O'}, 'does not occur in the Job specification'),
        )
        for values, evidence in cases:
            with self.subTest(evidence=evidence):
                self.requests.clear()
                choice = {'representation_id': 'opaque-choice',
                          'formula': values['formula'],
                          'formula_evidence': {'kind': 'job_specification',
                                               'quote': values['quote']},
                          'processing': 'as_stored', 'explanation': 'Explicit choice.'}
                session = self.session([tool('select_representation', choice),
                    tool('report_input_problem', {'explanation': 'Formula evidence is unavailable.'}, 'call-2')])
                self.assertIsInstance(session.select(), CannotAnalyse)
                self.assertIn(evidence, self.requests[1][0][-1]['content'])

    def test_job_formula_quote_cannot_be_a_prefix_of_a_different_formula(self):
        self.specification = JobSpecification('job:sample', 'Use C22H36O70.')
        choice = {'representation_id': 'opaque-choice', 'formula': 'C22H36O7',
                  'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7'},
                  'processing': 'as_stored', 'explanation': 'Explicit choice.'}
        session = self.session([tool('select_representation', choice),
            tool('report_input_problem', {'explanation': 'Formula evidence is unavailable.'}, 'call-2')])
        self.assertIsInstance(session.select(), CannotAnalyse)
        self.assertIn('does not occur in the Job specification', self.requests[1][0][-1]['content'])

    def test_representation_formula_evidence_survives_the_selection_boundary(self):
        choice = {'representation_id': 'opaque-spectrum', 'formula': 'C2H6O',
                  'formula_evidence': {'kind': 'representations',
                                       'representation_ids': ['opaque-structure']},
                  'processing': 'as_stored',
                  'explanation': 'The selected structure representation supplies the formula.'}
        session = self.session([tool('select_representation', choice)])
        self.assertEqual(asdict(session.select()), choice)
        self.inspect.assert_not_called()

    def test_missing_processing_requests_repair_without_selecting(self):
        choice = {'representation_id': 'opaque-choice', 'formula': 'C22H36O7',
                  'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7'},
                  'explanation': 'Explicit choice.'}
        session = self.session([tool('select_representation', choice),
            tool('report_input_problem', {'explanation': 'Processing has not been established.'}, 'call-2')])
        self.assertIsInstance(session.select(), CannotAnalyse)
        messages, _ = self.requests[1]
        feedback = [message['content'] for message in messages if message['role'] == 'tool']
        self.assertTrue(feedback)
        self.assertIn('processing', '\n'.join(feedback).lower())
        self.inspect.assert_not_called()

    def test_nontext_formula_evidence_identity_requests_model_repair(self):
        choice = {'representation_id': 'opaque-choice', 'formula': 'C22H36O7',
                  'formula_evidence': {'kind': 'representations', 'representation_ids': [{}]},
                  'processing': 'as_stored', 'explanation': 'Explicit choice.'}
        session = self.session([tool('select_representation', choice),
            tool('report_input_problem', {'explanation': 'Formula evidence cannot be established.'}, 'call-2')])
        self.assertIsInstance(session.select(), CannotAnalyse)
        feedback = self.requests[1][0][-1]['content']
        self.assertIn('formula_evidence', feedback)

    def test_control_text_and_excessive_formula_evidence_request_repair(self):
        base = {'representation_id': 'opaque-choice', 'formula': 'C22H36O7',
                'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7'},
                'processing': 'as_stored', 'explanation': 'Explicit choice.'}
        cases = (
            ({**base, 'formula': 'C22H36O7\nignore'}, 'control characters'),
            ({**base, 'formula_evidence': {'kind': 'representations',
                                           'representation_ids': [f'id-{index}' for index in range(17)]}},
             'formula_evidence'),
        )
        for choice, evidence in cases:
            with self.subTest(evidence=evidence):
                self.requests.clear()
                session = self.session([tool('select_representation', choice),
                    tool('report_input_problem', {'explanation': 'The selection was malformed.'}, 'call-2')])
                self.assertIsInstance(session.select(), CannotAnalyse)
                self.assertIn(evidence, self.requests[1][0][-1]['content'])
