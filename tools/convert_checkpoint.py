#!/usr/bin/env python3
"""Convert a verified Lightning checkpoint stream into SECS inference artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import torch
from safetensors.torch import save_file


READ_BYTES = 1024 * 1024
DTYPES = {
    "float32": torch.float32,
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch-directory", type=Path, required=True)
    parser.add_argument("--weights-output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--precision", choices=DTYPES, required=True)
    parser.add_argument("--source-record", required=True)
    parser.add_argument("--source-archive-md5", required=True)
    parser.add_argument("--source-member", required=True)
    parser.add_argument("--implementation-repository", required=True)
    parser.add_argument("--implementation-revision", required=True)
    return parser.parse_args()


def receive_checkpoint(scratch_directory: Path) -> Path:
    checkpoint = scratch_directory / "best_model.ckpt"
    with checkpoint.open("xb") as destination:
        shutil.copyfileobj(sys.stdin.buffer, destination, READ_BYTES)
    return checkpoint


def inference_state(checkpoint_path: Path, precision: str) -> dict[str, torch.Tensor]:
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    state = checkpoint["state_dict"]
    del checkpoint

    converted = {}
    dtype = DTYPES[precision]
    for source_name in list(state):
        tensor = state.pop(source_name)
        if not isinstance(tensor, torch.Tensor) or not source_name.startswith("model."):
            continue
        name = source_name.removeprefix("model.")
        if tensor.is_complex():
            raise TypeError(f"cannot store complex inference tensor: {source_name}")
        if tensor.is_floating_point():
            tensor = tensor.detach().to(device="cpu", dtype=dtype, copy=True)
        else:
            tensor = tensor.detach().cpu().clone()
        converted[name] = tensor.contiguous()
    if not converted:
        raise ValueError("checkpoint state_dict has no model.* tensors")
    return converted


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(READ_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_outputs(args: argparse.Namespace) -> None:
    checkpoint_path = receive_checkpoint(args.scratch_directory)
    state = inference_state(checkpoint_path, args.precision)
    checkpoint_path.unlink()

    with tempfile.TemporaryDirectory(
        dir=args.weights_output.parent, prefix=".secs-v3."
    ) as stage_name:
        stage = Path(stage_name)
        weights = stage / args.weights_output.name
        model = stage / args.model_output.name
        manifest = stage / args.manifest_output.name

        save_file(state, weights, metadata={"precision": args.precision})
        shutil.copyfile(args.model_config, model)
        manifest.write_text(
            json.dumps(
                {
                    "weights": {
                        "file": weights.name,
                        "bytes": weights.stat().st_size,
                        "sha256": sha256(weights),
                        "precision": args.precision,
                    },
                    "model": {"file": model.name, "sha256": sha256(model)},
                    "source": {
                        "archive": {
                            "record": args.source_record,
                            "md5": args.source_archive_md5,
                            "member": args.source_member,
                        },
                        "implementation": {
                            "repository": args.implementation_repository,
                            "revision": args.implementation_revision,
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        for artifact, output in (
            (weights, args.weights_output),
            (model, args.model_output),
            (manifest, args.manifest_output),
        ):
            artifact.chmod(0o444)
            os.link(artifact, output)

    print(
        "published "
        + ", ".join(
            output.name
            for output in (
                args.weights_output,
                args.model_output,
                args.manifest_output,
            )
        )
    )


if __name__ == "__main__":
    prepare_outputs(arguments())
