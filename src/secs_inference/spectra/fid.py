"""Turn one admitted complex JCAMP FID into a qualified frequency spectrum."""

from pathlib import Path
import warnings

import nmrglue as ng
import numpy as np

from secs_inference.spectra.errors import SpectrumReadError
from secs_inference.spectra.source import SourceSpectrum


def process_jcamp_fid(path: str | Path) -> tuple[SourceSpectrum, bool]:
    """Apply the qualified 1 Hz window, double zero fill, FFT and phase decision."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            parameters, decoded = ng.jcampdx.read(Path(path))
    except (AttributeError, IndexError, TypeError, ValueError) as error:
        raise SpectrumReadError("Cannot process this JCAMP-DX FID because its complex data could not be decoded") from error
    if (not isinstance(decoded, list) or len(decoded) != 2
            or any(not isinstance(channel, np.ndarray) or channel.ndim != 1 for channel in decoded)):
        raise SpectrumReadError("Cannot process this JCAMP-DX FID because it does not contain one complex trace")
    real, imaginary = (np.asarray(channel, dtype=np.float64) for channel in decoded)
    if real.size < 2 or real.size != imaginary.size or not np.all(np.isfinite(real + imaginary)):
        raise SpectrumReadError("Cannot process this JCAMP-DX FID because its complex trace is incomplete")
    sweep_ppm = _number(parameters, "$SW")
    frequency_mhz = _number(parameters, "$BF1")
    if sweep_ppm <= 0 or frequency_mhz <= 0:
        raise SpectrumReadError("Cannot process this JCAMP-DX FID because its sweep width or frequency is invalid")

    o1_hz = _optional_number(parameters, "$O1")
    sfo1_mhz = _optional_number(parameters, "$SFO1")
    if o1_hz is not None and sfo1_mhz is not None and sfo1_mhz > 0:
        center_ppm = o1_hz / sfo1_mhz
    else:
        center_ppm = _number(parameters, "$OFFSET")
    return process_complex_fid(real, imaginary, sweep_ppm, frequency_mhz, center_ppm)


def process_complex_fid(real, imaginary, sweep_ppm: float, frequency_mhz: float,
                        center_ppm: float) -> tuple[SourceSpectrum, bool]:
    """Apply the qualified automatic pipeline to one admitted complex trace."""
    real = np.asarray(real, dtype=np.float64)
    imaginary = np.asarray(imaginary, dtype=np.float64)
    if (real.ndim != 1 or imaginary.ndim != 1 or real.size < 2
            or real.size != imaginary.size
            or not np.all(np.isfinite(real)) or not np.all(np.isfinite(imaginary))):
        raise SpectrumReadError("Cannot process this FID because its complex trace is incomplete")
    if (not np.isfinite(sweep_ppm) or not np.isfinite(frequency_mhz) or not np.isfinite(center_ppm)
            or sweep_ppm <= 0 or frequency_mhz <= 0):
        raise SpectrumReadError("Cannot process this FID because its sweep width, frequency or reference is invalid")
    dwell_seconds = 1 / (sweep_ppm * frequency_mhz)
    window = np.exp(-np.pi * dwell_seconds * np.arange(real.size, dtype=np.float64))
    transformed = np.fft.fftshift(np.fft.fft((real + 1j * imaginary) * window, real.size * 2))
    phase = _positive_phase_angle(transformed.real, transformed.imag)
    phased = transformed * np.exp(-1j * np.deg2rad(phase))
    magnitude = float(np.max(phased.real)) > 0 and -float(np.min(phased.real)) > 0.2 * float(np.max(phased.real))
    intensities = np.abs(phased) if magnitude else phased.real
    ppm = np.linspace(center_ppm - sweep_ppm / 2, center_ppm + sweep_ppm / 2,
                      intensities.size, dtype=np.float64)
    return SourceSpectrum(ppm=ppm, intensities=np.asarray(intensities, dtype=np.float64)), magnitude


def _positive_phase_angle(real: np.ndarray, imaginary: np.ndarray) -> float:
    """Refine the zero-order angle that minimizes total downward signal."""
    start, stop = -180.0, 180.0
    best_angle = 0.0
    best_area = float("inf")
    for _ in range(10):
        step = (stop - start) / 7
        angle = start
        while angle <= stop:
            radians = np.deg2rad(angle)
            phased = real * np.cos(radians) - imaginary * np.sin(radians)
            negative_area = -float(np.sum(phased[phased < 0]))
            if negative_area < best_area:
                best_area = negative_area
                best_angle = angle
            angle += step
        start, stop = best_angle - step, best_angle + step
    return best_angle


def _number(parameters: dict, name: str) -> float:
    values = parameters.get(name)
    if not isinstance(values, list) or len(values) != 1:
        raise SpectrumReadError(f"Cannot process this JCAMP-DX FID because {name} is unavailable")
    try:
        value = float(values[0])
    except ValueError as error:
        raise SpectrumReadError(f"Cannot process this JCAMP-DX FID because {name} is not numeric") from error
    if not np.isfinite(value):
        raise SpectrumReadError(f"Cannot process this JCAMP-DX FID because {name} is not finite")
    return value


def _optional_number(parameters: dict, name: str) -> float | None:
    values = parameters.get(name)
    if values is None:
        return None
    return _number(parameters, name)
