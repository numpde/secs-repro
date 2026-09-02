from __future__ import annotations

import asyncio
from collections.abc import Iterable
import unittest

from secs_inference.provider.interpretation import (
    InterpretationCapability,
)
from secs_inference.provider.interpreter import (
    InterpretationResult,
    InterpreterEndpoint,
    InterpreterToolInvocation,
    InterpreterTransportError,
    InterpreterTurn,
    InterpreterUnavailable,
    InterpreterUnavailableReason,
    InterpretationRejected,
    ReportedInputProblem,
    interpret,
)
from secs_inference.provider.provider_events import InterpreterEndpointFailed
from secs_inference.provider.text_provenance import UserProvidedText


def _submitted(
    call_id: str,
    *,
    formula: str,
    slot: str,
    reason: str,
) -> InterpreterTurn:
    return InterpreterTurn(
        assistant_message={"role": "assistant", "tool_call": call_id},
        invocation=InterpreterToolInvocation(
            "submit_interpretation",
            {
                "value": {
                    "reported_formula": formula,
                    "input_slot": slot,
                    "selection_reason": reason,
                }
            },
        ),
        tool_call_ids=(call_id,),
    )


class ScriptedEndpoint:
    def __init__(self, turns: Iterable[InterpreterTurn | BaseException]) -> None:
        self._turns = iter(turns)
        self.prompts: list[list[dict[str, object]]] = []

    async def __call__(
        self,
        prompt: list[dict[str, object]],
    ) -> InterpreterTurn:
        self.prompts.append(prompt)
        turn = next(self._turns)
        if isinstance(turn, BaseException):
            raise turn
        return turn


