// SPDX-License-Identifier: AGPL-3.0-only
// Explicit fixture refresh in the pinned reference image; never run by tests.
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { from1DNMRVariables } from '/opt/frontend/node_modules/convert-to-jcamp/lib/index.js';
import { readSpectrum } from '/opt/frontend/src/spectrum/readSpectrum.ts';
import { normalizeSpectrum } from '/opt/frontend/src/spectrum/normalize.ts';
import { NMRiumCore } from '/opt/frontend/node_modules/@zakodium/nmrium-core/dist/nmrium-core.js';
import * as plugins from '/opt/frontend/node_modules/@zakodium/nmrium-core-plugins/dist/nmrium-core-plugins.js';
import { FileCollection } from '/opt/frontend/node_modules/file-collection/lib/index.js';

const revision = '5ab78f61e9fb679f3f0b9823be5217ae250e213f';
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const referenceSources = {
  'package-lock.json': '33f75513877322a49681aaacfc3609ffee3e616e4e085d1c3c9d31ecb915409b',
  'src/spectrum/readSpectrum.ts': 'be754221bd152bd92cdf1f5762eda4cce566b0ac622817f5b57a3cc865aa3623',
  'src/spectrum/normalize.ts': '9a127bfd29bfea840ec93ae6132222ddbdf1f8e1c300f1f72f000a64c82676aa',
  'src/spectrum/phaseSpectrum.ts': '4d485bc4aee0759ae9c41fbbe407b3d85b24f29483b36b68cff9a5851edd2427',
  'src/spectrum/grid.ts': 'f157c4855a8c2e56332c3105d3b3c098ec02d80a17cca7093078299a78407c98',
  'src/spectrum/phaseSearch.ts': 'f661b4b89800eec5083b6e2a789a297b8f3b29f615fdfe0fa0c2affe541ea792',
  'src/spectrum/integral.ts': '14f998d63e16417a3daad245251c9e30b4858169cb95257fd2c60704d4e9c042',
};
for (const [path, expected] of Object.entries(referenceSources)) {
  if (hash(await readFile(`/opt/frontend/${path}`)) !== expected) throw Error(`Reference input differs from ${revision}: ${path}`);
}
const exporter = JSON.parse(await readFile('/opt/frontend/node_modules/convert-to-jcamp/package.json'));
if (exporter.version !== '7.0.1') throw Error('Fixture exporter must be convert-to-jcamp 7.0.1');
const files = [];
const x = Array.from({ length: 257 }, (_, i) => 10 - i * 12 / 256);
const y = x.map((v) => Math.round(10000 * Math.exp(-(((v - 2.03125) / 0.12) ** 2))
  + 6000 * Math.exp(-(((v - 7) / 0.18) ** 2))));

async function save(name, bytes, description, source = null, parent = null) {
  await writeFile(`/output/${name}`, bytes);
  files.push({ path: name, sha256: hash(bytes), description, ...(parent ? { parent } : {}),
    origin: source ?? { generator: 'tools/generate_input_fixtures.mjs',
      author: 'secs-repro contributors', licence: 'AGPL-3.0-only',
      licence_text: 'LICENSE', basis: 'New synthetic test data; no experimental measurements copied' } });
}

function affn(axis, values, nucleus = '1H') {
  const header = `##TITLE=Synthetic two-peak spectrum
##JCAMP-DX=5.00
##DATA TYPE=NMR SPECTRUM
##DATA CLASS=XYDATA
##ORIGIN=secs-repro synthetic fixture
##OWNER=secs-repro contributors; AGPL-3.0-only
##.OBSERVE NUCLEUS=^${nucleus}
##.OBSERVE FREQUENCY=400
##XUNITS=PPM
##YUNITS=ARBITRARY UNITS
##XFACTOR=1
##YFACTOR=1
##FIRSTX=${axis[0]}
##LASTX=${axis.at(-1)}
##DELTAX=${axis[1] - axis[0]}
##NPOINTS=${axis.length}
##XYDATA=(X++(Y..Y))
`;
  return header + axis.map((v, i) => `${v} ${values[i]}`).join('\n') + '\n##END=\n';
}

const spectrum = affn(x, y);
await save('proton.jdx', spectrum, '257-point descending ppm spectrum; rounded Gaussian peaks at 2.03125 and 7 ppm');
await save('ascending.jdx', affn([...x].reverse(), [...y].reverse()), 'Same sampled spectrum, ascending axis');
await save('carbon.jdx', affn(x, y, '13C'), 'Same synthetic ordinates, explicitly carbon; not proton evidence');
const peakTable = `##TITLE=Synthetic companion peaks
##JCAMP-DX=5.00
##DATA TYPE=NMR PEAK TABLE
##BLOCK_ID=2
##CROSS REFERENCE=BLOCK_ID=1
##.OBSERVE NUCLEUS=^1H
##XUNITS=PPM
##YUNITS=ARBITRARY UNITS
##NPOINTS=2
##PEAK TABLE=(XYW..XYW)
2.03125,10000,0.12
7,6000,0.18
##END=
`;
await save('peaks.jdx', peakTable, 'Two authored peak positions, heights and width values; no integrated areas or assignments');
const block = spectrum.replace('##JCAMP-DX=5.00', '##JCAMP-DX=5.00\n##BLOCK_ID=1');
await save('linked.jdx', '##TITLE=Spectrum with companion table\n##JCAMP-DX=5.00\n##DATA TYPE=LINK\n##BLOCKS=2\n'
  + block + peakTable + '##END=\n', 'LINK container of proton.jdx (block 1) and peaks.jdx (block 2)');

