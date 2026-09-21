"""Arrange uploaded bytes and observe production boundaries; never decode them."""

from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
import unittest
from unittest.mock import Mock
from zipfile import ZipFile

from secs_inference.provider.worker_model import ScientificHandler, ScientificWorkerConfig


FIXTURES = Path('/fixtures/input')


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.workspace = TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.files = {}
        self.inference = Mock()
        self.candidates = Mock()
        self.worker = ScientificHandler(self.inference, self.candidates,
            ScientificWorkerConfig('unused', 'unused', device='cpu'))

    def upload(self, name, ref='upload:sample', *, contents=None):
        if contents is None:
            path = FIXTURES / name
        else:
            path = Path(mkdtemp(dir=self.root, prefix='upload-')) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents.encode() if isinstance(contents, str) else contents)
        self.files[ref] = str(path)
        return ref

    def archive(self, members, ref='upload:sample'):
        path = self.root / f'archive-{len(self.files)}.zip'
        with ZipFile(path, 'w') as archive:
            for name, contents in members:
                archive.writestr(name, contents)
        self.files[ref] = str(path)
        return ref

    def request(self, operation, **arguments):
        return self.worker({'operation': operation, 'files': dict(self.files),
                            'directory': str(self.root), **arguments})

    def discover(self, ref='upload:sample', member=None):
        response = self.request('inspect', source={'upload_ref': ref, 'member': member})
        self.assertEqual(response['outcome'], 'inspected')
        facts = response['facts']
        self.assertTrue('representations' in facts,
            'Discovery must expose representations; current facts: ' + ', '.join(facts))
        self.assertIsInstance(facts['representations'], list)
        self.assertIs(type(facts.get('complete')), bool, 'Discovery must disclose whether its inventory is complete')
        self.assertIsInstance(facts.get('issues'), list)
        self.assertEqual(self.inference.mock_calls, [], 'Discovery started inference')
        self.assertEqual(self.candidates.mock_calls, [], 'Discovery started retrieval')
        return facts

    def one(self, facts, kind='spectrum', nucleus='1H'):
        matches = [item for item in facts['representations'] if item['kind'] == kind
                   and (nucleus is None or item['metadata'].get('nucleus') == nucleus)]
        self.assertEqual(len(matches), 1, f'Expected one {nucleus} {kind}, found {len(matches)}')
        return matches[0]
