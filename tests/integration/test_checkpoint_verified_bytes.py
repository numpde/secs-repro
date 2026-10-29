"""Checkpoint decoding uses the bytes that passed the manifest digest."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import tomllib
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import torch
from safetensors.torch import load, save

from secs_inference.model import SecsInference, _verified_artifact


class VerifiedCheckpointTests(unittest.TestCase):
    def test_loader_decodes_verified_weights_after_source_is_replaced(self):
        original = save({"value": torch.tensor([2.0])})
        replacement = save({"value": torch.tensor([9.0])})
        specification = b"[model.encoders.h_nmr]\ninput_length = 2\n[inputs.smiles]\ncontext_length = 2\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weights, spec, lock, manifest = (root / name for name in ("weights", "spec.toml", "lock.toml", "manifest.json"))
            weights.write_bytes(original)
            spec.write_bytes(specification)
            lock.write_text('[snapshot]\nrepository = "fixture"\nrevision = "fixture-revision"\n')
            manifest.write_text(json.dumps({
                "spec": {"file": spec.name, "sha256": hashlib.sha256(specification).hexdigest()},
                "weights": {"file": weights.name, "bytes": len(original), "sha256": hashlib.sha256(original).hexdigest()},
            }))
            model = Mock()
            model.to.return_value = model
            molformer = ModuleType("secs.models.encoders.smiles.molformer")
            molformer.MOLFORMER_CHECKPOINT, molformer.MOLFORMER_REVISION = "fixture", "fixture-revision"
            tokenizers = ModuleType("secs.data.components.secs_tokenizers")
            tokenizers.SMILES_TOKENIZER = object()
            models = ModuleType("secs.models")
            models.MolBind = lambda _config: model
            def replace_then_load(data):
                weights.write_bytes(replacement)
                return load(data)
            parse = tomllib.loads
            def replace_then_parse(data, *args, **kwargs):
                if data.startswith("[model.encoders.h_nmr]"):
                    spec.write_bytes(specification.replace(b"input_length = 2", b"input_length = 9"))
                return parse(data, *args, **kwargs)
            with (patch.dict(sys.modules, {molformer.__name__: molformer, tokenizers.__name__: tokenizers,
                                          models.__name__: models}),
                  patch("safetensors.torch.load", side_effect=replace_then_load),
                  patch("secs_inference.model.tomllib.loads", side_effect=replace_then_parse)):
                inference = SecsInference.load(manifest, molformer_lock=lock, device="cpu", compute_dtype=torch.float32, smiles_batch_size=1)
            loaded = model.load_state_dict.call_args.args[0]
            self.assertEqual(loaded["value"].item(), 2.0)
            self.assertEqual(load(weights.read_bytes())["value"].item(), 9.0)
            self.assertEqual(inference._spectrum_points, 2)
            self.assertIn(b"input_length = 9", spec.read_bytes())

    def test_declared_weight_size_bounds_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.safetensors"
            path.write_bytes(b"oversized")
            with self.assertRaisesRegex(ValueError, "declared size"):
                _verified_artifact(path, "0" * 64, "weights", 2, "manifest.json")


if __name__ == "__main__":
    unittest.main()
