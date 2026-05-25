import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：Antigravity 配额计算应下沉到后端 fetch_quota 和渠道 QUOTA_UI，前端 Channels.tsx 只保留通用双额度插槽。
// 修改方式：通过源码回归测试锁定 Channels.tsx 不再包含 Antigravity provider 分组硬编码，同时保留通用边框弧和 QUOTA_UI click 气泡行为。
// 目的：防止后续维护时把渠道专属 quota 计算重新写回通用前端，或把点击气泡改回 hover/定时关闭/重建关闭。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const repoRoot = path.resolve(frontendRoot, '..');
const channelsSource = readFileSync(path.resolve(frontendRoot, 'src/pages/Channels.tsx'), 'utf8');
const antigravitySource = readFileSync(path.resolve(repoRoot, 'core/channels/antigravity_channel.py'), 'utf8');

function sliceBetween(source, startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

const quotaHelper = sliceBetween(channelsSource, 'function normalizeQuotaPct', 'function sortProvidersByWeight');
assert.match(quotaHelper, /function getOAuthQuota\(account: any\): OAuthQuota \| null/, 'getOAuthQuota 应只接收账号对象，不能再依赖具体 engine');
assert.doesNotMatch(quotaHelper, /normalizeRemainingFraction|classifyAntigravityQuotaProvider|getAntigravityQuotaPercentages/, 'Channels.tsx 不应保留 Antigravity 专属 quota helper');
assert.doesNotMatch(quotaHelper, /engine\?: string|engine === 'antigravity'|MODEL_PROVIDER_GOOGLE|MODEL_PROVIDER_ANTHROPIC|MODEL_PROVIDER_OPENAI/, 'Channels.tsx 的通用 quota 读取不应包含 Antigravity provider 分组硬编码');
assert.match(antigravitySource, /_compute_antigravity_provider_quota_percentages\(raw\.get\("modelQuotas", \[\]\)\)/, 'Antigravity provider 分组应在后端 fetch_quota 中执行');
assert.match(antigravitySource, /MODEL_PROVIDER_GOOGLE[\s\S]*MODEL_PROVIDER_ANTHROPIC[\s\S]*MODEL_PROVIDER_OPENAI/, '后端应按 Gemini 与外部模型 provider 分组计算 quota_inner 和 quota_outer');
assert.match(antigravitySource, /model\.startswith\(\("tab_", "chat_"\)\)/, '后端分组计算应过滤 tab_* 和 chat_* 模型');

const keyRows = sliceBetween(channelsSource, 'const renderFullKeyRow =', '\n  };\n\n  return (', channelsSource.indexOf('const renderFullKeyRow ='));
assert.match(keyRows, /const rowQuota = buildRowQuota\(bal, oauthAccount, isOAuthEngine\);/, 'Key 行应该通过统一 RowQuota 构建 OAuth 与普通 balance 数据');
assert.match(keyRows, /const rowQuotaPair = getQuotaPairFromGauges\(rowQuota\.gauges\);/, 'Key 行默认边框应从 gauges 派生 inner 和 outer');
const overlayBlock = sliceBetween(keyRows, '{/* 修改原因：key 行边框可能由渠道自定义', '{/* 修改原因：背景装饰属于渠道自定义 UI');
assert.match(overlayBlock, /hasKeyBorderSlot[\s\S]*<UiSlot engine=\{formData\.engine\} slot="key_border"[\s\S]*data=\{slotData\}[\s\S]*<QuotaBorderOverlay quotaInner=\{rowQuotaPair\.quota_inner\} quotaOuter=\{rowQuotaPair\.quota_outer\} \/>/, '只有 key_border 插槽才能替代 QuotaBorderOverlay，quota_display 不应该替代边框');
assert.doesNotMatch(overlayBlock, /quota_display/, 'QuotaBorderOverlay 不应该因为 ui_slots.quota_display 存在而跳过');
assert.match(keyRows, /<UiSlot engine=\{formData\.engine\} slot="quota_display" data=\{slotData\}/, '自定义 QUOTA_UI 仍应只负责标签和气泡插槽');

const quotaSlot = sliceBetween(channelsSource, 'const UiSlot = ({ engine, slot, data', '// ── 冷却中 Key 行组件');
assert.match(quotaSlot, /const dataKey = useMemo/, 'UiSlot 应该用 data 内容签名稳定 effect 依赖');
assert.match(quotaSlot, /const enabledPluginsKey = useMemo/, 'UiSlot 应该用 enabledPlugins 内容签名稳定 effect 依赖');
assert.match(quotaSlot, /\}, \[engine, slot, dataKey, contextKey, fallbackText, enabledPluginsKey\]\);/, 'UiSlot 不应该继续直接依赖 data 或 enabledPlugins 数组引用');
assert.doesNotMatch(quotaSlot, /\}, \[engine, data\]\);/, 'UiSlot 不能因 data 新对象引用而频繁重跑 render');

