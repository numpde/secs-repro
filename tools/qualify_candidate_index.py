#!/usr/bin/env python3
"""Qualify the production candidate builder without publishing its sampled artifacts.

This tool owns deterministic real-source sampling, serialized-bundle verification,
and the bounded extrapolation recorded by the qualification lane. The production
builder remains authoritative for table and index construction.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import tempfile
import tomllib

import faiss
import numpy as np
import polars as pl
import torch

from secs_inference import SecsInference


READ_BYTES = 1024 * 1024
CANDIDATE_COLUMNS = ("smiles", "molecular_formula")
SOURCE_ROW_COLUMN = "__qualification_source_row"
SCALE_ROW_COLUMN = "__qualification_scale_row"
QUALIFICATION_KIND = "secs.candidate-build-qualification.v2"
PROGRESS_PATTERN = re.compile(r"^Indexed ([0-9]+) of ([0-9]+) candidate rows\.$")


@dataclass(frozen=True)
class SamplingProfile:
    """Select one fixed congruence class of source-row ordinals."""

    name: str
    modulus: int
    remainder: int


@dataclass(frozen=True)
class QualificationPlan:
    """Bind the admitted source, nested profiles, and projection safety margin."""

    source_sha256: str
    functional: SamplingProfile
    scale: SamplingProfile
    maximum_memory_fraction: float
    maximum_disk_fraction: float


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(READ_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_toml(path: Path) -> dict:
    with path.open("rb") as source:
        return tomllib.load(source)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    """Replace a receipt only after its complete bytes are written beside it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as output:
        temporary_path = Path(output.name)
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fchmod(output.fileno(), 0o644)
    temporary_path.replace(path)


def selected_row_count(total_rows: int, profile: SamplingProfile) -> int:
    if profile.remainder >= total_rows:
        return 0
    return (total_rows - 1 - profile.remainder) // profile.modulus + 1


def sampling_profile(name: str, raw: dict) -> SamplingProfile:
    profile = SamplingProfile(
        name=name,
        modulus=raw["source_modulus"],
        remainder=raw["source_remainder"],
    )
    if profile.modulus <= 0 or not 0 <= profile.remainder < profile.modulus:
        raise ValueError(
            f"Cannot use qualification profile {name!r} with "
            f"modulus={profile.modulus}, remainder={profile.remainder}; "
            "the modulus must be positive and the remainder must lie inside it."
        )
    return profile


def load_plan(path: Path) -> QualificationPlan:
    raw = read_toml(path)
    source = raw["source"]
    profiles = raw["profiles"]
    plan = QualificationPlan(
        source_sha256=source["sha256"],
        functional=sampling_profile("functional", profiles["functional"]),
        scale=sampling_profile("scale", profiles["scale"]),
        maximum_memory_fraction=raw["projection"]["maximum_memory_fraction"],
        maximum_disk_fraction=raw["projection"]["maximum_disk_fraction"],
    )
    if not re.fullmatch(r"[0-9a-f]{64}", plan.source_sha256):
        raise ValueError("Cannot qualify candidates because source.sha256 is not a lowercase SHA-256 digest.")
    if not 0 < plan.maximum_memory_fraction < 1 or not 0 < plan.maximum_disk_fraction < 1:
        raise ValueError(
            "Cannot qualify candidates because both projection safety fractions must lie strictly between zero and one."
        )
    if (
        plan.functional.modulus % plan.scale.modulus != 0
        or plan.functional.remainder % plan.scale.modulus != plan.scale.remainder
    ):
        raise ValueError(
            "Cannot qualify candidates because every functional source row must also belong to the scale profile."
        )
    return plan


def table_statistics(table: pl.LazyFrame) -> tuple[dict[str, str], int, dict[str, int]]:
    schema_object = table.collect_schema()
    schema = {name: str(dtype) for name, dtype in schema_object.items()}
    missing = [column for column in CANDIDATE_COLUMNS if column not in schema_object]
    if missing:
        raise ValueError(f"Cannot sample candidate rows because the source is missing columns {missing!r}.")
    wrong_types = {column: str(schema_object[column]) for column in CANDIDATE_COLUMNS if schema_object[column] != pl.String}
    if wrong_types:
        raise ValueError(
            f"Cannot sample candidate rows because candidate columns must be String; got {wrong_types}."
        )
    statistics_row = table.select(
        pl.len().alias("rows"),
        *(pl.col(column).null_count().alias(column) for column in CANDIDATE_COLUMNS),
    ).collect(engine="streaming").row(0, named=True)
    rows = statistics_row.pop("rows")
    return schema, rows, statistics_row


