"""Interpreter endpoints are provider-owned and selected in filename order."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from secs_inference.provider.configuration_error import ConfigurationError
from secs_inference.provider.interpreter_config import decode_endpoint, read_endpoints


class InterpreterConfigTests(TestCase):
    def test_endpoint_files_are_selected_in_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('10-strong', '00-fast'):
                (root / (name + '.toml')).write_text(
                    'url = "https://model.example/v1/chat/completions"\n'
                    'model = "model"\nkey_file = "/run/secrets/provider/interpreters/model.key"\n')
            (root / 'ignored.toml.example').write_text('not TOML')
            endpoints = read_endpoints(root, lambda path, **_: path.read_bytes())
            self.assertEqual([endpoint.name for endpoint in endpoints], ['00-fast', '10-strong'])

    def test_key_must_be_an_installed_interpreter_secret(self):
        raw = b'url = "https://model.example"\nmodel = "m"\nkey_file = "/tmp/key"\n'
        with self.assertRaisesRegex(ConfigurationError, 'installed interpreter key'):
            decode_endpoint('00-fast', raw)

    def test_example_supports_hello_only_without_execution_or_interpreter(self):
        from secs_inference.provider.config import decode_provider_config

        example = (Path(__file__).resolve().parents[2] / 'config/provider.toml.example').read_text()
        hello_only = example.split('[execution]', 1)[0].encode()
        self.assertIsNone(decode_provider_config(hello_only).execution)

    def test_ordered_endpoints_capture_distinct_keys_and_fresh_carriers(self):
        from secs_inference.provider.main import _read_chat_settings

        endpoints = tuple(decode_endpoint(name, (f'url = "https://{name}.example/v1/chat/completions"\n'
            f'model = "{name}"\nkey_file = "/run/secrets/provider/interpreters/{name}.key"\n').encode())
            for name in ('fast', 'strong'))
        keys = {'fast.key': b'fast-key', 'strong.key': b'strong-key'}
        with patch('secs_inference.provider.main.read_endpoints', return_value=endpoints), \
                patch('secs_inference.provider.main.read_startup_file', side_effect=lambda path, **_: keys[path.name]):
            services, secrets = _read_chat_settings(30, 2)
        self.assertEqual(secrets, ('fast-key', 'strong-key'))
        carriers = [service(exchange_seconds=5, connect_seconds=2) for service in services]
        self.assertEqual([carrier.spec.model for carrier in carriers], ['fast', 'strong'])
        self.assertEqual([carrier.spec.api_key for carrier in carriers], list(secrets))
        self.assertIsNot(carriers[0], services[0](exchange_seconds=5, connect_seconds=2))

