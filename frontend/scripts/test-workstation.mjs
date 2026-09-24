import { build } from 'esbuild';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

const dir = await mkdtemp(join(tmpdir(), 'tracelens-workstation-'));
try {
  const outfile = join(dir, 'tests.mjs');
  await build({ absWorkingDir: fileURLToPath(new URL('../', import.meta.url)), define: {'import.meta.env':'{}'}, entryPoints: ['tests/workstation.test.ts'], outfile, bundle: true, platform: 'node', format: 'esm' });
  await import(pathToFileURL(outfile).href);
} finally {
  await rm(dir, { recursive: true, force: true });
}
