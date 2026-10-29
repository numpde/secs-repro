"""Inject a process exit into the actual retained SCI Task in a separate process."""

import asyncio
import json
import os
from pathlib import Path
import select
import sys
from time import monotonic

from secs_inference.provider.worker import WorkerClient


async def main():
    worker = WorkerClient(Path(sys.argv[1]), startup_seconds=10, retirement_seconds=10, observation_seconds=2)
    await worker.check_ready(deadline=monotonic() + 10)
    pid = (await worker.request({"operation": "pid"}, deadline=monotonic() + 3))["pid"]
    descriptor = os.pidfd_open(pid)
    original = SystemExit(0)
    mode = sys.argv[2]
    async def fail(_command, _deadline):
        raise original
    try:
        if mode == "science":
            worker._exchange = fail
            try:
                await worker.request({"operation": "injected"}, deadline=monotonic() + 3)
            except BaseExceptionGroup as failure:
                assert failure.exceptions == (original,)
            else:
                raise AssertionError("The original SCI failure was lost")
        elif mode == "control":
            def fail_control(*, deadline):
                raise original
            worker.native.session.observe_retirement = fail_control
        else:
            raise AssertionError("Unknown native fatal fixture mode")
        try:
            await worker.stop()
        except BaseExceptionGroup as failure:
            assert failure.exceptions == (original,)
        else:
            raise AssertionError("Retirement lost the original fatal observation")
        assert worker.stopped
        assert select.select([descriptor], [], [], 0)[0]
        if mode == "science":
            assert worker.native.session.retirement.scratch_released
        else:
            assert worker.native.reader.done() and not worker.native.reader.cancelled()
            assert worker.native.retirement_failure is original
            assert worker.proof.session.retirement.scratch_released
            assert worker.proof.retirement_deadline <= worker.native.retirement_deadline
        assert worker.native.session.original_exit is None
        print(json.dumps({"retired": True, "fatal_observed": True}), flush=True)
    finally:
        os.close(descriptor)


asyncio.run(main())
