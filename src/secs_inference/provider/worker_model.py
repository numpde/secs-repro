"""Load warm scientific state in the offline child, not the API controller."""

from dataclasses import MISSING, dataclass, fields
import json
from pathlib import Path
import sys
import tomllib
from secs_inference.provider.configuration_error import ConfigurationError
from secs_inference.provider.diagnostics import exception_evidence


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
        self.inference = inference
        self.candidates = candidates
        self.config = config

    def __call__(self, command: dict) -> dict:
        from secs_inference.elucidation import FormulaError
        from secs_inference.provider.input_operations import SourceRef
        from secs_inference.provider.source_access import InputReadError, SourceAccess
        from secs_inference.spectra.errors import SpectrumReadError

        try:
            access = SourceAccess({ref: Path(path) for ref, path in command["files"].items()}, Path(command["directory"]))
            if command["operation"] == "inspect":
                return {"outcome": "inspected", "facts": access.inspect(SourceRef(**command["source"]))}
            if command["operation"] != "analyse":
                raise AssertionError("No scientific operation is bound to the worker request")
            return {"outcome": "analysed", "analysis": self._analyse(access, command["selection"])}
        except (InputReadError, SpectrumReadError, FormulaError) as error:
            return {"outcome": "input_rejected", "reason": str(error)[:2048]}

    def _analyse(self, access, document):
        """Read the chosen input and report refinement or observed empty retrieval."""
        from secs.elucidation import GraphGAOptimizer
        from secs_inference.elucidation import NoStartingCandidates, SecsElucidator
        from secs_inference.provider.input_operations import BrukerSelection, JcampSelection, SourceRef
        from secs_inference.provider.spectrum_input import prepare_selected_spectrum
        from secs_inference.spectra.secs import SECS_PPM_FROM, SECS_PPM_TO, SECS_SPECTRUM_POINTS

        if document["reader"] == "jcamp":
            selection = JcampSelection(SourceRef(**document["source"]), document["formula"], document["explanation"])
        elif document["reader"] == "bruker":
            selection = BrukerSelection(document["upload_ref"], document["pdata_directory"], document["formula"], document["explanation"])
        else:
            raise AssertionError("No reader is bound to the selected scientific representation")
        spectrum = prepare_selected_spectrum(access, selection)
        optimizer = GraphGAOptimizer(
            population_size=self.config.population_size, offspring_size=self.config.offspring_size,
            max_generations=self.config.max_generations, seed=self.config.seed,
        )
        elucidator = SecsElucidator(self.inference, self.candidates, optimizer,
                                  initial_population_size=self.config.initial_population_size)
        result = elucidator.elucidate(spectrum, selection.formula)
        # This provider's canonical JSON excludes floating-point numbers. Scientific
        # decimal values travel as text; counts and discrete settings stay integers.
        if isinstance(result, NoStartingCandidates):
            candidates = []
            search = {
                "outcome": "no_starting_candidates", "generations": 0, "evaluated": 0,
                "explanation": "Candidate retrieval returned no starting molecules under the configured search. "
                               "Graph GA was not run. This does not establish that the formula is invalid "
                               "or that no matching structure exists.",
            }
        else:
            candidates = [{"smiles": smiles, "score": str(float(score))} for smiles, score in result.population]
            search = {"outcome": "optimized", "generations": result.generations, "evaluated": result.n_evaluated}
        return {
            "candidates": candidates,
            "search": {**search, "optimizer": "graph_ga",
                       "initial_population_size": self.config.initial_population_size,
                       "population_size": self.config.population_size, "offspring_size": self.config.offspring_size,
                       "max_generations": self.config.max_generations, "seed": self.config.seed},
            "preparation": {"operation": "SECS resampling and min-max normalization",
                            "ppm_from": str(SECS_PPM_FROM), "ppm_to": str(SECS_PPM_TO), "points": SECS_SPECTRUM_POINTS},
            "inference": {"device": self.config.device, "compute_dtype": self.config.compute_dtype,
                          "smiles_batch_size": self.config.smiles_batch_size, "retrieval_neighbours": self.config.neighbours},
        }


def main() -> int:
    """Report worker startup/supervision failures without controller credentials."""
    from secs_inference.provider.worker import WorkerError, serve_worker

    path = Path("/run/config/worker/worker.toml")
    operation = f"reading scientific worker configuration from {path}"
    try:
        raw = path.read_bytes()
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
        operation = "running the scientific worker supervisor"
        serve_worker(Path("/run/secs/worker/worker.sock"), config)
    except Exception as error:
        # Configuration and supervisor errors own their explanations. Parser
        # and library exception text can instead echo arbitrary supplied values.
        def owned_details(cause):
            return {"message": str(cause)} if isinstance(cause, (ConfigurationError, WorkerError)) else {}
        evidence = exception_evidence(error, boundary_details=owned_details)
        reason = evidence.get("message") or evidence.get("reason") or "an unexpected internal error occurred"
        print(f"Scientific worker stopped while {operation}: {reason}.", file=sys.stderr)
        print(json.dumps(evidence), file=sys.stderr)
        print("Correct the failure before restarting the worker; no model work will be accepted by this supervisor.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
