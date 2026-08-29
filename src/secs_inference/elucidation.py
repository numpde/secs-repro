from collections.abc import Sequence

import torch

from secs.elucidation.candidates import CandidateSource
from secs.elucidation.components import spectral_objective
from secs.elucidation.optimizers.base import MoleculeOptimizer, OptimizerResult
from secs.utils.elucidation import get_atom_counts_from_formula

from secs_inference.model import FloatArray, SecsInference


class _InferenceSmilesEmbedder:
    def __init__(self, inference: SecsInference) -> None:
        self._inference = inference

    def encode(self, smiles: list[str]) -> dict[str, torch.Tensor]:
        return {"h_nmr": torch.from_numpy(self._inference.embed_smiles(smiles))}


class SecsElucidator:
    """Compose retrieval, SECS scoring, and molecular search for one deployment."""

    def __init__(
        self,
        inference: SecsInference,
        candidate_source: CandidateSource,
        optimizer: MoleculeOptimizer,
        *,
        initial_population_size: int,
    ) -> None:
        self._inference = inference
        self._candidate_source = candidate_source
        self._optimizer = optimizer
        self._initial_population_size = initial_population_size

    def elucidate(self, spectrum: Sequence[float] | FloatArray, formula: str) -> OptimizerResult:
        target = torch.from_numpy(self._inference.embed_spectrum(spectrum))
        initial_population = self._candidate_source.propose(
            target,
            formula,
            self._initial_population_size,
        )
        objective = spectral_objective(
            _InferenceSmilesEmbedder(self._inference),
            {"h_nmr": target},
            get_atom_counts_from_formula(formula),
        )
        return self._optimizer.run(initial_population, objective)
