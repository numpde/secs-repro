import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import torch

from secs.elucidation import GraphGAOptimizer, StaticCandidateSource
from secs_inference import SecsInference
from secs_inference.elucidation import SecsElucidator


class SecsInferenceLoadTest(unittest.TestCase):
    def test_load_rejects_unsupported_compute_dtype_before_model_construction(self):
        with self.assertRaises(ValueError):
            SecsInference.load(
                "unused",
                molformer_lock="unused",
                device="cpu",
                compute_dtype=torch.float16,
                smiles_batch_size=1,
            )

    def test_load_rejects_weight_drift_before_model_construction(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_directory = Path(temporary_directory)
            specification = checkpoint_directory / "checkpoint.toml"
            weights = checkpoint_directory / "secs-v3.safetensors"
            specification.write_bytes(b"specification")
            weights.write_bytes(b"weights")
            manifest = checkpoint_directory / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "spec": {
                            "file": specification.name,
                            "sha256": hashlib.sha256(specification.read_bytes()).hexdigest(),
                        },
                        "weights": {"file": weights.name, "sha256": "0" * 64},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "Checkpoint artifact 'weights' does not match manifest.json"
            ):
                SecsInference.load(
                    manifest,
                    molformer_lock="unused",
                    device="cpu",
                    compute_dtype=torch.float32,
                    smiles_batch_size=1,
                )

    def test_bfloat16_compute_keeps_runtime_parameters_in_float32(self):
        inference = SecsInference.load(
            "/checkpoint/manifest.json",
            molformer_lock="/input/molformer.lock.toml",
            device="cpu",
            compute_dtype=torch.bfloat16,
            smiles_batch_size=1,
        )
        # The production BFloat16 path runs on CUDA, while a CPU BFloat16
        # forward exceeds this lane's 3 GiB limit. Inspecting parameters keeps
        # the whole-model-cast regression covered without treating CPU autocast
        # as evidence about CUDA execution.
        parameter_dtypes = {
            parameter.dtype for parameter in inference._model.parameters() if parameter.is_floating_point()
        }

        self.assertEqual(parameter_dtypes, {torch.float32})


class SecsIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inference = SecsInference.load(
            "/checkpoint/manifest.json",
            molformer_lock="/input/molformer.lock.toml",
            device="cpu",
            compute_dtype=torch.float32,
            smiles_batch_size=256,
        )

    def test_real_checkpoint_ranks_smiles_offline(self):
        spectrum = np.zeros(10_000, dtype=np.float32)
        spectrum[1_234] = 1.0
        candidates = ["CCO", "CCN"]

        spectrum_embedding = self.inference.embed_spectrum(spectrum)
        smiles_embeddings = self.inference.embed_smiles(candidates)
        ranked = self.inference.rank(spectrum, candidates)
        expected_scores = smiles_embeddings @ spectrum_embedding
        expected_scores /= np.linalg.norm(smiles_embeddings, axis=1) * np.linalg.norm(spectrum_embedding)
        scores_by_smiles = dict(zip(candidates, expected_scores, strict=True))
        expected_order = sorted(candidates, key=scores_by_smiles.__getitem__, reverse=True)

        self.assertEqual(spectrum_embedding.shape, (1024,))
        self.assertEqual(smiles_embeddings.shape, (2, 1024))
        self.assertEqual(spectrum_embedding.dtype, np.float32)
        self.assertEqual(smiles_embeddings.dtype, np.float32)
        self.assertTrue(np.isfinite(spectrum_embedding).all())
        self.assertTrue(np.isfinite(smiles_embeddings).all())
        self.assertEqual([smiles for smiles, _ in ranked], expected_order)
        np.testing.assert_allclose(
            [score for _, score in ranked],
            [scores_by_smiles[smiles] for smiles, _ in ranked],
            rtol=1e-5,
            atol=1e-6,
        )
        self.assertTrue(all(np.isfinite(score) for _, score in ranked))

    def test_real_checkpoint_runs_one_graph_ga_generation(self):
        spectrum = np.zeros(10_000, dtype=np.float32)
        spectrum[1_234] = 1.0
        initial_population = ["CCO", "COC", "CC", "CCC", "CCN", "CC=O", "C=C", "CO"]
        elucidator = SecsElucidator(
            self.inference,
            StaticCandidateSource(initial_population),
            GraphGAOptimizer(
                population_size=8,
                offspring_size=8,
                max_generations=1,
                seed=42,
            ),
            initial_population_size=len(initial_population),
        )

        result = elucidator.elucidate(spectrum, "C2H6O")

        self.assertTrue(result.population)
        self.assertEqual(result.generations, 1)
