"""Exercise scoring and one GA generation with a shared full-index GPU runtime."""

import hashlib
import json
from pathlib import Path
import unittest

import faiss
import numpy as np
from rdkit import Chem
import torch

from secs.elucidation import FaissCandidateSource, GraphGAOptimizer, ScoreOnlyOptimizer
from secs.elucidation.caching import TrajectoryCallback
from secs.elucidation.optimizers.base import OptimizerResult
from secs.utils.elucidation import smiles_to_molecular_formula
from secs_inference.elucidation import NoStartingCandidates, SecsElucidator
from secs_inference.model import SecsInference
from secs_inference.spectra.bruker import read_bruker_pdata
from secs_inference.spectra.secs import prepare_secs_spectrum


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
        cls.inference = SecsInference.load(
            "/checkpoint/manifest.json",
            molformer_lock="/input/molformer.lock.toml",
            device="cuda:0",
            compute_dtype=torch.bfloat16,
            smiles_batch_size=256,
        )
        cls.candidate_source = FaissCandidateSource.from_files(
            "/checkpoint/candidates/smiles.faiss",
            "/checkpoint/candidates/candidates.parquet",
            n_neighbours=100_000,
        )
        cls.elucidator = SecsElucidator(
            cls.inference,
            cls.candidate_source,
            ScoreOnlyOptimizer(),
            initial_population_size=512,
        )
        cls.cases = fixture_set["cases"]

    def test_bruker_full_index_runs_one_graph_ga_generation(self) -> None:
        """Require new molecules to be scored, not recovery of a known answer."""
        source = read_bruker_pdata(Path("/fixtures/bruker/F3697/1/pdata/1"))
        spectrum = prepare_secs_spectrum(source)
        trajectory = TrajectoryCallback()
        # Keep refinement small: this proves offspring scoring, not search quality.
        elucidator = SecsElucidator(
            self.inference,
            self.candidate_source,
            GraphGAOptimizer(
                population_size=32,
                offspring_size=32,
                max_generations=1,
                seed=42,
                callbacks=[trajectory],
            ),
            initial_population_size=32,
        )

        # The title identifies strychnine; PubChem CID 441071 gives C21H22N2O2.
        # https://pubchem.ncbi.nlm.nih.gov/compound/441071
        result = elucidator.elucidate(spectrum, "C21H22N2O2")

        self.assertIsInstance(result, OptimizerResult)
        self.assertEqual(result.generations, 1)
        self.assertTrue(result.population)
        self.assertGreater(result.n_evaluated, trajectory.history[0]["n_evaluated"])
        self.assertTrue(all(np.isfinite(score) for _, score in result.all_scored))
        scores = [score for _, score in result.population]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_full_index_score_only_baseline(self) -> None:
        self.assertEqual(len(self.cases), 20)

        for case in self.cases:
            with self.subTest(id=case["id"], formula=case["formula"]):
                spectrum_document = json.loads(
                    (FIXTURES / f"{case['id']}.json").read_bytes()
                )
                spectrum = np.asarray(spectrum_document["y"], dtype=np.float32)

                result = self.elucidator.elucidate(spectrum, case["formula"])
                # Empty retrieval means no recovered structure in this baseline.
                population = [] if isinstance(result, NoStartingCandidates) else result.population
                rank = rank_expected_structure(
                    population,
                    case["formula"],
                    case["expected_smiles"],
                )

                self.assertEqual(rank, case["score_only_rank"])


if __name__ == "__main__":
    unittest.main()
