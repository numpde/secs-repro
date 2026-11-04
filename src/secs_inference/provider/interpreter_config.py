"""Load the provider's ordered interpreter endpoint definitions."""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from secs_inference.provider.configuration_error import ConfigurationError


INTERPRETER_DIRECTORY = Path('/run/config/provider/interpreter.d')
KEY_DIRECTORY = Path('/run/secrets/provider/interpreters')


@dataclass(frozen=True, slots=True)
class InterpreterEndpoint:
    name: str
    url: str
    model: str
    key_file: Path
    ca_file: Path | None = None
    reasoning_effort: str | None = None


def decode_endpoint(name: str, raw: bytes) -> InterpreterEndpoint:
    try:
        table = tomllib.loads(raw.decode('utf-8'))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f'Interpreter endpoint {name!r} is not valid TOML.') from error
    required = {'url', 'model', 'key_file'}
    if not required <= table.keys() or table.keys() - required - {'ca_file', 'reasoning_effort'}:
        raise ConfigurationError(f'Interpreter endpoint {name!r} has missing or unsupported fields.')
    for key in ('key_file', 'ca_file'):
        if key in table and (type(table[key]) is not str or not table[key].strip()):
            raise ConfigurationError(f'Interpreter endpoint {name!r} field {key} must be nonempty text.')
    key_file = Path(table['key_file'])
    if (str(key_file) != table['key_file'] or key_file.parent != KEY_DIRECTORY
            or re.fullmatch(r'[a-z0-9][a-z0-9_-]*\.key', key_file.name) is None):
        raise ConfigurationError(f'Interpreter endpoint {name!r} key_file must name an installed interpreter key.')
    ca_file = Path(table['ca_file']) if 'ca_file' in table else None
    if ca_file is not None and (str(ca_file) != table['ca_file'] or ca_file.parent != INTERPRETER_DIRECTORY):
        raise ConfigurationError(f'Interpreter endpoint {name!r} ca_file must name a file in interpreter.d.')
    return InterpreterEndpoint(name, table['url'], table['model'], key_file, ca_file, table.get('reasoning_effort'))


def read_endpoints(directory: Path, read_file) -> tuple[InterpreterEndpoint, ...]:
    """Read only endpoint TOML files in filename order using the startup reader."""
    try:
        files = sorted(directory.glob('*.toml'))
    except OSError as error:
        raise ConfigurationError('Cannot list the interpreter endpoint directory.') from error
    if not files:
        raise ConfigurationError('Configure at least one interpreter endpoint in interpreter.d.')
    return tuple(decode_endpoint(path.stem, read_file(path, maximum_bytes=65536,
                                                      label='interpreter endpoint')) for path in files)
