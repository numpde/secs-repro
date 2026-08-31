"""Production composition for the provider hello process."""

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
    """Load hello inputs once, publish immediately, and resend until stopped.

    Configuration, credentials, TLS trust, and presentation remain fixed for
    the process lifetime. A deployment change takes effect after a restart.
    """

    config = decode_provider_config(_read_regular_file(config_path, _CONFIG_MAX_BYTES))
    credential = parse_provider_credential(
        _read_regular_file(
            CREDENTIAL_PATH,
            PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES,
        )
    )
    prepared = prepare_configured_hello(config)
    api = ProviderApi(
        endpoint=config.endpoint.materialize(),
        provider_ref=credential.provider_ref,
        credential_ref=credential.credential_ref,
        private_key=credential.private_key,
    )
    stop = Event()
    previous_handlers = {
        signal_number: signal.signal(signal_number, lambda *_args: stop.set())
        for signal_number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        publish_hello_until_stopped(
            api=api,
            prepared=prepared,
            policy=config.hello,
            stop=stop,
        )
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
    """Read one bounded regular file without following a final-path symlink."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError(f"Cannot read provider startup input {path}") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > maximum_bytes:
            raise ValueError("Provider startup input must be a bounded regular file")
        content = os.read(descriptor, maximum_bytes + 1)
        if len(content) != status.st_size:
            raise ValueError("Provider startup input changed while it was read")
        return content
    finally:
        os.close(descriptor)


def main() -> int:
    """Run the hello process and turn a terminal failure into a nonzero exit."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        run_provider()
    except Exception as error:
        print("Provider hello could not start or has stopped.", file=os.sys.stderr)
        traceback.print_exception(error, file=os.sys.stderr)
        print(
            "Hello publication is stopped. Fix the error above, then restart "
            "the provider.",
            file=os.sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
