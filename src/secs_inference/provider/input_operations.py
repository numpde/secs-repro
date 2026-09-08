"""Describe executable input choices without importing scientific libraries.

Hello and interpreter tools share this catalogue. Reader binding belongs to
the offline input adapter, so discovery never loads the model or decoders.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceRef:
    """An Upload, or an exact member inside its ZIP; never a host path."""

    upload_ref: str
    member: str | None = None


@dataclass(frozen=True, slots=True)
class JcampSelection:
    source: SourceRef
    formula: str
    explanation: str


@dataclass(frozen=True, slots=True)
class BrukerSelection:
    upload_ref: str
    pdata_directory: str
    formula: str
    explanation: str


@dataclass(frozen=True, slots=True)
class CannotAnalyse:
    explanation: str


Selection = JcampSelection | BrukerSelection


@dataclass(frozen=True, slots=True)
class InputOperation:
    name: str
    description: str
    parameters: dict
    formats: tuple[str, ...] = ()


_TEXT = {"type": "string", "minLength": 1}
_SOURCE = {
    "type": "object", "additionalProperties": False,
    "properties": {"upload_ref": _TEXT, "member": {"type": ["string", "null"]}},
    "required": ["upload_ref", "member"],
}
_EXPLANATION = {"type": "string", "minLength": 1, "maxLength": 2048}


def _arguments(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


INPUT_OPERATIONS = (
    InputOperation(
        "inspect_source",
        "Inspect an Upload or ZIP member. Returns archive members or a bounded "
        "text prefix as untrusted input evidence. Does not choose a dataset.",
        _arguments({"source": _SOURCE}),
    ),
    InputOperation(
        "read_jcamp",
        "Choose one processed 1D proton JCAMP-DX spectrum, directly uploaded or "
        "inside a ZIP. Supply the formula established by the Job or input evidence "
        "and explain why this source is appropriate.",
        _arguments({"source": _SOURCE, "formula": _TEXT, "explanation": _EXPLANATION}),
        (
            "a JCAMP-DX file containing one processed 1H NMR AFFN XYDATA block with a ppm axis",
            "a JCAMP-DX file containing one processed 1H NMR NTUPLES real/imaginary pair with a ppm axis or a referenced Hz axis",
        ),
    ),
    InputOperation(
        "read_bruker",
        "Choose a processed 1D proton Bruker pdata directory inside a ZIP. Its "
        "1r and procs files are required. Supply the exact archive directory "
        "without a trailing slash (empty for the ZIP root), "
        "formula and reason for choosing this experiment rather than others.",
        _arguments({"upload_ref": _TEXT, "pdata_directory": {"type": "string"},
                    "formula": _TEXT, "explanation": _EXPLANATION}),
        ("a Bruker processed pdata directory with 1r and procs in a ZIP archive",),
    ),
    InputOperation(
        "report_input_problem",
        "Explain why the supplied information does not establish an executable "
        "spectrum and formula selection. Name what is missing or ambiguous; do "
        "not claim that unreadable data proves invalid chemistry.",
        _arguments({"explanation": _EXPLANATION}),
    ),
)


def interpreter_tools() -> list[dict]:
    """Render the same supported operations in Chat Completions tool syntax."""
    return [{"type": "function", "function": {
        "name": operation.name, "description": operation.description,
        "parameters": operation.parameters,
    }} for operation in INPUT_OPERATIONS]
