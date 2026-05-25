import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：Channels.tsx 要从单一 quota_display 改为通用多插槽，不能保留渠道私有用量字段硬编码。
// 修改方式：用源码静态断言检查通用 UiSlot、四个插槽挂载点，以及私有字段不再进入通用数据流。
// 目的：防止后续维护时把渠道专属余额条、标签或汇总逻辑重新写回通用前端。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const channelsSource = readFileSync(path.resolve(frontendRoot, 'src/pages/Channels.tsx'), 'utf8');
// 修改原因：测试需要确认旧的渠道私有字段已经从 Channels.tsx 中消失，但测试源码本身不应重新形成该字段引用。
// 修改方式：用字符串拼接生成匹配模式，避免静态搜索把测试断言误判成业务引用。
// 目的：既锁住删除结果，又保持全项目源码搜索结果干净。
const privateUsagePattern = new RegExp(['extra', 'usage'].join('_'));

function sliceBetween(source, startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

const slotComponent = sliceBetween(channelsSource, 'const uiSlotCache', '// ── 冷却中 Key 行组件');
assert.match(channelsSource, /type UiSlotValue = string \| \{ script\?: string; requires_plugin\?: string \};/, '前端应支持后端返回带 requires_plugin 条件的 slot 对象');
assert.match(slotComponent, /function hasUiSlot\(engine: string \| undefined, slot: string, enabledPlugins\?: string\[\]\): boolean/, 'hasUiSlot 应接收当前 provider 的 enabled_plugins');
assert.match(slotComponent, /requires_plugin[\s\S]*providerHasEnabledPlugin/, 'hasUiSlot 应按 requires_plugin 判断 provider 是否启用对应插件');
assert.match(slotComponent, /const UiSlot = \(\{ engine, slot, data, context, className, element = 'span', fallbackText, enabledPlugins \}/, '前端应提供通用 UiSlot 组件，并接收 enabledPlugins');
assert.match(slotComponent, /const cacheKey = `\$\{engine\}:\$\{slot\}`;/, 'UiSlot 缓存 key 应包含 engine 和 slot');
assert.match(slotComponent, /__uiSlots\?\.\[engine\]\?\.\[slot\]/, 'UiSlot 应按 engine 和 slot 从 window.__uiSlots 读取脚本');
assert.match(slotComponent, /const jsSrc = getUiSlotScript\(engine, slot, enabledPlugins\);/, 'UiSlot 应从对象 slot 中提取 script，并复查 requires_plugin 条件');
assert.doesNotMatch(slotComponent, /quota_display;/, 'UiSlot 内部不能固定读取 quota_display');
assert.match(slotComponent, /fn\(\{ el, data: dataRef\.current, \.\.\.\(contextRef\.current \?\? \{\}\) \}\)/, 'UiSlot 应把 data 和额外 context 一起传给渠道脚本');

const balanceButton = sliceBetween(channelsSource, '<Wallet className={`w-3 h-3 ${balanceLoading ? \'animate-pulse\' : \'\'}`} />', '</button>', channelsSource.indexOf('onClick={() => queryAllBalances()}'));
assert.match(balanceButton, /hasUiSlot\(formData\.engine, 'balance_summary', formData\.preferences\.enabled_plugins\)/, 'OAuth 余额按钮应结合 enabled_plugins 检测 balance_summary 插槽');
assert.match(balanceButton, /slot="balance_summary"/, 'OAuth 余额按钮应使用 balance_summary 插槽');
assert.match(balanceButton, /context=\{\{ accounts: oauthAccounts \}\}/, 'balance_summary 插槽应收到所有 OAuth 账号');
assert.match(balanceButton, /fallbackText="余额"/, 'balance_summary 插槽应保留余额默认文本');
assert.doesNotMatch(balanceButton, privateUsagePattern, '余额按钮不能再包含渠道私有用量汇总硬编码');

// 修改原因：hint 插槽只负责渠道提示文本，通用前端不能硬编码 Antigravity 覆写格式或 Claude Code 充值链接。
// 修改方式：用源码静态断言检查 base_url_hint、key_hint、override_hint 三个挂载点都按 hasUiSlot 条件渲染。
// 目的：保证未注册 hint 的渠道不显示、不占空间，注册渠道能通过 UiSlot 自行写入提示内容。
const baseUrlBlock = sliceBetween(channelsSource, 'API 地址 (Base URL)', '修改原因：OAuth 引擎');
assert.match(baseUrlBlock, /hasUiSlot\(formData\.engine, 'base_url_hint', formData\.preferences\.enabled_plugins\)/, 'Base URL 区域应结合 enabled_plugins 检测 base_url_hint 插槽');
assert.match(baseUrlBlock, /slot="base_url_hint"[\s\S]*data=\{null\}[\s\S]*element="div"[\s\S]*className="text-xs text-muted-foreground mt-1"/, 'base_url_hint 应挂载为 muted 小字 div');

const keyHintBlock = sliceBetween(channelsSource, '{/* 2. API Keys', '<div data-key-scroll className="space-y-2 max-h-64');
assert.match(keyHintBlock, /hasUiSlot\(formData\.engine, 'key_hint', formData\.preferences\.enabled_plugins\)/, 'Key 列表标题附近应结合 enabled_plugins 检测 key_hint 插槽');
assert.match(keyHintBlock, /slot="key_hint"[\s\S]*data=\{null\}[\s\S]*element="div"[\s\S]*className="text-xs text-muted-foreground"/, 'key_hint 应挂载为 muted 小字 div');

const overrideHintBlock = sliceBetween(channelsSource, '请求体覆写 (JSON)', '<div className="flex items-center justify-between p-3 bg-muted/50');
assert.match(overrideHintBlock, /hasUiSlot\(formData\.engine, 'override_hint', formData\.preferences\.enabled_plugins\)/, '请求体覆写区域应结合 enabled_plugins 检测 override_hint 插槽');
assert.match(overrideHintBlock, /slot="override_hint"[\s\S]*data=\{null\}[\s\S]*element="div"[\s\S]*className="text-xs text-amber-600 dark:text-amber-400 mt-1"/, 'override_hint 应挂载为 amber 警告小字 div');

const keyRows = sliceBetween(channelsSource, 'const renderFullKeyRow =', '\n  };\n\n  return (', channelsSource.indexOf('const renderFullKeyRow ='));
assert.match(keyRows, /const enabledPlugins = formData\.preferences\.enabled_plugins \|\| \[\];/, 'Key 行应从 formData.preferences.enabled_plugins 读取当前 provider 插件列表');
assert.match(keyRows, /const hasKeyBorderSlot = hasUiSlot\(formData\.engine, 'key_border', enabledPlugins\);/, 'Key 行应结合 enabled_plugins 检测 key_border 插槽');
assert.match(keyRows, /const hasKeyBackgroundSlot = hasUiSlot\(formData\.engine, 'key_background', enabledPlugins\);/, 'Key 行应结合 enabled_plugins 检测 key_background 插槽');
assert.match(keyRows, /const hasQuotaDisplaySlot = hasUiSlot\(formData\.engine, 'quota_display', enabledPlugins\);/, 'Key 行应结合 enabled_plugins 检测 quota_display 插槽');
assert.doesNotMatch(keyRows, /quota_label/, 'P2 后 Key 行不应再保留独立 quota_label 插槽');
assert.match(keyRows, /slot="key_border"[\s\S]*data=\{slotData\}[\s\S]*context=\{slotContext\}[\s\S]*<QuotaBorderOverlay quotaInner=\{rowQuotaPair\.quota_inner\} quotaOuter=\{rowQuotaPair\.quota_outer\} \/>/, 'key_border 插槽存在时应替代默认双弧边框，否则保留默认边框');
assert.match(keyRows, /slot="key_background"[\s\S]*data=\{slotData\}[\s\S]*context=\{slotContext\}[\s\S]*element="div"[\s\S]*className="absolute inset-0 pointer-events-none rounded-\[7px\] z-0 transition-all duration-500"/, 'key_background 插槽应挂载为覆盖整行的 absolute div');
assert.match(keyRows, /slot="quota_display"/, 'quota_display 应继续作为通用 UiSlot 的一个插槽');
assert.match(keyRows, /slot="quota_display"[\s\S]*data=\{slotData\}[\s\S]*context=\{slotContext\}[\s\S]*enabledPlugins=\{enabledPlugins\}/, 'quota_display 插槽应收到当前行统一上下文和插件列表');
assert.doesNotMatch(keyRows, privateUsagePattern, 'Key 行渲染逻辑不能再读取渠道私有用量字段');

const rackCard = sliceBetween(channelsSource, 'function RackCard', 'export default function Channels');
assert.match(rackCard, /enabledPlugins: string\[\];/, 'RackCard props 应接收当前 provider 的 enabled_plugins');
assert.match(rackCard, /const hasQuotaDisplaySlot = hasUiSlot\(engine, 'quota_display', enabledPlugins\);/, '机房卡片应结合 enabled_plugins 检测 quota_display 插槽');
assert.match(rackCard, /<UiSlot engine=\{engine\} slot="quota_display"[\s\S]*enabledPlugins=\{enabledPlugins\}/, '机房卡片渲染 quota_display 时应传入 enabledPlugins');

const dataFlow = sliceBetween(channelsSource, 'const perAccount = data?.results || {};', 'const openModal = async');
assert.doesNotMatch(dataFlow, privateUsagePattern, '余额回调不能再写入渠道私有用量字段');
assert.match(dataFlow, /_quota_unavailable: !hasQuota/, '余额回调应只按标准 quota 字段判断默认双弧可用性');

console.log('channel UI slots regression passed');
// 修改原因：当前部署环境的 Node 18 在部分 ESM 脚本自然结束后会触发 Aborted。
// 修改方式：断言全部通过后显式以 0 退出，断言失败时仍会在这里之前抛出错误。
// 目的：让测试退出码只反映本文件断言是否通过。
process.exit(0);
