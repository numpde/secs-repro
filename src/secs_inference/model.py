from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from typing import Sequence
import tomllib

import numpy as np
from numpy.typing import NDArray
import torch
from torch.nn import functional as F


FloatArray = NDArray[np.float32]
_HASH_READ_BYTES = 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(_HASH_READ_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SecsInference:
    """Inference boundary for one converted SECS checkpoint.

    Spectrum inputs use the public application's ascending-ppm grid; this
    boundary max-normalizes and reverses them into the model's training order.
    The checkpoint owns the expected spectrum and token lengths. The container
    must admit the MolFormer cache and configure Hugging Face before loading.
    """

    def __init__(
        self,
        model,
        tokenizer,
        device: torch.device,
        compute_dtype: torch.dtype,
        spectrum_points: int,
        smiles_context_length: int,
        smiles_batch_size: int,
    ) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self._device = device
        self._compute_dtype = compute_dtype
        self._spectrum_points = spectrum_points
        self._smiles_context_length = smiles_context_length
        self._smiles_batch_size = smiles_batch_size

    @classmethod
    def load(
        cls,
        checkpoint_manifest: str | Path,
        *,
        molformer_lock: str | Path,
        device: str,
        compute_dtype: torch.dtype,
        smiles_batch_size: int,
    ) -> SecsInference:
        """Load a checkpoint with Float32 parameters and Float32 or BFloat16 forward compute."""
        if smiles_batch_size <= 0:
            raise ValueError("smiles_batch_size must be positive.")
        if compute_dtype not in (torch.float32, torch.bfloat16):
            raise ValueError(
                f"Cannot load SECS inference with {compute_dtype = }; "
                f"the compute dtype must be float32 or bfloat16."
            )
        compute_device = torch.device(device)

        manifest_path = Path(checkpoint_manifest)
        manifest = json.loads(manifest_path.read_text())
        checkpoint_directory = manifest_path.parent
        specification_path = checkpoint_directory / manifest["spec"]["file"]
        weights_path = checkpoint_directory / manifest["weights"]["file"]

        for artifact_name, artifact_path in (
            ("spec", specification_path),
            ("weights", weights_path),
        ):
            expected_sha256 = manifest[artifact_name]["sha256"]
            actual_sha256 = _sha256(artifact_path)
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    f"Checkpoint artifact '{artifact_name}' does not match {manifest_path.name}: "
                    f"expected SHA-256 {expected_sha256}, got {actual_sha256}."
                )

        with specification_path.open("rb") as source:
            specification = tomllib.load(source)
        spectrum_points = specification["model"]["encoders"]["h_nmr"]["input_length"]
        smiles_context_length = specification["inputs"]["smiles"]["context_length"]
        with Path(molformer_lock).open("rb") as source:
            locked_snapshot = tomllib.load(source)["snapshot"]

        from secs.models.encoders.smiles.molformer import MOLFORMER_CHECKPOINT, MOLFORMER_REVISION

        required_molformer_snapshot = MOLFORMER_CHECKPOINT, MOLFORMER_REVISION
        admitted_molformer_snapshot = locked_snapshot["repository"], locked_snapshot["revision"]
        if required_molformer_snapshot != admitted_molformer_snapshot:
            raise ValueError(
                f"SECS requires MolFormer {required_molformer_snapshot}, "
                f"but the admitted snapshot is {admitted_molformer_snapshot}."
            )

        from omegaconf import OmegaConf
        from safetensors.torch import load_file

        # Importing the tokenizer loads trusted remote code from the admitted cache,
        # so the revision comparison above must happen before this import.
        from secs.data.components.secs_tokenizers import SMILES_TOKENIZER
        from secs.models import MolBind

        # The checkpoint's storage dtype does not define its runtime parameter
        # dtype. MolFormer pooling returns Float32, so keeping parameters in
        # Float32 avoids an incompatible LayerNorm boundary while autocast
        # controls the eligible reduced-precision forward operations.
        model = MolBind(OmegaConf.create(specification)).to(device=compute_device)
        weights = load_file(weights_path, device="cpu")
        model.load_state_dict(weights, strict=True)
        model.eval()
        return cls(
            model,
            SMILES_TOKENIZER,
            compute_device,
            compute_dtype,
            spectrum_points,
            smiles_context_length,
            smiles_batch_size,
        )

    def _autocast_context(self):
        if self._compute_dtype == torch.float32:
            return nullcontext()
        return torch.autocast(device_type=self._device.type, dtype=self._compute_dtype)

    @torch.inference_mode()
    def embed_spectrum(self, spectrum: Sequence[float] | FloatArray) -> FloatArray:
        values = np.asarray(spectrum, dtype=np.float32)
        if values.shape != (self._spectrum_points,):
            raise ValueError(f"Expected {self._spectrum_points} spectrum intensities, got shape {values.shape}.")
        maximum_intensity = values.max()
        if not np.isfinite(values).all() or maximum_intensity <= 0:
            raise ValueError("Spectrum intensities must be finite and contain a positive signal.")

        normalized = values / maximum_intensity
        model_order = normalized[::-1].copy()

        model_input = torch.from_numpy(model_order).to(device=self._device).reshape(1, 1, -1)
        with self._autocast_context():
            embedding = self._model.encode_modality(model_input, modality="h_nmr")
        return embedding.squeeze(0).float().cpu().numpy()

    @torch.inference_mode()
    def embed_smiles(self, smiles: list[str]) -> FloatArray:
        batch_embeddings = []
        for start in range(0, len(smiles), self._smiles_batch_size):
            tokens = self._tokenizer(
                smiles[start : start + self._smiles_batch_size],
                padding="max_length",
                truncation=True,
                return_tensors="pt",
                max_length=self._smiles_context_length,
            )
            model_input = tokens["input_ids"].to(self._device), tokens["attention_mask"].to(self._device)
            with self._autocast_context():
                embedding = self._model.encode_modality(model_input, modality="smiles")
            batch_embeddings.append(embedding.float().cpu())
        return torch.cat(batch_embeddings).numpy()

    def rank(self, spectrum: Sequence[float] | FloatArray, candidates: list[str]) -> list[tuple[str, float]]:
        if not candidates:
            return []
        spectrum_embedding = torch.from_numpy(self.embed_spectrum(spectrum)).unsqueeze(0)
        candidate_embeddings = torch.from_numpy(self.embed_smiles(candidates))
        scores = F.cosine_similarity(spectrum_embedding, candidate_embeddings, dim=1).tolist()
        return sorted(zip(candidates, scores, strict=True), key=lambda candidate: candidate[1], reverse=True)
