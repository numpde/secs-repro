"""Load warm scientific state in the offline child, not the API controller."""

from dataclasses import dataclass
from pathlib import Path
import traceback


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
        for name in ("smiles_batch_size", "initial_population_size", "neighbours", "threads",
                     "population_size", "offspring_size", "max_generations"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"Worker {name} must be a positive integer")
        if self.compute_dtype not in {"float32", "bfloat16"}:
            raise ValueError("Worker compute_dtype must be float32 or bfloat16")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("Worker seed must be a nonnegative integer")

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
        except Exception as error:
            # No exception text, source data or locals cross the diagnostic
            # boundary. Frame locations still let the operator find the fault.
            return {"outcome": "failed", "exception_type": type(error).__name__,
                    "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                               for frame in traceback.extract_tb(error.__traceback__)]}

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


def main() -> None:
    """Read the worker's own scientific settings without controller credentials."""
    import tomllib
    from secs_inference.provider.worker import serve_worker

    with Path("/run/config/worker/worker.toml").open("rb") as stream:
        config = ScientificWorkerConfig(**tomllib.load(stream))
    serve_worker(Path("/run/secs/worker/worker.sock"), config)


if __name__ == "__main__":
    main()
