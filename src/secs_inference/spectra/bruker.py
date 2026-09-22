"""Read the selected processed Bruker real spectrum without dataset discovery."""

from pathlib import Path
import warnings

import nmrglue as ng
import numpy as np

from secs_inference.spectra.source import SourceSpectrum
from secs_inference.spectra.errors import SpectrumReadError


def read_bruker_pdata(
    processed_directory: str | Path,
    *,
    required_nucleus: str | None = "1H",
    required_dimension: int | None = 1,
) -> SourceSpectrum:
    """Decode a selected 1r/procs pair and its endpoint-inclusive ppm axis."""
    # nmrglue's discovery defaults may select another component or ascend to
    # acquisition files. The caller chose this processed pair, not a search root.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parameters, intensities = ng.bruker.read_pdata(
                Path(processed_directory), bin_files=["1r"], procs_files=["procs"],
                read_acqus=False, scale_data=True,
            )
    except (KeyError, IndexError, TypeError, ValueError, FileNotFoundError) as cause:
        raise SpectrumReadError("Cannot read the selected Bruker spectrum: its 1r/procs pair could not be decoded") from cause
    procs = parameters.get("procs", {})
    nucleus = procs.get("AXNUC")
    dimension = procs.get("PPARMOD") + 1 if type(procs.get("PPARMOD")) is int else None
    wrong_nucleus = required_nucleus is not None and nucleus != required_nucleus
    wrong_dimension = required_dimension is not None and dimension != required_dimension
    if wrong_nucleus or wrong_dimension:
        requirements = []
        if required_nucleus is not None:
            requirements.append(f"AXNUC={required_nucleus}")
        if required_dimension is not None:
            requirements.append(f"PPARMOD={required_dimension - 1}")
        raise SpectrumReadError(
            "Cannot read the selected Bruker spectrum: procs must declare " + " and ".join(requirements)
        )
    # A library warning (for example, missing optional intensity scaling) is
    # not a scientific verdict. Establish the facts used by this reader below.
    if (not isinstance(intensities, np.ndarray)
            or intensities.ndim != 1 or intensities.size < 2
            or intensities.size != procs.get("SI")
            or not np.all(np.isfinite(intensities))):
        raise SpectrumReadError("Cannot read the selected Bruker spectrum: 1r must contain finite one-dimensional data with at least two points, and its point count must match SI in procs")
    try:
        offset = float(procs["OFFSET"])
        frequency = float(procs["SF"])
        spectral_width = float(procs["SW_p"])
    except (KeyError, TypeError, ValueError) as cause:
        raise SpectrumReadError("Cannot read the selected Bruker spectrum: OFFSET, SF and SW_p must define its ppm axis") from cause
    if not np.all(np.isfinite([offset, frequency, spectral_width])) or frequency <= 0 or spectral_width <= 0:
        raise SpectrumReadError("Cannot read the selected Bruker spectrum: OFFSET must be finite; SF and SW_p must be finite and positive")
    width = spectral_width / frequency
    ppm = np.linspace(offset, offset - width, intensities.size, dtype=np.float64)
    return SourceSpectrum(
        ppm=ppm,
        intensities=np.asarray(intensities, dtype=np.float64),
    )
