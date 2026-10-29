"""Only an actual model forward exception receives the owned failure fact."""

import unittest
import json
import socket
import threading
from unittest.mock import Mock

import numpy as np
import torch

from secs_inference.model import ModelExecutionFailed, SecsInference
from secs_inference.provider.diagnostics import scientific_failure_evidence
from secs_inference.provider.worker_protocol import WorkerError, receive, send, serve_scientific_session


class ModelForwardFailureTests(unittest.TestCase):
    def test_actual_failed_frame_exposes_only_closed_model_fact(self):
        model = Mock()
        model.encode_modality.side_effect = RuntimeError("private scientific value 987654.321")
        inference = SecsInference(model, Mock(), torch.device("cpu"), torch.float32, 2, 1, 1)
        client, server = socket.socketpair()
        def run():
            try:
                serve_scientific_session(server, lambda: lambda _: inference.embed_spectrum(np.array([1, 2], dtype=np.float32)))
            except (OSError, WorkerError):
                pass
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertEqual(receive(client), {"outcome": "ready"})
            send(client, {"operation": "execute"})
            frame = receive(client)
            self.assertEqual(frame["outcome"], "failed")
            self.assertEqual(frame["model_failure"], {"modality": "h_nmr", "stage": "forward"})
            self.assertEqual(frame["cause"]["exception_type"], "RuntimeError")
            self.assertEqual(scientific_failure_evidence(frame)["model_failure"], frame["model_failure"])
            self.assertNotIn("987654.321", json.dumps(frame))
        finally:
            client.close()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_spectrum_and_smiles_forward_failures_keep_original_cause(self):
        for modality in ("h_nmr", "smiles"):
            with self.subTest(modality=modality):
                original = RuntimeError("private model payload")
                model = Mock()
                model.encode_modality.side_effect = original
                tokenizer = Mock(return_value={"input_ids": torch.tensor([[1]]), "attention_mask": torch.tensor([[1]])})
                inference = SecsInference(model, tokenizer, torch.device("cpu"), torch.float32, 2, 1, 1)
                with self.assertRaises(ModelExecutionFailed) as caught:
                    if modality == "h_nmr":
                        inference.embed_spectrum(np.array([1, 2], dtype=np.float32))
                    else:
                        inference.embed_smiles(["C"])
                self.assertIs(caught.exception.__cause__, original)
                self.assertEqual(caught.exception.modality, modality)
                self.assertEqual(caught.exception.stage, "forward")
                self.assertNotIn("private model payload", str(caught.exception))

    def test_preparation_and_result_conversion_are_not_forward_failures(self):
        model = Mock()
        tokenizer = Mock(side_effect=ValueError("tokenizer refused input"))
        inference = SecsInference(model, tokenizer, torch.device("cpu"), torch.float32, 2, 1, 1)
        with self.assertRaises(ValueError):
            inference.embed_spectrum(np.array([0, 0], dtype=np.float32))
        with self.assertRaisesRegex(ValueError, "tokenizer refused"):
            inference.embed_smiles(["C"])
        model.encode_modality.assert_not_called()
        model.encode_modality.return_value = object()
        with self.assertRaises(AttributeError):
            inference.embed_spectrum(np.array([1, 2], dtype=np.float32))

    def test_device_completion_failure_is_distinct_from_forward_failure(self):
        for modality in ("h_nmr", "smiles"):
            with self.subTest(modality=modality):
                original = RuntimeError("private completion detail")
                embedding = Mock()
                if modality == "h_nmr":
                    embedding.squeeze.return_value.float.return_value.cpu.side_effect = original
                else:
                    embedding.float.return_value.cpu.side_effect = original
                model = Mock()
                model.encode_modality.return_value = embedding
                tokenizer = Mock(return_value={"input_ids": torch.tensor([[1]]), "attention_mask": torch.tensor([[1]])})
                inference = SecsInference(model, tokenizer, torch.device("cpu"), torch.float32, 2, 1, 1)
                with self.assertRaises(ModelExecutionFailed) as caught:
                    if modality == "h_nmr":
                        inference.embed_spectrum(np.array([1, 2], dtype=np.float32))
                    else:
                        inference.embed_smiles(["C"])
                self.assertEqual((caught.exception.modality, caught.exception.stage), (modality, "completion"))
                self.assertIs(caught.exception.__cause__, original)


if __name__ == "__main__":
    unittest.main()
