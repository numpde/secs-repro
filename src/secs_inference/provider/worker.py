"""Borrow one resident scientific channel from the shared native lifecycle owner."""

import asyncio
import os
from pathlib import Path
import stat
from time import monotonic

from provider_core.async_runner_session import AsyncRunnerSession
from provider_core.task_boundary import contain_task_exit
from provider_core.runner_session import RunnerSession, RunnerStartupStopped, RunnerStartupUnavailable

from secs_inference.provider.worker_protocol import InvalidFailureEnvelope, ScientificWorkerFailed, WorkerError, encode, receive_async, scientific_worker_failure


class WorkerStopUnconfirmed(WorkerError):
    """Retain sources and admission until original native retirement is proved."""


class NativeWorkerExited(WorkerError):
    """Carry an actual original exit to the owning Attempt's scientific session."""

    def __init__(self, runner, cause):
        super().__init__("The scientific worker exited before returning its operation outcome")
        self.runner, self.cause = runner, cause


async def _join(task, deadline):
    if task.done():
        return task.result()
    async with asyncio.timeout_at(deadline):
        return await asyncio.shield(task)


def _observed(task):
    if not task.cancelled():
        task.exception()


class WorkerClient:
    """Keep scientific framing and warm reuse separate from native process facts.

    Only this channel's original request reads scientific frames. Shared SDK
    owners retain setup and control reads across cancellation. No API operation
    runs inside scientific I/O, so the operation's own deadline remains effective.
    """

    def __init__(self, path: Path, *, startup_seconds, retirement_seconds, observation_seconds=2):
        if not path.is_absolute() or path.parent.resolve() != path.parent:
            raise ValueError("SECS native endpoint requires a canonical absolute parent")
        if not 0 < observation_seconds < retirement_seconds:
            raise ValueError("Native exit observation must leave part of the retirement allowance for stopping")
        self.path, self.startup_seconds = path, startup_seconds
        self.retirement_seconds, self.observation_seconds = retirement_seconds, observation_seconds
        self._begin_lifetime()

    def _begin_lifetime(self):
        self.native = self.proof = None
        self._opening = self._pending = self._retirement = None
        self._observed_opening = None
        self._reader = self._writer = None
        self._stopping = self.stopped = False
        self._science_close_failure = None

    def _own(self, operation, *arguments):
        coroutine = contain_task_exit(operation, *arguments)
        try:
            task = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
        task.add_done_callback(_observed)
        return task

    async def _lease_identity(self, deadline):
        while True:
            if self._stopping:
                raise RunnerStartupStopped("SECS native startup was stopped before lease admission")
            try:
                descriptor = os.open(self.path.parent / ".owner-session.lock",
                                     os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            except FileNotFoundError:
                if monotonic() >= deadline:
                    raise RunnerStartupUnavailable("SECS native lifetime lock did not appear within its startup allowance") from None
                await asyncio.sleep(min(0.02, deadline - monotonic()))
                continue
            try:
                facts = os.fstat(descriptor)
                if (not stat.S_ISREG(facts.st_mode) or facts.st_uid != os.geteuid()
                        or stat.S_IMODE(facts.st_mode) != 0o600):
                    raise PermissionError("SECS native lifetime lock must be a caller-owned mode 0600 regular file")
                return facts.st_dev, facts.st_ino
            finally:
                os.close(descriptor)

    async def _open(self, deadline, require_execution):
        identity = await self._lease_identity(deadline)
        if require_execution is not None:
            require_execution()
        self.native = AsyncRunnerSession(RunnerSession(
            self.path, role="secs", expected_lock_identity=identity,
            startup_seconds=self.startup_seconds, retirement_seconds=self.retirement_seconds,
        ), setup_not_after=deadline)
        if self._stopping:
            self.native.request_stop()
        await self.native.connect(wait_seconds=max(0.001, deadline - monotonic()))
        if self._stopping:
            raise WorkerError("SECS native startup was interrupted before scientific channel adoption")
        channel = self.native.session.science
        channel.setblocking(False)
        self._reader, self._writer = await asyncio.open_connection(sock=channel)
        self.native.session.science = None
        if self._stopping:
            self._request_stop()
            raise WorkerError("SECS native startup was interrupted during scientific channel adoption")
        ready = await self._receive_reply()
        if ready.get("outcome") == "failed":
            raise scientific_worker_failure(ready)
        if ready.get("outcome") != "ready":
            raise WorkerError("The scientific child did not confirm that its configured model and candidate index loaded")

    async def check_ready(self, *, deadline, require_execution=None):
        if self.stopped:
            # A fresh SDK owner is allowed only after this client's retirement
            # positively settled; a poisoned or pending read never reaches here.
            self._begin_lifetime()
        starting = self._opening is None
        if starting:
            self._opening = self._own(self._open, deadline, require_execution)
        try:
            await _join(self._opening, deadline)
        except WorkerError:
            self._observed_opening = self._opening
            raise
        if starting:
            return
        response = await self.request({"operation": "ready"}, deadline=deadline)
        if response.get("outcome") != "ready":
            self._request_stop()
            raise WorkerError("The resident scientific worker did not confirm readiness")

    async def _observe_exit(self, cause):
        try:
            await self.native.observe_retirement(wait_seconds=self.observation_seconds)
        except Exception:
            # Caller expiry retains the same SDK reader; it is never exit proof.
            if self.native.session.original_exit is None:
                raise WorkerError("Scientific communication ended without confirmed original worker exit") from cause
        if self.native.session.original_exit is not None:
            raise NativeWorkerExited(self.native.session, cause) from cause
        raise WorkerError("Scientific communication ended without an operation outcome") from cause

    async def _receive_reply(self):
        try:
            return await receive_async(self._reader)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as cause:
            await self._observe_exit(cause)

    async def _exchange(self, command, deadline, require_execution):
        if self._stopping or self.stopped:
            raise WorkerError("The scientific request was stopped before dispatch")
        frame = encode(command)
        if monotonic() >= deadline:
            raise TimeoutError("Scientific request deadline elapsed before dispatch")
        if require_execution is not None:
            require_execution()
        try:
            self._writer.write(frame)
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError) as cause:
            await self._observe_exit(cause)
        return await self._receive_reply()

    async def request(self, command, *, deadline, require_execution=None):
        if command.get("operation") in ("inspect", "prepare", "execute") and not callable(require_execution):
            raise ValueError("Scientific work requires its active Attempt permission")
        if self._stopping or self.stopped or self._reader is None:
            raise WorkerError("The scientific worker is not available for another request")
        if self._pending is not None:
            raise WorkerError("The scientific worker still owns its original request; join or retire it before more work")
        self._pending = self._own(self._exchange, command, deadline, require_execution)
        try:
            response = await _join(self._pending, deadline)
        except NativeWorkerExited:
            raise
        except BaseException:
            self._request_stop()
            raise
        else:
            self._pending = None
            return response

    def _request_stop(self):
        self._stopping = True
        if self.native is not None:
            try:
                self.native.request_stop()
            except OSError:
                # Shared ownership retains this failure; still join its original
                # reader because a queued receipt can establish actual retirement.
                pass
        if self._writer is not None:
            try:
                self._writer.close()
            except OSError as cause:
                if self._science_close_failure is None:
                    self._science_close_failure = cause

    async def _prove_retirement(self, deadline):
        if monotonic() >= deadline:
            raise TimeoutError("SECS native cleanup allowance expired before proof-session acquisition")
        generation = self.native.session.generation
        if generation is None:
            raise WorkerStopUnconfirmed("Original runner generation was not admitted; its scratch cleanup remains unconfirmed")
        self.native.session.close()
        remaining = deadline - monotonic()
        self.proof = AsyncRunnerSession(RunnerSession(
            self.path, role="secs", expected_lock_identity=self.native.session.expected_lock_identity,
            startup_seconds=remaining, retirement_seconds=remaining,
        ), not_after=deadline)
        try:
            await self.proof.connect(wait_seconds=max(0.001, deadline - monotonic()))
            self.proof.request_stop()
            await self.proof.join_retirement(wait_seconds=max(0.001, deadline - monotonic()))
        except BaseException:
            try:
                self.proof.request_stop()
            except OSError:
                # The proof owner retains its original stop failure and futures.
                pass
            raise
        self.proof.session.close()
        if self.proof.session.generation != generation:
            raise WorkerStopUnconfirmed("A replacement runner cannot confirm cleanup of the original runner's scratch")

    async def _join_science(self, task, deadline, retained_failures):
        try:
            await _join(task, deadline)
        except Exception:
            if not task.done():
                raise
        except BaseExceptionGroup as cause:
            retained_failures.append(cause)
            if not task.done():
                raise

    async def _retire_native(self, deadline, retained_failures):
        try:
            await contain_task_exit(self.native.join_retirement, wait_seconds=max(0.001, deadline - monotonic()))
        except (RunnerStartupStopped, RunnerStartupUnavailable):
            # These exact SDK setup outcomes exclude any control connection.
            if self.native.session.control_connected:
                raise
        except (Exception, BaseExceptionGroup) as cause:
            if not isinstance(cause, Exception):
                retained_failures.append(cause)
            original = self.native.reader if self.native.reader is not None else self.native.setup
            failure = (self.native.retirement_failure if self.native.reader is not None
                       else self.native.setup_failure)
            if original is None or not original.done() or original.cancelled() or failure is None:
                raise
            await self._prove_retirement(deadline)

    def _unconsumed_scientific_failures(self):
        failures = []
        if (self._opening is not None and self._opening is not self._observed_opening
                and self._opening.done() and not self._opening.cancelled()):
            failure = self._opening.exception()
            if type(failure) in (ScientificWorkerFailed, InvalidFailureEnvelope):
                failures.append(failure)
        if (self._pending is not None and self._pending.done() and not self._pending.cancelled()
                and self._pending.exception() is None):
            response = self._pending.result()
            if response.get("outcome") == "failed":
                try:
                    failures.append(scientific_worker_failure(response))
                except WorkerError as failure:
                    failures.append(failure)
        return failures

    async def _retire(self):
        deadline = monotonic() + self.retirement_seconds if self.native is None else self.native.retirement_deadline
        retained_failures = []
        cleanup_failure = None
        try:
            if self._opening is not None:
                await self._join_science(self._opening, deadline, retained_failures)
            if self.native is not None:
                await self._retire_native(deadline, retained_failures)
            if self._pending is not None:
                await self._join_science(self._pending, deadline, retained_failures)
            if self._writer is not None:
                try:
                    await _join(self._own(self._writer.wait_closed), deadline)
                except (BrokenPipeError, ConnectionResetError):
                    # The close waiter reports the earlier stream error only
                    # after connection_lost; native retirement was proved above.
                    pass
            if self.native is not None:
                self.native.session.close()
        except BaseException as cleanup:
            cleanup_failure = cleanup
        retained_failures.extend(self._unconsumed_scientific_failures())
        if cleanup_failure is not None:
            if retained_failures and all(cleanup_failure is not cause for cause in retained_failures):
                raise BaseExceptionGroup("Native retirement retained observations and cleanup failure",
                                         (*retained_failures, cleanup_failure)) from None
            raise cleanup_failure
        self.stopped = True
        if len(retained_failures) == 1:
            raise retained_failures[0]
        if retained_failures:
            raise BaseExceptionGroup("Native retirement retained observations", retained_failures)

    async def stop(self):
        if not self.stopped:
            self._request_stop()
        if self._retirement is None:
            self._retirement = self._own(self._retire)
        try:
            await asyncio.shield(self._retirement)
        except Exception as cause:
            if self.stopped:
                raise
            raise WorkerStopUnconfirmed("Scientific worker retirement is unconfirmed; retain its sources and stop admission") from cause
