"""Check the SECS deployment adapter against the installed shared probe."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from linux_idmap import IdMap, IdRange
from linux_idmap.probe import ProbeFailure, ProbeResult, Problem, ResourceState, Retirement
from deployment import rootless_access


IMAGE = "sha256:" + "a" * 64
UID_MAP = IdMap((IdRange(0, 1000, 1), IdRange(1, 100000, 65536)))
GID_MAP = IdMap((IdRange(0, 2000, 1), IdRange(1, 200000, 65536)))


class SharedIdMapTests(unittest.TestCase):
    def test_uses_pinned_engine_namespace_and_existing_resource_ceilings(self):
        project = SimpleNamespace(docker_endpoint="unix:///test.sock")
        with patch("linux_idmap.probe.probe_identity",
                   return_value=ProbeResult(UID_MAP, GID_MAP, "c" * 64)) as operation:
            self.assertEqual((100999, 200999), rootless_access.mapped_ids(
                project, IMAGE, 1000, 1000, "host"))
        request = operation.call_args.args[0]
        self.assertEqual(("unix:///test.sock", IMAGE, "/usr/local/bin/python3"),
                         (request.endpoint, request.image, request.interpreter))
        self.assertEqual((0, 0, "host"), (request.user_uid, request.user_gid, request.userns_mode))
        self.assertEqual((67108864, 16, 1000000000),
                         (request.resources.memory_bytes, request.resources.pids_limit,
                          request.resources.nano_cpus))
        self.assertEqual("/usr/bin/docker", operation.call_args.kwargs["executable"])

    def test_inverse_maps_both_ids_from_same_retired_probe(self):
        project = SimpleNamespace(docker_endpoint="unix:///test.sock")
        with patch("linux_idmap.probe.probe_identity",
                   return_value=ProbeResult(UID_MAP, GID_MAP, "c" * 64)) as operation:
            self.assertEqual((0, 0), rootless_access.container_ids_for_host(
                project, IMAGE, 1000, 2000))
        operation.assert_called_once()

    def test_unresolved_retirement_never_grants_access(self):
        project = SimpleNamespace(docker_endpoint="unix:///test.sock")
        failure = ProbeFailure(Problem("engine", "create ambiguous"),
                               Retirement(ResourceState.UNRESOLVED))
        with patch("linux_idmap.probe.probe_identity", side_effect=failure), \
             self.assertRaisesRegex(ValueError, "uncertain-outcome"):
            rootless_access.mapped_ids(project, IMAGE, 1000, 1000)


if __name__ == "__main__":
    unittest.main()
