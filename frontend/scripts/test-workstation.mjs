import { build } from 'esbuild';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

const dir = await mkdtemp(join(tmpdir(), 'tracelens-workstation-'));
try {
  const outfile = join(dir, 'tests.mjs');
  // jsx: 'automatic' 与应用的 tsconfig.app.json 对齐；少了它，组件里的 JSX 会被编译成
  // React.createElement，而这个测试没有全局 React，直接 ReferenceError。
  await build({ absWorkingDir: fileURLToPath(new URL('../', import.meta.url)), define: {'import.meta.env':'{}'}, jsx: 'automatic', entryPoints: ['tests/workstation.test.ts'], outfile, bundle: true, platform: 'node', format: 'esm' });
  await import(pathToFileURL(outfile).href);
} finally {
  await rm(dir, { recursive: true, force: true });
}
