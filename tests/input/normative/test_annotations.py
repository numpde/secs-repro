"""Structures and supplied annotations retain their evidence and exact relationships."""

from input.helpers import FIXTURES, WorkerCase
from support_evidence import qualification_evidence


class AnnotationTests(WorkerCase):
    def test_smiles_archive_records_are_separate_structure_choices(self):
        self.archive([('sample/structures.smi', (FIXTURES / 'structures.smi').read_bytes())])
        facts = self.discover()
        self.assertTrue(facts['complete'])
        choices = facts['representations']
        self.assertEqual(len(choices), 2)
        self.assertEqual(len({item['id'] for item in choices}), 2)
        self.assertCountEqual([item['metadata']['formula'] for item in choices], ['C2H6O', 'C2H4O2'])
        for item in choices:
            self.assertEqual(item['kind'], 'structure')
            self.assertEqual(item['sources'], [{'upload_ref': 'upload:sample', 'member': 'sample/structures.smi'}])

    @qualification_evidence("input.nmredata.relationships.v1")
    def test_nmredata_preserves_declared_counts_assignments_and_relationships(self):
        self.archive([(f'sample/{name}', (FIXTURES / name).read_bytes())
                      for name in ('annotations.sdf', 'proton.jdx', 'ethanol.mol')])
        facts = self.discover()
        self.assertTrue(facts['complete'])
        spectrum = self.one(facts)
        sources = [{'upload_ref': 'upload:sample', 'member': f'sample/{name}'}
                   for name in ('annotations.sdf', 'proton.jdx')]
        self.assertCountEqual(spectrum['sources'], sources)
        structures = [item for item in facts['representations'] if item['kind'] == 'structure']
        self.assertEqual(len(structures), 2)
        linked = [item for item in structures if sources[0] in item['sources']]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]['metadata']['formula'], 'C2H6O')
        self.assertIn(linked[0]['id'], spectrum['related_ids'])
        unrelated = next(item for item in structures if item['id'] != linked[0]['id'])
        self.assertNotIn(unrelated['id'], spectrum['related_ids'])
        annotations = sorted(spectrum['metadata']['annotations'], key=lambda item: item['shift'])
        self.assertEqual(len(annotations), 2)
        for annotation, shift, count, label, atom in zip(annotations, (2.03125, 7), (3, 2), ('a', 'b'), ('H1', 'H2')):
            self.assertEqual(annotation['shift'], shift)
            self.assertEqual(annotation['multiplicity'], 's')
            self.assertEqual(annotation['atom_count'], count)
            self.assertEqual(annotation['assignment']['label'], label)
            self.assertEqual(annotation['assignment']['atoms'], [atom])
            self.assertIsNone(annotation.get('integral'), 'Declared atom counts are not measured integrals')

    def test_exact_nmredata_member_resolves_its_declared_spectrum(self):
        self.archive([(f'sample/{name}', (FIXTURES / name).read_bytes())
                      for name in ('annotations.sdf', 'proton.jdx')])
        facts = self.discover(member='sample/annotations.sdf')
        self.assertTrue(facts['complete'])
        spectrum = self.one(facts)
        self.assertEqual({source['member'] for source in spectrum['sources']},
                         {'sample/annotations.sdf', 'sample/proton.jdx'})

    def test_unavailable_annotation_resource_preserves_structure_and_reports_the_gap(self):
        self.upload('annotations.sdf')
        facts = self.discover()
        self.assertFalse(facts['complete'])
        self.assertEqual(self.one(facts, 'structure', nucleus=None)['metadata']['formula'], 'C2H6O')
        self.assert_issue_mentions(
            facts, {'upload_ref': 'upload:sample', 'member': None},
            'unavailable', 'proton.jdx')
