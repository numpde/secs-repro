"""Scientific selection yields while shared carriers and source readers work."""

import asyncio
from copy import deepcopy
import json
from time import monotonic
from types import SimpleNamespace
import unittest

from provider_core.execution import ObservationGapExpired
from secs_inference.provider.input_operations import CannotAnalyse, SelectedRepresentation
from secs_inference.provider.interpreter import InterpretationFailed, InterpretationSession


def call(name, arguments, identity):
    return {"id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def choice(identity="choice"):
    return call("select_representation", {
        "representation_id": "proton-spectrum", "formula": "C2H6O",
        "formula_evidence": {"kind": "job_specification", "quote": "C2H6O"},
        "processing": "as_stored", "explanation": "The representation identifies proton data.",
    }, identity)


class ScriptedCarrier:
    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        return {"role": "assistant", "tool_calls": turn}


class AsyncInterpretationTests(unittest.IsolatedAsyncioTestCase):
    async def test_observation_expiry_survives_interpreter_deadline(self):
        expiry = ObservationGapExpired(1, executor=SimpleNamespace(_observation_read_started=False))
        async def inspect(_source):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise expiry
        chat = ScriptedCarrier([call("inspect_source", {
            "source": {"upload_ref": "upload:chosen", "member": None}}, "inspect")])
        session = self.session(chat, inspect)
        session.deadline = monotonic() + 0.05
        with self.assertRaises(ObservationGapExpired) as caught:
            await session.select()
        self.assertIs(caught.exception, expiry)

    def session(self, chat, inspect, turns=3):
        return InterpretationSession(chat, "Find C2H6O", (), inspect,
                                     deadline=monotonic() + 3, max_turns=turns)

    async def test_inspection_yields_and_rejection_keeps_the_original_conversation_budget(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def inspect(source):
            self.assertEqual(source.upload_ref, "upload:chosen")
            entered.set()
            await release.wait()
            return {"representations": [{"id": "proton-spectrum"}]}
        chat = ScriptedCarrier(
            [call("inspect_source", {"source": {"upload_ref": "upload:chosen", "member": None}}, "inspect")],
            [choice()], [call("report_input_problem", {"explanation": "No supported input remains."}, "refusal")],
        )
        session = self.session(chat, inspect)
        task = asyncio.create_task(session.select())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(task.done())
            release.set()
            self.assertIsInstance(await task, SelectedRepresentation)
            deadline = session.deadline
            session.reject("The selected representation cannot be prepared.")
            self.assertIsInstance(await session.select(), CannotAnalyse)
            self.assertEqual(session.deadline, deadline)
            self.assertEqual(session.remaining_turns, 0)
            self.assertEqual(chat.requests[-1][-1]["tool_call_id"], "choice")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_multiple_tools_per_turn_perform_no_scientific_access(self):
        async def inspect(_source):
            self.fail("Multiple tools must not execute")
        chat = ScriptedCarrier([choice("one"), choice("two")], [choice("corrected")])
        session = self.session(chat, inspect, turns=2)
        self.assertIsInstance(await session.select(), SelectedRepresentation)
        feedback = chat.requests[-1][-2:]
        self.assertEqual([item["tool_call_id"] for item in feedback], ["one", "two"])
        self.assertEqual(session.remaining_turns, 0)

    async def test_carrier_failure_is_not_input_feedback_or_an_implicit_resend(self):
        failure = OSError("model service unavailable")
        chat = ScriptedCarrier(failure)
        session = self.session(chat, None)
        with self.assertRaises(OSError) as caught:
            await session.select()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(chat.requests), 1)
        self.assertEqual(len(session.messages), 2)

    async def test_deadline_during_inspection_is_not_semantic_feedback(self):
        entered = asyncio.Event()
        async def inspect(_source):
            entered.set()
            await asyncio.Event().wait()
        chat = ScriptedCarrier([call("inspect_source", {
            "source": {"upload_ref": "upload:chosen", "member": None}}, "inspect")])
        session = self.session(chat, inspect)
        session.deadline = monotonic() + 0.05
        with self.assertRaisesRegex(InterpretationFailed, "interpretation deadline elapsed"):
            await session.select()
        self.assertTrue(entered.is_set())
        self.assertEqual(len(chat.requests), 1)
        self.assertEqual(session.messages[-1]["role"], "assistant")
        self.assertEqual(session.rejections, [])

    async def test_cancelled_observation_does_not_turn_interruption_into_input_feedback(self):
        for phase in ("carrier", "inspection"):
            with self.subTest(phase=phase):
                entered = asyncio.Event()
                class Carrier(ScriptedCarrier):
                    async def complete(self, messages, tools):
                        if phase == "carrier":
                            self.requests.append(deepcopy(messages))
                            entered.set()
                            await asyncio.Event().wait()
                        return await super().complete(messages, tools)
                async def inspect(_source):
                    entered.set()
                    await asyncio.Event().wait()
                chat = Carrier([call("inspect_source", {
                    "source": {"upload_ref": "upload:chosen", "member": None}}, "inspect")])
                session = self.session(chat, inspect)
                task = asyncio.create_task(session.select())
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(len(chat.requests), 1)
                self.assertNotEqual(session.messages[-1]["role"], "tool")
                self.assertEqual(session.rejections, [])
