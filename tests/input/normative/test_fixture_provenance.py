"""Verify fixture bytes, required provenance fields and reference-parent bindings."""

from hashlib import sha256
import json
from pathlib import Path
import unittest


ROOT = Path('/fixtures/input')


class FixtureProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads((ROOT / 'provenance.json').read_text())
        self.records = self.document['files']

    def test_every_corpus_file_has_one_record(self):
        recorded = [item['path'] for item in self.records]
        actual = {str(path.relative_to(ROOT)) for path in ROOT.rglob('*')
                  if path.is_file() and path != ROOT / 'provenance.json'}
        self.assertEqual(len(recorded), len(set(recorded)), 'Fixture records repeat a path')
        self.assertEqual(set(recorded), actual)

    def test_bytes_and_attribution_match_the_record(self):
        for item in self.records:
            with self.subTest(fixture=item['path']):
                self.assertEqual(sha256((ROOT / item['path']).read_bytes()).hexdigest(), item['sha256'])
                self.assertTrue(item['description'].strip())
                for field in ('generator', 'author', 'licence', 'licence_text', 'basis'):
                    self.assertTrue(item['origin'][field].strip(), field)

    def test_generator_is_the_one_that_produced_the_corpus(self):
        self.assertEqual(sha256(Path('/generator.mjs').read_bytes()).hexdigest(),
                         self.document['generator_sha256'], 'Explicitly regenerate after changing the fixture producer')

    def test_reference_vectors_name_their_exact_parent(self):
        records = {item['path']: item for item in self.records}
        references = [item for item in self.records if item['path'].endswith('.reference.json')]
        self.assertTrue(references, 'No scientific references were recorded')
        for item in references:
            with self.subTest(reference=item['path']):
                reference = json.loads((ROOT / item['path']).read_text())
                self.assertEqual(reference['input_sha256'], records[item['parent']]['sha256'])
                self.assertEqual(reference['frontend_revision'], self.document['frontend_revision'])
                self.assertEqual(len(reference['intensities']), 10000)
