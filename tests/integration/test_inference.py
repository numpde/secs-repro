import unittest

import numpy as np
import torch

from secs_inference import SecsInference


class InferenceTest(unittest.TestCase):
    def test_real_checkpoint_ranks_smiles_offline(self):
        inference = SecsInference.load(
            "/checkpoint/manifest.json",
            molformer_lock="/input/molformer.lock.toml",
            device="cpu",
            dtype=torch.float32,
            smiles_batch_size=256,
        )
        spectrum = np.zeros(10_000, dtype=np.float32)
        spectrum[1_234] = 1.0
        candidates = ["CCO", "CCN"]

        spectrum_embedding = inference.embed_spectrum(spectrum)
        smiles_embeddings = inference.embed_smiles(candidates)
        ranked = inference.rank(spectrum, candidates)
        expected_scores = smiles_embeddings @ spectrum_embedding
        expected_scores /= np.linalg.norm(smiles_embeddings, axis=1) * np.linalg.norm(spectrum_embedding)
        scores_by_smiles = dict(zip(candidates, expected_scores, strict=True))
        expected_order = sorted(candidates, key=scores_by_smiles.__getitem__, reverse=True)

        self.assertEqual(spectrum_embedding.shape, (1024,))
        self.assertEqual(smiles_embeddings.shape, (2, 1024))
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
