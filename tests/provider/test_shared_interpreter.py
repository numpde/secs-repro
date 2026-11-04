"""Exercise SECS decisions through the shared multi-model interpreter."""

import asyncio
import json
from time import monotonic
import unittest

from secs_inference.provider.interpreter import InterpretationSession
from secs_inference.provider.interpreter import InterpretationFailed
from secs_inference.provider.input_operations import SelectedRepresentation
from secs_inference.provider.session import _diagnostic_details
from provider_core.diagnostics import failure_evidence
from provider_core.interpreter import ModelWindowExpired


def call(name, arguments):
    return {"role": "assistant", "tool_calls": [{"id": "one", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}]}


class Endpoint:
    identity = b"{}"
    retirement_confirmed = True

    def __init__(self, response):
        self.response = response
        self.retired = False
        self.requests = []

    async def complete(self, messages, tools, *, not_after):
        self.requests.append(messages)
        return self.response

    async def retire(self):
        self.retired = True


class SharedInterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_problem_hands_off_before_preparation(self):
        private = "private-report-canary"
        first = Endpoint(call("report_interpretation_problem", {"explanation": private}))
        second = Endpoint(call("select_representation", {
            "representation_id": "spectrum", "formula": "C2H6O",
            "formula_evidence": {"kind": "job_specification", "quote": "C2H6O"},
            "processing": "as_stored", "explanation": "The proton spectrum.",
        }))
        def factory(endpoint):
            return lambda **_settings: endpoint
        session = InterpretationSession((factory(first), factory(second)), "Find C2H6O", (), None,
                                        deadline=monotonic() + 5, timeout_seconds=5, context_bytes=1000000)
        prepared = []
        async def prepare(choice):
            prepared.append(choice)
            return choice
        result = await session.run(prepare)
        self.assertIsInstance(result, SelectedRepresentation)
        self.assertEqual(prepared, [result])
        self.assertTrue(first.retired)
        self.assertTrue(second.retired)
        self.assertNotIn(private, json.dumps(second.requests))
        report = session.conversation.failures[0][1]
        evidence = failure_evidence(report, secrets=(), describe_owner=_diagnostic_details)
        self.assertIn(private, json.dumps(evidence))

    async def test_exhaustion_retains_private_report(self):
        private = "private-report-canary"
        endpoint = Endpoint(call("report_interpretation_problem", {"explanation": private}))
        session = InterpretationSession((lambda **_: endpoint,), "Find C2H6O", (), None,
                                        deadline=monotonic() + 5, timeout_seconds=5, context_bytes=1000000)
        with self.assertRaises(InterpretationFailed):
            await session.run(None)
        report = session.conversation.failures[0][1]
        evidence = failure_evidence(report, secrets=(), describe_owner=_diagnostic_details)
        self.assertIn(private, json.dumps(evidence))

    async def test_initial_credential_in_context_fails_before_any_model_request(self):
        endpoint = Endpoint(call("report_interpretation_problem", {"explanation": "unused"}))
        session = InterpretationSession((lambda **_: endpoint,), "secret-key", (), None,
                                        deadline=monotonic() + 5, timeout_seconds=5, context_bytes=1000000, secrets=("secret-key",))
        with self.assertRaises(InterpretationFailed):
            await session.run(None)
        self.assertEqual(endpoint.requests, [])

    async def test_settled_inspection_expiry_allows_next_model(self):
        first = Endpoint(call("inspect_source", {"source": {"upload_ref": "upload:one", "member": None}}))
        second = Endpoint(call("select_representation", {
            "representation_id": "spectrum", "formula": "C2H6O",
            "formula_evidence": {"kind": "job_specification", "quote": "C2H6O"},
            "processing": "as_stored", "explanation": "The proton spectrum.",
        }))
        async def inspect(_source, deadline):
            await asyncio.sleep(max(0, deadline - monotonic()))
            raise ModelWindowExpired()
        async def prepare(choice):
            return choice
        session = InterpretationSession((lambda **_: first, lambda **_: second), "Find C2H6O", (), inspect,
                                        deadline=monotonic() + 0.4, timeout_seconds=0.4, context_bytes=1000000)
        self.assertIsInstance(await session.run(prepare), SelectedRepresentation)
        self.assertTrue(first.retired)
