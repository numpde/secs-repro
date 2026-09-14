"""Stopped archival admits bound identity before giving a reader writable state."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch


class JournalArchiveDeploymentTests(TestCase):
    def prepare(self, root):
        for name in ('config/deployments/test', 'secrets/deployments/test/state/journal'):
            (root / name).mkdir(parents=True, mode=0o700)
        state = root / 'secrets/deployments/test'
        state.chmod(0o700)
        for path, content in (
            (state / 'attempt-owner.json', json.dumps({'origin': 'https://api.test', 'provider_ref': 'provider:test'}, sort_keys=True) + '\n'),
            (state / 'provider.signing.private.json', json.dumps({'principal_ref': 'provider:test'})),
            (root / 'config/deployments/test/provider.toml', '[api]\norigin="https://api.test"\n'),
        ):
            path.write_text(content)
            path.chmod(0o600)
        return state

    def archive(self, root):
        from deployment.provider_deployment import archive_deployment_journal
        return archive_deployment_journal(root, 'test', execution_attempt_ref='execution_attempt:sha256:'+'a'*64,
                                          expected_record_digest='sha256:'+'b'*64, reason='Investigated expired Attempt.')

    def test_drifted_owner_or_running_provider_cannot_launch_archiver(self):
        for condition in ('provider', 'origin', 'running', 'missing_binding'):
            with self.subTest(condition=condition), TemporaryDirectory() as directory:
                root = Path(directory)
                state = self.prepare(root)
                project = Mock()
                project.inventory.return_value = {}
                if condition == 'provider':
                    (state / 'provider.signing.private.json').write_text('{"principal_ref":"provider:other"}')
                elif condition == 'origin':
                    (root / 'config/deployments/test/provider.toml').write_text('[api]\norigin="https://other.test"\n')
                elif condition == 'missing_binding':
                    (state / 'attempt-owner.json').unlink()
                else:
                    project.inventory.return_value = {'provider': {'State': {'Running': True}}}
                with patch('deployment.provider_deployment._project', return_value=project):
                    with self.assertRaises((ValueError, RuntimeError, OSError)):
                        self.archive(root)
                project.command.assert_not_called()

    def test_archive_command_mounts_only_required_inputs_and_validates_result(self):
        result = {'schema_id': 'secs.journal_archive_result.v1',
                  'archive_path': '/state/journal/'+'b'*64+'.archive.json',
                  'record_digest': 'sha256:'+'b'*64, 'execution_attempt_ref': 'execution_attempt:sha256:'+'a'*64,
                  'delivery': 'unconfirmed', 'retained': 'Exact work is archived.', 'api_effect': 'Only read.',
                  'automatic': 'Provider stopped.', 'next_actor': 'provider_operator', 'next_action': 'Restart provider.'}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.prepare(root)
            project = Mock()
            project.inventory.return_value = {}
            project.command.return_value = json.dumps(result).encode()
            with patch('deployment.provider_deployment._project', return_value=project), patch('deployment.provider_deployment.render_deployment', return_value={'services': {'provider': {'image': 'sha256:'+'c'*64}}}):
                observed = self.archive(root)
                self.assertEqual(observed['deployment'], 'test')
                args = project.command.call_args.args
                self.assertIn('secs_inference.provider.journal_archive', args)
                self.assertIn(f'type=bind,src={state}/state/journal,dst=/state/journal', args)
                self.assertIn('--read-only', args)
                self.assertIn('--cap-drop', args)
                self.assertNotIn('interpreter.key', ' '.join(args))
                self.assertNotIn('/sources', ' '.join(args))
                project.command.return_value = json.dumps(result | {'body_base64': 'PRIVATE_REPORT_MARKER'}).encode()
                with self.assertRaises(ValueError) as caught:
                    self.archive(root)
                self.assertNotIn('PRIVATE_REPORT_MARKER', str(caught.exception))

    def test_observer_failure_cleans_only_verified_invocation_and_preserves_uncertainty(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign), TemporaryDirectory() as directory:
                root = Path(directory)
                self.prepare(root)
                project = Mock()
                project.inventory.return_value = {}
                identifier = 'd' * 64
                invocation = {}
                def command(*args):
                    if args[0] == 'run':
                        invocation['name'] = args[args.index('--name') + 1]
                        invocation['label'] = args[args.index('--label') + 1]
                        raise RuntimeError('Docker run observation timed out')
                    if args[0] == 'ps':
                        return (identifier + '\n').encode()
                    if args[0] == 'inspect':
                        key, value = invocation['label'].split('=', 1)
                        return json.dumps([{'Id': identifier, 'Name': '/' + invocation['name'],
                                            'Config': {'Labels': {key: 'foreign' if foreign else value}}}]).encode()
                    if args[0] == 'rm':
                        self.assertEqual(args, ('rm', '-f', identifier))
                        return b''
                    self.fail(args)
                project.command.side_effect = command
                with patch('deployment.provider_deployment._project', return_value=project), patch('deployment.provider_deployment.render_deployment', return_value={'services': {'provider': {'image': 'sha256:'+'c'*64}}}):
                    with self.assertRaises(RuntimeError) as caught:
                        self.archive(root)
                message = str(caught.exception)
                self.assertIn('timed out', message)
                self.assertIn('inspect', message.lower())
                self.assertIn('unconfirmed', message.lower())
                removals = [call.args for call in project.command.call_args_list if call.args[0] == 'rm']
                self.assertEqual(len(removals), 0 if foreign else 1)

    def test_public_dispatch_holds_lifecycle_lock_during_archive(self):
        import fcntl
        import os
        from contextlib import redirect_stdout
        from io import StringIO
        from deployment.provider_deployment import main
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            def archive(*args, **kwargs):
                descriptor = os.open(root / 'config/deployments', os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
                self.assertEqual(kwargs['reason'], 'Investigated; literal $value remains text.')
                return {'completed': True}
            with patch('deployment.provider_deployment.__file__', str(root / 'deployment/cli.py')), patch('deployment.provider_deployment._project'), patch('deployment.provider_deployment.archive_deployment_journal', side_effect=archive), redirect_stdout(StringIO()):
                self.assertEqual(main(['archive-closed', 'test', '--execution-attempt-ref', 'execution_attempt:sha256:'+'a'*64,
                                       '--expected-record-digest', 'sha256:'+'b'*64,
                                       '--reason', 'Investigated; literal $value remains text.']), 0)
