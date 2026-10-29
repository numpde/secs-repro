"""Load the shared deployment grant and compose the SECS scientific binding."""

import asyncio
from dataclasses import replace
import logging
import os
from pathlib import Path
from secrets import token_hex
import signal

from provider_core.api import ProviderAPI
from provider_core.api_config import api_settings, https_settings
from provider_core.attempt_record import RecoveryScope
from provider_core.chat import ChatEndpoint, ChatEndpointSpec
from provider_core.controller import AttemptController, LiveStartPolicy
from provider_core.delivery_client import DeliveryClient
from provider_core.deployment_binding import load_deployment_bindings
from provider_core.diagnostics import failure_evidence, write_stderr
from provider_core.failure_projection import owned_details
from provider_core.lifetime import ProviderLifetime
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.startup_file import read_startup_file

from secs_inference.provider.analysis import ANALYSIS_KIND_REF
from secs_inference.provider.config import CONFIG_PATH, INTERPRETER_KEY_PATH, decode_provider_config
from secs_inference.provider.configuration_error import ConfigurationError
from secs_inference.provider.process import publish_hello_until_stopped
from secs_inference.provider.runtime import _drain_controller, run_services
from secs_inference.provider.session import SecsSession
from secs_inference.provider.sources import AttemptSources
from secs_inference.provider.worker import WorkerClient


_LOG = logging.getLogger(__name__)
LIFETIME_DIRECTORY = Path("/state")
SOURCE_DIRECTORY = Path("/run/secs/sources/current")
WORKER_SOCKET = Path("/run/secs/worker/worker.sock")


def _diagnostic_details(error, text, diagnostic_window):
    if type(error) is ConfigurationError:
        return {"message": str(error)}
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
    definition = load_deployment_bindings(config.binding_definition)
    binding = definition.selected(config.binding_name)
    settings, provider_ref = api_settings(binding.api, read_startup_file)
    proxy_socket = os.environ.get("SECS_API_PROXY_SOCKET")
    if proxy_socket is None:
        raise ConfigurationError("Cannot start SECS provider: the launcher did not grant its shared API proxy socket")
    settings = replace(settings, http=replace(settings.http, proxy_socket=proxy_socket))
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
                                      secrets=(chat_spec.api_key,))
                delivery = DeliveryClient(binding.endpoint, exchange_seconds=definition.request_seconds,
                                          retirement_seconds=settings.http.retirement_seconds)
                return AttemptController.live(
                    new_api(), claims, session, scope=scope, stopping=science_stopping, admission_stopping=stop,
                    start_policy=LiveStartPolicy(cadence_seconds=execution.poll_seconds,
                                                on_retry=_retry_notice, on_recovered=_retry_notice),
                    delivery=delivery, on_delivery_retry=_retry_notice,
                    delivery_retry_initial_seconds=definition.retry_initial_seconds,
                    delivery_retry_max_seconds=definition.retry_max_seconds,
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
        write_stderr(json.dumps({"operation": "run SECS provider", "failure": failure_evidence(
            cause, secrets=(), describe_owner=_diagnostic_details,
        )}).encode())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
