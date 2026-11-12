"""Capture installer build inputs and hash the exact bytes passed to Docker."""

import hashlib
import json
import runpy
import stat


def capture_installer_inputs(stage, repository, dockerfile):
    """Capture the verified SDK installer and Dockerfile before wheel selection."""
    bundle = repository / 'tools/bootstrap/acquisition'
    verify = runpy.run_path(str(bundle / 'acquisition_tool/source_update.py'))['verify']
    entries = verify(bundle)['payload_files']
    captured = stage / 'installer-inputs'
    captured.mkdir()
    sources = [(repository / dockerfile, dockerfile, None)]
    for name, entry in entries.items():
        if name.startswith('acquisition_tool/installer/'):
            sources.append((bundle / name, f'tools/bootstrap/acquisition/{name}', entry))
    result = []
    for source, destination, entry in sources:
        metadata = source.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f'Installer input is not a regular file: {source}')
        data = source.read_bytes()
        if entry is not None:
            # Check the captured bytes too: source may change after tree verification.
            if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise ValueError(f'Installer input differs from its acquisition manifest: {source}')
        target = captured / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        result.append({'path': str(target), 'destination': destination,
                       'sha256': hashlib.sha256(data).hexdigest(), 'executable': False})
    (stage / 'installer-sources.json').write_text(json.dumps(result, sort_keys=True))

