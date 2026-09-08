"""Decode bounded provider responses without ambiguous JSON members.

Adapted from nmrpeak-repro's provider_response_json.py. Response spelling need
not be canonical; only signed request and retained result bytes require that.
"""

import json


def response_object(raw: bytes) -> dict:
    """Read ordinary JSON with integer numbers and one object at its root."""
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_members,
        parse_float=_reject_number,
        parse_constant=_reject_number,
    )
    if type(value) is not dict:
        raise ValueError("API response is not a JSON object")
    return value


def _unique_members(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("API response repeats a JSON member")
        result[key] = value
    return result


def _reject_number(_value: str) -> None:
    raise ValueError("API response contains a non-integer number")
