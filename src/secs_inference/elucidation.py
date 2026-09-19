"""Compose formula parsing, candidate retrieval and molecular refinement."""

from collections.abc import Sequence
from dataclasses import dataclass

import torch

from secs.elucidation.candidates import CandidateProposal, CandidateSource
from secs.elucidation.components import spectral_objective
from secs.elucidation.optimizers.base import MoleculeOptimizer, OptimizerResult
from secs.utils.elucidation import build_formula_string, get_atom_counts_from_formula

from secs_inference.model import FloatArray, SecsInference


class FormulaError(ValueError):
    """The formula parser rejected the supplied text before model work."""


@dataclass(frozen=True, slots=True)
class ElucidationResult:
    """Per-call retrieval evidence and optional refinement.

    Optimization is absent only when retrieval supplied no starting molecules.
    """

    proposal: CandidateProposal
    optimization: OptimizerResult | None


class _HnmrCandidateEmbedder:
    """Expose this checkpoint's SMILES embeddings to the H-NMR objective."""

    def __init__(self, inference: SecsInference) -> None:
        self._inference = inference

    def encode(self, smiles: list[str]) -> dict[str, torch.Tensor]:
        return {"h_nmr": torch.from_numpy(self._inference.embed_smiles(smiles))}


class SecsElucidator:
    """Retrieve, score, and evolve candidates for an H-NMR spectrum and formula."""

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

    def elucidate(
        self, spectrum: Sequence[float] | FloatArray, formula: str
    ) -> ElucidationResult:
        """Return retrieval evidence and any refinement that ran.

        Malformed formulas are rejected before model work. Empty retrieval is
        a search limitation, not evidence that the supplied formula is invalid.
        """

        try:
            target_atom_counts = get_atom_counts_from_formula(formula)
        except ValueError as cause:
            raise FormulaError(str(cause)) from cause
        canonical_formula = build_formula_string(target_atom_counts)
        spectrum_embedding = torch.from_numpy(self._inference.embed_spectrum(spectrum))

        proposal = self._candidate_source.propose(
            spectrum_embedding,
            canonical_formula,
            self._initial_population_size,
        )
        if not proposal.smiles:
            # Keep empty retrieval distinct from an optimizer that ran and
            # returned no survivors; callers must report the observed stage.
            return ElucidationResult(proposal, None)

        candidate_embedder = _HnmrCandidateEmbedder(self._inference)
        objective = spectral_objective(
            candidate_embedder,
            {"h_nmr": spectrum_embedding},
            target_atom_counts,
        )

        return ElucidationResult(proposal, self._optimizer.run(proposal.smiles, objective))
