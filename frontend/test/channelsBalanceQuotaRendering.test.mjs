import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：Channels.tsx 的 balance/quota 展示曾混有 OAuth engine、plan 字段和渠道私有用量字段硬编码。
// 修改方式：通过源码静态断言锁定余额标签格式化、后端 is_oauth 字段、标准 tier 字段和通用双额度渲染路径。
// 目的：防止后续维护再次把渠道专属字段或 engine 名称写回通用前端。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const channelsSource = readFileSync(path.resolve(frontendRoot, 'src/pages/Channels.tsx'), 'utf8');
// 修改原因：本测试要检查旧常量和私有字段已从业务源码删除，但测试本身不能重新制造静态引用。
// 修改方式：用字符串拼接生成旧常量名和私有字段前缀。
// 目的：保持全项目文本搜索结果干净，同时继续提供回归保护。
const oldOAuthEngineSetName = ['OAUTH', 'ENGINES'].join('_');
const privateUsagePattern = new RegExp(['extra', 'usage'].join('_'));

function sliceBetween(source, startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

assert.doesNotMatch(channelsSource, new RegExp(oldOAuthEngineSetName), '前端不能继续维护 OAuth engine 硬编码集合');
assert.match(channelsSource, /const isOAuthEngine = selectedChannelType\?\.is_oauth \?\? false;/, 'OAuth 判断只能读取后端渠道元数据 is_oauth 字段');
assert.doesNotMatch(channelsSource, privateUsagePattern, 'Channels.tsx 不能再读取、保存或注释渠道私有用量字段');

const balanceHelpers = sliceBetween(channelsSource, 'function formatCompactNumber', 'function normalizeQuotaPct');
// 修改原因：列表模式空间足够，应保留 available/total 原始语义；只有机房卡片才把 amount 压缩成百分比。
// 修改方式：静态断言同时锁定 getBalanceLabel 与 getBalanceCompactLabel 两条路径，避免后续再次共用同一标签函数。
// 目的：防止列表行显示被误改成百分比，同时保证机房卡片仍能使用短标签。
assert.match(balanceHelpers, /function formatCompactNumber\(n: number\): string/, '余额标签应提供指定的紧凑数字格式化函数');
assert.match(balanceHelpers, /Math\.abs\(n\) >= 1e9[\s\S]*1e6[\s\S]*1e3/, '紧凑数字格式化应覆盖 B、M、K 三档');
assert.match(balanceHelpers, /function getBalanceLabel\(b: BalanceResult\): string \| null[\s\S]*\$\{formatCompactNumber\(b\.available\)\} \/ \$\{formatCompactNumber\(b\.total\)\}/, '列表余额标签应保留 available/total，并对大数字做紧凑格式化');
const listLabelBlock = sliceBetween(balanceHelpers, 'function getBalanceLabel', 'function getBalanceCompactLabel');
assert.doesNotMatch(listLabelBlock, /b\.available \/ b\.total|b\.percent\.toFixed\(1\).*value_type === 'amount'/, '列表余额标签不能把 amount 模式改成百分比');
assert.match(balanceHelpers, /function getBalanceCompactLabel\(b: BalanceResult\): string \| null[\s\S]*b\.value_type === 'amount'[\s\S]*b\.percent != null[\s\S]*return `\$\{b\.percent\.toFixed\(1\)\}%`;/, '机房卡片紧凑标签应优先使用后端补算的 amount 百分比');
assert.match(balanceHelpers, /function getBalanceCompactLabel\(b: BalanceResult\): string \| null[\s\S]*b\.available \/ b\.total[\s\S]*\.toFixed\(1\)/, '机房卡片紧凑标签应保留旧数据的前端计算兜底');

const quotaHelpers = sliceBetween(channelsSource, 'function normalizeQuotaPct', 'function sortProvidersByWeight');
assert.match(quotaHelpers, /function getQuotaFromSource\(source: any, rawValue\?: any\): OAuthQuota \| null/, '双额度读取应抽成通用 helper');
assert.match(quotaHelpers, /function getBalanceQuota\(bal: BalanceResult \| undefined\): OAuthQuota \| null/, '普通 balance 结果也应能进入双额度读取路径');
assert.match(quotaHelpers, /function getOAuthQuota\(account: any\): OAuthQuota \| null/, 'OAuth 账号仍应通过同一双额度 helper 归一化');
assert.match(quotaHelpers, /function buildRowQuota\(bal: BalanceResult \| undefined, oauthAccount: any, isOAuthEngine: boolean\): RowQuota/, 'Key 行应通过 buildRowQuota 统一构建 gauges');
assert.match(quotaHelpers, /if \(Array\.isArray\(bal\?\.gauges\) && bal\.gauges\.length > 0\)/, 'buildRowQuota 应优先读取后端新 gauges 字段');
assert.doesNotMatch(quotaHelpers, /badges\.push|bal\?\.tier|rowQuota\.badges/, 'buildRowQuota 不应构建或读取 quota badges，tier 展示应交给 quota_display 插槽');
assert.match(quotaHelpers, /function buildRowQuotaSlotData\(bal: BalanceResult \| undefined, oauthAccount: any, rowQuota: RowQuota\): any/, '旧 ui_slots 数据应通过兼容 helper 保留');
assert.match(quotaHelpers, /function getQuotaPairFromGauges\(gauges: QuotaGauge\[\]\): OAuthQuota \| null/, '默认边框应从 gauges 派生 inner\/outer 兼容数据');

const quotaComponents = sliceBetween(channelsSource, 'function RackOAuthRings', 'function RackCoolingBorder');
assert.match(quotaComponents, /function QuotaRings\(\{ gauges, hideText \}: \{ gauges: QuotaGauge\[\]; hideText\?: boolean \}\)/, '前端应提供通用 QuotaRings 组件');
assert.match(quotaComponents, /visibleGauges\.length === 0[\s\S]*暂无额度数据/, 'QuotaRings 应支持 0 gauge 空态灰环');
assert.match(quotaComponents, /visibleGauges\.length === 1[\s\S]*RackRingCircle radius=\{25\}/, 'QuotaRings 应支持单 gauge 单环');
assert.match(quotaComponents, /filter\(Boolean\)\.slice\(0, 2\)[\s\S]*RackRingCircle radius=\{26\}[\s\S]*RackRingCircle radius=\{18\}/, 'QuotaRings 应对 2 个及以上 gauge 使用双环并只取前两个');
assert.doesNotMatch(quotaComponents, /function QuotaBadges|QUOTA_BADGE_TONE_CLASSES/, '前端不应再提供或渲染 QuotaBadges，标签展示应交给 quota_display 插槽');

const rackCard = sliceBetween(channelsSource, 'function RackCard', 'export default function Channels');
assert.match(rackCard, /const rowQuota = buildRowQuota\(bal, oauthAccount, isOAuthEngine\);/, '机房卡片应统一构建 RowQuota');
assert.match(rackCard, /const rackGauges = withRackCompactBalanceFallback\(rowQuota\.gauges, bal\);/, '机房卡片应只在旧 fallback 下使用紧凑余额文本');
assert.match(rackCard, /<QuotaRings gauges=\{rackGauges\} hideText=\{hasQuotaDisplaySlot && slotPayloadAvailable\} \/>/, '机房卡片应通过 QuotaRings 渲染圆环');
assert.doesNotMatch(rackCard, /<QuotaBadges|rowQuota\.badges\.length/, '机房卡片不应渲染 QuotaBadges 或把 badges 作为 slot 可用性条件');
assert.doesNotMatch(rackCard, /<RackOAuthRings|<RackSingleRing/, '机房卡片不应再按 OAuth 分支选择旧圆环组件');

const fullRow = sliceBetween(channelsSource, 'const renderFullKeyRow =', '\n  };\n\n  return (', channelsSource.indexOf('const renderFullKeyRow ='));
assert.match(fullRow, /const rowQuota = buildRowQuota\(bal, oauthAccount, isOAuthEngine\);/, '完整 Key 行应统一构建 RowQuota');
assert.match(fullRow, /const rowQuotaPair = getQuotaPairFromGauges\(rowQuota\.gauges\);/, '完整 Key 行默认边框应从 gauges 派生双额度');
assert.match(fullRow, /<QuotaBorderOverlay quotaInner=\{rowQuotaPair\.quota_inner\} quotaOuter=\{rowQuotaPair\.quota_outer\} \/>/, '默认双弧边框应支持通用 gauges 数据');
assert.match(fullRow, /<QuotaRings gauges=\{rowQuota\.gauges\} \/>/, '完整 Key 行应通过 QuotaRings 渲染圆环');
assert.doesNotMatch(fullRow, /<QuotaBadges|rowQuota\.badges\.length/, '完整 Key 行不应渲染 QuotaBadges 或把 badges 作为右侧标签条件');
assert.doesNotMatch(fullRow, /const balLabel|const tierLabel|<QuotaArcs/, '完整 Key 行不应再保留 tier\/balance 标签或 QuotaArcs 旧路径');

console.log('channels balance quota rendering regression passed');
// 修改原因：当前部署环境的 Node 18 在部分 ESM 脚本自然结束后可能触发 Aborted。
// 修改方式：断言全部通过后显式以 0 退出，断言失败时仍会在这里之前抛出错误。
// 目的：让测试退出码只反映本文件断言是否通过。
process.exit(0);
