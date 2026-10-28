"""Preserve artifact hashes while making known wheel identities usable offline."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from normalize_lock import main


class OfflineLockTests(unittest.TestCase):
    def test_known_wheels_keep_hashes_and_unrelated_requirements(self):
        sdk_hash = "1a741c14664d2f849d3dad48f287e2179d6c5e2c076a9e9db4057787c1d77561"
        torch_hash = "ee40b8a4b4b2cf0670c6fd4f35a7ef23871af956fecb238fbf5da15a72650b1d"
        other = "other @ https://example.test/other-1.0-py3-none-any.whl#sha256=" + "c" * 64
        source = (
            "# Generated wheel requirements\n"
            "provider-core @ https://github.com/numpde/nmr-provider-tools/releases/download/v0.4.0/"
            f"provider_core-0.4.0-py3-none-any.whl#sha256={sdk_hash} \\\n"
            f"    --hash=sha256:{sdk_hash}\n"
            "torch @ https://download.pytorch.org/whl/cpu/"
            f"torch-2.10.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl#sha256={torch_hash} \\\n"
            f"    --hash=sha256:{torch_hash}\n" + other + "\n"
            "cryptography==50.0.0\n"
        )
        with TemporaryDirectory() as directory:
            original, output = Path(directory) / "raw", Path(directory) / "lock"
            original.write_text(source)
            main(original, output)
            normalized = output.read_text()
        self.assertIn("provider-core==0.4.0 \\\n", normalized)
        self.assertIn("torch==2.10.0+cpu \\\n", normalized)
        self.assertEqual([line for line in normalized.splitlines() if "--hash=" in line],
                         [line for line in source.splitlines() if "--hash=" in line])
        self.assertIn(other + "\ncryptography==50.0.0\n", normalized)

