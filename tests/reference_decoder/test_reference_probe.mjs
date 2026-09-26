import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

import { normalizeSpectrum } from '/opt/frontend/src/spectrum/normalize.ts';

const probe = '/tests/reference_probe.mjs';
const fixtures = '/fixtures';
const frontendRevision = process.env.FRONTEND_REFERENCE_REVISION;

assert.match(frontendRevision, /^[0-9a-f]{40}$/);

function runProbe(request) {
  return spawnSync(process.execPath, [probe], {
    encoding: 'utf8',
    input: `${JSON.stringify(request)}\n`,
    maxBuffer: 4 * 1024 * 1024,
    timeout: 5_000,
  });
}

function invoke(request) {
  const run = runProbe(request);
  assert.equal(run.status, 0, run.stderr || run.stdout);
  assert.equal(run.stderr, '');
  return JSON.parse(run.stdout);
}

function invokeFailure(request, message) {
  const run = runProbe(request);
  assert.notEqual(run.status, 0);
  assert.match(run.stderr, message);
}

function source(id, name = id) {
  return { id, path: `${fixtures}/${name}` };
}

async function loadReference(name) {
  const [document, bytes] = await Promise.all([
    readFile(`${fixtures}/${name}.reference.json`, 'utf8').then(JSON.parse),
    readFile(`${fixtures}/${name}`),
  ]);
  assert.equal(createHash('sha256').update(bytes).digest('hex'), document.input_sha256);
  assert.equal(document.frontend_revision, frontendRevision);
  return document;
}

function assertNormalizedMatches(name, decoded, reference) {
  const normalized = normalizeSpectrum({
    x: decoded.axis,
    y: decoded.intensities,
  }).spectrum.y;
  assert.equal(normalized.length, reference.intensities.length);
  let maximumDifference = 0;
  for (let index = 0; index < normalized.length; index += 1) {
    maximumDifference = Math.max(
      maximumDifference,
      Math.abs(normalized[index] - reference.intensities[index]),
    );
  }
  assert.ok(
    maximumDifference <= 1e-12,
    `${name} differs from the pinned reference by ${maximumDifference}`,
  );
}

test('candidate inventory projects the supplied processed JCAMP entries without spectral arrays', () => {
  const result = invoke({
    operation: 'inventory',
    sources: [
      source('proton.jdx'),
      source('alternate.jdx'),
      source('carbon.jdx'),
    ],
  });

  assert.equal(result.protocol, 'secs.reference-decoder-spike.v1');
  assert.equal(result.operation, 'inventory');
  assert.equal(result.representations.length, 3);
  assert.equal(new Set(result.representations.map((item) => item.id)).size, 3);
  const bySource = Object.fromEntries(
    result.representations.map((item) => [item.sources[0], item]),
  );
  assert.deepEqual(Object.keys(bySource).sort(), [
    'alternate.jdx',
    'carbon.jdx',
    'proton.jdx',
  ]);
  const expectedNucleus = {
    'proton.jdx': '1H',
    'alternate.jdx': '1H',
    'carbon.jdx': '13C',
  };
  for (const [name, representation] of Object.entries(bySource)) {
    assert.equal(typeof representation.id, 'string');
    assert.ok(representation.id.length > 0);
    assert.equal(representation.kind, 'spectrum');
    assert.deepEqual(representation.sources, [name]);
    assert.equal(representation.nucleus, expectedNucleus[name]);
    assert.equal(representation.dimension, 1);
    assert.equal(representation.points, 257);
    assert.deepEqual(representation.shape, [257]);
    assert.deepEqual(representation.preparation_options, ['as_stored']);
    assert.equal('intensities' in representation, false);
    assert.equal('x' in representation, false);
    assert.equal('y' in representation, false);
  }
});