def candidate_row_sha256(path: Path, batch_rows: int) -> str:
    """Hash candidate values with length framing so row order and column boundaries remain visible."""

    digest = hashlib.sha256()
    table = pl.scan_parquet(path).select(CANDIDATE_COLUMNS)
    for batch in table.collect_batches(chunk_size=batch_rows, maintain_order=True, engine="streaming"):
        for row in batch.iter_rows():
            for value in row:
                if value is None:
                    raise ValueError(f"Cannot hash candidate rows from {path}: candidate values must not be null.")
                encoded = value.encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
    return digest.hexdigest()


def sample_details(path: Path, profile: SamplingProfile, expected_rows: int, batch_rows: int) -> dict:
    schema, rows, nulls = table_statistics(pl.scan_parquet(path))
    if rows != expected_rows or any(nulls.values()):
        raise ValueError(
            f"Cannot admit {profile.name!r} sample: the fixed rule requires {expected_rows} non-null rows, "
            f"but the written sample has rows={rows}, nulls={nulls}."
        )
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "candidate_row_sha256": candidate_row_sha256(path, batch_rows),
        "rows": rows,
        "schema": schema,
        "selection": {
            "source_modulus": profile.modulus,
            "source_remainder": profile.remainder,
        },
    }


def sample_source(args: argparse.Namespace) -> None:
    plan = load_plan(args.qualification_spec)
    candidate_spec = read_toml(args.candidate_spec)
    batch_rows = candidate_spec["table"]["batch_rows"]
    actual_source_sha256 = sha256(args.source)
    if actual_source_sha256 != plan.source_sha256:
        raise ValueError(
            f"Cannot sample the admitted candidate member: its SHA-256 is {actual_source_sha256}, "
            f"but the qualification specification requires {plan.source_sha256}."
        )

    source_table = pl.scan_parquet(args.source)
    source_schema, source_rows, source_nulls = table_statistics(source_table)
    if source_rows == 0 or any(source_nulls.values()):
        raise ValueError(
            f"Cannot sample the admitted candidate member: the source must have rows without null candidate "
            f"values, but it has rows={source_rows}, nulls={source_nulls}."
        )

    args.output_directory.mkdir(parents=True, exist_ok=True)
    scale_path = args.output_directory / "scale.parquet"
    functional_path = args.output_directory / "functional.parquet"
    source_columns = tuple(source_schema)
    source_table.with_row_index(SOURCE_ROW_COLUMN).filter(
        pl.col(SOURCE_ROW_COLUMN) % plan.scale.modulus == plan.scale.remainder
    ).select(source_columns).sink_parquet(
        scale_path,
        compression="zstd",
        maintain_order=True,
        engine="streaming",
    )

    # Selecting from the scale sample avoids a second full-source scan. The
    # plan validation above proves this ordinal rule is the same source-row set.
    scale_period = plan.functional.modulus // plan.scale.modulus
    scale_remainder = (plan.functional.remainder - plan.scale.remainder) // plan.scale.modulus
    pl.scan_parquet(scale_path).with_row_index(SCALE_ROW_COLUMN).filter(
        pl.col(SCALE_ROW_COLUMN) % scale_period == scale_remainder
    ).select(source_columns).sink_parquet(
        functional_path,
        compression="zstd",
        maintain_order=True,
        engine="streaming",
    )

    receipt = {
        "kind": QUALIFICATION_KIND,
        "created_at": utc_now(),
        "qualification_spec": {
            "file": args.qualification_spec.name,
            "sha256": sha256(args.qualification_spec),
        },
        "candidate_spec": {
            "file": args.candidate_spec.name,
            "sha256": sha256(args.candidate_spec),
        },
        "source": {
            "archive_member": candidate_spec["archive"]["member"],
            "bytes": args.source.stat().st_size,
            "sha256": actual_source_sha256,
            "rows": source_rows,
            "schema": source_schema,
            "candidate_nulls": source_nulls,
        },
        "profiles": {
            "functional": sample_details(
                functional_path,
                plan.functional,
                selected_row_count(source_rows, plan.functional),
                batch_rows,
            ),
            "scale": sample_details(
                scale_path,
                plan.scale,
                selected_row_count(source_rows, plan.scale),
                batch_rows,
            ),
        },
    }
    write_json(args.output_directory / "samples.json", receipt)


