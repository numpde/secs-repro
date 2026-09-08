"""Real checkpoint and a tiny, explicitly non-production candidate index."""

from pathlib import Path

from secs_inference.provider.worker import serve_worker


def load_fixture():
    """Exercise real retrieval and GA without loading or rebuilding the full index."""
    import faiss
    import numpy as np
    import torch
    from secs.elucidation import FaissCandidateSource
    from secs.utils.elucidation import smiles_to_molecular_formula
    from secs_inference.model import SecsInference
    from secs_inference.provider.worker_model import ScientificHandler, ScientificWorkerConfig

    inference = SecsInference.load(
        "/checkpoint/manifest.json", molformer_lock="/input/molformer.lock.toml",
        device="cpu", compute_dtype=torch.float32, smiles_batch_size=8,
    )
    smiles = ["NCc1ccc(Cl)cc1", "NCc1cc(Cl)ccc1", "NCc1ccccc1Cl", "CNc1ccc(Cl)cc1",
              "Cc1cc(Cl)ccc1N", "CCO", "CCC", "CCN"]
    embeddings = inference.embed_smiles(smiles).copy()
    faiss.normalize_L2(embeddings)
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    source = FaissCandidateSource(index, np.array(smiles), np.array([smiles_to_molecular_formula(item) for item in smiles]), n_neighbours=8)
    config = ScientificWorkerConfig("/checkpoint", "/input/molformer.lock.toml", device="cpu", compute_dtype="float32",
                                    smiles_batch_size=8, threads=2, neighbours=8, initial_population_size=8,
                                    population_size=8, offspring_size=8, max_generations=1)
    return ScientificHandler(inference, source, config)


if __name__ == "__main__":
    serve_worker(Path("/run/secs/worker/worker.sock"), load_fixture)
