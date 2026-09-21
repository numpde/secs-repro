"""Invalid, stale or unsuitable choices fail before scientific computation."""

from input.helpers import FIXTURES, WorkerCase
from pathlib import Path


class SelectionRefusalTests(WorkerCase):
    def test_annotation_evidence_cannot_execute_without_its_dense_resource(self):
        self.upload('annotations.sdf')
        facts = self.discover()
        self.assertFalse(facts['complete'])
        self.assertTrue(facts['representations'], 'The structure remains discoverable')
        # Known spectrum metadata may remain visible despite its missing data.
        for item in facts['representations']:
            with self.subTest(kind=item['kind']):
                if item['kind'] == 'spectrum':
                    response = self.rejected(self.selection(item['id']), 'proton.jdx')
                    self.assertRegex(response['reason'].lower(), r'missing|unavailable|unresolved|not (available|provided)|cannot.*(find|resolve|load)')
                else:
                    self.rejected(self.selection(item['id']), 'proton', 'spectrum')

    def test_two_dimensional_proton_data_are_not_flattened_for_secs(self):
        self.upload('synthetic-2d.jdx')
        item = self.one(self.discover(), nucleus=None)
        self.rejected(self.selection(item['id']), '2D', '1D')

    def selection(self, identity):
        return {'representation_id': identity, 'formula': 'C22H36O7',
                'processing': 'as_stored', 'explanation': 'Explicit selection for SECS.'}

    def rejected(self, selection, *evidence):
        response = self.request('analyse', selection=selection)
        self.assertEqual(response['outcome'], 'input_rejected')
        for term in evidence:
            self.assertIn(term.lower(), response['reason'].lower())
        self.assertNotIn(str(self.root), response['reason'])
        self.assertEqual(self.inference.mock_calls, [])
        self.assertEqual(self.candidates.mock_calls, [])
        return response

    def test_unknown_selection_cannot_fall_back_to_available_proton_data(self):
        self.upload('proton.jdx')
        self.rejected(self.selection('unissued-representation'), 'representation')

    def test_missing_identity_cannot_choose_the_only_available_spectrum(self):
        self.upload('proton.jdx')
        selection = self.selection('unused')
        del selection['representation_id']
        self.rejected(selection, 'representation')

    def test_peak_table_and_carbon_cannot_be_substituted_with_proton_data(self):
        self.upload('proton.jdx', 'upload:alternative')
        for fixture, kind, nucleus, evidence in (
            ('peaks.jdx', 'peak_table', '1H', 'peak'),
            ('carbon.jdx', 'spectrum', '13C', '13C'),
        ):
            with self.subTest(fixture=fixture):
                self.upload(fixture)
                item = self.one(self.discover(), kind, nucleus)
                self.rejected(self.selection(item['id']), evidence, 'proton')

    def test_raw_fid_cannot_run_as_stored(self):
        self.upload('fid.jdx')
        item = self.one(self.discover(), 'fid')
        self.rejected(self.selection(item['id']), 'processing')

    def test_missing_formula_is_not_inferred_from_an_unselected_structure(self):
        self.upload('proton.jdx')
        self.upload('ethanol.mol', 'upload:structure')
        item = self.one(self.discover())
        selection = self.selection(item['id'])
        del selection['formula']
        self.rejected(selection, 'formula')

    def test_missing_processing_is_not_implicit_auto_processing(self):
        self.upload('fid.jdx')
        item = self.one(self.discover(), 'fid')
        selection = self.selection(item['id'])
        del selection['processing']
        self.rejected(selection, 'processing')

    def test_selection_from_removed_upload_cannot_resolve_to_duplicate_bytes(self):
        self.upload('proton.jdx', 'upload:removed')
        self.upload('proton.jdx', 'upload:retained')
        item = self.one(self.discover('upload:removed'))
        del self.files['upload:removed']
        self.rejected(self.selection(item['id']), 'representation')

    def test_changed_bytes_invalidate_the_previously_discovered_selection(self):
        self.upload('spectrum.jdx', contents=(FIXTURES / 'proton.jdx').read_bytes())
        item = self.one(self.discover())
        Path(self.files['upload:sample']).write_bytes((FIXTURES / 'alternate.jdx').read_bytes())
        self.rejected(self.selection(item['id']), 'representation')
