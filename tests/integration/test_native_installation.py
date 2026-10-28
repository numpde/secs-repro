"""The contained native owner has isolated dependencies and an explicit child."""

from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class NativeInstallationTests(unittest.TestCase):
    def test_native_sdk_is_isolated_from_the_unchanged_scientific_environment(self):
        self.assertEqual(sys.prefix, "/usr/local")
        for package, expected in (("numpy", "2.3.5"), ("certifi", "2026.7.22"), ("idna", "3.19")):
            self.assertEqual(version(package), expected)
        self.assertEqual(version("torch").split("+")[0], "2.10.0")
        with self.assertRaises(PackageNotFoundError):
            version("provider-core")
        science = subprocess.run((sys.executable, "-I", "-m", "pip", "check"),
                                 capture_output=True, text=True)
        self.assertEqual(science.returncode, 0, science.stdout + science.stderr)

        # The isolated launcher must ignore even an inherited global package path.
        checked = subprocess.run(
            ("/opt/native/bin/python", "-I", str(Path(__file__).with_name("native_installation_probe.py"))),
            env=os.environ | {"PYTHONPATH": "/usr/local/lib/python3.12/site-packages"},
            capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        facts = json.loads(checked.stdout)
        self.assertEqual(facts["native_prefix"], "/opt/native")
        self.assertEqual(facts["sdk_version"], "0.4.0")
        self.assertEqual(facts["science_prefix"], "/usr/local")
        self.assertEqual(facts["science_executable"], "/usr/local/bin/python")
        self.assertTrue(facts["same_child_reused"])
        self.assertTrue(facts["scratch_released"])