test('linked inventory preserves distinct entries and replays the selected spectrum', async () => {
  const sources = [source('linked.jdx')];
  const result = invoke({
    operation: 'inventory',
    sources,
  });

  assert.equal(result.representations.length, 2);
  const spectrum = result.representations.find((item) => item.kind === 'spectrum');
  const peakTable = result.representations.find((item) => item.kind === 'peak_table');
  assert.ok(spectrum);
  assert.ok(peakTable);
  assert.notEqual(spectrum.id, peakTable.id);
  assert.deepEqual(spectrum.sources, ['linked.jdx']);
  assert.equal(spectrum.nucleus, '1H');
  assert.equal(spectrum.dimension, 1);
  assert.equal(spectrum.points, 257);
  assert.deepEqual(spectrum.shape, [257]);
  assert.deepEqual(spectrum.preparation_options, ['as_stored']);
  assert.deepEqual(peakTable.sources, ['linked.jdx']);
  assert.equal(peakTable.nucleus, '1H');
  assert.equal(peakTable.dimension, 1);
  assert.equal(peakTable.points, 2);
  assert.deepEqual(peakTable.shape, [2]);
  assert.deepEqual(peakTable.related_ids, [spectrum.id]);
  assert.deepEqual(peakTable.preparation_options, []);

  const prepared = invoke({
    operation: 'prepare',
    sources,
    representation_id: spectrum.id,
    preparation: 'as_stored',
  });
  assertNormalizedMatches('linked.jdx', prepared, await loadReference('linked.jdx'));
  invokeFailure({
    operation: 'prepare',
    sources,
    representation_id: peakTable.id,
    preparation: 'as_stored',
  }, /not a processed one-dimensional spectrum/);
});

test('complex channels and two-dimensional rows remain grouped by JCAMP entry', () => {
  const result = invoke({
    operation: 'inventory',
    sources: [source('complex.jdx'), source('synthetic-2d.jdx')],
  });

  assert.equal(result.representations.length, 2);
  const bySource = Object.fromEntries(
    result.representations.map((item) => [item.sources[0], item]),
  );
  assert.equal(new Set(result.representations.map((item) => item.id)).size, 2);
  assert.equal(bySource['complex.jdx'].kind, 'spectrum');
  assert.deepEqual(bySource['complex.jdx'].sources, ['complex.jdx']);
  assert.equal(bySource['complex.jdx'].dimension, 1);
  assert.equal(bySource['complex.jdx'].nucleus, '1H');
  assert.equal(bySource['complex.jdx'].points, 257);
  assert.deepEqual(bySource['complex.jdx'].shape, [257]);
  assert.deepEqual(bySource['complex.jdx'].preparation_options, ['as_stored']);
  assert.equal(bySource['synthetic-2d.jdx'].kind, 'spectrum');
  assert.deepEqual(bySource['synthetic-2d.jdx'].sources, ['synthetic-2d.jdx']);
  assert.equal(bySource['synthetic-2d.jdx'].dimension, 2);
  assert.equal(bySource['synthetic-2d.jdx'].points, 32);
  assert.deepEqual(bySource['synthetic-2d.jdx'].shape, [8, 4]);
  assert.deepEqual(bySource['synthetic-2d.jdx'].nucleus, ['1H', '1H']);
  assert.deepEqual(bySource['synthetic-2d.jdx'].preparation_options, []);
});

test('selected raw JCAMP data reproduces the pinned frontend normalization', async () => {
  const sources = [source('proton.jdx'), source('alternate.jdx')];
  const inventory = invoke({ operation: 'inventory', sources });
  const bySource = Object.fromEntries(
    inventory.representations.map((item) => [item.sources[0], item]),
  );

  for (const name of ['proton.jdx', 'alternate.jdx']) {
    const prepareSources = sources.map((item) => item.id === name
      ? item
      : { ...item, path: `/unreadable/${item.id}` });
    const result = invoke({
      operation: 'prepare',
      sources: prepareSources,
      representation_id: bySource[name].id,
      preparation: 'as_stored',
    });
    const reference = await loadReference(name);
    assert.equal(result.protocol, 'secs.reference-decoder-spike.v1');
    assert.equal(result.operation, 'prepare');
    assert.equal(result.representation_id, bySource[name].id);
    assert.equal(result.points, 257);
    assert.equal(result.axis.length, 257);
    assert.equal(result.intensities.length, 257);
    assertNormalizedMatches(name, result, reference);
  }
});

test('the grouped complex entry prepares the frontend-selected real channel', async () => {
  const sources = [source('complex.jdx')];
  const inventory = invoke({ operation: 'inventory', sources });
  const selected = inventory.representations[0];
  const result = invoke({
    operation: 'prepare',
    sources,
    representation_id: selected.id,
    preparation: 'as_stored',
  });
  assertNormalizedMatches('complex.jdx', result, await loadReference('complex.jdx'));
});
