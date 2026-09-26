// SPDX-License-Identifier: AGPL-3.0-only
// Generate resource-backed NMRium state without contacting an external service.
import { createHash } from 'node:crypto';
import { NMRiumCore } from '/opt/frontend/node_modules/@zakodium/nmrium-core/dist/nmrium-core.js';
import * as plugins from '/opt/frontend/node_modules/@zakodium/nmrium-core-plugins/dist/nmrium-core-plugins.js';
import { ZipReader, Uint8ArrayReader, Uint8ArrayWriter, ZipWriter } from '/opt/frontend/node_modules/@zip.js/zip.js/index.js';
import { normalizeSpectrum } from '/opt/frontend/src/spectrum/normalize.ts';

const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const json = (value) => JSON.stringify(value, (_, item) => ArrayBuffer.isView(item) ? Array.from(item) : item, 2) + '\n';
const origin = { generator: 'tools/generate_nmrium_fixtures.mjs', author: 'secs-repro contributors',
  licence: 'AGPL-3.0-only', licence_text: 'LICENSE',
  basis: 'Authored proton.jdx, serialized through the pinned core with a stored +1 ppm shift' };

async function canonicalArchive(serialized) {
  // Normalize generated IDs and timestamps for repeatable bytes, preserving
  // resource-path bindings and the leading, uncompressed .mimetype member.
  const reader = new ZipReader(new Uint8ArrayReader(serialized));
  const members = new Map();
  try {
    for (const entry of await reader.getEntries()) members.set(entry.filename, await entry.getData(new Uint8ArrayWriter()));
  } finally {
    await reader.close();
  }
  const index = JSON.parse(new TextDecoder().decode(members.get('index.json')));
  const sources = index.sources.toSorted((left, right) => index.paths[left.uuid].localeCompare(index.paths[right.uuid], 'en'));
  const paths = {};
  sources.forEach((source, i) => {
    const path = index.paths[source.uuid];
    source.uuid = `00000000-0000-4000-8000-${String(i + 1).padStart(12, '0')}`;
    paths[source.uuid] = path;
    if ('lastModified' in source) source.lastModified = 315532800000;
  });
  index.sources = sources;
  index.paths = paths;
  members.set('index.json', new TextEncoder().encode(json(index)));
  const names = ['.mimetype', ...Array.from(members.keys()).filter((name) => name !== '.mimetype').sort()];
  const writer = new ZipWriter(new Uint8ArrayWriter());
  for (const name of names) {
    await writer.add(name, new Uint8ArrayReader(members.get(name)), {
      level: 0, compressionMethod: 0, lastModDate: new Date('1980-01-01T00:00:00Z'), extendedTimestamp: false,
    });
  }
  return { bytes: await writer.close(), members };
}

function shiftedSpectrum(result, originalValues, label) {
  const spectra = result.state.data.spectra;
  const observed = spectra.map(({ info, data, selector }) => ({ nucleus: info.nucleus,
    dimension: info.dimension, points: data.re.length, bounds: [data.x[0], data.x.at(-1)], selector }));
  if (spectra.length !== 1 || spectra[0].info.dimension !== 1 || spectra[0].info.nucleus !== '1H'
      || spectra[0].data.x[0] !== -1 || spectra[0].data.x.at(-1) !== 11
      || spectra[0].data.re.length !== 257
      || JSON.stringify(Array.from(spectra[0].data.re)) !== JSON.stringify(originalValues)) {
    throw Error(`Cannot admit ${label}: expected 257 unchanged proton intensities and one +1 ppm shift; observed ${JSON.stringify(observed)}`);
  }
  return spectra[0];
}

async function saveReference(save, referenceLock, referenceBuild, name, bytes, spectrum, entrypoint) {
  const normalized = normalizeSpectrum({ x: spectrum.data.x, y: spectrum.data.re });
  const reference = { reference_lock: referenceLock, reference_build: referenceBuild,
    input_sha256: hash(bytes), nucleus: spectrum.info.nucleus, dimension: spectrum.info.dimension,
    points: spectrum.data.re.length, first_ppm: spectrum.data.x[0], last_ppm: spectrum.data.x.at(-1),
    entrypoint, auto_processing: false, normalization: 'normalizeSpectrum', float32_cast: false,
    stored_processings: spectrum.processings,
    intensities: Array.from(normalized.spectrum.y) };
  await save(`${name}.reference.json`, json(reference), `Pinned ${entrypoint} then normalization of stored processing`,
    { ...origin, basis: `Reference decoding and normalization of ${name}` }, name);
}

