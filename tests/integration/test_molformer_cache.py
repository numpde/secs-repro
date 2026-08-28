import tomllib
import unittest
from pathlib import Path


class MolFormerCacheTest(unittest.TestCase):
    def test_locked_snapshot_matches_secs_and_supports_stable_repeated_forward(self):
        with Path("/input/molformer.lock.toml").open("rb") as source:
            snapshot = tomllib.load(source)["snapshot"]

        from secs.models.encoders.smiles.molformer import (
            MOLFORMER_CHECKPOINT,
            MOLFORMER_REVISION,
            MolformerEncoder,
        )

        locked_snapshot = (snapshot["repository"], snapshot["revision"])
        secs_snapshot = (MOLFORMER_CHECKPOINT, MOLFORMER_REVISION)
        self.assertEqual(secs_snapshot, locked_snapshot)

        import torch

        from secs.data.components.secs_tokenizers import SMILES_TOKENIZER

        tokens = SMILES_TOKENIZER("CCO", return_tensors="pt")
        encoder = MolformerEncoder(pretrained=False).eval()

        with torch.inference_mode():
            embedding = encoder((tokens["input_ids"], tokens["attention_mask"]))
            repeated_embedding = encoder((tokens["input_ids"], tokens["attention_mask"]))

        self.assertEqual(tuple(embedding.shape), (1, encoder.output_dim))
        self.assertTrue(torch.isfinite(embedding).all().item())
        torch.testing.assert_close(repeated_embedding, embedding)


if __name__ == "__main__":
    unittest.main()
