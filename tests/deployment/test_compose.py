"""Preserve NMRPeak's project ownership and ordered-stop behavior across providers."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from deployment.compose import ComposeProject


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

    def test_malformed_inspection_cannot_select_a_stop_target(self):
        for records in (["wrong"], [record(self.project, "provider", 2)]):
            with self.subTest(records=records):
                with patch.object(ComposeProject, "command", side_effect=(b"1" * 64 + b"\n", json.dumps(records).encode())):
                    with self.assertRaises(ValueError):
                        self.project.stop()


if __name__ == "__main__":
    unittest.main()
