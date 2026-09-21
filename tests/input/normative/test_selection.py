"""An exact choice reaches the encoder without first-file or first-block substitution."""

import json
from zipfile import ZipFile

import numpy as np
import torch
from unittest.mock import Mock

from secs.elucidation import StaticCandidateSource
from secs_inference.model import SecsInference

from input.helpers import FIXTURES, WorkerCase


class SelectedInputTests(WorkerCase):
    def setUp(self):
        super().setUp()
        self.model = Mock(encode_modality=Mock(return_value=torch.tensor([[1., 0.]])))
        adapter = SecsInference(self.model, None, torch.device('cpu'), torch.float32, 10000, 1, 1)
        self.inference = Mock(wraps=adapter)
        self.candidates = Mock(wraps=StaticCandidateSource([]))
        self.worker.inference = self.inference
        self.worker.candidates = self.candidates

    def analyse(self, representation, processing='as_stored'):
        return self.request('analyse', selection={
            'representation_id': representation['id'], 'formula': 'C22H36O7',
            'processing': processing, 'explanation': 'Use the explicitly selected synthetic proton data.'})

    def reference(self, name):
        document = json.loads((FIXTURES / f'{name}.reference.json').read_text())
        return np.asarray(document['intensities'], dtype=np.float32)

    def reset_observations(self):
        self.model.reset_mock()
        self.inference.reset_mock()
        self.candidates.reset_mock()

    def assert_encoder_input(self, reference):
        self.model.encode_modality.assert_called_once()
        call = self.model.encode_modality.call_args
        self.assertEqual(call.kwargs, {'modality': 'h_nmr'})
        tensor = call.args[0]
        self.assertEqual(tuple(tensor.shape), (1, 1, 10000))
        self.assertEqual(tensor.dtype, torch.float32)
        self.assertTrue(torch.isfinite(tensor).all())
        # Match the existing reference lanes' one-Float32-ULP allowance.
        np.testing.assert_array_max_ulp(tensor.cpu().numpy().reshape(-1), reference[::-1], maxulp=1)

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
                    self.assert_encoder_input(references[name])
            self.files.clear()

    def test_selected_link_block_reaches_encoder(self):
        self.upload('linked.jdx')
        facts = self.discover()
        self.assertEqual(len(facts['representations']), 2)
        response = self.analyse(self.one(facts))
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.assert_encoder_input(self.reference('linked.jdx'))

    def test_reference_encodings_reach_the_encoder(self):
        names = ['ascending.jdx', 'complex.jdx', 'mixed.nmrium', 'upstream-4-chlorobenzylamine.jdx']
        names += [f'encoded-{encoding}.jdx' for encoding in ('fix', 'sqz', 'dif', 'difdup', 'pac')]
        for name in names:
            with self.subTest(fixture=name):
                self.reset_observations()
                self.upload(name)
                response = self.analyse(self.one(self.discover()))
                self.assertEqual(response['outcome'], 'no_starting_candidates')
                self.assert_encoder_input(self.reference('proton.jdx' if name == 'mixed.nmrium' else name))

    def test_explicit_fid_processing_matches_reference(self):
        for name, magnitude in (('fid.jdx', False), ('fid-magnitude.jdx', True)):
            with self.subTest(fixture=name):
                reference = json.loads((FIXTURES / f'{name}.reference.json').read_text())
                self.assertIs(reference['from_fid'], True)
                self.assertIs(reference['magnitude'], magnitude)
                self.reset_observations()
                self.upload(name)
                response = self.analyse(self.one(self.discover(), 'fid'), processing='auto')
                self.assertEqual(response['outcome'], 'no_starting_candidates')
                self.assert_encoder_input(np.asarray(reference['intensities'], dtype=np.float32))
                preparation = response['analysis']['preparation']
                self.assertIs(preparation['from_fid'], True)
                self.assertIs(preparation['magnitude'], magnitude)

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
                self.assert_encoder_input(self.reference(name))
                tensor = self.model.encode_modality.call_args.args[0].numpy().reshape(-1)
                peak_ppm = 10 - int(np.argmax(tensor)) * 12 / 9999
                self.assertAlmostEqual(peak_ppm, 3.03125, delta=.002)

    def test_annotations_do_not_change_the_selected_spectrums_encoder_input(self):
        self.archive([(f'sample/{name}', (FIXTURES / name).read_bytes())
                      for name in ('annotations.sdf', 'proton.jdx')])
        response = self.analyse(self.one(self.discover()))
        self.assertEqual(response['outcome'], 'no_starting_candidates')
        self.assert_encoder_input(self.reference('proton.jdx'))

    def test_encoder_adapter_uses_training_order_without_discovery(self):
        reference = self.reference('alternate.jdx')
        self.inference.embed_spectrum(reference)
        self.assert_encoder_input(reference)
        tensor = self.model.encode_modality.call_args.args[0].numpy().reshape(-1)
        peak_ppm = 10 - int(np.argmax(tensor)) * 12 / 9999
        self.assertAlmostEqual(peak_ppm, 4, delta=.002)
