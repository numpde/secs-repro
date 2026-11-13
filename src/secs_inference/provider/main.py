"""Load the shared deployment grant and compose the SECS scientific binding."""

import asyncio
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
from secrets import token_hex
import signal

from provider_core.api import ProviderAPI
from provider_core.api_config import api_settings_with_material, https_settings
from provider_core.attempt_record import RecoveryScope
from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
from provider_core.interpreter import ProseService
from provider_core.controller import AttemptController, LiveStartPolicy
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.shared_grants import SharedGrants
from provider_core.diagnostics import failure_evidence, write_stderr
from provider_core.execution import ConditionRecovery, ObservationRecovery
from provider_core.failure_projection import owned_details
from provider_core.lifetime import ProviderLifetime
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.startup_file import read_startup_file
from provider_core.uploads import require_store_origin

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.config import CONFIG_PATH, decode_provider_config
from secs_inference.provider.interpreter_config import INTERPRETER_DIRECTORY, read_endpoints
from secs_inference.provider.configuration_error import ConfigurationError
from secs_inference.provider.diagnostics import scientific_failure_evidence
from secs_inference.provider.process import publish_hello_until_stopped
from secs_inference.provider.runtime import _drain_controller, run_services
from secs_inference.provider.mock_runner_session import MockRunnerSession
from secs_inference.provider.session import SecsSession
from secs_inference.provider.interpreter import PreparationRejected, _InvalidArguments
from secs_inference.provider.input_operations import (
    FORMULA_COMPOSITION_MISMATCH, QUOTED_FORMULA_UNPARSEABLE, SELECTED_FORMULA_UNPARSEABLE,
)
from secs_inference.provider.sources import AttemptSources
from secs_inference.provider.worker import WorkerClient
from secs_inference.provider.worker_protocol import ScientificWorkerFailed


_LOG = logging.getLogger(__name__)
LIFETIME_DIRECTORY = Path("/state")
SOURCE_DIRECTORY = Path("/run/secs/sources/current")
WORKER_SOCKET = Path("/run/secs/worker/worker.sock")


def _report_idle_exit_diagnostic(worker):
    if worker is None or worker.idle_exit_diagnostic_failure is None:
        return
    exit_failure, diagnostic = worker.idle_exit_diagnostic_failure
    try:
        write_stderr(json.dumps({"operation": "report idle scientific worker exit",
                                 "returncode": exit_failure.runner.original_exit.returncode,
                                 "diagnostic_error_type": type(diagnostic).__name__}).encode())
    except Exception:
        # A broken fallback sink cannot change successful retirement into failure.
        pass


def _diagnostic_details(error, text, diagnostic_window):
    if type(error) is ConfigurationError:
        return {"message": str(error)}
    if type(error) is ScientificWorkerFailed:
        return {"scientific_worker": scientific_failure_evidence(error.diagnostic)}
    if type(error) is _InvalidArguments:
        # Its producers use only fixed text and names or limits from the closed tool schema.
        return {"message": text(str(error), 1024)}
    if type(error) is PreparationRejected and str(error) in (
        FORMULA_COMPOSITION_MISMATCH, QUOTED_FORMULA_UNPARSEABLE, SELECTED_FORMULA_UNPARSEABLE,
    ):
        return {"message": str(error)}
    return owned_details(error)


def _retry_notice(*_facts):
    _LOG.warning("Shared Attempt owner is retrying its retained operation; no scientific rerun was requested")


def _recovered_notice(*_facts):
    _LOG.info("Shared Attempt API operation recovered; no scientific rerun was requested")


def _api_recovery_policies(*, exchange_seconds, retirement_seconds, poll_seconds):
    backoff_max_seconds = 30
    initial_delay = min(poll_seconds, backoff_max_seconds)
    next_delay = min(initial_delay * 2, backoff_max_seconds)
    observation_gap = 2 * exchange_seconds + retirement_seconds + 2 * initial_delay
    # The SDK opens this window after the first failed PUT. It bounds retry dispatch;
    # the final exchange and its retirement can finish after the window.
    condition_window = (initial_delay + exchange_seconds + retirement_seconds + next_delay
                        + exchange_seconds + initial_delay)
    return (
        ObservationRecovery(
            gap_seconds=observation_gap,
            backoff_max_seconds=backoff_max_seconds,
            on_retry=_retry_notice,
            on_recovered=_recovered_notice,
        ),
        ConditionRecovery(
            window_seconds=condition_window,
            backoff_max_seconds=backoff_max_seconds,
            on_retry=_retry_notice,
            on_recovered=_recovered_notice,
        ),
    )


