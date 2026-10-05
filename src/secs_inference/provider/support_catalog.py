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
        "A job may contain multiple uploads",
        ("In the job instructions, name the intended upload or experiment path and formula source.",),
        (
            "input.multiple-uploads.selection.v1",
        ),
    ),
    SupportClaim(
        SupportCategory.SUBMISSION,
        True,
        "Vendor experiment folders",
        "Keep each Bruker or Varian experiment in one upload",
        (
            "For a folder, use one ZIP with relative paths intact; separate uploads are not joined.",
        ),
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
            "Reads LINK blocks and AFFN, FIX, SQZ, DIF, DIFDUP, and PAC numeric encodings.",
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
            "The processed pair does not require acqus; include title when present.",
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
        "Complex JCAMP-DX FIDs with acquisition parameters",
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
            "Rejects other group-delay profiles.",
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
            "Rejects other reference profiles.",
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
        "Processed JEOL JDF with a ppm axis",
        ("Tested only with synthetic files, not instrument output.",),
        (
            "input.jeol.processed.inference-input.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.SPECTRUM,
        True,
        "NMRium v21",
        "NMRium v21 states with dense proton data or a referenced JCAMP-DX resource",
        (
            "Keep the referenced resource at its expected relative path in the same ZIP; stored spectrum shifts are applied once.",
        ),
        (
            "input.nmrium.v21.inference-input.v1",
            _FIXTURE_PROVENANCE,
        ),
    ),
    SupportClaim(
        SupportCategory.FORMULA,
        True,
        "Formula in the job instructions",
        "A formula written verbatim in the job instructions",
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
        "A formula calculated from a selected MOL, SDF or SMILES attachment",
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
