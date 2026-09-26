"""Verify fixture bytes, required provenance fields and reference-parent bindings."""

from hashlib import sha256
import json
import os
from pathlib import Path
import unittest
from zipfile import ZipFile


ROOT = Path('/fixtures/input')
REFERENCE_LOCK = Path('/contracts/upstream/frontend_reference.json')


class FixtureProvenanceTests(unittest.TestCase):
    def assert_origin(self, origin):
        self.assertIn('author', origin)
        author = origin['author']
        self.assertTrue(author is None or isinstance(author, str) and author.strip())
        for field in ('generator', 'licence', 'licence_text', 'basis'):
            self.assertTrue(origin[field].strip(), field)

    def setUp(self):
        self.document = json.loads((ROOT / 'provenance.json').read_text())
        self.records = self.document['files']

    def reference_lock(self):
        contents = REFERENCE_LOCK.read_bytes()
        return json.loads(contents), f"sha256:{sha256(contents).hexdigest()}"

    def test_every_corpus_file_has_one_record(self):
        recorded = [item['path'] for item in self.records]
        actual = {str(path.relative_to(ROOT)) for path in ROOT.rglob('*')
                  if path.is_file() and path != ROOT / 'provenance.json'}
        self.assertEqual(len(recorded), len(set(recorded)), 'Fixture records repeat a path')
        self.assertEqual(set(recorded), actual)

    def test_bytes_and_attribution_match_the_record(self):
        records = {item['path']: item for item in self.records}
        for item in self.records:
            with self.subTest(fixture=item['path']):
                self.assertEqual(sha256((ROOT / item['path']).read_bytes()).hexdigest(), item['sha256'])
                self.assertTrue(item['description'].strip())
                self.assert_origin(item['origin'])
                for parent in item['origin'].get('parents', []):
                    self.assertEqual(parent['sha256'], records[parent['path']]['sha256'])

    def test_generator_is_the_one_that_produced_the_corpus(self):
        sources = self.document['generator_sources']
        self.assertEqual(set(sources), {'tools/generate_input_fixtures.mjs', 'tools/generate_nmrium_fixtures.mjs'})
        for path, digest in sources.items():
            self.assertEqual(sha256((Path('/') / path).read_bytes()).hexdigest(), digest,
                             'Explicitly regenerate after changing a fixture producer')

    def test_native_archive_members_have_complete_provenance(self):
        records = {item['path']: item for item in self.records}
        archives = [item for item in self.records if item['path'].endswith('.zip')]
        self.assertTrue(archives, 'The native NMRium resource archive must remain in the corpus')
        for item in archives:
            with self.subTest(archive=item['path']), ZipFile(ROOT / item['path']) as archive:
                members = item['members']
                self.assertCountEqual(archive.namelist(), [member['path'] for member in members])
                self.assertEqual(len(members), len({member['path'] for member in members}))
                for member in members:
                    self.assertEqual(sha256(archive.read(member['path'])).hexdigest(), member['sha256'])
                    self.assertIn(member['parent'], records)
                    self.assert_origin(member['origin'])

    def test_imported_specimens_retain_the_pinned_source_origin(self):
        reference_lock, _ = self.reference_lock()
        imported = [item for item in self.records if 'source_path' in item['origin'] and 'parent' not in item]
        self.assertTrue(imported, 'The admitted upstream specimen must remain in the corpus')
        for item in imported:
            self.assertEqual(item['origin']['source_repository'], reference_lock['repository'])
            self.assertEqual(item['origin']['source_revision'], reference_lock['revision'])

    def test_reference_vectors_name_their_exact_parent(self):
        _, reference_lock_id = self.reference_lock()
        self.assertEqual(self.document['reference_lock'], reference_lock_id)
        self.assertEqual(self.document['reference_build'], os.environ['INPUT_REFERENCE_BUILD_ID'])
        self.assertNotIn('frontend_revision', self.document)
        self.assertNotIn('reference_sources', self.document)
        records = {item['path']: item for item in self.records}
        references = [item for item in self.records if item['path'].endswith('.reference.json')]
        self.assertTrue(references, 'No scientific references were recorded')
        for item in references:
            with self.subTest(reference=item['path']):
                reference = json.loads((ROOT / item['path']).read_text())
                self.assertEqual(reference['input_sha256'], records[item['parent']]['sha256'])
                self.assertEqual(reference['reference_lock'], reference_lock_id)
                self.assertEqual(reference['reference_build'], os.environ['INPUT_REFERENCE_BUILD_ID'])
                self.assertNotIn('frontend_revision', reference)
                self.assertEqual(len(reference['intensities']), 10000)
