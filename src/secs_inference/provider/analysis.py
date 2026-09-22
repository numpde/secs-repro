"""Declare the SECS analysis offering and the spectrum formats it admits."""

from __future__ import annotations

ANALYSIS_KIND_REF = "mol_from_1h_spectrum_formula"

ADMISSIBLE_SPECTRUM_FORMATS = (
    "one-dimensional JCAMP-DX spectra",
    "processed Bruker data",
    "qualified JCAMP-DX, Bruker or Varian FIDs",
    "compatible processed JEOL JDF data",
    "NMRium v21 states with dense or declared JCAMP-DX data",
)


def analysis_offering_description() -> str:
    """Render the API description from the admitted spectrum format inventory."""

    formats = "; ".join(ADMISSIBLE_SPECTRUM_FORMATS)
    return (
        "SECS ranks candidate structures from a molecular formula and one usable 1D "
        "proton NMR spectrum or processable FID. For Bruker or Varian data, upload "
        "each complete experiment folder to keep its data and parameter-file paths "
        "together. You can attach multiple uploads and say which sample or experiment "
        "you want. The input interpreter inspects uploads and uses your instructions "
        "and file evidence to choose the best usable input. Put the formula in the "
        "job input, or attach a MOL, SDF "
        f"or SMILES structure that supplies it. Supported spectrum data include {formats}. "
        "The result reports the spectrum and formula used; check them before "
        "interpreting the ranking."
    )
