from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


Float32Array = NDArray[np.float32]
Float64Array = NDArray[np.float64]

SECS_PPM_FROM = -2.0
SECS_PPM_TO = 10.0
SECS_SPECTRUM_POINTS = 10_000


@dataclass(frozen=True)
class SourceSpectrum:
    ppm: Float64Array
    intensities: Float64Array


def prepare_secs_spectrum(source: SourceSpectrum) -> Float32Array:
    """Resample source data onto the ascending grid expected by SECS."""
    order = np.argsort(source.ppm, kind="stable")
    ppm = source.ppm[order]
    intensities = source.intensities[order]
    resampled = _smooth_resample(
        ppm,
        intensities,
        start=SECS_PPM_FROM,
        stop=SECS_PPM_TO,
        points=SECS_SPECTRUM_POINTS,
    )
    minimum = float(np.min(resampled))
    maximum = float(np.max(resampled))
    return ((resampled - minimum) / (maximum - minimum)).astype(np.float32)


def _smooth_resample(
    ppm: Float64Array,
    intensities: Float64Array,
    *,
    start: float,
    stop: float,
    points: int,
) -> Float64Array:
    """Average the piecewise-linear spectrum across each destination bin."""
    source_step_before = ppm[1] - ppm[0]
    source_step_after = ppm[-1] - ppm[-2]
    extended_ppm = np.concatenate(
        ([ppm[0] - source_step_before], ppm, [ppm[-1] + source_step_after]),
    )
    extended_intensities = np.concatenate(([0.0], intensities, [0.0]))

    widths = np.diff(extended_ppm)
    cumulative_integral = np.concatenate(
        (
            [0.0],
            np.cumsum(
                widths
                * (extended_intensities[:-1] + extended_intensities[1:])
                / 2,
            ),
        ),
    )

    destination_step = (stop - start) / (points - 1)
    edges = np.linspace(
        start - destination_step / 2,
        stop + destination_step / 2,
        points + 1,
    )
    integral_at_edges = _piecewise_linear_integral(
        edges,
        extended_ppm,
        extended_intensities,
        cumulative_integral,
    )
    return np.diff(integral_at_edges) / destination_step


def _piecewise_linear_integral(
    coordinates: Float64Array,
    ppm: Float64Array,
    intensities: Float64Array,
    cumulative_integral: Float64Array,
) -> Float64Array:
    segments = np.searchsorted(ppm, coordinates, side="right") - 1
    segments = np.clip(segments, 0, ppm.size - 2)
    offsets = coordinates - ppm[segments]
    slopes = (
        (intensities[segments + 1] - intensities[segments])
        / (ppm[segments + 1] - ppm[segments])
    )
    integral = (
        cumulative_integral[segments]
        + intensities[segments] * offsets
        + slopes * offsets**2 / 2
    )
    integral = np.where(coordinates <= ppm[0], 0.0, integral)
    return np.where(
        coordinates >= ppm[-1],
        cumulative_integral[-1],
        integral,
    )
