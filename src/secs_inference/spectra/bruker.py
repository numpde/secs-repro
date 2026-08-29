from pathlib import Path

import nmrglue as ng
import numpy as np

from secs_inference.spectra.source import SourceSpectrum


def read_processed_bruker(upload_directory: str | Path) -> SourceSpectrum:
    """Read processing number 1 and reconstruct its endpoint-inclusive ppm axis."""
    processed_directory = Path(upload_directory) / "pdata" / "1"
    parameters, intensities = ng.bruker.read_pdata(
        processed_directory,
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
