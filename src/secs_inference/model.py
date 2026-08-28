from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
import tomllib

import numpy as np
from numpy.typing import NDArray
import torch
from torch.nn import functional as F


FloatArray = NDArray[np.float32]
DTYPES = {
    "float32": torch.float32,
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
}
SPECTRUM_POINTS = 10_000
SMILES_CONTEXT_LENGTH = 128


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    smiles: str
    score: float


class SecsInference:
    """Inference boundary for one converted SECS checkpoint.

    Spectrum inputs are 10,000 intensities on the ascending-ppm grid used by
    the public SECS application. This boundary max-normalizes and reverses them
    into the order used to train the H-NMR encoder.

    The caller must admit the MolFormer cache and configure Hugging Face before
    loading. In the maintained container lane, hash verification precedes this
    call and Docker owns network denial.
    """

    def __init__(
        self,
        model,
        tokenizer,
        embedding_size: int,
        device: torch.device,
        dtype: torch.dtype,
        smiles_batch_size: int,
    ) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.embedding_size = embedding_size
        self.device = device
        self.dtype = dtype
        self.smiles_batch_size = smiles_batch_size

    @classmethod
    def load(
        cls,
        checkpoint_directory: str | Path,
        *,
        molformer_lock: str | Path,
        device: str,
        dtype: str,
        smiles_batch_size: int = 256,
    ) -> SecsInference:
        if dtype not in DTYPES:
            raise ValueError(f"dtype must be one of {', '.join(DTYPES)}, got {dtype!r}.")
        if smiles_batch_size <= 0:
            raise ValueError("smiles_batch_size must be positive.")
        compute_dtype = DTYPES[dtype]
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

        model = MolBind(OmegaConf.create(specification)).to(device=compute_device, dtype=compute_dtype)
        state = load_file(checkpoint_directory / "secs-v3.safetensors", device="cpu")
        model.load_state_dict(state, strict=True)
        model.eval().requires_grad_(False)
        embedding_size = specification["model"]["projection_heads"]["smiles"]["dims"][-1]
        return cls(
            model,
            SMILES_TOKENIZER,
            embedding_size,
            compute_device,
            compute_dtype,
            smiles_batch_size,
        )

    def embed_spectrum(self, spectrum: Sequence[float] | FloatArray) -> FloatArray:
        values = np.asarray(spectrum, dtype=np.float32)
        if values.shape != (SPECTRUM_POINTS,):
            raise ValueError(f"Expected {SPECTRUM_POINTS} spectrum intensities, got shape {values.shape}.")
        maximum = values.max()
        if not np.isfinite(values).all() or maximum <= 0:
            raise ValueError("Spectrum intensities must be finite and contain a positive signal.")

        values = (values / maximum)[::-1].copy()

        tensor = torch.from_numpy(values).to(device=self.device, dtype=self.dtype).reshape(1, 1, -1)
        with torch.inference_mode():
            embedding = self.model.encode_modality(tensor, modality="h_nmr")
        return embedding.squeeze(0).float().cpu().numpy()

    def embed_smiles(self, smiles: list[str]) -> FloatArray:
        if not smiles:
            return np.empty((0, self.embedding_size), dtype=np.float32)
        embeddings = []
        with torch.inference_mode():
            for start in range(0, len(smiles), self.smiles_batch_size):
                tokens = self.tokenizer(
                    smiles[start : start + self.smiles_batch_size],
                    padding="max_length",
                    truncation=True,
                    return_tensors="pt",
                    max_length=SMILES_CONTEXT_LENGTH,
                )
                inputs = tokens["input_ids"].to(self.device), tokens["attention_mask"].to(self.device)
                embeddings.append(self.model.encode_modality(inputs, modality="smiles").float().cpu())
        return torch.cat(embeddings).numpy()

    def rank(self, spectrum: Sequence[float] | FloatArray, candidates: list[str]) -> list[RankedCandidate]:
        if not candidates:
            return []
        spectrum_embedding = torch.from_numpy(self.embed_spectrum(spectrum)).unsqueeze(0)
        candidate_embeddings = torch.from_numpy(self.embed_smiles(candidates))
        scores = F.cosine_similarity(spectrum_embedding, candidate_embeddings, dim=1).tolist()
        ranked = sorted(zip(candidates, scores, strict=True), key=lambda candidate: candidate[1], reverse=True)
        return [RankedCandidate(smiles, score) for smiles, score in ranked]
