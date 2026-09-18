import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { fileURLToPath } from 'node:url';

// 修改原因：Key Rules 保存逻辑新增 retry 三态和 remap 空值清理，必须用纯函数测试避免 UI 回归。
// 修改方式：临时编译 src/lib/keyRules.ts 后直接断言序列化结果。
// 目的：确认保存 payload 只在非默认 retry 和有效 remap 时写入对应字段。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const tempDir = mkdtempSync(path.join(os.tmpdir(), 'zoaholic-key-rules-'));
writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');

execFileSync(process.execPath, [
  path.join(frontendRoot, 'node_modules/typescript/bin/tsc'),
  '--target', 'ES2020',
  '--module', 'ES2020',
  '--moduleResolution', 'Bundler',
  '--rootDir', path.join(frontendRoot, 'src'),
  '--outDir', tempDir,
  '--noEmit', 'false',
  '--skipLibCheck', 'true',
  path.join(frontendRoot, 'src/lib/keyRules.ts'),
], { cwd: frontendRoot, stdio: 'pipe' });

const helpers = await import(pathToFileURL(path.join(tempDir, 'lib/keyRules.js')).href);
const { sanitizeKeyRulesForSave, setKeyRuleRetryMode } = helpers;

assert.equal(typeof sanitizeKeyRulesForSave, 'function', '应该导出 Key Rules 保存清理 helper');
assert.equal(typeof setKeyRuleRetryMode, 'function', '应该导出 retry 三态更新 helper');

const sanitized = sanitizeKeyRulesForSave([
  { match: { status: [429] }, duration: 30, retry: 'default', remap: '' },
  { match: { status: [500] }, duration: 10, retry: true, remap: 503 },
  { match: 'default', duration: 0, retry: false, remap: '502' },
]);

assert.deepEqual(sanitized, [
  { match: { status: [429] }, duration: 30 },
  { match: { status: [500] }, duration: 10, retry: true, remap: 503 },
  { match: 'default', duration: 0, retry: false, remap: 502 },
]);
assert.deepEqual(setKeyRuleRetryMode({ match: 'default', duration: 3, retry: true }, 'default'), { match: 'default', duration: 3 });
assert.deepEqual(setKeyRuleRetryMode({ match: 'default', duration: 3 }, 'force'), { match: 'default', duration: 3, retry: true });
assert.deepEqual(setKeyRuleRetryMode({ match: 'default', duration: 3 }, 'disable'), { match: 'default', duration: 3, retry: false });

console.log('key rules serialization passed');
// 修改原因：当前部署环境的 Node 18 在部分 ESM 脚本自然结束后会触发 Aborted。
// 修改方式：断言全部通过后显式以 0 退出，断言失败时仍会在这里之前抛出错误。
// 目的：让测试退出码只反映 Key Rules 序列化断言是否通过。
process.exit(0);
