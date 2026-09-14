"""The deployment command exposes offline inspection under existing ownership."""
import json
import os
import fcntl
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from io import StringIO
from unittest import TestCase
from unittest.mock import Mock, patch
from deployment.provider_deployment import main

class JournalInspectDeploymentTests(TestCase):
    def test_real_dispatch_selects_restricted_installed_image(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for path in ('config/deployments/test','secrets/deployments/test','secrets/deployments/test/state/journal'):
                (root/path).mkdir(parents=True,exist_ok=True,mode=0o700)
            binding=root/'secrets/deployments/test/attempt-owner.json'
            binding.write_text(json.dumps({'provider_ref':'provider:test','origin':'https://api.test'}));binding.chmod(0o600)
            project=Mock()
            project.inventory.return_value={}
            def inspect_while_locked(*args):
                descriptor=os.open(root/'config/deployments',os.O_RDONLY|os.O_DIRECTORY)
                try:
                    with self.assertRaises(BlockingIOError): fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally: os.close(descriptor)
                return json.dumps({'schema_id':'secs.journal_inspection.v1','record_count':0,'observed_at':'2026-09-14T00:00:00+00:00','current_automation':'stopped','records':[]}).encode()
            project.command.side_effect=inspect_while_locked
            output=StringIO()
            with patch('deployment.provider_deployment.__file__',str(root/'deployment/cli.py')), patch('deployment.provider_deployment._project',return_value=project), patch('deployment.provider_deployment.render_deployment',return_value={'services':{'provider':{'image':'sha256:'+'a'*64}}}), redirect_stdout(output):
                self.assertEqual(main(['inspect','test']),0)
            self.assertEqual(json.loads(output.getvalue())['deployment'],'test')
            args=project.command.call_args.args
            self.assertEqual(args[0],'run')
            for value in ('none','--read-only','never','secs_inference.provider.journal_inspect'):
                self.assertIn(value,args)
            self.assertIn(f'type=bind,src={root}/secrets/deployments/test/state/journal,dst=/state/journal,readonly',args)
            self.assertNotIn('provider.signing.private.json',' '.join(args))

    def test_running_or_foreign_project_cannot_start_inspection(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);(root/'config/deployments/test').mkdir(parents=True,mode=0o700)
            for foreign in (False,True):
                project=Mock()
                project.inventory.return_value={'provider':{'State':{'Running':True}}}
                if foreign: project.inventory.side_effect=ValueError('Foreign ownership')
                with patch('deployment.provider_deployment.__file__',str(root/'deployment/cli.py')), patch('deployment.provider_deployment._project',return_value=project):
                    self.assertEqual(main(['inspect','test']),1)
                project.command.assert_not_called()

    def test_malformed_or_body_bearing_image_output_is_rejected_without_forwarding(self):
        from contextlib import redirect_stderr
        base={'schema_id':'secs.journal_inspection.v1','record_count':0,'observed_at':'2026-09-14T00:00:00+00:00','current_automation':'stopped','records':[]}
        for document in (base|{'records':[None],'record_count':1}, base|{'body_base64':'PRIVATE_REPORT_MARKER'}):
            with self.subTest(document=document),TemporaryDirectory() as directory:
                root=Path(directory)
                for path in ('config/deployments/test','secrets/deployments/test','secrets/deployments/test/state/journal'):
                    (root/path).mkdir(parents=True,exist_ok=True,mode=0o700)
                binding=root/'secrets/deployments/test/attempt-owner.json'
                binding.write_text(json.dumps({'provider_ref':'provider:test','origin':'https://api.test'}));binding.chmod(0o600)
                project=Mock();project.inventory.return_value={};project.command.return_value=json.dumps(document).encode()
                output=StringIO();errors=StringIO()
                with patch('deployment.provider_deployment.__file__',str(root/'deployment/cli.py')),patch('deployment.provider_deployment._project',return_value=project),patch('deployment.provider_deployment.render_deployment',return_value={'services':{'provider':{'image':'sha256:'+'a'*64}}}),redirect_stdout(output),redirect_stderr(errors):
                    self.assertEqual(main(['inspect','test']),1)
                self.assertEqual(output.getvalue(),'')
                self.assertNotIn('PRIVATE_REPORT_MARKER',errors.getvalue())

    def test_malformed_owner_binding_is_controlled_before_image_launch(self):
        from contextlib import redirect_stderr
        for value in (None, [], "private-binding-marker", 42):
            with self.subTest(value=value), TemporaryDirectory() as directory:
                root = Path(directory)
                for path in ('config/deployments/test', 'secrets/deployments/test/state/journal'):
                    (root / path).mkdir(parents=True, mode=0o700)
                (root / 'secrets/deployments/test').chmod(0o700)
                binding = root / 'secrets/deployments/test/attempt-owner.json'
                binding.write_text(json.dumps(value))
                binding.chmod(0o600)
                project = Mock()
                project.inventory.return_value = {}
                output, errors = StringIO(), StringIO()
                with patch('deployment.provider_deployment.__file__', str(root / 'deployment/cli.py')), patch('deployment.provider_deployment._project', return_value=project), redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(main(['inspect', 'test']), 1)
                project.command.assert_not_called()
                self.assertEqual(output.getvalue(), '')
                self.assertNotIn('private-binding-marker', errors.getvalue())
