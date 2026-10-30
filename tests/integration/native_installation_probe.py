"""Exercise installed interpreter and FD grants without starting model science."""

import argparse
from importlib import import_module
from importlib.metadata import distribution, version
import json
from importlib.util import find_spec
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile


def science_child():
    parser = argparse.ArgumentParser()
    parser.add_argument("--science-child", action="store_true")
    parser.add_argument("--session-fd", type=int, required=True)
    parser.add_argument("--lease-fd", type=int, required=True)
    parser.add_argument("--operation", required=True)
    arguments = parser.parse_args()
    assert sys.prefix == "/usr/local"
    assert sys.executable == "/usr/local/bin/python"
    model = import_module("secs_inference.model")
    assert Path(model.__file__).resolve() == Path(
        distribution("secs-inference").locate_file("secs_inference/model.py")
    ).resolve()
    assert version("provider-core") == "0.5.4"
    import_module("provider_core.upload_file")
    import_module("secs_inference.provider.worker")
    assert all(find_spec(name) is None for name in ("httpx", "httpcore", "cryptography", "http_sf", "tomli_w"))
    lease = os.fstat(arguments.lease_fd)
    facts = {"pid": os.getpid(), "prefix": sys.prefix, "executable": sys.executable,
             "operation": arguments.operation, "lease": [lease.st_dev, lease.st_ino],
             "scratch": os.getcwd()}
    with socket.socket(fileno=arguments.session_fd) as science:
        with science.makefile("rwb", buffering=0) as frames:
            frames.write(json.dumps(facts).encode() + b"\n")
            for line in frames:
                assert line == b"ping\n"
                frames.write(json.dumps(facts).encode() + b"\n")


def native_owner():
    from provider_core.runner import LOCK_NAME
    from provider_core.runner_session import RunnerSession

    installed = distribution("provider-core")
    package = import_module("provider_core")
    assert installed.version == "0.5.4"
    assert sys.prefix == "/usr/local"
    assert Path(package.__file__).resolve() == Path(installed.locate_file("provider_core/__init__.py")).resolve()
    assert not Path("/opt/native").exists()
    assert all(find_spec(name) is None for name in ("httpx", "httpcore", "cryptography", "http_sf", "tomli_w"))
    checked = subprocess.run((sys.executable, "-I", "-m", "pip", "check"), capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    root = Path(tempfile.mkdtemp(prefix="secs-native-installation-"))
    endpoint = root / "control" / "worker.sock"
    endpoint.parent.mkdir(mode=0o700)
    lock = endpoint.parent / LOCK_NAME
    lock.touch(mode=0o600)
    observed = lock.stat()
    identity = (observed.st_dev, observed.st_ino)
    scratch, status = root / "scratch", root / "status"
    scratch.mkdir(mode=0o700)
    status.mkdir(mode=0o700)
    native = subprocess.Popen((sys.executable, "-I", "-m", "provider_core.runner",
        "--role", "secs-installation", "--socket", str(endpoint), "--scratch", str(scratch),
        "--status-directory", str(status), "--owner-session-lifetime", "--startup-seconds", "10",
        "--terminate-seconds", "2", "--kill-seconds", "2", "--cleanup-seconds", "2", "--",
        "/usr/local/bin/python", "-I", __file__, "--science-child"))
    session = RunnerSession(endpoint, role="secs-installation", expected_lock_identity=identity,
                            startup_seconds=10, retirement_seconds=10)
    try:
        session.connect()
        session.science.settimeout(15)
        with session.science.makefile("rwb", buffering=0) as frames:
            facts = json.loads(frames.readline())
            assert facts["operation"] == session.operation
            assert facts["lease"] == list(identity)
            for _ in range(2):
                frames.write(b"ping\n")
                assert json.loads(frames.readline()) == facts
            session.retire()
            assert session.retirement.scratch_released
            assert session.original_exit is None
            assert not Path(facts["scratch"]).exists()
        print(json.dumps({"native_prefix": sys.prefix, "sdk_version": installed.version,
            "science_prefix": facts["prefix"], "science_executable": facts["executable"],
            "same_child_reused": True, "scratch_released": session.retirement.scratch_released}))
    finally:
        session.close()
        native.terminate()
        native.wait(timeout=10)
        if session.retirement is not None and session.retirement.scratch_released:
            shutil.rmtree(root)


if __name__ == "__main__":
    if "--science-child" in sys.argv:
        science_child()
    else:
        native_owner()
