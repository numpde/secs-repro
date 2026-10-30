import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
GPU_UUID = "GPU-01234567-89ab-cdef-0123-456789abcdef"
TARGETS = (
    "candidates",
    "candidates/qualification",
    "test/integration/challenges",
    "test/integration/challenges/bruker",
)


class GpuSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        commands = self.root / "bin"
        commands.mkdir()
        self.log = self.root / "gpu-commands"
        probe = (
            '#!/bin/bash\nset -eu\n'
            'if [[ "$1" == image ]]; then printf "sha256:fixture\\n"; exit 0; fi\n'
            'printf "%s\\n" "$@" >> "$GPU_TEST_LOG"\nexit 79\n'
        )
        for name in ("docker", "nvidia-smi"):
            path = commands / name
            path.write_text(probe)
            path.chmod(0o755)
        (self.work / "checkpoint").mkdir()
        (self.work / "checkpoint/candidates.toml").touch()
        (self.work / "cache").mkdir()
        (self.work / "tools").mkdir()
        (self.work / "tools/retain_uncertain_stage.sh").write_bytes(
            (REPOSITORY / "tools/retain_uncertain_stage.sh").read_bytes())
        (self.work / "tests/challenges").mkdir(parents=True)
        (self.work / "tests/challenges/test_published_challenges.py").touch()
        (self.work / "tests/fixtures/challenges").mkdir(parents=True)
        self.archive = self.root / "archive.tar.gz"
        self.archive.touch()
        (self.work / "Makefile").write_text(
            'SHELL := /bin/bash\n.SHELLFLAGS := -eu -o pipefail -c\n.ONESHELL:\n'
            'HOST_UID := 1000\nCHECKPOINT_DIRECTORY := checkpoint\n'
            f'REPOSITORY_ROOT := {REPOSITORY}\n'
            'MOLFORMER_CACHE := cache\nDOCKER := docker\n'
            'packages_image_tag = fixture\n'
            f'include {REPOSITORY}/make/candidates.mk\n'
            f'include {REPOSITORY}/make/candidate-qualification.mk\n'
            f'include {REPOSITORY}/make/tests.mk\n'
            'checkpoint/image packages/gpu/image packages/cpu/image:\n\t@:\n'
        )
        self.environment = {
            "PATH": f"{commands}:/usr/bin:/bin",
            "GPU_TEST_LOG": str(self.log),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        for arguments in (
            ("init", "--quiet"),
            ("add", "."),
            ("-c", "user.name=Test", "-c", "user.email=test@example.test",
             "commit", "--quiet", "-m", "fixture"),
        ):
            subprocess.run(
                ("git", *arguments), cwd=self.work, env=self.environment,
                check=True, capture_output=True,
            )

    def run_target(self, target, selector=None):
        arguments = ["make", "--no-print-directory", target, f"ARCHIVE={self.archive}"]
        if target == "candidates":
            arguments.append("TARGET=gpu")
        if selector is not None:
            arguments.append(f"CANDIDATE_GPU={selector}")
        return subprocess.run(
            arguments, cwd=self.work, env=self.environment,
            capture_output=True, text=True, timeout=10,
        )

    @unittest.skipIf(os.getuid() == 0, "Candidate recipes reject host UID 0.")
    def test_invalid_selection_never_reaches_gpu_commands(self):
        canary = self.root / "expanded-selector"
        for target in TARGETS:
            for selector in (
                None, "", "0", "all", "GPU-------------------------------------",
                f"{GPU_UUID},{GPU_UUID}", f"{GPU_UUID}\n",
                f"$(shell touch {canary})",
            ):
                with self.subTest(target=target, selector=selector):
                    canary.unlink(missing_ok=True)
                    result = self.run_target(target, selector)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("CANDIDATE_GPU=GPU-<uuid>", result.stderr)
                    self.assertIn("one NVIDIA GPU", result.stderr)
                    self.assertFalse(self.log.exists(), result.stdout)
                    self.assertFalse(canary.exists())

    @unittest.skipIf(os.getuid() == 0, "Candidate recipes reject host UID 0.")
    def test_exact_uuid_reaches_the_selected_gpu_command(self):
        for target in TARGETS:
            for selector in (GPU_UUID, GPU_UUID.upper()):
                with self.subTest(target=target, selector=selector):
                    self.log.unlink(missing_ok=True)
                    result = self.run_target(target, selector)
                    self.assertNotEqual(result.returncode, 0)
                    arguments = self.log.read_text().splitlines()
                    if target == "candidates/qualification":
                        self.assertEqual(arguments[:2], ["-i", selector])
                    else:
                        position = arguments.index("--gpus")
                        self.assertEqual(arguments[position + 1], f"device={selector}")
                    self.assertIn("79", result.stderr)

    @unittest.skipIf(os.getuid() == 0, "Candidate recipes reject host UID 0.")
    def test_cpu_candidates_do_not_grant_a_gpu(self):
        result = subprocess.run(
            ["make", "--no-print-directory", "candidates", "TARGET=cpu", f"ARCHIVE={self.archive}"],
            cwd=self.work, env=self.environment, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("--gpus", self.log.read_text())

    def test_empty_dtype_is_rejected_before_build(self):
        result = subprocess.run(
            ["make", "--no-print-directory", "candidates", "TARGET=cpu", "CANDIDATE_DTYPE="],
            cwd=self.work, env=self.environment, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("select float32 or bfloat16", result.stderr)
        self.assertFalse(self.log.exists())
