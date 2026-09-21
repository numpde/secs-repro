// SPDX-License-Identifier: AGPL-3.0-only
// Explicit fixture refresh in the pinned reference image; never run by tests.
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { from1DNMRVariables, from2DNMRVariables } from '/opt/frontend/node_modules/convert-to-jcamp/lib/index.js';
import { readSpectrum } from '/opt/frontend/src/spectrum/readSpectrum.ts';
import { normalizeSpectrum } from '/opt/frontend/src/spectrum/normalize.ts';
import { NMRiumCore } from '/opt/frontend/node_modules/@zakodium/nmrium-core/dist/nmrium-core.js';
import * as plugins from '/opt/frontend/node_modules/@zakodium/nmrium-core-plugins/dist/nmrium-core-plugins.js';
import { FileCollection } from '/opt/frontend/node_modules/file-collection/lib/index.js';

const revision = '5ab78f61e9fb679f3f0b9823be5217ae250e213f';
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const referenceSources = {
  'src/spectrum/__tests__/data/4-chlorobenzylamine.jdx': '9852cd6b851f83ab0c7e372ec9d800b72b0cd5aa54bec32e4e91fc92efcd11ff',
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

await save('upstream-4-chlorobenzylamine.jdx',
  await readFile('/opt/frontend/src/spectrum/__tests__/data/4-chlorobenzylamine.jdx'),
  'Unchanged upstream synthetic 1H fixture; retains its embedded public-domain declaration', {
    generator: 'tools/generate_input_fixtures.mjs', author: null,
    source_repository: 'numpde/fork-of-elucidation.cheminfo.org', source_revision: revision,
    source_path: 'src/spectrum/__tests__/data/4-chlorobenzylamine.jdx',
    origin_declaration: 'elucidation.cheminfo.org test fixture',
    licence: 'Public domain, as declared in the fixture',
    licence_text: 'upstream-4-chlorobenzylamine.jdx: ##OWNER=public domain',
    basis: 'Copied byte-for-byte; individual creator is not stated in the fixture',
  });

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
const alternate = x.map((v) => Math.round(10000 * Math.exp(-(((v - 4) / 0.18) ** 2))
  + 3000 * Math.exp(-(((v - 8) / 0.12) ** 2))));
await save('alternate.jdx', affn(x, alternate), 'Distinct proton signal: rounded samples of Gaussians centered at 4 and 8 ppm with amplitudes 10000 and 3000');
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

const direct = Array.from({ length: 8 }, (_, i) => i);
const indirect = Array.from({ length: 4 }, (_, i) => 2 * i);
const matrix = indirect.map((_, row) => direct.map((_, column) => 10 * row + column + 1));
let twoDimensional = from2DNMRVariables({
  x: { data: direct, label: 'Direct [ppm]', symbol: 'F2', units: 'HZ', isDependent: false },
  y: { data: indirect, label: 'Indirect [ppm]', symbol: 'F1', units: 'HZ', isDependent: false },
  z: { data: matrix, label: 'Intensity', symbol: 'Y', units: 'ARBITRARY UNITS', isDependent: true },
}, {
  nmrInfo: { title: 'Authored 4 by 8 matrix', dataType: 'nD NMR SPECTRUM',
    owner: 'secs-repro contributors; AGPL-3.0-only', origin: 'Synthetic test data' },
  meta: { SFO1: 400, SFO2: 400, NUC1: '1H', NUC2: '1H' },
  factor: { x: 1, y: 1, z: 1 }, xyEncoding: 'FIX',
});
// The exporter frequency-scales ppm axes. Its redundant scalar vendor nucleus
// records make the pinned core misclassify this actual matrix as 1D.
const vendorNuclei = twoDimensional.match(/^##\$NUC[12]=.*$/gm) ?? [];
if (vendorNuclei.length !== 2) throw Error(`Cannot generate the 2D fixture: expected two vendor nucleus records, got ${vendorNuclei.length}`);
twoDimensional = twoDimensional.replace(/^##\$NUC[12]=.*\n/gm, '') + '##END NTUPLES=\n##END=\n';
await save('synthetic-2d.jdx', twoDimensional,
  'Authored 4x8 1H/1H matrix z[row][column]=10*row+column+1; exporter scalar NUC1/NUC2 records removed, NTUPLES/end terminators appended');
const matrixCollection = new FileCollection();
await matrixCollection.appendFileList([new File([twoDimensional], 'synthetic-2d.jdx')]);
const matrixState = await core.read(matrixCollection, { onLoadProcessing: { autoProcessing: false } });
const decodedMatrix = matrixState.state.data.spectra;
const matrixMetadata = decodedMatrix.map(({ info, data }) => ({ dimension: info.dimension,
  nucleus: info.nucleus, shape: info.spectrumSize,
  bounds: data.rr && [data.rr.minX, data.rr.maxX, data.rr.minY, data.rr.maxY] }));
const expectedMatrixMetadata = [{ dimension: 2, nucleus: ['1H', '1H'], shape: [8, 4], bounds: [0, 7, 0, 6] }];
if (JSON.stringify(matrixMetadata) !== JSON.stringify(expectedMatrixMetadata)
    || JSON.stringify(decodedMatrix[0].data.rr.z.map((row) => Array.from(row))) !== JSON.stringify(matrix)) {
  throw Error(`Cannot admit the synthetic 2D fixture: expected the authored 4x8 proton/proton matrix and axes; observed ${JSON.stringify(matrixMetadata)}. Matrix values or metadata differ.`);
}

function brukerParameters(fields) {
  return '##TITLE=Synthetic parameters\n##JCAMPDX=5.00\n##DATATYPE=Parameter Values\n'
    + '##ORIGIN=secs-repro synthetic fixture\n##OWNER=secs-repro contributors; AGPL-3.0-only\n'
    + Object.entries(fields).map(([key, value]) => `##$${key}= ${value}`).join('\n') + '\n##END=\n';
}
const acqus = brukerParameters({ NUC1: '<1H>', SFO1: 400, BF1: 400, SW: 10, SW_h: 4000,
  TD: 128, PARMODE: 0, SOLVENT: '<DMSO>', AQ_mod: 3, BYTORDA: 0, DTYPA: 2,
  GRPDLY: 0, DSPFVS: 20, DECIM: 1, O1: 0 });
const procs = brukerParameters({ SI: 64, SF: 400, SW_p: 4000, OFFSET: 10,
  BYTORDP: 0, DTYPP: 0, PPARMOD: 0, AXNUC: '<1H>', NC_proc: 0 });
const brukerReal = Buffer.alloc(64 * 4);
const brukerFid = Buffer.alloc(64 * 16);
for (let i = 0; i < 64; i++) {
  brukerReal.writeInt32LE(Math.round(10000 * Math.exp(-(((i - 20) / 3) ** 2))), i * 4);
  brukerFid.writeDoubleLE(Math.exp(-i / 16) * Math.cos(2 * Math.PI * i / 8), i * 16);
  brukerFid.writeDoubleLE(Math.exp(-i / 16) * Math.sin(2 * Math.PI * i / 8), i * 16 + 8);
}
await save('bruker-acqus.txt', acqus, 'Authored acquisition parameters for a 64-complex-point 1H FID');
await save('bruker-procs.txt', procs, 'Authored 64-point processed proton parameters; sufficient for the existing 1r/procs reader');
await save('bruker-1r.bin', brukerReal, '64 little-endian int32 samples round(10000 exp(-((i-20)/3)^2))');
await save('bruker-fid.bin', brukerFid, '64 interleaved little-endian complex Float64 samples exp(-i/16) exp(2pi sqrt(-1) i/8)');

const varianParameters = { np: 128, at: .016, sfrq: 400, reffrq: 400, sw: 4000,
  rfl: 2000, rfp: 0, rp: 0, temp: 25, tn: 'H1', solvent: 'DMSO' };
const procpar = Object.entries(varianParameters).map(([name, value]) =>
  `${name} 7 ${typeof value === 'string' ? 2 : 1} 32767 0 0 2 1 0 1 64\n1 ${JSON.stringify(value)}\n0\n`).join('');
const varianFid = Buffer.alloc(32 + 28 + 128 * 4);
// One block, one trace, 128 real/imaginary Float32 values, one block header.
[1, 1, 128, 4, 512, 540].forEach((value, i) => varianFid.writeInt32BE(value, i * 4));
varianFid.writeInt16BE(0, 24);
varianFid.writeInt16BE(201, 26);
varianFid.writeInt32BE(1, 28);
[0, 137, 1, 0].forEach((value, i) => varianFid.writeInt16BE(value, 32 + i * 2));
varianFid.writeInt32BE(1, 40);
for (let i = 0; i < 64; i++) {
  varianFid.writeFloatBE(Math.exp(-i / 16) * Math.cos(2 * Math.PI * i / 8), 60 + i * 8);
  varianFid.writeFloatBE(Math.exp(-i / 16) * Math.sin(2 * Math.PI * i / 8), 64 + i * 8);
}
await save('varian-procpar.txt', procpar, 'Authored Varian procpar for a 64-complex-point 1H FID');
await save('varian-fid.bin', varianFid, 'One-block big-endian Float32 FID; exp(-i/16) exp(2pi sqrt(-1) i/8)');

const jeol = Buffer.alloc(5120);
jeol.write('JEOL.NMR', 0, 'ascii');
// One-dimensional, big-endian, complex Float64 data; axis unit 26 is ppm.
[0, 1, 0, 0, 1, 1, 1, 20].forEach((value, i) => { jeol[8 + i] = value; });
jeol[24] = 3;
jeol[33] = 26;
jeol.write('Synthetic processed proton spectrum', 48, 'ascii');
jeol.writeUInt32BE(64, 176);
jeol.writeUInt32BE(63, 240);
jeol.writeDoubleBE(10, 272);
jeol.write('secs-repro synthetic fixture; AGPL-3.0-only', 552, 'ascii');
jeol.write('1H', 808, 'ascii');
jeol.writeDoubleBE(400, 1064);
jeol[1192] = 1;
jeol.writeUInt32BE(2048, 1212);
jeol.writeUInt32BE(4096, 1284);
jeol.writeUInt32BE(1024, 1292);
const jeolParameters = [
  ['x_domain', '1H', 0], ['field_strength', 9.394637, 31], ['x_freq', 400000000, 13],
  ['x_sweep', 4000, 13], ['x_sweep_clipped', 4000, 13], ['x_offset', 0, 26],
  ['x_acq_time', .016, 28], ['x_points', 64, 25], ['solvent', 'DMSO', 0],
  ['actual_start_time', 1704067200, 28], ['x90', .00001, 28],
  ['relaxation_delay', 1, 28], ['temp_get', 298, 14],
];
// Parameter table: 16-byte header followed by 64-byte records.
const jeolParameterBytes = 16 + 64 * jeolParameters.length;
jeol.writeUInt32BE(jeolParameterBytes, 1216);
[64, 0, jeolParameters.length - 1, jeolParameterBytes].forEach((value, i) => jeol.writeUInt32BE(value, 2048 + i * 4));
jeolParameters.forEach(([name, value, unit], i) => {
  const offset = 2064 + i * 64;
  jeol[offset + 7] = unit;
  if (typeof value === 'string') {
    jeol.write(value.padEnd(16), offset + 16, 'ascii');
  } else {
    jeol.writeDoubleBE(value, offset + 16);
    jeol.writeUInt32BE(2, offset + 32);
  }
  jeol.write(name.padEnd(28), offset + 36, 'ascii');
});
for (let i = 0; i < 64; i++) jeol.writeDoubleBE(Math.exp(-(((i - 20) / 3) ** 2)), 4096 + i * 8);
await save('synthetic.jdf', jeol, 'Authored minimal reference-compatible JEOL processed spectrum: 64 Float64 Gaussian samples plus zero imaginary channel; not instrument qualification');

for (const [format, members, isFid] of [
  ['Bruker processed', [['sample/1/acqus', acqus], ['sample/1/pdata/1/procs', procs], ['sample/1/pdata/1/1r', brukerReal]], false],
  ['Bruker FID', [['sample/1/acqus', acqus], ['sample/1/fid', brukerFid]], true],
  ['Varian FID', [['sample/procpar', procpar], ['sample/fid', varianFid]], true],
  ['JEOL processed', [['sample/synthetic.jdf', jeol]], false],
]) {
  const collection = new FileCollection();
  const files = members.map(([path, bytes]) => {
    const file = new File([bytes], path.split('/').at(-1));
    Object.defineProperty(file, 'webkitRelativePath', { value: path });
    return file;
  });
  await collection.appendFileList(files);
  const result = await core.read(collection, { onLoadProcessing: { autoProcessing: false } });
  const spectra = result.state.data.spectra;
  if (spectra.length !== 1 || spectra[0].info.nucleus !== '1H'
      || spectra[0].data.re.length !== 64 || spectra[0].info.isFid !== isFid) {
    const observed = spectra.map((item) => ({ nucleus: item.info.nucleus,
      points: item.data.re.length, isFid: item.info.isFid }));
    throw Error(`Cannot admit the synthetic ${format} fixture: expected one 64-point proton ${isFid ? 'FID' : 'spectrum'}; observed ${JSON.stringify(observed)}`);
  }
}

for (const record of [...files].filter((item) => item.path.endsWith('.jdx')
  && !['peaks.jdx', 'carbon.jdx', 'synthetic-2d.jdx'].includes(item.path))) {
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
    `Pinned reference normalization of ${record.path}`,
    { ...record.origin, basis: `Derived by pinned reference normalization of ${record.path}` }, record.path);
}
await writeFile('/output/provenance.json', JSON.stringify({ frontend_revision: revision,
  reference_sources: referenceSources, processing: { autoProcessing: true, normalization: 'normalizeSpectrum', float32_cast: false },
  generator_sha256: hash(await readFile('/generator.mjs')), exporter: `${exporter.name}@${exporter.version} (${exporter.license})`, files }, null, 2) + '\n');
