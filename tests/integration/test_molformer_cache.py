import os
import unittest


class MolformerCacheTest(unittest.TestCase):
    def test_cached_definition_and_tokenizer_support_offline_inference(self):
        """Pinned cached assets suffice without IBM base weights or network access."""
        self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
        self.assertEqual(os.environ.get("TRANSFORMERS_OFFLINE"), "1")

        import torch

        from secs.data.components.secs_tokenizers import SMILES_TOKENIZER
        from secs.models.encoders.smiles.molformer import MolformerEncoder

        tokens = SMILES_TOKENIZER("CCO", return_tensors="pt")
        encoder = MolformerEncoder(pretrained=False).eval()

        with torch.inference_mode():
            embedding = encoder((tokens["input_ids"], tokens["attention_mask"]))

        self.assertEqual(tuple(embedding.shape), (1, 768))
        self.assertTrue(torch.isfinite(embedding).all().item())


if __name__ == "__main__":
    unittest.main()
