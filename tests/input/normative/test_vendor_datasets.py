"""Vendor companions retain their experiment paths and processing alternatives."""

from input.helpers import FIXTURES, WorkerCase


class VendorDatasetTests(WorkerCase):
    def test_processed_jeol_is_discovered_directly(self):
        self.upload('synthetic.jdf')
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 1)
        item = self.one(facts)
        self.assertEqual(item['metadata']['points'], 64)
        self.assertEqual(item['metadata']['dimension'], 1)
        self.assertEqual(float(item['metadata']['frequency_mhz']), 400)
        self.assertEqual(item['sources'], [{'upload_ref': 'upload:sample', 'member': None}])

    def processed(self, prefix='sample/1/pdata/1'):
        return [(f'{prefix}/1r', (FIXTURES / 'bruker-1r.bin').read_bytes()),
                (f'{prefix}/procs', (FIXTURES / 'bruker-procs.txt').read_bytes())]

    def test_processed_bruker_pair_remains_supported_without_acqus(self):
        self.archive(self.processed())
        facts = self.discover()
        self.assertTrue(facts['complete'])
        self.assertEqual(facts['issues'], [])
        item = self.one(facts)
        self.assertEqual(item['metadata']['points'], 64)
        self.assertEqual(item['metadata']['dimension'], 1)
        self.assertEqual(float(item['metadata']['frequency_mhz']), 400)
        self.assertEqual({source['member'] for source in item['sources']},
                         {'sample/1/pdata/1/1r', 'sample/1/pdata/1/procs'})

    def test_exact_bruker_members_resolve_only_their_declared_companions(self):
        members = self.processed() + [('unrelated.txt', b'not part of the dataset')]
        self.archive(members)
        for selected in ('sample/1/pdata/1/1r', 'sample/1/pdata/1/procs'):
            with self.subTest(selected=selected):
                item = self.one(self.discover(member=selected))
                self.assertEqual({source['member'] for source in item['sources']},
                                 {'sample/1/pdata/1/1r', 'sample/1/pdata/1/procs'})

    def test_complete_processed_bruker_dataset_is_one_choice(self):
        members = self.processed() + [('sample/1/acqus', (FIXTURES / 'bruker-acqus.txt').read_bytes())]
        self.archive(members)
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 1)
        item = self.one(facts)
        self.assertEqual(item['metadata']['points'], 64)
        sources = {source['member'] for source in item['sources']}
        self.assertTrue({'sample/1/pdata/1/1r', 'sample/1/pdata/1/procs'} <= sources)
        self.assertTrue(sources <= {name for name, _ in members})

    def test_raw_bruker_and_varian_are_discovered_before_processing(self):
        for vendor, parameter in (('bruker', 'acqus'), ('varian', 'procpar')):
            with self.subTest(vendor=vendor):
                members = [('experiment/fid', (FIXTURES / f'{vendor}-fid.bin').read_bytes()),
                           (f'experiment/{parameter}', (FIXTURES / f'{vendor}-{parameter}.txt').read_bytes())]
                self.archive(members)
                facts = self.discover()
                self.assertEqual(len(facts['representations']), 1)
                item = self.one(facts, 'fid')
                self.assertEqual(item['metadata']['points'], 64)
                self.assertEqual(item['metadata']['dimension'], 1)
                self.assertEqual(float(item['metadata']['frequency_mhz']), 400)
                self.assertEqual({source['member'] for source in item['sources']}, {name for name, _ in members})
                for selected, _ in members:
                    with self.subTest(vendor=vendor, selected=selected):
                        exact = self.one(self.discover(member=selected), 'fid')
                        self.assertEqual({source['member'] for source in exact['sources']},
                                         {name for name, _ in members})

    def test_raw_and_processed_files_do_not_hide_each_other(self):
        members = self.processed() + [
            ('sample/1/acqus', (FIXTURES / 'bruker-acqus.txt').read_bytes()),
            ('sample/1/fid', (FIXTURES / 'bruker-fid.bin').read_bytes())]
        self.archive(members)
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 2)
        self.one(facts, 'fid')
        self.one(facts, 'spectrum')

    def test_multiple_pdata_directories_remain_separate_choices(self):
        self.archive(self.processed('sample/1/pdata/1') + self.processed('sample/1/pdata/2'))
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 2)
        self.assertEqual(len({item['id'] for item in facts['representations']}), 2)
        pairs = {frozenset(source['member'] for source in item['sources'])
                 for item in facts['representations']}
        self.assertEqual(pairs, {
            frozenset({'sample/1/pdata/1/1r', 'sample/1/pdata/1/procs'}),
            frozenset({'sample/1/pdata/2/1r', 'sample/1/pdata/2/procs'}),
        })
