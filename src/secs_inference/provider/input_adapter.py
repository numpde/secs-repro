"""Discover exact scientific representations and prepare one verified choice."""

from __future__ import annotations

from base64 import urlsafe_b64encode
from dataclasses import dataclass
import hmac
import json
import secrets

import numpy as np
from numpy.typing import NDArray

from secs_inference.provider.input_formats import (
    DiscoveredRepresentation,
    discover_representations,
    prepare_representation,
    read_declared_companions,
    safe_text,
)
from secs_inference.provider.input_operations import (
    SourceRef,
    source_document,
)
from secs_inference.provider.source_access import InputReadError, ReadSource, SourceAccess

_TOKEN_PREFIX = "secs-input-v1."
_MAX_INVENTORY_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class PreparedSpectrum:
    """The verified model input and the processing facts that produced it."""

    representation_id: str
    sources: tuple[SourceRef, ...]
    values: NDArray[np.float32]
    from_fid: bool = False
    magnitude: bool = False


class InputAdapter:
    """Own representation discovery, identity and exact preparation."""

    def __init__(self, *, token_key: bytes | None = None):
        self._token_key = token_key if token_key is not None else secrets.token_bytes(32)
        self._attempt_ref: str | None = None
        self._claims: dict[str, dict] = {}

    def discover(self, access: SourceAccess, attempt_ref: str, scope: SourceRef) -> dict:
        """Return every recognized representation in the requested source scope."""
        if attempt_ref != self._attempt_ref:
            self._attempt_ref = attempt_ref
            self._claims.clear()
        entries = access.scope(scope)
        read_sources: list[ReadSource] = []
        issues = []
        complete = True
        for entry in entries:
            try:
                read_sources.append(access.read(entry.source))
            except InputReadError as error:
                complete = False
                issues.append({"source": source_document(entry.source), "reason": str(error)})

        if scope.member is not None and read_sources:
            read_sources.extend(read_declared_companions(access, read_sources[0]))

        discovered, format_issues = discover_representations(access, read_sources)
        if format_issues:
            complete = False
            issues.extend(format_issues)

        identities = {item.key: self._encode_claim(attempt_ref, item) for item in discovered}
        representations = []
        for item in discovered:
            representations.append({
                "id": identities[item.key],
                "kind": item.kind,
                "sources": [source_document(component.source) for component in item.components],
                "metadata": item.metadata,
                "related_ids": [identities[key] for key in item.related_keys if key in identities],
            })
        result = {"representations": representations, "complete": complete, "issues": issues}
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_INVENTORY_BYTES:
            raise InputReadError(
                "Cannot inspect this source: its discovered representation inventory exceeds the "
                f"{_MAX_INVENTORY_BYTES}-byte result limit; inspect an exact member to narrow the scope"
            )
        return result

    def prepare(
        self,
        access: SourceAccess,
        attempt_ref: str,
        selection: dict,
    ) -> PreparedSpectrum:
        """Verify one issued representation and prepare exactly that choice."""
        required = {"representation_id", "formula", "formula_evidence", "processing", "explanation"}
        if type(selection) is not dict or set(selection) != required:
            missing = required - set(selection) if type(selection) is dict else required
            if "representation_id" in missing:
                raise InputReadError("Cannot execute this selection: a representation identity is required")
            if "formula" in missing:
                raise InputReadError("Cannot execute this selection: a molecular formula is required")
            if "formula_evidence" in missing:
                raise InputReadError("Cannot execute this selection: formula evidence is required")
            if "processing" in missing:
                raise InputReadError("Cannot execute this selection: an explicit processing choice is required")
            raise InputReadError("Cannot execute this selection: its fields do not match the selection contract")
        for name in ("representation_id", "formula", "processing", "explanation"):
            if type(selection[name]) is not str or not selection[name].strip():
                raise InputReadError(f"Cannot execute this selection: {name.replace('_', ' ')} must be nonempty text")

        claim = self._decode_claim(selection["representation_id"], attempt_ref)
        current = self._verify_components(access, claim)
        self._verify_formula_evidence(access, attempt_ref, selection["formula"], selection["formula_evidence"])
        metadata = claim["metadata"]
        kind = claim["kind"]
        if kind == "structure":
            raise InputReadError("Cannot execute the selected structure: SECS requires 1H spectrum data")
        if kind == "peak_table":
            raise InputReadError("Cannot execute the selected peak table: SECS requires 1H spectrum data")
        if kind == "fid":
            if selection["processing"] != "auto":
                raise InputReadError("Cannot execute the selected FID as stored: SECS requires processing")
        dimension = metadata.get("dimension")
        if dimension != 1:
            name = "unknown-dimensional" if dimension is None else f"{dimension}D"
            raise InputReadError(f"Cannot execute the selected {name} spectrum: SECS requires 1D spectrum data with established dimensionality")
        nucleus = metadata.get("nucleus")
        if nucleus != "1H":
            name = "unknown-nucleus" if nucleus is None else safe_text(str(nucleus))
            raise InputReadError(f"Cannot execute the selected {name} spectrum: SECS requires 1H spectrum data")
        if kind != "fid" and selection["processing"] != "as_stored":
            raise InputReadError("Cannot execute the selected processed spectrum: processing must be as_stored")

        values, from_fid, magnitude = prepare_representation(access, claim, current)
        return PreparedSpectrum(
            selection["representation_id"],
            tuple(_source_from_document(component["source"]) for component in claim["components"]),
            values,
            from_fid,
            magnitude,
        )

    def _encode_claim(self, attempt_ref: str, item: DiscoveredRepresentation) -> str:
        document = {
            "version": 1,
            "attempt_ref": attempt_ref,
            "locator": item.locator,
            "kind": item.kind,
            "components": [{"role": component.role, "source": source_document(component.source),
                            "digest": component.digest} for component in item.components],
            "metadata": item.metadata,
        }
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        identity = _TOKEN_PREFIX + urlsafe_b64encode(
            hmac.digest(self._token_key, payload, "sha256")
        ).decode("ascii").rstrip("=")
        self._claims[identity] = document
        return identity

    def _decode_claim(self, identity: str, attempt_ref: str) -> dict:
        if not identity.startswith(_TOKEN_PREFIX):
            raise InputReadError("Cannot execute this selection: the representation identity was not issued for this Attempt")
        try:
            claim = self._claims[identity]
        except KeyError:
            raise InputReadError("Cannot execute this selection: the representation identity was not issued for this Attempt")
        if claim.get("version") != 1 or claim.get("attempt_ref") != attempt_ref:
            raise InputReadError("Cannot execute this selection: the representation identity was not issued for this Attempt")
        return claim

    def _verify_components(self, access: SourceAccess, claim: dict) -> list[ReadSource]:
        try:
            components = claim["components"]
            if type(components) is not list or not components:
                raise KeyError
            sources = [source_from_document(component["source"]) for component in components]
            digests = [component["digest"] for component in components]
        except (KeyError, TypeError) as error:
            raise InputReadError(
                "Cannot execute this selection: the representation is unavailable in the current Attempt"
            ) from error
        current = []
        for source, expected_digest in zip(sources, digests):
            try:
                read = access.read(source)
            except InputReadError as error:
                raise InputReadError(
                    "Cannot execute this selection: the representation is unavailable in the current Attempt"
                ) from error
            if read.digest != expected_digest:
                raise InputReadError(
                    "Cannot execute this selection: the representation source bytes changed after inspection"
                )
            current.append(read)
        return current

    def _verify_formula_evidence(self, access, attempt_ref, formula, evidence):
        if evidence == {"kind": "job_specification"}:
            return
        if (type(evidence) is not dict or set(evidence) != {"kind", "representation_ids"}
                or evidence.get("kind") != "representations"
                or type(evidence.get("representation_ids")) is not list
                or not evidence["representation_ids"]
                or len(evidence["representation_ids"]) > 16
                or any(type(identity) is not str or not identity for identity in evidence["representation_ids"])
                or len(set(evidence["representation_ids"])) != len(evidence["representation_ids"])):
            raise InputReadError("Cannot execute this selection: formula evidence is malformed")
        verified: dict[tuple, list[ReadSource]] = {}
        for identity in evidence["representation_ids"]:
            try:
                claim = self._decode_claim(identity, attempt_ref)
                component_key = tuple(
                    (component["role"], component["source"]["upload_ref"],
                     component["source"]["member"], component["digest"])
                    for component in claim["components"]
                )
                if component_key not in verified:
                    verified[component_key] = self._verify_components(access, claim)
            except InputReadError as error:
                raise InputReadError(
                    "Cannot execute this selection: a formula evidence representation is unavailable"
                ) from error
            observed = claim.get("metadata", {}).get("formula")
            if claim.get("kind") != "structure" or observed != formula:
                shown = "unknown" if observed is None else str(observed)
                raise InputReadError(
                    f"Cannot execute this selection: formula evidence reports {shown}, not {formula}"
                )


def _source_from_document(document: object) -> SourceRef:
    """Admit a claim's source identity at the selection boundary."""
    if (type(document) is not dict or set(document) != {"upload_ref", "member"}
            or type(document["upload_ref"]) is not str
            or type(document["member"]) not in {str, type(None)}):
        raise InputReadError("Cannot execute this selection: its representation source is malformed")
    return SourceRef(document["upload_ref"], document["member"])
