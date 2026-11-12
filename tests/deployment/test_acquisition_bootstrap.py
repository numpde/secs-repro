"""Consumer wiring and captured installer bytes retain the source manifest contract."""

import hashlib
import json
from pathlib import Path
import runpy
import shutil
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from tools import installer_inputs

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / 'tools/bootstrap/acquisition'
UPDATER = runpy.run_path(str(BUNDLE / 'acquisition_tool/source_update.py'))


class AcquisitionBootstrapTests(TestCase):
    def test_checked_in_bundle_matches_source_pin(self):
        manifest = UPDATER['verify'](BUNDLE)
        pin = UPDATER['read_pin'](ROOT / 'requirements/provider-tools-source.json')
        self.assertEqual(manifest['release_version'], pin['version'])

    def test_capture_hashes_the_bytes_passed_to_docker(self):
        with TemporaryDirectory() as directory:
            stage = Path(directory)
            installer_inputs.capture_installer_inputs(stage, ROOT, 'containers/provider/Dockerfile')
            entries = json.loads((stage / 'installer-sources.json').read_text())
            self.assertTrue(any(entry['destination'].endswith('/installer/tools/installer.py') for entry in entries))
            for entry in entries:
                self.assertEqual(hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest(), entry['sha256'])

    def test_capture_rejects_an_edit_after_initial_tree_verification(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / 'repository'
            bundle = repository / 'tools/bootstrap/acquisition'
            shutil.copytree(BUNDLE, bundle)
            dockerfile = repository / 'containers/provider/Dockerfile'
            dockerfile.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / 'containers/provider/Dockerfile', dockerfile)
            stage = root / 'stage'
            stage.mkdir()

            def verify_then_edit(source):
                manifest = UPDATER['verify'](source)
                target = source / 'acquisition_tool/installer/tools/installer.py'
                target.write_bytes(target.read_bytes() + b'\n# concurrent edit\n')
                return manifest

            with mock.patch.object(installer_inputs.runpy, 'run_path', return_value={'verify': verify_then_edit}):
                with self.assertRaisesRegex(ValueError, 'differs from its acquisition manifest'):
                    installer_inputs.capture_installer_inputs(stage, repository, 'containers/provider/Dockerfile')
