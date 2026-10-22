import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：OAuth 账号删除会立即删除后端 refresh_token，误触后无法恢复。
// 修改方式：通过源码回归测试锁定 deleteKey 在 DELETE /v1/oauth/accounts 前必须先执行 window.confirm，并且取消确认时直接返回。
// 目的：防止后续维护把二次确认移除，导致管理员误删 OAuth token。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(__dirname, '../src/pages/Channels.tsx'), 'utf8');

function sliceBetween(startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

const deleteKeyBlock = sliceBetween('const deleteKey = async', 'const handleKeyPaste');

assert.match(
  deleteKeyBlock,
  /window\.confirm\([\s\S]*OAuth 账号 \$\{keyValue\}[\s\S]*不可逆[\s\S]*token 将无法恢复[\s\S]*重新导入[\s\S]*\)/,
  'OAuth 账号删除前应该展示包含账号、不可逆、token 无法恢复和重新导入提示的确认框',
);
assert.match(
  deleteKeyBlock,
  /if \(!confirmOAuthDelete\) return;[\s\S]*apiFetch\(`\/v1\/oauth\/accounts\/\$\{encodeURIComponent\(keyValue\)\}\?provider=\$\{encodeURIComponent\(providerName\)\}`,[\s\S]*method: 'DELETE'/,
  '用户取消确认时应该直接返回，只有确认后才调用 OAuth 删除接口',
);
assert.match(
  deleteKeyBlock,
  /修改原因：OAuth 账号删除会立即调用后端清除 refresh_token/,
  '确认逻辑旁应该保留说明删除风险的修改注释',
);

console.log('oauth delete confirmation regression passed');
process.exit(0);
