"""Run typed tool interpretation across an ordered set of model endpoints.

This is the transport-independent portion of the proven Magnet interpreter.
It owns prompt assembly, bounded same-conversation repair, endpoint fallback,
and generic tool dispatch. Callers inject endpoint transport, analysis-specific
construction, deterministic admission, and the operator-event destination.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from itertools import count
import math
from pathlib import Path
import re
from typing import Generic, Protocol, TypeVar

from secs_inference.provider.provider_events import InterpreterEndpointFailed
from secs_inference.provider.text_provenance import (
    ModelGeneratedText,
    ProviderDiagnosticText,
    UserProvidedText,
)


T = TypeVar("T")
MAX_TURNS_PER_ENDPOINT = 3
MAX_INTERPRETER_ENDPOINTS = 4
MAX_INTERPRETER_MESSAGE_CHARACTERS = 1_024
_MAX_PROMPT_BYTES = 64 * 1024
_CONFIGURATION_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
_SYSTEM_PROMPT_PATH = _PROMPT_DIRECTORY / "interpreter.md"
_CORRECTION_PROMPT_PATH = _PROMPT_DIRECTORY / "protocol_correction.md"

PromptMessage = dict[str, object]
InterpreterPrompt = list[PromptMessage]


class InterpreterProtocolError(ValueError):
    """An assistant turn did not satisfy the generic or capability contract."""

    def __init__(self, reason: str) -> None:
        if type(reason) is not str or not reason.strip():
            raise TypeError("Interpreter protocol reason must be non-blank text")
        self.reason = reason
        super().__init__(reason)


class InterpreterTransportError(RuntimeError):
    """One endpoint failed with a content-free operational reason."""

    def __init__(self, reason: str = "unclassified") -> None:
        self.reason = _require_failure_reason(reason)
        super().__init__(self.reason)


class InterpreterUnavailableReason(StrEnum):
    """The factual cause known at the interpreter boundary."""

    PROMPT_UNAVAILABLE = "prompt_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    ENDPOINTS_EXHAUSTED = "endpoints_exhausted"


class InterpreterUnavailable(RuntimeError):
    """No configured endpoint produced a trustworthy interpretation."""

    def __init__(
        self,
        reason: InterpreterUnavailableReason,
        attempted_configuration_ids: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason
        self.attempted_configuration_ids = attempted_configuration_ids
        attempted = ",".join(attempted_configuration_ids) or "none"
        super().__init__(
            f"{reason.value}; attempted interpreter endpoints: {attempted}"
        )


class ReportedInputProblem(ValueError):
    """The model completed interpretation by explaining a caller problem."""

    def __init__(
        self,
        message: ModelGeneratedText,
        *,
        configuration_id: str,
        attempted_configuration_ids: tuple[str, ...],
    ) -> None:
        if not _is_interpreter_message(message):
            raise TypeError("message must satisfy the interpreter text contract")
        self.message = message
        self.configuration_id = configuration_id
        self.attempted_configuration_ids = attempted_configuration_ids
        # Model prose is deliberately projected through ``message``. Avoid
        # making incidental exception rendering another disclosure path.
        super().__init__("reported_input_problem")


class InterpretationCandidateRejected(ValueError):
    """Deterministic admission supplied safe evidence for model correction."""

    def __init__(self, diagnostic: ProviderDiagnosticText) -> None:
        if not _is_interpreter_message(diagnostic):
            raise TypeError("diagnostic must satisfy the interpreter text contract")
        self.diagnostic = diagnostic
        super().__init__(diagnostic)


class InterpretationRejected(ValueError):
    """Every endpoint exhausted deterministic candidate repair."""

    def __init__(
        self,
        diagnostic: ProviderDiagnosticText,
        *,
        configuration_id: str,
        attempted_configuration_ids: tuple[str, ...],
    ) -> None:
        self.diagnostic = diagnostic
        self.configuration_id = configuration_id
        self.attempted_configuration_ids = attempted_configuration_ids
        super().__init__(diagnostic)


class InterpreterTool(StrEnum):
    SUBMIT_INTERPRETATION = "submit_interpretation"
    REPORT_INPUT_PROBLEM = "report_input_problem"


@dataclass(frozen=True, slots=True)
class InterpreterToolInvocation:
    """One transport-parsed invocation; generic dispatch validates its fields."""

    name: object
    arguments: object


@dataclass(frozen=True, slots=True)
class InterpreterTurn:
    """One assistant message and its best-effort tool projection.

    A tuple of retained tool-call IDs permits faithful continuation. ``None``
    means the adapter could not preserve enough context for a repair turn.
    """

    assistant_message: PromptMessage
    invocation: InterpreterToolInvocation | None
    tool_call_ids: tuple[str, ...] | None

    def __post_init__(self) -> None:
        if type(self.assistant_message) is not dict:
            raise TypeError("assistant_message must be an object")
        if (
            self.invocation is not None
            and type(self.invocation) is not InterpreterToolInvocation
        ):
            raise TypeError("invocation must be a tool invocation or None")
        if self.tool_call_ids is not None and (
            type(self.tool_call_ids) is not tuple
            or any(
                type(call_id) is not str or not call_id
                for call_id in self.tool_call_ids
            )
        ):
            raise TypeError("tool_call_ids must be a tuple of non-empty text")


InterpreterCall = Callable[[InterpreterPrompt], Awaitable[InterpreterTurn]]
InterpretationAdmission = Callable[[T], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class InterpreterEndpoint:
    """One configured model destination in fallback order."""

    configuration_id: str
    model: str
    call: InterpreterCall

    def __post_init__(self) -> None:
        if (
            type(self.configuration_id) is not str
            or _CONFIGURATION_ID.fullmatch(self.configuration_id) is None
        ):
            raise TypeError("configuration_id must be a bounded safe identifier")
        if type(self.model) is not str or not self.model.strip():
            raise TypeError("model must be bounded non-blank UTF-8 text")
        try:
            model_bytes = self.model.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise TypeError("model must be bounded non-blank UTF-8 text") from None
        if len(model_bytes) > 256:
            raise TypeError("model must be bounded non-blank UTF-8 text")
        if not callable(self.call):
            raise TypeError("call must be callable")


@dataclass(frozen=True, slots=True)
class InterpretationResult(Generic[T]):
    """A typed interpretation plus endpoint-selection provenance."""

    value: T
    configuration_id: str
    model: str
    attempted_configuration_ids: tuple[str, ...]


class InterpretationCapability(Protocol[T]):
    """Supply analysis instructions and construct its typed model proposal."""

    @property
    def interpreter_prompt_path(self) -> Path: ...

    @property
    def interpreter_context(self) -> str: ...

    def construct_interpretation(self, value: object, /) -> T: ...


class _ReportedInputProblem(ValueError):
    def __init__(self, message: ModelGeneratedText) -> None:
        self.message = message


async def interpret(
    *,
    source_text: UserProvidedText,
    capability: InterpretationCapability[T],
    endpoints: tuple[InterpreterEndpoint, ...],
    interpretation_timeout_seconds: float,
    report_endpoint_failure: Callable[[InterpreterEndpointFailed], None],
    admit_interpretation: InterpretationAdmission[T],
) -> InterpretationResult[T]:
    """Return one typed interpretation using bounded repair and fallback."""

    if type(source_text) is not str or not source_text:
        raise TypeError("source_text must be non-empty text")
    _require_endpoints(endpoints)
    timeout = _require_timeout(interpretation_timeout_seconds)
    try:
        capability_prompt = _read_prompt(capability.interpreter_prompt_path)
        capability_context = _require_prompt(capability.interpreter_context)
        base_prompt = [
            {"role": "system", "content": _read_prompt(_SYSTEM_PROMPT_PATH)},
            {
                "role": "user",
                "content": _require_prompt(
                    f"{capability_prompt}\n\n{capability_context}"
                ),
            },
            {"role": "user", "content": source_text},
        ]
        correction = _read_prompt(_CORRECTION_PROMPT_PATH)
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        # Broken prompt deployment is operational unavailability, never
        # evidence that the caller supplied bad scientific input.
        raise InterpreterUnavailable(
            InterpreterUnavailableReason.PROMPT_UNAVAILABLE
        ) from error

    attempted: list[str] = []
    failures: list[BaseException] = []
    last_rejection: ProviderDiagnosticText | None = None
    only_admission_exhaustion = True
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            for endpoint in endpoints:
                attempted.append(endpoint.configuration_id)
                prompt = list(base_prompt)
                for turn_number in count(1):
                    try:
                        # An endpoint owns transport, not shared prompt state.
                        response = await endpoint.call(deepcopy(prompt))
                    except InterpreterTransportError as error:
                        only_admission_exhaustion = False
                        failures.append(error)
                        _report_failure(
                            report_endpoint_failure,
                            endpoint.configuration_id,
                            "transport",
                            error.reason,
                        )
                        break
                    if type(response) is not InterpreterTurn:
                        only_admission_exhaustion = False
                        error = InterpreterProtocolError("invalid_turn_type")
                        failures.append(error)
                        _report_failure(
                            report_endpoint_failure,
                            endpoint.configuration_id,
                            "protocol",
                            error.reason,
                        )
                        break

                    repairable = response.tool_call_ids is not None
                    exhausted = turn_number >= MAX_TURNS_PER_ENDPOINT
                    try:
                        value = _dispatch_turn(response, capability)
                        await admit_interpretation(value)
                    except _ReportedInputProblem as problem:
                        raise ReportedInputProblem(
                            problem.message,
                            configuration_id=endpoint.configuration_id,
                            attempted_configuration_ids=tuple(attempted),
                        ) from None
                    except InterpretationCandidateRejected as rejection:
                        last_rejection = rejection.diagnostic
                        if exhausted or not repairable:
                            failures.append(rejection)
                            if not exhausted:
                                only_admission_exhaustion = False
                            _report_failure(
                                report_endpoint_failure,
                                endpoint.configuration_id,
                                "admission",
                                rejection.diagnostic,
                                (
                                    "repair_exhausted"
                                    if exhausted
                                    else "repair_unavailable"
                                ),
                            )
                            break
                        _append_repair(
                            prompt,
                            response,
                            rejection.diagnostic,
                            correction,
                        )
                        continue
                    except InterpreterProtocolError as error:
                        if exhausted or not repairable:
                            only_admission_exhaustion = False
                            failures.append(error)
                            _report_failure(
                                report_endpoint_failure,
                                endpoint.configuration_id,
                                "protocol",
                                error.reason,
                                (
                                    "repair_exhausted"
                                    if exhausted
                                    else "repair_unavailable"
                                ),
                            )
                            break
                        _append_repair(prompt, response, error.reason, correction)
                        continue

                    return InterpretationResult(
                        value=value,
                        configuration_id=endpoint.configuration_id,
                        model=endpoint.model,
                        attempted_configuration_ids=tuple(attempted),
                    )
    except TimeoutError as error:
        if not deadline.expired():
            raise
        if attempted:
            _report_failure(
                report_endpoint_failure,
                attempted[-1],
                "timeout",
                "aggregate_timeout",
            )
        raise InterpreterUnavailable(
            InterpreterUnavailableReason.DEADLINE_EXCEEDED,
            tuple(attempted),
        ) from error

    if last_rejection is not None and only_admission_exhaustion:
        rejected = InterpretationRejected(
            last_rejection,
            configuration_id=attempted[-1],
            attempted_configuration_ids=tuple(attempted),
        )
        if failures:
            raise rejected from ExceptionGroup(
                "Interpreter candidate rejections", failures
            )
        raise rejected
    unavailable = InterpreterUnavailable(
        InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED,
        tuple(attempted),
    )
    if failures:
        raise unavailable from ExceptionGroup("Interpreter endpoint failures", failures)
    raise unavailable


def _append_repair(
    prompt: InterpreterPrompt,
    response: InterpreterTurn,
    tool_result: str,
    correction: str,
) -> None:
    if response.tool_call_ids is None:
        raise AssertionError("repair requires retained assistant context")
    prompt.append(response.assistant_message)
    prompt.extend(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": tool_result,
        }
        for call_id in response.tool_call_ids
    )
    prompt.append({"role": "user", "content": correction})


def _dispatch_turn(
    turn: InterpreterTurn,
    capability: InterpretationCapability[T],
) -> T:
    invocation = turn.invocation
    if type(invocation) is not InterpreterToolInvocation:
        raise InterpreterProtocolError("missing_tool_invocation")
    if type(invocation.name) is not str or type(invocation.arguments) is not dict:
        raise InterpreterProtocolError("invalid_tool_invocation_fields")
    arguments = invocation.arguments
    if (
        invocation.name == InterpreterTool.SUBMIT_INTERPRETATION
        and set(arguments) == {"value"}
    ):
        return capability.construct_interpretation(arguments["value"])
    if (
        invocation.name == InterpreterTool.REPORT_INPUT_PROBLEM
        and set(arguments) == {"message"}
        and _is_interpreter_message(arguments["message"])
    ):
        raise _ReportedInputProblem(ModelGeneratedText(arguments["message"]))
    raise InterpreterProtocolError("unexpected_tool_invocation")


def _report_failure(
    report: Callable[[InterpreterEndpointFailed], None],
    configuration_id: str,
    failure_kind: str,
    failure_reason: str,
    failure_state: str | None = None,
) -> None:
    report(
        InterpreterEndpointFailed(
            configuration_id=configuration_id,
            failure_kind=failure_kind,
            failure_reason=failure_reason,
            failure_state=failure_state,
        )
    )


def _require_endpoints(endpoints: tuple[InterpreterEndpoint, ...]) -> None:
    if type(endpoints) is not tuple or not endpoints:
        raise TypeError("endpoints must be a non-empty tuple")
    if len(endpoints) > MAX_INTERPRETER_ENDPOINTS:
        raise ValueError(
            f"at most {MAX_INTERPRETER_ENDPOINTS} interpreter endpoints are supported"
        )
    configuration_ids = set()
    for endpoint in endpoints:
        if type(endpoint) is not InterpreterEndpoint:
            raise TypeError("endpoints must contain InterpreterEndpoint values")
        if endpoint.configuration_id in configuration_ids:
            raise ValueError("interpreter configuration IDs must be unique")
        configuration_ids.add(endpoint.configuration_id)


def _require_timeout(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(
            "interpretation_timeout_seconds must be positive and finite"
        )
    return float(value)


def _require_failure_reason(reason: object) -> str:
    if (
        type(reason) is not str
        or not reason
        or len(reason) > 64
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in reason
        )
    ):
        raise TypeError("reason must be a bounded safe identifier")
    return reason


def _read_prompt(path: Path) -> str:
    with path.open("rb") as prompt_file:
        raw = prompt_file.read(_MAX_PROMPT_BYTES + 1)
    if not raw or len(raw) > _MAX_PROMPT_BYTES:
        raise ValueError("prompt must be non-empty and bounded")
    return _require_prompt(raw.decode("utf-8", errors="strict"))


def _require_prompt(prompt: object) -> str:
    if type(prompt) is not str or not prompt.strip():
        raise ValueError("prompt must contain non-whitespace text")
    if len(prompt.encode("utf-8", errors="strict")) > _MAX_PROMPT_BYTES:
        raise ValueError("prompt must be bounded")
    return prompt


def _is_interpreter_message(value: object) -> bool:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MAX_INTERPRETER_MESSAGE_CHARACTERS
        or "\0" in value
    ):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True
