"""Companion names do not grant permission to merge experiments or Uploads."""

import struct

from input.helpers import FIXTURES, WorkerCase


class VendorCompanionTests(WorkerCase):
    def test_truncated_jeol_does_not_turn_into_a_complete_inventory(self):
        self.upload('truncated.jdf', contents=(FIXTURES / 'synthetic.jdf').read_bytes()[:4096])
        facts = self.discover()
        self.assertFalse(facts['complete'])
        self.assert_issue_mentions(
            facts, {'upload_ref': 'upload:sample', 'member': None},
            'incomplete', 'spectrum data')

    def test_jeol_dimension_and_axis_unit_are_read_from_the_file(self):
        for offset, value, evidence in ((12, 2, '2D'), (33, 13, 'not in ppm')):
            with self.subTest(offset=offset):
                contents = bytearray((FIXTURES / 'synthetic.jdf').read_bytes())
                contents[offset] = value
                self.upload('unsupported.jdf', contents=bytes(contents))
                facts = self.discover()
                self.assertFalse(facts['complete'])
                self.assertEqual(facts['representations'], [])
                self.assert_issue_mentions(
                    facts, {'upload_ref': 'upload:sample', 'member': None}, evidence)

    def test_unqualified_vendor_fid_processing_profiles_are_reported(self):
        cases = (
            ('acqus', (FIXTURES / 'bruker-acqus.txt').read_text().replace('##$GRPDLY= 0', '##$GRPDLY= 44.75'),
             'digital-filter profile'),
            ('procpar', (FIXTURES / 'varian-procpar.txt').read_text().replace('\n1 2000\n', '\n1 2200\n', 1),
             'chemical-shift reference profile'),
        )
        for parameter, text, evidence in cases:
            with self.subTest(parameter=parameter):
                vendor = 'bruker' if parameter == 'acqus' else 'varian'
                self.archive([
                    ('experiment/fid', (FIXTURES / f'{vendor}-fid.bin').read_bytes()),
                    (f'experiment/{parameter}', text.encode()),
                ])
                facts = self.discover()
                self.assertFalse(facts['complete'])
                self.assertEqual(facts['representations'], [])
                self.assertTrue(any(evidence in issue['reason'] for issue in facts['issues']))
                self.files.clear()

    def test_nonfinite_or_zero_vendor_metadata_is_a_localized_input_issue(self):
        cases = (
            ('processed-bruker', [('experiment/1r', (FIXTURES / 'bruker-1r.bin').read_bytes()),
                                  ('experiment/procs', (FIXTURES / 'bruker-procs.txt').read_text().replace('##$SF= 400', '##$SF= nan').encode())]),
            ('bruker-fid', [('experiment/fid', (FIXTURES / 'bruker-fid.bin').read_bytes()),
                            ('experiment/acqus', (FIXTURES / 'bruker-acqus.txt').read_text().replace('##$SFO1= 400', '##$SFO1= 0').encode())]),
            ('varian-fid', [('experiment/fid', (FIXTURES / 'varian-fid.bin').read_bytes()),
                            ('experiment/procpar', (FIXTURES / 'varian-procpar.txt').read_text().replace('\n1 400\n', '\n1 nan\n', 1).encode())]),
        )
        for name, members in cases:
            with self.subTest(name=name):
                self.archive(members)
                facts = self.discover()
                self.assertFalse(facts['complete'])
                self.assertEqual(facts['representations'], [])
                self.assertTrue(any('malformed or incomplete' in issue['reason']
                                    for issue in facts['issues']))
                self.files.clear()

    def test_malformed_processed_bruker_data_is_partial_at_discovery(self):
        parameters = (FIXTURES / 'bruker-procs.txt').read_text()
        cases = (
            ('empty', b'', parameters),
            ('truncated', (FIXTURES / 'bruker-1r.bin').read_bytes()[:-1], parameters),
            ('nonfinite', struct.pack('<2d', float('nan'), 1.0),
             parameters.replace('##$SI= 64', '##$SI= 2').replace('##$DTYPP= 0', '##$DTYPP= 2')),
        )
        for name, data, procs in cases:
            with self.subTest(name=name):
                self.archive([('experiment/1r', data), ('experiment/procs', procs.encode())])
                facts = self.discover()
                self.assertFalse(facts['complete'])
                self.assertEqual(facts['representations'], [])
                self.assert_issue_mentions(
                    facts, {'upload_ref': 'upload:sample', 'member': 'experiment/1r'},
                    '1r', 'malformed or incomplete')
                self.files.clear()

    def missing_procs(self, facts, source):
        self.assertFalse(facts['complete'])
        self.assert_issue_mentions(facts, source, 'unavailable', 'procs')

    def test_separate_uploads_do_not_supply_implicit_bruker_companions(self):
        self.archive([('sample/pdata/1/1r', (FIXTURES / 'bruker-1r.bin').read_bytes())], 'upload:data')
        self.archive([('sample/pdata/1/procs', (FIXTURES / 'bruker-procs.txt').read_bytes())], 'upload:parameters')
        for ref in ('upload:data', 'upload:parameters'):
            with self.subTest(upload=ref):
                facts = self.discover(ref)
                for item in facts['representations']:
                    self.assertTrue(all(source['upload_ref'] == ref for source in item['sources']))
        facts = self.discover('upload:data')
        self.missing_procs(facts, {'upload_ref': 'upload:data', 'member': 'sample/pdata/1/1r'})

    def test_other_experiment_parameters_do_not_complete_a_bruker_pair(self):
        self.archive([('sample/1/pdata/1/1r', (FIXTURES / 'bruker-1r.bin').read_bytes()),
                      ('sample/2/pdata/1/procs', (FIXTURES / 'bruker-procs.txt').read_bytes())])
        facts = self.discover()
        for item in facts['representations']:
            experiments = {source['member'].split('/')[1] for source in item['sources']}
            self.assertLessEqual(len(experiments), 1, 'A representation combined separate experiments')
        self.missing_procs(facts, {'upload_ref': 'upload:sample', 'member': 'sample/1/pdata/1/1r'})

    def test_truncated_varian_data_reports_the_affected_member(self):
        self.archive([('sample/fid', (FIXTURES / 'varian-fid.bin').read_bytes()[:60]),
                      ('sample/procpar', (FIXTURES / 'varian-procpar.txt').read_bytes())])
        facts = self.discover()
        self.assertFalse(facts['complete'])
        self.assert_issue_mentions(
            facts, {'upload_ref': 'upload:sample', 'member': 'sample/fid'},
            'incomplete', 'FID data')

    def test_truncated_bruker_and_malformed_vendor_parameters_are_partial(self):
        cases = (
            ('bruker', 'acqus', (FIXTURES / 'bruker-fid.bin').read_bytes()[:16],
             (FIXTURES / 'bruker-acqus.txt').read_bytes(), 'incomplete FID data'),
            ('bruker', 'acqus', (FIXTURES / 'bruker-fid.bin').read_bytes(), b'bad acqus', 'acqus'),
            ('varian', 'procpar', (FIXTURES / 'varian-fid.bin').read_bytes(), b'bad procpar', 'procpar'),
        )
        for vendor, parameter, fid, parameters, evidence in cases:
            with self.subTest(vendor=vendor, evidence=evidence):
                self.archive([('sample/fid', fid), (f'sample/{parameter}', parameters)])
                facts = self.discover()
                self.assertFalse(facts['complete'])
                self.assertTrue(any(evidence.lower() in issue['reason'].lower()
                                    for issue in facts['issues']))
                self.files.clear()
