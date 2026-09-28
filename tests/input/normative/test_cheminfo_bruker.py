"""Real 1D instrument datasets retain their scientific choices and provenance."""

from hashlib import sha256
from itertools import product
import json
import unittest
from zipfile import ZipFile

from input.cheminfo import CORPUS, DATA, CheminfoCase


# Reviewed acquisition/processing declarations, independent of runtime decoding.
PROCESSED = (
    ('aspirin-1h-processed.zip', (('1/pdata/1', '1H', 32768),)),
    ('cyclosporin/cyclosporin_1h.zip', (('cyclosporin_1h/1/pdata/1', '1H', 32768),)),
    ('ibuprofen/processed/carbon.zip', (('2/pdata/1', '13C', 32768),)),
    ('ibuprofen/processed/proton.zip', (('1/pdata/1', '1H', 65536), ('1/pdata/700', '1H', 512))),
    ('rb87.zip', (('rb87/13/pdata/1', '87Rb', 32768),)),
    ('strychnine-1h.zip', (('strychnine/10/pdata/1', '1H', 131072),)),
    ('topspin365.zip', (('topspin365/10/pdata/1', '1H', 65536),
                        ('topspin365/11/pdata/1', '13C', 131072))),
)


class CheminfoDiscoveryTests(CheminfoCase):
    def test_mol_titles_survive_standalone_and_sdf_record_boundaries(self):
        contents = (DATA / 'zipped/ibuprofen/ibuprofen.mol').read_bytes()
        self.assertTrue(contents.startswith(b'\n'))
        for title, separator in product((b'', b'Price $$$$ sample'), (None, b'$$$$\n', b'$$$$\r\n')):
            with self.subTest(title=title, separator=separator):
                molecule = title + contents
                wrapped = molecule if separator is None else (molecule + separator) * 2
                self.upload('structures', contents=wrapped)
                facts = self.discover()
                self.assertTrue(facts['complete'])
                self.assertEqual(facts['issues'], [])
                items = facts['representations']
                count = 1 if separator is None else 2
                self.assertEqual(len(items), count)
                self.assertEqual(len({item['id'] for item in items}), count)
                for item in items:
                    self.assertEqual(item['kind'], 'structure')
                    self.assertEqual(item['metadata']['formula'], 'C13H18O2')

    def test_real_processed_choices_keep_exact_companions_and_metadata(self):
        for path, expected in PROCESSED:
            with self.subTest(archive=path):
                self.fixture('zipped/' + path)
                facts = self.discover()
                self.assertEqual(len(facts['representations']), len(expected))
                ids = set()
                for directory, nucleus, points in expected:
                    sources = [{'upload_ref': 'upload:sample', 'member': f'{directory}/{name}'}
                               for name in ('1r', 'procs')]
                    matches = [item for item in facts['representations'] if item['sources'] == sources]
                    self.assertEqual(len(matches), 1)
                    item = matches[0]
                    ids.add(item['id'])
                    self.assertEqual(item['kind'], 'spectrum')
                    self.assertEqual(item['metadata']['nucleus'], nucleus)
                    self.assertEqual(item['metadata']['dimension'], 1)
                    self.assertEqual(item['metadata']['points'], points)
                    self.assertGreater(item['metadata']['frequency_mhz'], 0)
                self.assertEqual(len(ids), len(expected))

    def test_exact_processed_member_does_not_import_unrelated_raw_fid_issues(self):
        self.fixture('zipped/cyclosporin/cyclosporin_1h.zip')
        for member in ('1r', 'procs'):
            with self.subTest(member=member):
                facts = self.discover(member=f'cyclosporin_1h/1/pdata/1/{member}')
                self.assertTrue(facts['complete'])
                self.assertEqual(facts['issues'], [])
                self.one(facts)


class CheminfoIntegrityTests(unittest.TestCase):
    def test_complete_inventory_retains_recorded_bytes_and_archive_members(self):
        document = json.loads((CORPUS / 'provenance.json').read_text())
        records = document['files']
        paths = [item['path'] for item in records]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(set(paths), {str(path.relative_to(CORPUS))
                         for path in (CORPUS / 'data').rglob('*') if path.is_file()})
        for item in records + document['source_documents']:
            with self.subTest(path=item['path']):
                contents = (CORPUS / item['path']).read_bytes()
                self.assertEqual(len(contents), item['byte_length'])
                self.assertEqual(sha256(contents).hexdigest(), item['sha256'])
                if not item['path'].endswith('.zip'):
                    continue
                with ZipFile(CORPUS / item['path']) as archive:
                    members = item['members']
                    names = [member['path'] for member in members]
                    self.assertEqual(len(names), len(set(names)))
                    self.assertCountEqual(archive.namelist(), names)
                    for member in members:
                        with self.subTest(member=member['path']):
                            contents = archive.read(member['path'])
                            self.assertEqual(len(contents), member['byte_length'])
                            self.assertEqual(sha256(contents).hexdigest(), member['sha256'])

    def test_shared_origin_and_documented_omissions_remain_accounted_for(self):
        document = json.loads((CORPUS / 'provenance.json').read_text())
        source = document['source']
        self.assertTrue(source['repository'].startswith('https://github.com/'))
        self.assertRegex(source['revision'], r'^[0-9a-f]{40}$')
        self.assertEqual(source['licence'], 'MIT')
        self.assertIn(source['copyright'], (CORPUS / source['licence_text']).read_text())
        self.assertTrue(source['basis'].strip())
        recorded = {item['path'] for item in document['files']}
        for omitted in document['omitted_files']:
            self.assertNotIn(omitted['path'], recorded)
            self.assertTrue(omitted['reason'].strip())
        for item in document['files']:
            if 'omitted_members' not in item:
                continue
            members = {member['path'] for member in item['members']}
            for omitted in item['omitted_members']:
                self.assertNotIn(omitted['path'], members)
                self.assertTrue(omitted['reason'].strip())
