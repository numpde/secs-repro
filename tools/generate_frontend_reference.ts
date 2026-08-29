import { readdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { parseDroppedFiles } from '/opt/frontend/src/spectrum/parseFiles.ts';

interface Arguments {
  frontendRevision: string;
  input: string;
  output: string;
  pathPrefix: string;
}

async function main(): Promise<void> {
  const args = parseArguments(process.argv.slice(2));
  const files = await readUpload(args.input, args.pathPrefix);
  const parsed = await parseDroppedFiles(files);

  if (parsed.spectrum === null || parsed.errors.length > 0) {
    throw new Error(
      `The pinned frontend could not produce a spectrum: ${parsed.errors.join(' ')}`,
    );
  }

  const reference = {
    schema: 'secs.frontend-spectrum-reference.v1',
    frontend_revision: args.frontendRevision,
    upload_path: args.pathPrefix,
    grid: {
      from_ppm: -2,
      to_ppm: 10,
      points: 10_000,
      order: 'ascending',
      intensity_scaling: 'min-max-0-1',
      dtype: 'float64',
    },
    intensities: Array.from(parsed.spectrum.spectrum.y),
  };

  await writeFile(args.output, `${JSON.stringify(reference, null, 2)}\n`);
}

async function readUpload(root: string, pathPrefix: string): Promise<File[]> {
  const relativePaths = await listFiles(root);
  const files: File[] = [];

  for (const relativePath of relativePaths) {
    const contents = await readFile(path.join(root, relativePath));
    const file = new File([contents], path.basename(relativePath), {
      lastModified: 0,
    });
    Object.defineProperty(file, 'path', {
      value: path.posix.join(pathPrefix, relativePath),
    });
    files.push(file);
  }

  return files;
}

async function listFiles(root: string, relative = ''): Promise<string[]> {
  const entries = await readdir(path.join(root, relative), {
    withFileTypes: true,
  });
  const files: string[] = [];

  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    const child = path.join(relative, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await listFiles(root, child)));
    } else if (entry.isFile()) {
      files.push(child);
    }
  }

  return files;
}

function parseArguments(argv: string[]): Arguments {
  const values = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (name === undefined || value === undefined || !name.startsWith('--')) {
      throw new Error('Expected --name value arguments.');
    }
    values.set(name, value);
  }

  return {
    frontendRevision: required(values, '--frontend-revision'),
    input: required(values, '--input'),
    output: required(values, '--output'),
    pathPrefix: required(values, '--path-prefix'),
  };
}

function required(values: Map<string, string>, name: string): string {
  const value = values.get(name);
  if (value === undefined || value.length === 0) {
    throw new Error(`Missing ${name}.`);
  }
  return value;
}

await main();
