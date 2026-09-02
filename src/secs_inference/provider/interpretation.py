"""Construct the model-proposed formula and spectrum selection.

This boundary owns the closed tool-result shape and attachment membership. It
does not validate molecular chemistry or artifact contents: deterministic
admission owns those decisions after interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from secs_inference.provider.interpreter import (
    InterpretationCandidateRejected,
    InterpreterProtocolError,
)
from secs_inference.provider.text_provenance import (
    ModelGeneratedText,
    ProviderDiagnosticText,
)


MAX_REPORTED_FORMULA_CHARACTERS = 256
MAX_INPUT_SLOT_CHARACTERS = 256
MAX_SELECTION_REASON_CHARACTERS = 2_000
MAX_INPUT_SLOT_INVENTORY_UTF8_BYTES = 16_384


@dataclass(frozen=True, slots=True)
class InterpretationCandidate:
    """A model-proposed formula and attached input, before deterministic admission.

    ``selection_reason`` records why the model chose the input. It is
    explanatory provenance only; artifact resolution and execution must not
    treat it as evidence.
    """

    reported_formula: str = field(repr=False)
    input_slot: str = field(repr=False)
    selection_reason: ModelGeneratedText = field(repr=False)


def construct_interpretation_candidate(
    value: object,
) -> InterpretationCandidate:
    """Validate the closed shape of one model tool value."""

    if type(value) is not dict or set(value) != {
        "reported_formula",
        "input_slot",
        "selection_reason",
    }:
        raise InterpreterProtocolError(
            "Submit exactly reported_formula, input_slot, and selection_reason."
        )

    formula = _bounded_text(
        value["reported_formula"],
        field_name="reported_formula",
        maximum_characters=MAX_REPORTED_FORMULA_CHARACTERS,
    )
    input_slot = _bounded_text(
        value["input_slot"],
        field_name="input_slot",
        maximum_characters=MAX_INPUT_SLOT_CHARACTERS,
    )
    reason = _bounded_text(
        value["selection_reason"],
        field_name="selection_reason",
        maximum_characters=MAX_SELECTION_REASON_CHARACTERS,
    )
    return InterpretationCandidate(
        reported_formula=formula,
        input_slot=input_slot,
        selection_reason=ModelGeneratedText(reason),
    )


@dataclass(frozen=True, slots=True)
class InterpretationCapability:
    """Expose SECS instructions and admit selection against one slot inventory."""

    available_input_slots: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.available_input_slots) is not tuple:
            raise TypeError("available_input_slots must be a tuple")
        if not self.available_input_slots or any(
                not _is_admissible_input_slot(slot)
                for slot in self.available_input_slots
            ):
            raise ValueError(
                "available_input_slots must be distinct bounded non-blank UTF-8 text"
            )
        if (
            len(set(self.available_input_slots)) != len(self.available_input_slots)
            or sum(
                len(slot.encode("utf-8")) for slot in self.available_input_slots
            )
            > MAX_INPUT_SLOT_INVENTORY_UTF8_BYTES
        ):
            raise ValueError(
                "available_input_slots must be distinct bounded non-blank UTF-8 text"
            )

    @property
    def interpreter_prompt_path(self) -> Path:
        return Path(__file__).with_name("prompts") / "analysis_interpretation.md"

    @property
    def interpreter_context(self) -> str:
        """Render API-owned slot choices separately from untrusted Job prose."""

        slots = json.dumps(
            self.available_input_slots,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return f"Application-provided available input slots:\n{slots}"

    def construct_interpretation(self, value: object, /) -> InterpretationCandidate:
        return construct_interpretation_candidate(value)

    async def admit_interpretation(self, candidate: InterpretationCandidate) -> None:
        """Reject a fabricated slot before any artifact retrieval can begin."""

        if candidate.input_slot not in self.available_input_slots:
            # Do not echo a fabricated identifier into repair prompts or logs.
            raise InterpretationCandidateRejected(
                ProviderDiagnosticText(
                    "Choose input_slot from the available attached inputs."
                )
            )


def _bounded_text(
    value: object,
    *,
    field_name: str,
    maximum_characters: int,
) -> str:
    if type(value) is not str or not value.strip():
        raise InterpreterProtocolError(
            f"{field_name} must be non-blank text."
        )
    if "\0" in value:
        raise InterpreterProtocolError(
            f"{field_name} must not contain NUL."
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise InterpreterProtocolError(
            f"{field_name} must be UTF-8 text."
        ) from None
    if len(value) > maximum_characters:
        raise InterpreterProtocolError(
            f"{field_name} must contain at most {maximum_characters} characters."
        )
    return value


def _is_admissible_input_slot(value: object) -> bool:
    if (
        type(value) is not str
        or not value.strip()
        or "\0" in value
        or len(value) > MAX_INPUT_SLOT_CHARACTERS
    ):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True
