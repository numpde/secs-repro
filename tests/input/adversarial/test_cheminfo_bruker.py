"""Instrument evidence cannot be repaired by borrowing, guessing or substitution."""

from input.cheminfo import CheminfoCase


class CheminfoRefusalTests(CheminfoCase):
    def test_unqualified_real_raw_profiles_are_localized_beside_processed_choices(self):
        for path, member in (
            ('ibuprofen/fid/proton.zip', '1/acqus'),
            ('ibuprofen/fid/carbon.zip', '2/acqus'),
            ('cyclosporin/cyclosporin_1h.zip', 'cyclosporin_1h/1/acqus'),
            ('topspin450.zip', 'topspin450/20/acqus'),
        ):
            with self.subTest(archive=path):
                self.fixture('zipped/' + path)
                facts = self.discover()
                self.assertFalse(facts['complete'])
                self.assertFalse(any(item['kind'] == 'fid' for item in facts['representations']))
                self.assert_issue_mentions(facts, {'upload_ref': 'upload:sample', 'member': member},
                                          'digital-filter', 'unsupported')

    def test_real_nonproton_spectra_cannot_substitute_an_attached_proton(self):
        self.fixture('zipped/aspirin-1h-processed.zip', 'upload:proton')
        self.one(self.discover('upload:proton'))
        for path, nucleus in (('ibuprofen/processed/carbon.zip', '13C'), ('rb87.zip', '87Rb')):
            with self.subTest(nucleus=nucleus):
                self.fixture('zipped/' + path)
                item = self.one(self.discover(), nucleus=nucleus)
                self.assert_rejected(self.request('analyse', selection=self.selection(item)),
                                     nucleus, 'requires 1H spectrum')

    def test_mutated_real_pair_reports_missing_parameters_or_truncated_data(self):
        members = self.processed_members('zipped/aspirin-1h-processed.zip')
        for mutation in ('missing-procs', 'truncated-1r'):
            with self.subTest(mutation=mutation):
                changed = (members[:1] if mutation == 'missing-procs'
                           else [(members[0][0], members[0][1][:-4]), members[1]])
                self.archive(changed)
                facts = self.discover()
                self.assertFalse(facts['complete'])
                self.assertEqual(facts['representations'], [])
                self.assert_issue_mentions(facts, {'upload_ref': 'upload:sample',
                                          'member': '1/pdata/1/1r'},
                                          *('procs', 'unavailable') if mutation == 'missing-procs'
                                          else ('1r', 'incomplete'))

    def test_split_real_pair_cannot_borrow_a_companion_from_another_upload(self):
        data, parameters = self.processed_members('zipped/aspirin-1h-processed.zip')
        self.archive([data], 'upload:data')
        self.archive([parameters], 'upload:parameters')
        self.discover('upload:parameters')
        facts = self.discover('upload:data')
        self.assertFalse(facts['complete'])
        self.assertEqual(facts['representations'], [])
        self.assert_issue_mentions(facts, {'upload_ref': 'upload:data', 'member': data[0]},
                                  'procs', 'unavailable')

    def test_changed_selected_real_data_invalidates_selection_before_inference(self):
        members = self.processed_members('zipped/aspirin-1h-processed.zip')
        self.archive(members)
        item = self.one(self.discover())
        self.archive([(members[0][0], bytes([members[0][1][0] ^ 1]) + members[0][1][1:]), members[1]])
        self.assert_rejected(self.request('analyse', selection=self.selection(item)), 'changed')