def _read_chat_settings(interpretation_seconds, retirement_seconds):
    endpoints = read_endpoints(INTERPRETER_DIRECTORY, read_startup_file)
    factories, secrets = [], []
    for endpoint in endpoints:
        key = read_startup_file(endpoint.key_file, maximum_bytes=16384,
                                label="interpreter API key", secret=True)
        try:
            key = key.decode("ascii").rstrip("\r\n")
        except UnicodeDecodeError as cause:
            raise ConfigurationError("Interpreter API key must contain header-safe ASCII text") from cause
        spec = ChatEndpointSpec(configuration_id=endpoint.name, model=endpoint.model,
                                url=endpoint.url, api_key=key,
                                reasoning_effort=endpoint.reasoning_effort)
        table = {"exchange_seconds": interpretation_seconds, "retirement_seconds": retirement_seconds}
        if endpoint.ca_file is not None:
            table["ca_file"] = str(endpoint.ca_file)
        http = https_settings(table, "interpreter", read_startup_file)
        factories.append(ChatEndpointFactory(spec=spec, trust=http.trust,
            retirement_seconds=http.retirement_seconds, proxy_socket=http.proxy_socket,
            request_bytes=2097152, response_bytes=262144))
        secrets.append(key)
    return tuple(ProseService(factory) for factory in factories), tuple(secrets)


def _upload_store_settings(execution, api_settings):
    store_origin = require_store_origin(execution.upload_store_origin)
    table = {"exchange_seconds": execution.work_seconds,
             "retirement_seconds": api_settings.http.retirement_seconds}
    if execution.upload_store_use_private_ca:
        table["ca_file"] = "/run/config/provider/upload-store-ca.crt"
    store_http = https_settings(table, "Upload store", read_startup_file)
    if store_origin == api_settings.identity.origin:
        store_http = replace(store_http, proxy_socket=api_settings.http.proxy_socket)
    return store_http


