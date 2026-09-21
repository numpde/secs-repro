"""Describe discovery and exact representation selection to the interpreter."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceRef:
    """An Upload, or an exact member inside its ZIP; never a host path."""

    upload_ref: str
    member: str | None = None


@dataclass(frozen=True, slots=True)
class SelectedRepresentation:
    representation_id: str
    formula: str
    formula_evidence: dict
    processing: str
    explanation: str


@dataclass(frozen=True, slots=True)
class CannotAnalyse:
    explanation: str


@dataclass(frozen=True, slots=True)
class InputOperation:
    name: str
    description: str
    parameters: dict


_TEXT = {"type": "string", "minLength": 1}
_SOURCE = {
    "type": "object", "additionalProperties": False,
    "properties": {"upload_ref": _TEXT, "member": {"type": ["string", "null"]}},
    "required": ["upload_ref", "member"],
}
_EXPLANATION = {"type": "string", "minLength": 1, "maxLength": 2048}
_FORMULA_EVIDENCE = {"oneOf": [
    {
        "type": "object", "additionalProperties": False,
        "properties": {"kind": {"const": "job_specification"}},
        "required": ["kind"],
    },
    {
        "type": "object", "additionalProperties": False,
        "properties": {
            "kind": {"const": "representations"},
            "representation_ids": {
                "type": "array", "items": _TEXT, "minItems": 1, "maxItems": 16,
                "uniqueItems": True,
            },
        },
        "required": ["kind", "representation_ids"],
    },
]}


def _arguments(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


INPUT_OPERATIONS = (
    InputOperation(
        "inspect_source",
        "Discover every recognized representation in an Upload or exact ZIP-member scope. "
        "The result preserves source identities, relationships, scientific metadata and partial-discovery issues.",
        _arguments({"source": _SOURCE}),
    ),
    InputOperation(
        "select_representation",
        "Select one discovered representation for this analysis. Use its opaque identity exactly, "
        "state the evidenced molecular formula, cite either the Job specification or discovered structure "
        "representations as formula evidence, choose stored data or automatic FID processing, and explain the choice.",
        _arguments({
            "representation_id": _TEXT,
            "formula": _TEXT,
            "formula_evidence": _FORMULA_EVIDENCE,
            "processing": {"type": "string", "enum": ["as_stored", "auto"]},
            "explanation": _EXPLANATION,
        }),
    ),
    InputOperation(
        "report_input_problem",
        "Explain why the available representations and evidence do not establish an executable "
        "spectrum and formula. Name what is missing or ambiguous without claiming invalid chemistry.",
        _arguments({"explanation": _EXPLANATION}),
    ),
)


def interpreter_tools() -> list[dict]:
    return [{"type": "function", "function": {
        "name": operation.name, "description": operation.description,
        "parameters": operation.parameters, "strict": True,
    }} for operation in INPUT_OPERATIONS]
