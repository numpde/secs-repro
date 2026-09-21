"""Arrange uploaded bytes and observe production boundaries; never decode them."""

from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
import unittest
from unittest.mock import Mock
from zipfile import ZipFile

from secs_inference.provider.worker_model import ScientificHandler, ScientificWorkerConfig


FIXTURES = Path('/fixtures/input')
ATTEMPT_REF = 'execution_attempt:sha256:' + '1' * 64
OTHER_ATTEMPT_REF = 'execution_attempt:sha256:' + '2' * 64


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

    def request(self, operation, *, attempt_ref=ATTEMPT_REF, **arguments):
        return self.worker({'operation': operation, 'files': dict(self.files),
                            'directory': str(self.root), 'attempt_ref': attempt_ref, **arguments})

    def discover(self, ref='upload:sample', member=None):
        response = self.request('inspect', source={'upload_ref': ref, 'member': member})
        self.assertEqual(response['outcome'], 'inspected')
        facts = response['facts']
        self.assertTrue('representations' in facts,
            'Discovery must expose representations; current facts: ' + ', '.join(facts))
        self.assertIsInstance(facts['representations'], list)
        self.assertIs(type(facts.get('complete')), bool, 'Discovery must disclose whether its inventory is complete')
        self.assertIsInstance(facts.get('issues'), list)
        for issue in facts['issues']:
            source = issue.get('source')
            self.assertIsInstance(source, dict)
            self.assertEqual(set(source), {'upload_ref', 'member'})
            self.assertIsInstance(source['upload_ref'], str)
            self.assertTrue(source['upload_ref'].strip())
            self.assertEqual(source['upload_ref'], ref)
            self.assertTrue(source['member'] is None or isinstance(source['member'], str))
            if isinstance(source['member'], str):
                self.assertTrue(source['member'].strip())
            self.assert_safe_reason(issue.get('reason'))
        self.assertEqual(self.inference.mock_calls, [], 'Discovery started inference')
        self.assertEqual(self.candidates.mock_calls, [], 'Discovery started retrieval')
        return facts

    def assert_safe_reason(self, reason):
        self.assertIsInstance(reason, str)
        self.assertTrue(reason.strip(), 'A diagnostic reason must explain the blocked operation')
        self.assertTrue(reason.isprintable(), 'A diagnostic reason must not contain control characters')
        self.assertNotIn(str(self.root), reason, 'A diagnostic reason exposed the private workspace path')
        self.assertNotIn(str(FIXTURES), reason, 'A diagnostic reason exposed the internal fixture path')
        for path in self.files.values():
            self.assertNotIn(path, reason, 'A diagnostic reason exposed an acquired source path')

    def assert_issue_mentions(self, facts, source, *evidence):
        issues = [issue for issue in facts['issues'] if issue['source'] == source]
        self.assertTrue(issues, f'No issue was attributed to {source!r}')
        self.assertTrue(any(all(term.lower() in issue['reason'].lower() for term in evidence)
                            for issue in issues),
                        f'One attributed issue must name {evidence!r}')

    def one(self, facts, kind='spectrum', nucleus='1H'):
        matches = [item for item in facts['representations'] if item['kind'] == kind
                   and (nucleus is None or item['metadata'].get('nucleus') == nucleus)]
        self.assertEqual(len(matches), 1, f'Expected one {nucleus} {kind}, found {len(matches)}')
        return matches[0]