class InterpreterTests(unittest.TestCase):
    def test_repairs_a_fabricated_selection_before_admission(self) -> None:
        endpoint = ScriptedEndpoint(
            (
                _submitted(
                    "call-1",
                    formula="H6C2O",
                    slot="invented-private-name",
                    reason="The name looked relevant.",
                ),
                _submitted(
                    "call-2",
                    formula="H6C2O",
                    slot="spectrum",
                    reason="The source identifies this as the processed spectrum.",
                ),
            )
        )
        failures: list[InterpreterEndpointFailed] = []

        capability = InterpretationCapability(("spectrum",))
        result = asyncio.run(
            interpret(
                source_text=UserProvidedText("Formula: H6C2O"),
                capability=capability,
                endpoints=(InterpreterEndpoint("primary", "model-a", endpoint),),
                interpretation_timeout_seconds=1,
                report_endpoint_failure=failures.append,
                admit_interpretation=capability.admit_interpretation,
            )
        )

        self.assertEqual(
            result,
            InterpretationResult(
                value=result.value,
                configuration_id="primary",
                model="model-a",
                attempted_configuration_ids=("primary",),
            ),
        )
        self.assertEqual(result.value.reported_formula, "H6C2O")
        self.assertEqual(result.value.input_slot, "spectrum")
        self.assertIn("processed spectrum", result.value.selection_reason)
        self.assertNotIn("H6C2O", repr(result.value))
        self.assertNotIn("processed spectrum", repr(result.value))
        self.assertEqual(failures, [])

        second_prompt = endpoint.prompts[1]
        self.assertTrue(
            any(
                message.get("content")
                == "Choose input_slot from the available attached inputs."
                for message in second_prompt
            )
        )
        self.assertNotIn("invented-private-name", repr(second_prompt[3:]))

    def test_reported_input_problem_remains_model_authored_prose(self) -> None:
        private_message = "The source does not state a molecular formula."
        endpoint = ScriptedEndpoint(
            (
                InterpreterTurn(
                    assistant_message={"role": "assistant"},
                    invocation=InterpreterToolInvocation(
                        "report_input_problem",
                        {"message": "  \n "},
                    ),
                    tool_call_ids=("call-blank",),
                ),
                InterpreterTurn(
                    assistant_message={"role": "assistant"},
                    invocation=InterpreterToolInvocation(
                        "report_input_problem",
                        {"message": private_message},
                    ),
                    tool_call_ids=("call-1",),
                ),
            )
        )
        capability = InterpretationCapability(("spectrum",))

        with self.assertRaises(ReportedInputProblem) as raised:
            asyncio.run(
                interpret(
                    source_text=UserProvidedText("No formula is present."),
                    capability=capability,
                    endpoints=(
                        InterpreterEndpoint("primary", "model-a", endpoint),
                    ),
                    interpretation_timeout_seconds=1,
                    report_endpoint_failure=lambda _event: None,
                    admit_interpretation=capability.admit_interpretation,
                )
            )

        self.assertEqual(raised.exception.message, private_message)
        self.assertNotIn(private_message, str(raised.exception))
        self.assertEqual(len(endpoint.prompts), 2)

    def test_transport_failure_falls_back_with_closed_operator_evidence(self) -> None:
        failed = ScriptedEndpoint((InterpreterTransportError("endpoint_unavailable"),))
        fallback = ScriptedEndpoint(
            (
                _submitted(
                    "call-2",
                    formula="C2H6O",
                    slot="spectrum",
                    reason="The source identifies this as a processed spectrum.",
                ),
            )
        )
        failures: list[InterpreterEndpointFailed] = []

        capability = InterpretationCapability(("spectrum",))
        result = asyncio.run(
            interpret(
                source_text=UserProvidedText("Formula C2H6O; input spectrum"),
                capability=capability,
                endpoints=(
                    InterpreterEndpoint("primary", "model-a", failed),
                    InterpreterEndpoint("fallback", "model-b", fallback),
                ),
                interpretation_timeout_seconds=1,
                report_endpoint_failure=failures.append,
                admit_interpretation=capability.admit_interpretation,
            )
        )

        self.assertEqual(result.configuration_id, "fallback")
        self.assertEqual(
            result.attempted_configuration_ids,
            ("primary", "fallback"),
        )
        self.assertEqual(
            failures,
            [
                InterpreterEndpointFailed(
                    configuration_id="primary",
                    failure_kind="transport",
                    failure_reason="endpoint_unavailable",
                )
            ],
        )

    def test_admission_repair_is_bounded_before_endpoint_fallback(self) -> None:
        rejected_turns = tuple(
            _submitted(
                f"rejected-{number}",
                formula="C2H6O",
                slot="invented",
                reason="The name looked relevant.",
            )
            for number in range(3)
        )
        primary = ScriptedEndpoint(rejected_turns)
        fallback = ScriptedEndpoint(
            (
                _submitted(
                    "accepted",
                    formula="C2H6O",
                    slot="spectrum",
                    reason="The application lists this spectrum input.",
                ),
            )
        )
        capability = InterpretationCapability(("spectrum",))

        result = asyncio.run(
            interpret(
                source_text=UserProvidedText("Formula C2H6O"),
                capability=capability,
                endpoints=(
                    InterpreterEndpoint("primary", "model-a", primary),
                    InterpreterEndpoint("fallback", "model-b", fallback),
                ),
                interpretation_timeout_seconds=1,
                report_endpoint_failure=lambda _event: None,
                admit_interpretation=capability.admit_interpretation,
            )
        )

        self.assertEqual(len(primary.prompts), 3)
        self.assertEqual(result.configuration_id, "fallback")

    def test_exhausted_admission_repair_is_not_operational_unavailability(self) -> None:
        endpoint = ScriptedEndpoint(
            tuple(
                _submitted(
                    f"rejected-{number}",
                    formula="C2H6O",
                    slot="invented",
                    reason="The name looked relevant.",
                )
                for number in range(3)
            )
        )
        capability = InterpretationCapability(("spectrum",))

        with self.assertRaises(InterpretationRejected):
            asyncio.run(
                interpret(
                    source_text=UserProvidedText("Formula C2H6O"),
                    capability=capability,
                    endpoints=(
                        InterpreterEndpoint("primary", "model-a", endpoint),
                    ),
                    interpretation_timeout_seconds=1,
                    report_endpoint_failure=lambda _event: None,
                    admit_interpretation=capability.admit_interpretation,
                )
            )

    def test_aggregate_deadline_is_operational_unavailability(self) -> None:
        async def never_returns(
            _prompt: list[dict[str, object]],
        ) -> InterpreterTurn:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        failures: list[InterpreterEndpointFailed] = []
        capability = InterpretationCapability(("spectrum",))
        with self.assertRaises(InterpreterUnavailable) as raised:
            asyncio.run(
                interpret(
                    source_text=UserProvidedText("Formula C2H6O"),
                    capability=capability,
                    endpoints=(
                        InterpreterEndpoint("primary", "model-a", never_returns),
                    ),
                    interpretation_timeout_seconds=0.01,
                    report_endpoint_failure=failures.append,
                    admit_interpretation=capability.admit_interpretation,
                )
            )

        self.assertEqual(
            raised.exception.reason,
            InterpreterUnavailableReason.DEADLINE_EXCEEDED,
        )
        self.assertEqual(failures[0].failure_kind, "timeout")


if __name__ == "__main__":
    unittest.main()
