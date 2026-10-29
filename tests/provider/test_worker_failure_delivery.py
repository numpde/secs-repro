"""Scientific failure delivery keeps the original cause if the peer disappears."""

import json
import socket
import threading
import unittest

from secs_inference.provider.diagnostics import exception_evidence
from secs_inference.provider.worker_protocol import receive, send, serve_scientific_session


class WorkerFailureDeliveryTests(unittest.TestCase):
    def test_disconnected_peer_retains_original_failure_without_private_text(self):
        client, server = socket.socketpair()
        entered, release = threading.Event(), threading.Event()
        original = RuntimeError("private scientific result 987654.321")
        observed = []

        def handler(_command):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release handler")
            raise original

        def run():
            try:
                serve_scientific_session(server, lambda: handler)
            except Exception as error:
                observed.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertEqual(receive(client), {"outcome": "ready"})
            send(client, {"operation": "execute"})
            self.assertTrue(entered.wait(5))
            client.close()
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(observed), 1)
            self.assertIsInstance(observed[0], OSError)
            self.assertIs(observed[0].__context__, original)
            evidence = json.dumps(exception_evidence(observed[0]))
            self.assertIn('"exception_type": "RuntimeError"', evidence)
            self.assertNotIn("987654.321", evidence)
        finally:
            client.close()
            release.set()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