def require_equal(operation: str, actual, expected) -> None:
    if actual != expected:
        raise ValueError(f"Cannot {operation}: got {actual!r}, but the qualification requires {expected!r}.")


def verified_artifact(directory: Path, receipt: dict, operation: str) -> Path:
    path = directory / receipt["file"]
    if not path.is_file():
        raise ValueError(f"Cannot {operation}: the recorded artifact is missing at {path}.")
    require_equal(f"{operation} because its byte count changed", path.stat().st_size, receipt["bytes"])
    require_equal(f"{operation} because its SHA-256 changed", sha256(path), receipt["sha256"])
    return path


def file_receipt(path: Path) -> dict:
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}


def parsed_utc(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def progress_summary(path: Path, expected_rows: int, started_at: datetime, finished_at: datetime) -> dict:
    observations: list[tuple[datetime, int]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        timestamp_text, separator, message = line.partition("|")
        if not separator:
            continue
        match = PROGRESS_PATTERN.fullmatch(message)
        if match is None:
            continue
        indexed_rows, total_rows = map(int, match.groups())
        require_equal("accept builder progress with a different total row count", total_rows, expected_rows)
        observations.append((parsed_utc(timestamp_text), indexed_rows))
    if not observations or observations[-1][1] != expected_rows:
        raise ValueError(
            f"Cannot verify builder progress from {path}: the log does not end at {expected_rows} indexed rows."
        )
    if observations[0][0] < started_at or observations[-1][0] > finished_at:
        raise ValueError(f"Cannot verify builder progress from {path}: progress timestamps lie outside the bound run.")
    intervals = []
    for (earlier_time, earlier_rows), (later_time, later_rows) in zip(observations, observations[1:]):
        if later_time <= earlier_time or later_rows <= earlier_rows:
            raise ValueError(
                f"Cannot verify builder progress from {path}: timestamps and indexed row counts must increase strictly."
            )
        intervals.append((later_time - earlier_time).total_seconds())
    summary = {"observations": len(observations), "final_rows": observations[-1][1]}
    if intervals:
        window = min(10, len(intervals))
        summary.update(
            {
                "batch_seconds_median": statistics.median(intervals),
                "batch_seconds_p90": float(np.percentile(intervals, 90)),
                "first_window_mean_seconds": statistics.fmean(intervals[:window]),
                "last_window_mean_seconds": statistics.fmean(intervals[-window:]),
                "window_batches": window,
            }
        )
    return summary


def gpu_summary(path: Path, started_at: datetime, finished_at: datetime) -> dict:
    observations = []
    with path.open(newline="", encoding="utf-8") as source:
        for timestamped_line in source:
            timestamp, separator, values = timestamped_line.rstrip("\n").partition("|")
            if not separator:
                continue
            row = next(csv.reader([values], skipinitialspace=True))
            if len(row) != 5:
                raise ValueError(f"Cannot verify GPU observations from {path}: malformed row at {timestamp!r}.")
            uuid, name, total_mib, used_mib, utilization = row
            observations.append(
                {
                    "timestamp": timestamp,
                    "observed_at": parsed_utc(timestamp),
                    "uuid": uuid,
                    "name": name,
                    "total_mib": float(total_mib),
                    "used_mib": float(used_mib),
                    "utilization_percent": float(utilization),
                }
            )
    if not observations:
        raise ValueError(f"Cannot verify GPU resource use because {path} contains no observations.")
    for earlier, later in zip(observations, observations[1:]):
        if later["observed_at"] <= earlier["observed_at"]:
            raise ValueError(f"Cannot verify GPU resource use because timestamps in {path} must increase strictly.")
    run_observations = [row for row in observations if started_at <= row["observed_at"] <= finished_at]
    if not run_observations:
        raise ValueError(f"Cannot verify GPU resource use because {path} has no observation during the bound run.")
    identities = {(row["uuid"], row["name"], row["total_mib"]) for row in run_observations}
    if len(identities) != 1:
        raise ValueError(f"Cannot verify GPU resource use because the observed GPU identity changed: {identities!r}.")
    uuid, name, total_mib = identities.pop()
    return {
        "uuid": uuid,
        "name": name,
        "total_mib": total_mib,
        "observations": len(run_observations),
        "peak_used_mib": max(row["used_mib"] for row in run_observations),
        "peak_utilization_percent": max(row["utilization_percent"] for row in run_observations),
    }


def verify_index(index, index_config: dict, expected_dimension: int, expected_rows: int) -> dict:
    """Compare the reloaded index with the configured HNSW-PQ retrieval contract."""

    require_equal("accept a candidate index with a different metric", index_config["metric"], "inner_product")
    require_equal("accept a candidate index with a different type", type(index), faiss.IndexHNSWPQ)
    require_equal("accept an index with a different dimension", index.d, expected_dimension)
    require_equal("accept an index with a different metric", index.metric_type, faiss.METRIC_INNER_PRODUCT)
    require_equal("accept an untrained index", index.is_trained, True)
    require_equal("accept an index with a different row count", index.ntotal, expected_rows)
    require_equal("accept an index with a different efConstruction", index.hnsw.efConstruction, index_config["ef_construction"])
    require_equal("accept an index with a different efSearch", index.hnsw.efSearch, index_config["ef_search"])
    storage = faiss.downcast_index(index.storage)
    require_equal("accept candidate index storage with a different type", type(storage), faiss.IndexPQ)
    require_equal("accept candidate index storage with a different metric", storage.metric_type, faiss.METRIC_INNER_PRODUCT)
    require_equal("accept an HNSW index with a different neighbor count", index.hnsw.nb_neighbors(1), index_config["hnsw_neighbors"])
    require_equal(
        "accept an HNSW index with a different level-zero neighbor count",
        index.hnsw.nb_neighbors(0),
        2 * index_config["hnsw_neighbors"],
    )
    require_equal("accept a product quantizer with a different subquantizer count", storage.pq.M, index_config["pq_subquantizers"])
    require_equal("accept a product quantizer with a different code width", storage.pq.nbits, index_config["pq_bits"])
    return {
        "type": type(index).__name__,
        "dimension": index.d,
        "rows": index.ntotal,
        "metric": "inner_product",
        "hnsw_neighbors": index.hnsw.nb_neighbors(1),
        "hnsw_level_zero_neighbors": index.hnsw.nb_neighbors(0),
        "pq_subquantizers": storage.pq.M,
        "pq_bits": storage.pq.nbits,
        "pq_code_bytes_per_vector": storage.code_size,
        "ef_construction": index.hnsw.efConstruction,
        "ef_search": index.hnsw.efSearch,
    }


def verify_search(
    index,
    checkpoint_manifest: Path,
    molformer_lock: Path,
    frontend_spectrum: Path,
    compute_dtype: str,
    smiles_batch_size: int,
) -> dict:
    inference = SecsInference.load(
        checkpoint_manifest,
        molformer_lock=molformer_lock,
        device="cuda:0",
        compute_dtype={"float32": torch.float32, "bfloat16": torch.bfloat16}[compute_dtype],
        smiles_batch_size=smiles_batch_size,
    )
    spectrum = read_json(frontend_spectrum)["intensities"]
    query = np.asarray(inference.embed_spectrum(spectrum), dtype=np.float32).reshape(1, -1)
    norm = np.linalg.norm(query, axis=1, keepdims=True)
    if not np.isfinite(query).all() or np.any(norm == 0):
        raise ValueError("Cannot search the reloaded index because the real-spectrum embedding is not finite and nonzero.")
    query /= norm
    result_count = min(100, index.ntotal)
    scores, identifiers = index.search(query, result_count)
    result_scores = scores[0]
    result_identifiers = identifiers[0]
    if (
        not np.isfinite(result_scores).all()
        or np.any(result_identifiers < 0)
        or np.any(result_identifiers >= index.ntotal)
        or len(set(map(int, result_identifiers))) != result_count
    ):
        raise ValueError(
            "Cannot verify real-spectrum search because results must have finite scores and unique in-range row identifiers."
        )
    return {
        "fixture": frontend_spectrum.name,
        "fixture_sha256": sha256(frontend_spectrum),
        "results": result_count,
        "top_identifier": int(result_identifiers[0]),
        "top_score": float(result_scores[0]),
        "minimum_score": float(result_scores[-1]),
    }


def verify_profile(args: argparse.Namespace) -> None:
    """Verify one disposable builder output and bind its evidence to one measured run."""

    plan = load_plan(args.qualification_spec)
    profile = getattr(plan, args.profile)
    samples = read_json(args.samples_receipt)
    require_equal("use a sampling receipt from another qualification kind", samples["kind"], QUALIFICATION_KIND)
    require_equal("use a drifted qualification specification", samples["qualification_spec"]["sha256"], sha256(args.qualification_spec))
    require_equal("use a drifted candidate specification", samples["candidate_spec"]["sha256"], sha256(args.candidate_spec))
    require_equal("use a sampling receipt for a different admitted source digest", samples["source"]["sha256"], plan.source_sha256)
    sample = samples["profiles"][args.profile]
    profile_rows = selected_row_count(samples["source"]["rows"], profile)
    require_equal("verify a profile with a different sample row count", sample["rows"], profile_rows)
    require_equal(
        "verify a profile with a different source-row selection",
        sample["selection"],
        {"source_modulus": profile.modulus, "source_remainder": profile.remainder},
    )

    candidate_spec = read_toml(args.candidate_spec)
    index_config = candidate_spec["index"]
    builder_manifest_path = args.bundle / "manifest.json"
    builder_manifest = read_json(builder_manifest_path)
    require_equal("accept a builder manifest with a different schema", builder_manifest["schema_version"], 3)
    require_equal("accept a builder manifest from a different builder", builder_manifest["builder_sha256"], sha256(args.builder))
    require_equal("accept a builder manifest from a different package image", builder_manifest["package_image_id"], args.package_image_id)
    require_equal("accept a builder manifest with a different candidate spec", builder_manifest["candidate_spec"]["sha256"], sha256(args.candidate_spec))
    require_equal("accept a builder manifest with a different sampled source digest", builder_manifest["source"]["member_sha256"], sample["sha256"])
    require_equal("accept a builder manifest with a different sampled source size", builder_manifest["source"]["member_bytes"], sample["bytes"])
    require_equal("accept a builder manifest with a different sampled source schema", builder_manifest["source"]["schema"], sample["schema"])
    require_equal(
        "accept a builder manifest with a different configured member",
        builder_manifest["source"]["member"],
        candidate_spec["archive"]["member"],
    )
    require_equal("accept a builder manifest with a different source kind", builder_manifest["source"]["acquisition"], "local")
    require_equal("accept a builder manifest with a different compute dtype", builder_manifest["embedding"]["compute_dtype"], args.compute_dtype)
    require_equal(
        "accept a builder manifest with a different index input dtype",
        builder_manifest["embedding"]["index_input_dtype"],
        "float32",
    )
    require_equal("accept a builder manifest with a different normalization", builder_manifest["embedding"]["normalization"], "l2")
    require_equal("accept a builder manifest with a different training row count", builder_manifest["index"]["training_rows"], min(index_config["training_rows"], profile_rows))
    require_equal("accept a builder manifest with a different training seed", builder_manifest["index"]["training_seed"], index_config["training_seed"])
    require_equal("accept a builder manifest with a different thread count", builder_manifest["index"]["threads"], args.threads)
    require_equal("accept a builder manifest with a different table row count", builder_manifest["table"]["rows"], profile_rows)

    checkpoint_manifest = read_json(args.checkpoint_manifest)
    require_equal("accept a builder manifest from a different checkpoint receipt", builder_manifest["checkpoint"]["manifest_sha256"], sha256(args.checkpoint_manifest))
    require_equal("accept a builder manifest from a different checkpoint specification", builder_manifest["checkpoint"]["spec_sha256"], checkpoint_manifest["spec"]["sha256"])
    require_equal("accept a builder manifest from different checkpoint weights", builder_manifest["checkpoint"]["weights_sha256"], checkpoint_manifest["weights"]["sha256"])
    checkpoint_spec_path = args.checkpoint_manifest.parent / checkpoint_manifest["spec"]["file"]
    checkpoint_spec = read_toml(checkpoint_spec_path)
    expected_dimension = checkpoint_spec["model"]["projection_heads"]["smiles"]["dims"][-1]
    require_equal("accept a builder manifest with a different embedding dimension", builder_manifest["embedding"]["dimension"], expected_dimension)
    require_equal("accept a builder manifest from a different MolFormer lock", builder_manifest["molformer"]["lock_sha256"], sha256(args.molformer_lock))

    table_path = verified_artifact(args.bundle, builder_manifest["table"], "verify the serialized candidate table")
    index_path = verified_artifact(args.bundle, builder_manifest["index"], "verify the serialized candidate index")
    table_schema, table_rows, table_nulls = table_statistics(pl.scan_parquet(table_path))
    require_equal("accept a candidate table with a different schema", table_schema, builder_manifest["table"]["schema"])
    require_equal("accept a candidate table with a different row count", table_rows, profile_rows)
    require_equal("accept a candidate table with null candidate values", table_nulls, {column: 0 for column in CANDIDATE_COLUMNS})
    require_equal(
        "accept a candidate table whose logical row order differs from its sample",
        candidate_row_sha256(table_path, candidate_spec["table"]["batch_rows"]),
        sample["candidate_row_sha256"],
    )

    reloaded_index = faiss.read_index(str(index_path))
    index_receipt = verify_index(reloaded_index, index_config, expected_dimension, profile_rows)
    for fact in (
        "type",
        "metric",
        "hnsw_neighbors",
        "pq_subquantizers",
        "pq_bits",
        "pq_code_bytes_per_vector",
        "ef_construction",
        "ef_search",
        "rows",
    ):
        require_equal(
            f"accept a builder manifest that misstates the serialized index {fact}",
            builder_manifest["index"][fact],
            index_receipt[fact],
        )
    search_receipt = verify_search(
        reloaded_index,
        args.checkpoint_manifest,
        args.molformer_lock,
        args.frontend_spectrum,
        args.compute_dtype,
        candidate_spec["embedding"]["batch_size"],
    )
    metrics = read_json(args.metrics)
    require_equal("accept metrics from a failed builder container", metrics["exit_status"], 0)
    if metrics["memory_peak_bytes"] <= 0 or metrics["memory_limit_bytes"] <= 0:
        raise ValueError(f"Cannot verify builder memory use from {args.metrics}: cgroup byte counts must be positive.")
    expected_metrics = {
        "run_id": args.run_id,
        "builder_manifest_sha256": sha256(builder_manifest_path),
    }
    for name, expected in expected_metrics.items():
        require_equal(f"accept run metrics with a different {name}", metrics[name], expected)
    started_at = parsed_utc(metrics["started_at"])
    finished_at = parsed_utc(metrics["finished_at"])
    if finished_at <= started_at or metrics["elapsed_nanoseconds"] <= 0:
        raise ValueError(f"Cannot verify builder timing from {args.metrics}: the bound run must have positive duration.")

    report = {
        "kind": QUALIFICATION_KIND,
        "profile": args.profile,
        "verified_at": utc_now(),
        "samples_receipt_sha256": sha256(args.samples_receipt),
        "builder": {"file": args.builder.name, "sha256": sha256(args.builder)},
        "qualification_tool": {"file": Path(__file__).name, "sha256": sha256(Path(__file__))},
        "package_image_id": args.package_image_id,
        "checkpoint_manifest_sha256": sha256(args.checkpoint_manifest),
        "molformer_lock_sha256": sha256(args.molformer_lock),
        "artifacts": {
            "table": builder_manifest["table"],
            "index": builder_manifest["index"],
        },
        "index": index_receipt,
        "search": search_receipt,
        "builder_run": metrics,
        "logs": {
            "builder": file_receipt(args.builder_log),
            "gpu": file_receipt(args.gpu_log),
        },
        "progress": progress_summary(args.builder_log, profile_rows, started_at, finished_at),
        "gpu": gpu_summary(args.gpu_log, started_at, finished_at),
    }
    write_json(args.output, report)


def linear_projection(first_rows: int, first_value: int, second_rows: int, second_value: int, target_rows: int) -> int:
    """Extrapolate beyond the larger observation and round fractional units upward."""

    if second_rows <= first_rows or second_value <= first_value or target_rows <= second_rows:
        raise ValueError(
            "Cannot project the production build because rows, measured values, and the target must increase strictly."
        )
    numerator = (second_value - first_value) * (target_rows - first_rows)
    return first_value + math.ceil(numerator / (second_rows - first_rows))


def qualification_projection(
    plan: QualificationPlan,
    functional: dict,
    scale: dict,
    functional_rows: int,
    scale_rows: int,
    target_rows: int,
    disk_free_bytes: int,
) -> dict:
    """Project from sampling-receipt row counts and enforce the predeclared resource margins."""

    memory_limit = functional["builder_run"]["memory_limit_bytes"]
    require_equal("combine profiles with different memory limits", scale["builder_run"]["memory_limit_bytes"], memory_limit)
    projected_memory = linear_projection(
        functional_rows,
        functional["builder_run"]["memory_peak_bytes"],
        scale_rows,
        scale["builder_run"]["memory_peak_bytes"],
        target_rows,
    )
    memory_gate = math.floor(memory_limit * plan.maximum_memory_fraction)
    if projected_memory >= memory_gate:
        raise ValueError(
            f"Cannot qualify the production candidate build: the two-profile linear projection is "
            f"{projected_memory} bytes, which does not stay below the predeclared {memory_gate}-byte memory gate."
        )
    projected_index_bytes = linear_projection(
        functional_rows,
        functional["artifacts"]["index"]["bytes"],
        scale_rows,
        scale["artifacts"]["index"]["bytes"],
        target_rows,
    )
    projected_table_bytes = linear_projection(
        functional_rows,
        functional["artifacts"]["table"]["bytes"],
        scale_rows,
        scale["artifacts"]["table"]["bytes"],
        target_rows,
    )
    projected_artifact_bytes = projected_index_bytes + projected_table_bytes
    disk_gate = math.floor(disk_free_bytes * plan.maximum_disk_fraction)
    if projected_artifact_bytes >= disk_gate:
        raise ValueError(
            f"Cannot qualify the production candidate build: projected table and index bytes total "
            f"{projected_artifact_bytes}, which does not stay below the predeclared {disk_gate}-byte disk gate."
        )
    return {
        "target_rows": target_rows,
        "model": "line through the functional and scale observations",
        "memory_peak_bytes": projected_memory,
        "memory_limit_bytes": memory_limit,
        "memory_gate_fraction": plan.maximum_memory_fraction,
        "memory_gate_bytes": memory_gate,
        "index_bytes": projected_index_bytes,
        "table_bytes": projected_table_bytes,
        "artifact_bytes": projected_artifact_bytes,
        "production_filesystem_free_bytes": disk_free_bytes,
        "disk_gate_fraction": plan.maximum_disk_fraction,
        "disk_gate_bytes": disk_gate,
        "builder_elapsed_nanoseconds": linear_projection(
            functional_rows,
            functional["builder_run"]["elapsed_nanoseconds"],
            scale_rows,
            scale["builder_run"]["elapsed_nanoseconds"],
            target_rows,
        ),
    }


def write_receipt_command(args: argparse.Namespace) -> None:
    """Publish a pass only from two identity-matched reports after disposable artifacts are gone."""

    plan = load_plan(args.qualification_spec)
    samples = read_json(args.samples_receipt)
    functional = read_json(args.functional_report)
    scale = read_json(args.scale_report)
    require_equal("use a sampling receipt from another qualification kind", samples["kind"], QUALIFICATION_KIND)
    require_equal(
        "use a stale sampling receipt after the qualification specification changed",
        samples["qualification_spec"]["sha256"],
        sha256(args.qualification_spec),
    )
    require_equal("use a sampling receipt for a different admitted source digest", samples["source"]["sha256"], plan.source_sha256)
    for name, report in (("functional", functional), ("scale", scale)):
        require_equal("combine a report from another qualification kind", report["kind"], QUALIFICATION_KIND)
        require_equal("combine a report for the wrong profile", report["profile"], name)
        require_equal("combine reports from different sampling receipts", report["samples_receipt_sha256"], sha256(args.samples_receipt))
        verified_artifact(args.evidence_directory, report["logs"]["builder"], f"publish a changed {name} builder log")
        verified_artifact(args.evidence_directory, report["logs"]["gpu"], f"publish a changed {name} GPU log")
    require_equal("combine reports from different package images", functional["package_image_id"], scale["package_image_id"])
    require_equal("combine reports from different builders", functional["builder"], scale["builder"])
    require_equal("combine reports from different qualification tools", functional["qualification_tool"], scale["qualification_tool"])
    require_equal("combine reports after the qualification tool changed", functional["qualification_tool"]["sha256"], sha256(Path(__file__)))
    require_equal("combine reports from different checkpoint manifests", functional["checkpoint_manifest_sha256"], scale["checkpoint_manifest_sha256"])
    require_equal("combine reports after the checkpoint manifest changed", functional["checkpoint_manifest_sha256"], sha256(args.checkpoint_manifest))
    require_equal("combine reports from different MolFormer locks", functional["molformer_lock_sha256"], scale["molformer_lock_sha256"])
    require_equal("combine reports after the MolFormer lock changed", functional["molformer_lock_sha256"], sha256(args.molformer_lock))
    require_equal("combine reports from different GPUs", functional["gpu"]["uuid"], scale["gpu"]["uuid"])
    require_equal(
        "combine reports from different qualification runs",
        functional["builder_run"]["run_id"],
        scale["builder_run"]["run_id"],
    )
    require_equal(
        "combine reports under a different qualification run",
        functional["builder_run"]["run_id"],
        args.run_id,
    )
    retained_paths = [path for path in args.discarded_path if path.exists() or path.is_symlink()]
    if retained_paths:
        raise ValueError(
            f"Cannot publish the qualification receipt because disposable build artifacts remain: {retained_paths!r}."
        )
    disk_free_bytes = shutil.disk_usage(args.production_output_directory).free
    projection = qualification_projection(
        plan,
        functional,
        scale,
        samples["profiles"]["functional"]["rows"],
        samples["profiles"]["scale"]["rows"],
        samples["source"]["rows"],
        disk_free_bytes,
    )
    receipt = {
        "kind": QUALIFICATION_KIND,
        "result": "passed",
        "completed_at": utc_now(),
        "repository_revision": args.repository_revision,
        "run_id": args.run_id,
        "claim": (
            "The exact production builder completed, serialized, reloaded, and searched two fixed nested "
            "samples derived from the admitted real candidate member."
        ),
        "limitations": [
            f"The {samples['source']['rows']:,}-row production candidate bundle was not built by this qualification.",
            "The memory, index-size, and elapsed-time projections are two-point linear extrapolations, not guarantees.",
            "This proof does not remove late HNSW slowdown, nonlinear memory growth, final I/O, host interruption, or non-resumability risk.",
            "The disposable sampled builder manifests are not production archive provenance.",
        ],
        "qualification_spec": samples["qualification_spec"],
        "candidate_spec": samples["candidate_spec"],
        "source": samples["source"],
        "samples": samples["profiles"],
        "disposable_artifacts": {
            "retained": False,
            "checked_paths": [path.name for path in args.discarded_path],
        },
        "implementation": {
            "builder": functional["builder"],
            "qualification_tool": functional["qualification_tool"],
            "package_image_id": functional["package_image_id"],
            "checkpoint_manifest_sha256": sha256(args.checkpoint_manifest),
            "molformer_lock_sha256": sha256(args.molformer_lock),
        },
        "profiles": {"functional": functional, "scale": scale},
        "projection": projection,
    }
    write_json(args.output, receipt)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser("sample-source")
    sample.add_argument("--source", type=Path, required=True)
    sample.add_argument("--qualification-spec", type=Path, required=True)
    sample.add_argument("--candidate-spec", type=Path, required=True)
    sample.add_argument("--output-directory", type=Path, required=True)
    sample.set_defaults(run=sample_source)

    verify = subparsers.add_parser("verify-profile")
    verify.add_argument("--profile", choices=("functional", "scale"), required=True)
    verify.add_argument("--qualification-spec", type=Path, required=True)
    verify.add_argument("--samples-receipt", type=Path, required=True)
    verify.add_argument("--candidate-spec", type=Path, required=True)
    verify.add_argument("--checkpoint-manifest", type=Path, required=True)
    verify.add_argument("--molformer-lock", type=Path, required=True)
    verify.add_argument("--builder", type=Path, required=True)
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--frontend-spectrum", type=Path, required=True)
    verify.add_argument("--metrics", type=Path, required=True)
    verify.add_argument("--builder-log", type=Path, required=True)
    verify.add_argument("--gpu-log", type=Path, required=True)
    verify.add_argument("--compute-dtype", choices=("float32", "bfloat16"), required=True)
    verify.add_argument("--threads", type=int, required=True)
    verify.add_argument("--package-image-id", required=True)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--output", type=Path, required=True)
    verify.set_defaults(run=verify_profile)

    receipt = subparsers.add_parser("write-receipt")
    receipt.add_argument("--qualification-spec", type=Path, required=True)
    receipt.add_argument("--samples-receipt", type=Path, required=True)
    receipt.add_argument("--functional-report", type=Path, required=True)
    receipt.add_argument("--scale-report", type=Path, required=True)
    receipt.add_argument("--evidence-directory", type=Path, required=True)
    receipt.add_argument("--checkpoint-manifest", type=Path, required=True)
    receipt.add_argument("--molformer-lock", type=Path, required=True)
    receipt.add_argument("--repository-revision", required=True)
    receipt.add_argument("--run-id", required=True)
    receipt.add_argument("--discarded-path", type=Path, action="append", required=True)
    receipt.add_argument("--production-output-directory", type=Path, required=True)
    receipt.add_argument("--output", type=Path, required=True)
    receipt.set_defaults(run=write_receipt_command)
    return parser.parse_args()


if __name__ == "__main__":
    parsed_arguments = arguments()
    parsed_arguments.run(parsed_arguments)
