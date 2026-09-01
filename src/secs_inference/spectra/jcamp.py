"""Decode processed one-dimensional proton JCAMP-DX spectra for SECS."""

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
    """Read one processed real 1D proton spectrum from XYDATA or NTUPLES."""
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
    nucleus = _single_parameter(parameters, ".OBSERVENUCLEUS")
    if nucleus.replace("^", "").strip().upper() != "1H":
        _reject("the observed nucleus is not 1H")

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

    data_class = _single_parameter(parameters, "DATACLASS").strip().upper()
    if data_class == "XYDATA":
        return _read_xydata(spectrum_path, parameters, intensities)
    if data_class == "NTUPLES":
        return _read_ntuples(spectrum_path, parameters, intensities)
    _reject(f"DATA CLASS {data_class!r} is unsupported")


def _read_xydata(
    spectrum_path: Path,
    parameters: dict,
    decoded: object,
) -> SourceSpectrum:
    """Interpret the admitted dense XYDATA profile after nmrglue decoding."""
    if _single_parameter(parameters, "XUNITS").strip().upper() != "PPM":
        _reject("the XYDATA X axis is not expressed in ppm")
    if _finite_parameter(parameters, "XFACTOR") != 1.0:
        _reject("XYDATA XFACTOR is not 1")

    declared_points = _integer_parameter(parameters, "NPOINTS")
    intensities = _validated_intensities(decoded, declared_points)
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


def _read_ntuples(
    spectrum_path: Path,
    parameters: dict,
    decoded: object,
) -> SourceSpectrum:
    """Interpret paired real and imaginary pages as one processed spectrum."""
    ntuples = _single_parameter(parameters, "NTUPLES")
    if ntuples.replace(" ", "").upper() != "NMRSPECTRUM":
        _reject("NTUPLES does not identify an NMR spectrum")

    symbols = _comma_parameter(parameters, "SYMBOL")
    if len(symbols) != len(set(symbols)) or "" in symbols:
        _reject("NTUPLES SYMBOL entries must be nonempty and unique")
    if set(symbols) != {"X", "R", "I", "N"}:
        _reject("NTUPLES must contain X, real R, imaginary I, and page N variables")

    dimensions = _ntuple_integer_metadata(parameters, "VARDIM", symbols)
    points = dimensions["X"]
    if points < 2 or dimensions["R"] != points:
        _reject("NTUPLES X and R dimensions do not define one 1D spectrum")
    if dimensions["I"] != points:
        _reject("NTUPLES I does not have the same dimension as X")

    units = _ntuple_metadata(parameters, "UNITS", symbols)
    first = _ntuple_float_metadata(parameters, "FIRST", symbols)
    last = _ntuple_float_metadata(parameters, "LAST", symbols)
    table_channels = _ntuple_table_channels(spectrum_path)
    if len(table_channels) != 2 or set(table_channels) != {"R", "I"}:
        _reject("NTUPLES must contain one real and one imaginary DATA TABLE")

    if not isinstance(decoded, list) or len(decoded) != 2:
        _reject("the NTUPLES real and imaginary pages were not both decoded")
    intensities = _validated_intensities(decoded[0], points)

    first_x = first["X"]
    last_x = last["X"]
    if first_x == last_x:
        _reject("NTUPLES FIRST and LAST do not define an X axis")
    x_axis = np.linspace(first_x, last_x, points, dtype=np.float64)
    x_units = units["X"].upper()
    if x_units == "HZ":
        ppm = _referenced_ppm_axis(parameters, x_axis)
    elif x_units == "PPM":
        ppm = x_axis
    else:
        _reject("the NTUPLES X axis is neither Hz nor ppm")

    return SourceSpectrum(ppm=ppm, intensities=intensities)


def _ntuple_table_channels(spectrum_path: Path) -> list[str]:
    """Read the table headers that justify reconstructing a linear X axis."""
    channels: list[str] = []
    for raw_line in _read_utf8_lines(spectrum_path):
        line = raw_line.split("$$", 1)[0].strip()
        if not line.startswith("##"):
            continue
        label, separator, value = line.partition("=")
        canonical_label = re.sub(r"[\s\-_/]", "", label[2:]).upper()
        if separator == "" or canonical_label != "DATATABLE":
            continue
        header = "".join(value.split()).upper()
        match = re.fullmatch(r"\(X\+\+\(([RI])\.\.\1\)\),XYDATA", header)
        if match is None:
            _reject("an NTUPLES DATA TABLE does not define a linear X++ axis")
        channels.append(match.group(1))
    return channels


