from __future__ import annotations

from pathlib import Path
from typing import Sequence
import tomllib

import numpy as np
from numpy.typing import NDArray
import torch
from torch.nn import functional as F


FloatArray = NDArray[np.float32]
SPECTRUM_POINTS = 10_000
SMILES_CONTEXT_LENGTH = 128


class SecsInference:
    """Inference boundary for one converted SECS checkpoint.

    Spectrum inputs are 10,000 intensities on the public application's
    ascending-ppm grid; this boundary max-normalizes and reverses them into the
    model's training order. The container must admit the MolFormer cache and
    configure Hugging Face before loading.
    """

    def __init__(
        self,
        model,
        tokenizer,
        device: torch.device,
        dtype: torch.dtype,
        smiles_batch_size: int,
    ) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self._device = device
        self._dtype = dtype
        self._smiles_batch_size = smiles_batch_size

    @classmethod
    def load(
        cls,
        checkpoint_directory: str | Path,
        *,
        molformer_lock: str | Path,
        device: str,
        dtype: torch.dtype,
        smiles_batch_size: int,
    ) -> SecsInference:
        if smiles_batch_size <= 0:
            raise ValueError("smiles_batch_size must be positive.")
        compute_device = torch.device(device)

        checkpoint_directory = Path(checkpoint_directory)
        with (checkpoint_directory / "checkpoint.toml").open("rb") as source:
            specification = tomllib.load(source)
        with Path(molformer_lock).open("rb") as source:
            locked_snapshot = tomllib.load(source)["snapshot"]

        from secs.models.encoders.smiles.molformer import MOLFORMER_CHECKPOINT, MOLFORMER_REVISION

        secs_snapshot = MOLFORMER_CHECKPOINT, MOLFORMER_REVISION
        admitted_snapshot = locked_snapshot["repository"], locked_snapshot["revision"]
        if secs_snapshot != admitted_snapshot:
            raise ValueError(f"SECS requires MolFormer {secs_snapshot}, but the admitted snapshot is {admitted_snapshot}.")

        from omegaconf import OmegaConf
        from safetensors.torch import load_file
        from secs.data.components.secs_tokenizers import SMILES_TOKENIZER
        from secs.models import MolBind

        model = MolBind(OmegaConf.create(specification)).to(device=compute_device, dtype=dtype)
        state = load_file(checkpoint_directory / "secs-v3.safetensors", device="cpu")
        model.load_state_dict(state, strict=True)
        model.eval()
        return cls(
            model,
            SMILES_TOKENIZER,
            compute_device,
            dtype,
            smiles_batch_size,
        )

    @torch.inference_mode()
    def embed_spectrum(self, spectrum: Sequence[float] | FloatArray) -> FloatArray:
        values = np.asarray(spectrum, dtype=np.float32)
        if values.shape != (SPECTRUM_POINTS,):
            raise ValueError(f"Expected {SPECTRUM_POINTS} spectrum intensities, got shape {values.shape}.")
        maximum = values.max()
        if not np.isfinite(values).all() or maximum <= 0:
            raise ValueError("Spectrum intensities must be finite and contain a positive signal.")

        normalized = values / maximum
        model_order = normalized[::-1].copy()

        tensor = torch.from_numpy(model_order).to(device=self._device, dtype=self._dtype).reshape(1, 1, -1)
        embedding = self._model.encode_modality(tensor, modality="h_nmr")
        return embedding.squeeze(0).float().cpu().numpy()

    @torch.inference_mode()
    def embed_smiles(self, smiles: list[str]) -> FloatArray:
        embeddings = []
        for start in range(0, len(smiles), self._smiles_batch_size):
            tokens = self._tokenizer(
                smiles[start : start + self._smiles_batch_size],
                padding="max_length",
                truncation=True,
                return_tensors="pt",
                max_length=SMILES_CONTEXT_LENGTH,
            )
            inputs = tokens["input_ids"].to(self._device), tokens["attention_mask"].to(self._device)
            embeddings.append(self._model.encode_modality(inputs, modality="smiles").float().cpu())
        return torch.cat(embeddings).numpy()

    def rank(self, spectrum: Sequence[float] | FloatArray, candidates: list[str]) -> list[tuple[str, float]]:
        if not candidates:
            return []
        spectrum_embedding = torch.from_numpy(self.embed_spectrum(spectrum)).unsqueeze(0)
        candidate_embeddings = torch.from_numpy(self.embed_smiles(candidates))
        scores = F.cosine_similarity(spectrum_embedding, candidate_embeddings, dim=1).tolist()
        return sorted(zip(candidates, scores, strict=True), key=lambda candidate: candidate[1], reverse=True)
