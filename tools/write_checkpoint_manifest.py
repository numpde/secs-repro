#!/usr/bin/env python3
"""Write a checkpoint receipt from a read-only safetensors artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import tomllib


READ_BYTES = 1024 * 1024
MAX_SAFETENSORS_HEADER_BYTES = 16 * 1024 * 1024
SAFETENSORS_DTYPES = {"F32": "float32", "F16": "float16", "BF16": "bfloat16"}
NON_FLOATING_SAFETENSORS_DTYPES = {
    "BOOL", "I8", "I16", "I32", "I64", "U8", "U16", "U32", "U64"
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--existing-manifest", type=Path)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--reference-repository", required=True)
    parser.add_argument("--reference-revision", required=True)
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


def safetensors_header(path: Path) -> dict:
    with path.open("rb") as source:
        header_bytes = int.from_bytes(source.read(8), "little")
        if header_bytes > MAX_SAFETENSORS_HEADER_BYTES:
            raise ValueError("safetensors header exceeds the supported size")
        return json.loads(source.read(header_bytes))


def expected_prefixes(spec: dict) -> dict[str, str]:
    model = spec["model"]
    return {
        **{f"encoder:{name}": f"dict_encoders.{name}." for name in model["encoders"]},
        **{
            f"projection_head:{name}": f"dict_projection_heads.{name}."
            for name, config in model["projection_heads"].items()
            if isinstance(config, dict)
        },
    }


def component_inventory(header: dict, spec: dict) -> dict:
    tensor_names = {name for name in header if name != "__metadata__"}
    components = {}
    for component, prefix in expected_prefixes(spec).items():
        names = [name for name in tensor_names if name.startswith(prefix)]
        if not names:
            raise ValueError(f"safetensors has no tensors under {prefix}")
        components[component] = {
            "tensors": len(names),
            "scalar_values": sum(math.prod(header[name]["shape"]) for name in names),
        }

    h_nmr_prefix = expected_prefixes(spec).get("encoder:h_nmr")
    h_nmr_names = {
        name for name in tensor_names if h_nmr_prefix and name.startswith(h_nmr_prefix)
    }
    suffixes = ("running_mean", "running_var", "num_batches_tracked")
    groups = {
        name.rsplit(".", 1)[0]
        for name in h_nmr_names
        if name.rsplit(".", 1)[-1] in suffixes
    }
    if not groups:
        raise ValueError("H-NMR encoder has no BatchNorm buffer groups")
    for group in groups:
        missing = [suffix for suffix in suffixes if f"{group}.{suffix}" not in h_nmr_names]
        if missing:
            raise ValueError(f"incomplete BatchNorm buffers under {group}: {', '.join(missing)}")

    return {
        "tensors": len(tensor_names),
        "components": components,
        "h_nmr_batch_norm_groups": len(groups),
    }


def floating_precision(header: dict) -> str:
    tensor_dtypes = {
        tensor["dtype"] for name, tensor in header.items() if name != "__metadata__"
    }
    unsupported = tensor_dtypes - set(SAFETENSORS_DTYPES) - NON_FLOATING_SAFETENSORS_DTYPES
    if unsupported:
        raise ValueError(f"safetensors has unsupported tensor dtypes: {sorted(unsupported)}")
    precisions = {SAFETENSORS_DTYPES[dtype] for dtype in tensor_dtypes if dtype in SAFETENSORS_DTYPES}
    if len(precisions) != 1:
        raise ValueError(f"safetensors has mixed floating-point precisions: {sorted(precisions)}")
    return precisions.pop()


def archive_receipt(spec: dict) -> dict:
    archive = spec["archive"]
    return {"record": archive["record"], "md5": archive["md5"], "member": archive["member"]}


def manifest_data(args: argparse.Namespace, spec: dict, weights_sha256: str) -> dict:
    header = safetensors_header(args.weights)
    return {
        "spec": {"file": args.spec.name, "sha256": sha256(args.spec)},
        "source": {"archive": archive_receipt(spec)},
        "reference_implementation": {
            "repository": args.reference_repository,
            "revision": args.reference_revision,
        },
        "weights": {
            "file": args.weights.name,
            "bytes": args.weights.stat().st_size,
            "sha256": weights_sha256,
            "precision": floating_precision(header),
            "inventory": component_inventory(header, spec),
        },
    }


def write_manifest(args: argparse.Namespace) -> None:
    spec = load_spec(args.spec)
    weights_sha256 = sha256(args.weights)
    if args.existing_manifest is not None:
        existing = json.loads(args.existing_manifest.read_text())
        if existing["weights"]["sha256"] != weights_sha256:
            raise ValueError("weights do not match the existing provenance receipt")
        if existing["source"]["archive"] != archive_receipt(spec):
            raise ValueError("archive identity changed since the existing provenance receipt")
    args.manifest_output.write_text(
        json.dumps(manifest_data(args, spec, weights_sha256), indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    write_manifest(arguments())
