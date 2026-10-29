"""Retain SECS scientific selection and correction policy over an async carrier."""

from copy import deepcopy
import json
from time import monotonic
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.upload_response import UploadMember
from secs_inference.provider.input_operations import CannotAnalyse, SelectedRepresentation, SourceRef
from secs_inference.provider.interpreter import InterpretationFailed, InterpretationSession


def tool(name, arguments, call_id='call-1'):
    return {'role': 'assistant', 'tool_calls': [{'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}

def selection(*, representation_id='opaque-spectrum', formula='C2H6O', explanation='The selected representation is the proton spectrum.', processing='as_stored', evidence=None, call_id='call-1'):
    return tool('select_representation', {'representation_id': representation_id, 'formula': formula, 'formula_evidence': evidence or {'kind': 'job_specification', 'quote': formula}, 'processing': processing, 'explanation': explanation}, call_id)


class ScriptedChat:
    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        return next(self.turns)


class InterpreterTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, chat, inspect=lambda source: {}, max_turns=8):
        return InterpretationSession(
            chat, "Find C2H6O",
            (UploadMember("upload:first", "carbon distractor", 4, None),
             UploadMember("upload:second", "proton choices", 10, None)),
            AsyncMock(side_effect=inspect), deadline=monotonic() + 3, max_turns=max_turns,
        )

    async def test_inspection_selects_among_distractors_and_preserves_every_description(self):
        chat = ScriptedChat(tool('inspect_source', {'source': {'upload_ref': 'upload:second', 'member': None}}), selection(representation_id='opaque-experiment-2', explanation='Experiment 2 identifies proton data.', call_id='choice'))
        inspected = []

        def inspect(source):
            inspected.append(source)
            return {'members': ['experiment1/carbon.jdx', 'experiment2/spectrum.jdx']}
        decision = await self._session(chat, inspect).select()
        self.assertEqual(decision.representation_id, 'opaque-experiment-2')
        self.assertEqual(inspected, [SourceRef('upload:second')])
        initial = chat.requests[0][1]['content']
        self.assertIn('carbon distractor', initial)
        self.assertIn('proton choices', initial)
        self.assertIn('experiment1/carbon.jdx', chat.requests[1][-1]['content'])

    async def test_selection_preserves_identity_formula_reason_and_processing(self):
        decision = await self._session(ScriptedChat(selection(representation_id='opaque-bruker-pair', processing='as_stored', explanation='This processed pair identifies proton data.'))).select()
        self.assertEqual(decision, SelectedRepresentation('opaque-bruker-pair', 'C2H6O', {'kind': 'job_specification', 'quote': 'C2H6O'}, 'as_stored', 'This processed pair identifies proton data.'))

    async def test_selection_rejection_returns_to_its_call_without_resetting_turn_budget(self):
        choice = selection(explanation='Description identifies proton.', call_id='selected')
        chat = ScriptedChat(choice, tool('report_input_problem', {'explanation': 'No supported proton data was established.'}))
        session = self._session(chat, max_turns=2)
        self.assertIsInstance(await session.select(), SelectedRepresentation)
        deadline = session.deadline
        session.reject('The selected file contains carbon, not proton data.')
        self.assertIsInstance(await session.select(), CannotAnalyse)
        self.assertEqual(chat.requests[1][-1], {'role': 'tool', 'tool_call_id': 'selected', 'content': 'The selected file contains carbon, not proton data.'})
        self.assertEqual(session.deadline, deadline)
        self.assertEqual(session.remaining_turns, 0)

    async def test_budget_exhaustion_is_not_an_input_problem(self):
        chat = ScriptedChat(tool('invent_reader', {}))
        with self.assertRaises(InterpretationFailed) as caught:
            await self._session(chat, max_turns=1).select()
        self.assertIn('allowed model turns were exhausted', str(caught.exception))
        self.assertIn('selecting an input representation', str(caught.exception))

    async def test_expired_interpretation_budget_does_not_call_the_model(self):
        chat = ScriptedChat()
        session = self._session(chat)
        session.deadline = monotonic() - 1
        with self.assertRaises(InterpretationFailed) as caught:
            await session.select()
        self.assertIn('interpretation deadline elapsed', str(caught.exception))
        self.assertEqual(chat.requests, [])

    async def test_repair_feedback_distinguishes_unknown_tools_and_text_constraints(self):
        valid = {'representation_id': 'opaque-spectrum', 'formula': 'C2H6O', 'formula_evidence': {'kind': 'job_specification', 'quote': 'C2H6O'}, 'processing': 'as_stored', 'explanation': 'Proton spectrum.'}
        cases = [(tool('invent_reader', {}), 'choose one of the advertised tools')]
        for value, reason in ((5, 'formula field must be text'), ('', 'formula field must not be empty'), ('\x00', 'formula field contains control characters'), ('x' * 65537, '65536-character limit')):
            cases.append((tool('select_representation', valid | {'formula': value}), reason))
        for call, reason in cases:
            with self.subTest(reason=reason):
                chat = ScriptedChat(call, tool('report_input_problem', {'explanation': 'Cannot establish an input.'}))
                await self._session(chat).select()
                self.assertIn(reason, chat.requests[-1][-1]['content'])

    async def test_malformed_json_arguments_get_correction_in_a_valid_conversation(self):
        malformed = tool('inspect_source', {})
        malformed['tool_calls'][0]['function']['arguments'] = 'not JSON'
        chat = ScriptedChat(malformed, tool('report_input_problem', {'explanation': 'No supported source established.'}))
        decision = await self._session(chat).select()
        self.assertIsInstance(decision, CannotAnalyse)
        self.assertIn('not readable JSON', chat.requests[1][-1]['content'])

    async def test_broken_selection_construction_is_not_sent_to_the_model_for_repair(self):
        chat = ScriptedChat(selection())
        session = self._session(chat)
        with patch('secs_inference.provider.interpreter.SelectedRepresentation', side_effect=TypeError('broken wiring')):
            with self.assertRaisesRegex(TypeError, 'broken wiring'):
                await session.select()
        self.assertEqual(len(chat.requests), 1)
        self.assertNotEqual(session.messages[-1]['role'], 'tool')

    async def test_unserializable_member_is_corrected_before_source_access(self):
        chat = ScriptedChat(tool('inspect_source', {'source': {'upload_ref': 'upload:second', 'member': '\ud800'}}), selection(representation_id='opaque-proton'))
        decision = await self._session(chat, lambda _: self.fail('Malformed source reached inspection')).select()
        self.assertEqual(decision.representation_id, 'opaque-proton')
        self.assertIn('not valid Unicode', chat.requests[1][-1]['content'])

    async def test_nontext_tool_arguments_are_not_replayed_as_an_assistant_message(self):
        malformed = tool('inspect_source', {})
        malformed['tool_calls'][0]['function']['arguments'] = {}
        session = self._session(ScriptedChat(malformed))
        with self.assertRaises(InterpretationFailed):
            await session.select()
        self.assertEqual(len(session.messages), 2)

    async def test_ambiguous_call_identity_is_not_admitted_to_the_conversation(self):
        message = tool('inspect_source', {})
        message['tool_calls'] *= 2
        session = self._session(ScriptedChat(message))
        with self.assertRaises(InterpretationFailed):
            await session.select()
        self.assertEqual(len(session.messages), 2)

    async def test_download_outage_never_becomes_semantic_feedback(self):
        chat = ScriptedChat(tool('inspect_source', {'source': {'upload_ref': 'upload:second', 'member': None}}))

        def inspect(source):
            raise OSError('store unavailable')
        session = self._session(chat, inspect)
        with self.assertRaises(OSError):
            await session.select()
        self.assertEqual(len(chat.requests), 1)
        self.assertNotEqual(session.messages[-1]['role'], 'tool')
