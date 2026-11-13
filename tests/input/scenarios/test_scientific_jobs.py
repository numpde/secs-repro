"""Recognizable multi-input Jobs at the scientific-worker boundary."""

from dataclasses import asdict
import json
import unittest
from time import monotonic

import numpy as np
from unittest.mock import AsyncMock, Mock

from secs.elucidation import StaticCandidateSource
from support_evidence import qualification_evidence
from secs_inference.provider.interpreter import InterpretationFailed, InterpretationSession, PreparationRejected

from input.helpers import FIXTURES, WorkerCase


async def _accept_selection(action):
    return action


class ScientificJobScenarios(WorkerCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.inference.embed_spectrum.return_value = np.array([1., 0.], dtype=np.float32)
        self.candidates = Mock(wraps=StaticCandidateSource([]))
        self.worker.candidates = self.candidates

    async def test_formula_correction_crosses_interpreter_and_scientific_worker(self):
        self.upload('proton.jdx')
        spectrum = self.one(self.discover())
        base = {'representation_id': spectrum['id'], 'formula': 'C21H36O7',
                'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7'},
                'processing': 'as_stored', 'explanation': 'Use the proton spectrum.'}
        corrected = base | {'formula': 'O7C22H36'}
        def turn(choice, call_id):
            return {'role': 'assistant', 'tool_calls': [{'id': call_id, 'type': 'function',
                'function': {'name': 'select_representation', 'arguments': json.dumps(choice)}}]}
        chat = Mock(identity='{}', retirement_confirmed=True)
        chat.retire = AsyncMock()
        chat.complete = AsyncMock(side_effect=[turn(base, 'select-1'), turn(corrected, 'select-2')])
        session = InterpretationSession((lambda **_: chat,), 'Formula C22H36O7.', [], Mock(),
                                        deadline=monotonic() + 30, timeout_seconds=30, context_bytes=1000000)
        async def prepare(choice):
            response = self.request('prepare', selection=asdict(choice))
            if response['outcome'] == 'input_rejected':
                raise PreparationRejected(response['reason'])
            return response
        result = await session.run(prepare)
        self.assertEqual(result['outcome'], 'prepared')
        self.assertEqual(len(chat.complete.await_args_list), 2)
        self.assertIn('different elemental compositions', str(chat.complete.await_args_list[1]))
        await session.retire()

    async def test_punctuated_quote_exhaustion_retains_safe_worker_reason(self):
        self.upload('proton.jdx')
        spectrum = self.one(self.discover())
        choice = {'representation_id': spectrum['id'], 'formula': 'C22H36O7',
                  'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7.'},
                  'processing': 'as_stored', 'explanation': 'PRIVATE_TEST_PAYLOAD'}
        turns = [{'role': 'assistant', 'tool_calls': [{'id': f'select-{index}', 'type': 'function',
                  'function': {'name': 'select_representation', 'arguments': json.dumps(choice)}}]}
                 for index in range(3)]
        chat = Mock(identity='{}', retirement_confirmed=True)
        chat.retire = AsyncMock()
        chat.complete = AsyncMock(side_effect=turns)
        session = InterpretationSession((lambda **_: chat,), 'Formula C22H36O7.', [], Mock(),
                                        deadline=monotonic() + 30, timeout_seconds=30, context_bytes=1000000)
        async def prepare(action):
            response = self.request('prepare', selection=asdict(action))
            if response['outcome'] == 'input_rejected':
                raise PreparationRejected(response['reason'])
            return response
        with self.assertRaises(InterpretationFailed) as caught:
            await session.run(prepare)
        corrections = session.conversation.correction_failures
        self.assertTrue(all('quoted formula cannot be parsed' in str(error) for _, _, error in corrections))
        self.assertTrue(all('PRIVATE_TEST_PAYLOAD' not in str(error) for _, _, error in corrections))
        self.assertEqual(len(corrections), 3)
        self.assertEqual(chat.complete.await_count, 3)
        await session.retire()

    async def test_formatted_job_formula_reaches_retrieval_with_its_original_evidence(self):
        self.upload('proton.jdx')
        spectrum = self.one(self.discover())
        for formula, quote in (
            ('C22H36O7', 'C₂₂H₃₆O₇'),
            (' C₂₂ H₃₆ O₇ ', 'C 22 H 36 O 7'),
            ('C\u00a022 H\u200936 O7', 'C₂₂H₃₆O₇'),
        ):
            with self.subTest(formula=formula, quote=quote):
                self.inference.reset_mock()
                self.candidates.reset_mock()
                choice = {'representation_id': spectrum['id'], 'formula': formula,
                          'formula_evidence': {'kind': 'job_specification', 'quote': quote},
                          'processing': 'as_stored', 'explanation': 'Use the supplied proton spectrum and formula.'}
                chat = Mock()
                chat.identity = '{}'
                chat.retirement_confirmed = True
                chat.retire = AsyncMock()
                chat.complete = AsyncMock()
                chat.complete.return_value = {'role': 'assistant', 'tool_calls': [{'id': 'selection', 'type': 'function',
                    'function': {'name': 'select_representation', 'arguments': json.dumps(choice)}}]}
                session = InterpretationSession((lambda **_: chat,),
                    f'Molecular formula: {quote}.', [], Mock(),
                    deadline=monotonic() + 10, timeout_seconds=10, context_bytes=1000000)
                decision = asdict(await session.run(_accept_selection))
                self.assertEqual(decision, choice)
                response = self.search(selection=decision)
                self.assertEqual(response['outcome'], 'no_starting_candidates')
                self.inference.embed_spectrum.assert_called_once()
                self.assertEqual(self.candidates.propose.call_args.args[1], 'C22H36O7')

    def test_worker_accepts_formatted_formula_from_discovered_structure_evidence(self):
        self.upload('proton.jdx', 'upload:spectrum')
        self.upload('ethanol.mol', 'upload:structure')
        spectrum = self.one(self.discover('upload:spectrum'))
        structure = self.one(self.discover('upload:structure'), 'structure', nucleus=None)
        response = self.search(selection={
            'representation_id': spectrum['id'], 'formula': ' C₂ H 6 O ',
            'formula_evidence': {'kind': 'representations', 'representation_ids': [structure['id']]},
            'processing': 'as_stored', 'explanation': 'Use the attached structure formula.'})
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.assertEqual(self.candidates.propose.call_args.args[1], 'C2H6O')

    @qualification_evidence("input.job-formula.execution.v1")
    def test_explicit_job_formula_survives_unrelated_structure_evidence(self):
        self.upload('proton.jdx', 'upload:proton')
        self.upload('carbon.jdx', 'upload:carbon')
        self.upload('ethanol.mol', 'upload:structure')
        proton = self.one(self.discover('upload:proton'))
        self.one(self.discover('upload:carbon'), nucleus='13C')
        structure = self.one(self.discover('upload:structure'), 'structure', nucleus=None)
        self.assertEqual(structure['metadata']['formula'], 'C2H6O')
        response = self.search(selection={'representation_id': proton['id'],
            'formula': 'C22H36O7',
            'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7'},
            'processing': 'as_stored',
            'explanation': 'Use the explicitly selected proton data and supplied Job formula; the structure is another sample.'})
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.inference.embed_spectrum.assert_called_once()
        self.candidates.propose.assert_called_once()
        self.assertEqual(self.candidates.propose.call_args.args[1], 'C22H36O7')

    @qualification_evidence("input.structure-formula.execution.v1")
    def test_discovered_structure_can_supply_the_executed_formula(self):
        for index, name in enumerate(('ethanol.mol', 'ethanol.sdf', 'structures.smi')):
            with self.subTest(fixture=name):
                self.files.clear()
                self.inference.reset_mock()
                self.candidates.reset_mock()
                spectrum_ref = f'upload:spectrum:{index}'
                structure_ref = f'upload:structure:{index}'
                self.upload('proton.jdx', spectrum_ref)
                self.upload(name, structure_ref)
                spectrum = self.one(self.discover(spectrum_ref))
                structures = self.discover(structure_ref)['representations']
                matching = [item for item in structures
                            if item['kind'] == 'structure'
                            and item['metadata'].get('formula') == 'C2H6O']
                self.assertEqual(len(matching), 1)

                response = self.search(selection={
                    'representation_id': spectrum['id'],
                    'formula': 'C2H6O',
                    'formula_evidence': {
                        'kind': 'representations',
                        'representation_ids': [matching[0]['id']],
                    },
                    'processing': 'as_stored',
                    'explanation': 'Use the selected proton spectrum and its attached structure formula.',
                })

                self.assertEqual(response['outcome'], 'no_starting_candidates')
                self.inference.embed_spectrum.assert_called_once()
                self.candidates.propose.assert_called_once()
                self.assertEqual(self.candidates.propose.call_args.args[1], 'C2H6O')

    def test_partial_inventory_allows_correction_then_one_execution(self):
        contents = (FIXTURES / 'proton.jdx').read_text()
        self.assertEqual(contents.count('##XYDATA=(X++(Y..Y))'), 1)
        broken = contents.replace('##XYDATA=(X++(Y..Y))', '##XYDATA=garbage')
        self.archive([('broken.jdx', broken.encode()), ('valid.jdx', contents.encode())])
        facts = self.discover()
        self.assertFalse(facts['complete'])
        self.assertTrue(any(issue['source'] == {'upload_ref': 'upload:sample', 'member': 'broken.jdx'}
                            for issue in facts['issues']))
        usable = [item for item in facts['representations']
                  if item['sources'] == [{'upload_ref': 'upload:sample', 'member': 'valid.jdx'}]]
        self.assertEqual(len(usable), 1)
        choice = {'representation_id': usable[0]['id'], 'formula': 'C22H36O7',
                  'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7'},
                  'explanation': 'Use the complete proton spectrum despite the separately reported corrupt member.'}
        rejected = self.search(selection=choice)
        self.assertEqual(rejected['outcome'], 'input_rejected')
        self.assertIn('processing', rejected['reason'].lower())
        self.assertEqual(self.inference.mock_calls, [])
        self.assertEqual(self.candidates.mock_calls, [])
        response = self.search(selection={**choice, 'processing': 'as_stored'})
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.inference.embed_spectrum.assert_called_once()
        self.candidates.propose.assert_called_once()
