from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


Float64Array = NDArray[np.float64]


@dataclass(frozen=True)
class SourceSpectrum:
    ppm: Float64Array
    intensities: Float64Array
