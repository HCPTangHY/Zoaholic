import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../src/pages/channels/lib/batchProviders.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
const { applyProviderBatchAction, mergeProviderModels, selectedProviderNames, executeProviderBatch } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

const target = { provider: 'target', api: 'secret', base_url: 'https://target', groups: ['a', 'b'], preferences: { cooldown_period: 3, key_rules: [{ duration: 3 }], api_key_cooldown_period: 8, weight: 7 }, model: ['plain', { upstream: 'old-alias' }], sub_channels: [{ engine: 'claude' }] };
const sourceProvider = { provider: 'source', api: 'do-not-copy', base_url: 'https://source', groups: ['elsewhere'], preferences: { cooldown_period: 0, key_rules: [] }, model: ['plain', { upstream: 'new-alias', extra: 'extra-alias' }, 'new'] };
const before = structuredClone(target);
const copied = applyProviderBatchAction(target, { kind: 'copy', source: sourceProvider, cooling: true, models: 'merge' });
assert.deepEqual(copied.model, ['plain', { upstream: 'old-alias' }, { extra: 'extra-alias' }, 'new']);
assert.deepEqual(copied.preferences, { cooldown_period: 0, key_rules: [], weight: 7 });
for (const field of ['api', 'base_url', 'groups', 'sub_channels']) assert.deepEqual(copied[field], target[field]);
assert.deepEqual(target, before, 'copy must not mutate cached target');
copied.preferences.key_rules.push({ duration: 42 });
assert.deepEqual(sourceProvider.preferences.key_rules, [], 'copy must not retain source references');
assert.deepEqual(applyProviderBatchAction(target, { kind: 'copy', source: { provider: 'empty' }, cooling: false, models: 'replace' }).model, []);
assert.deepEqual(applyProviderBatchAction(target, { kind: 'copy', source: sourceProvider, cooling: false, models: 'none' }), target);
assert.equal(applyProviderBatchAction(target, { kind: 'enabled', enabled: false }).enabled, false);
assert.deepEqual(mergeProviderModels([{ a: 'alias' }], ['a', 'b', 'b']), [{ a: 'alias' }, 'b']);
assert.deepEqual(selectedProviderNames(['a', 'b', 'a'], new Set(['a', 'hidden'])), ['a']);
const writes = [];
const result = await executeProviderBatch(['one', 'one', 'bad-read', 'bad-write', 'renamed', 'last'], { kind: 'enabled', enabled: true }, async name => {
  if (name === 'bad-read') throw new Error('not found');
  return { ...target, provider: name === 'renamed' ? 'other' : name, preferences: { weight: 99 } };
}, async (name, provider) => {
  if (name === 'bad-write') throw new Error('write rejected');
  writes.push(provider);
});
assert.deepEqual(result.succeeded, ['one', 'last']);
assert.deepEqual(result.failed.map(item => item.name), ['bad-read', 'bad-write', 'renamed']);
assert.equal(writes.length, 2);
assert.equal(writes[0].preferences.weight, 99, 'updates must use freshly read configuration');
console.log('batchProviders: all checks passed');
