import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const tmp = mkdtempSync(path.join(os.tmpdir(), 'zoaholic-sub-models-'));
let buildSubChannelModelsRequest;
try {
  writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
  execFileSync(process.execPath, [
    path.join(root, 'node_modules/typescript/bin/tsc'),
    '--target', 'ES2020', '--module', 'ES2020', '--moduleResolution', 'Bundler',
    '--rootDir', path.join(root, 'src'), '--outDir', tmp,
    '--noEmit', 'false', '--skipLibCheck', 'true', '--strict', 'true',
    path.join(root, 'src/pages/channels/lib/subChannelModels.ts'),
  ], { cwd: root, stdio: 'pipe' });
  ({ buildSubChannelModelsRequest } = await import(pathToFileURL(path.join(tmp, 'pages/channels/lib/subChannelModels.js')).href));
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

const row = (key, disabled = false) => ({ key, disabled, _clientId: 'fixture' });
const parent = (extra = {}) => ({
  base_url: 'https://gateway.invalid/v1', api_keys: [row('dummy-key')],
  preferences: { headers: { Authorization: 'Bearer dummy-custom' }, enabled_plugins: ['dummy-plugin'], proxy: 'http://proxy.invalid' },
  ...extra,
});
const sub = (extra = {}) => ({ engine: 'gemini', preferences: {}, ...extra });

test('uses child engine but inherits parent URL, key and preferences', () => {
  assert.deepEqual(buildSubChannelModelsRequest(parent(), sub()), {
    engine: 'gemini', base_url: 'https://gateway.invalid/v1', api_key: 'dummy-key',
    preferences: parent().preferences,
  });
});

test('child URL and top-level preference overrides match runtime shallow merge', () => {
  const child = sub({ base_url: 'https://gemini.invalid/v1beta',
    preferences: { headers: { 'X-Child': 'child' }, enabled_plugins: [], temperature: 0 } });
  assert.deepEqual(buildSubChannelModelsRequest(parent(), child), {
    engine: 'gemini', base_url: child.base_url, api_key: 'dummy-key',
    preferences: { headers: { 'X-Child': 'child' }, enabled_plugins: [], proxy: 'http://proxy.invalid', temperature: 0 },
  });
});

test('skips blank and disabled keys without changing key values', () => {
  const p = parent({ api_keys: [row(''), row('  '), row('disabled-key', true), row('first-active'), row('later-active')] });
  assert.equal(buildSubChannelModelsRequest(p, sub()).api_key, 'first-active');
});

test('missing usable URL or key yields no request', () => {
  assert.equal(buildSubChannelModelsRequest(parent({ base_url: '' }), sub()), null);
  assert.equal(buildSubChannelModelsRequest(parent({ api_keys: [] }), sub()), null);
  assert.equal(buildSubChannelModelsRequest(parent({ api_keys: [row('x', true), row(' ')] }), sub()), null);
  assert.equal(buildSubChannelModelsRequest(parent({ base_url: '' }), sub({ base_url: 'https://child.invalid' })).base_url, 'https://child.invalid');
});

test('missing preferences are compatible with old drafts', () => {
  assert.deepEqual(buildSubChannelModelsRequest(parent({ preferences: undefined }), sub({ preferences: undefined })).preferences, {});
});

test('does not mutate frozen parent or child drafts', () => {
  const p = Object.freeze(parent({ api_keys: Object.freeze([Object.freeze(row('dummy-key'))]), preferences: Object.freeze({ parent: true }) }));
  const child = Object.freeze(sub({ preferences: Object.freeze({ child: true }) }));
  const result = buildSubChannelModelsRequest(p, child);
  assert.deepEqual(result.preferences, { parent: true, child: true });
  assert.notEqual(result.preferences, p.preferences);
  assert.notEqual(result.preferences, child.preferences);
});

test('inline model fetch serializes the tested inherited request, not child-only preferences', () => {
  const source = readFileSync(path.join(root, 'src/pages/channels/components/ChannelEditor.tsx'), 'utf8');
  assert.match(source, /const request = buildSubChannelModelsRequest\(formData, sub\)/);
  assert.match(source, /body: JSON\.stringify\(request\)/);
  assert.doesNotMatch(source, /api_key: firstKey\.key, preferences: sub\.preferences/);
});
