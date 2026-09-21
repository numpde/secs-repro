"""Companion names do not grant permission to merge experiments or Uploads."""

from input.helpers import FIXTURES, WorkerCase


class VendorCompanionTests(WorkerCase):
    def test_truncated_jeol_does_not_turn_into_a_complete_inventory(self):
        self.upload('truncated.jdf', contents=(FIXTURES / 'synthetic.jdf').read_bytes()[:4096])
        facts = self.discover()
        self.assertFalse(facts['complete'])
        self.assert_issue_mentions(
            facts, {'upload_ref': 'upload:sample', 'member': None},
            'incomplete', 'spectrum data')

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
