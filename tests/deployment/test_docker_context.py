"""Keep Docker selection explicit and restricted to local Unix sockets."""

import io
import json
import os
import pty
from pathlib import Path
import re
import select
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools import docker_context

ROOT = Path(__file__).resolve().parents[2]


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class DockerContextTests(unittest.TestCase):
    def test_resolves_named_local_endpoint_without_selecting_the_global_context(self):
        with patch.object(docker_context, "_docker", side_effect=("science\n", json.dumps([
            {"Endpoints": {"docker": {"Host": "unix:///run/user/1000/science/docker.sock"}}}
        ]))) as command:
            self.assertEqual(docker_context._endpoint("science"),
                             "unix:///run/user/1000/science/docker.sock")
        self.assertEqual(command.call_args_list, [
            unittest.mock.call("context", "ls", "--format", "{{.Name}}"),
            unittest.mock.call("context", "inspect", "science"),
        ])

    def test_rejects_remote_endpoints(self):
        with patch.object(docker_context, "_docker", side_effect=("remote\n", json.dumps([
            {"Endpoints": {"docker": {"Host": "ssh://host.example"}}}
        ]))):
            with self.assertRaisesRegex(ValueError, "local unix://"):
                docker_context._endpoint("remote")

    def test_interactive_menu_requires_a_choice_and_does_not_default(self):
        context_listing = "default\nscience\n"
        inspection = json.dumps([{"Endpoints": {"docker": {"Host": "unix:///tmp/default.sock"}}}])
        science = json.dumps([{"Endpoints": {"docker": {"Host": "unix:///tmp/science.sock"}}}])
        remote = json.dumps([{"Endpoints": {"docker": {"Host": "ssh://remote.example"}}}])
        context_listing = "default\nremote\nscience\n"
        responses = [context_listing, context_listing, inspection, context_listing, remote,
                     context_listing, science]
        with patch.object(docker_context, "_docker", side_effect=responses), \
             patch.object(docker_context.sys, "argv", ["docker_context.py", "--interactive"]), \
             patch.object(docker_context.sys, "stdin", TerminalInput("2\n")), \
             patch.object(docker_context.sys, "stdout", new_callable=io.StringIO) as output, \
             patch.object(docker_context.sys, "stderr", new_callable=io.StringIO) as error:
            self.assertEqual(docker_context.main(), 0)
        self.assertEqual(output.getvalue(), "unix:///tmp/science.sock\n")
        self.assertIn("(no default)", error.getvalue())
        self.assertNotIn("remote.example", error.getvalue())

    def test_command_line_context_is_read_from_the_exported_make_value(self):
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "science"}), \
             patch.object(docker_context, "_endpoint", return_value="unix:///tmp/science.sock"), \
             patch.object(docker_context.sys, "argv", ["docker_context.py", "--command-line-context"]), \
             patch.object(docker_context.sys, "stdout", new_callable=io.StringIO) as output:
            self.assertEqual(docker_context.main(), 0)
        self.assertEqual(output.getvalue(), "unix:///tmp/science.sock\n")

    def test_explicit_default_named_context_is_a_valid_choice(self):
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "default"}), \
             patch.object(docker_context, "_endpoint", return_value="unix:///tmp/default.sock") as resolve, \
             patch.object(docker_context.sys, "argv", ["docker_context.py", "--command-line-context"]), \
             patch.object(docker_context.sys, "stdout", new_callable=io.StringIO) as output:
            self.assertEqual(docker_context.main(), 0)
        resolve.assert_called_once_with("default")
        self.assertEqual(output.getvalue(), "unix:///tmp/default.sock\n")

    def test_noninteractive_make_requires_a_context_only_for_docker_goals(self):
        environment = os.environ.copy()
        environment.update({
            "DOCKER_HOST": "tcp://poison.invalid:2375",
            "DOCKER_CONTEXT": "ambient-context-must-not-be-used",
            "SECS_DOCKER_ENDPOINT": "unix:///tmp/ambient.sock",
        })
        missing = subprocess.run(
            ["make", "--no-print-directory", "-n", "packages/cpu/image"], cwd=ROOT,
            env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("Set DOCKER_CONTEXT=<local-context>", missing.stderr)
        self.assertEqual(missing.stdout, "")

        expected_endpoint = docker_context._endpoint("default")
        explicit = subprocess.run(
            ["make", "--no-print-directory", "-pn", "packages/cpu/image", "DOCKER_CONTEXT=default"], cwd=ROOT,
            env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertIn(f"SECS_DOCKER_ENDPOINT := {expected_endpoint}", explicit.stdout)
        self.assertNotIn("unix:///tmp/ambient.sock", explicit.stdout)

        non_docker = subprocess.run(
            ["make", "--no-print-directory", "help", "DOCKER_CONTEXT=not-a-context"], cwd=ROOT,
            env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(non_docker.returncode, 0, non_docker.stderr)
        self.assertNotIn("Docker context selection failed", non_docker.stderr)

    def test_unclassified_make_goal_and_shell_launcher_fail_closed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / "docker"
            marker = root / "docker-was-called"
            docker.write_text(f"#!/bin/sh\nprintf called > '{marker}'\n")
            docker.chmod(0o700)
            environment = os.environ | {
                "PATH": f"{root}:/usr/bin:/bin",
                "DOCKER_HOST": "tcp://remote.example:2376",
                "SECS_DOCKER_ENDPOINT": "unix:///tmp/ambient.sock",
            }
            makefile = "unclassified-docker: ; @$(DOCKER) version"
            make = subprocess.run(
                ["make", "--no-print-directory", f"--eval={makefile}", "unclassified-docker",
                 "DOCKER_CONTEXT=default"], cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(make.returncode, 0)
            self.assertIn("No Docker endpoint was inherited", make.stderr)
            self.assertFalse(marker.exists())

            environment.pop("SECS_DOCKER_ENDPOINT")
            shell = subprocess.run(
                ["bash", str(ROOT / "tools/docker.sh"), "version"], cwd=ROOT, env=environment,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(shell.returncode, 0)
            self.assertIn("No Docker endpoint was inherited", shell.stderr)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(Path("/usr/bin/docker").is_file(), "Docker CLI is unavailable")
    def test_make_interactive_menu_lists_only_local_contexts(self):
        pid, terminal = pty.fork()
        if pid == 0:
            environment = os.environ.copy()
            environment.pop("DOCKER_CONTEXT", None)
            environment.pop("DOCKER_HOST", None)
            os.execvpe("make", ["make", "--no-print-directory", "-n", "packages/base-images/pull",
                                "checkpoint/image"], environment)
        output = bytearray()
        reaped = False
        try:
            while b"Choice: " not in output:
                ready, _, _ = select.select([terminal], [], [], 10)
                self.assertTrue(ready, "Make did not reach the Docker context menu")
                output.extend(os.read(terminal, 4096))
            menu = output.decode(errors="replace")
            self.assertIn("Select a local Docker context (no default)", menu)
            entries = [line for line in menu.splitlines() if re.match(r"\s+\d+\) ", line)]
            self.assertTrue(entries, menu)
            self.assertTrue(all("unix://" in line for line in entries), menu)
            self.assertNotRegex(menu, r"(?:ssh|tcp)://")
            first_menu_length = len(output)
            os.write(terminal, b"1\n")
            while True:
                ready, _, _ = select.select([terminal], [], [], 10)
                if not ready:
                    break
                try:
                    chunk = os.read(terminal, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                self.assertNotIn(b"Choice: ", output[first_menu_length:], "Make prompted again for the second Docker goal")
            _, status = os.waitpid(pid, 0)
            reaped = True
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode(errors="replace"))
            self.assertEqual(output.decode(errors="replace").count("Select a local Docker context (no default)"), 1)
            self.assertIn(b"tools/docker.sh", output)
        finally:
            os.close(terminal)
            if not reaped:
                os.kill(pid, 9)
                os.waitpid(pid, 0)

    def test_make_passes_context_as_shell_data(self):
        with TemporaryDirectory() as temporary:
            marker = Path(temporary) / "executed"
            context = f"missing'; touch {marker}; echo '"
            result = subprocess.run(
                ["make", "--no-print-directory", "-n", "packages/cpu/image", f"DOCKER_CONTEXT={context}"],
                cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not exist", result.stderr)
            self.assertFalse(marker.exists())

    def test_runtime_docker_boundaries_have_no_implicit_context_or_socket_fallback(self):
        runtime_files = [ROOT / "Makefile", *sorted((ROOT / "make").glob("*.mk")),
                         *sorted((ROOT / "deployment").glob("*.py")),
                         *sorted((ROOT / "tools").glob("*.py")),
                         *sorted((ROOT / "tools").glob("*.sh"))]
        forbidden = (
            re.compile(r"--context\s+['\"]?default\b"),
            re.compile(r"DOCKER_CONTEXT.{0,40}['\"]default['\"]"),
            re.compile(r"\b(?:DOCKER_CONTEXT|context)\s*(?:=|\bor\b)\s*['\"]?default\b"),
            re.compile(r"(?:unix://)?/(?:var/run/docker\.sock|run/user/\d+/.+docker\.sock)"),
        )
        for path in runtime_files:
            source = path.read_text()
            for pattern in forbidden:
                with self.subTest(path=path, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(source), f"implicit Docker selection in {path}")


if __name__ == "__main__":
    unittest.main()
