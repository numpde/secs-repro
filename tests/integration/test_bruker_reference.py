import json
import unittest
from pathlib import Path

import nmrglue as ng
import numpy as np


FIXTURES = Path("/fixtures")
BRUKER_PROCESSED = FIXTURES / "bruker/F3697/1/pdata/1"
FRONTEND_REFERENCE = FIXTURES / "frontend/F3697-1.json"
SLIGHT_BLUR = np.asarray([1, 4, 6, 4, 1], dtype=np.float64) / 16
MINIMUM_SHAPE_SIMILARITY = 0.999
MAXIMUM_TRANSPORT_DISTANCE_IN_GRID_STEPS = 2.0


class BrukerFrontendReferenceTest(unittest.TestCase):
    def test_nmrglue_preserves_frontend_spectral_shape(self):
        reference = json.loads(FRONTEND_REFERENCE.read_text())
        parameters, intensities = ng.bruker.read_pdata(
            BRUKER_PROCESSED,
            scale_data=True,
        )
        ppm = processed_bruker_ppm_axis(parameters, intensities.size)

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

        blurred_actual = np.convolve(actual, SLIGHT_BLUR, mode="same")
        blurred_expected = np.convolve(expected, SLIGHT_BLUR, mode="same")
        grid_step = abs(
            (float(grid["to_ppm"]) - float(grid["from_ppm"]))
            / (int(grid["points"]) - 1),
        )
        similarity = float(
            np.dot(blurred_actual, blurred_expected)
            / (
                np.linalg.norm(blurred_actual)
                * np.linalg.norm(blurred_expected)
            ),
        )
        transport_distance = l1_transport_distance(
            blurred_actual,
            blurred_expected,
            grid_step=grid_step,
        )
        transport_grid_steps = transport_distance / grid_step
        self.assertGreaterEqual(
            similarity,
            MINIMUM_SHAPE_SIMILARITY,
            f"blurred spectrum cosine similarity {similarity:.9f} is below "
            f"{MINIMUM_SHAPE_SIMILARITY:.9f}; L1 transport distance is "
            f"{transport_distance:.9g} ppm ({transport_grid_steps:.3f} grid steps)",
        )
        self.assertLessEqual(
            transport_grid_steps,
            MAXIMUM_TRANSPORT_DISTANCE_IN_GRID_STEPS,
            f"blurred spectrum L1 transport distance {transport_distance:.9g} ppm "
            f"({transport_grid_steps:.3f} grid steps) exceeds "
            f"{MAXIMUM_TRANSPORT_DISTANCE_IN_GRID_STEPS:.3f} grid steps; cosine "
            f"similarity is {similarity:.9f}",
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


def processed_bruker_ppm_axis(
    parameters: dict,
    points: int,
) -> np.ndarray:
    procs = parameters["procs"]
    offset = float(procs["OFFSET"])
    width = float(procs["SW_p"]) / float(procs["SF"])
    return np.linspace(offset, offset - width, points, dtype=np.float64)


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


def l1_transport_distance(
    actual: np.ndarray,
    expected: np.ndarray,
    *,
    grid_step: float,
) -> float:
    actual_mass = actual / np.sum(actual)
    expected_mass = expected / np.sum(expected)
    return float(
        grid_step * np.sum(np.abs(np.cumsum(actual_mass - expected_mass))),
    )
