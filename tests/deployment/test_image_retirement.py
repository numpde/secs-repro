import datetime
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import retire_images as retirement

NOW = datetime.datetime(2026, 11, 3, tzinfo=datetime.timezone.utc)


def image(number, input_number, *, tags=None, digests=None, age=48, role="cpu"):
    key = retirement.LABELS[role]
    label = {key: f"{input_number:064x}"}
    if role != "provider":
        label["io.secs-repro.packages.variant"] = role
    name = "provider" if role == "provider" else "packages-" + role
    return {
        "Id": "sha256:" + f"{number:064x}", "Config": {"Labels": label},
        "RepoTags": [f"secs-repro/{name}:inputs-{input_number:064x}"] if tags is None else tags,
        "RepoDigests": digests or [], "Created": (NOW - datetime.timedelta(hours=age)).isoformat(),
    }


class ImageRetirementTests(unittest.TestCase):
    def test_timed_out_removal_reports_uncertain_outcome(self):
        with mock.patch.object(retirement.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 30)):
            with self.assertRaisesRegex(retirement.Refused, "outcome is unconfirmed"):
                retirement.docker("image", "rm", "--no-prune", "sha256:" + "a" * 64)

    def test_image_work_holds_the_checkout_lock(self):
        helper = pathlib.Path(retirement.ROOT) / "tools/image_operation_lock.sh"
        with tempfile.TemporaryDirectory() as checkout:
            command = 'source "$1"; hold_image_operation_lock "$2"; flock -xn "$2" true'
            result = subprocess.run(["bash", "-c", command, "lock-test", str(helper), checkout], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_preserves_current_pins_containers_recent_and_foreign_aliases(self):
        current = image(1, 1, tags=["secs-repro/packages-cpu:local"])
        old = image(2, 2)
        pinned = image(3, 3)
        recent = image(4, 4, age=23)
        foreign = image(5, 5, tags=["other/image:keep"])
        digest = image(6, 6, digests=["other/image@sha256:" + "f" * 64])
        current_input = image(7, 1)
        images = [current, old, pinned, recent, foreign, digest, current_input]
        self.assertEqual(retirement.select(images, {pinned["Id"]}, NOW), [old["Id"]])

    def test_missing_current_moving_tag_preserves_role(self):
        self.assertEqual(retirement.select([image(2, 2)], set(), NOW), [])

    def test_invalid_package_variant_is_not_owned_as_provider(self):
        current = image(1, 1, tags=["secs-repro/provider:local"], role="provider")
        invalid = image(2, 2, tags=[], role="cpu")
        invalid["Config"]["Labels"]["io.secs-repro.packages.variant"] = "provider"
        self.assertEqual(retirement.select([current, invalid], set(), NOW), [])

    def test_retained_build_receipt_protects_the_recorded_image(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = pathlib.Path(directory) / "secs-package-inputs.test"
            stage.mkdir()
            identifier = "sha256:" + "a" * 64
            (stage / "image-id").write_text(identifier + "\n")
            with mock.patch.object(retirement, "STAGE_ROOT", pathlib.Path(directory)):
                self.assertEqual(retirement.staged_images(), {identifier})

    def test_missing_alias_metadata_refuses_before_selection(self):
        current = image(1, 1, tags=["secs-repro/packages-cpu:local"])
        old = image(2, 2)
        del old["RepoDigests"]
        with self.assertRaises(retirement.Refused):
            retirement.select([current, old], set(), NOW)

    def test_invalid_container_inventory_prevents_removal(self):
        with mock.patch.object(retirement, "_private_directory"), mock.patch.object(retirement, "_locked_parent") as lock, mock.patch.object(retirement, "pins", return_value=set()), mock.patch.object(retirement, "staged_images", return_value=set()), mock.patch.object(retirement, "docker", return_value="bad-container\n") as docker:
            lock.return_value.__enter__.return_value = None
            with self.assertRaises(retirement.Refused):
                retirement.retire()
            docker.assert_called_once_with("ps", "--all", "--quiet", "--no-trunc")

    def test_rechecks_aliases_before_removing_an_image(self):
        current = image(1, 1, tags=["secs-repro/packages-cpu:local"])
        old = image(2, 2)
        changed = dict(old, RepoTags=["someone/retained:tag"])
        removals = []

        def fake_docker(*args):
            if args[:2] == ("image", "ls"):
                return current["Id"] + "\n" + old["Id"] + "\n"
            if args[:2] == ("image", "inspect"):
                return json.dumps([changed] if len(args) == 3 else [current, old])
            if args[:2] == ("image", "rm"):
                removals.append(args)
            return ""

        with mock.patch.object(retirement, "_private_directory"), mock.patch.object(retirement, "_locked_parent"), mock.patch.object(retirement, "pins", return_value=set()), mock.patch.object(retirement, "staged_images", return_value=set()), mock.patch.object(retirement, "docker", side_effect=fake_docker):
            retirement.retire()
        self.assertEqual(removals, [])


if __name__ == "__main__":
    unittest.main()
