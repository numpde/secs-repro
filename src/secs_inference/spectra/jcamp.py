"""Decode the one processed JCAMP-DX profile admitted as a SECS spectrum source."""

from decimal import Decimal
from pathlib import Path
import re
from typing import Never
import warnings

import nmrglue as ng
import numpy as np

from secs_inference.spectra.source import SourceSpectrum


_AFFN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?")


def read_jcamp_spectrum(spectrum_file: str | Path) -> SourceSpectrum:
    """Read one real 1D proton AFFN XYDATA spectrum whose X coordinates are ppm.

    Time-domain FIDs, NTUPLES, and files containing multiple data blocks remain
    unsupported because they require processing or selection policy beyond decoding.
    """
    spectrum_path = Path(spectrum_file)
    try:
        with warnings.catch_warnings(record=True) as parser_warnings:
            warnings.simplefilter("always")
            parameters, intensities = ng.jcampdx.read(spectrum_path)
    except (AttributeError, IndexError, TypeError, ValueError) as cause:
        raise ValueError(
            "Cannot read the processed JCAMP-DX spectrum because its parser "
            "could not decode the file.",
        ) from cause

    data_type = _single_parameter(parameters, "DATATYPE")
    if data_type.replace(" ", "").upper() != "NMRSPECTRUM":
        _reject("DATA TYPE does not identify an NMR spectrum")
    if _single_parameter(parameters, "DATACLASS").strip().upper() != "XYDATA":
        _reject("DATA CLASS is not XYDATA")
    nucleus = _single_parameter(parameters, ".OBSERVENUCLEUS")
    if nucleus.replace("^", "").strip().upper() != "1H":
        _reject("the observed nucleus is not 1H")
    if _single_parameter(parameters, "XUNITS").strip().upper() != "PPM":
        _reject("the X axis is not expressed in ppm")
    if _finite_parameter(parameters, "XFACTOR") != 1.0:
        _reject("XFACTOR is not 1")

    if any(
        name.startswith("_datatype_") and blocks
        for name, blocks in parameters.items()
    ):
        _reject("the file contains more than one data block")
    if parser_warnings:
        _reject(
            f"the parser reported {len(parser_warnings)} warning(s), so "
            "decoding may be incomplete",
        )
    if intensities.size < 2 or not np.all(np.isfinite(intensities)):
        _reject("it does not contain at least two finite intensity points")
    if float(np.min(intensities)) == float(np.max(intensities)):
        _reject("its intensity range is constant")

    declared_points = _integer_parameter(parameters, "NPOINTS")
    if declared_points != intensities.size:
        _reject("NPOINTS does not match the decoded intensity count")
    first_ppm = _finite_parameter(parameters, "FIRSTX")
    last_ppm = _finite_parameter(parameters, "LASTX")
    if first_ppm == last_ppm:
        _reject("FIRSTX and LASTX do not define an axis")
    declared_step = _finite_parameter(parameters, "DELTAX")
    endpoint_step = (last_ppm - first_ppm) / (declared_points - 1)
    if not np.isclose(declared_step, endpoint_step, rtol=1e-9, atol=0):
        _reject("DELTAX disagrees with FIRSTX, LASTX, and NPOINTS")

    _validate_affn_xydata(
        spectrum_path,
        first_x=first_ppm,
        delta_x=declared_step,
    )

    # FIRSTX and LASTX define the dense axis after the encoded row checkpoints
    # agree; nmrglue's universal dictionary cannot recover its absolute position.
    ppm = np.linspace(first_ppm, last_ppm, declared_points, dtype=np.float64)
    return SourceSpectrum(
        ppm=ppm,
        intensities=intensities,
    )


def _single_parameter(parameters: dict, name: str) -> str:
    values = parameters.get(name, [])
    if len(values) != 1:
        _reject(f"{name} must occur exactly once")
    return values[0]


def _finite_parameter(parameters: dict, name: str) -> float:
    text = _single_parameter(parameters, name)
    try:
        value = float(text)
    except ValueError as cause:
        raise ValueError(
            "Cannot read the processed JCAMP-DX spectrum because "
            f"{name} is not numeric.",
        ) from cause
    if not np.isfinite(value):
        _reject(f"{name} is not finite")
    return value


def _integer_parameter(parameters: dict, name: str) -> int:
    text = _single_parameter(parameters, name)
    try:
        return int(text)
    except ValueError as cause:
        raise ValueError(
            "Cannot read the processed JCAMP-DX spectrum because "
            f"{name} is not an integer.",
        ) from cause


def _validate_affn_xydata(
    spectrum_path: Path,
    *,
    first_x: float,
    delta_x: float,
) -> None:
    """Verify the X checkpoints that nmrglue deliberately omits from its result."""
    try:
        lines = spectrum_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as cause:
        raise ValueError(
            "Cannot read the processed JCAMP-DX spectrum because the file is "
            "not UTF-8 text.",
        ) from cause

    in_xydata = False
    decoded_points = 0
    for raw_line in lines:
        line = raw_line.split("$$", 1)[0].strip()
        if not line:
            continue
        if line.startswith("##"):
            if in_xydata:
                break
            label, _, value = line.partition("=")
            canonical_label = re.sub(r"[\s\-_/]", "", label[2:]).upper()
            if canonical_label == "XYDATA":
                descriptor = "".join(value.split()).upper()
                if descriptor != "(X++(Y..Y))":
                    _reject("XYDATA is not encoded as X++(Y..Y)")
                in_xydata = True
            continue
        if not in_xydata:
            continue

        fields = line.split()
        if len(fields) < 2:
            _reject("an XYDATA row does not contain an X checkpoint and Y values")
        if any(_AFFN_NUMBER.fullmatch(field) is None for field in fields):
            _reject("XYDATA is not plain numeric AFFN data")
        checkpoint_decimal = Decimal(fields[0])
        checkpoint = float(checkpoint_decimal)
        if not np.isfinite(checkpoint):
            _reject("an XYDATA X checkpoint is not finite")

        expected_checkpoint = first_x + decoded_points * delta_x
        # A checkpoint is authoritative only to the precision written in the file.
        checkpoint_tolerance = float(
            Decimal("0.5").scaleb(checkpoint_decimal.as_tuple().exponent),
        )
        if not np.isclose(
            checkpoint,
            expected_checkpoint,
            rtol=0,
            atol=checkpoint_tolerance,
        ):
            _reject("an XYDATA X checkpoint disagrees with FIRSTX and DELTAX")
        decoded_points += len(fields) - 1

    if not in_xydata:
        _reject("the file does not contain an XYDATA table")


def _reject(reason: str) -> Never:
    raise ValueError(
        f"Cannot read the processed JCAMP-DX spectrum because {reason}.",
    )
