import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const helperPath = 'src/pages/channels/lib/keyDuplicates.ts';
const editorSource = readFileSync(path.join(frontendRoot, 'src/pages/channels/components/ChannelEditor.tsx'), 'utf8');
const tempDir = mkdtempSync(path.join(os.tmpdir(), 'zoaholic-key-duplicates-'));
let inspectKeyDuplicates;
try {
  writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');
  // Use the installed compiler directly: no npx download or network access.
  execFileSync(process.execPath, [
    path.join(frontendRoot, 'node_modules/typescript/bin/tsc'),
    '--target', 'ES2020', '--module', 'ES2020', '--moduleResolution', 'Bundler',
    '--rootDir', path.join(frontendRoot, 'src'), '--outDir', tempDir,
    '--noEmit', 'false', '--skipLibCheck', 'true', '--strict', 'true',
    path.join(frontendRoot, helperPath),
  ], { cwd: frontendRoot, stdio: 'pipe' });
  ({ inspectKeyDuplicates } = await import(pathToFileURL(path.join(tempDir, 'pages/channels/lib/keyDuplicates.js')).href));
} finally {
  rmSync(tempDir, { recursive: true, force: true });
}

const row = (key, extra = {}) => ({ key, disabled: false, label: '', _clientId: 'test-row', ...extra });
const keys = values => values.map(value => row(value));
const summary = (groups, checkableCount, emptyCount = 0) => ({
  groups: groups.map(rows => ({ rows })),
  duplicateGroupCount: groups.length,
  duplicateRowCount: groups.reduce((count, rows) => count + rows.length, 0),
  checkableCount,
  emptyCount,
});

test('compares trimmed values but never folds case or internal whitespace', () => {
  assert.deepEqual(inspectKeyDuplicates(keys([
    '  Alpha\t', 'Alpha', 'alpha', 'ALPHA', 'a b', 'ab', 'a  b', '\na b\r\n',
  ])), summary([[1, 2], [5, 8]], 8));
});

test('ignores empty values while preserving 1-based original row positions', () => {
  assert.deepEqual(inspectKeyDuplicates(keys(['', 'same', ' \t\r\n', 'same', '\u3000', 'other'])),
    summary([[2, 4]], 3, 3));
  assert.deepEqual(inspectKeyDuplicates(keys(['', '  ', '\t'])), summary([], 0, 3));
  assert.deepEqual(inspectKeyDuplicates([]), summary([], 0));
});

test('disabled rows participate; labels and client identities never affect equality', () => {
  assert.deepEqual(inspectKeyDuplicates([
    row('same', { label: 'different-note-one', _clientId: 'one' }),
    row(' same ', { disabled: true, label: 'different-note-two', _clientId: 'two' }),
    row('disabled-pair', { disabled: true, label: 'shared-note' }),
    row('disabled-pair', { disabled: true, label: 'other-note' }),
    row('unique-one', { label: 'shared-note' }),
    row('unique-two', { label: 'shared-note' }),
    row(' ', { disabled: true }),
  ]), summary([[1, 2], [3, 4]], 6, 1));
});

test('multiple groups retain first-appearance order and count all involved rows', () => {
  assert.deepEqual(inspectKeyDuplicates(keys(['B', 'A', 'C', 'A', 'B', 'A', 'C', 'unique'])),
    summary([[1, 5], [2, 4, 6], [3, 7]], 8));
  assert.deepEqual(inspectKeyDuplicates(keys(['__proto__', 'constructor', '__proto__', 'constructor'])),
    summary([[1, 3], [2, 4]], 4));
});

test('mask-like strings are ordinary literal configuration values, not redacted comparisons', () => {
  assert.deepEqual(inspectKeyDuplicates(keys([
    'sk-***', ' sk-*** ', 'sk-****', 'sk-secret', '••••', '••••', 'oauth-key-id', 'oauth-key-id',
  ])), summary([[1, 2], [5, 6], [7, 8]], 8));
});

test('frozen input rows and array remain untouched, including untrimmed values', () => {
  const input = Object.freeze([
    Object.freeze(row(' secret-value ', { disabled: true, label: 'private-note', _clientId: 'one' })),
    Object.freeze(row('secret-value', { _clientId: 'two' })),
  ]);
  const before = JSON.stringify(input);
  const result = inspectKeyDuplicates(input);
  assert.deepEqual(result, summary([[1, 2]], 2));
  result.groups[0].rows.push(99);
  assert.equal(JSON.stringify(input), before);
  assert.deepEqual(inspectKeyDuplicates(input), summary([[1, 2]], 2));
});

