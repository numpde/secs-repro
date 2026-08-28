#!/usr/bin/env python3
"""Create SECS weights and their reproducible artifact receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib


READ_BYTES = 1024 * 1024
MAX_SAFETENSORS_HEADER_BYTES = 16 * 1024 * 1024
PRECISIONS = ("float32", "float16", "bfloat16")


def add_provenance_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--implementation-repository", required=True)
    parser.add_argument("--implementation-revision", required=True)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    operations = parser.add_subparsers(dest="operation", required=True)

    convert = operations.add_parser("convert")
    add_provenance_arguments(convert)
    convert.add_argument("--scratch-directory", type=Path, required=True)
    convert.add_argument("--weights-output", type=Path, required=True)
    convert.add_argument("--manifest-output", type=Path, required=True)
    convert.add_argument("--precision", choices=PRECISIONS)

    manifest = operations.add_parser("manifest")
    add_provenance_arguments(manifest)
    manifest.add_argument("--weights", type=Path, required=True)
    manifest.add_argument("--manifest-output", type=Path, required=True)
    return parser.parse_args()


def load_spec(path: Path) -> dict:
    with path.open("rb") as source:
        return tomllib.load(source)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(READ_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def safetensors_precision(path: Path) -> str:
    with path.open("rb") as source:
        header_bytes = int.from_bytes(source.read(8), "little")
        if header_bytes > MAX_SAFETENSORS_HEADER_BYTES:
            raise ValueError("safetensors header exceeds the supported size")
        header = json.loads(source.read(header_bytes))
    return header["__metadata__"]["precision"]


def manifest_data(
    weights: Path,
    spec_path: Path,
    spec: dict,
    repository: str,
    revision: str,
) -> dict:
    archive = spec["archive"]
    return {
        "spec": {"file": spec_path.name, "sha256": sha256(spec_path)},
        "source": {
            "archive": {
                "record": archive["record"],
                "md5": archive["md5"],
                "member": archive["member"],
            },
            "implementation": {"repository": repository, "revision": revision},
        },
        "weights": {
            "file": weights.name,
            "bytes": weights.stat().st_size,
            "sha256": sha256(weights),
            "precision": safetensors_precision(weights),
        },
    }


def write_manifest(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def receive_checkpoint(scratch_directory: Path) -> Path:
    checkpoint = scratch_directory / "best_model.ckpt"
    with checkpoint.open("xb") as destination:
        shutil.copyfileobj(sys.stdin.buffer, destination, READ_BYTES)
    return checkpoint


def inference_state(checkpoint_path: Path, precision: str) -> dict:
    import torch

    dtypes = {
        "float32": torch.float32,
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
    }
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    state = checkpoint["state_dict"]
    del checkpoint

    converted = {}
    for source_name in list(state):
        tensor = state.pop(source_name)
        if not isinstance(tensor, torch.Tensor) or not source_name.startswith("model."):
            continue
        name = source_name.removeprefix("model.")
        if tensor.is_complex():
            raise TypeError(f"cannot store complex inference tensor: {source_name}")
        if tensor.is_floating_point():
            tensor = tensor.detach().to(device="cpu", dtype=dtypes[precision], copy=True)
        else:
            tensor = tensor.detach().cpu().clone()
        converted[name] = tensor.contiguous()
    if not converted:
        raise ValueError("checkpoint state_dict has no model.* tensors")
    return converted


def convert(args: argparse.Namespace) -> None:
    from safetensors.torch import save_file

    spec = load_spec(args.spec)
    precision = args.precision or spec["conversion"]["precision"]
    if precision not in PRECISIONS:
        raise ValueError(f"unsupported checkpoint precision: {precision}")

    checkpoint_path = receive_checkpoint(args.scratch_directory)
    state = inference_state(checkpoint_path, precision)
    checkpoint_path.unlink()

    with tempfile.TemporaryDirectory(
        dir=args.weights_output.parent, prefix=".secs-v3."
    ) as stage_name:
        stage = Path(stage_name)
        weights = stage / args.weights_output.name
        manifest = stage / args.manifest_output.name
        save_file(state, weights, metadata={"precision": precision})
        write_manifest(
            manifest,
            manifest_data(
                weights,
                args.spec,
                spec,
                args.implementation_repository,
                args.implementation_revision,
            ),
        )
        for artifact, output in (
            (weights, args.weights_output),
            (manifest, args.manifest_output),
        ):
            artifact.chmod(0o444)
            os.link(artifact, output)

    print(f"published {args.weights_output.name}, {args.manifest_output.name}")


def refresh_manifest(args: argparse.Namespace) -> None:
    write_manifest(
        args.manifest_output,
        manifest_data(
            args.weights,
            args.spec,
            load_spec(args.spec),
            args.implementation_repository,
            args.implementation_revision,
        ),
    )


def main() -> None:
    args = arguments()
    if args.operation == "convert":
        convert(args)
    else:
        refresh_manifest(args)


if __name__ == "__main__":
    main()
