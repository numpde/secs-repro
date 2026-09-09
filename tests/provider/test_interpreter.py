"""Selection, inspection and correction share one explained conversation."""

from copy import deepcopy
import json
from time import monotonic
import unittest
from unittest.mock import patch

from secs_inference.provider.chat import ChatEndpoint, InterpreterError
from secs_inference.provider.input_operations import BrukerSelection, CannotAnalyse, JcampSelection, SourceRef
from secs_inference.provider.interpreter import InterpretationSession
from secs_inference.provider.job_input import JobSpecification
from secs_inference.provider.job_upload import JobUpload
from test_http import _tls_server, _write_test_certificates
from pathlib import Path
from tempfile import TemporaryDirectory
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.execution import ExecutionLoop
from test_execution import FakeApi, ACTIVE


def tool(name, arguments, call_id="call-1"):
    return {"role": "assistant", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


class ScriptedChat:
    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []

    def complete(self, messages, tools, *, deadline):
        self.requests.append(deepcopy(messages))
        return next(self.turns)


class InterpreterTests(unittest.TestCase):
    def test_endpoint_rejection_reaches_attempt_result_and_private_diagnostic(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_test_certificates(root)
            prompt = "Private Job input with model-secret that must not be retained in diagnostics"
            reason = "Unsupported parameter: parallel_tool_calls.\nmodel-secret " + prompt
            raw = json.dumps({"error": {"message": reason, "type": "invalid_request_error",
                                      "code": "unsupported_parameter", "param": "parallel_tool_calls"},
                              "request": prompt}).encode()
            with _tls_server(root, status=400, response_body=raw,
                             response_headers={"Content-Type": "application/json", "x-request-id": "req-test"}) as server:
                endpoint = ChatEndpoint(f"https://localhost:{server.port}/chat", "test-model", "model-secret", root / "ca.pem")
                with AttemptStore(root / "journal") as store:
                    api = FakeApi()
                    def analyse(active):
                        return endpoint.complete([{"role": "user", "content": "Private Job"},
                                                  {"role": "user", "content": prompt}], [], deadline=monotonic() + 2)
                    ExecutionLoop(api, store, analyse, store.diagnose).step()
                    result = json.loads(api.calls[-1])
                    self.assertEqual(result["failure_code"], "interpretation_failed")
                    self.assertIn("Unsupported parameter: parallel_tool_calls", result["failure_message"])
                    self.assertIn("HTTP 400", result["failure_message"])
                path = next((root / "journal").glob("*.diagnostic.json"))
                retained = json.loads(path.read_bytes())
                detail = retained["interpreter"]
                self.assertEqual(retained["execution_attempt_ref"], ACTIVE.execution_attempt_ref)
                self.assertNotIn("worker", retained)
                self.assertEqual(detail["status"], 400)
                self.assertEqual(detail["request_id"], "req-test")
                self.assertEqual(detail["model"], "test-model")
                self.assertEqual(detail["endpoint"], f"https://localhost:{server.port}")
                self.assertEqual(detail["param"], "parallel_tool_calls")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                for output in (json.dumps(retained), result["failure_message"]):
                    self.assertNotIn("model-secret", output)
                    self.assertNotIn(prompt, output)
                    self.assertNotIn("must not be retained", output)
                self.assertEqual(len(server.requests), 1)

    def test_unreadable_rejection_detail_does_not_hide_http_status(self):
        raw = b'{"error":{"message":"partial reason"}}'
        cases = (
            ({"response_body": b"<html>private proxy page</html>"}, "JSONDecodeError"),
            ({"response_body": b"x" * 16385}, "16384-byte"),
            ({"response_body": raw, "declared_response_length": len(raw) + 1}, "declared bytes"),
            ({"response_body": raw, "response_headers": {"Content-Encoding": "gzip"}}, "encoded"),
            ({"response_body": raw, "drip_seconds": 0.05}, "declared bytes"),
            ({"response_body": b'{"error":{"message":42}}'}, "error.message"),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_test_certificates(root)
            for response, evidence in cases:
                with self.subTest(evidence=evidence), _tls_server(root, status=400, **response) as server:
                    endpoint = ChatEndpoint(f"https://localhost:{server.port}/chat", "model", "key", root / "ca.pem")
                    started = monotonic()
                    with self.assertRaisesRegex(InterpreterError, "HTTP 400") as raised:
                        endpoint.complete([], [], deadline=monotonic() + (0.15 if "drip_seconds" in response else 2))
                    if "drip_seconds" in response:
                        self.assertLess(monotonic() - started, 1)
                    detail = raised.exception.diagnostic
                    self.assertEqual(detail["status"], 400)
                    self.assertIn(evidence, detail["detail_unavailable"])
                    self.assertNotIn("message", detail)
                    self.assertNotIn("private proxy page", str(raised.exception))

    def test_rejection_redacts_before_bounding_and_removes_controls(self):
        from secs_inference.provider.chat import _diagnostic_text
        secret = "a-very-long-configured-key"
        text = "x" * 20 + secret + "\x00\u202e\nBearer other-token sk-proj-partial***"
        result = _diagnostic_text(text, (secret,), 200)
        self.assertNotIn(secret, result)
        self.assertNotIn("other-token", result)
        self.assertNotIn("sk-proj", result)
        self.assertTrue(all(c.isprintable() for c in result))
        short = _diagnostic_text(text, (secret,), 25)
        self.assertLessEqual(len(short), 25)
        self.assertNotIn("a-ver", short)

    def _session(self, chat, inspect=lambda source: {}, max_turns=8):
        return InterpretationSession(
            chat, JobSpecification("job:chosen", "Find C2H6O"),
            (JobUpload("upload:first", "carbon distractor", 4, None),
             JobUpload("upload:second", "proton choices", 10, None)),
            inspect, deadline=monotonic() + 3, max_turns=max_turns,
        )

    def test_inspection_selects_among_distractors_and_preserves_every_description(self):
        chat = ScriptedChat(
            tool("inspect_source", {"source": {"upload_ref": "upload:second", "member": None}}),
            tool("read_jcamp", {"source": {"upload_ref": "upload:second", "member": "experiment2/spectrum.jdx"},
                                "formula": "C2H6O", "explanation": "Experiment 2 identifies proton data."}, "choice"),
        )
        inspected = []
        def inspect(source):
            inspected.append(source)
            return {"members": ["experiment1/carbon.jdx", "experiment2/spectrum.jdx"]}
        selection = self._session(chat, inspect).select()
        self.assertEqual(selection.source, SourceRef("upload:second", "experiment2/spectrum.jdx"))
        self.assertEqual(inspected, [SourceRef("upload:second")])
        initial = chat.requests[0][1]["content"]
        self.assertIn("carbon distractor", initial)
        self.assertIn("proton choices", initial)
        self.assertIn("experiment1/carbon.jdx", chat.requests[1][-1]["content"])

    def test_bruker_choice_preserves_formula_reason_and_exact_directory(self):
        for directory in ("", "chosen/./pdata/7"):
            with self.subTest(directory=directory):
                chat = ScriptedChat(tool("read_bruker", {
                    "upload_ref": "upload:second", "pdata_directory": directory,
                    "formula": "C2H6O", "explanation": "This processed pair identifies proton data.",
                }))
                self.assertEqual(self._session(chat).select(), BrukerSelection(
                    "upload:second", directory, "C2H6O", "This processed pair identifies proton data.",
                ))

    def test_reader_rejection_returns_to_its_call_without_resetting_turn_budget(self):
        choice = tool("read_jcamp", {"source": {"upload_ref": "upload:second", "member": None},
                                    "formula": "C2H6O", "explanation": "Description identifies proton."}, "selected")
        chat = ScriptedChat(choice, tool("report_input_problem", {"explanation": "No supported proton data was established."}))
        session = self._session(chat, max_turns=2)
        self.assertIsInstance(session.select(), JcampSelection)
        deadline = session.deadline
        session.reject("The selected file contains carbon, not proton data.")
        self.assertIsInstance(session.select(), CannotAnalyse)
        self.assertEqual(chat.requests[1][-1], {"role": "tool", "tool_call_id": "selected",
                                             "content": "The selected file contains carbon, not proton data."})
        self.assertEqual(session.deadline, deadline)
        self.assertEqual(session.remaining_turns, 0)

    def test_budget_exhaustion_is_not_an_input_problem(self):
        chat = ScriptedChat(tool("invent_reader", {}))
        with self.assertRaises(InterpreterError):
            self._session(chat, max_turns=1).select()

    def test_repair_feedback_distinguishes_unknown_tools_and_text_constraints(self):
        valid = {"source": {"upload_ref": "upload:second", "member": None}, "formula": "C2H6O", "explanation": "Proton spectrum."}
        cases = [(tool("invent_reader", {}), "choose one of the advertised tools")]
        for value, reason in ((5, "formula field must be text"), ("", "formula field must not be empty"),
                              ("\x00", "formula field contains a NUL"), ("x" * 65537, "65536-character limit")):
            cases.append((tool("read_jcamp", valid | {"formula": value}), reason))
        for call, reason in cases:
            with self.subTest(reason=reason):
                chat = ScriptedChat(call, tool("report_input_problem", {"explanation": "Cannot establish an input."}))
                self._session(chat).select()
                self.assertIn(reason, chat.requests[-1][-1]["content"])

    def test_chat_byte_limits_are_reported_as_provider_limits_not_model_token_limits(self):
        endpoint = ChatEndpoint("https://model.test/chat", "model", "key")
        with patch("secs_inference.provider.chat.ChatEndpoint._post") as post:
            with self.assertRaisesRegex(InterpreterError, "provider's 2097152-byte model-request limit"):
                endpoint.complete([{"role": "user", "content": "x" * 2097153}], [], deadline=monotonic() + 1)
            post.assert_not_called()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_test_certificates(root)
            with _tls_server(root, response_body=b"x" * 262145) as server:
                endpoint = ChatEndpoint(f"https://localhost:{server.port}/chat", "model", "key", root / "ca.pem")
                with self.assertRaisesRegex(InterpreterError, "provider's 262144-byte model-response limit"):
                    endpoint.complete([], [], deadline=monotonic() + 2)

    def test_malformed_json_arguments_get_correction_in_a_valid_conversation(self):
        malformed = tool("inspect_source", {})
        malformed["tool_calls"][0]["function"]["arguments"] = "not JSON"
        chat = ScriptedChat(malformed, tool("report_input_problem", {"explanation": "No supported source established."}))
        decision = self._session(chat).select()
        self.assertIsInstance(decision, CannotAnalyse)
        self.assertIn("not readable JSON", chat.requests[1][-1]["content"])

    def test_broken_selection_construction_is_not_sent_to_the_model_for_repair(self):
        chat = ScriptedChat(tool("read_jcamp", {
            "source": {"upload_ref": "upload:second", "member": None},
            "formula": "C2H6O", "explanation": "Proton experiment.",
        }))
        session = self._session(chat)
        with patch("secs_inference.provider.interpreter.JcampSelection", side_effect=TypeError("broken wiring")):
            with self.assertRaisesRegex(TypeError, "broken wiring"):
                session.select()
        self.assertEqual(len(chat.requests), 1)
        self.assertNotEqual(session.messages[-1]["role"], "tool")

    def test_unserializable_member_is_corrected_before_source_access(self):
        chat = ScriptedChat(
            tool("inspect_source", {"source": {"upload_ref": "upload:second", "member": "\ud800"}}),
            tool("read_jcamp", {"source": {"upload_ref": "upload:second", "member": "proton.jdx"},
                                "formula": "C2H6O", "explanation": "Proton experiment."}),
        )
        selection = self._session(chat, lambda _: self.fail("Malformed source reached inspection")).select()
        self.assertEqual(selection.source.member, "proton.jdx")
        self.assertIn("not valid Unicode", chat.requests[1][-1]["content"])

    def test_nontext_tool_arguments_are_not_replayed_as_an_assistant_message(self):
        malformed = tool("inspect_source", {})
        malformed["tool_calls"][0]["function"]["arguments"] = {}
        session = self._session(ScriptedChat(malformed))
        with self.assertRaises(InterpreterError):
            session.select()
        self.assertEqual(len(session.messages), 2)

    def test_ambiguous_call_identity_is_not_admitted_to_the_conversation(self):
        message = tool("inspect_source", {})
        message["tool_calls"] *= 2
        session = self._session(ScriptedChat(message))
        with self.assertRaises(InterpreterError):
            session.select()
        self.assertEqual(len(session.messages), 2)

    def test_download_outage_never_becomes_semantic_feedback(self):
        chat = ScriptedChat(tool("inspect_source", {"source": {"upload_ref": "upload:second", "member": None}}))
        def inspect(source):
            raise OSError("store unavailable")
        session = self._session(chat, inspect)
        with self.assertRaises(OSError):
            session.select()
        self.assertEqual(len(chat.requests), 1)
        self.assertNotEqual(session.messages[-1]["role"], "tool")

    def test_real_chat_transport_preserves_tool_request_and_bearer_separation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_test_certificates(root)
            message = tool("report_input_problem", {"explanation": "No spectrum was attached."})
            with _tls_server(root, response_body=json.dumps({"choices": [{"message": message}]}).encode()) as server:
                endpoint = ChatEndpoint(f"https://localhost:{server.port}/v1/chat/completions", "test-model", "model-secret", root / "ca.pem")
                # Trust is a startup input, not a file reread on each model turn.
                (root / "ca.pem").unlink()
                session = self._session(endpoint)
                decision = session.select()
                self.assertEqual(decision.explanation, "No spectrum was attached.")
                request = server.requests[0]
                self.assertEqual(request["authorization"], "Bearer model-secret")
                self.assertEqual(request["signature-input"], "")
                body = json.loads(request["body"])
                self.assertEqual(body["model"], "test-model")
                self.assertEqual(len(body["tools"]), 4)

    def test_truncated_chat_response_is_not_a_delivered_decision_even_if_json_parses(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_test_certificates(root)
            message = tool("report_input_problem", {"explanation": "No spectrum was attached."})
            raw = json.dumps({"choices": [{"message": message}]}).encode()
            with _tls_server(root, response_body=raw, declared_response_length=len(raw) + 1) as server:
                endpoint = ChatEndpoint(f"https://localhost:{server.port}/v1/chat/completions", "test-model", "model-secret", root / "ca.pem")
                with self.assertRaises(InterpreterError):
                    self._session(endpoint).select()
