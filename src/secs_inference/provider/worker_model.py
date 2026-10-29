"""Load warm scientific state in the offline child, not the API controller."""

import argparse
from dataclasses import MISSING, asdict, dataclass, fields
import json
import math
import os
import socket
from pathlib import Path
import sys
import tomllib
from typing import TYPE_CHECKING
from provider_core.startup_file import StartupFileRejected, read_startup_file
from secs_inference.provider.configuration_error import ConfigurationError
from secs_inference.provider.diagnostics import exception_evidence
from secs_inference.provider.outcomes import AnalysisOutcome, NO_STARTING_CANDIDATES_MESSAGE
from secs_inference.provider.formula_notation import normalize_formula_notation

if TYPE_CHECKING:
    from secs_inference.provider.input_adapter import PreparedSpectrum


@dataclass(frozen=True, slots=True)
class _PreparedSearch:
    handle: int
    attempt_ref: str
    spectrum: "PreparedSpectrum"
    formula: str


@dataclass(frozen=True, slots=True)
class ScientificWorkerConfig:
    checkpoint_directory: str
    molformer_lock: str
    device: str = "cuda:0"
    compute_dtype: str = "bfloat16"
    smiles_batch_size: int = 256
    initial_population_size: int = 512
    neighbours: int = 100000
    threads: int = 8
    population_size: int = 512
    offspring_size: int = 1024
    max_generations: int = 10
    seed: int = 42

    def __post_init__(self):
        for name in ("checkpoint_directory", "molformer_lock", "device"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ConfigurationError(f"Worker {name} must be a nonempty string")
        for name in ("smiles_batch_size", "initial_population_size", "neighbours", "threads",
                     "population_size", "offspring_size", "max_generations"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ConfigurationError(f"Worker {name} must be a positive integer")
        if not isinstance(self.compute_dtype, str) or self.compute_dtype not in {"float32", "bfloat16"}:
            raise ConfigurationError("Worker compute_dtype must be float32 or bfloat16")
        if type(self.seed) is not int or self.seed < 0:
            raise ConfigurationError("Worker seed must be a nonnegative integer")

    def __call__(self):
        """Materialize the configured model/index once per supervised child."""
        import faiss
        import torch
        from secs.elucidation import FaissCandidateSource
        from secs_inference.model import SecsInference

        faiss.omp_set_num_threads(self.threads)
        torch.set_num_threads(self.threads)
        root = Path(self.checkpoint_directory)
        inference = SecsInference.load(
            root / "manifest.json", molformer_lock=self.molformer_lock,
            device=self.device, compute_dtype={"float32": torch.float32, "bfloat16": torch.bfloat16}[self.compute_dtype],
            smiles_batch_size=self.smiles_batch_size,
        )
        candidates = FaissCandidateSource.from_files(
            root / "candidates/smiles.faiss", root / "candidates/candidates.parquet",
            n_neighbours=self.neighbours,
        )
        return ScientificHandler(inference, candidates, self)


class ScientificHandler:
    """Run explicit reads and inference; translate only owner-identified input errors."""

    def __init__(self, inference, candidates, config: ScientificWorkerConfig):
        from secs_inference.provider.input_adapter import InputAdapter

        self.inference = inference
        self.candidates = candidates
        self.config = config
        self.input_adapter = InputAdapter()
        self._prepared = None
        self._next_handle = 0

    def __call__(self, command: dict) -> dict:
        from secs_inference.elucidation import FormulaError
        from secs_inference.provider.input_operations import SourceRef
        from secs_inference.provider.source_access import InputReadError, SourceAccess
        from secs_inference.spectra.errors import SpectrumReadError

        if command["operation"] == "execute":
            prepared = self._prepared
            if (prepared is None or command["attempt_ref"] != prepared.attempt_ref
                    or type(command.get("prepared_handle")) is not int
                    or command["prepared_handle"] != prepared.handle):
                raise ValueError("Cannot execute prepared input: no matching prepared handle belongs to this Attempt")
            # Consumption precedes model dispatch. Even an unexpected scientific
            # failure cannot make this request eligible for another search.
            self._prepared = None
            analysis = self._execute(prepared.spectrum, prepared.formula)
            outcome = (AnalysisOutcome.NO_STARTING_CANDIDATES
                       if analysis["search"]["outcome"] == AnalysisOutcome.NO_STARTING_CANDIDATES
                       else AnalysisOutcome.ANALYSED)
            return {"outcome": outcome.value, "analysis": analysis}
        if self._prepared is not None:
            raise ValueError("Cannot prepare or inspect input: this worker still owns an unconsumed prepared request")
        try:
            access = SourceAccess(_read_upload_snapshots(command), Path(command["directory"]))
            if command["operation"] == "inspect":
                facts = self.input_adapter.discover(
                    access,
                    command["attempt_ref"],
                    SourceRef(**command["source"]),
                )
                return {"outcome": "inspected", "facts": facts}
            if command["operation"] != "prepare":
                raise AssertionError("No scientific operation is bound to the worker request")
            from secs_inference.elucidation import canonical_formula
            spectrum = self.input_adapter.prepare(access, command["attempt_ref"], command["selection"])
            formula = canonical_formula(normalize_formula_notation(command["selection"]["formula"]))
            spectrum.values.flags.writeable = False
            self._next_handle += 1
            self._prepared = _PreparedSearch(self._next_handle, command["attempt_ref"], spectrum, formula)
            return {"outcome": "prepared", "prepared_handle": self._prepared.handle}
        except (InputReadError, SpectrumReadError, FormulaError) as error:
            return {"outcome": "input_rejected", "reason": str(error)[:2048]}

    def _execute(self, prepared, formula):
        """Search the exact admitted in-memory input without reopening sources."""
        from secs.elucidation import GraphGAOptimizer
        from secs_inference.elucidation import SecsElucidator
        from secs_inference.spectra.secs import SECS_PPM_FROM, SECS_PPM_TO, SECS_SPECTRUM_POINTS

        optimizer = GraphGAOptimizer(
            population_size=self.config.population_size, offspring_size=self.config.offspring_size,
            max_generations=self.config.max_generations, seed=self.config.seed,
        )
        elucidator = SecsElucidator(self.inference, self.candidates, optimizer,
                                  initial_population_size=self.config.initial_population_size)
        result = elucidator.elucidate(prepared.values, formula)
        # This provider's canonical JSON excludes floating-point numbers. Scientific
        # decimal values travel as text; counts and discrete settings stay integers.
        if result.optimization is None:
            candidates = []
            search = {
                "outcome": AnalysisOutcome.NO_STARTING_CANDIDATES.value, "generations": 0, "evaluated": 0,
                "explanation": NO_STARTING_CANDIDATES_MESSAGE,
            }
        else:
            if not result.optimization.population:
                raise RuntimeError("Cannot produce an elucidation result: Graph GA returned an empty population after receiving starting candidates")
            candidates = []
            for smiles, score in result.optimization.population:
                score = float(score)
                if not math.isfinite(score):
                    raise RuntimeError("Cannot rank the elucidation candidates: Graph GA returned a nonfinite score")
                candidates.append({"smiles": smiles, "score": str(score)})
            search = {"outcome": "optimized", "generations": result.optimization.generations, "evaluated": result.optimization.n_evaluated}
        retrieval = {"starting_candidates": len(result.proposal.smiles)}
        if result.proposal.retrieval is not None:
            retrieval.update(asdict(result.proposal.retrieval))
        return {
            "candidates": candidates,
            "retrieval": retrieval,
            "search": {**search, "optimizer": "graph_ga",
                       "initial_population_size": self.config.initial_population_size,
                       "population_size": self.config.population_size, "offspring_size": self.config.offspring_size,
                       "max_generations": self.config.max_generations, "seed": self.config.seed},
            "preparation": {"operation": "SECS resampling and min-max normalization",
                            "ppm_from": str(SECS_PPM_FROM), "ppm_to": str(SECS_PPM_TO), "points": SECS_SPECTRUM_POINTS,
                            "representation_id": prepared.representation_id,
                            "from_fid": prepared.from_fid, "magnitude": prepared.magnitude,
                            "sources": [asdict(source) for source in prepared.sources]},
            "inference": {"device": self.config.device, "compute_dtype": self.config.compute_dtype,
                          "smiles_batch_size": self.config.smiles_batch_size, "retrieval_neighbours": self.config.neighbours},
        }


def _read_upload_snapshots(command):
    """Read each granted file once per request; parsers receive checked immutable bytes."""
    from provider_core.upload_file import VerifiedUploadFile, read_upload_file

    receipts = {ref: VerifiedUploadFile(**document) for ref, document in command["receipts"].items()}
    if any(ref != receipt.upload_ref for ref, receipt in receipts.items()):
        raise ValueError("Scientific source receipt does not match its Upload reference")
    maximum_total = command["max_total_bytes"]
    if type(maximum_total) is not int or maximum_total <= 0:
        raise ValueError("Scientific sources require an explicit positive aggregate byte limit")
    if sum(receipt.byte_length for receipt in receipts.values()) > maximum_total:
        raise ValueError("Scientific source receipts exceed the Attempt's aggregate byte limit")
    descriptor = os.open(command["directory"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return {ref: read_upload_file(receipt, directory_fd=descriptor,
                                      maximum_bytes=command["max_upload_bytes"]).data
                for ref, receipt in receipts.items()}
    finally:
        os.close(descriptor)


def main(arguments=None) -> int:
    """Serve science on inherited descriptors; the SDK owns the child lifetime."""
    from secs_inference.provider.worker_protocol import WorkerError, serve_scientific_session

    parser = argparse.ArgumentParser(description="Serve SECS scientific requests on the SDK-granted socket")
    parser.add_argument("--session-fd", type=int, required=True)
    parser.add_argument("--lease-fd", type=int, required=True)
    parser.add_argument("--operation", required=True)
    options = parser.parse_args(arguments)

    path = Path("/run/config/worker/worker.toml")
    operation = f"reading scientific worker configuration from {path}"
    try:
        try:
            raw = read_startup_file(path, maximum_bytes=65536, label="Scientific worker configuration")
        except StartupFileRejected as error:
            raise ConfigurationError(str(error)) from error
        operation = f"parsing scientific worker configuration from {path}"
        try:
            document = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ConfigurationError("Worker configuration must be UTF-8 text with valid TOML syntax") from error
        operation = f"configuring the scientific worker from {path}"
        try:
            config = ScientificWorkerConfig(**document)
        except TypeError as error:
            settings = fields(ScientificWorkerConfig)
            required = ", ".join(setting.name for setting in settings if setting.default is MISSING)
            allowed = ", ".join(setting.name for setting in settings)
            raise ConfigurationError(f"Worker configuration requires {required}; supported fields are: {allowed}") from error
        operation = "adopting the inherited scientific socket and lifetime lease"
        if options.session_fd < 0 or options.lease_fd < 0 or options.session_fd == options.lease_fd:
            raise ConfigurationError("Scientific session and lifetime lease require distinct open descriptors")
        # Keep the inherited lease open until process exit, including model work.
        # It grants no controller/API authority and is never a scientific message.
        os.fstat(options.lease_fd)
        connection = socket.socket(fileno=options.session_fd)
        operation = "serving the loaded scientific handler"
        serve_scientific_session(connection, config)
    except Exception as error:
        # Configuration and scientific protocol errors own their explanations. Parser
        # and library exception text can instead echo arbitrary supplied values.
        def owned_details(cause):
            if type(cause) is StartupFileRejected:
                details = {}
                if cause.cause is not None:
                    details["read_cause"] = exception_evidence(cause.cause)
                if cause.cleanup_failures:
                    details["close_failures"] = [exception_evidence(item) for item in cause.cleanup_failures]
                return details
            return {"message": str(cause)} if isinstance(cause, (ConfigurationError, WorkerError)) else {}
        evidence = exception_evidence(error, boundary_details=owned_details)
        reason = evidence.get("message") or evidence.get("reason") or "an unexpected internal error occurred"
        print(f"Scientific worker stopped while {operation}: {reason}.", file=sys.stderr)
        print(json.dumps(evidence), file=sys.stderr)
        print("Correct the failure before restarting the worker; no scientific requests will be accepted by this child.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
