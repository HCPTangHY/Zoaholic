import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const utilsSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/utils.ts'), 'utf8');
const fullRowSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/FullKeyRow.tsx'), 'utf8');
const coreSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useChannelsCore.tsx'), 'utf8');

function sliceBetween(source, startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

// Channels.tsx 拆分后，行级 quota 构建逻辑位于 channels/utils.ts。
const buildRowQuota = sliceBetween(utilsSource, 'export function buildRowQuota', 'export function buildRowQuotaSlotData');
assert.match(buildRowQuota, /if \(Array\.isArray\(bal\?\.gauges\) && bal\.gauges\.length > 0\)/, '后端返回 gauges 时应优先使用');
assert.match(buildRowQuota, /const accountQuota = getOAuthQuota\(oauthAccount\);/, 'OAuth 行应从账号读取额度');
assert.match(buildRowQuota, /normalizeQuotaPct|percent: quota\.quota_inner/, 'OAuth 内圈应使用 quota_inner');
assert.match(buildRowQuota, /percent: quota\.quota_outer/, 'OAuth 外圈应使用 quota_outer');
assert.match(buildRowQuota, /id: 'balance', label: '余额'/, '普通余额模式应继续生成余额 gauge 数据');

// 完整 Key 行位于 components/FullKeyRow.tsx，应通过统一 RowQuota 同时支持 OAuth 与普通余额。
assert.match(fullRowSource, /const rowQuota = buildRowQuota\(bal, oauthAccount, isOAuthEngine\);/, 'Key 行应该通过统一 RowQuota 构建 OAuth 与普通 balance 数据');
assert.match(fullRowSource, /const rowQuotaPair = getQuotaPairFromGauges\(rowQuota\.gauges\);/, 'Key 行默认边框应从 gauges 派生 inner 和 outer');
const overlayBlock = sliceBetween(fullRowSource, '{showRowDecorations && rowQuotaPair && (', '{showRowDecorations && slotPayloadAvailable && hasKeyBackgroundSlot');
assert.match(overlayBlock, /<QuotaBorderOverlay quotaInner=\{rowQuotaPair\.quota_inner\} quotaOuter=\{rowQuotaPair\.quota_outer\} \/>/, 'QuotaBorderOverlay 应该接收统一的 quota pair');
assert.match(overlayBlock, /hasKeyBorderSlot[\s\S]*<UiSlot engine=\{formData\.engine\} slot="key_border"/, 'key_border 插槽应替代默认边框');
assert.match(fullRowSource, /oauthAccount\.status === 'active' \? '已连接' : oauthAccount\.status === 'error' \? '刷新失败' : '冷却中'/, 'OAuth Key 行应保留账号状态标识');
assert.match(fullRowSource, /key_status\/re_enable/, '冷却 Key 行应保留恢复按钮');

// 手动余额查询应通过 getQuotaFromSource 归一化旧字段，并用标准 quota 是否存在标记默认双弧可用性。
assert.match(coreSource, /const quotaResult = getQuotaFromSource\(resultForKey\);/, '余额结果应通过 getQuotaFromSource 归一化');
assert.match(coreSource, /_quota_unavailable: !hasQuota/, '缺少标准 quota 时应标记 quota_unavailable');

console.log('channels balance quota rendering regression passed');
process.exit(0);
