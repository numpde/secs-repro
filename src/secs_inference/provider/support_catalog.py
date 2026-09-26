"""Own the provider's public input-support claims and their evidence requirements."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SupportCategory(StrEnum):
    SUBMISSION = "submission"
    SPECTRUM = "spectrum"
    FORMULA = "formula"
    ANNOTATION = "annotation"


@dataclass(frozen=True, slots=True)
class SupportClaim:
    category: SupportCategory
    advertise_in_offering: bool
    title: str
    public_summary: str
    limits: tuple[str, ...]
    required_evidence: tuple[str, ...]


_FIXTURE_PROVENANCE = "input.fixtures.provenance.v1"


SUPPORT_CLAIMS = (
    SupportClaim(
        SupportCategory.SUBMISSION,
        True,
        "Multiple uploads",
        "Multiple uploads are accepted",
        ("In the job input, name the intended upload or experiment path and formula source.",),
        (
            "input.multiple-uploads.selection.v1",
        ),
    ),
    SupportClaim(
        SupportCategory.SUBMISSION,
        True,
        "Vendor experiment folders",
        "Keep Bruker and Varian files in their experiment folders",
        (),
        (
            "input.bruker.processed.companion-relationships.v1",
            "input.vendor-fids.companion-relationships.v1",
        ),
    ),
    SupportClaim(
        SupportCategory.SUBMISSION,
        True,
        "Dense spectrum required",
        "Provide dense 1D proton data",
        ("Peak tables may be reported but cannot replace a spectrum.",),
        (
            "input.peak-tables.discovery.v1",
            "input.peak-tables.non-executable.v1",
        ),
    ),
    SupportClaim(
        SupportCategory.SPECTRUM,
        True,
        "Processed JCAMP-DX",
        "JCAMP-DX XYDATA or NTUPLES",
        (
            "Supported cases include LINK blocks and AFFN, FIX, SQZ, DIF, DIFDUP, and PAC numeric encodings.",
        ),
        (
            "input.jcamp.processed.inference-input.v1",
            "input.jcamp.link.inference-input.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.SPECTRUM,
        True,
        "Processed Bruker",
        "Bruker 1r with its procs file",
        (
            "Keep 1r and procs together in the same pdata directory.",
            "The processed pair does not require acqus; include title when it is available.",
        ),
        (
            "input.bruker.processed.inference-input.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.SPECTRUM,
        True,
        "JCAMP-DX FID",
        "complex JCAMP-DX FIDs with acquisition parameters",
        (),
        (
            "input.jcamp.fid.inference-input.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.SPECTRUM,
        True,
        "Bruker FID",
        "Bruker fid with acqus and zero group delay",
        (
            "Keep fid and acqus together in one experiment directory.",
            "Other group-delay profiles are reported as unsupported.",
        ),
        (
            "input.vendor-fids.inference-input.v1",
            "input.vendor-fids.profile-limits.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.SPECTRUM,
        True,
        "Varian FID",
        "Varian fid with procpar where reffrq=sfrq and rfl-rfp=sw/2",
        (
            "Keep fid and procpar together in one experiment directory.",
            "Other reference profiles are reported as unsupported.",
        ),
        (
            "input.vendor-fids.inference-input.v1",
            "input.vendor-fids.profile-limits.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.SPECTRUM,
        True,
        "Processed JEOL JDF",
        "processed JEOL JDF with a ppm axis (synthetic qualification only)",
        (),
        (
            "input.jeol.processed.inference-input.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.SPECTRUM,
        True,
        "NMRium v21",
        "NMRium v21 states with dense proton data or attached JCAMP-DX resources",
        (
            "Keep embedded resources with the state; stored spectrum shifts are applied once.",
        ),
        (
            "input.nmrium.v21.inference-input.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.FORMULA,
        True,
        "Formula in the job input",
        "a formula written verbatim in the job input",
        (),
        (
            "input.job-formula.execution.v1",
            "input.job-formula.exact-quote.v1",
        ),
    ),
    SupportClaim(
        SupportCategory.FORMULA,
        True,
        "Formula from a structure",
        "a formula calculated from a selected MOL, SDF or SMILES attachment",
        (),
        (
            "input.structure-formula.execution.v1",
            "input.structure-formula.rejections.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.ANNOTATION,
        False,
        "NMReDATA relationships",
        "NMReDATA links between a structure, its JCAMP-DX resource and proton assignments",
        (
            "The linked spectrum resource must be attached and readable.",
            "Assignments are preserved as supplied context; they do not change the spectrum sent to SECS.",
        ),
        (
            "input.nmredata.relationships.v1",
            "input.nmredata.resource-limits.v1",
            "input.nmredata.annotations-no-spectrum-change.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
)
