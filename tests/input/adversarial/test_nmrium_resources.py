"""A URL-backed state cannot acquire missing bytes or borrow unrelated uploads."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import subprocess
import sys
from threading import Thread
from zipfile import ZipFile

from input.helpers import FIXTURES, WorkerCase


class NmriumResourceTests(WorkerCase):
    def assert_unresolved_resource(self, facts, source):
        self.assertIn('complete', facts, 'Inspection must disclose unresolved resources')
        self.assertFalse(facts['complete'])
        self.assert_issue_mentions(facts, source, 'unavailable', 'proton.jdx')

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

    def test_exact_native_state_resolves_only_its_declared_resource(self):
        self.upload('resource-embedded.nmrium.zip')
        facts = self.discover(member='state.json')
        self.assertTrue(facts['complete'])
        choices = [item for item in facts['representations'] if len(item['sources']) == 2]
        self.assertEqual(len(choices), 1)
        item = choices[0]
        self.assertEqual({source['member'] for source in item['sources']},
                         {'state.json', 'data/authored-proton/proton.jdx'})

    def test_nested_native_state_resolves_its_resource_relative_to_the_state(self):
        with ZipFile(FIXTURES / 'resource-embedded.nmrium.zip') as source:
            state = source.read('state.json')
            proton = source.read('data/authored-proton/proton.jdx')
        self.archive([
            ('experiment/state.json', state),
            ('experiment/data/authored-proton/proton.jdx', proton),
        ])
        facts = self.discover(member='experiment/state.json')
        self.assertTrue(facts['complete'])
        item = next(item for item in facts['representations'] if len(item['sources']) == 2)
        self.assertEqual({source['member'] for source in item['sources']}, {
            'experiment/state.json',
            'experiment/data/authored-proton/proton.jdx',
        })

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

    def test_malformed_dense_state_is_partial_and_does_not_invent_dimension(self):
        base = json.loads((FIXTURES / 'mixed.nmrium').read_text())
        spectrum = base['data']['spectra'][0]
        for mutation, evidence in (
            (lambda: spectrum['info'].pop('dimension'), None),
            (lambda: spectrum['data']['re'].pop(str(len(spectrum['data']['re']) - 1)), 'different lengths'),
        ):
            with self.subTest(evidence=evidence):
                document = json.loads(json.dumps(base))
                spectrum = document['data']['spectra'][0]
                mutation()
                self.upload('malformed.nmrium', contents=json.dumps(document))
                facts = self.discover()
                if evidence is None:
                    item = next(item for item in facts['representations']
                                if item['metadata'].get('nucleus') == '1H')
                    self.assertIsNone(item['metadata']['dimension'])
                else:
                    self.assertFalse(facts['complete'])
                    self.assert_issue_mentions(facts, {'upload_ref': 'upload:sample', 'member': None}, evidence)

    def test_only_the_qualified_nmrium_schema_version_is_recognized(self):
        for version in (20, 22):
            with self.subTest(version=version):
                document = json.loads((FIXTURES / 'mixed.nmrium').read_text())
                document['version'] = version
                self.upload('unsupported.nmrium', contents=json.dumps(document))
                facts = self.discover()
                self.assertFalse(facts['complete'])
                self.assertEqual(facts['representations'], [])
                self.assert_issue_mentions(
                    facts, {'upload_ref': 'upload:sample', 'member': None},
                    'schema version', str(version), 'version 21')

    def test_huge_json_numbers_are_localized_input_issues(self):
        document = json.loads((FIXTURES / 'mixed.nmrium').read_text())
        document['data']['spectra'][0]['data']['re']['0'] = 10 ** 400
        self.upload('huge-number.nmrium', contents=json.dumps(document))
        facts = self.discover()
        self.assertFalse(facts['complete'])
        self.assert_issue_mentions(
            facts, {'upload_ref': 'upload:sample', 'member': None}, 'different lengths')

        document = json.loads((FIXTURES / 'stored-shift.nmrium').read_text())
        document['data']['spectra'][0]['processings'][0]['settings'] = 10 ** 400
        self.upload('huge-shift.nmrium', contents=json.dumps(document))
        facts = self.discover()
        item = self.one(facts)
        self.rejected_shift(item)

    def rejected_shift(self, item):
        response = self.request('analyse', selection={
            'representation_id': item['id'], 'formula': 'C22H36O7',
            'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7'},
            'processing': 'as_stored', 'explanation': 'Use stored data.',
        })
        self.assertEqual(response['outcome'], 'input_rejected')
        self.assert_safe_reason(response['reason'])
        self.assertIn('stored shift is malformed', response['reason'])

    def test_malformed_source_inventory_and_control_bearing_resource_are_safe_issues(self):
        document = json.loads((FIXTURES / 'resource-wrapper.nmrium').read_text())
        document['data']['sources'] = 7
        self.upload('malformed.nmrium', contents=json.dumps(document))
        facts = self.discover()
        self.assertFalse(facts['complete'])
        self.assert_issue_mentions(facts, {'upload_ref': 'upload:sample', 'member': None}, 'source inventory', 'malformed')

        document = json.loads((FIXTURES / 'resource-wrapper.nmrium').read_text())
        document['data']['spectra'][0]['selector']['files'] = ['missing\nforged.jdx']
        self.upload('malformed.nmrium', contents=json.dumps(document))
        facts = self.discover()
        self.assertFalse(facts['complete'])
        self.assertTrue(all(issue['reason'].isprintable() for issue in facts['issues']))
