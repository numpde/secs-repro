"""The provider uses the released, independently installed SDK and its closure."""

from importlib import import_module
from importlib.metadata import distribution
from pathlib import Path
import subprocess
import sys
import unittest


class SdkInstallationTests(unittest.TestCase):
    def test_released_sdk_imports_from_its_installed_distribution(self):
        installed = distribution("provider-core")
        self.assertEqual(installed.version, "0.5.3")
        self.assertGreaterEqual(sys.version_info[:2], (3, 12))
        package = import_module("provider_core")
        self.assertEqual(Path(package.__file__).resolve(),
                         Path(installed.locate_file("provider_core/__init__.py")).resolve())
        for module in ("controller", "analysis", "shared_delivery_client", "shared_grants", "runner_session"):
            import_module("provider_core." + module)
        checked = subprocess.run((sys.executable, "-m", "pip", "check"),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout)


if __name__ == "__main__":
    unittest.main()