async def run_provider(config_path=CONFIG_PATH):
    config = decode_provider_config(read_startup_file(config_path, maximum_bytes=65536, label="SECS provider configuration"))
    mock_marker = os.environ.get("SECS_MOCK_INDEX", "0")
    if mock_marker not in ("0", "1"):
        raise ConfigurationError("Cannot start SECS provider: mock index selection must be 0 or 1")
    mock_index = mock_marker == "1"
    runner_marker = os.environ.get("SECS_MOCK_RUNNER", "0")
    if runner_marker not in ("0", "1") or (runner_marker == "1" and mock_index):
        raise ConfigurationError("Cannot start SECS provider: mock runner and mock index selections conflict")
    mock_runner = runner_marker == "1"
    if mock_runner:
        config = replace(config, hello=replace(config.hello, mock_runner=True,
            display_name="SECS (mock runner)", provider_description="MOCK RUNNER: no model or scientific analysis is run."))
    elif mock_index:
        config = replace(config, hello=replace(config.hello, mock_index=True,
            display_name="SECS (mock index)", provider_description="MOCK INDEX: eight starting molecules; not a full scientific search."))
    grants = SharedGrants(config.shared_project, config.shared_gid)
    settings, provider_ref, publication = api_settings_with_material(config.api, read_startup_file)
    settings = replace(settings, http=replace(settings.http, proxy_socket=str(grants.proxy_socket)))
    if (LIFETIME_DIRECTORY / "journal").exists():
        raise ConfigurationError("Legacy Attempt journal remains: drain its obligations under the old runtime and retain its evidence before selecting a fresh SDK lifetime directory")
    stop, science_stopping = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    lifetime = ProviderLifetime(LIFETIME_DIRECTORY).open()
    worker = claims = None
    apis, handlers, failures = [], [], []
    cleanup_failed = False
    def new_api():
        return ProviderAPI(settings.identity, settings.http.new_transport(), expected_topology=settings.expected_topology)
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(number, stop.set)
            handlers.append(number)
        hello_api = new_api()
        apis.append(hello_api)
        discovery_api = new_api()
        apis.append(discovery_api)
        if config.execution is None:
            await publish_hello_until_stopped(api=hello_api, provider_ref=provider_ref, policy=config.hello, stop=stop)
        else:
            execution = config.execution
            if not mock_runner:
                endpoint_factories, interpreter_secrets = _read_chat_settings(config.interpretation_seconds, settings.http.retirement_seconds)
                store_http = _upload_store_settings(execution, settings)
            # The mock runner never asks a prose service to interpret Job text.
            prose_services = () if mock_runner else endpoint_factories
            worker = WorkerClient(WORKER_SOCKET, startup_seconds=execution.worker_startup_seconds,
                                   retirement_seconds=10, observation_seconds=2)
            claims = AttemptClaims.live(limits=LiveAttemptLimits(
                maximum_attempts=1, maximum_control_bytes=131072, maximum_pending_terminal_bytes=2097152,
            ))
            scope = RecoveryScope(settings.identity.origin, provider_ref, "secs", token_hex(16))
            observation_recovery, condition_recovery = _api_recovery_policies(
                exchange_seconds=settings.http.exchange_seconds,
                retirement_seconds=settings.http.retirement_seconds,
                poll_seconds=execution.poll_seconds,
            )
            def assemble(pending):
                if mock_runner:
                    session = MockRunnerSession(worker=worker, work_seconds=execution.work_seconds,
                                                startup_seconds=execution.worker_startup_seconds)
                else:
                    sources = AttemptSources(
                        job_ref=pending.input.job_ref, api=new_api(), store=store_http.new_transport(),
                        store_origin=execution.upload_store_origin, directory=SOURCE_DIRECTORY,
                        max_upload_bytes=execution.max_upload_bytes, max_total_bytes=execution.max_total_bytes,
                        retirement_seconds=store_http.retirement_seconds,
                    )
                    session = SecsSession(worker=worker, sources=sources, endpoint_factories=endpoint_factories, work_seconds=execution.work_seconds,
                                          interpretation_seconds=config.interpretation_seconds,
                                          startup_seconds=execution.worker_startup_seconds,
                                          context_bytes=min(factory.context_bytes for factory in endpoint_factories),
                                          secrets=interpreter_secrets, mock_index=mock_index)
                delivery = SharedDeliveryClient(grants.endpoint(settings.identity.origin, provider_ref), publication,
                                                exchange_seconds=config.delivery_exchange_seconds,
                                                retirement_seconds=settings.http.retirement_seconds)
                return AttemptController.live(
                    new_api(), claims, session, scope=scope, stopping=science_stopping, admission_stopping=stop,
                    start_policy=LiveStartPolicy(cadence_seconds=execution.poll_seconds,
                                                on_retry=_retry_notice, on_recovered=_recovered_notice),
                    observation_recovery=observation_recovery, condition_recovery=condition_recovery,
                    delivery=delivery, on_delivery_retry=_retry_notice,
                    delivery_retry_initial_seconds=config.delivery_retry_initial_seconds,
                    delivery_retry_max_seconds=config.delivery_retry_max_seconds,
                    allowed_job_states=frozenset({"open", "closed"}), poll_seconds=execution.poll_seconds,
                    task_settle_seconds=10,
                )
            await run_services(hello_api=hello_api, discovery_api=discovery_api, provider_ref=provider_ref,
                               config=config, scope=scope, claims=claims, stop=stop, assemble=assemble, worker=worker,
                                prose_services=prose_services)
    except BaseException as cause:
        failures.append(cause)
    finally:
        stop.set()
        if claims is not None:
            for controller in claims.live_owners():
                try:
                    await _drain_controller(controller, poll_seconds=config.execution.poll_seconds)
                except BaseException as cause:
                    cleanup_failed = True
                    failures.append(cause)
        for operation in (() if worker is None else (worker.stop,)) + tuple(api.retire for api in apis):
            try:
                await operation()
            except BaseException as cause:
                cleanup_failed = True
                failures.append(cause)
        try:
            _report_idle_exit_diagnostic(worker)
        except BaseException as cause:
            failures.append(cause)
        for number in handlers:
            try:
                loop.remove_signal_handler(number)
            except BaseException as cause:
                failures.append(cause)
        if claims is not None and any(not owner.shutdown_ready for owner in claims.live_owners()):
            cleanup_failed = True
            failures.append(RuntimeError("Attempt work or reporting remains unsettled; provider lifetime ownership is retained"))
        if not cleanup_failed:
            try:
                lifetime.close()
            except BaseException as cause:
                failures.append(cause)
    if failures:
        raise BaseExceptionGroup("SECS provider stopped with retained failures or ownership", failures)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run_provider())
    except BaseException as cause:
        import json
        try:
            write_stderr(json.dumps({"operation": "run SECS provider", "failure": failure_evidence(
                cause, secrets=(), describe_owner=_diagnostic_details,
            )}).encode())
        except BaseException as reporting:
            retained = BaseExceptionGroup("SECS operation and diagnostic output failed", (cause, reporting))
            raise SystemExit(1) from retained
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
