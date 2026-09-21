"""Companion names do not grant permission to merge experiments or Uploads."""

from input.helpers import FIXTURES, WorkerCase


class VendorCompanionTests(WorkerCase):
    def test_truncated_jeol_does_not_turn_into_a_complete_inventory(self):
        self.upload('truncated.jdf', contents=(FIXTURES / 'synthetic.jdf').read_bytes()[:4096])
        facts = self.discover()
        self.assertFalse(facts['complete'])
        issues = [issue for issue in facts['issues']
                  if issue['source'] == {'upload_ref': 'upload:sample', 'member': None}]
        self.assertTrue(issues)
        self.assertRegex(' '.join(issue['reason'] for issue in issues).lower(),
                         r'truncat|incomplete|missing.*(data|point)|expect.*(data|point|byte)')

    def missing_procs(self, facts, source):
        self.assertFalse(facts['complete'])
        issues = [issue for issue in facts['issues'] if issue['source'] == source]
        self.assertTrue(issues, 'The incomplete data source needs its own missing-companion issue')
        self.assertRegex(' '.join(issue['reason'] for issue in issues).lower(),
                         r'(missing|not found|need|required|without).*procs|procs.*(missing|not found|need|required)')

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
        issues = [issue for issue in facts['issues']
                  if issue['source'] == {'upload_ref': 'upload:sample', 'member': 'sample/fid'}]
        self.assertTrue(issues)
        self.assertRegex(' '.join(issue['reason'] for issue in issues).lower(),
                         r'truncat|incomplete|missing.*(data|point)|expect.*(data|point|byte)')
