"""Exercise the maintained acquisition launcher without external downloads."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '5')
        self.end_headers()
        self.wfile.write(b'proof')

    def log_message(self, *_arguments):
        pass


class AcquisitionNetworkTests(unittest.TestCase):
    def test_system_route_uses_relay_and_has_no_direct_route(self):
        with HTTPServer(('127.0.0.1', 0), Origin) as origin:
            thread = threading.Thread(target=origin.serve_forever, daemon=True)
            thread.start()
            try:
                worker = r'''
import os, socket, sys
from urllib.parse import urlsplit
routes = open('/proc/net/route').read().splitlines()[1:]
assert not any(x.split()[1] == '00000000' for x in routes)
assert set(os.listdir('/sys/class/net')) == {'lo'}
with socket.socket() as direct:
    direct.settimeout(2)
    assert direct.connect_ex(('192.0.2.1', 443)) != 0
if socket.has_ipv6:
    try:
        with socket.socket(socket.AF_INET6) as direct:
            direct.settimeout(2)
            assert direct.connect_ex(('2001:db8::1', 443, 0, 0)) != 0
    except OSError:
        pass  # A disabled IPv6 stack also prevents a direct connection.
proxy = urlsplit(os.environ['HTTP_PROXY'])
for tunnel in (False, True):
    with socket.create_connection((proxy.hostname, proxy.port), timeout=3) as conn:
        if tunnel:
            conn.sendall(('CONNECT 127.0.0.1:' + sys.argv[1] + ' HTTP/1.1\r\nHost: fixture\r\n\r\n').encode())
            response = b''
            while b'\r\n\r\n' not in response:
                data = conn.recv(4096)
                assert data
                response += data
                assert len(response) <= 65536
            assert response.startswith(b'HTTP/1.1 200 '), response
            conn.sendall(b'GET /proof HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n')
        else:
            conn.sendall(('GET http://127.0.0.1:' + sys.argv[1] + '/proof HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n').encode())
        conn.shutdown(socket.SHUT_WR)
        response = b''
        while data := conn.recv(4096):
            response += data
            assert len(response) <= 65536
        assert response.endswith(b'\r\n\r\nproof'), response
'''
                env = os.environ | {'SECS_NETWORK_INTERFACE_INPUT': 'system',
                                    'SECS_NETWORK_INTERFACE_ORIGIN': 'command line'}
                process = subprocess.Popen(
                    ['bash', '-c',
                     'source tools/acquisition.sh; grants=(--read-only --entrypoint python3); '
                     'application=(-I -B -c "$2" "$3"); secs_acquire "$1" grants application',
                     'bash', os.environ['DEPLOYMENT_TEST_IMAGE'], worker, str(origin.server_port)],
                    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    _stdout, stderr = process.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        _stdout, stderr = process.communicate(timeout=20)
                    except subprocess.TimeoutExpired:
                        self.fail(f'Acquisition launcher {process.pid} did not confirm cleanup after TERM; inspect its owned resources.')
                    self.fail(f'Acquisition timed out; launcher exited {process.returncode} after TERM: {stderr}')
                self.assertEqual(process.returncode, 0, stderr)
                subprocess.run([sys.executable, '-B', str(ROOT / 'tools/bootstrap/acquisition/acquisition_tool/source_update.py'), 'verify', '--source-dir', str(ROOT / 'tools/bootstrap/acquisition')],
                               cwd=ROOT, check=True)
            finally:
                origin.shutdown()
                thread.join(5)
                self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
