"""A URL-backed state cannot acquire missing bytes or borrow unrelated uploads."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import subprocess
import sys
from threading import Thread

from input.helpers import FIXTURES, WorkerCase


class NmriumResourceTests(WorkerCase):
    def assert_unresolved_resource(self, facts, source):
        self.assertIn('complete', facts, 'Inspection must disclose unresolved resources')
        self.assertFalse(facts['complete'])
        issues = [issue for issue in facts['issues'] if issue['source'] == source]
        self.assertTrue(issues, 'The unresolved NMRium resource must be attributed to its state file')
        reason = ' '.join(issue['reason'] for issue in issues).lower()
        self.assertIn('proton.jdx', reason)
        self.assertRegex(reason, r'missing|unavailable|unresolved|not (available|provided|fetched)|cannot.*(fetch|resolve)')

    def test_same_name_file_does_not_satisfy_a_url_resource(self):
        wrapper = (FIXTURES / 'resource-wrapper.nmrium').read_bytes()
        proton = (FIXTURES / 'proton.jdx').read_bytes()
        for arrangement in ('separate_uploads', 'zip_siblings'):
            with self.subTest(arrangement=arrangement):
                self.files.clear()
                if arrangement == 'separate_uploads':
                    self.upload('resource-wrapper.nmrium')
                    self.upload('proton.jdx', 'upload:proton')
                    state_source = {'upload_ref': 'upload:sample', 'member': None}
                    proton_source = {'upload_ref': 'upload:proton', 'member': None}
                else:
                    self.archive([('state.nmrium', wrapper), ('proton.jdx', proton)])
                    state_source = {'upload_ref': 'upload:sample', 'member': 'state.nmrium'}
                    proton_source = {'upload_ref': 'upload:sample', 'member': 'proton.jdx'}
                facts = self.discover()
                self.assert_unresolved_resource(facts, state_source)
                for item in facts['representations']:
                    if state_source in item['sources']:
                        self.assertNotIn(proton_source, item['sources'], 'A matching filename is not a resource association')
                proton_facts = self.discover('upload:proton') if arrangement == 'separate_uploads' else facts
                self.assertTrue(any(item['kind'] == 'spectrum' and item['sources'] == [proton_source]
                                    for item in proton_facts['representations']))

    def test_inspection_does_not_fetch_a_reachable_url_resource(self):
        connections = []
        proton = (FIXTURES / 'proton.jdx').read_bytes()

        class CanaryServer(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(2)
                connections.append(address)
                return connection, address

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(proton)))
                self.end_headers()
                self.wfile.write(proton)

            def log_message(self, *_arguments):
                pass

        server = CanaryServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .05})
        thread.start()
        try:
            base_url = f'http://127.0.0.1:{server.server_port}/'
            subprocess.run([sys.executable, '-c',
                'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2).read()',
                base_url + 'probe'], check=True, timeout=5, capture_output=True)
            self.assertEqual(len(connections), 1, 'The resource canary must be reachable from another process')
            connections.clear()
            wrapper = json.loads((FIXTURES / 'resource-wrapper.nmrium').read_text())
            self.assertEqual(len(wrapper['data']['sources']), 1)
            self.assertEqual(wrapper['data']['sources'][0]['baseURL'], 'https://fixture.invalid/')
            wrapper['data']['sources'][0]['baseURL'] = base_url
            self.upload('state.nmrium', contents=json.dumps(wrapper))
            response = self.request('inspect', source={'upload_ref': 'upload:sample', 'member': None})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive(), 'The resource canary did not shut down')
        self.assertEqual(connections, [], 'Inspection contacted an external resource named by the uploaded state')
        self.assertEqual(response['outcome'], 'inspected')
        self.assert_unresolved_resource(response['facts'], {'upload_ref': 'upload:sample', 'member': None})
        self.assertEqual(self.inference.mock_calls, [])
        self.assertEqual(self.candidates.mock_calls, [])
