"""An exact choice reaches scientific inference without source substitution."""

import json
from zipfile import ZipFile

import numpy as np
import torch
from unittest.mock import Mock

from secs.elucidation import StaticCandidateSource
from secs_inference.model import SecsInference
from support_evidence import qualification_evidence

from input.helpers import FIXTURES, WorkerCase


class SelectedInputTests(WorkerCase):
    def setUp(self):
        super().setUp()
        self.inference.embed_spectrum.return_value = np.array([1., 0.], dtype=np.float32)
        self.candidates = Mock(wraps=StaticCandidateSource([]))
        self.worker.candidates = self.candidates

    def analyse(self, representation, processing='as_stored'):
        return self.request('analyse', selection={
            'representation_id': representation['id'], 'formula': 'C22H36O7',
            'formula_evidence': {'kind': 'job_specification', 'quote': 'C22H36O7'},
            'processing': processing, 'explanation': 'Use the explicitly selected synthetic proton data.'})

    def reference(self, name):
        document = json.loads((FIXTURES / f'{name}.reference.json').read_text())
        return np.asarray(document['intensities'], dtype=np.float32)

    def reset_observations(self):
        self.inference.reset_mock()
        self.candidates.reset_mock()

    def assert_prepared_spectrum(self, reference):
        self.inference.embed_spectrum.assert_called_once()
        spectrum = self.inference.embed_spectrum.call_args.args[0]
        self.assertEqual(spectrum.shape, (10000,))
        self.assertEqual(spectrum.dtype, np.float32)
        self.assertTrue(np.isfinite(spectrum).all())
        # The pinned JavaScript normalization and NumPy differ by two Float32
        # ULPs at one of 10,000 points for the magnitude-FID vector.
        np.testing.assert_array_max_ulp(spectrum, reference, maxulp=2)

    @qualification_evidence("input.multiple-uploads.selection.v1")
    def test_upload_order_cannot_replace_the_selected_proton_spectrum(self):
        references = {name: self.reference(name) for name in ('proton.jdx', 'alternate.jdx')}
        self.assertGreater(float(np.max(np.abs(references['proton.jdx'] - references['alternate.jdx']))), .9)
        for order in (('proton.jdx', 'alternate.jdx'), ('alternate.jdx', 'proton.jdx')):
            self.reset_observations()
            for name in order:
                self.upload(name, f'upload:{name}')
            choices = {name: self.one(self.discover(f'upload:{name}')) for name in order}
            for name in order:
                with self.subTest(order=order, selected=name):
                    self.reset_observations()
                    response = self.analyse(choices[name])
                    self.assertEqual(response['outcome'], 'no_starting_candidates')
                    self.assert_prepared_spectrum(references[name])
            self.files.clear()

    @qualification_evidence("input.jcamp.link.inference-input.v1")
    def test_selected_link_block_reaches_inference(self):
        self.upload('linked.jdx')
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 2)
        response = self.analyse(self.one(facts))
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.assert_prepared_spectrum(self.reference('linked.jdx'))

    @qualification_evidence("input.jcamp.processed.inference-input.v1")
    def test_reference_encodings_reach_inference(self):
        names = ['ascending.jdx', 'complex.jdx', 'mixed.nmrium', 'upstream-4-chlorobenzylamine.jdx']
        names += [f'encoded-{encoding}.jdx' for encoding in ('fix', 'sqz', 'dif', 'difdup', 'pac')]
        for name in names:
            with self.subTest(fixture=name):
                self.reset_observations()
                self.upload(name)
                response = self.analyse(self.one(self.discover()))
                self.assertEqual(response['outcome'], 'no_starting_candidates')
                self.assert_prepared_spectrum(self.reference('proton.jdx' if name == 'mixed.nmrium' else name))

    @qualification_evidence("input.jcamp.fid.inference-input.v1")
    def test_explicit_fid_processing_matches_reference(self):
        for name, magnitude in (('fid.jdx', False), ('fid-magnitude.jdx', True)):
            with self.subTest(fixture=name):
                reference = json.loads((FIXTURES / f'{name}.reference.json').read_text())
                self.assertIs(reference['from_fid'], True)
                self.assertIs(reference['magnitude'], magnitude)
                self.reset_observations()
                self.upload(name)
                selected = self.one(self.discover(), 'fid')
                response = self.analyse(selected, processing='auto')
                self.assertEqual(response['outcome'], 'no_starting_candidates')
                self.assert_prepared_spectrum(np.asarray(reference['intensities'], dtype=np.float32))
                preparation = response['analysis']['preparation']
                self.assertEqual(preparation['representation_id'], selected['id'])
                self.assertIs(preparation['from_fid'], True)
                self.assertIs(preparation['magnitude'], magnitude)

    @qualification_evidence("input.vendor-fids.inference-input.v1")
    def test_raw_vendor_fids_decode_to_the_same_automatic_result(self):
        observed = {}
        for vendor, parameter in (('bruker', 'acqus'), ('varian', 'procpar')):
            with self.subTest(vendor=vendor):
                self.reset_observations()
                self.archive([
                    ('experiment/fid', (FIXTURES / f'{vendor}-fid.bin').read_bytes()),
                    (f'experiment/{parameter}', (FIXTURES / f'{vendor}-{parameter}.txt').read_bytes()),
                ])
                response = self.analyse(self.one(self.discover(), 'fid'), processing='auto')
                self.assertEqual(response['outcome'], 'no_starting_candidates')
                preparation = response['analysis']['preparation']
                self.assertIs(preparation['from_fid'], True)
                observed[vendor] = self.inference.embed_spectrum.call_args.args[0].copy()
                self.files.clear()
        # The authored Bruker trace stores Float64 while Varian stores Float32.
        np.testing.assert_allclose(observed['bruker'], observed['varian'], rtol=5e-7, atol=1e-7)

    @qualification_evidence("input.bruker.processed.inference-input.v1")
    def test_selected_processed_bruker_reaches_inference(self):
        self.archive([
            ('sample/1/pdata/1/1r', (FIXTURES / 'bruker-1r.bin').read_bytes()),
            ('sample/1/pdata/1/procs', (FIXTURES / 'bruker-procs.txt').read_bytes()),
        ])
        selected = self.one(self.discover())

        response = self.analyse(selected)

        self.assertEqual(response['outcome'], 'no_starting_candidates')
        preparation = response['analysis']['preparation']
        self.assertEqual(preparation['representation_id'], selected['id'])
        self.assertIs(preparation['from_fid'], False)
        self.inference.embed_spectrum.assert_called_once()
        spectrum = self.inference.embed_spectrum.call_args.args[0]
        self.assertEqual(spectrum.shape, (10000,))
        self.assertEqual(spectrum.dtype, np.float32)
        self.assertTrue(np.isfinite(spectrum).all())
        peak_ppm = -2 + int(np.argmax(spectrum)) * 12 / 9999
        self.assertAlmostEqual(peak_ppm, 10 - 20 * 10 / 63, delta=.002)

    def test_fid_references_shift_the_prepared_chemical_axis(self):
        original = (FIXTURES / 'fid.jdx').read_text()
        peaks = []
        for offset in (0, 2):
            with self.subTest(format='jcamp', offset=offset):
                self.reset_observations()
                self.upload('offset-fid.jdx', contents=original.replace('##$OFFSET=0', f'##$OFFSET={offset}'))
                response = self.analyse(self.one(self.discover(), 'fid'), processing='auto')
                self.assertEqual(response['outcome'], 'no_starting_candidates')
                peaks.append(int(np.argmax(self.inference.embed_spectrum.call_args.args[0])))
        self.assertAlmostEqual(peaks[1] - peaks[0], 2 * 9999 / 12, delta=2)

        acqus = (FIXTURES / 'bruker-acqus.txt').read_text()
        peaks = []
        for center_ppm in (0, 2):
            with self.subTest(format='bruker', center_ppm=center_ppm):
                self.reset_observations()
                parameters = acqus.replace('##$O1= 0', f'##$O1= {center_ppm * 400}')
                self.archive([
                    ('experiment/fid', (FIXTURES / 'bruker-fid.bin').read_bytes()),
                    ('experiment/acqus', parameters.encode()),
                ])
                response = self.analyse(self.one(self.discover(), 'fid'), processing='auto')
                self.assertEqual(response['outcome'], 'no_starting_candidates')
                peaks.append(int(np.argmax(self.inference.embed_spectrum.call_args.args[0])))
                self.files.clear()
        self.assertAlmostEqual(peaks[1] - peaks[0], 2 * 9999 / 12, delta=2)

    @qualification_evidence("input.jeol.processed.inference-input.v1")
    def test_jeol_preparation_uses_the_stored_axis(self):
        self.upload('synthetic.jdf')
        facts = self.discover()
        selected = self.one(facts)
        self.assertEqual(selected['metadata']['ppm_from'], 10)
        self.assertEqual(selected['metadata']['ppm_to'], 0)
        response = self.analyse(selected)
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        spectrum = self.inference.embed_spectrum.call_args.args[0]
        peak_ppm = -2 + int(np.argmax(spectrum)) * 12 / 9999
        self.assertAlmostEqual(peak_ppm, 10 - 20 * 10 / 63, delta=.002)

    @qualification_evidence("input.nmrium.v21.inference-input.v1")
    def test_nmrium_stored_shift_is_applied_exactly_once(self):
        for name in ('stored-shift.nmrium', 'resource-embedded.nmrium.zip'):
            with self.subTest(fixture=name):
                self.reset_observations()
                self.upload(name)
                facts = self.discover()
                self.assertTrue(facts['complete'])
                if name.endswith('.zip'):
                    required = {'state.json', 'data/authored-proton/proton.jdx'}
                    choices = [item for item in facts['representations']
                               if required <= {source['member'] for source in item['sources']}]
                    self.assertEqual(len(choices), 1, 'The saved spectrum and its resource must remain one choice')
                    selected = choices[0]
                    with ZipFile(FIXTURES / name) as archive:
                        members = set(archive.namelist())
                    for source in selected['sources']:
                        self.assertEqual(source['upload_ref'], 'upload:sample')
                        self.assertIn(source['member'], members)
                else:
                    selected = self.one(facts)
                    self.assertEqual(selected['sources'], [{'upload_ref': 'upload:sample', 'member': None}])
                response = self.analyse(selected)
                self.assertEqual(response['outcome'], 'no_starting_candidates')
                self.assert_prepared_spectrum(self.reference(name))
                spectrum = self.inference.embed_spectrum.call_args.args[0]
                peak_ppm = -2 + int(np.argmax(spectrum)) * 12 / 9999
                self.assertAlmostEqual(peak_ppm, 3.03125, delta=.002)

    @qualification_evidence("input.nmredata.annotations-no-spectrum-change.v1")
    def test_annotations_do_not_change_the_selected_prepared_spectrum(self):
        self.archive([(f'sample/{name}', (FIXTURES / name).read_bytes())
                      for name in ('annotations.sdf', 'proton.jdx')])
        response = self.analyse(self.one(self.discover()))
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.assert_prepared_spectrum(self.reference('proton.jdx'))

    def test_one_selected_spectrum_reaches_the_model_adapter(self):
        model = Mock(encode_modality=Mock(return_value=torch.tensor([[1., 0.]])))
        self.inference = Mock(wraps=SecsInference(
            model, None, torch.device('cpu'), torch.float32, 10000, 1, 1))
        self.worker.inference = self.inference
        reference = self.reference('alternate.jdx')
        self.upload('alternate.jdx')
        response = self.analyse(self.one(self.discover()))
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.assert_prepared_spectrum(reference)
        model.encode_modality.assert_called_once()
        spectrum = self.inference.embed_spectrum.call_args.args[0]
        peak_ppm = -2 + int(np.argmax(spectrum)) * 12 / 9999
        self.assertAlmostEqual(peak_ppm, 4, delta=.002)
