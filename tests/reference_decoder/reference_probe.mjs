import { readFile } from 'node:fs/promises';

import { convert } from '/opt/frontend/node_modules/jcampconverter/dist/jcampconverter.js';

const protocol = 'secs.reference-decoder-spike.v1';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
const sources = validateSources(request.sources);

if (request.operation === 'inventory') {
  const representations = [];
  for (const source of sources) {
    representations.push(...await inventorySource(source));
  }
  process.stdout.write(JSON.stringify({ protocol, operation: 'inventory', representations }));
} else if (request.operation === 'prepare') {
  const locator = decodeIdentity(request.representation_id);
  const source = sources.find((item) => item.id === locator.source);
  if (source === undefined) throw new Error('The selected representation source is absent');
  if (request.preparation !== 'as_stored') {
    throw new Error('This spike prepares processed JCAMP spectra only as stored');
  }
  const entries = decodeJcamp(await readFile(source.path)).flatten;
  const entry = entries[locator.entry];
  const spectrum = entry?.spectra?.[0];
  if (kindOf(entry) !== 'spectrum' || entry.twoD === true || spectrum === undefined) {
    throw new Error('The selected representation is not a processed one-dimensional spectrum');
  }
  process.stdout.write(JSON.stringify({
    protocol,
    operation: 'prepare',
    representation_id: request.representation_id,
    points: spectrum.data.y.length,
    axis: spectrum.data.x,
    intensities: spectrum.data.y,
  }));
} else {
  throw new Error('The spike operation must be inventory or prepare');
}

async function inventorySource(source) {
  const entries = decodeJcamp(await readFile(source.path)).flatten;
  const items = [];
  const byBlock = new Map();
  for (const [entryIndex, entry] of entries.entries()) {
    const kind = kindOf(entry);
    if (kind === null) continue;
    const dimension = entry.twoD === true ? 2 : 1;
    const shape = shapeOf(entry);
    const id = encodeIdentity({ source: source.id, entry: entryIndex });
    const item = {
      id,
      kind,
      sources: [source.id],
      nucleus: nucleusOf(entry),
      dimension,
      points: shape.reduce((product, length) => product * length, 1),
      shape,
      preparation_options: kind === 'spectrum' && dimension === 1 ? ['as_stored'] : [],
      related_ids: [],
    };
    items.push({ item, crossReference: entry.info?.CROSSREFERENCE });
    const block = entry.info?.BLOCKID;
    if (block !== undefined) byBlock.set(String(block), id);
  }
  for (const { item, crossReference } of items) {
    const target = referencedBlock(crossReference);
    if (target !== null && byBlock.has(target)) {
      item.related_ids.push(byBlock.get(target));
    }
  }
  return items.map(({ item }) => item);
}

function decodeJcamp(bytes) {
  return convert(bytes, {
    canonicDataLabels: true,
    canonicMetadataLabels: true,
    dynamicTyping: true,
    keepRecordsRegExp: /.*/,
    keepSpectra: true,
  });
}

function kindOf(entry) {
  const type = String(entry?.dataType ?? '').replaceAll(' ', '').toUpperCase();
  if (type.includes('PEAKTABLE')) return 'peak_table';
  if (type.includes('NMRFID')) return 'fid';
  if (type.includes('NMRSPECTRUM')) return 'spectrum';
  return null;
}

function nucleusOf(entry) {
  if (entry.twoD === true) {
    return entry.ntuples
      .filter((variable) => variable.vartype === 'INDEPENDENT')
      .map((variable) => String(variable.nucleus).replaceAll('^', '').replace(/^<|>$/g, ''));
  }
  const value = entry?.xType ?? entry?.info?.['.OBSERVENUCLEUS'] ?? '';
  return String(value).replaceAll('^', '').replace(/^<|>$/g, '');
}

function shapeOf(entry) {
  const spectrum = entry.spectra?.[0];
  const count = spectrum?.nbPoints ?? spectrum?.data?.x?.length ?? entry.info?.NPOINTS;
  if (!Number.isSafeInteger(count) || count < 0) {
    throw new Error('The pinned decoder did not report a valid point count');
  }
  return entry.twoD === true ? [count, entry.spectra.length] : [count];
}

function referencedBlock(value) {
  const match = /BLOCK_ID\s*=\s*([^,;\s]+)/i.exec(String(value ?? ''));
  return match?.[1] ?? null;
}

function encodeIdentity(locator) {
  return `spike-jcamp-v1.${Buffer.from(JSON.stringify(locator)).toString('base64url')}`;
}

function decodeIdentity(identity) {
  if (typeof identity !== 'string' || !identity.startsWith('spike-jcamp-v1.')) {
    throw new Error('The representation identity was not issued by this spike');
  }
  const locator = JSON.parse(
    Buffer.from(identity.slice('spike-jcamp-v1.'.length), 'base64url').toString(),
  );
  if (
    typeof locator.source !== 'string'
    || !Number.isSafeInteger(locator.entry)
  ) {
    throw new Error('The representation identity is malformed');
  }
  return locator;
}

function validateSources(value) {
  if (
    !Array.isArray(value)
    || value.length === 0
    || value.some((item) =>
      item === null
      || typeof item !== 'object'
      || Object.keys(item).sort().join(',') !== 'id,path'
      || typeof item.id !== 'string'
      || item.id.length === 0
      || typeof item.path !== 'string'
      || item.path.length === 0)
  ) {
    throw new Error('The spike requires nonempty source id/path records');
  }
  if (new Set(value.map((item) => item.id)).size !== value.length) {
    throw new Error('The spike source identities must be unique');
  }
  return value;
}
