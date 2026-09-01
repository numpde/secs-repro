"""Regress published spectra against one artifact-bound ScoreOnly runtime."""

import hashlib
import json
from pathlib import Path
import unittest

import faiss
import numpy as np
from rdkit import Chem
import torch

from secs.elucidation import FaissCandidateSource, ScoreOnlyOptimizer
from secs.utils.elucidation import smiles_to_molecular_formula
from secs_inference.elucidation import SecsElucidator
from secs_inference.model import SecsInference


FIXTURES = Path("/fixtures/challenges")


def canonical_smiles_without_stereochemistry(smiles: str) -> str:
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ValueError(f"Cannot canonicalize challenge SMILES {smiles!r}.")
    return Chem.MolToSmiles(molecule, isomericSmiles=False)


def rank_expected_structure(
    population: list[tuple[str, float]],
    formula: str,
    expected_smiles: str,
) -> int | None:
    """Rank the expected structure by exact formula, ignoring stereochemistry."""

    expected = canonical_smiles_without_stereochemistry(expected_smiles)
    matching_formula = (
        smiles
        for smiles, _score in population
        if smiles_to_molecular_formula(smiles) == formula
    )
    return next(
        (
            rank
            for rank, smiles in enumerate(matching_formula, start=1)
            if canonical_smiles_without_stereochemistry(smiles) == expected
        ),
        None,
    )


class PublishedChallengeTest(unittest.TestCase):
    """Share one model and full candidate index across all challenge cases."""

    @classmethod
    def setUpClass(cls) -> None:
        fixture_set = json.loads((FIXTURES / "cases.json").read_bytes())
        required_manifest_sha256 = fixture_set["candidate_manifest_sha256"]
        candidate_manifest = Path("/checkpoint/candidates/manifest.json")
        actual_manifest_sha256 = hashlib.sha256(candidate_manifest.read_bytes()).hexdigest()
        if actual_manifest_sha256 != required_manifest_sha256:
            raise RuntimeError(
                "Cannot run the published challenge baselines with candidate "
                f"manifest SHA-256 {actual_manifest_sha256}; cases.json requires "
                f"{required_manifest_sha256}."
            )

        faiss.omp_set_num_threads(8)
        inference = SecsInference.load(
            "/checkpoint/manifest.json",
            molformer_lock="/input/molformer.lock.toml",
            device="cuda:0",
            compute_dtype=torch.bfloat16,
            smiles_batch_size=256,
        )
        candidate_source = FaissCandidateSource.from_files(
            "/checkpoint/candidates/smiles.faiss",
            "/checkpoint/candidates/candidates.parquet",
            n_neighbours=100_000,
        )
        cls.elucidator = SecsElucidator(
            inference,
            candidate_source,
            ScoreOnlyOptimizer(),
            initial_population_size=512,
        )
        cls.cases = fixture_set["cases"]

    def test_full_index_score_only_baseline(self) -> None:
        self.assertEqual(len(self.cases), 20)

        for case in self.cases:
            with self.subTest(id=case["id"], formula=case["formula"]):
                spectrum_document = json.loads(
                    (FIXTURES / f"{case['id']}.json").read_bytes()
                )
                spectrum = np.asarray(spectrum_document["y"], dtype=np.float32)

                result = self.elucidator.elucidate(spectrum, case["formula"])
                rank = rank_expected_structure(
                    result.population,
                    case["formula"],
                    case["expected_smiles"],
                )

                self.assertEqual(rank, case["score_only_rank"])


if __name__ == "__main__":
    unittest.main()
