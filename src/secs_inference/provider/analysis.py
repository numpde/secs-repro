"""Declare the SECS analysis offering and the spectrum formats it admits."""

from __future__ import annotations

ANALYSIS_KIND_REF = "mol_from_1h_spectrum_formula"

ADMISSIBLE_SPECTRUM_FORMATS = (
    "a Bruker processed pdata directory",
    (
        "a JCAMP-DX file containing one processed 1H NMR AFFN XYDATA "
        "block with a ppm axis"
    ),
)


def analysis_offering_description() -> str:
    """Render the API description from the admitted spectrum format inventory."""

    formats = "; ".join(ADMISSIBLE_SPECTRUM_FORMATS)
    return (
        "Retrieves and refines candidate molecular structures from a molecular "
        "formula and one processed one-dimensional proton NMR spectrum. "
        f"Accepted spectrum inputs: {formats}. Results are ranked candidate "
        "proposals, not validated structure assignments."
    )
