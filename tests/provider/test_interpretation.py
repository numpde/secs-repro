from __future__ import annotations

import asyncio
import unittest

from secs_inference.provider.interpretation import (
    MAX_REPORTED_FORMULA_CHARACTERS,
    InterpretationCandidate,
    InterpretationCapability,
    construct_interpretation_candidate,
)
from secs_inference.provider.interpreter import (
    InterpretationCandidateRejected,
    InterpreterProtocolError,
)


class InterpretationCandidateTests(unittest.TestCase):
    def test_constructs_the_exact_model_proposal_for_an_available_slot(self) -> None:
        candidate = construct_interpretation_candidate(
            {
                "reported_formula": "H6C2O",
                "input_slot": "spectrum",
                "selection_reason": (
                    "This input is described as the processed 1H spectrum."
                ),
            },
        )

        self.assertEqual(
            candidate,
            InterpretationCandidate(
                reported_formula="H6C2O",
                input_slot="spectrum",
                selection_reason=(
                    "This input is described as the processed 1H spectrum."
                ),
            ),
        )
        self.assertNotIn("processed 1H spectrum", repr(candidate))
        self.assertNotIn("H6C2O", repr(candidate))
        self.assertNotIn("spectrum", repr(candidate))

    def test_requires_the_closed_tool_value_shape(self) -> None:
        valid = {
            "reported_formula": "C2H6O",
            "input_slot": "spectrum",
            "selection_reason": "It is the spectrum input.",
        }
        malformed = (
            None,
            {"reported_formula": "C2H6O"},
            valid | {"confidence": 0.9},
            valid | {"reported_formula": 123},
            valid | {"selection_reason": " \n "},
            valid | {"selection_reason": "unsafe\0text"},
            valid
            | {
                "reported_formula": "C" * (
                    MAX_REPORTED_FORMULA_CHARACTERS + 1
                )
            },
        )

        for value in malformed:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(InterpreterProtocolError):
                    construct_interpretation_candidate(value)

    def test_rejects_a_slot_outside_the_authoritative_inventory(self) -> None:
        candidate = construct_interpretation_candidate(
            {
                "reported_formula": "C2H6O",
                "input_slot": "invented-private-name",
                "selection_reason": "The name looked relevant.",
            }
        )
        capability = InterpretationCapability(("spectrum",))

        with self.assertRaisesRegex(
            InterpretationCandidateRejected,
            "available attached inputs",
        ) as raised:
            asyncio.run(capability.admit_interpretation(candidate))

        self.assertNotIn("invented-private-name", str(raised.exception))

    def test_capability_presents_the_slot_inventory_outside_job_prose(self) -> None:
        capability = InterpretationCapability(("spectrum", "notes"))

        self.assertIn(
            'Application-provided available input slots:\n["spectrum","notes"]',
            capability.interpreter_context,
        )


if __name__ == "__main__":
    unittest.main()
