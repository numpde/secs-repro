import json
import unittest
from pathlib import Path

import numpy as np

from secs_inference import prepare_bruker_upload


FIXTURES = Path("/fixtures")
BRUKER_UPLOAD = FIXTURES / "bruker/F3697/1"
FRONTEND_REFERENCE = FIXTURES / "frontend/F3697-1.json"
SLIGHT_BLUR = np.asarray([1, 4, 6, 4, 1], dtype=np.float64) / 16
MINIMUM_SHAPE_SIMILARITY = 0.999
MAXIMUM_TRANSPORT_DISTANCE_IN_GRID_STEPS = 2.0


class BrukerFrontendReferenceTest(unittest.TestCase):
    def test_bruker_upload_matches_frontend_spectral_shape(self):
        reference = json.loads(FRONTEND_REFERENCE.read_text())
        actual = prepare_bruker_upload(BRUKER_UPLOAD)
        grid = reference["grid"]
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
