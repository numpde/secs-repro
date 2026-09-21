"""Recognizable multi-input Jobs at the scientific-worker boundary."""

import numpy as np
from unittest.mock import Mock

from secs.elucidation import StaticCandidateSource

from input.helpers import FIXTURES, WorkerCase


class ScientificJobScenarios(WorkerCase):
    def setUp(self):
        super().setUp()
        self.inference.embed_spectrum.return_value = np.array([1., 0.], dtype=np.float32)
        self.candidates = Mock(wraps=StaticCandidateSource([]))
        self.worker.candidates = self.candidates

    def test_explicit_job_formula_survives_unrelated_structure_evidence(self):
        self.upload('proton.jdx', 'upload:proton')
        self.upload('carbon.jdx', 'upload:carbon')
        self.upload('ethanol.mol', 'upload:structure')
        proton = self.one(self.discover('upload:proton'))
        self.one(self.discover('upload:carbon'), nucleus='13C')
        structure = self.one(self.discover('upload:structure'), 'structure', nucleus=None)
        self.assertEqual(structure['metadata']['formula'], 'C2H6O')
        response = self.request('analyse', selection={'representation_id': proton['id'],
            'formula': 'C22H36O7', 'processing': 'as_stored',
            'explanation': 'Use the explicitly selected proton data and supplied Job formula; the structure is another sample.'})
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.inference.embed_spectrum.assert_called_once()
        self.candidates.propose.assert_called_once()
        self.assertEqual(self.candidates.propose.call_args.args[1], 'C22H36O7')

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
                  'explanation': 'Use the complete proton spectrum despite the separately reported corrupt member.'}
        rejected = self.request('analyse', selection=choice)
        self.assertEqual(rejected['outcome'], 'input_rejected')
        self.assertIn('processing', rejected['reason'].lower())
        self.assertEqual(self.inference.mock_calls, [])
        self.assertEqual(self.candidates.mock_calls, [])
        response = self.request('analyse', selection={**choice, 'processing': 'as_stored'})
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.inference.embed_spectrum.assert_called_once()
        self.candidates.propose.assert_called_once()
