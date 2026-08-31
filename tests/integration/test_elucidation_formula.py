from __future__ import annotations

import unittest

import numpy as np

from secs.elucidation.optimizers.base import OptimizerResult
from secs_inference.elucidation import SecsElucidator


class _Inference:
    def __init__(self) -> None:
        self.spectrum_calls = 0

    def embed_spectrum(self, _spectrum: object) -> np.ndarray:
        self.spectrum_calls += 1
        return np.array([1.0, 0.0], dtype=np.float32)


class _Candidates:
    def __init__(self) -> None:
        self.formulas: list[str] = []

    def propose(
        self,
        _embedding: object,
        formula: str,
        _population_size: int,
    ) -> list[str]:
        self.formulas.append(formula)
        return ["CCO"]


class _Optimizer:
    def run(self, population: list[str], _objective: object) -> OptimizerResult:
        return OptimizerResult(population=[(smiles, 0.0) for smiles in population])


class ElucidationFormulaTests(unittest.TestCase):
    def test_canonical_formula_reaches_candidate_retrieval(self) -> None:
        inference = _Inference()
        candidates = _Candidates()
        elucidator = SecsElucidator(
            inference,
            candidates,
            _Optimizer(),
            initial_population_size=32,
        )

        elucidator.elucidate([0.0], "H6C2O")

        self.assertEqual(candidates.formulas, ["C2H6O"])

    def test_malformed_formula_is_rejected_before_model_or_candidate_work(self) -> None:
        inference = _Inference()
        candidates = _Candidates()
        elucidator = SecsElucidator(
            inference,
            candidates,
            _Optimizer(),
            initial_population_size=32,
        )

        with self.assertRaises(ValueError):
            elucidator.elucidate([0.0], "C2H6O followed by prose")

        self.assertEqual(inference.spectrum_calls, 0)
        self.assertEqual(candidates.formulas, [])


if __name__ == "__main__":
    unittest.main()
