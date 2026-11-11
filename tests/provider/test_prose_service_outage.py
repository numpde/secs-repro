"""The running SECS provider must honor one prose service's wait across Jobs.

These tests exercise SECS's real discovery/admission loop and exact SDK
StartPending construction. The model-service delay and scientific worker are
controlled test ports; the SDK owns HTTP request and Retry-After semantics.
"""
import asyncio
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.attempt_record import RecoveryScope
from provider_core.conversation import ToolFinished
from provider_core.diagnostics import failure_evidence
from provider_core.interpreter import InterpretationFailed, Interpreter
from provider_core.job_input import FeedInputFacts
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from secs_inference.provider.interpreter_config import decode_endpoint
from secs_inference.provider.main import _read_chat_settings
from secs_inference.provider.runtime import run_execution
from tls_fixture import _write_test_certificates


class ProseServiceOutageTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_http_service_preserves_retry_after_between_jobs(self):
        stop, first_finished = asyncio.Event(), asyncio.Event()
        requests, starts, discoveries = [], [], []
        failures, postponements = [], []
        healthy = False

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                requests.append(monotonic())
                if healthy:
                    body = json.dumps({'choices': [{'message': {'role': 'assistant', 'tool_calls': [{
                        'id': 'prepared', 'type': 'function',
                        'function': {'name': 'prepare', 'arguments': '{}'},
                    }]}, 'finish_reason': 'tool_calls'}]}).encode()
                    self.send_response(200)
                else:
                    body = b'{"error":{"message":"unavailable"}}'
                    self.send_response(503)
                    self.send_header('Retry-After', '2')
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_arguments):
                pass

        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_test_certificates(root)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            trust = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            trust.load_cert_chain(root / 'server.pem', root / 'server-key.pem')
            server.socket = trust.wrap_socket(server.socket, server_side=True)
            thread = Thread(target=server.serve_forever)
            thread.start()
            task = None
            try:
                endpoint = decode_endpoint('local', (
                    f'url = "https://localhost:{server.server_port}/v1/chat/completions"\n'
                    'model = "local"\nkey_file = "/run/secrets/provider/interpreters/local.key"\n'
                    'ca_file = "/run/config/provider/interpreter.d/local.crt"\n'
                ).encode())
                with patch('secs_inference.provider.main.read_endpoints', return_value=(endpoint,)), \
                        patch('secs_inference.provider.main.read_startup_file', side_effect=lambda path, **_: (
                            b'local-test-key' if Path(path).suffix == '.key' else (root / 'ca.pem').read_bytes())):
                    services, secrets = _read_chat_settings(1, 1)
                postpone = services[0]._postpone

                def observe_postponement(failure, delay):
                    postpone(failure, delay)
                    response = failure.response
                    postponements.append({
                        'job_ref': starts[-1][0], 'at': monotonic(), 'delay': delay,
                        'status': response.status if response is not None else None,
                        'retry_after': [value.decode('ascii', errors='replace') for name, value in response.headers
                                        if name.lower() == b'retry-after'] if response is not None else [],
                    })

                self.enterContext(patch.object(services[0], '_postpone', side_effect=observe_postponement))
                payload = b'Interpret this Job'
                facts = tuple(FeedInputFacts(
                    f'job:configured-outage-{index}', 'nmr.job.specification.text.v1',
                    'sha256:' + sha256(payload).hexdigest(), len(payload),
                ) for index in range(2))

                class Discovery:
                    async def jobs(self, **_query):
                        discoveries.append(monotonic())
                        return SimpleNamespace(jobs=(SimpleNamespace(input=facts[len(starts)]),))

                class Controller:
                    shutdown_ready = True

                    async def run(self, pending):
                        starts.append((pending.input.job_ref, monotonic()))
                        # Reach the real HTTPS child while keeping the model window
                        # below Retry-After 2, which must defer the next Job.
                        owner = Interpreter(services, timeout_seconds=1.5, context_bytes=16384, secrets=secrets)
                        dispatch = AsyncMock(return_value=ToolFinished('prepared'))
                        try:
                            result = await owner.run(
                                [{'role': 'user', 'content': pending.input.job_ref}],
                                [{'type': 'function', 'function': {
                                    'name': 'prepare', 'parameters': {'type': 'object', 'properties': {}},
                                }}], dispatch,
                            )
                        except InterpretationFailed as failure:
                            failures.append({'job_ref': pending.input.job_ref,
                                             'failure': failure_evidence(failure, secrets=secrets)})
                            if len(starts) != 1:
                                raise
                            dispatch.assert_not_awaited()
                            first_finished.set()
                        else:
                            if result.value != 'prepared' or len(starts) != 2:
                                raise AssertionError('The recovered Job must prepare exactly once')
                            dispatch.assert_awaited_once()
                            stop.set()
                        finally:
                            await owner.retire()

                task = asyncio.create_task(run_execution(
                    api=Discovery(), scope=RecoveryScope('https://api.test', 'provider:secs-test', 'secs', 'http'),
                    claims=AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152)),
                    stop=stop, poll_seconds=0.01, assemble=lambda _pending: Controller(),
                    worker=SimpleNamespace(check_ready=AsyncMock()), startup_seconds=1, prose_services=services,
                ))
                await asyncio.wait_for(first_finished.wait(), 2)
                evidence = json.dumps({'failures': failures, 'postponements': postponements,
                                       'requests': requests, 'starts': starts, 'discoveries': discoveries})
                self.assertEqual(1, len(postponements), msg=evidence)
                observed = postponements[0]
                self.assertEqual(facts[0].job_ref, observed['job_ref'], msg=evidence)
                self.assertEqual(503, observed['status'], msg=evidence)
                self.assertEqual(['2'], observed['retry_after'], msg=evidence)
                self.assertGreaterEqual(observed['delay'], 2, msg=evidence)
                self.assertGreater(services[0].retry_seconds, 0, msg=evidence)
                self.assertEqual(1, len(starts))
                self.assertEqual(1, len(discoveries))
                healthy = True
                await asyncio.wait_for(task, 5)
                self.assertEqual([fact.job_ref for fact in facts], [ref for ref, _ in starts])
                self.assertEqual(2, len(requests))
                self.assertGreaterEqual(starts[1][1] - requests[0], 2)
            finally:
                stop.set()
                if task is not None:
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join()

    async def test_a_second_job_waits_for_the_first_jobs_service_delay_then_runs_automatically(self):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        first_finished = asyncio.Event()
        starts = []
        discoveries = []
        delay_until = 0.0
        payload = b"1H NMR interpretation input"
        facts = tuple(
            FeedInputFacts(f"job:secs-outage-{index}", "nmr.job.specification.text.v1",
                           "sha256:" + sha256(payload).hexdigest(), len(payload))
            for index in range(2)
        )

        class Service:
            @property
            def retry_seconds(self):
                return max(0.0, delay_until - loop.time())

        class Discovery:
            async def jobs(self, **query):
                discoveries.append((loop.time(), query))
                return SimpleNamespace(jobs=(SimpleNamespace(input=facts[len(starts)]),))

        class Worker:
            async def check_ready(self, *, deadline):
                self_deadline = deadline
                if self_deadline < monotonic():
                    raise AssertionError("The original worker-readiness deadline elapsed")

        class Controller:
            shutdown_ready = True

            async def run(self, pending):
                nonlocal delay_until
                starts.append((pending.input.job_ref, loop.time()))
                if len(starts) == 1:
                    # The SDK already classified the preceding service refusal;
                    # a new Job must not spend that service's requested wait.
                    delay_until = loop.time() + 0.25
                    first_finished.set()
                else:
                    stop.set()

        async def run():
            await run_execution(
                api=Discovery(),
                scope=RecoveryScope("https://api.test", "provider:secs-test", "secs", "b4"),
                claims=AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152)),
                stop=stop, poll_seconds=0.01, assemble=lambda _pending: Controller(),
                worker=Worker(), startup_seconds=1, prose_services=(Service(),),
            )

        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(first_finished.wait(), 1)
            await asyncio.sleep(0.08)
            self.assertEqual(1, len(starts), "SECS started another Job during the previous Job's prose-service wait")
            self.assertEqual(1, len(discoveries), "SECS continued discovery while all prose services were unavailable")
            await asyncio.wait_for(task, 1)
            self.assertEqual([item.job_ref for item in facts], [ref for ref, _ in starts])
            self.assertGreaterEqual(starts[1][1] - starts[0][1], 0.25)
        finally:
            stop.set()
            await asyncio.wait_for(task, 2)

    async def test_stop_interrupts_a_long_service_delay_without_discovering_a_job(self):
        stop = asyncio.Event()
        selected = []
        class Service:
            retry_seconds = 600.0
        class Discovery:
            async def jobs(self, **_query):
                selected.append("discovered")
                raise AssertionError("No Job should be discovered while the sole prose service is postponed")
        task = asyncio.create_task(run_execution(
            api=Discovery(), scope=RecoveryScope("https://api.test", "provider:secs-test", "secs", "stop"),
            claims=AttemptClaims.live(limits=LiveAttemptLimits(1, 131072, 2097152)),
            stop=stop, poll_seconds=0.01,
            assemble=lambda _pending: (_ for _ in ()).throw(AssertionError("No Attempt may be constructed")),
            worker=SimpleNamespace(check_ready=None), startup_seconds=1, prose_services=(Service(),),
        ))
        try:
            await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(task, 0.5)
            self.assertEqual([], selected)
        finally:
            stop.set()
            await asyncio.wait_for(task, 1)
