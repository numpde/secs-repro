"""Invalid, stale or unsuitable choices fail before scientific computation."""

from input.helpers import FIXTURES, OTHER_ATTEMPT_REF, WorkerCase
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
                    self.rejected(self.selection(item['id']),
                                  'cannot execute', 'unavailable', 'proton.jdx')
                else:
                    self.rejected(self.selection(item['id']),
                                  'selected structure', 'requires 1H spectrum')

    def test_two_dimensional_proton_data_are_not_flattened_for_secs(self):
        self.upload('synthetic-2d.jdx')
        item = self.one(self.discover(), nucleus=None)
        self.rejected(self.selection(item['id']),
                      'selected 2D spectrum', 'requires 1D spectrum')

    def selection(self, identity):
        return {'representation_id': identity, 'formula': 'C22H36O7',
                'formula_evidence': {'kind': 'job_specification'},
                'processing': 'as_stored', 'explanation': 'Explicit selection for SECS.'}

    def rejected(self, selection, *evidence):
        response = self.request('analyse', selection=selection)
        self.assertEqual(response['outcome'], 'input_rejected')
        self.assert_safe_reason(response['reason'])
        for term in evidence:
            self.assertIn(term.lower(), response['reason'].lower())
        self.assertEqual(self.inference.mock_calls, [])
        self.assertEqual(self.candidates.mock_calls, [])

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
            ('peaks.jdx', 'peak_table', '1H', ('selected peak table', 'requires 1H spectrum')),
            ('carbon.jdx', 'spectrum', '13C', ('selected 13C spectrum', 'requires 1H spectrum')),
        ):
            with self.subTest(fixture=fixture):
                self.upload(fixture)
                item = self.one(self.discover(), kind, nucleus)
                self.rejected(self.selection(item['id']), *evidence)

    def test_control_bearing_nucleus_cannot_forge_a_rejection_line(self):
        contents = (FIXTURES / 'carbon.jdx').read_text().replace('^13C', '^13C\tforged')
        self.upload('control-nucleus.jdx', contents=contents)
        item = self.one(self.discover(), nucleus=None)
        self.rejected(self.selection(item['id']), 'requires 1H spectrum')

    def test_raw_fid_cannot_run_as_stored(self):
        self.upload('fid.jdx')
        item = self.one(self.discover(), 'fid')
        self.rejected(self.selection(item['id']), 'selected FID', 'requires processing')

    def test_malformed_fid_parameters_and_channels_make_discovery_partial(self):
        original = (FIXTURES / 'fid.jdx').read_text()
        variants = (
            (original.replace('##$SW=12', '##$SW=not-a-number'), ('SW', 'numeric')),
            (original.replace('##$BF1=400', '##$BF1=nan'), ('BF1', 'finite')),
            (original.split('##DATA TABLE= (X++(I..I)), XYDATA', 1)[0] + '##END=\n',
             ('complex', 'trace')),
        )
        for contents, evidence in variants:
            with self.subTest(evidence=evidence):
                self.upload('malformed-fid.jdx', contents=contents)
                facts = self.discover()
                self.assertFalse(facts['complete'])
                self.assertFalse(any(item['kind'] == 'fid' for item in facts['representations']))
                self.assert_issue_mentions(
                    facts, {'upload_ref': 'upload:sample', 'member': None}, *evidence)

    def test_missing_formula_is_not_inferred_from_an_unselected_structure(self):
        self.upload('proton.jdx')
        self.upload('ethanol.mol', 'upload:structure')
        item = self.one(self.discover())
        selection = self.selection(item['id'])
        del selection['formula']
        self.rejected(selection, 'formula')

    def test_missing_formula_evidence_is_not_replaced_by_explanation_prose(self):
        self.upload('proton.jdx')
        item = self.one(self.discover())
        selection = self.selection(item['id'])
        del selection['formula_evidence']
        self.rejected(selection, 'formula', 'evidence')

    def test_unknown_formula_evidence_representation_is_rejected(self):
        self.upload('proton.jdx')
        item = self.one(self.discover())
        selection = self.selection(item['id'])
        selection['formula_evidence'] = {
            'kind': 'representations', 'representation_ids': ['unissued-structure']}
        self.rejected(selection, 'formula', 'evidence', 'representation')

    def test_structure_formula_evidence_must_match_the_selected_formula(self):
        self.upload('proton.jdx', 'upload:spectrum')
        self.upload('ethanol.mol', 'upload:structure')
        spectrum = self.one(self.discover('upload:spectrum'))
        structure = self.one(self.discover('upload:structure'), 'structure', nucleus=None)
        selection = self.selection(spectrum['id'])
        selection['formula_evidence'] = {
            'kind': 'representations', 'representation_ids': [structure['id']]}
        self.rejected(selection, 'formula', 'evidence', 'C2H6O')

    def test_removed_formula_evidence_representation_is_rejected(self):
        self.upload('proton.jdx', 'upload:spectrum')
        self.upload('ethanol.mol', 'upload:structure')
        spectrum = self.one(self.discover('upload:spectrum'))
        structure = self.one(self.discover('upload:structure'), 'structure', nucleus=None)
        del self.files['upload:structure']
        selection = self.selection(spectrum['id'])
        selection['formula_evidence'] = {
            'kind': 'representations', 'representation_ids': [structure['id']]}
        self.rejected(selection, 'formula', 'evidence', 'representation')

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

    def test_selection_identity_does_not_cross_attempts(self):
        self.upload('proton.jdx')
        item = self.one(self.discover())
        response = self.request('analyse', attempt_ref=OTHER_ATTEMPT_REF, selection=self.selection(item['id']))
        self.assertEqual(response['outcome'], 'input_rejected')
        self.assert_safe_reason(response['reason'])
        self.assertIn('representation', response['reason'].lower())
        self.assertEqual(self.inference.mock_calls, [])
        self.assertEqual(self.candidates.mock_calls, [])
