"""Load the shared deployment grant and compose the SECS scientific binding."""

import asyncio
from dataclasses import replace
import logging
import os
from pathlib import Path
from secrets import token_hex
import signal

from provider_core.api import ProviderAPI
from provider_core.api_config import api_settings_with_material, https_settings
from provider_core.attempt_record import RecoveryScope
from provider_core.chat import ChatEndpoint, ChatEndpointSpec
from provider_core.controller import AttemptController, LiveStartPolicy
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.shared_grants import shared_grants
from provider_core.diagnostics import failure_evidence, write_stderr
from provider_core.failure_projection import owned_details
from provider_core.lifetime import ProviderLifetime
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.startup_file import read_startup_file

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.config import CONFIG_PATH, INTERPRETER_KEY_PATH, decode_provider_config
from secs_inference.provider.configuration_error import ConfigurationError
from secs_inference.provider.diagnostics import scientific_failure_evidence
from secs_inference.provider.process import publish_hello_until_stopped
from secs_inference.provider.runtime import _drain_controller, run_services
from secs_inference.provider.mock_runner_session import MockRunnerSession
from secs_inference.provider.session import SecsSession
from secs_inference.provider.sources import AttemptSources
from secs_inference.provider.worker import WorkerClient
from secs_inference.provider.worker_protocol import ScientificWorkerFailed


_LOG = logging.getLogger(__name__)
LIFETIME_DIRECTORY = Path("/state")
SOURCE_DIRECTORY = Path("/run/secs/sources/current")
WORKER_SOCKET = Path("/run/secs/worker/worker.sock")


def _diagnostic_details(error, text, diagnostic_window):
    if type(error) is ConfigurationError:
        return {"message": str(error)}
    if type(error) is ScientificWorkerFailed:
        return {"scientific_worker": scientific_failure_evidence(error.diagnostic)}
    return owned_details(error)


def _retry_notice(*_facts):
    _LOG.warning("Shared Attempt owner is retrying its retained operation; no scientific rerun was requested")


def _read_chat_settings(execution, retirement_seconds):
    key = read_startup_file(INTERPRETER_KEY_PATH, maximum_bytes=16384,
                            label="interpreter API key", secret=True)
    try:
        key = key.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError as cause:
        raise ConfigurationError("Interpreter API key must contain header-safe ASCII text") from cause
    spec = ChatEndpointSpec(configuration_id="secs-input", model=execution.interpreter_model,
                            url=execution.interpreter_url, api_key=key,
                            reasoning_effort=execution.interpreter_reasoning_effort)
    table = {"exchange_seconds": execution.interpretation_seconds, "retirement_seconds": retirement_seconds}
    if execution.interpreter_use_private_ca:
        table["ca_file"] = "/run/config/provider/interpreter-ca.crt"
    return spec, https_settings(table, "interpreter", read_startup_file)


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
    grants = shared_grants(config.shared_settings)
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
                chat_spec, chat_http = _read_chat_settings(execution, settings.http.retirement_seconds)
                store_table = {"exchange_seconds": execution.work_seconds,
                               "retirement_seconds": settings.http.retirement_seconds}
                if execution.upload_store_use_private_ca:
                    store_table["ca_file"] = "/run/config/provider/upload-store-ca.crt"
                store_http = https_settings(store_table, "Upload store", read_startup_file)
            worker = WorkerClient(WORKER_SOCKET, startup_seconds=execution.worker_startup_seconds,
                                   retirement_seconds=10, observation_seconds=2)
            claims = AttemptClaims.live(limits=LiveAttemptLimits(
                maximum_attempts=1, maximum_control_bytes=131072, maximum_pending_terminal_bytes=2097152,
            ))
            scope = RecoveryScope(settings.identity.origin, provider_ref, "secs", token_hex(16))
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
                    chat = ChatEndpoint(chat_spec, transport=chat_http.new_transport(),
                                        request_bytes=2097152, response_bytes=262144)
                    session = SecsSession(worker=worker, sources=sources, chat=chat, work_seconds=execution.work_seconds,
                                          interpretation_seconds=execution.interpretation_seconds,
                                          startup_seconds=execution.worker_startup_seconds, max_turns=execution.max_turns,
                                          secrets=(chat_spec.api_key,), mock_index=mock_index)
                delivery = SharedDeliveryClient(grants.endpoint(settings.identity.origin, provider_ref), publication,
                                                exchange_seconds=config.delivery_exchange_seconds,
                                                retirement_seconds=settings.http.retirement_seconds)
                return AttemptController.live(
                    new_api(), claims, session, scope=scope, stopping=science_stopping, admission_stopping=stop,
                    start_policy=LiveStartPolicy(cadence_seconds=execution.poll_seconds,
                                                on_retry=_retry_notice, on_recovered=_retry_notice),
                    delivery=delivery, on_delivery_retry=_retry_notice,
                    delivery_retry_initial_seconds=config.delivery_retry_initial_seconds,
                    delivery_retry_max_seconds=config.delivery_retry_max_seconds,
                    allowed_job_states=frozenset({"open", "closed"}), poll_seconds=execution.poll_seconds,
                    task_settle_seconds=10,
                )
            await run_services(hello_api=hello_api, discovery_api=discovery_api, provider_ref=provider_ref,
                               config=config, scope=scope, claims=claims, stop=stop, assemble=assemble, worker=worker)
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
