"""Compose one durable Attempt owner with hello and an offline scientific worker."""

import logging
from pathlib import Path
import shutil
from threading import Thread
from time import monotonic

from secs_inference.provider.analysis_run import run_analysis
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.execution import ExecutionLoop, ProviderStopping
from secs_inference.provider.job_api import ApiUnavailable, JobApi
from secs_inference.provider.process import publish_hello_until_stopped
from secs_inference.provider.worker import WorkerClient, WorkerError, WorkerStopUnconfirmed


WORKER_SOCKET = Path("/run/secs/worker/worker.sock")
SOURCE_DIRECTORY = Path("/run/secs/sources/current")
JOURNAL_DIRECTORY = Path("/state/journal")
_LOG = logging.getLogger(__name__)


def run_execution(*, api, config, chat, upload_store, stop, journal):
    """Keep terminal replay independent of model startup and the work deadline.

    Only this thread touches the worker and journal. A new ready connection
    proves the supervisor reaped its previous child before leftover source
    files are removed. Interrupted inference is reported, never resumed.
    """
    jobs = JobApi(api)
    worker = None

    def check_running():
        if stop.is_set():
            raise ProviderStopping("The provider is shutting down")

    def before_start():
        nonlocal worker
        check_running()
        if worker is None or worker.stopped:
            deadline = monotonic() + config.worker_startup_seconds
            while True:
                check_running()
                try:
                    worker = WorkerClient(WORKER_SOCKET, startup_deadline=deadline, check_running=check_running)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if monotonic() >= deadline:
                        raise WorkerError("Cannot start analysis: the offline worker socket is unavailable; start the worker before restarting the provider") from None
                    stop.wait(min(1, max(0, deadline - monotonic())))
        # A completed operation also establishes idleness. Retry any cleanup
        # left by that operation before another remote Attempt can be started.
        if SOURCE_DIRECTORY.exists():
            shutil.rmtree(SOURCE_DIRECTORY)

    def analyse(active):
        check_running()
        return run_analysis(
            api=jobs, active=active, chat=chat, worker=worker, store=upload_store,
            directory=SOURCE_DIRECTORY, work_deadline=monotonic() + config.work_seconds,
            interpretation_seconds=config.interpretation_seconds, max_turns=config.max_turns,
            max_total_bytes=config.max_total_bytes, check_running=check_running,
        )

    loop = ExecutionLoop(jobs, journal, analyse, journal.diagnose, before_start)
    retry_seconds = config.poll_seconds
    stop_unconfirmed = False
    try:
        while not stop.is_set():
            try:
                worked = loop.step()
            except ApiUnavailable as error:
                _LOG.warning("Retrying the unavailable Provider API operation; any pending Attempt state is retained: %s", error)
                stop.wait(retry_seconds)
                retry_seconds = min(300, retry_seconds * 2)
            else:
                retry_seconds = config.poll_seconds
                if not worked:
                    stop.wait(config.poll_seconds)
    except ProviderStopping:
        pass
    except WorkerStopUnconfirmed:
        stop_unconfirmed = True
        raise
    finally:
        if worker is not None and not worker.stopped and not stop_unconfirmed:
            worker.stop()


def run_services(*, api, prepared, config, chat, upload_store, stop):
    """Join hello before releasing Attempt ownership; relay its fatal errors."""
    hello_errors = []

    def hello():
        try:
            publish_hello_until_stopped(api=api, prepared=prepared, policy=config.hello, stop=stop)
        except BaseException as error:
            hello_errors.append(error)
            stop.set()

    with AttemptStore(JOURNAL_DIRECTORY) as journal:
        thread = Thread(target=hello, name="provider-hello")
        thread.start()
        try:
            run_execution(api=api, config=config.execution, chat=chat,
                          upload_store=upload_store, stop=stop, journal=journal)
        finally:
            stop.set()
            thread.join()
        if hello_errors:
            raise hello_errors[0]
