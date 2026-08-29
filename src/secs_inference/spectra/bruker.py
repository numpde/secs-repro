from pathlib import Path

import nmrglue as ng
import numpy as np

from secs_inference.spectra.source import SourceSpectrum


def read_bruker_pdata(processed_directory: str | Path) -> SourceSpectrum:
    """Read one Bruker pdata directory and reconstruct its endpoint-inclusive ppm axis."""
    parameters, intensities = ng.bruker.read_pdata(
        Path(processed_directory),
        scale_data=True,
    )
    procs = parameters["procs"]
    offset = float(procs["OFFSET"])
    width = float(procs["SW_p"]) / float(procs["SF"])
    ppm = np.linspace(offset, offset - width, intensities.size, dtype=np.float64)
    return SourceSpectrum(
        ppm=ppm,
        intensities=np.asarray(intensities, dtype=np.float64),
    )
