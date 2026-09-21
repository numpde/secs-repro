"""Declare the SECS analysis offering and the spectrum formats it admits."""

from __future__ import annotations

ANALYSIS_KIND_REF = "mol_from_1h_spectrum_formula"

ADMISSIBLE_SPECTRUM_FORMATS = (
    "one-dimensional JCAMP-DX spectra in XYDATA or NTUPLES and complex JCAMP-DX FIDs",
    "processed Bruker pdata directories containing 1r and procs",
    "raw one-dimensional Bruker and Varian FIDs within the qualified zero-delay or centered-reference profiles",
    "reference-compatible processed one-dimensional JEOL JDF data with an explicit stored ppm axis",
    "NMRium v21 saved states containing dense spectra or declared JCAMP-DX resources",
)


def analysis_offering_description() -> str:
    """Render the API description from the admitted spectrum format inventory."""

    formats = "; ".join(ADMISSIBLE_SPECTRUM_FORMATS)
    return (
        "Retrieves and refines candidate molecular structures from a molecular "
        "formula and one one-dimensional proton NMR spectrum or processable FID. "
        f"Accepted spectrum inputs: {formats}. Results are ranked candidate "
        "proposals, not validated structure assignments."
    )