const quotaUi = sliceBetween(antigravitySource, 'QUOTA_UI = """', '""".strip()');
assert.match(quotaUi, /export default function render\(ctx\)/, 'Antigravity QUOTA_UI 应该保持 Blob dynamic import 的默认导出');
const antigravityTierField = ['paid', 'Tier'].join('');
assert.match(quotaUi, new RegExp(`const tierName = ${antigravityTierField}\\?\\.name[\\s\\S]*Google\\\\s\\\\*[\\s\\S]*AI\\\\s\\\\*[\\s\\S]*Gemini Code Assist in`), 'Antigravity QUOTA_UI 应从后端订阅字段提取短 tier 名称');
assert.match(quotaUi, /el\.textContent = tierName \? `\$\{tierName\} \$\{minPct\}%` : `\$\{minPct\}%`;/, 'Antigravity QUOTA_UI 应优先显示 tier 名称加最低百分比');
assert.match(quotaUi, /data\.quota_inner = geminiPct/, 'QUOTA_UI 应该把 Gemini 最低百分比回写到 data.quota_inner');
assert.match(quotaUi, /data\.quota_outer = externalPct/, 'QUOTA_UI 应该把外部模型最低百分比回写到 data.quota_outer');
assert.match(quotaUi, /el\.__agTooltipOpen && el\.__agQuotaState\?\.update/, 'QUOTA_UI 打开时应该跳过 DOM 重建，只更新现有气泡内容');
assert.match(quotaUi, /el\.__agTooltipOpen = true[\s\S]*el\.__agTooltipOpen = false/, 'QUOTA_UI 应该用 el.__agTooltipOpen 追踪打开状态');
assert.match(quotaUi, /addEventListener\('click', onElClick\)/, 'QUOTA_UI 应该通过点击标签切换气泡');
assert.match(quotaUi, /document\.addEventListener\('click', onOutsideClick\)/, 'QUOTA_UI 应该保留 document 外部点击关闭逻辑');
assert.match(quotaUi, /window\.addEventListener\('scroll', onScroll, true\)/, 'QUOTA_UI 应该保留滚动关闭逻辑');
assert.doesNotMatch(quotaUi, /addEventListener\('mouseenter'|addEventListener\('mouseleave'|setTimeout|hideTimer/, 'QUOTA_UI 不应该再使用 hover 或定时关闭逻辑');
assert.match(quotaUi, /document\.createElement\('div'\)/, '点击气泡必须由纯 JS 创建 DOM');
assert.match(quotaUi, /bg-popover[\s\S]*border-border[\s\S]*text-foreground[\s\S]*rounded-lg[\s\S]*shadow-lg/, '点击气泡样式应该使用面板主题类');
assert.match(quotaUi, /resets in/, '点击气泡应该显示 reset 倒计时文案');
// 修改原因：QUOTA_UI 的点击气泡可以使用小型 SVG 图标，但 key 行边框弧线应由 QuotaBorderOverlay 或 key_border 插槽负责。
// 修改方式：只禁止 QUOTA_UI 直接把整行边框 SVG append 到 el，不再把 tooltip 内部 pathLength 误判为边框绘制。
// 目的：保持 Antigravity 标签职责清晰，同时允许气泡明细继续显示小型 quota 图标。
assert.doesNotMatch(quotaUi, /el\.appendChild\(svg\)/, 'QUOTA_UI 不应该直接向 key 行追加边框 SVG，弧线应由 QuotaBorderOverlay 负责');

console.log('antigravity quota UI regression passed');
// 修改原因：当前部署环境的 Node 18 在部分 ESM 脚本自然结束后会触发 Aborted。
// 修改方式：断言全部通过后显式以 0 退出，断言失败时仍会在这里之前抛出错误。
// 目的：让测试退出码只反映本文件断言是否通过。
process.exit(0);
