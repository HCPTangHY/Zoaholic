import test from 'node:test';
import assert from 'node:assert/strict';
import { keyTreeRows, ownKeyFields, retainKeyOverrides } from '../src/lib/keyHierarchy.mjs';

test('tree preserves source indices, follows parent and collapses only descendants', () => {
  const keys = [{id: 'child', parent_id: 'root'}, {id: 'root'}, {id: 'other'}, {id: 'grandchild', parent_id: 'child'}];
  assert.deepEqual(keyTreeRows(keys).map(row => [row.index, row.depth]), [[1, 0], [0, 1], [3, 2], [2, 0]]);
  assert.deepEqual(keyTreeRows(keys, new Set(['root'])).map(row => row.index), [1, 2]);
});

test('new child does not materialize inherited form defaults', () => {
  const result = retainKeyOverrides({api: 'new', parent_id: 'root', groups: ['default'], model: [], ip_blacklist: [], preferences: {quota: {}, enabled_plugins: []}}, new Set());
  assert.deepEqual(result, {api: 'new', parent_id: 'root'});
});

test('explicit empty and falsy values survive and resetting removes overrides', () => {
  const key = {api: 'child', id: 'child', parent_id: 'root', role: 'user', model: [], preferences: {cooldown: 0, active: false}};
  const fields = ownKeyFields(key);
  assert.deepEqual(retainKeyOverrides(key, fields), key);
  fields.delete('preferences.cooldown');
  assert.deepEqual(retainKeyOverrides(key, fields).preferences, {active: false});
  assert.equal(fields.has('role'), false);
});

test('starting an override copies effective values without mutating its parent', async () => {
  const { inheritedKeyValue, splitKeyQuota } = await import('../src/lib/keyHierarchy.mjs');
  const parent = { model: ['gpt-a'], groups: [], preferences: { timeout: 0, enabled: false, quota: { token: '100/day', 'gpt-a': '2/min' }, quota_rules: [{ id: 'r', limit: '3/day' }] } };
  const models = inheritedKeyValue(parent, 'model');
  models.push('gpt-b');
  assert.deepEqual(parent.model, ['gpt-a']);
  assert.deepEqual(inheritedKeyValue(parent, 'groups'), []);
  assert.equal(inheritedKeyValue(parent, 'preferences.timeout'), 0);
  assert.equal(inheritedKeyValue(parent, 'preferences.enabled'), false);
  const rules = inheritedKeyValue(parent, 'preferences.quota_rules');
  rules[0].limit = '5/day';
  assert.equal(parent.preferences.quota_rules[0].limit, '3/day');
  assert.deepEqual(splitKeyQuota(parent.preferences.quota), { rules: [{key: 'token', value: '100/day'}], models: [{key: 'gpt-a', value: '2/min'}] });
});

test('legacy parent quota initializes the ordinary quota editor and empty override stays empty', async () => {
  const { inheritedKeyValue } = await import('../src/lib/keyHierarchy.mjs');
  assert.deepEqual(inheritedKeyValue({ preferences: { rate_limit: '5/min', credits: 0 } }, 'preferences.quota'), {default: '5/min', cost: '0/inf'});
  assert.deepEqual(inheritedKeyValue({ preferences: { quota: {}, rate_limit: '5/min' } }, 'preferences.quota'), {});
});
