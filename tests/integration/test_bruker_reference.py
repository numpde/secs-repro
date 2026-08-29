import json
import unittest
from pathlib import Path

import numpy as np

from secs_inference.bruker import prepare_bruker_upload


FIXTURES = Path("/fixtures")
BRUKER_UPLOAD = FIXTURES / "bruker/F3697/1"
FRONTEND_REFERENCE = FIXTURES / "frontend/F3697-1.json"
SLIGHT_BLUR = np.asarray([1, 4, 6, 4, 1], dtype=np.float64) / 16


class BrukerFrontendReferenceTest(unittest.TestCase):
    def test_bruker_upload_matches_frontend_float32_input(self):
        reference = json.loads(FRONTEND_REFERENCE.read_text())
        actual = prepare_bruker_upload(BRUKER_UPLOAD)
        expected = np.asarray(reference["intensities"], dtype=np.float32)

        try:
            np.testing.assert_allclose(
                actual,
                expected,
                rtol=0,
                atol=np.finfo(np.float32).eps,
            )
        except AssertionError as mismatch:
            blurred_actual = np.convolve(actual, SLIGHT_BLUR, mode="same")
            blurred_expected = np.convolve(expected, SLIGHT_BLUR, mode="same")
            similarity = float(
                np.dot(blurred_actual, blurred_expected)
                / (
                    np.linalg.norm(blurred_actual)
                    * np.linalg.norm(blurred_expected)
                ),
            )
            grid = reference["grid"]
            grid_step = abs(
                (float(grid["to_ppm"]) - float(grid["from_ppm"]))
                / (int(grid["points"]) - 1),
            )
            transport_distance = l1_transport_distance(
                blurred_actual,
                blurred_expected,
                grid_step=grid_step,
            )
            raise self.failureException(
                f"{mismatch}\nBlurred cosine similarity: {similarity:.9f}; "
                f"L1 transport distance: {transport_distance:.9g} ppm "
                f"({transport_distance / grid_step:.3f} grid steps)",
            ) from None


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
