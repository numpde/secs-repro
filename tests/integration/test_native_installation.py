"""The same SDK native wheel works without changing scientific dependencies."""

from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class NativeInstallationTests(unittest.TestCase):
    def test_native_profile_preserves_scientific_pins_and_needs_no_api_dependencies(self):
        self.assertEqual(sys.prefix, "/usr/local")
        for package, expected in (("numpy", "2.3.5"), ("certifi", "2026.7.22"), ("idna", "3.19")):
            self.assertEqual(version(package), expected)
        self.assertEqual(version("torch").split("+")[0], "2.10.0")
        self.assertEqual(version("provider-core"), "0.5.0")
        self.assertFalse(Path("/opt/native").exists())
        science = subprocess.run((sys.executable, "-I", "-m", "pip", "check"),
                                 capture_output=True, text=True)
        self.assertEqual(science.returncode, 0, science.stdout + science.stderr)

        # Isolated Python must ignore an ambient import path while using installed packages.
        checked = subprocess.run(
            (sys.executable, "-I", str(Path(__file__).with_name("native_installation_probe.py"))),
            env=os.environ | {"PYTHONPATH": "/not-a-granted-import-path"},
            capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        facts = json.loads(checked.stdout)
        self.assertEqual(facts["native_prefix"], "/usr/local")
        self.assertEqual(facts["sdk_version"], "0.5.0")
        self.assertEqual(facts["science_prefix"], "/usr/local")
        self.assertEqual(facts["science_executable"], "/usr/local/bin/python")
        self.assertTrue(facts["same_child_reused"])
        self.assertTrue(facts["scratch_released"])

