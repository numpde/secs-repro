#!/usr/bin/env python3
"""Qualify one fixed-shape TensorRT path without changing production inference."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import time
import traceback
import tomllib

import numpy as np
import polars as pl
import torch
from torch import nn

from secs_inference.model import SecsInference


SAMPLE_ROWS = 8192
MODEL_BATCH_ROWS = 256
OUTER_BATCH_ROWS = 8192
SAMPLE_SEED = 20_260_830
TRIALS = 10
TARGET_BUILD_SPEEDUP = 10.0
PERFORMANCE_SAFETY_FACTOR = 1.1
MIN_ROW_COSINE = 0.99999
MAX_NORMALIZED_L2 = 0.005
MAX_SCORE_DELTA = 0.0001


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("base-reference", "probe"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--checkpoint-manifest", type=Path, required=True)
    parser.add_argument("--candidate-spec", type=Path, required=True)
    parser.add_argument("--molformer-lock", type=Path, required=True)
    parser.add_argument("--base-cache", type=Path, required=True)
    parser.add_argument("--derived-cache", type=Path, required=True)
    parser.add_argument("--fixed-model-code", type=Path, required=True)
    parser.add_argument("--base-reference", type=Path, required=True)
    parser.add_argument("--base-reference-report", type=Path, required=True)
    parser.add_argument("--frontend-spectrum", type=Path, required=True)
    parser.add_argument("--dependency-manifest", type=Path, required=True)
    parser.add_argument("--wheelhouse-manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--package-image-id", required=True)
    parser.add_argument("--host-gpu-index", required=True)
    parser.add_argument("--host-gpu-uuid", required=True)
    parser.add_argument("--repository-revision", required=True)
    parser.add_argument("--launcher-sha256", required=True)
    parser.add_argument("--gpu-monitor-interval-seconds", type=float, required=True)
    return parser.parse_args()


def admitted_molformer_source(args: argparse.Namespace) -> dict:
    admission = json.loads(args.dependency_manifest.read_text())
    matches = [
        artifact
        for artifact in admission["artifacts"]
        if artifact["role"] == "molformer-source"
    ]
    require(len(matches) == 1, "The dependency manifest must admit one MolFormer source file.")
    artifact = matches[0]
    actual = sha256(args.fixed_model_code)
    require(actual == artifact["sha256"], f"The fixed MolFormer source has unexpected SHA-256 {actual}.")
    return artifact


def prepare_derived_cache(args: argparse.Namespace, artifact: dict) -> Path:
    shutil.copytree(args.base_cache / "hub", args.derived_cache / "hub")
    snapshots = list((args.derived_cache / "hub").glob("models--*/snapshots/*"))
    require(len(snapshots) == 1, f"Expected one cached MolFormer snapshot, found {snapshots!r}.")
    destination = snapshots[0] / "modeling_molformer.py"
    destination.unlink()
    shutil.copyfile(args.fixed_model_code, destination)
    require(
        sha256(destination) == artifact["sha256"],
        "The derived MolFormer source changed while copying it.",
    )
    return destination


def candidate_configuration(path: Path) -> dict:
    with path.open("rb") as source:
        specification = tomllib.load(source)
    model_batch_rows = specification["embedding"]["batch_size"]
    outer_batch_rows = specification["table"]["batch_rows"]
    require(
        model_batch_rows == MODEL_BATCH_ROWS,
        f"This fixed-shape probe requires embedding.batch_size={MODEL_BATCH_ROWS}; got {model_batch_rows}.",
    )
    require(
        outer_batch_rows == OUTER_BATCH_ROWS,
        f"This probe requires table.batch_rows={OUTER_BATCH_ROWS}; got {outer_batch_rows}.",
    )
    return specification


def source_smiles(
    path: Path,
    candidate_specification: dict,
) -> tuple[list[str], list[str], list[str], dict]:
    table = pl.scan_parquet(path).select("smiles")
    statistics_row = table.select(
        pl.len().alias("rows"),
        pl.col("smiles").null_count().alias("nulls"),
    ).collect(engine="streaming").row(0, named=True)
    rows = int(statistics_row["rows"])
    require(rows >= SAMPLE_ROWS, f"Expected at least {SAMPLE_ROWS:,} source rows, got {rows:,}.")
    require(statistics_row["nulls"] == 0, "The source SMILES column contains null values.")

    # One seeded row from each equal-width source stratum covers the whole
    # production table without pretending that the first 8,192 rows are typical.
    edges = np.linspace(0, rows, SAMPLE_ROWS + 1, dtype=np.int64)
    rng = np.random.default_rng(SAMPLE_SEED)
    sample_positions = np.asarray(
        [rng.integers(edges[index], edges[index + 1]) for index in range(SAMPLE_ROWS)],
        dtype=np.int64,
    )
    outer_tail_rows = rows % OUTER_BATCH_ROWS
    require(outer_tail_rows > 0, "The admitted source has no partial final outer batch to prove.")
    tail_positions = np.arange(rows - outer_tail_rows, rows, dtype=np.int64)
    training_rows = min(candidate_specification["index"]["training_rows"], rows)
    training_seed = candidate_specification["index"]["training_seed"]
    # Reproduce the builder's seeded strata exactly, then retain only its last
    # partial model batch. Full training batches already share the sampled
    # fixed-shape path; this is the distinct eager fallback the build will use.
    training_edges = np.arange(training_rows + 1, dtype=np.int64) * rows // training_rows
    training_widths = np.diff(training_edges)
    training_offsets = np.floor(
        np.random.default_rng(training_seed).random(training_rows) * training_widths
    ).astype(np.int64)
    training_positions = training_edges[:-1] + training_offsets
    training_residual_rows = training_rows % MODEL_BATCH_ROWS
    require(training_residual_rows > 0, "The configured training sample has no model-batch residual.")
    training_tail_positions = training_positions[-training_residual_rows:]
    wanted = np.unique(
        np.concatenate((sample_positions, tail_positions, training_tail_positions))
    )
    selected: dict[int, str] = {}
    row_offset = 0
    for batch in table.collect_batches(
        chunk_size=OUTER_BATCH_ROWS,
        maintain_order=True,
        engine="streaming",
    ):
        batch_end = row_offset + batch.height
        first = np.searchsorted(wanted, row_offset, side="left")
        last = np.searchsorted(wanted, batch_end, side="left")
        local = wanted[first:last] - row_offset
        values = batch["smiles"].gather(local).to_list()
        selected.update(zip(wanted[first:last].tolist(), values, strict=True))
        row_offset = batch_end
    require(len(selected) == len(wanted), "The source scan did not yield every selected row.")
    sample = [selected[int(position)] for position in sample_positions]
    tail = [selected[int(position)] for position in tail_positions]
    training_tail = [selected[int(position)] for position in training_tail_positions]
    return sample, tail, training_tail, {
        "rows": rows,
        "sha256": sha256(path),
        "sample_rows": SAMPLE_ROWS,
        "sample_seed": SAMPLE_SEED,
        "sample_position_sha256": hashlib.sha256(sample_positions.tobytes()).hexdigest(),
        "production_outer_tail_rows": outer_tail_rows,
        "production_model_tail_rows": rows % MODEL_BATCH_ROWS,
        "training_rows": training_rows,
        "training_seed": training_seed,
        "training_model_tail_rows": training_residual_rows,
        "training_tail_position_sha256": hashlib.sha256(
            training_tail_positions.tobytes()
        ).hexdigest(),
    }


class SmilesPath(nn.Module):
    """The unchanged production encoder and projection boundary for one full batch."""

    def __init__(self, model: nn.Module, compute_dtype: torch.dtype) -> None:
        super().__init__()
        self.model = model
        self.compute_dtype = compute_dtype

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        context = (
            torch.autocast(device_type="cuda", dtype=self.compute_dtype)
            if self.compute_dtype != torch.float32
            else nullcontext()
        )
        with context:
            return self.model.encode_modality(
                (input_ids, attention_mask),
                modality="smiles",
            )


def tokenize(inference: SecsInference, smiles: list[str]) -> tuple[torch.Tensor, torch.Tensor]:
    """Apply the exact tokenizer contract owned by the loaded production boundary."""

    tokens = inference._tokenizer(
        smiles,
        padding="max_length",
        truncation=True,
        return_tensors="pt",
        max_length=inference._smiles_context_length,
    )
    return (
        tokens["input_ids"].to(inference._device),
        tokens["attention_mask"].to(inference._device),
    )


def output_tensor(output) -> torch.Tensor:
    if isinstance(output, torch.Tensor):
        return output
    require(isinstance(output, (tuple, list)) and len(output) == 1, f"Unexpected model output: {type(output)!r}.")
    return output[0]


@torch.inference_mode()
def run_tokens(module: nn.Module, batches: list[tuple[torch.Tensor, torch.Tensor]]) -> np.ndarray:
    outputs = [output_tensor(module(*batch)).float().cpu() for batch in batches]
    return torch.cat(outputs).numpy()


def normalized(values: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    require(bool(np.isfinite(values).all()) and bool((norms > 0).all()), "Embeddings must be finite and nonzero.")
    return values / norms


def correctness(
    eager: np.ndarray,
    accelerated: np.ndarray,
    spectrum: np.ndarray,
) -> dict:
    eager_norm = normalized(eager)
    accelerated_norm = normalized(accelerated)
    row_cosines = np.sum(eager_norm * accelerated_norm, axis=1)
    normalized_l2 = np.linalg.norm(eager_norm - accelerated_norm, axis=1)
    query = spectrum / np.linalg.norm(spectrum)
    eager_scores = eager_norm @ query
    accelerated_scores = accelerated_norm @ query
    eager_order = np.argsort(-eager_scores, kind="stable")
    accelerated_order = np.argsort(-accelerated_scores, kind="stable")
    result = {
        "minimum_row_cosine": float(row_cosines.min()),
        "maximum_normalized_l2": float(normalized_l2.max()),
        "maximum_score_delta": float(np.max(np.abs(eager_scores - accelerated_scores))),
        "top_10_order_equal": bool(np.array_equal(eager_order[:10], accelerated_order[:10])),
        "top_100_membership_equal": bool(
            np.array_equal(np.sort(eager_order[:100]), np.sort(accelerated_order[:100]))
        ),
        "limits": {
            "minimum_row_cosine": MIN_ROW_COSINE,
            "maximum_normalized_l2": MAX_NORMALIZED_L2,
            "maximum_score_delta": MAX_SCORE_DELTA,
            "top_10_order_equal": True,
            "top_100_membership_equal": True,
        },
    }
    result["passed"] = (
        result["minimum_row_cosine"] >= MIN_ROW_COSINE
        and result["maximum_normalized_l2"] <= MAX_NORMALIZED_L2
        and result["maximum_score_delta"] <= MAX_SCORE_DELTA
        and result["top_10_order_equal"]
        and result["top_100_membership_equal"]
    )
    return result


@torch.inference_mode()
def model_seconds(module: nn.Module, batches: list[tuple[torch.Tensor, torch.Tensor]]) -> float:
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    start.record()
    for batch in batches:
        output_tensor(module(*batch))
    end.record()
    end.synchronize()
    return start.elapsed_time(end) / 1000.0


@torch.inference_mode()
def accelerated_end_to_end_seconds(
    inference: SecsInference,
    module: nn.Module,
    smiles: list[str],
) -> float:
    torch.cuda.synchronize()
    start = time.perf_counter()
    outputs = []
    for offset in range(0, len(smiles), MODEL_BATCH_ROWS):
        batch = tokenize(inference, smiles[offset : offset + MODEL_BATCH_ROWS])
        outputs.append(output_tensor(module(*batch)).float().cpu())
    torch.cat(outputs).numpy()
    torch.cuda.synchronize()
    return time.perf_counter() - start


def eager_end_to_end_seconds(inference: SecsInference, smiles: list[str]) -> float:
    torch.cuda.synchronize()
    start = time.perf_counter()
    inference.embed_smiles(smiles)
    torch.cuda.synchronize()
    return time.perf_counter() - start


def timing(
    inference: SecsInference,
    eager: nn.Module,
    accelerated: nn.Module,
    smiles: list[str],
    batches: list[tuple[torch.Tensor, torch.Tensor]],
    maximum_end_to_end_seconds: float,
) -> dict:
    for _ in range(2):
        run_tokens(eager, batches)
        run_tokens(accelerated, batches)
        eager_end_to_end_seconds(inference, smiles)
        accelerated_end_to_end_seconds(inference, accelerated, smiles)
    model_trials = {"eager": [], "tensorrt": []}
    end_to_end_trials = {"eager": [], "tensorrt": []}
    modules = {"eager": eager, "tensorrt": accelerated}
    for trial in range(TRIALS):
        order = ("eager", "tensorrt") if trial % 2 == 0 else ("tensorrt", "eager")
        for name in order:
            model_trials[name].append(model_seconds(modules[name], batches))
            if name == "eager":
                elapsed = eager_end_to_end_seconds(inference, smiles)
            else:
                elapsed = accelerated_end_to_end_seconds(inference, accelerated, smiles)
            end_to_end_trials[name].append(elapsed)
    result = {
        "trials": TRIALS,
        "model_only_seconds": model_trials,
        "end_to_end_seconds": end_to_end_trials,
        "median": {
            "eager_model_only_seconds": statistics.median(model_trials["eager"]),
            "tensorrt_model_only_seconds": statistics.median(model_trials["tensorrt"]),
            "eager_end_to_end_seconds": statistics.median(end_to_end_trials["eager"]),
            "tensorrt_end_to_end_seconds": statistics.median(end_to_end_trials["tensorrt"]),
        },
        "target": {
            "kind": "necessary_embedding_boundary_budget",
            "maximum_tensorrt_end_to_end_seconds": maximum_end_to_end_seconds,
            "does_not_prove_full_build_time": True,
            "unmeasured_full_build_phases": [
                "parquet_scan",
                "embedding_normalization",
                "faiss_training_and_add",
                "candidate_table_and_index_serialization",
            ],
        },
        "paired_speedups": {
            "model_only": [
                eager_time / accelerated_time
                for eager_time, accelerated_time in zip(
                    model_trials["eager"], model_trials["tensorrt"], strict=True
                )
            ],
            "end_to_end": [
                eager_time / accelerated_time
                for eager_time, accelerated_time in zip(
                    end_to_end_trials["eager"], end_to_end_trials["tensorrt"], strict=True
                )
            ],
        },
    }
    medians = result["median"]
    medians["model_only_speedup"] = (
        medians["eager_model_only_seconds"] / medians["tensorrt_model_only_seconds"]
    )
    medians["end_to_end_speedup"] = (
        medians["eager_end_to_end_seconds"] / medians["tensorrt_end_to_end_seconds"]
    )
    return result


@torch.inference_mode()
def tail_proof(
    inference: SecsInference,
    eager: nn.Module,
    accelerated: nn.Module,
    tail_smiles: list[str],
    base_tail: np.ndarray,
    spectrum: np.ndarray,
) -> dict:
    full_batches, residual_rows = divmod(len(tail_smiles), MODEL_BATCH_ROWS)
    require(full_batches > 0, "The source tail has no complete model batch to prove.")
    require(residual_rows > 0, "The source tail has no undersized model batch to prove.")
    calls = {"tensorrt": 0, "eager": 0}
    residual_output = None
    accelerated_outputs = []
    for offset in range(0, len(tail_smiles), MODEL_BATCH_ROWS):
        smiles = tail_smiles[offset : offset + MODEL_BATCH_ROWS]
        module = accelerated if len(smiles) == MODEL_BATCH_ROWS else eager
        calls["tensorrt" if module is accelerated else "eager"] += 1
        output = output_tensor(module(*tokenize(inference, smiles))).float().cpu().numpy()
        if module is eager:
            residual_output = output
        else:
            accelerated_outputs.append(output)
    expected_calls = {"tensorrt": full_batches, "eager": 1}
    require(calls == expected_calls, f"Unexpected production-tail dispatch: {calls!r}.")
    require(
        residual_output is not None and np.array_equal(residual_output, base_tail[-residual_rows:]),
        "The undersized eager fallback differs from the current production reference.",
    )
    accelerated_rows = full_batches * MODEL_BATCH_ROWS
    full_batch_correctness = correctness(
        base_tail[:accelerated_rows],
        np.concatenate(accelerated_outputs),
        spectrum,
    )
    require(
        full_batch_correctness["passed"],
        f"TensorRT changed the complete batches in the production tail: {full_batch_correctness!r}",
    )
    return {
        "rows": len(tail_smiles),
        "residual_rows": residual_rows,
        "calls": calls,
        "full_batch_correctness": full_batch_correctness,
        "eager_residual_bitwise_equal_to_production": True,
    }


def percentile(values: list[float], percent: float) -> float:
    return float(np.percentile(np.asarray(values), percent))


def loaded_native_libraries() -> list[dict]:
    paths = set()
    for line in Path("/proc/self/maps").read_text().splitlines():
        fields = line.split()
        if fields and fields[-1].startswith("/"):
            path = Path(fields[-1])
            if "libnvinfer" in path.name or "libcudart" in path.name:
                paths.add(path)
    require(any("libnvinfer" in path.name for path in paths), "No loaded TensorRT runtime library was found.")
    require(any("libcudart" in path.name for path in paths), "No loaded CUDA runtime library was found.")
    require(
        all(str(path).startswith("/probe/") for path in paths if "libnvinfer" in path.name),
        f"TensorRT loaded outside the admitted probe target: {sorted(map(str, paths))!r}.",
    )
    return [
        {"path": str(path), "sha256": sha256(path)}
        for path in sorted(paths, key=str)
    ]


def compile_path(eager: nn.Module, example: tuple[torch.Tensor, torch.Tensor]):
    import torch_tensorrt

    exported = torch.export.export(eager, example)
    started = time.perf_counter()
    compiled = torch_tensorrt.compile(
        exported,
        ir="dynamo",
        arg_inputs=example,
        min_block_size=1,
        require_full_compilation=True,
        use_python_runtime=True,
        use_explicit_typing=True,
        enable_autocast=True,
        autocast_low_precision_type=torch.bfloat16,
        enabled_precisions={torch.float32},
        cache_built_engines=False,
        reuse_cached_engines=False,
        pass_through_build_failures=True,
    )
    graph = str(compiled.graph)
    compute_node_objects = [
        node
        for node in compiled.graph.nodes
        if node.op in {"call_function", "call_module", "call_method"}
    ]
    engine_node_objects = [
        node for node in compute_node_objects if "execute_engine" in str(node.target)
    ]
    require(
        len(engine_node_objects) == 1,
        f"Expected one full-model TensorRT engine, got {compute_node_objects!r}.",
    )
    engine_node = engine_node_objects[0]
    allowed_getitems = [
        node
        for node in compute_node_objects
        if "getitem" in str(node.target) and node.args and node.args[0] is engine_node
    ]
    other_compute = [
        node
        for node in compute_node_objects
        if node is not engine_node and node not in allowed_getitems
    ]
    require(not other_compute, f"Compiled graph retains non-TensorRT compute: {other_compute!r}.")
    compute_nodes = [
        {"op": node.op, "target": str(node.target)} for node in compute_node_objects
    ]
    return compiled, {
        "seconds": time.perf_counter() - started,
        "engine_nodes": len(engine_node_objects),
        "compute_nodes": compute_nodes,
        "require_full_compilation": True,
        "pass_through_build_failures": True,
        "graph": graph,
    }


def expected_embedding_dimension(args: argparse.Namespace) -> int:
    manifest = json.loads(args.checkpoint_manifest.read_text())
    specification = args.checkpoint_manifest.parent / manifest["spec"]["file"]
    with specification.open("rb") as source:
        return tomllib.load(source)["model"]["projection_heads"]["smiles"]["dims"][-1]


def load_inference(args: argparse.Namespace) -> SecsInference:
    return SecsInference.load(
        args.checkpoint_manifest,
        molformer_lock=args.molformer_lock,
        device="cuda:0",
        compute_dtype=torch.bfloat16,
        smiles_batch_size=MODEL_BATCH_ROWS,
    )


def write_base_reference(args: argparse.Namespace, report: dict) -> None:
    print("Building the current-production eager control.", flush=True)
    specification = candidate_configuration(args.candidate_spec)
    sample, tail, training_tail, report["source"] = source_smiles(
        args.source,
        specification,
    )
    report["candidate_batching"] = {
        "model_batch_rows": specification["embedding"]["batch_size"],
        "outer_batch_rows": specification["table"]["batch_rows"],
    }
    inference = load_inference(args)
    sample_embeddings = inference.embed_smiles(sample)
    residual_rows = report["source"]["production_model_tail_rows"]
    require(residual_rows > 0, "The admitted source has no model-batch residual.")
    tail_embeddings = inference.embed_smiles(tail)
    training_tail_embeddings = inference.embed_smiles(training_tail)
    expected_shape = (SAMPLE_ROWS, expected_embedding_dimension(args))
    require(sample_embeddings.shape == expected_shape, f"Production eager shape is {sample_embeddings.shape}.")
    np.savez(
        args.base_reference,
        sample_smiles=np.asarray(sample),
        tail_smiles=np.asarray(tail),
        training_tail_smiles=np.asarray(training_tail),
        sample_embeddings=sample_embeddings,
        tail_embeddings=tail_embeddings,
        training_tail_embeddings=training_tail_embeddings,
        source_rows=np.asarray(report["source"]["rows"], dtype=np.int64),
    )
    report["reference"] = {
        "file": args.base_reference.name,
        "sha256": sha256(args.base_reference),
        "sample_shape": list(sample_embeddings.shape),
        "sample_dtype": str(sample_embeddings.dtype),
        "tail_shape": list(tail_embeddings.shape),
        "tail_dtype": str(tail_embeddings.dtype),
        "training_tail_shape": list(training_tail_embeddings.shape),
        "training_tail_dtype": str(training_tail_embeddings.dtype),
    }
    for _ in range(2):
        eager_end_to_end_seconds(inference, sample)
    trials = [eager_end_to_end_seconds(inference, sample) for _ in range(TRIALS)]
    report["baseline_timing"] = {
        "operation": "unchanged SecsInference.embed_smiles",
        "warmups": 2,
        "trials": trials,
        "median_seconds": statistics.median(trials),
        "worst_seconds": max(trials),
    }


def run_probe(args: argparse.Namespace, report: dict) -> None:
    print("Checking the fixed eager path before TensorRT compilation.", flush=True)
    artifact = admitted_molformer_source(args)
    derived_model = prepare_derived_cache(args, artifact)
    report["molformer"] = {
        "repository": artifact["repository"],
        "revision": artifact["revision"],
        "fixed_model_sha256": sha256(derived_model),
    }
    base_report = json.loads(args.base_reference_report.read_text())
    require(base_report["status"] == "passed", "The current-production control did not pass.")
    require(
        base_report["repository"] == report["repository"]
        and base_report["inputs"] == report["inputs"]
        and base_report["package_image_id"] == report["package_image_id"],
        "The current-production control and TensorRT probe do not admit the same inputs.",
    )
    require(
        base_report["reference"]["sha256"] == sha256(args.base_reference),
        "The current-production reference does not match its control receipt.",
    )
    report["base_reference_report_sha256"] = sha256(args.base_reference_report)
    with np.load(args.base_reference, allow_pickle=False) as reference:
        sample = reference["sample_smiles"].tolist()
        tail = reference["tail_smiles"].tolist()
        base_sample = reference["sample_embeddings"]
        base_tail = reference["tail_embeddings"]
        training_tail = reference["training_tail_smiles"].tolist()
        base_training_tail = reference["training_tail_embeddings"]
        source_rows = int(reference["source_rows"])
    report["base_reference_sha256"] = sha256(args.base_reference)
    inference = load_inference(args)
    specification = candidate_configuration(args.candidate_spec)
    report["candidate_batching"] = {
        "model_batch_rows": specification["embedding"]["batch_size"],
        "outer_batch_rows": specification["table"]["batch_rows"],
    }
    # This disposable probe deliberately reaches through SecsInference instead
    # of adding a TensorRT-shaped method to the production API before the
    # compiler and its numerical behavior have qualified.
    eager = SmilesPath(inference._model, torch.bfloat16).eval()
    batches = [
        tokenize(inference, sample[offset : offset + MODEL_BATCH_ROWS])
        for offset in range(0, len(sample), MODEL_BATCH_ROWS)
    ]
    report["tokens"] = {
        "input_ids_dtype": str(batches[0][0].dtype),
        "attention_mask_dtype": str(batches[0][1].dtype),
        "shape": list(batches[0][0].shape),
    }
    production_eager = inference.embed_smiles(sample)
    expected_shape = (SAMPLE_ROWS, expected_embedding_dimension(args))
    require(production_eager.shape == expected_shape, f"Fixed eager shape is {production_eager.shape}.")
    require(base_sample.shape == expected_shape, f"Base eager shape is {base_sample.shape}.")
    report["fixed_eager_control"] = {
        "shape": list(production_eager.shape),
        "dtype": str(production_eager.dtype),
        "bitwise_equal_to_current_production": bool(np.array_equal(production_eager, base_sample)),
    }
    require(
        report["fixed_eager_control"]["bitwise_equal_to_current_production"],
        "The fixed MolFormer source changes current production eager embeddings.",
    )
    print("Fixed eager matches production; compiling the full TensorRT path.", flush=True)
    wrapper_eager = run_tokens(eager, batches)
    require(
        np.array_equal(wrapper_eager, production_eager),
        "The compile wrapper differs from SecsInference.embed_smiles.",
    )
    accelerated, report["compilation"] = compile_path(eager, batches[0])
    accelerated_reference = run_tokens(accelerated, batches)
    report["output"] = {
        "shape": list(accelerated_reference.shape),
        "numpy_dtype": str(accelerated_reference.dtype),
        "raw_torch_dtype": str(output_tensor(accelerated(*batches[0])).dtype),
    }
    require(accelerated_reference.shape == expected_shape, f"TensorRT shape is {accelerated_reference.shape}.")
    with args.frontend_spectrum.open() as source:
        spectrum = np.asarray(json.load(source)["intensities"], dtype=np.float32)
    spectrum_embedding = inference.embed_spectrum(spectrum)
    report["correctness"] = correctness(production_eager, accelerated_reference, spectrum_embedding)
    require(report["correctness"]["passed"], f"TensorRT changed model or ranking behavior: {report['correctness']!r}")
    report["tail"] = tail_proof(
        inference,
        eager,
        accelerated,
        tail,
        base_tail,
        spectrum_embedding,
    )
    fixed_training_tail = run_tokens(eager, [tokenize(inference, training_tail)])
    report["training_tail"] = {
        "rows": len(training_tail),
        "fixed_eager_bitwise_equal_to_production": bool(
            np.array_equal(fixed_training_tail, base_training_tail)
        ),
    }
    require(
        report["training_tail"]["fixed_eager_bitwise_equal_to_production"],
        "The configured training-tail eager fallback differs from current production.",
    )
    production_outer_batches = (source_rows + OUTER_BATCH_ROWS - 1) // OUTER_BATCH_ROWS
    training_rows = specification["index"]["training_rows"]
    training_equivalent_batches = (training_rows + SAMPLE_ROWS - 1) // SAMPLE_ROWS
    measured_equivalent_batches = production_outer_batches + training_equivalent_batches
    baseline_median_seconds = base_report["baseline_timing"]["median_seconds"]
    baseline_trials = base_report["baseline_timing"]["trials"]
    require(
        len(baseline_trials) == TRIALS
        and all(np.isfinite(value) and value > 0 for value in baseline_trials)
        and baseline_median_seconds == statistics.median(baseline_trials),
        "The current-production timing receipt is internally inconsistent.",
    )
    maximum_batch_seconds = baseline_median_seconds / (
        TARGET_BUILD_SPEEDUP * PERFORMANCE_SAFETY_FACTOR
    )
    maximum_full_embedding_seconds = maximum_batch_seconds * measured_equivalent_batches
    report["timing"] = timing(
        inference,
        eager,
        accelerated,
        sample,
        batches,
        maximum_batch_seconds,
    )

    for group in ("model_only_seconds", "end_to_end_seconds"):
        report["timing"]["p90"] = report["timing"].get("p90", {})
        report["timing"]["worst"] = report["timing"].get("worst", {})
        for name, values in report["timing"][group].items():
            key = f"{name}_{group}"
            report["timing"]["p90"][key] = percentile(values, 90)
            report["timing"]["worst"][key] = max(values)
    observed_worst = report["timing"]["worst"]["tensorrt_end_to_end_seconds"]
    required_embedding_speedup = TARGET_BUILD_SPEEDUP * PERFORMANCE_SAFETY_FACTOR
    contemporaneous_speedup = (
        report["timing"]["median"]["eager_end_to_end_seconds"] / observed_worst
    )
    # The separate unchanged-production control measures the total proposed
    # gain, including the MolFormer correctness fix. The interleaved fixed-eager
    # control independently rejects a pass caused by an unusually slow earlier
    # baseline. Both must clear the same conservative threshold.
    report["timing"]["target"]["observed_worst_seconds"] = observed_worst
    report["timing"]["target"]["production_outer_batches"] = production_outer_batches
    report["timing"]["target"]["training_rows"] = training_rows
    report["timing"]["target"]["training_equivalent_batches"] = training_equivalent_batches
    report["timing"]["target"]["measured_equivalent_batches"] = measured_equivalent_batches
    report["timing"]["target"]["maximum_full_embedding_seconds"] = (
        maximum_full_embedding_seconds
    )
    report["timing"]["target"]["derivation"] = {
        "baseline_operation": base_report["baseline_timing"]["operation"],
        "baseline_trials": baseline_trials,
        "baseline_median_seconds": baseline_median_seconds,
        "target_embedding_speedup": TARGET_BUILD_SPEEDUP,
        "performance_safety_factor": PERFORMANCE_SAFETY_FACTOR,
        "formula": "baseline_median / (target_speedup * safety_factor)",
    }
    report["timing"]["target"]["contemporaneous_fixed_eager_speedup"] = (
        contemporaneous_speedup
    )
    report["timing"]["target"]["required_embedding_speedup"] = (
        required_embedding_speedup
    )
    report["timing"]["target"]["absolute_control_passed"] = (
        observed_worst <= maximum_batch_seconds
    )
    report["timing"]["target"]["contemporaneous_control_passed"] = (
        contemporaneous_speedup >= required_embedding_speedup
    )
    report["timing"]["target"]["passed"] = (
        report["timing"]["target"]["absolute_control_passed"]
        and report["timing"]["target"]["contemporaneous_control_passed"]
    )
    require(
        report["timing"]["target"]["passed"],
        f"TensorRT missed the necessary embedding-time budget: {report['timing']!r}",
    )
    repeated = run_tokens(accelerated, batches)
    report["post_timing_repeatability"] = {
        "bitwise_equal_to_first_tensorrt_result": bool(
            np.array_equal(repeated, accelerated_reference)
        )
    }
    require(
        report["post_timing_repeatability"]["bitwise_equal_to_first_tensorrt_result"],
        "TensorRT changed its output after the repeated timing workload.",
    )
    report["loaded_native_libraries"] = loaded_native_libraries()


def main() -> None:
    args = arguments()
    report = {
        "kind": f"secs.tensorrt-{args.mode}.v1",
        "status": "running",
        "package_image_id": args.package_image_id,
        "probe_sha256": sha256(Path(__file__)),
        "dependency_manifest_sha256": sha256(args.dependency_manifest),
        "wheelhouse_manifest": {
            "sha256": sha256(args.wheelhouse_manifest),
            "artifacts": args.wheelhouse_manifest.read_text().splitlines(),
        },
        "repository": {
            "revision": args.repository_revision,
            "launcher_sha256": args.launcher_sha256,
        },
        "inputs": {
            "checkpoint_manifest_sha256": sha256(args.checkpoint_manifest),
            "candidate_spec_sha256": sha256(args.candidate_spec),
            "molformer_lock_sha256": sha256(args.molformer_lock),
            "frontend_spectrum_sha256": sha256(args.frontend_spectrum),
        },
    }
    try:
        require(torch.cuda.device_count() == 1, "The probe container must expose exactly one GPU.")
        visible_gpu_uuid = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
            text=True,
        ).strip()
        require(
            visible_gpu_uuid == args.host_gpu_uuid,
            f"Container GPU UUID {visible_gpu_uuid!r} does not match host selection {args.host_gpu_uuid!r}.",
        )
        report["runtime"] = {
            "torch": torch.__version__,
            "torch_cuda": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(0),
            "compute_capability": list(torch.cuda.get_device_capability(0)),
            "host_gpu_index": args.host_gpu_index,
            "host_gpu_uuid": args.host_gpu_uuid,
            "visible_gpu_uuid": visible_gpu_uuid,
            "gpu_observation": {
                "idle_preflight": True,
                "poll_interval_seconds": args.gpu_monitor_interval_seconds,
                "continuous_exclusivity_proven": False,
            },
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("torch-tensorrt", "tensorrt", "tensorrt-cu13")
            },
        }
        if args.mode == "base-reference":
            write_base_reference(args, report)
        else:
            run_probe(args, report)
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["failure"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
        raise
    finally:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
