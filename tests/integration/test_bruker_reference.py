import json
import unittest
from pathlib import Path

import nmrglue as ng
import numpy as np


FIXTURES = Path("/fixtures")
BRUKER_PROCESSED = FIXTURES / "bruker/F3697/1/pdata/1"
FRONTEND_REFERENCE = FIXTURES / "frontend/F3697-1.json"


class BrukerFrontendReferenceTest(unittest.TestCase):
    def test_nmrglue_reproduces_frontend_float32_input(self):
        reference = json.loads(FRONTEND_REFERENCE.read_text())
        parameters, intensities = ng.bruker.read_pdata(
            BRUKER_PROCESSED,
            scale_data=True,
        )
        universal = ng.bruker.guess_udic(parameters, intensities)
        ppm = ng.fileiobase.uc_from_udic(universal).ppm_scale()

        order = np.argsort(ppm, kind="stable")
        growing_ppm = np.asarray(ppm[order], dtype=np.float64)
        growing_intensities = np.asarray(intensities[order], dtype=np.float64)
        grid = reference["grid"]
        resampled = smooth_resample(
            growing_ppm,
            growing_intensities,
            start=float(grid["from_ppm"]),
            stop=float(grid["to_ppm"]),
            points=int(grid["points"]),
        )
        actual = min_max_scale(resampled).astype(np.float32)
        expected = np.asarray(reference["intensities"], dtype=np.float32)

        differing = np.flatnonzero(
            actual.view(np.uint32) != expected.view(np.uint32),
        )
        if differing.size:
            first = int(differing[0])
            maximum_error = float(
                np.max(np.abs(actual.astype(np.float64) - expected.astype(np.float64))),
            )
            self.fail(
                f"Float32 spectra differ at {differing.size} points; first index "
                f"{first}: nmrglue={actual[first]!r}, frontend={expected[first]!r}; "
                f"maximum absolute difference={maximum_error:.9g}",
            )


def smooth_resample(
    x: np.ndarray,
    y: np.ndarray,
    *,
    start: float,
    stop: float,
    points: int,
) -> np.ndarray:
    """Port the pinned frontend's default xyEquallySpaced smooth variant."""
    step = (stop - start) / (points - 1)
    lower = start - step / 2
    upper = start + step / 2
    output = np.empty(points, dtype=np.float64)

    previous_x = float(-(2**53 - 1))
    previous_y = 0.0
    next_x = float(x[0] - (x[1] - x[0]))
    next_y = 0.0
    cumulative = 0.0
    slope = 0.0
    intercept = 0.0
    sum_at_lower = 0.0
    input_index = 0
    output_index = 0
    last_input_step = float(x[-1] - x[-2])

    while output_index < points:
        if previous_x <= lower <= next_x:
            sum_at_lower = cumulative + line_integral(
                0.0,
                lower - previous_x,
                slope,
                previous_y,
            )

        while next_x >= upper:
            sum_at_upper = cumulative + line_integral(
                0.0,
                upper - previous_x,
                slope,
                previous_y,
            )
            output[output_index] = (sum_at_upper - sum_at_lower) / step
            output_index += 1
            if output_index == points:
                break
            lower = upper
            upper += step
            sum_at_lower = sum_at_upper

        if output_index == points:
            break

        cumulative += line_integral(
            previous_x,
            next_x,
            slope,
            intercept,
        )
        previous_x = next_x
        previous_y = next_y

        if input_index < x.size:
            next_x = float(x[input_index])
            next_y = float(y[input_index])
            input_index += 1
        elif input_index == x.size:
            next_x += last_input_step
            next_y = 0.0

        slope = (next_y - previous_y) / (next_x - previous_x)
        intercept = -slope * previous_x + previous_y

    return output


def line_integral(
    start: float,
    stop: float,
    slope: float,
    intercept: float,
) -> float:
    def primitive(value: float) -> float:
        return 0.5 * slope * value * value + intercept * value

    return primitive(stop) - primitive(start)


def min_max_scale(values: np.ndarray) -> np.ndarray:
    minimum = float(np.min(values))
    maximum = float(np.max(values))
    return (values - minimum) * (1.0 / (maximum - minimum))
