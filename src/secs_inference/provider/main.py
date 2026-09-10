"""Load fixed provider inputs, then compose hello and optional Job execution."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import stat
from threading import Event
import traceback

from secs_inference.provider.analysis import (
    ANALYSIS_KIND_REF,
    analysis_offering_description,
)
from secs_inference.provider.api import ProviderApi
from secs_inference.provider.config import (
    CONFIG_PATH,
    CREDENTIAL_PATH,
    INTERPRETER_KEY_PATH,
    ExecutionConfig,
    ProviderConfig,
    decode_provider_config,
)
from secs_inference.provider.credential import (
    PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES,
    parse_provider_credential,
)
from secs_inference.provider.hello import (
    AnalysisOffering,
    PreparedHello,
    prepare_hello,
)
from secs_inference.provider.process import publish_hello_until_stopped
from secs_inference.provider.chat import ChatEndpoint
from secs_inference.provider.upload_download import UploadStore
from secs_inference.provider.runtime import run_services


_CONFIG_MAX_BYTES = 65_536


def prepare_configured_hello(config: ProviderConfig) -> PreparedHello:
    """Combine deployment presentation with the code-owned analysis offering."""

    return prepare_hello(
        display_name=config.hello.display_name,
        description=config.hello.provider_description,
        analysis_offerings=(
            AnalysisOffering(
                analysis_kind_ref=ANALYSIS_KIND_REF,
                description=analysis_offering_description(),
            ),
        ),
    )


def run_provider(config_path: Path = CONFIG_PATH) -> None:
    """Load deployment inputs once; changes take effect after a restart.

    Configuration, credentials, TLS trust, and presentation remain fixed for
    the process lifetime. A deployment change takes effect after a restart.
    """

    config = decode_provider_config(_read_regular_file(config_path, _CONFIG_MAX_BYTES))
    prepared = prepare_configured_hello(config)
    endpoint = config.endpoint.materialize()
    credential = parse_provider_credential(
        _read_regular_file(
            CREDENTIAL_PATH,
            PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES,
        )
    )
    api = ProviderApi(
        endpoint=endpoint,
        provider_ref=credential.provider_ref,
        credential_ref=credential.credential_ref,
        private_key=credential.private_key,
    )
    chat = upload_store = None
    if config.execution is not None:
        execution = config.execution
        chat = load_chat_endpoint(execution)
        upload_store = UploadStore(execution.upload_store_origin, execution.max_upload_bytes,
                                   ca_file=Path("/run/config/provider/upload-store-ca.crt") if execution.upload_store_use_private_ca else None)
    stop = Event()
    previous_handlers = {
        signal_number: signal.signal(signal_number, lambda *_args: stop.set())
        for signal_number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        if config.execution is None:
            publish_hello_until_stopped(api=api, prepared=prepared, policy=config.hello, stop=stop)
        else:
            run_services(api=api, prepared=prepared, config=config,
                         chat=chat, upload_store=upload_store, stop=stop)
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
    """Read one bounded regular file without following a final-path symlink."""

    failure = f"Cannot read provider startup input {path}"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise ValueError(f"{failure}: it is not a regular file")
            if status.st_size > maximum_bytes:
                raise ValueError(f"{failure}: its {status.st_size} bytes exceed the {maximum_bytes}-byte limit")
            content = os.read(descriptor, maximum_bytes + 1)
            if len(content) != status.st_size:
                raise ValueError(f"{failure}: the file changed while it was read")
            return content
        finally:
            os.close(descriptor)
    except OSError as error:
        reason = os.strerror(error.errno) if error.errno is not None else "an operating-system error occurred without a recorded reason"
        raise ValueError(f"{failure}: {reason}") from error


def load_chat_endpoint(execution: ExecutionConfig) -> ChatEndpoint:
    """Load the same interpreter inputs for service startup and live qualification.

    This acquires only interpreter credentials and trust, not API or worker authority.
    """
    key = _read_regular_file(INTERPRETER_KEY_PATH, 16_384)
    if not key.isascii():
        raise ValueError("Interpreter API key must contain only header-safe ASCII characters")
    return ChatEndpoint(execution.interpreter_url, execution.interpreter_model,
                        key.decode("ascii").rstrip("\r\n"),
                        Path("/run/config/provider/interpreter-ca.crt") if execution.interpreter_use_private_ca else None,
                        reasoning_effort=execution.interpreter_reasoning_effort)


def main() -> int:
    """Turn a terminal failure into a nonzero exit without remote error text."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        run_provider()
    except Exception as error:
        print(f"Provider stopped ({type(error).__name__}).", file=os.sys.stderr)
        # Startup and runtime boundaries supply safe outer diagnostics. Hidden
        # parser/transport causes may contain source bytes or credentials.
        traceback.print_exception(error, chain=False, file=os.sys.stderr)
        print(
            "Job admission and hello publication are stopped. If Attempt state or "
            "diagnostics were retained, they are in /state/journal; correct the failure "
            "before restarting. Do not delete a pending result to force a retry.",
            file=os.sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
