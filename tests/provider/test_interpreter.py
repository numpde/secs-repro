"""Selection, inspection and correction share one explained conversation."""

from copy import deepcopy
from contextlib import nullcontext
import errno
import json
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from secs_inference.provider.chat import ChatEndpoint, InterpreterError
from secs_inference.provider.input_operations import BrukerSelection, CannotAnalyse, JcampSelection, SourceRef, interpreter_tools
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

    def failure(self, phase, reason):
        return ChatEndpoint("https://model.test/chat", "scripted-model", "key").failure(phase, reason)


class InterpreterTests(unittest.TestCase):
    def test_model_reply_can_outlast_connection_timeout_within_interpretation_deadline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_test_certificates(root)
            message = tool("report_input_problem", {"explanation": "No spectrum supplied."})
            body = json.dumps({"choices": [{"message": message}]}).encode()
            # A real eleven-second pause catches the inherited ten-second
            # socket timeout without replacing the transport under test.
            with _tls_server(root, response_body=body, reply_delay_seconds=11) as server:
                endpoint = ChatEndpoint(f"https://localhost:{server.port}/chat", "test-model", "key", root / "ca.pem")
                self.assertEqual(endpoint.complete([], [], deadline=monotonic() + 30), message)
                self.assertEqual(len(server.requests), 1)

    def test_transport_failure_names_model_phase_and_retains_only_safe_evidence(self):
        phases = (
            ("connect", "connecting to the model service"),
            ("request", "sending the interpretation request"),
            ("getresponse", "waiting for the model service to reply"),
            ("read1", "reading the model's reply"),
        )
        for failing_call, phase in phases:
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                connection = Mock()
                response = connection.getresponse.return_value
                response.status, response.headers = 200, {}
                target = response if failing_call == "read1" else connection
                getattr(target, failing_call).side_effect = OSError(errno.EIO, "private prompt model-secret")
                endpoint = ChatEndpoint("https://model.test/chat", "test-model", "model-secret")
                with (patch("secs_inference.provider.chat.http.client.HTTPSConnection", return_value=connection),
                      patch("secs_inference.provider.chat.socket_deadline", return_value=nullcontext()),
                      AttemptStore(Path(directory) / "journal") as store):
                    api = FakeApi()
                    ExecutionLoop(api, store, lambda _: endpoint.complete(
                        [{"role": "user", "content": "private prompt"}], [], deadline=monotonic() + 10,
                    ), store.diagnose).step()
                public = json.loads(api.calls[-1])
                self.assertEqual(public["failure_code"], "interpretation_failed")
                self.assertIn("test-model", public["failure_message"])
                self.assertIn(phase, public["failure_message"])
                self.assertIn("Input/output error", public["failure_message"])
                self.assertIn("request was not sent" if failing_call == "connect" else "processing this request is unknown", public["failure_message"])
                private = json.loads(next((Path(directory) / "journal").glob("*.diagnostic.json")).read_bytes())
                self.assertEqual(private["interpreter"]["phase"], phase)
                self.assertEqual(private["interpreter"]["errno"], errno.EIO)
                self.assertEqual(private["interpreter"]["exception_type"], "OSError")
                for output in (json.dumps(private), public["failure_message"]):
                    self.assertNotIn("private prompt", output)
                    self.assertNotIn("model-secret", output)
                self.assertEqual(connection.connect.call_count, 1)

    def test_socket_timeout_and_interpretation_deadline_are_distinct(self):
        for now, reason in ((1, "connection timed out"), (11, "interpretation deadline elapsed")):
            with self.subTest(now=now):
                connection = Mock()
                connection.getresponse.side_effect = TimeoutError("private timeout detail")
                endpoint = ChatEndpoint("https://model.test/chat", "test-model", "key")
                with (patch("secs_inference.provider.chat.http.client.HTTPSConnection", return_value=connection),
                      patch("secs_inference.provider.chat.socket_deadline", return_value=nullcontext()),
                      patch("secs_inference.provider.chat.monotonic", side_effect=[0, now])):
                    with self.assertRaises(InterpreterError) as caught:
                        endpoint.complete([], [], deadline=10)
                self.assertIn(reason, str(caught.exception))
                self.assertIn("waiting for the model service", str(caught.exception))
                self.assertNotIn("private timeout detail", str(caught.exception))

    def test_real_deadline_interrupt_reports_whether_headers_or_body_were_pending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_test_certificates(root)
            for delays, phase in (({"header_drip_seconds": 0.03}, "waiting for the model service"),
                                  ({"drip_seconds": 0.03}, "reading the model's reply")):
                with self.subTest(phase=phase), _tls_server(root, response_body=b"x" * 100, **delays) as server:
                    endpoint = ChatEndpoint(f"https://localhost:{server.port}/chat", "test-model", "key", root / "ca.pem")
                    began = monotonic()
                    with self.assertRaises(InterpreterError) as caught:
                        endpoint.complete([], [], deadline=began + 0.2)
                    self.assertLess(monotonic() - began, 1)
                    self.assertIn("test-model", str(caught.exception))
                    self.assertIn(phase, str(caught.exception))
                    self.assertIn("deadline elapsed", str(caught.exception))
                    self.assertEqual(len(server.requests), 1)

    def test_deadline_eof_is_not_blame_for_a_truncated_model_reply(self):
        for bytes_remaining in (0, 5):
            with self.subTest(bytes_remaining=bytes_remaining):
                connection = Mock()
                response = connection.getresponse.return_value
                response.status, response.headers, response.length = 200, {}, bytes_remaining
                response.read1.return_value = b""
                endpoint = ChatEndpoint("https://model.test/chat", "test-model", "key")
                with (patch("secs_inference.provider.chat.http.client.HTTPSConnection", return_value=connection),
                      patch("secs_inference.provider.chat.socket_deadline", return_value=nullcontext()),
                      patch("secs_inference.provider.chat.monotonic", side_effect=[0, 11])):
                    with self.assertRaisesRegex(InterpreterError, "deadline elapsed before the reply could be accepted"):
                        endpoint.complete([], [], deadline=10)

    def test_failure_model_name_is_bounded_redacted_and_printable(self):
        endpoint = ChatEndpoint("https://model.test/chat", "test\nmodel-secret " + "x" * 500, "model-secret")
        error = endpoint.failure("connecting to the model service", "connection refused")
        self.assertNotIn("model-secret", str(error))
        self.assertNotIn("\n", str(error))
        self.assertLessEqual(len(error.diagnostic["model"]), 128)

    def test_endpoint_is_asked_to_generate_schema_conforming_tool_arguments(self):
        response = json.dumps({"choices": [{"message": tool("report_input_problem", {"explanation": "No input."})}]}).encode()
        with patch.object(ChatEndpoint, "_post", return_value=response) as post:
            ChatEndpoint("https://model.test/chat", "model", "key").complete(
                [], interpreter_tools(), deadline=monotonic() + 1)
        request = json.loads(post.call_args.args[0])
        self.assertTrue(all(item["function"]["strict"] is True for item in request["tools"]))

    def test_reasoning_effort_is_explicit_or_absent_not_inferred_from_model(self):
        response = json.dumps({"choices": [{"message": tool("report_input_problem", {"explanation": "No input."})}]}).encode()
        for effort in (None, "none", "low"):
            with self.subTest(effort=effort):
                endpoint = ChatEndpoint("https://model.test/chat", "model", "key", reasoning_effort=effort)
                with patch.object(ChatEndpoint, "_post", return_value=response) as post:
                    endpoint.complete([], [], deadline=monotonic() + 1)
                request = json.loads(post.call_args.args[0])
                if effort is None:
                    self.assertNotIn("reasoning_effort", request)
                else:
                    self.assertEqual(request["reasoning_effort"], effort)

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
            ({"response_body": b"<html>private proxy page</html>"}, "readable JSON"),
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
                    if evidence == "readable JSON":
                        self.assertEqual(detail["detail_parse_failure"]["exception_type"], "JSONDecodeError")
                    self.assertNotIn("message", detail)
                    self.assertNotIn("private proxy page", str(raised.exception))

    def test_rejection_body_network_failure_remains_secondary_and_inspectable(self):
        endpoint = ChatEndpoint("https://model.test/chat", "chosen-model", "model-secret")
        response = Mock(status=400, headers={"x-request-id": "request-test"})
        response.read1.side_effect = ConnectionResetError(errno.ECONNRESET, "private-model-secret")
        rejection = endpoint._rejection(response, ())
        self.assertIn("HTTP 400", str(rejection))
        self.assertIn("chosen-model", str(rejection))
        self.assertEqual(rejection.diagnostic["status"], 400)
        self.assertEqual(rejection.diagnostic["detail_read_failure"]["errno"], errno.ECONNRESET)
        self.assertIn("connection was reset", rejection.diagnostic["detail_unavailable"])
        self.assertNotIn("private-model-secret", json.dumps(rejection.diagnostic))

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
        with self.assertRaises(InterpreterError) as caught:
            self._session(chat, max_turns=1).select()
        self.assertIn("scripted-model", str(caught.exception))
        self.assertIn("allowed model turns were exhausted", str(caught.exception))
        self.assertIn("selecting an input and reader", str(caught.exception))

    def test_expired_interpretation_budget_does_not_call_the_model(self):
        chat = ScriptedChat()
        session = self._session(chat)
        session.deadline = monotonic() - 1
        with self.assertRaises(InterpreterError) as caught:
            session.select()
        self.assertIn("scripted-model", str(caught.exception))
        self.assertIn("interpretation deadline elapsed", str(caught.exception))
        self.assertEqual(chat.requests, [])

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
