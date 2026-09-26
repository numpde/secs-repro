"""Render the SECS analysis offering from its public support claims."""

from secs_inference.provider.support_catalog import (
    SUPPORT_CLAIMS,
    SupportCategory,
)

ANALYSIS_KIND_REF = "mol_from_1h_spectrum_formula"


def analysis_offering_description() -> str:
    """Render the API description from the support catalog."""

    spectra = "; ".join(
        claim.public_summary for claim in SUPPORT_CLAIMS
        if claim.advertise_in_offering and claim.category is SupportCategory.SPECTRUM
    )
    formulas = " or ".join(
        claim.public_summary for claim in SUPPORT_CLAIMS
        if claim.advertise_in_offering and claim.category is SupportCategory.FORMULA
    )
    submission = " ".join(
        " ".join((claim.public_summary + ".", *claim.limits))
        for claim in SUPPORT_CLAIMS
        if claim.advertise_in_offering and claim.category is SupportCategory.SUBMISSION
    )
    return (
        "SECS infers structures from a formula and 1D proton NMR "
        f"data. {submission} The "
        "interpreter selects usable data from the job input and files or explains why "
        f"it cannot. Formula: {formulas}. Spectra: {spectra}. "
        "Check the reported spectrum and formula."
    )
