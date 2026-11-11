"""The cold acquisition bundle must match the pinned SDK release."""

import json
import hashlib
import io
import os
from pathlib import Path
import shutil
import tarfile
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from tools import acquisition_bootstrap as bootstrap


class AcquisitionBootstrapTests(TestCase):
    def test_checked_in_bundle_matches_release_and_locks(self):
        record = json.loads(bootstrap.RECORD.read_text())
        bootstrap.pinned_release(record)
        bootstrap.verify(bootstrap.BUNDLE, bootstrap.inventory(record))

    def test_changed_or_extra_bundle_file_is_rejected(self):
        record = json.loads(bootstrap.RECORD.read_text())
        entries = bootstrap.inventory(record)
        with TemporaryDirectory() as directory:
            snapshot = Path(directory) / 'acquisition'
            shutil.copytree(bootstrap.BUNDLE, snapshot)
            owner = snapshot / 'acquisition_tool/owner.sh'
            owner.chmod(0o664)
            bootstrap.verify(snapshot, entries)
            owner.chmod(0o755)
            with self.assertRaisesRegex(ValueError, 'non-executable regular file'):
                bootstrap.verify(snapshot, entries)
            owner.chmod(0o644)
            (snapshot / 'unexpected').write_text('unexpected')
            (snapshot / 'unexpected').chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'differ from the release inventory'):
                bootstrap.verify(snapshot, entries)
            (snapshot / 'unexpected').unlink()
            (snapshot / 'acquisition_tool/owner.sh').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'differs from its release record'):
                bootstrap.verify(snapshot, entries)

    def test_refresh_replaces_only_after_archive_is_verified(self):
        record = json.loads(bootstrap.RECORD.read_text())
        entries = bootstrap.inventory(record)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / 'bundle.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                for name in entries:
                    archive.add(bootstrap.BUNDLE / name, arcname=name, recursive=False)
            record = {'files': {archive_path.name: hashlib.sha256(archive_path.read_bytes()).hexdigest()}}
            snapshot = root / 'acquisition'
            shutil.copytree(bootstrap.BUNDLE, snapshot)
            old_owner = snapshot / 'acquisition_tool/owner.sh'
            old_owner.write_bytes(b'previous release')
            old_entries = dict(entries)
            old_entries['acquisition_tool/owner.sh'] = {
                'size': len(b'previous release'),
                'sha256': hashlib.sha256(b'previous release').hexdigest(), 'mode': 0o644,
            }
            with (mock.patch.object(bootstrap, 'BUNDLE', snapshot),
                  mock.patch.object(bootstrap, 'previous_inventory', return_value=old_entries)):
                wrong = {'files': {archive_path.name: '0' * 64}}
                with self.assertRaisesRegex(ValueError, 'archive differs'):
                    bootstrap.install(archive_path, wrong, entries, archive_path.name)
                bootstrap.verify(snapshot, old_entries)
                (snapshot / 'unexpected').write_text('preserve me')
                with self.assertRaises(ValueError):
                    bootstrap.install(archive_path, record, entries, archive_path.name)
                self.assertEqual((snapshot / 'unexpected').read_text(), 'preserve me')
                (snapshot / 'unexpected').unlink()
                local_directory = snapshot / 'local-directory'
                local_directory.mkdir()
                with self.assertRaisesRegex(ValueError, 'directories differ'):
                    bootstrap.install(archive_path, record, entries, archive_path.name)
                self.assertTrue(local_directory.is_dir())
                local_directory.rmdir()
                with mock.patch.object(bootstrap, 'exchange_directories', side_effect=OSError('failed')):
                    with self.assertRaises(OSError):
                        bootstrap.install(archive_path, record, entries, archive_path.name)
                bootstrap.verify(snapshot, old_entries)
                previous_umask = os.umask(0o077)
                try:
                    bootstrap.install(archive_path, record, entries, archive_path.name)
                finally:
                    os.umask(previous_umask)
                bootstrap.verify(snapshot, entries)
                for directory in (snapshot, *(path for path in snapshot.rglob('*') if path.is_dir())):
                    self.assertEqual(directory.stat().st_mode & 0o777, 0o755)

    def test_backup_cleanup_failure_reports_the_installed_replacement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'acquisition'
            snapshot.mkdir()
            (snapshot / 'owner.sh').write_bytes(b'old owner\n')
            old_entries = {'owner.sh': {
                'size': 10, 'sha256': hashlib.sha256(b'old owner\n').hexdigest(), 'mode': 0o644,
            }}
            content = b'new owner\n'
            entries = {'owner.sh': {
                'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'mode': 0o644,
            }}
            archive_path = root / 'bundle.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                member = tarfile.TarInfo('owner.sh')
                member.mode, member.size = 0o644, len(content)
                archive.addfile(member, io.BytesIO(content))
            record = {'files': {archive_path.name: hashlib.sha256(archive_path.read_bytes()).hexdigest()}}
            with (
                mock.patch.object(bootstrap, 'BUNDLE', snapshot),
                mock.patch.object(bootstrap, 'previous_inventory', return_value=old_entries),
                mock.patch.object(bootstrap.shutil, 'rmtree', side_effect=OSError('backup cleanup failed')),
                self.assertRaisesRegex(OSError, 'replacement installed; cannot remove previous snapshot') as raised,
            ):
                bootstrap.install(archive_path, record, entries, archive_path.name)
            self.assertEqual('backup cleanup failed', str(raised.exception.__cause__))
            bootstrap.verify(snapshot, entries)
            backups = list(root.glob('.acquisition.*'))
            self.assertEqual(1, len(backups))
            bootstrap.verify(backups[0], old_entries)
