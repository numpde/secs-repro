"""Preserve NMRPeak's project ownership and ordered-stop behavior across providers."""

import json
from contextlib import nullcontext, redirect_stderr
from io import StringIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deployment.compose import ComposeProject
from deployment.provider_deployment import main


def record(project, role, number, *, running=True, grace=47):
    """Build the relevant engine response, not an alternate ownership checker."""
    return {
        "Id": str(number) * 64, "Image": "sha256:" + "a" * 64,
        "State": {"Running": running, "Status": "running" if running else "exited"},
        "Config": {"StopTimeout": grace, "Labels": {
            "com.docker.compose.project": project.name,
            "com.docker.compose.project.working_dir": str(project.repository),
            "com.docker.compose.service": role,
            "com.docker.compose.oneoff": "False",
        }},
    }


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.project = ComposeProject(Path("/workspace"), "example-prod", ("provider", "worker"))

    def test_foreign_checkout_is_rejected_before_stop(self):
        document = record(self.project, "provider", 1)
        document["Config"]["Labels"]["com.docker.compose.project.working_dir"] = "/another-checkout"
        with patch.object(ComposeProject, "command", side_effect=(b"1" * 64 + b"\n", json.dumps([document]).encode())) as command:
            with self.assertRaisesRegex(ValueError, "foreign"):
                self.project.stop()
        self.assertEqual([call.args[0] for call in command.call_args_list], ["ps", "inspect"])

    def test_stop_uses_inspected_ids_and_preserves_worker_on_provider_failure(self):
        records = {role: record(self.project, role, n) for n, role in enumerate(("provider", "worker"), 1)}
        with patch.object(ComposeProject, "inventory", return_value=records):
            with patch.object(ComposeProject, "command", side_effect=RuntimeError("stop failed")) as command:
                with self.assertRaisesRegex(RuntimeError, "stop failed"):
                    self.project.stop()
        command.assert_called_once_with("stop", "1" * 64, timeout=77)

    def test_stop_is_ordered_and_does_not_delete_containers_or_state(self):
        records = {role: record(self.project, role, n) for n, role in enumerate(("provider", "worker"), 1)}
        stopped = {role: record(self.project, role, n, running=False) for n, role in enumerate(("provider", "worker"), 1)}
        with patch.object(ComposeProject, "inventory", side_effect=(records, stopped)):
            with patch.object(ComposeProject, "command") as command:
                self.project.stop()
        self.assertEqual([call.args for call in command.call_args_list], [
            ("stop", "1" * 64), ("stop", "2" * 64),
        ])

    def test_unbounded_grace_is_rejected_before_stopping_any_service(self):
        for grace in (None, -1, "30", True):
            records = [record(self.project, "provider", 1),
                       record(self.project, "worker", 2, grace=grace)]
            with self.subTest(grace=grace):
                with patch.object(ComposeProject, "command", side_effect=(
                    b"1" * 64 + b"\n" + b"2" * 64 + b"\n", json.dumps(records).encode(),
                )) as command:
                    with self.assertRaisesRegex(ValueError, "shutdown grace"):
                        self.project.stop()
                self.assertEqual([call.args[0] for call in command.call_args_list], ["ps", "inspect"])

    def test_logs_remain_accessible_without_a_finite_shutdown_grace(self):
        document = record(self.project, "provider", 1, grace=-1)
        with patch.object(ComposeProject, "command", side_effect=(
            b"1" * 64 + b"\n", json.dumps([document]).encode(), b"",
        )) as command:
            self.project.logs()
        self.assertEqual(command.call_args.args, ("logs", "--tail", "80", "1" * 64))

    def test_running_deployment_is_not_replaced(self):
        with patch.object(ComposeProject, "inventory", return_value={"provider": record(self.project, "provider", 1)}):
            with patch.object(ComposeProject, "command") as command:
                with self.assertRaisesRegex(ValueError, "already running"):
                    self.project.start({"services": {}})
        command.assert_not_called()

    def test_start_uses_rendered_snapshot_without_build_or_pull(self):
        plan = {"services": {"provider": {"image": "sha256:" + "a" * 64,
                                         "environment": {"LITERAL": "$not_a_variable"}}}}
        captured = []

        def run(*args, **kwargs):
            """Observe the actual file handed to Compose, before its temporary removal."""
            if args[0] == "compose":
                captured.append(json.loads(Path(args[args.index("--file") + 1]).read_text()))
                self.assertIn("--no-build", args)
                self.assertEqual(args[args.index("--pull") + 1], "never")
            return b""

        with patch.object(ComposeProject, "inventory", side_effect=({}, {"provider": record(self.project, "provider", 1)})):
            with patch.object(ComposeProject, "command", side_effect=run):
                self.project.start(plan)
        self.assertEqual(captured[0]["services"]["provider"]["environment"]["LITERAL"], "$$not_a_variable")

    def test_engine_failure_is_not_an_empty_inventory(self):
        with patch.object(ComposeProject, "command", side_effect=RuntimeError("engine unavailable")):
            with self.assertRaisesRegex(RuntimeError, "engine unavailable"):
                self.project.inventory()

    def test_post_effect_inspection_failure_reports_uncertainty(self):
        plan = {"services": {"provider": {"image": "sha256:" + "a" * 64}}}
        initial = {"provider": record(self.project, "provider", 1)}
        for operation, before, invoke in (
            ("Startup", {}, lambda: self.project.start(plan)),
            ("Shutdown", initial, self.project.stop),
        ):
            with self.subTest(operation=operation):
                with patch.object(ComposeProject, "inventory", side_effect=(before, ValueError("foreign container"))):
                    with patch.object(ComposeProject, "command", return_value=b"") as command:
                        with self.assertRaises(ValueError) as failure:
                            invoke()
                self.assertTrue(command.called)
                self.assertIn(f"{operation} is unconfirmed", failure.exception.__notes__[0])

    def test_cli_keeps_docker_diagnostics_private_for_exits_and_timeouts(self):
        secret = b"Docker could not use interpolated-config-secret"
        for result in (subprocess.CompletedProcess([], 1, b"", secret),
                       subprocess.TimeoutExpired("docker", 60, stderr=secret)):
            with self.subTest(result=type(result).__name__), TemporaryDirectory() as temporary:
                output = StringIO()
                behavior = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch("tempfile.tempdir", temporary), patch("deployment.compose.subprocess.run", **behavior):
                    with patch("deployment.provider_deployment._private_directory"), patch(
                        "deployment.provider_deployment._locked_parent", return_value=nullcontext(),
                    ), redirect_stderr(output):
                        self.assertEqual(main(["status", "production"]), 1)
                files = list(Path(temporary).iterdir())
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].read_bytes(), secret)
                self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
                self.assertIn(str(files[0]), output.getvalue())
                self.assertNotIn(secret.decode(), output.getvalue())
                self.assertIn("Deployment status failed", output.getvalue())

    def test_diagnostic_retention_failure_preserves_docker_failure(self):
        result = subprocess.CompletedProcess([], 7, b"", b"private diagnostic")
        with patch("deployment.compose.subprocess.run", return_value=result), patch(
            "deployment.compose.NamedTemporaryFile", side_effect=OSError("storage unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Docker ps failed .*exit 7") as failure:
                self.project.command("ps")
        self.assertIn("Could not retain", failure.exception.__notes__[0])

    def test_malformed_inspection_cannot_select_a_stop_target(self):
        for records in (["wrong"], [record(self.project, "provider", 2)]):
            with self.subTest(records=records):
                with patch.object(ComposeProject, "command", side_effect=(b"1" * 64 + b"\n", json.dumps(records).encode())):
                    with self.assertRaises(ValueError):
                        self.project.stop()


if __name__ == "__main__":
    unittest.main()
