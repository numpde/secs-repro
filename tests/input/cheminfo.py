"""Upload retained Cheminfo bytes; runtime wrappers inherit the corpus license."""

from pathlib import Path
from zipfile import ZipFile

import numpy as np

from input.helpers import WorkerCase


CORPUS = Path('/fixtures/bruker/cheminfo')
DATA = CORPUS / 'data'
COFFEE = (
    'UV1009_M1-1003-1002_6268712_73uEjPg4XR',
    'UV1010_M1-1003-1002_6268756_ErISKLIoeB',
)


class CheminfoCase(WorkerCase):
    def fixture(self, path, ref='upload:sample'):
        self.files[ref] = str(DATA / path)
        return ref

    def coffee(self, name, ref='upload:sample'):
        directory = DATA / 'flat/coffee' / name
        return self.archive(((str(path.relative_to(directory.parent)), path.read_bytes())
                             for path in sorted(directory.rglob('*')) if path.is_file()), ref)

    def processed_members(self, path, directory='1/pdata/1'):
        with ZipFile(DATA / path) as archive:
            return [(f'{directory}/{name}', archive.read(f'{directory}/{name}'))
                    for name in ('1r', 'procs')]

    def selection(self, item, *, processing='as_stored', formula='C22H36O7'):
        return {'representation_id': item['id'], 'formula': formula,
                'formula_evidence': {'kind': 'job_specification', 'quote': formula},
                'processing': processing, 'explanation': 'Use the explicitly selected uploaded data.'}

    def assert_prepared(self, response, item, *, from_fid=False):
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        preparation = response['analysis']['preparation']
        self.assertEqual(preparation['representation_id'], item['id'])
        self.assertEqual(preparation['sources'], item['sources'])
        self.assertIs(preparation['from_fid'], from_fid)
        self.assertIs(type(preparation['magnitude']), bool)
        if not from_fid:
            self.assertIs(preparation['magnitude'], False)
        self.inference.embed_spectrum.assert_called_once()
        values = self.inference.embed_spectrum.call_args.args[0]
        self.assertEqual(values.shape, (10000,))
        self.assertEqual(values.dtype, np.float32)
        self.assertTrue(np.isfinite(values).all())
        self.assertAlmostEqual(float(values.min()), 0.)
        self.assertAlmostEqual(float(values.max()), 1.)
        self.assertGreater(np.count_nonzero(values), 1)
        self.candidates.propose.assert_called_once()

    def assert_rejected(self, response, *keywords):
        self.assertEqual(response['outcome'], 'input_rejected')
        self.assert_safe_reason(response['reason'])
        for keyword in keywords:
            self.assertIn(keyword.lower(), response['reason'].lower())
        self.assertEqual(self.inference.mock_calls, [])
        self.assertEqual(self.candidates.mock_calls, [])