for (const encoding of ['FIX', 'SQZ', 'DIF', 'DIFDUP', 'PAC']) {
  const bytes = from1DNMRVariables({ x: { data: x, label: 'X' }, r: { data: y, label: 'R' } }, {
    xyEncoding: encoding, factor: { r: 1 }, nmrInfo: { isFid: false, nucleus: '1H',
      originFrequency: 400, title: `Synthetic ${encoding}`, dataType: 'NMR SPECTRUM',
      owner: 'secs-repro contributors; AGPL-3.0-only' },
  });
  await save(`encoded-${encoding.toLowerCase()}.jdx`, bytes, `Same analytic signal exported by convert-to-jcamp 7.0.1 as ${encoding}, Hz axis`);
}
const complex = from1DNMRVariables({ x: { data: x, label: 'X' },
  r: { data: y, label: 'R' }, i: { data: y.map(() => 0), label: 'I' } }, {
  xyEncoding: 'DIFDUP', factor: { r: 1, i: 1 }, nmrInfo: { isFid: false, nucleus: '1H',
    originFrequency: 400, title: 'Synthetic complex spectrum', dataType: 'NMR SPECTRUM' },
});
await save('complex.jdx', complex, 'NTUPLES real/imaginary spectrum; imaginary channel zero, not a second experiment');
const t = Array.from({ length: 512 }, (_, i) => i / 4800);
const fid = from1DNMRVariables({ x: { data: t, label: 'X', units: 'SECONDS' },
  r: { data: t.map((v) => Math.exp(-v * 30) * Math.cos(2 * Math.PI * 800 * v)), label: 'R' },
  i: { data: t.map((v) => Math.exp(-v * 30) * Math.sin(2 * Math.PI * 800 * v)), label: 'I' } }, {
  xyEncoding: 'DIFDUP', nmrInfo: { isFid: true, nucleus: '1H', originFrequency: 400,
    baseFrequency: 400, spectralWidth: 12, frequencyOffset: 1600,
    title: 'Synthetic decaying complex tone', dataType: 'NMR FID' },
});
await save('fid.jdx', fid, '512 complex samples exp(-30t) exp(i 2pi 800t), dwell 1/4800 second; synthetic FID');

const mol = `Ethanol
  secs-repro
Synthetic fixture; AGPL-3.0-only
  3  2  0  0  0  0            999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.5000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    3.0000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  2  3  1  0  0  0  0
M  END
`;
await save('ethanol.mol', mol, 'Authored ethanol connection table; formula C2H6O is structural evidence, not spectrum identity');
await save('ethanol.sdf', mol + '$$$$\n', 'Single-record SDF wrapper of ethanol.mol');

const core = new NMRiumCore();
core.registerPlugins(plugins.recommended(core));
const collection = new FileCollection();
await collection.appendFileList([new File([spectrum], 'proton.jdx'), new File([affn(x, y, '13C')], 'carbon.jdx')]);
const loadedState = await core.read(collection, { onLoadProcessing: { autoProcessing: false } });
loadedState.state.data.spectra.forEach((item, index) => { item.id = `synthetic-${index}`; });
await save('mixed.nmrium', JSON.stringify(core.serializeNmriumState(loadedState.state)) + '\n',
  'NMRium serialization of authored proton/carbon spectra; data and processing preserved by reference core');

for (const record of [...files].filter((item) => item.path.endsWith('.jdx') && item.path !== 'peaks.jdx' && item.path !== 'carbon.jdx')) {
  const bytes = await readFile(`/output/${record.path}`);
  const loaded = await readSpectrum([new File([bytes], record.path)]);
  if (!loaded) throw Error(`Cannot generate a reference for ${record.path}: the loader returned no spectrum`);
  if (loaded.isFid) throw Error(`Cannot generate a reference for ${record.path}: processing left the data in the time domain`);
  if (loaded.meta.nucleus !== '1H') throw Error(`Cannot generate a proton reference for ${record.path}: the loader reported nucleus ${JSON.stringify(loaded.meta.nucleus)}`);
  const normalized = normalizeSpectrum(loaded.data);
  const reference = { frontend_revision: revision, input_sha256: record.sha256,
    nucleus: loaded.meta.nucleus, dimension: loaded.dimension, points: loaded.data.y.length,
    from_fid: loaded.fromFid, magnitude: loaded.magnitude,
    first_ppm: loaded.data.x[0], last_ppm: loaded.data.x.at(-1),
    intensities: Array.from(normalized.spectrum.y) };
  await save(`${record.path}.reference.json`, JSON.stringify(reference) + '\n',
    `Pinned reference normalization of ${record.path}`, null, record.path);
}
await writeFile('/output/provenance.json', JSON.stringify({ frontend_revision: revision,
  reference_sources: referenceSources, processing: { autoProcessing: true, normalization: 'normalizeSpectrum', float32_cast: false },
  generator_sha256: hash(await readFile('/generator.mjs')), exporter: `${exporter.name}@${exporter.version} (${exporter.license})`, files }, null, 2) + '\n');
