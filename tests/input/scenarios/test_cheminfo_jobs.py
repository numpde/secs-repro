"""Real uploaded experiments reach preparation through an explicit selection."""

import numpy as np
from unittest.mock import Mock

from secs.elucidation import StaticCandidateSource

from input.cheminfo import COFFEE, CheminfoCase


class CheminfoJobScenarios(CheminfoCase):
    def setUp(self):
        super().setUp()
        self.inference.embed_spectrum.return_value = np.array([1., 0.], dtype=np.float32)
        self.candidates = Mock(wraps=StaticCandidateSource([]))
        self.worker.candidates = self.candidates

    def test_processed_proton_examples_reach_inference_despite_unusable_raw_alternatives(self):
        for path in ('aspirin-1h-processed.zip', 'cyclosporin/cyclosporin_1h.zip',
                     'ibuprofen/processed/proton.zip', 'strychnine-1h.zip', 'topspin365.zip'):
            self.fixture('zipped/' + path)
            facts = self.discover()
            protons = [item for item in facts['representations'] if item['metadata']['nucleus'] == '1H']
            self.assertTrue(protons)
            for item in protons:
                with self.subTest(archive=path, sources=item['sources']):
                    response = self.request('analyse', selection=self.selection(item))
                    self.assert_prepared(response, item)
                    self.inference.reset_mock()
                    self.candidates.reset_mock()

    def test_whole_coffee_folders_keep_supported_1d_experiments_and_choices(self):
        for name, experiments in zip(COFFEE, (('20', '21', '22', '99999'),
                                              ('10', '11', '12', '99999'))):
            with self.subTest(sample=name):
                self.coffee(name)
                facts = self.discover()
                expected = {(f'{name}/{experiment}/pdata/1/1r',
                             f'{name}/{experiment}/pdata/1/procs') for experiment in experiments}
                processed = [item for item in facts['representations'] if item['kind'] == 'spectrum']
                self.assertEqual({tuple(source['member'] for source in item['sources'])
                                  for item in processed}, expected)
                self.assertEqual(len(processed), len(expected))
                self.assertEqual(len({item['id'] for item in facts['representations']}), 5)
                fid = self.one(facts, 'fid')
                self.assertEqual(fid['sources'], [{'upload_ref': 'upload:sample',
                                 'member': f'{name}/99999/{member}'} for member in ('fid', 'acqus')])
                for item in processed + [fid]:
                    with self.subTest(sources=item['sources']):
                        raw = item['kind'] == 'fid'
                        response = self.request('analyse', selection=self.selection(
                            item, processing='auto' if raw else 'as_stored'))
                        self.assert_prepared(response, item, from_fid=raw)
                        self.inference.reset_mock()
                        self.candidates.reset_mock()

    def test_proton_carbon_and_structure_uploads_supply_distinct_roles_in_either_order(self):
        for order in (('proton', 'carbon', 'structure'), ('structure', 'carbon', 'proton')):
            with self.subTest(order=order):
                self.files.clear()
                for role in order:
                    path = ('zipped/ibuprofen/ibuprofen.mol' if role == 'structure'
                            else f'zipped/ibuprofen/processed/{role}.zip')
                    self.fixture(path, f'upload:{role}')
                facts = {role: self.discover(f'upload:{role}') for role in order}
                carbon = self.one(facts['carbon'], nucleus='13C')
                self.assertEqual(carbon['metadata']['dimension'], 1)
                structure = self.one(facts['structure'], 'structure', nucleus=None)
                self.assertEqual(structure['metadata']['formula'], 'C13H18O2')
                protons = facts['proton']['representations']
                self.assertEqual(len(protons), 2)
                prepared = []
                for item in protons:
                    selection = self.selection(item, formula='C13H18O2')
                    selection['formula_evidence'] = {'kind': 'representations',
                                                     'representation_ids': [structure['id']]}
                    response = self.request('analyse', selection=selection)
                    self.assert_prepared(response, item)
                    self.assertEqual(self.candidates.propose.call_args.args[1], 'C13H18O2')
                    prepared.append(self.inference.embed_spectrum.call_args.args[0].copy())
                    self.inference.reset_mock()
                    self.candidates.reset_mock()
                self.assertFalse(np.array_equal(*prepared), 'Distinct pdata must not share one prepared input')