/** Emit states and reference artifacts through save. Run serially: this replaces
 * process-global fetch while constructing the authored resource, then restores
 * it in finally, including when decoding or publication to staging fails. */
export async function generateNmriumFixtures({ protonBytes, referenceLock, referenceBuild, save }) {
  const core = new NMRiumCore();
  core.registerPlugins(plugins.recommended(core, [plugins.spectrum1DProcessings(),
    plugins.filtersToProcessingsMigrator(), plugins.autoProcessingsPipeline()]));
  const previousFetch = globalThis.fetch;
  try {
    // The logical web source is authored here. Only these exact in-memory bytes
    // are available during serialization; archive admission denies every fetch.
    globalThis.fetch = async (request) => {
      if (String(request) !== 'https://fixture.invalid/proton.jdx') throw Error('Fixture generation requested an unprovided resource');
      return new Response(protonBytes);
    };
    const result = await core.readFromWebSource({ baseURL: 'https://fixture.invalid/',
      entries: [{ relativePath: 'proton.jdx' }] }, { selectorRoot: 'authored-proton', onLoadProcessing: { autoProcessing: false } });
    if (result.state.data.spectra.length !== 1) throw Error('Cannot generate NMRium resources: the authored proton source was not decoded');
    const spectrum = result.state.data.spectra[0];
    spectrum.id = 'authored-spectrum';
    const originalValues = Array.from(spectrum.data.re);
    const wrapper = json(core.serializeNmriumState(result.state, { includeData: 'dataSource' }));
    await save('resource-wrapper.nmrium', wrapper, 'Genuine URL-backed state with no embedded arrays; not satisfied by an arbitrary sibling filename',
      { ...origin, basis: 'Serialized dataSource state from authored proton.jdx; no shift applied; URL supplied by an in-memory fixture stub' }, 'proton.jdx');
    spectrum.processings = [{ uid: 'authored-shift',
      operatorId: '@zakodium/nmrium-core-plugins#shiftX1D', settings: 1, enabled: true }];
    result.state.data.spectra = [await core.processSpectrum(spectrum)];
    const stored = json(core.serializeNmriumState(result.state, { includeData: 'rawData' }));
    await save('stored-shift.nmrium', stored, 'Raw proton data plus an enabled +1 ppm shift, to be applied exactly once', origin, 'proton.jdx');
    const serialized = await core.serializeNmriumArchive({ state: result.state, aggregator: result.aggregator,
      externalData: 'embedded', includeData: true });
    const archive = await canonicalArchive(serialized);
    const members = Array.from(archive.members, ([path, bytes]) => ({ path, sha256: hash(bytes),
      parent: path === 'data/authored-proton/proton.jdx' ? 'proton.jdx' : 'stored-shift.nmrium',
      origin: { ...origin, basis: path === 'data/authored-proton/proton.jdx'
        ? 'Unchanged embedded proton.jdx' : 'Core-generated package metadata; UUIDs and ZIP timestamps canonicalized' } }));
    await save('resource-embedded.nmrium.zip', archive.bytes,
      'Native NMRium archive with a stored +1 ppm shift and its actual embedded resource; canonical UUIDs and 1980 ZIP timestamps',
      origin, 'proton.jdx', members);
    globalThis.fetch = async () => { throw Error('NMRium fixture admission attempted a network fetch'); };
    const restored = shiftedSpectrum(await core.readNMRiumObject(JSON.parse(stored),
      { onLoadProcessing: { autoProcessing: false } }), originalValues, 'stored-shift.nmrium');
    await saveReference(save, referenceLock, referenceBuild,
      'stored-shift.nmrium', stored, restored, 'readNMRiumObject');
    const embedded = shiftedSpectrum(await core.readNMRiumArchive(archive.bytes,
      { onLoadProcessing: { autoProcessing: false } }), originalValues, 'resource-embedded.nmrium.zip');
    if (embedded.selector?.root !== 'authored-proton'
        || JSON.stringify(embedded.selector?.files) !== JSON.stringify(['proton.jdx'])) {
      throw Error(`Cannot admit the NMRium archive: embedded resource identity changed; observed ${JSON.stringify(embedded.selector)}`);
    }
    await saveReference(save, referenceLock, referenceBuild,
      'resource-embedded.nmrium.zip', archive.bytes, embedded, 'readNMRiumArchive');
  } finally {
    globalThis.fetch = previousFetch;
  }
}
