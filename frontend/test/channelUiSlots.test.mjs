import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：Channels.tsx 拆分后，ui_slots 相关 helper 位于 channels/utils.ts，
//   UiSlot 组件位于 components/KeyComponents.tsx，完整 Key 行位于 components/FullKeyRow.tsx。
// 修改方式：按新文件位置做源码回归断言。
// 目的：防止后续维护时把渠道专属 quota 计算重新写回通用前端，或破坏 ui_slots 兼容数据。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const utilsSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/utils.ts'), 'utf8');
const fullRowSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/FullKeyRow.tsx'), 'utf8');
const keyComponentsSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/KeyComponents.tsx'), 'utf8');

function sliceBetween(source, startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

const quotaHelper = sliceBetween(utilsSource, 'export function getOAuthQuota', 'export function normalizeOAuthAccountStateMap');
assert.match(quotaHelper, /getQuotaFromSource\(account, account\?\.quota_raw \?\? account\?\.raw\)/, 'getOAuthQuota 应通过通用 getQuotaFromSource 读取标准双额度字段');
assert.doesNotMatch(quotaHelper, /OAUTH_ENGINES|extra_usage|MODEL_PROVIDER_GOOGLE/, '通用 OAuth quota 读取不应包含渠道专属硬编码');

const slotDataBuilder = sliceBetween(utilsSource, 'export function buildRowQuotaSlotData', 'export function withRackCompactBalanceFallback');
assert.match(slotDataBuilder, /if \(bal\) return bal;/, '普通 balance 应优先原样传给 ui_slots 保持兼容');
assert.match(slotDataBuilder, /getOAuthQuota\(oauthAccount\) \?\? getQuotaPairFromGauges\(rowQuota\.gauges\)/, '没有 balance 时应从账号或 gauges 构造兼容 quota 数据');
assert.match(slotDataBuilder, /raw: quota\.raw \?\? oauthAccount\?\.quota_raw \?\? oauthAccount\?\.raw/, '兼容数据应保留 raw 给旧渠道脚本');

const rackFallback = sliceBetween(utilsSource, 'export function withRackCompactBalanceFallback', 'export function sortProvidersByWeight');
assert.match(rackFallback, /getBalanceCompactLabel\(bal\)/, '机房卡片应对旧 amount 余额使用紧凑显示');
assert.match(rackFallback, /displayLabel: compactLabel/, '紧凑显示应替换单 gauge 的 displayLabel');

// 完整 Key 行应把兼容 slot 数据同时传给 key_background 和 quota_display 插槽。
assert.match(fullRowSource, /buildRowQuotaSlotData\(bal, oauthAccount, rowQuota\)/, 'Key 行应通过 buildRowQuotaSlotData 构造插槽数据');
assert.match(fullRowSource, /<UiSlot engine=\{formData\.engine\} slot="key_background"/, 'Key 行应保留 key_background 插槽');
assert.match(fullRowSource, /<UiSlot engine=\{formData\.engine\} slot="quota_display"/, 'Key 行应保留 quota_display 插槽');

const uiSlot = sliceBetween(keyComponentsSource, 'export const UiSlot = ', '// ── 冷却中 Key 行组件');
assert.match(uiSlot, /fallbackText/, 'UiSlot 应支持 fallbackText');
assert.match(uiSlot, /useMemo/, 'UiSlot 应使用内容签名稳定 effect 依赖，避免数组/对象引用变化导致重跑');

console.log('channel ui slots regression passed');
process.exit(0);
