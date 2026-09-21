"""Packaging changes source locators, not which scientific data exist."""

from input.helpers import FIXTURES, WorkerCase
from pathlib import Path


class UploadCombinationTests(WorkerCase):
    def test_same_direct_filename_does_not_merge_upload_bytes(self):
        for ref, fixture in (('upload:carbon', 'carbon.jdx'), ('upload:proton', 'proton.jdx')):
            self.upload('spectrum.jdx', ref, contents=(FIXTURES / fixture).read_bytes())
        self.assertNotEqual(self.files['upload:carbon'], self.files['upload:proton'])
        self.assertEqual(Path(self.files['upload:carbon']).read_bytes(), (FIXTURES / 'carbon.jdx').read_bytes())
        self.assertEqual(Path(self.files['upload:proton']).read_bytes(), (FIXTURES / 'proton.jdx').read_bytes())
        self.one(self.discover('upload:carbon'), nucleus='13C')
        self.one(self.discover('upload:proton'))

    def test_archive_exposes_every_experiment(self):
        self.archive([('sample/1/spectrum.jdx', (FIXTURES / 'carbon.jdx').read_bytes()),
                      ('sample/2/spectrum.jdx', (FIXTURES / 'proton.jdx').read_bytes())])
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 2)
        proton = self.one(facts)
        self.assertEqual(proton['sources'], [{'upload_ref': 'upload:sample', 'member': 'sample/2/spectrum.jdx'}])

    def test_exact_archive_member_can_be_discovered_separately(self):
        self.archive([('experiment/spectrum.jdx', (FIXTURES / 'linked.jdx').read_bytes())])
        facts = self.discover(member='experiment/spectrum.jdx')
        self.assertEqual(len(facts['representations']), 2)
        for item in facts['representations']:
            self.assertEqual(item['sources'], [{'upload_ref': 'upload:sample', 'member': 'experiment/spectrum.jdx'}])

    def test_same_member_names_in_two_uploads_keep_separate_identities(self):
        for ref, fixture in (('upload:carbon', 'carbon.jdx'), ('upload:proton', 'proton.jdx')):
            self.archive([('spectrum.jdx', (FIXTURES / fixture).read_bytes())], ref)
        first, second = self.discover('upload:carbon'), self.discover('upload:proton')
        carbon, proton = self.one(first, nucleus='13C'), self.one(second)
        self.assertNotEqual(carbon['id'], proton['id'])
        self.assertEqual(proton['sources'][0]['upload_ref'], 'upload:proton')

    def test_duplicate_bytes_do_not_rewrite_the_selected_upload(self):
        self.upload('proton.jdx', 'upload:first')
        self.upload('proton.jdx', 'upload:second')
        for ref in ('upload:first', 'upload:second'):
            with self.subTest(upload=ref):
                item = self.one(self.discover(ref))
                self.assertEqual(item['sources'], [{'upload_ref': ref, 'member': None}])

    def test_unrelated_text_does_not_erase_a_valid_experiment(self):
        self.archive([('notes.txt', b'Laboratory notes'), ('proton.jdx', (FIXTURES / 'proton.jdx').read_bytes())])
        self.one(self.discover())

    def test_archive_order_does_not_choose_an_experiment(self):
        members = [('a.jdx', (FIXTURES / 'proton.jdx').read_bytes()),
                   ('b.jdx', (FIXTURES / 'carbon.jdx').read_bytes())]
        for order in (members, list(reversed(members))):
            with self.subTest(order=[item[0] for item in order]):
                self.archive(order)
                facts = self.discover()
                self.assertEqual({item['metadata']['nucleus'] for item in facts['representations']}, {'1H', '13C'})
