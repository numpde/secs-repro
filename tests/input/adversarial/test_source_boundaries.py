"""Untrusted source locators cannot escape Job membership or archive identity."""

import errno
from stat import S_IFIFO, S_IFLNK
from unittest.mock import patch
import warnings
from zipfile import ZipFile, ZipInfo

from input.helpers import FIXTURES, WorkerCase


class SourceBoundaryTests(WorkerCase):
    def rejected(self, response, *terms):
        self.assertEqual(response['outcome'], 'input_rejected')
        for term in terms:
            self.assertIn(term.lower(), response['reason'].lower())
        self.assertNotIn(str(self.root), response['reason'])
        self.assertEqual(self.inference.mock_calls, [])
        self.assertEqual(self.candidates.mock_calls, [])

    def test_foreign_and_path_like_upload_references_are_rejected(self):
        self.upload('proton.jdx')
        for ref in ('upload:other-job', str(FIXTURES / 'proton.jdx'), '../../etc/passwd'):
            with self.subTest(source=ref):
                response = self.request('inspect', source={'upload_ref': ref, 'member': None})
                self.rejected(response, 'Upload', 'Job')

    def test_missing_member_cannot_select_another_member(self):
        self.archive([('valid.jdx', (FIXTURES / 'proton.jdx').read_bytes())])
        response = self.request('inspect', source={'upload_ref': 'upload:sample', 'member': 'missing.jdx'})
        self.rejected(response, 'member', 'missing')

    def test_duplicate_member_names_cannot_select_the_first_entry(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            self.archive([('same.jdx', (FIXTURES / 'proton.jdx').read_bytes()),
                          ('same.jdx', (FIXTURES / 'carbon.jdx').read_bytes())])
        response = self.request('inspect', source={'upload_ref': 'upload:sample', 'member': 'same.jdx'})
        self.rejected(response, 'member', 'repeated')

    def test_nonregular_archive_members_are_rejected(self):
        for mode in (S_IFLNK, S_IFIFO):
            with self.subTest(mode=mode):
                path = self.root / 'special.zip'
                member = ZipInfo('spectrum.jdx')
                member.create_system = 3
                member.external_attr = (mode | 0o600) << 16
                with ZipFile(path, 'w') as archive:
                    archive.writestr(member, b'/etc/passwd')
                self.files['upload:sample'] = str(path)
                response = self.request('inspect', source={'upload_ref': 'upload:sample', 'member': 'spectrum.jdx'})
                self.rejected(response, 'regular file')

    def test_storage_fault_is_not_reported_as_bad_scientific_input(self):
        self.upload('proton.jdx')
        failure = OSError(errno.EIO, 'simulated storage failure')
        with patch('secs_inference.provider.source_access.SourceAccess.open', side_effect=failure):
            with self.assertRaises(OSError) as caught:
                self.request('inspect', source={'upload_ref': 'upload:sample', 'member': None})
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.inference.mock_calls, [])

    def test_archive_limit_does_not_claim_no_matching_spectrum_exists(self):
        self.archive([(f'note-{i}.txt', b'') for i in range(4097)])
        response = self.request('inspect', source={'upload_ref': 'upload:sample', 'member': None})
        self.rejected(response, '4096', 'limit')
        self.assertNotIn('no matching', response['reason'].lower())
