"""The small index applies its fixed CPU thread budget before model use."""

import unittest
from unittest.mock import Mock, patch

import faiss
import numpy as np
import torch
from secs_inference.provider.mock_index_worker import load_mock_index


class MockIndexThreadsTests(unittest.TestCase):
    def test_loader_applies_two_threads_to_torch_and_faiss(self):
        torch.set_num_threads(3)
        faiss.omp_set_num_threads(3)
        inference = Mock()
        inference.embed_smiles.return_value = np.eye(8, dtype=np.float32)
        def load(*_args, **_kwargs):
            self.assertEqual(torch.get_num_threads(), 2)
            self.assertEqual(faiss.omp_get_max_threads(), 2)
            return inference
        with patch('secs_inference.model.SecsInference.load', side_effect=load):
            load_mock_index()
        self.assertEqual(torch.get_num_threads(), 2)
        self.assertEqual(faiss.omp_get_max_threads(), 2)
