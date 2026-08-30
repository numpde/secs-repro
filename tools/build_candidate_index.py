#!/usr/bin/env python3
"""Build the row-aligned SECS candidate table and FAISS index."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tomllib

import faiss
import numpy as np
import polars as pl
import torch

from secs_inference import SecsInference


READ_BYTES = 1024 * 1024
CANDIDATE_COLUMNS = ("smiles", "molecular_formula")
COMPUTE_DTYPE_BY_NAME = {
    "float32": torch.float32,
    "bfloat16": torch.bfloat16,
}
FAISS_METRIC_BY_NAME = {
    "inner_product": faiss.METRIC_INNER_PRODUCT,
}
FAISS_METRIC_NAME = {value: name for name, value in FAISS_METRIC_BY_NAME.items()}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-spec", type=Path, required=True)
    parser.add_argument("--candidate-spec", type=Path, required=True)
    parser.add_argument("--checkpoint-manifest", type=Path, required=True)
    parser.add_argument("--molformer-lock", type=Path, required=True)
    parser.add_argument("--scratch-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--source-kind", choices=("configured", "override", "local"), required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--compute-dtype", choices=COMPUTE_DTYPE_BY_NAME, required=True)
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--package-image-id", required=True)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(READ_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_input(destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    with destination.open("xb") as output:
        while chunk := sys.stdin.buffer.read(READ_BYTES):
            output.write(chunk)
            digest.update(chunk)
            byte_count += len(chunk)
    return byte_count, digest.hexdigest()


def normalized_embeddings(inference: SecsInference, smiles: list[str]) -> np.ndarray:
    embeddings = np.asarray(inference.embed_smiles(smiles), dtype=np.float32, order="C")
    if embeddings.ndim != 2 or not np.isfinite(embeddings).all():
        raise ValueError("SMILES encoder must return a finite two-dimensional embedding matrix.")
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("SMILES encoder returned a zero-length embedding.")
    embeddings /= norms
    return embeddings


def configured_metric(index_config: dict) -> int:
    """Resolve the supported metric before model loading or source publication."""

    metric_name = index_config["metric"]
    try:
        return FAISS_METRIC_BY_NAME[metric_name]
    except KeyError as error:
        raise ValueError(f"Unsupported FAISS metric: {metric_name!r}") from error


def configured_index(dimension: int, index_config: dict, metric: int):
    """Construct the owned HNSW-PQ structure with the configured retrieval metric."""

    # Faiss 1.13.2's generic factory silently constructs HNSW-PQ storage as L2
    # even when its metric argument requests inner product. The metric-aware
    # constructor makes the retrieval contract explicit at the owning boundary.
    index = faiss.IndexHNSWPQ(
        dimension,
        index_config["pq_subquantizers"],
        index_config["hnsw_neighbors"],
        index_config["pq_bits"],
        metric,
    )
    storage = faiss.downcast_index(index.storage)
    if type(index) is not faiss.IndexHNSWPQ or type(storage) is not faiss.IndexPQ:
        raise RuntimeError(
            f"FAISS constructed {type(index).__name__} with {type(storage).__name__} storage; "
            "candidate indexing requires IndexHNSWPQ with IndexPQ storage."
        )
    index.hnsw.efConstruction = index_config["ef_construction"]
    index.hnsw.efSearch = index_config["ef_search"]
    actual_structure = {
        "metric": index.metric_type,
        "storage_metric": storage.metric_type,
        "hnsw_neighbors": index.hnsw.nb_neighbors(1),
        "hnsw_level_zero_neighbors": index.hnsw.nb_neighbors(0),
        "pq_subquantizers": storage.pq.M,
        "pq_bits": storage.pq.nbits,
        "ef_construction": index.hnsw.efConstruction,
        "ef_search": index.hnsw.efSearch,
    }
    expected_structure = {
        "metric": metric,
        "storage_metric": metric,
        "hnsw_neighbors": index_config["hnsw_neighbors"],
        "hnsw_level_zero_neighbors": 2 * index_config["hnsw_neighbors"],
        "pq_subquantizers": index_config["pq_subquantizers"],
        "pq_bits": index_config["pq_bits"],
        "ef_construction": index_config["ef_construction"],
        "ef_search": index_config["ef_search"],
    }
    if actual_structure != expected_structure:
        raise RuntimeError(
            f"FAISS did not preserve the configured candidate-index structure: "
            f"got {actual_structure}, expected {expected_structure}."
        )
    return index


def stratified_training_smiles(
    table: pl.LazyFrame,
    row_count: int,
    sample_count: int,
    seed: int,
    batch_rows: int,
) -> list[str]:
    sample_count = min(sample_count, row_count)
    edges = np.arange(sample_count + 1, dtype=np.int64) * row_count // sample_count
    widths = np.diff(edges)
    random_offsets = np.floor(np.random.default_rng(seed).random(sample_count) * widths).astype(np.int64)
    positions = edges[:-1] + random_offsets

    selected: list[str] = []
    row_offset = 0
    for batch in table.select("smiles").collect_batches(
        chunk_size=batch_rows, maintain_order=True, engine="streaming"
    ):
        batch_end = row_offset + batch.height
        first = np.searchsorted(positions, row_offset, side="left")
        last = np.searchsorted(positions, batch_end, side="left")
        local_positions = positions[first:last] - row_offset
        selected.extend(batch["smiles"].gather(local_positions).to_list())
        row_offset = batch_end
    return selected


def build(args: argparse.Namespace) -> None:
    if args.threads <= 0:
        raise ValueError("threads must be positive")

    with args.archive_spec.open("rb") as source:
        archive_spec = tomllib.load(source)["archive"]
    with args.candidate_spec.open("rb") as source:
        candidate_spec = tomllib.load(source)

    table_config = candidate_spec["table"]
    embedding_config = candidate_spec["embedding"]
    index_config = candidate_spec["index"]
    index_metric = configured_metric(index_config)
    inference = SecsInference.load(
        args.checkpoint_manifest,
        molformer_lock=args.molformer_lock,
        device=args.device,
        compute_dtype=COMPUTE_DTYPE_BY_NAME[args.compute_dtype],
        smiles_batch_size=embedding_config["batch_size"],
    )

    args.output_directory.mkdir(parents=True, exist_ok=True)
    source_path = args.scratch_directory / "filtered_pubchem.parquet"
    print("Reading the admitted PubChem parquet from stdin.", file=sys.stderr, flush=True)
    source_bytes, source_sha256 = stage_input(source_path)

    source_table = pl.scan_parquet(source_path)
    source_schema_object = source_table.collect_schema()
    candidate_schema = {column: source_schema_object[column] for column in CANDIDATE_COLUMNS}
    if any(dtype != pl.String for dtype in candidate_schema.values()):
        raise ValueError(f"Candidate columns must be String; got {candidate_schema}.")
    source_schema = {name: str(dtype) for name, dtype in source_schema_object.items()}
    table_path = args.output_directory / "candidates.parquet"
    print("Publishing the ordered SMILES and molecular-formula table.", file=sys.stderr, flush=True)
    source_table.select(CANDIDATE_COLUMNS).sink_parquet(
        table_path,
        compression="zstd",
        maintain_order=True,
        engine="streaming",
    )
    table = pl.scan_parquet(table_path)
    table_schema = {name: str(dtype) for name, dtype in table.collect_schema().items()}
    statistics = table.select(
        pl.len().alias("rows"),
        *(pl.col(column).null_count().alias(column) for column in CANDIDATE_COLUMNS),
    ).collect(engine="streaming").row(0, named=True)
    row_count = statistics.pop("rows")
    if row_count == 0 or any(statistics.values()):
        raise ValueError(f"Candidate columns must contain rows without nulls; got rows={row_count}, nulls={statistics}.")
    source_path.unlink()

    training_smiles = stratified_training_smiles(
        table,
        row_count,
        index_config["training_rows"],
        index_config["training_seed"],
        table_config["batch_rows"],
    )
    print(f"Training FAISS from {len(training_smiles)} row-aligned embeddings.", file=sys.stderr, flush=True)
    training_embeddings = normalized_embeddings(inference, training_smiles)
    training_row_count = len(training_smiles)
    del training_smiles
    dimension = training_embeddings.shape[1]

    faiss.omp_set_num_threads(args.threads)
    index = configured_index(dimension, index_config, index_metric)
    index.train(training_embeddings)
    del training_embeddings

    indexed_rows = 0
    for batch in table.select("smiles").collect_batches(
        chunk_size=table_config["batch_rows"], maintain_order=True, engine="streaming"
    ):
        embeddings = normalized_embeddings(inference, batch["smiles"].to_list())
        index.add(embeddings)
        indexed_rows += batch.height
        if index.ntotal != indexed_rows:
            raise RuntimeError(f"FAISS indexed {index.ntotal} rows after reading {indexed_rows} rows.")
        print(f"Indexed {indexed_rows} of {row_count} candidate rows.", file=sys.stderr, flush=True)
    if indexed_rows != row_count:
        raise RuntimeError(f"Read {indexed_rows} candidate rows after publishing a {row_count}-row table.")

    index_path = args.output_directory / "smiles.faiss"
    faiss.write_index(index, str(index_path))
    index_storage = faiss.downcast_index(index.storage)
    checkpoint_manifest = json.loads(args.checkpoint_manifest.read_text())
    with args.molformer_lock.open("rb") as source:
        molformer_snapshot = tomllib.load(source)["snapshot"]
    manifest = {
        "schema_version": 2,
        "candidate_spec": {
            "checkpoint_file": args.candidate_spec.name,
            "sha256": sha256(args.candidate_spec),
        },
        "builder_sha256": sha256(Path(__file__)),
        "package_image_id": args.package_image_id,
        "source": {
            "acquisition": args.source_kind,
            "configured_archive_url": archive_spec["url"],
            "archive_md5": archive_spec["md5"],
            "member": candidate_spec["archive"]["member"],
            "member_bytes": source_bytes,
            "member_sha256": source_sha256,
            "schema": source_schema,
        },
        "checkpoint": {
            "manifest_sha256": sha256(args.checkpoint_manifest),
            "spec_sha256": checkpoint_manifest["spec"]["sha256"],
            "weights_sha256": checkpoint_manifest["weights"]["sha256"],
        },
        "molformer": {
            "lock_sha256": sha256(args.molformer_lock),
            "repository": molformer_snapshot["repository"],
            "revision": molformer_snapshot["revision"],
        },
        "table": {
            "file": table_path.name,
            "bytes": table_path.stat().st_size,
            "sha256": sha256(table_path),
            "rows": row_count,
            "schema": table_schema,
        },
        "embedding": {
            "dimension": dimension,
            "compute_dtype": args.compute_dtype,
            "storage_dtype": "float32",
            "normalization": "l2",
        },
        "index": {
            "file": index_path.name,
            "bytes": index_path.stat().st_size,
            "sha256": sha256(index_path),
            "type": type(index).__name__,
            "metric": FAISS_METRIC_NAME[index.metric_type],
            "hnsw_neighbors": index.hnsw.nb_neighbors(1),
            "pq_subquantizers": index_storage.pq.M,
            "pq_bits": index_storage.pq.nbits,
            "training_rows": training_row_count,
            "training_seed": index_config["training_seed"],
            "training_selection": "one seeded random row from each equal-width row stratum",
            "ef_construction": index.hnsw.efConstruction,
            "ef_search": index.hnsw.efSearch,
            "threads": args.threads,
            "rows": index.ntotal,
            "faiss_version": faiss.__version__,
        },
        "polars_version": pl.__version__,
    }
    (args.output_directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    build(arguments())
