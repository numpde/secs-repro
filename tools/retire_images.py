#!/usr/bin/env python3
"""Retire obsolete provider images from the selected engine and canonical checkout."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import tempfile

from deployment.provider_deployment import _private_directory, _provider_image_ref, _worker_image_ref
from deployment.templates import _locked_parent

ROOT = Path(__file__).resolve().parent.parent
STAGE_ROOT = Path("/tmp")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
INPUT_ID = re.compile(r"[0-9a-f]{64}")
LABELS = {
    "provider": "io.secs-repro.provider.input-id",
    "cpu": "io.secs-repro.packages.input-id",
    "gpu": "io.secs-repro.packages.input-id",
}


class Refused(Exception):
    pass


def docker(*args):
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            result = subprocess.run(
                [str(ROOT / "tools/docker.sh"), *args], stdout=output, stderr=error, timeout=30,
                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024)),
            )
            output.seek(0)
            raw = output.read(16 * 1024 * 1024 + 1)
    except subprocess.TimeoutExpired as error:
        if args[:2] == ("image", "rm"):
            raise Refused("Image removal timed out; its outcome is unconfirmed. Inspect images before retrying.") from error
        raise Refused("Docker inventory timed out; image retirement is incomplete.") from error
    except OSError as error:
        raise Refused("Docker could not start; image retirement is incomplete.") from error
    if result.returncode:
        raise Refused("Docker rejected an image-retirement operation; earlier removals are not rolled back.")
    if len(raw) >= 16 * 1024 * 1024:
        raise Refused("Docker returned too much image metadata.")
    try:
        return raw.decode("utf-8")
    except UnicodeError as error:
        raise Refused("Docker returned invalid image metadata.") from error


def ids(raw):
    values = raw.splitlines()
    if any(not IMAGE_ID.fullmatch(value) for value in values):
        raise Refused("Docker returned an incomplete image inventory.")
    return values


def inspect_images(batch):
    try:
        values = json.loads(docker("image", "inspect", *batch))
    except ValueError as error:
        raise Refused("Docker returned invalid image metadata.") from error
    if not isinstance(values, list) or len(values) != len(batch) or {v.get("Id") for v in values if isinstance(v, dict)} != set(batch):
        raise Refused("Docker returned incomplete image metadata.")
    return values


def facts(image):
    config = image.get("Config")
    if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
        raise Refused("An owned image has incomplete labels.")
    labels = config["Labels"]
    if "RepoTags" not in image or "RepoDigests" not in image:
        raise Refused("An owned image has incomplete alias metadata.")
    tags, digests = image["RepoTags"], image["RepoDigests"]
    if any(value is not None and (not isinstance(value, list) or any(not isinstance(v, str) for v in value)) for value in (tags, digests)):
        raise Refused("An owned image has incomplete alias metadata.")
    tags, digests = tags or [], digests or []
    provider, package = labels.get(LABELS["provider"]), labels.get(LABELS["cpu"])
    if (provider is None) == (package is None):
        return None
    role = "provider" if provider is not None else labels.get("io.secs-repro.packages.variant")
    input_id = provider if provider is not None else package
    if (provider is None and role not in ("cpu", "gpu")) or not isinstance(input_id, str) or not INPUT_ID.fullmatch(input_id):
        return None
    try:
        created = datetime.datetime.fromisoformat(image["Created"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError
    except (KeyError, ValueError, AttributeError):
        raise Refused("An owned image has an invalid creation time.") from None
    return role, input_id, tags, digests, created


def select(images, protected, now):
    current = {role: set() for role in LABELS}
    admitted = []
    for image in images:
        value = facts(image)
        if value is None:
            continue
        role, input_id, tags, digests, created = value
        local = f"secs-repro/{'provider' if role == 'provider' else 'packages-' + role}:local"
        if local in tags:
            current[role].add(input_id)
        admitted.append((image["Id"], role, input_id, tags, digests, created))
    selected = []
    for identifier, role, input_id, tags, digests, created in admitted:
        reference = f"secs-repro/{'provider' if role == 'provider' else 'packages-' + role}:inputs-{input_id}"
        if (identifier in protected or not current[role] or input_id in current[role] or digests
                or tags not in ([], [reference]) or now - created < datetime.timedelta(hours=24)):
            continue
        selected.append(identifier)
    return selected


def pins(parent):
    result = set()
    for directory in sorted(parent.iterdir()):
        _private_directory(directory)
        path = directory / "deployment.env"
        for value in (_provider_image_ref(path), _worker_image_ref(path)):
            if value:
                result.add(value)
    return result


def staged_images():
    result = set()
    for pattern in ("secs-provider-inputs.*", "secs-package-inputs.*"):
        for stage in STAGE_ROOT.glob(pattern):
            if stage.is_symlink() or not stage.is_dir() or stage.stat().st_uid != os.getuid():
                continue
            receipt = stage / "image-id"
            if not receipt.exists() and not receipt.is_symlink():
                continue
            if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 80:
                raise Refused("A retained image build receipt is unreadable or invalid.")
            value = receipt.read_text(encoding="ascii").strip()
            if value:
                if not IMAGE_ID.fullmatch(value):
                    raise Refused("A retained image build receipt has an invalid identity.")
                result.add(value)
    return result


def retire():
    parent = ROOT / "config" / "deployments"
    _private_directory(parent)
    with _locked_parent(parent):
        protected = pins(parent) | staged_images()
        containers = docker("ps", "--all", "--quiet", "--no-trunc").splitlines()
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in containers):
            raise Refused("Docker returned an incomplete container inventory.")
        for offset in range(0, len(containers), 64):
            batch = containers[offset:offset + 64]
            found = ids(docker("inspect", "--type", "container", "--format", "{{.Image}}", *batch))
            if len(found) != len(batch):
                raise Refused("Docker returned incomplete container image references.")
            protected.update(found)
        owned = set()
        for label in set(LABELS.values()):
            owned.update(ids(docker("image", "ls", "--all", "--quiet", "--no-trunc", "--filter", "label=" + label)))
        images = []
        owned = sorted(owned)
        for offset in range(0, len(owned), 64):
            images.extend(inspect_images(owned[offset:offset + 64]))
        now = datetime.datetime.now(datetime.timezone.utc)
        selected = select(images, protected, now)
        removed = 0
        for identifier in selected:
            if docker("ps", "--all", "--quiet", "--no-trunc", "--filter", "ancestor=" + identifier).strip():
                continue
            refreshed = inspect_images([identifier])[0]
            # Include the original census so current input identities remain protected.
            others = [image for image in images if image["Id"] != identifier]
            if identifier not in select(others + [refreshed], protected, now):
                continue
            docker("image", "rm", "--no-prune", identifier)
            removed += 1
        print(f"Retired {removed} obsolete provider and package images; saved pins and container images were preserved.")


def main():
    try:
        fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused("Image work is busy; retry after it finishes.") from None
            retire()
        finally:
            os.close(fd)
    except (Refused, OSError, ValueError, UnicodeError) as error:
        print(f"Cannot complete image retirement: {error}. Retry make packages/images/retire; earlier removals are not rolled back.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
