#!/usr/bin/env python3
"""Convert an admitted Lightning checkpoint into SECS inference artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tomllib


READ_BYTES = 1024 * 1024
PRECISIONS = ("float32", "float16", "bfloat16")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--scratch-directory", type=Path, required=True)
    parser.add_argument("--weights-output", type=Path, required=True)
    parser.add_argument("--precision", choices=PRECISIONS)
    return parser.parse_args()


def load_spec(path: Path) -> dict:
    with path.open("rb") as source:
        return tomllib.load(source)


def expected_prefixes(spec: dict) -> dict[str, str]:
    model = spec["model"]
    return {
        **{
            f"encoder:{modality}": f"dict_encoders.{modality}."
            for modality in model["encoders"]
        },
        **{
            f"projection_head:{modality}": f"dict_projection_heads.{modality}."
            for modality, config in model["projection_heads"].items()
            if isinstance(config, dict)
        },
    }


def receive_checkpoint(scratch_directory: Path) -> Path:
    checkpoint = scratch_directory / "best_model.ckpt"
    with checkpoint.open("xb") as destination:
        shutil.copyfileobj(sys.stdin.buffer, destination, READ_BYTES)
    return checkpoint


def load_inference_state(checkpoint_path: Path, precision: str, spec: dict) -> dict:
    import torch

    dtypes = {
        "float32": torch.float32,
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
    }
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    state = checkpoint["state_dict"]
    del checkpoint

    prefixes = tuple(expected_prefixes(spec).values())
    unexpected = [
        name
        for name in state
        if name.startswith("model.")
        and not any(
            name == "model." + prefix[:-1] or name.startswith("model." + prefix)
            for prefix in prefixes
        )
    ]
    if unexpected:
        raise ValueError(f"checkpoint contains unexpected state tensor: {unexpected[0]}")

    converted = {}
    for source_name in list(state):
        tensor = state.pop(source_name)
        if not source_name.startswith("model."):
            continue
        name = source_name.removeprefix("model.")
        if not isinstance(tensor, torch.Tensor):
            raise TypeError(f"checkpoint state is not a tensor: {source_name}")
        if tensor.is_complex():
            raise TypeError(f"cannot store complex inference tensor: {source_name}")
        if tensor.is_floating_point():
            tensor = tensor.detach().to(device="cpu", dtype=dtypes[precision], copy=True)
        else:
            tensor = tensor.detach().cpu().clone()
        converted[name] = tensor.contiguous()
    return converted


def convert(args: argparse.Namespace) -> None:
    from safetensors.torch import save_file

    spec = load_spec(args.spec)
    archive_run = Path(spec["archive"]["member"]).parent.name
    if args.run_name != archive_run:
        raise ValueError(
            f"checkpoint specification is under {args.run_name!r}, not archive run {archive_run!r}"
        )
    precision = args.precision or spec["conversion"]["precision"]
    if precision not in PRECISIONS:
        raise ValueError(f"unsupported checkpoint precision: {precision}")

    checkpoint_path = receive_checkpoint(args.scratch_directory)
    state = load_inference_state(checkpoint_path, precision, spec)
    checkpoint_path.unlink()
    save_file(state, args.weights_output)
    print(f"wrote {args.weights_output}")


if __name__ == "__main__":
    convert(arguments())
