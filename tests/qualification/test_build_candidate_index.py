from pathlib import Path
import tempfile
import unittest

import faiss
import numpy as np

from build_candidate_index import configured_index, configured_metric


class ConfiguredIndexTest(unittest.TestCase):
    def test_serialized_hnsw_pq_ranks_by_inner_product(self):
        config = {
            "metric": "inner_product",
            "hnsw_neighbors": 4,
            "pq_subquantizers": 1,
            "pq_bits": 1,
            "ef_construction": 20,
            "ef_search": 32,
        }
        # Inner product prefers the longer first vector, while L2 prefers the
        # nearly identical second vector. The winner therefore proves behavior.
        database = np.array([[2.0, 0.0, 0.0, 0.0], [1.0, 0.01, 0.0, 0.0]], dtype=np.float32)
        training = np.repeat(database, 39, axis=0)
        query = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)
        faiss.omp_set_num_threads(1)

        index = configured_index(4, config, configured_metric(config))
        index.train(training)
        index.add(database)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "candidates.faiss"
            faiss.write_index(index, str(path))
            reloaded = faiss.read_index(str(path))

        storage = faiss.downcast_index(reloaded.storage)
        scores, identifiers = reloaded.search(query, 2)
        self.assertEqual(reloaded.metric_type, faiss.METRIC_INNER_PRODUCT)
        self.assertEqual(storage.metric_type, faiss.METRIC_INNER_PRODUCT)
        self.assertEqual(identifiers.tolist(), [[0, 1]])
        np.testing.assert_allclose(scores, [[2.0, 1.0]], rtol=0.0, atol=1e-5)


if __name__ == "__main__":
    unittest.main()