test('returns numeric metadata only, never keys, labels, or client IDs', () => {
  const sensitive = ['fake-private-key-A', 'fake-private-key-B', 'fake-private-label', 'fake-private-client-id'];
  const result = inspectKeyDuplicates([
    row(sensitive[0], { label: sensitive[2], _clientId: sensitive[3] }),
    row(sensitive[0]), row(sensitive[1]), row(sensitive[1]),
  ]);
  assert.deepEqual(result, summary([[1, 2], [3, 4]], 4));
  for (const value of sensitive) assert.equal(JSON.stringify(result).includes(value), false);
  const assertNumericLeaves = value => {
    if (value && typeof value === 'object') Object.values(value).forEach(assertNumericLeaves);
    else assert.equal(typeof value, 'number');
  };
  assertNumericLeaves(result);
  const entry = { key: 'value' };
  for (const field of ['label', 'disabled', '_clientId']) {
    Object.defineProperty(entry, field, { get() { throw new Error(`${field} must not be read`); } });
  }
  assert.deepEqual(inspectKeyDuplicates([entry, entry]), summary([[1, 2]], 2));
});

test('recomputes latest rows after edits, removal, addition, clearing, and reordering', () => {
  const input = keys(['same', 'other', 'same']);
  assert.deepEqual(inspectKeyDuplicates(input), summary([[1, 3]], 3));
  input[2].key = 'other';
  assert.deepEqual(inspectKeyDuplicates(input), summary([[2, 3]], 3));
  input.splice(0, 1);
  assert.deepEqual(inspectKeyDuplicates(input), summary([[1, 2]], 2));
  input.unshift(row('new'));
  assert.deepEqual(inspectKeyDuplicates(input), summary([[2, 3]], 3));
  input.reverse();
  assert.deepEqual(inspectKeyDuplicates(input), summary([[1, 2]], 3));
  input.length = 0;
  assert.deepEqual(inspectKeyDuplicates(input), summary([], 0));
});

test('main-channel custom menu opens a local report without invoking other actions', () => {
  const mainSection = editorSource.indexOf('{!editingSubChannel && <section>');
  const menuStart = editorSource.indexOf('<div ref={keyMoreMenuRef}', mainSection);
  const menuEnd = editorSource.indexOf('{formData.api_keys.length >= 12', menuStart);
  assert.ok(mainSection >= 0 && menuStart > mainSection && menuEnd > menuStart);
  const menu = editorSource.slice(menuStart, menuEnd);
  const button = menu.match(/<button type="button" onClick=\{\(\) => \{([^}]+)\}\}[^>]*><CopyCheck[^>]*\/> 检查重复 Key<\/button>/);
  assert.ok(button, 'duplicate check must be in the existing custom menu');
  assert.equal(button[1].trim(), 'setShowKeyDuplicates(true); setKeyMoreMenuOpen(false);');
  assert.match(menu, /复制全部/);
  assert.match(menu, /清空/);
});

test('report derives from live form state, resets with editor identity, and renders only safe metadata', () => {
  assert.match(editorSource, /import \{ inspectKeyDuplicates \} from '\.\.\/lib\/keyDuplicates'/);
  assert.match(editorSource, /const \[showKeyDuplicates, setShowKeyDuplicates\] = useState\(false\)/);
  assert.match(editorSource, /const keyDuplicateResult = showKeyDuplicates && isModalOpen && !editingSubChannel && formData\s*\? inspectKeyDuplicates\(formData\.api_keys\)\s*: null/);
  assert.doesNotMatch(editorSource, /\[keyDuplicateResult,|setKeyDuplicateResult/);
  assert.match(editorSource, /useEffect\(\(\) => \{\s*setShowKeyDuplicates\(false\);\s*\}, \[isModalOpen, originalIndex, editingSubChannel\?\.parentIdx, editingSubChannel\?\.subIdx\]\)/);
  const panelStart = editorSource.indexOf('{keyDuplicateResult && (');
  const panelEnd = editorSource.indexOf('{hasUiSlot(formData.engine, \'key_hint\'', panelStart);
  assert.ok(panelStart >= 0 && panelEnd > panelStart);
  const panel = editorSource.slice(panelStart, panelEnd);
  assert.match(panel, /onClick=\{\(\) => setShowKeyDuplicates\(false\)\}/);
  for (const field of ['duplicateGroupCount', 'duplicateRowCount', 'checkableCount', 'emptyCount']) {
    assert.ok(panel.includes(`keyDuplicateResult.${field}`));
  }
  assert.match(panel, /group\.rows\.join\('、'\)/);
  assert.match(panel, /未发现重复的非空配置值/);
  assert.match(panel, /当前未保存表单实时检查/);
  assert.match(panel, /行号从 1 开始/);
  assert.match(panel, /不验证有效性/);
  assert.match(panel, /OAuth key_id 只是标识符，不是背后的 token/);
  assert.doesNotMatch(panel, /formData|\.key\b|\.label\b|_clientId|apiFetch|fetch\(|oauthAccounts|openKeyTestDialog|copyAllKeys|handleSave|deleteKey|setFormData/);
});