def _referenced_ppm_axis(parameters: dict, x_axis: np.ndarray) -> np.ndarray:
    if ".SHIFTREFERENCE" in parameters:
        fields = _comma_parameter(parameters, ".SHIFTREFERENCE")
        if len(fields) != 4:
            _reject(".SHIFT REFERENCE does not contain its four defined fields")
        try:
            reference_point = float(fields[2])
            reference_ppm = float(fields[3])
        except ValueError as cause:
            raise ValueError(
                "Cannot read the processed JCAMP-DX spectrum because "
                ".SHIFT REFERENCE has a non-numeric point or shift.",
            ) from cause
        if not np.isfinite(reference_point) or not np.isfinite(reference_ppm):
            _reject(".SHIFT REFERENCE has a non-finite point or shift")
        if not 1 <= reference_point <= x_axis.size:
            _reject(".SHIFT REFERENCE points outside the spectrum")

        frequency = _finite_parameter(parameters, ".OBSERVEFREQUENCY")
        if frequency <= 0:
            _reject("the observed frequency is not positive")
        # JCAMP numbers points from one; AFFN also permits a reference between points.
        reference_x = x_axis[0] + (reference_point - 1) * (
            x_axis[-1] - x_axis[0]
        ) / (x_axis.size - 1)
        return reference_ppm + (x_axis - reference_x) / frequency

    if "$OFFSET" not in parameters:
        _reject("its Hz axis has no chemical-shift reference")
    offset = _finite_parameter(parameters, "$OFFSET")
    frequency = _finite_parameter(parameters, ".OBSERVEFREQUENCY")
    if frequency <= 0:
        _reject("the observed frequency is not positive")

    # JCAMP-DX 5.0 predates the standard shift-reference label. Bruker files
    # preserve the axis origin as $OFFSET; the standard frequency converts Hz to ppm.
    return offset + (x_axis - x_axis[0]) / frequency


def _validated_intensities(
    decoded: object,
    expected_points: int,
) -> np.ndarray:
    if not isinstance(decoded, np.ndarray) or decoded.ndim != 1:
        _reject("the decoded intensity data is not one-dimensional")
    if decoded.size != expected_points:
        _reject("the declared point count does not match the decoded intensities")
    if decoded.size < 2 or not np.all(np.isfinite(decoded)):
        _reject("it does not contain at least two finite intensity points")
    if float(np.min(decoded)) == float(np.max(decoded)):
        _reject("its intensity range is constant")
    return decoded.astype(np.float64, copy=False)


def _comma_parameter(parameters: dict, name: str) -> list[str]:
    return [value.strip() for value in _single_parameter(parameters, name).split(",")]


def _ntuple_metadata(
    parameters: dict,
    name: str,
    symbols: list[str],
) -> dict[str, str]:
    values = _comma_parameter(parameters, name)
    if len(values) != len(symbols):
        _reject(f"NTUPLES {name} does not describe every variable")
    return dict(zip(symbols, values, strict=True))


def _ntuple_integer_metadata(
    parameters: dict,
    name: str,
    symbols: list[str],
) -> dict[str, int]:
    text = _ntuple_metadata(parameters, name, symbols)
    try:
        values = {symbol: int(value) for symbol, value in text.items()}
    except ValueError as cause:
        raise ValueError(
            "Cannot read the processed JCAMP-DX spectrum because "
            f"NTUPLES {name} contains a non-integer value.",
        ) from cause
    if any(value < 1 for value in values.values()):
        _reject(f"NTUPLES {name} contains a non-positive dimension")
    return values


def _ntuple_float_metadata(
    parameters: dict,
    name: str,
    symbols: list[str],
) -> dict[str, float]:
    text = _ntuple_metadata(parameters, name, symbols)
    try:
        values = {symbol: float(value) for symbol, value in text.items()}
    except ValueError as cause:
        raise ValueError(
            "Cannot read the processed JCAMP-DX spectrum because "
            f"NTUPLES {name} contains a non-numeric value.",
        ) from cause
    if not all(np.isfinite(value) for value in values.values()):
        _reject(f"NTUPLES {name} contains a non-finite value")
    return values


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
    lines = _read_utf8_lines(spectrum_path)

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


def _read_utf8_lines(spectrum_path: Path) -> list[str]:
    try:
        return spectrum_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as cause:
        raise ValueError(
            "Cannot read the processed JCAMP-DX spectrum because the file is "
            "not UTF-8 text.",
        ) from cause


def _reject(reason: str) -> Never:
    raise ValueError(
        f"Cannot read the processed JCAMP-DX spectrum because {reason}.",
    )
