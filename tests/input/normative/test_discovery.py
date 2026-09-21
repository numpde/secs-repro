"""Every representation remains visible before an analysis-specific choice."""

from input.helpers import FIXTURES, WorkerCase


class DiscoveryTests(WorkerCase):
    def test_two_dimensional_metadata_are_preserved_in_discovery(self):
        self.upload('synthetic-2d.jdx')
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 1)
        item = self.one(facts, nucleus=None)
        self.assertEqual(item['metadata']['dimension'], 2)
        self.assertEqual(item['metadata']['nucleus'], ['1H', '1H'])

    def test_jcamp_encodings_retain_scientific_metadata(self):
        names = ['proton.jdx', 'ascending.jdx', 'complex.jdx']
        names += [f'encoded-{encoding}.jdx' for encoding in ('fix', 'sqz', 'dif', 'difdup', 'pac')]
        for name in names:
            with self.subTest(fixture=name):
                self.upload(name)
                facts = self.discover()
                item = self.one(facts)
                self.assertTrue(facts['complete'])
                self.assertEqual(facts['issues'], [])
                self.assertEqual(item['metadata']['dimension'], 1)
                self.assertEqual(item['metadata']['points'], 257)
                self.assertEqual(float(item['metadata']['frequency_mhz']), 400)
                self.assertEqual(item['sources'], [{'upload_ref': 'upload:sample', 'member': None}])

    def test_complex_channels_are_one_representation(self):
        self.upload('complex.jdx')
        self.assertEqual(len(self.discover()['representations']), 1)

    def test_linked_spectrum_and_peak_table_keep_their_relationship(self):
        self.upload('linked.jdx')
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 2)
        spectrum, peaks = self.one(facts), self.one(facts, 'peak_table')
        self.assertNotEqual(spectrum['id'], peaks['id'])
        self.assertIn(spectrum['id'], peaks['related_ids'])
        self.assertEqual(peaks['metadata']['columns'], ['shift', 'height', 'width'])
        self.assertNotIn('integral', peaks['metadata']['columns'])
        self.assertNotIn('assignment', peaks['metadata']['columns'])

    def test_peak_table_is_discoverable_without_a_dense_spectrum(self):
        self.upload('peaks.jdx')
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 1)
        self.assertEqual(self.one(facts, 'peak_table')['metadata']['peaks'], 2)

    def test_carbon_is_discovered_without_being_relabelled_proton(self):
        self.upload('carbon.jdx')
        item = self.one(self.discover(), nucleus='13C')
        self.assertEqual(item['metadata']['dimension'], 1)

    def test_fid_is_identified_before_processing(self):
        self.upload('fid.jdx')
        item = self.one(self.discover(), 'fid')
        self.assertEqual(item['metadata']['points'], 512)

    def test_nmrium_retains_both_nuclei(self):
        self.upload('mixed.nmrium')
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 2)
        self.one(facts, nucleus='1H')
        self.one(facts, nucleus='13C')

    def test_structure_files_supply_formula_evidence_not_spectra(self):
        for name in ('ethanol.mol', 'ethanol.sdf'):
            with self.subTest(fixture=name):
                self.upload(name)
                facts = self.discover()
                item = self.one(facts, 'structure', nucleus=None)
                self.assertEqual(item['metadata']['formula'], 'C2H6O')
                self.assertFalse(any(item['kind'] == 'spectrum' for item in facts['representations']))

    def test_multiple_sdf_records_remain_distinct_choices(self):
        self.upload('two.sdf', contents=(FIXTURES / 'ethanol.sdf').read_bytes() * 2)
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 2)
        self.assertEqual(len({item['id'] for item in facts['representations']}), 2)

    def test_supported_content_is_identified_without_a_trusted_extension(self):
        cases = [
            ('proton.jdx', [('spectrum', '1H', 257)]),
            ('synthetic.jdf', [('spectrum', '1H', 64)]),
            ('mixed.nmrium', [('spectrum', '1H', 257), ('spectrum', '13C', 257)]),
            ('ethanol.mol', [('structure', None, None)]),
            ('ethanol.sdf', [('structure', None, None)]),
        ]
        for fixture, expected in cases:
            for name in (fixture.upper(), 'unknown', 'misleading.fid'):
                with self.subTest(fixture=fixture, filename=name):
                    self.upload(name, contents=(FIXTURES / fixture).read_bytes())
                    facts = self.discover()
                    self.assertEqual(len(facts['representations']), len(expected))
                    for kind, nucleus, points in expected:
                        item = self.one(facts, kind, nucleus)
                        self.assertEqual(item['sources'], [{'upload_ref': 'upload:sample', 'member': None}])
                        if kind == 'structure':
                            self.assertEqual(item['metadata']['formula'], 'C2H6O')
                        else:
                            self.assertEqual(item['metadata']['points'], points)
