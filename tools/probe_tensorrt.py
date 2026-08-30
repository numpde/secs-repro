#!/usr/bin/env python3
"""Qualify or diagnose one fixed-shape TensorRT path outside production inference."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import gc
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
NUMERICS_BOUNDARIES = (
    "projection_layer_normalization",
    "masked_average_pool",
    "final_layer_normalization",
    "last_encoder_layer",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def exact_tensor_equal(first: torch.Tensor, second: torch.Tensor) -> bool:
    if first.shape != second.shape or first.dtype != second.dtype:
        return False
    first_bytes = first.detach().reshape(-1).contiguous().view(torch.uint8).cpu()
    second_bytes = second.detach().reshape(-1).contiguous().view(torch.uint8).cpu()
    return bool(torch.equal(first_bytes, second_bytes))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("base-reference", "probe"), required=True)
    parser.add_argument("--diagnose-numerics", action="store_true")
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


class SmilesNumericsPath(nn.Module):
    """Expose one natural boundary alongside the unchanged final projection."""

    def __init__(
        self,
        model: nn.Module,
        compute_dtype: torch.dtype,
        boundary: str | None,
    ) -> None:
        super().__init__()
        self.backbone = model.dict_encoders["smiles"].encoder
        projection = model.dict_projection_heads["smiles"].projection_head
        require(
            len(projection) == 2
            and isinstance(projection[0], nn.LayerNorm)
            and isinstance(projection[1], nn.Linear),
            f"The diagnostic does not understand this projection head: {projection!r}.",
        )
        self.projection_normalization = projection[0]
        self.projection = projection[1]
        self.compute_dtype = compute_dtype
        self.boundary = boundary

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> torch.Tensor | tuple[torch.Tensor, torch.Tensor]:
        context = (
            torch.autocast(device_type="cuda", dtype=self.compute_dtype)
            if self.compute_dtype != torch.float32
            else nullcontext()
        )
        with context:
            needs_last_encoder_layer = self.boundary == "last_encoder_layer"
            backbone_outputs = self.backbone(
                input_ids=input_ids,
                attention_mask=attention_mask,
                output_attentions=False,
                output_hidden_states=needs_last_encoder_layer,
                return_dict=False,
            )
            final_layer_normalization = backbone_outputs[0]
            pooled = backbone_outputs[1]
            projection_normalization = self.projection_normalization(pooled)
            projected = self.projection(projection_normalization)
            if self.boundary is None:
                return projected
            if self.boundary == "projection_layer_normalization":
                boundary_output = projection_normalization
            elif self.boundary == "masked_average_pool":
                boundary_output = pooled
            elif self.boundary == "final_layer_normalization":
                boundary_output = final_layer_normalization
            elif self.boundary == "last_encoder_layer":
                boundary_output = backbone_outputs[2][-1]
            else:
                raise RuntimeError(
                    f"Unsupported numerical diagnostic boundary: {self.boundary!r}."
                )
        return boundary_output, projected


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


def output_tensors(output) -> tuple[torch.Tensor, ...]:
    if isinstance(output, torch.Tensor):
        return (output,)
    require(isinstance(output, (tuple, list)), f"Unexpected model output: {type(output)!r}.")
    tensors = tuple(tensor for item in output for tensor in output_tensors(item))
    require(tensors, "The model returned no tensors.")
    return tensors


def tensor_sha256(tensor: torch.Tensor) -> str:
    contiguous = tensor.detach().cpu().contiguous()
    return hashlib.sha256(contiguous.numpy().tobytes()).hexdigest()


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
        "maximum_normalized_l2_row": int(np.argmax(normalized_l2)),
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
    from torch_tensorrt.dynamo.runtime import PythonTorchTensorRTModule

    # Export once here so this program is the sole graph authority for
    # compilation. The generic entry point owns export from a module;
    # dynamo.compile consumes an existing ExportedProgram without retracing it.
    exported = torch.export.export(eager, example)
    with torch.inference_mode():
        eager_example = output_tensors(eager(*example))
        exported_example = output_tensors(exported.module()(*example))
    exported_example_equal = len(exported_example) == len(eager_example) and all(
        exact_tensor_equal(exported_tensor, eager_tensor)
        for exported_tensor, eager_tensor in zip(
            exported_example,
            eager_example,
            strict=True,
        )
    )
    require(
        exported_example_equal,
        "The exported program differs from the eager compile example.",
    )
    output_tensor_count = len(eager_example)
    del eager_example, exported_example
    torch.cuda.empty_cache()
    require_full_compilation = True
    # TensorRT otherwise permits TF32-rounded multiplicands in FP32 inner
    # products, a numerical relaxation the eager graph does not request.
    disable_tf32 = True
    started = time.perf_counter()
    compiled = torch_tensorrt.dynamo.compile(
        exported,
        arg_inputs=example,
        min_block_size=1,
        require_full_compilation=require_full_compilation,
        use_python_runtime=True,
        use_explicit_typing=True,
        # The exported graph already owns production's mixed-precision
        # autocast. Leaving compiler autocast disabled prevents a second,
        # independent policy from rewriting those recorded dtype boundaries.
        enabled_precisions={torch.float32},
        disable_tf32=disable_tf32,
        cache_built_engines=False,
        reuse_cached_engines=False,
    )
    graph = str(compiled.graph)
    compute_node_objects = [
        node
        for node in compiled.graph.nodes
        if node.op in {"call_function", "call_module", "call_method"}
    ]
    # Dynamo returns an outer dispatch graph whose accelerated child owns the
    # TensorRT engine. Resolve the child instead of assuming the outer graph
    # exposes the runtime's internal execute_engine implementation.
    engine_node_objects = []
    for node in compute_node_objects:
        if node.op != "call_module":
            continue
        module = compiled.get_submodule(str(node.target))
        if isinstance(module, PythonTorchTensorRTModule):
            engine_node_objects.append(node)
    require(
        len(engine_node_objects) == 1,
        f"Expected one full-model TensorRT engine, got {compute_node_objects!r}.",
    )
    engine_node = engine_node_objects[0]
    engine_module = compiled.get_submodule(str(engine_node.target))
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
        "engine_module_type": (
            f"{type(engine_module).__module__}.{type(engine_module).__qualname__}"
        ),
        "exported_example_bitwise_equal_to_eager": exported_example_equal,
        "output_tensors": output_tensor_count,
        "compute_nodes": compute_nodes,
        "require_full_compilation": require_full_compilation,
        "requested_disable_tf32": disable_tf32,
        "graph": graph,
    }, engine_module


def unchanged_engine_layer_information(engine_module: nn.Module) -> dict:
    raw_information = engine_module.get_layer_info()
    try:
        information = json.loads(raw_information)
    except json.JSONDecodeError:
        information = raw_information
    serialized_engine = engine_module.serialized_engine
    require(
        isinstance(serialized_engine, (bytes, bytearray)),
        "The rejected TensorRT engine does not expose its serialized identity.",
    )
    return {
        "serialized_engine_sha256": hashlib.sha256(serialized_engine).hexdigest(),
        "profiling_verbosity": str(engine_module.engine.profiling_verbosity),
        "layers": int(engine_module.engine.num_layers),
        "io_tensors": int(engine_module.engine.num_io_tensors),
        "layer_information_sha256": hashlib.sha256(
            raw_information.encode()
        ).hexdigest(),
        "layer_information": information,
        "limitation": (
            "This is read-only implementation metadata from the rejected engine. "
            "It does not expose activation values or identify a numerical cause."
        ),
    }


def tensor_difference(eager: torch.Tensor, accelerated: torch.Tensor) -> dict:
    require(
        eager.shape == accelerated.shape,
        f"Diagnostic tensor shapes differ: {tuple(eager.shape)} != {tuple(accelerated.shape)}.",
    )
    eager_float = eager.float()
    accelerated_float = accelerated.float()
    difference = accelerated_float - eager_float
    eager_norm = torch.linalg.vector_norm(eager_float)
    accelerated_norm = torch.linalg.vector_norm(accelerated_float)
    difference_norm = torch.linalg.vector_norm(difference)
    eager_rms = torch.sqrt(torch.mean(torch.square(eager_float)))
    difference_rms = torch.sqrt(torch.mean(torch.square(difference)))
    cosine_denominator = float(eager_norm) * float(accelerated_norm)
    return {
        "shape": list(eager.shape),
        "eager_dtype": str(eager.dtype),
        "tensorrt_dtype": str(accelerated.dtype),
        "bitwise_equal": exact_tensor_equal(eager, accelerated),
        "finite": bool(
            torch.isfinite(eager_float).all()
            and torch.isfinite(accelerated_float).all()
        ),
        "maximum_absolute_error": float(torch.max(torch.abs(difference))),
        "root_mean_square_error": float(difference_rms),
        "root_mean_square_error_over_eager_root_mean_square": (
            float(difference_rms / eager_rms) if float(eager_rms) > 0 else None
        ),
        "normalized_l2": float(difference_norm) / max(
            float(eager_norm),
            torch.finfo(torch.float32).tiny,
        ),
        "cosine": (
            float(torch.sum(eager_float * accelerated_float)) / cosine_denominator
            if cosine_denominator > 0
            else None
        ),
    }


@torch.inference_mode()
def run_numerics_variant(
    eager: nn.Module,
    compile_example: tuple[torch.Tensor, torch.Tensor],
    selected_example: tuple[torch.Tensor, torch.Tensor],
    original_eager: torch.Tensor,
    original_accelerated: torch.Tensor,
    boundary: str | None,
) -> tuple[dict, torch.Tensor | None, torch.Tensor | None]:
    result = {"boundary": boundary or "final_only_control", "status": "running"}
    diagnostic_eager = SmilesNumericsPath(
        eager.model,
        eager.compute_dtype,
        boundary,
    ).eval()
    eager_outputs = tuple(
        tensor.detach().cpu()
        for tensor in output_tensors(diagnostic_eager(*selected_example))
    )
    expected_outputs = 1 if boundary is None else 2
    require(
        len(eager_outputs) == expected_outputs,
        f"The {result['boundary']} eager diagnostic returned {len(eager_outputs)} tensors.",
    )
    eager_final_equal = exact_tensor_equal(eager_outputs[-1].float(), original_eager)
    result["eager_final_bitwise_equal_to_qualification_path"] = eager_final_equal
    if not eager_final_equal:
        result["endpoint_binding_passed"] = False
        result["status"] = "endpoint_changed"
        result["reason"] = (
            "The diagnostic wrapper did not reproduce the eager qualification endpoint."
        )
        return result, None, None

    print(f"Compiling numerical diagnostic: {result['boundary']}.", flush=True)
    torch.cuda.empty_cache()
    diagnostic_accelerated = None
    engine_module = None
    try:
        diagnostic_accelerated, result["compilation"], engine_module = compile_path(
            diagnostic_eager,
            compile_example,
        )
        accelerated_outputs = tuple(
            tensor.detach().cpu()
            for tensor in output_tensors(diagnostic_accelerated(*selected_example))
        )
        require(
            len(accelerated_outputs) == len(eager_outputs),
            "The diagnostic engine returned the wrong number of tensors.",
        )
    finally:
        diagnostic_accelerated = None
        engine_module = None
        gc.collect()
        torch.cuda.empty_cache()

    accelerated_final = accelerated_outputs[-1].float()
    accelerated_final_equal = exact_tensor_equal(
        accelerated_final,
        original_accelerated,
    )
    result["tensorrt_final_bitwise_equal_to_qualification_path"] = (
        accelerated_final_equal
    )
    result["tensorrt_endpoint_sha256"] = tensor_sha256(accelerated_final)
    result["tensorrt_endpoint_difference"] = tensor_difference(
        original_accelerated,
        accelerated_final,
    )
    # Adding an output can change TensorRT fusion or tactics. Exact endpoint
    # equality admits a localization hypothesis; it does not prove that this
    # rebuilt engine shares the rejected engine's unobserved internal values.
    if not accelerated_final_equal:
        result["endpoint_binding_passed"] = False
        result["status"] = "endpoint_changed"
        result["reason"] = (
            "This rebuilt graph did not reproduce the rejected TensorRT endpoint."
        )
        return result, None, None

    result["endpoint_binding_passed"] = True
    result["status"] = "usable"
    if boundary is None:
        return result, None, None
    return result, eager_outputs[0], accelerated_outputs[0]


@torch.inference_mode()
def diagnose_numerics(
    eager: nn.Module,
    compile_example: tuple[torch.Tensor, torch.Tensor],
    selected_example: tuple[torch.Tensor, torch.Tensor],
    original_eager: torch.Tensor,
    original_accelerated: torch.Tensor,
    selected_row_within_batch: int,
    report: dict,
) -> None:
    try:
        final_control, _, _ = run_numerics_variant(
            eager,
            compile_example,
            selected_example,
            original_eager,
            original_accelerated,
            None,
        )
    except Exception as error:
        final_control = {
            "boundary": "final_only_control",
            "status": "failed",
            "failure": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }
    report["final_only_control"] = final_control
    if final_control["status"] != "usable":
        report["status"] = "inconclusive"
        report["usable_as_localization_hypothesis"] = False
        report["reason"] = {
            "endpoint_changed": (
                "The direct diagnostic wrapper changed an endpoint before any boundary was exposed."
            ),
            "failed": "The final-only diagnostic control could not be executed.",
        }.get(
            final_control["status"],
            "The final-only diagnostic control was not usable.",
        )
        return

    report["boundary_probes"] = []
    nearest_divergent_downstream_boundary = "projection"
    for boundary in NUMERICS_BOUNDARIES:
        try:
            probe, eager_boundary, accelerated_boundary = run_numerics_variant(
                eager,
                compile_example,
                selected_example,
                original_eager,
                original_accelerated,
                boundary,
            )
        except Exception as error:
            probe = {
                "boundary": boundary,
                "status": "failed",
                "failure": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }
            eager_boundary = None
            accelerated_boundary = None
        report["boundary_probes"].append(probe)
        if probe["status"] != "usable":
            report["status"] = "blocked_at_boundary"
            report["blocked_boundary"] = boundary
            report["usable_as_localization_hypothesis"] = False
            return
        require(
            eager_boundary is not None and accelerated_boundary is not None,
            f"The usable {boundary} probe did not return its boundary tensors.",
        )
        probe["whole_qualification_sample_batch"] = tensor_difference(
            eager_boundary,
            accelerated_boundary,
        )
        probe["selected_sample_row"] = tensor_difference(
            eager_boundary[selected_row_within_batch],
            accelerated_boundary[selected_row_within_batch],
        )
        if probe["selected_sample_row"]["bitwise_equal"]:
            report["status"] = "completed"
            report["nearest_equal_upstream_boundary_for_selected_sample_row"] = (
                boundary
            )
            report[
                "nearest_observed_divergent_downstream_boundary_for_selected_sample_row"
            ] = (
                nearest_divergent_downstream_boundary
            )
            report["usable_as_localization_hypothesis"] = True
            break
        nearest_divergent_downstream_boundary = boundary
    else:
        report["status"] = "no_equal_boundary_for_selected_row_within_bound"
        report["usable_as_localization_hypothesis"] = True
        report["interpretation"] = (
            "The selected row differed as far upstream as the last bounded probe. "
            "Each result still comes from a separate endpoint-equivalent graph, not "
            "from observing the rejected engine's internal values."
        )
        return

    report["interpretation"] = (
        "Each admitted boundary comes from a separate endpoint-equivalent graph. "
        "For the selected worst sample row, the nearest equal upstream boundary "
        "narrows a downstream reproducer; it does not prove the rejected engine's "
        "internal values or identify a causal layer."
    )


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
    accelerated, report["compilation"], rejected_engine_module = compile_path(
        eager,
        batches[0],
    )
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
    if args.diagnose_numerics:
        require(
            not report["correctness"]["passed"],
            "The diagnostic target requires a rejected numerical result and can never qualify a candidate.",
        )
        worst_row = report["correctness"]["maximum_normalized_l2_row"]
        batch_index, row_within_batch = divmod(worst_row, MODEL_BATCH_ROWS)
        batch_start = batch_index * MODEL_BATCH_ROWS
        example = batches[batch_index]
        report["numerics_diagnostic"] = {
            "qualifies": False,
            "status": "running",
            "selection": {
                "criterion": "maximum normalized L2 over the qualification sample",
                "sample_row": worst_row,
                "normalized_l2": report["correctness"]["maximum_normalized_l2"],
                "batch_index": batch_index,
                "batch_sample_row_start": batch_start,
                "batch_sample_row_end_exclusive": batch_start + MODEL_BATCH_ROWS,
                "row_within_batch": row_within_batch,
                "input_ids_sha256": tensor_sha256(example[0]),
                "attention_mask_sha256": tensor_sha256(example[1]),
            },
        }
        batch_end = batch_start + MODEL_BATCH_ROWS
        # These exact slices produced the rejected correctness result. Binding
        # diagnostics to them avoids substituting a fresh model rerun for the
        # numerical event that selected this batch.
        original_eager = torch.from_numpy(
            production_eager[batch_start:batch_end].copy()
        )
        original_accelerated = torch.from_numpy(
            accelerated_reference[batch_start:batch_end].copy()
        )
        require(
            not exact_tensor_equal(
                original_eager[row_within_batch],
                original_accelerated[row_within_batch],
            ),
            "The selected worst row has no bitwise TensorRT endpoint divergence.",
        )
        report["numerics_diagnostic"]["endpoint_binding"] = {
            "eager_sha256": tensor_sha256(original_eager),
            "rejected_tensorrt_sha256": tensor_sha256(original_accelerated),
        }
        try:
            report["numerics_diagnostic"]["unchanged_engine_layer_information"] = (
                unchanged_engine_layer_information(rejected_engine_module)
            )
            repeated_accelerated = output_tensor(accelerated(*example)).float().cpu()
            repeatable = exact_tensor_equal(
                repeated_accelerated,
                original_accelerated,
            )
            report["numerics_diagnostic"]["rejected_engine_repeatability"] = {
                "bitwise_equal_to_rejected_sample_slice": repeatable,
                "repeated_sha256": tensor_sha256(repeated_accelerated),
            }
            # Subsequent graphs need the GPU memory owned by this engine. Its
            # exact selected endpoint and read-only layer map are now retained.
            del rejected_engine_module, accelerated
            gc.collect()
            torch.cuda.empty_cache()
            if not repeatable:
                report["numerics_diagnostic"]["status"] = "inconclusive"
                report["numerics_diagnostic"][
                    "usable_as_localization_hypothesis"
                ] = False
                report["numerics_diagnostic"]["reason"] = (
                    "The rejected TensorRT engine did not repeat its selected endpoint."
                )
                raise RuntimeError(
                    "TensorRT failed numerical qualification and its rejected endpoint was not repeatable."
                )
            diagnose_numerics(
                eager,
                batches[0],
                example,
                original_eager,
                original_accelerated,
                row_within_batch,
                report["numerics_diagnostic"],
            )
        except BaseException as error:
            diagnostic_report = report["numerics_diagnostic"]
            if diagnostic_report["status"] == "running":
                diagnostic_report["status"] = "failed"
            diagnostic_report["usable_as_localization_hypothesis"] = False
            diagnostic_report["failure"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            raise
        raise RuntimeError(
            "TensorRT failed numerical qualification; the separate diagnostic report cannot qualify it."
        )
    require(
        report["correctness"]["passed"],
        f"TensorRT changed model or ranking behavior: {report['correctness']!r}",
    )
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
    report_kind = (
        "secs.tensorrt-numerics-diagnostic.v1"
        if args.diagnose_numerics
        else f"secs.tensorrt-{args.mode}.v1"
    )
    report = {
        "kind": report_kind,
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
    if args.diagnose_numerics:
        report["qualifies"] = False
    try:
        require(
            not args.diagnose_numerics or args.mode == "probe",
            "Numerical diagnosis is only valid for the fixed-model probe mode.",
        )
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
