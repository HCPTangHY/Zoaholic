/* eslint-disable @typescript-eslint/no-explicit-any */

import { sanitizeKeyRulesForSave } from '../../lib/keyRules';
import { parseEnabledPluginValue, type EnabledPluginValue } from '../../lib/pluginEntries';
import type { ApiKeyObj, BalanceResult, OAuthQuota, QuotaGauge, RowQuota, UiSlotValue } from './types';

// 修改原因：Channels.tsx 拆分后，余额、插槽、SVG 和机房模式 helper 需要在多个模块复用。
// 修改方式：把原页面中的纯函数和常量集中导出，调用点只替换 import，不改业务逻辑。
// 目的：减少页面文件体积，并保证后续 hooks 与组件使用同一套计算规则。
export const SCHEDULE_ALGORITHMS = [
  { value: 'round_robin', label: '轮询 (Round Robin)' },
  { value: 'fixed_priority', label: '固定优先级 (Fixed)' },
  { value: 'random', label: '随机 (Random)' },
  { value: 'smart_round_robin', label: '智能轮询 (Smart)' },
  { value: 'sticky_ip', label: 'IP 粘滞 (Sticky IP)' },
];

export function readBooleanPreference(value: any): boolean {
  // 修改原因：后端新增 pool_sharing 布尔开关，旧配置中也可能存在字符串形式的布尔值。
  // 修改方式：统一把 true/1/yes/on 转为 true，其余值按 JavaScript 布尔语义处理。
  // 目的：让表单初始化时能够稳定显示共享路由池开关的实际状态。
  if (typeof value === 'string') return ['true', '1', 'yes', 'on'].includes(value.trim().toLowerCase());
  return Boolean(value);
}

export function serializeChannelPreferences(preferences: Record<string, any>): Record<string, any> {
  // 修改原因：Key Rules 的 retry 默认态和 remap 空值不能原样写入配置，否则后端会难以区分默认和显式动作。
  // 修改方式：保存和测试预览前复制 preferences，并用统一 helper 清理 key_rules 字段。
  // 目的：保证 retry 只在强制重试或禁止重试时保存，remap 只在填写有效目标状态码时保存。
  const next = { ...(preferences || {}) };
  if (Array.isArray(next.key_rules)) {
    next.key_rules = sanitizeKeyRulesForSave(next.key_rules);
  }
  return next;
}

// ── 余额类型 ──

export function formatCompactNumber(n: number): string {
  // 修改原因：完整列表行需要保留 available/total 原始语义，但大额数字直接显示会挤压输入区域。
  // 修改方式：按用户指定的 B、M、K 阶梯缩写数字，并统一保留一位小数。
  // 目的：让列表模式显示 178.8M / 250.0M 这类短文本，而不是改成百分比。
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(1);
}

export function getBalancePercent(b: BalanceResult): number | null {
  if (!b.supported || b.error) return null;
  if (b.value_type === 'percent' && b.percent != null) return b.percent;
  // 修改原因：后端会为 amount 模式补算 percent，圆环进度应优先复用同一个标准字段。
  // 修改方式：amount 且 percent 已存在时直接返回 percent，旧数据仍保留 available/total 兜底计算。
  // 目的：让前后端对剩余额度百分比使用一致口径。
  if (b.value_type === 'amount' && b.percent != null) return b.percent;
  if (b.value_type === 'quota' && b.available != null) return Math.min(b.available, 100);
  if (b.total != null && b.total > 0 && b.available != null) return (b.available / b.total) * 100;
  return null;
}

export function getBalanceColor(pct: number | null): 'green' | 'yellow' | 'red' | null {
  if (pct == null) return null;
  if (pct >= 50) return 'green';
  if (pct >= 20) return 'yellow';
  return 'red';
}

export function getBalanceLabel(b: BalanceResult): string | null {
  // 修改原因：完整 Key 行空间较充足，amount 模式不应被全局改成百分比。
  // 修改方式：percent 模式仍显示百分比，其余模式保留 available/total 或 available 原始标签，并只压缩大数字。
  // 目的：让列表模式继续展示 178.8M / 250.0M 这类可读明细。
  if (!b.supported || b.error) return null;
  if (b.value_type === 'percent' && b.percent != null) return `${b.percent.toFixed(1)}%`;
  if (b.available != null && b.total != null) return `${formatCompactNumber(b.available)} / ${formatCompactNumber(b.total)}`;
  if (b.available != null) {
    const prefix = getCurrencySymbol((b as any).currency);
    return `${prefix}${formatCompactNumber(b.available)}`;
  }
  return null;
}

export function getBalanceCompactLabel(b: BalanceResult): string | null {
  // 修改原因：机房卡片圆环中心空间有限，amount 模式继续显示 available/total 会发生溢出。
  // 修改方式：新增紧凑标签函数只供 RackCard 使用，amount 模式优先读后端 percent，并保留旧数据的前端计算兜底。
  // 目的：把百分比显示限定在机房卡片模式，不影响完整列表行。
  if (!b.supported || b.error) return null;
  if (b.value_type === 'percent' && b.percent != null) return `${b.percent.toFixed(1)}%`;
  if (b.value_type === 'amount' && b.total != null && b.total > 0) {
    if (b.percent != null) return `${b.percent.toFixed(1)}%`;
    if (b.available != null) return `${((b.available / b.total) * 100).toFixed(1)}%`;
    if (b.used != null) return `${(((b.total - b.used) / b.total) * 100).toFixed(1)}%`;
  }
  return getBalanceLabel(b);
}

export function normalizeQuotaPct(value: any): number | undefined {
  // 修改原因：双额度来源既可能是后端数字，也可能是缓存字符串，直接参与弧线长度计算会产生 NaN。
  // 修改方式：统一把空值转为 undefined，把可解析数字裁剪到 0 到 100。
  // 目的：保证通用双弧只接收稳定百分比，渠道专属计算由后端或 ui_slots 完成。
  if (value == null || value === '') return undefined;
  const n = Number(value);
  if (!Number.isFinite(n)) return undefined;
  return Math.max(0, Math.min(100, n));
}

export function getQuotaFromSource(source: any, rawValue?: any): OAuthQuota | null {
  // 修改原因：后端 OAuthManager 仍可能返回旧 quota_5h/quota_7d 字段，而前端内部字段已经统一为 quota_inner/quota_outer。
  // 修改方式：读取时兼容旧字段并回退到新字段，只把归一化后的新字段写入前端状态和渲染数据。
  // 目的：后端无需同步改名，前端的默认双弧和渠道插槽仍只消费 quota_inner/quota_outer。
  if (!source) return null;
  const quota_inner = normalizeQuotaPct(source.quota_5h ?? source.quota_inner);
  const quota_outer = normalizeQuotaPct(source.quota_7d ?? source.quota_outer);
  const raw = rawValue ?? undefined;
  if (quota_inner == null && quota_outer == null && raw == null) return null;
  return { quota_inner, quota_outer, raw };
}

export function getOAuthQuota(account: any): OAuthQuota | null {
  // 修改原因：OAuth 账号状态会把后端 fetch_quota 的 raw 缓存在 quota_raw 中，但标准额度字段仍与普通 balance 相同。
  // 修改方式：复用通用双额度读取 helper，并把账号 raw 显式传给渠道插槽。
  // 目的：OAuth 渠道保留自定义额度展示能力，同时不再维护渠道专属字段猜测。
  return getQuotaFromSource(account, account?.quota_raw ?? account?.raw);
}

export function normalizeOAuthAccountStateMap(accounts: Record<string, any> | null | undefined): Record<string, any> {
  // 修改原因：OAuth 状态接口和旧缓存可能继续返回 quota_5h/quota_7d，前端状态需要统一使用 quota_inner/quota_outer。
  // 修改方式：遍历账号状态并复用 getQuotaFromSource 兼容旧字段，只把归一化后的新字段补回状态副本。
  // 目的：让初始加载、自动刷新和手动刷新都消费同一套前端字段名。
  const next: Record<string, any> = {};
  for (const [keyId, account] of Object.entries(accounts || {})) {
    const quota = getQuotaFromSource(account, account?.quota_raw ?? account?.raw);
    next[keyId] = {
      ...account,
      ...(quota?.quota_inner != null ? { quota_inner: quota.quota_inner } : {}),
      ...(quota?.quota_outer != null ? { quota_outer: quota.quota_outer } : {}),
      ...(quota?.raw != null ? { quota_raw: quota.raw } : {}),
    };
  }
  return next;
}

export function getBalanceQuota(bal: BalanceResult | undefined): OAuthQuota | null {
  // 修改原因：普通 Key 的 balance 结果如果返回 quota_inner 和 quota_outer，也应进入默认双弧展示。
  // 修改方式：只从 BalanceResult 顶层标准字段读取双额度，不把普通 raw 响应误判成可渲染 quota。
  // 目的：让非 OAuth Key 复用 RackOAuthRings 和 QuotaBorderOverlay，同时避免所有普通余额都出现空双弧。
  if (!bal || !bal.supported || bal.error) return null;
  return getQuotaFromSource(bal);
}

export function getQuotaPairFromGauges(gauges: QuotaGauge[]): OAuthQuota | null {
  // 修改原因：QuotaBorderOverlay 和旧 quota_display 插槽仍消费 quota_inner/quota_outer，但 P2 行模型已统一成 gauges。
  // 修改方式：从前两个 gauge 提取百分比并映射为 inner/outer，超过两个 gauge 时和 QuotaRings 一样只取前两个。
  // 目的：保留默认双额度边框和已有渠道插槽兼容性，同时不在渲染层重新按 OAuth 分支取值。
  if (!Array.isArray(gauges) || gauges.length < 2) return null;
  const quota_inner = normalizeQuotaPct(gauges[0]?.percent);
  const quota_outer = normalizeQuotaPct(gauges[1]?.percent);
  if (quota_inner == null && quota_outer == null) return null;
  return { quota_inner, quota_outer };
}

export function buildRowQuota(bal: BalanceResult | undefined, oauthAccount: any, isOAuthEngine: boolean): RowQuota {
  // 修改原因：机房卡片和完整 Key 行原先分别判断 OAuth 与普通余额，展示分支会随渠道继续增加。
  // 修改方式：只构建通用圆环 gauges；tier、plan 和插件标签不再进入 RowQuota，改由 quota_display slot 渲染。
  // 目的：保留 QuotaRings 的统一入口，同时删除通用 badge 这条前端硬编码展示路径。
  if (Array.isArray(bal?.gauges) && bal.gauges.length > 0) {
    return { gauges: bal.gauges };
  }

  const gauges: QuotaGauge[] = [];

  if (isOAuthEngine) {
    // 修改原因：getOAuthQuota 会为了插槽保留 raw-only 对象，但 raw-only 不能遮蔽 balance 结果中的旧 quota_inner/quota_outer。
    // 修改方式：只有账号 quota 含有实际百分比时才优先使用账号数据，否则回退到 balance 旧字段。
    // 目的：保持 API 未返回 gauges 时，OAuth 行仍能从旧 balance quota 构建双环。
    const accountQuota = getOAuthQuota(oauthAccount);
    const balanceQuota = getBalanceQuota(bal);
    const quota = (accountQuota?.quota_inner != null || accountQuota?.quota_outer != null) ? accountQuota : balanceQuota;
    if (quota?.quota_inner != null) gauges.push({ id: 'inner', label: 'inner', role: 'short_window', percent: quota.quota_inner });
    if (quota?.quota_outer != null) gauges.push({ id: 'outer', label: 'outer', role: 'long_window', percent: quota.quota_outer });
  } else if (bal) {
    const mode = bal.value_type === 'quota' ? 'quota' : bal.value_type === 'amount' ? 'amount' : 'percent';
    if (mode === 'quota' && bal.available != null) {
      gauges.push({
        id: 'balance', label: '余额', role: 'primary',
        available: bal.available, unit: (bal as any).currency,
        display_mode: 'quota',
      });
    } else {
      const pct = getBalancePercent(bal);
      if (pct != null) {
        gauges.push({
          id: 'balance', label: '余额', role: 'primary',
          percent: pct, tone: getBalanceColor(pct),
          display_mode: mode,
          displayLabel: mode === 'amount' ? getBalanceLabel(bal) : null,
          total: bal.total, available: bal.available, unit: (bal as any).currency,
        });
      }
    }
  }

  // 修改原因：后端或插件返回的 badges 只应作为原始 slot 数据保留，不能由通用前端直接渲染。
  // 修改方式：buildRowQuota 不再读取 bal.badges 或 bal.tier，只返回圆环 gauges。
  // 目的：让 tier/plan 等展示完全由 quota_display 插槽和对应插件开关控制。
  return { gauges };
}

export function buildRowQuotaSlotData(bal: BalanceResult | undefined, oauthAccount: any, rowQuota: RowQuota): any {
  // 修改原因：现有渠道 ui_slots 仍读取 data.quota_inner、data.quota_outer、data.raw 或普通 balance 字段，不能在 P2 中突然改为只传 gauges。
  // 修改方式：普通 balance 优先原样传入；没有 balance 时从账号或 gauges 构造兼容 quota 数据，并把 raw 保留给旧脚本。
  // 目的：在展示层统一为 RowQuota 的同时，保证 P1 已注册的 quota_display、key_background 等插槽继续工作。
  if (bal) return bal;
  const quota = getOAuthQuota(oauthAccount) ?? getQuotaPairFromGauges(rowQuota.gauges);
  if (!quota) return null;
  return {
    ...quota,
    raw: quota.raw ?? oauthAccount?.quota_raw ?? oauthAccount?.raw,
  };
}

export function withRackCompactBalanceFallback(gauges: QuotaGauge[], bal: BalanceResult | undefined): QuotaGauge[] {
  // 修改原因：旧 amount 余额没有 gauges 时，机房卡片圆心不能继续显示完整 available/total 长文本。
  // 修改方式：只在 legacy balance fallback 的单 gauge 上替换 displayLabel 为 getBalanceCompactLabel，后端新 gauges 不改写。
  // 目的：保留 P2 统一圆环入口，同时延续机房卡片的紧凑显示策略。
  if (bal?.gauges?.length || !bal || gauges.length !== 1 || gauges[0]?.id !== 'balance') return gauges;
  const compactLabel = bal.value_type !== 'percent' ? getBalanceCompactLabel(bal) : null;
  if (!compactLabel) return gauges;
  return [{ ...gauges[0], displayLabel: compactLabel }];
}

export function sortProvidersByWeight(list: any[]): any[] {
  // 排序规则：权重降序 → 同权重按名称自然排序（数字感知 + Unicode）
  return [...list].sort((a, b) => {
    const weightA = a.preferences?.weight ?? a.weight ?? 0;
    const weightB = b.preferences?.weight ?? b.weight ?? 0;
    if (weightB !== weightA) return weightB - weightA;
    const nameA = String(a.provider || '');
    const nameB = String(b.provider || '');
    return nameA.localeCompare(nameB, undefined, { numeric: true, sensitivity: 'base' });
  });
}

export function buildProviderApiPath(providerId: string): string {
  // 修改原因：provider 名可能包含空格、斜杠或其他需要转义的字符，直接拼 URL 会请求错误路径。
  // 修改方式：所有单渠道 PUT/DELETE 统一通过 encodeURIComponent 生成路径。
  // 目的：保证前端调用 /v1/providers/{provider_id} 时按真实渠道名定位。
  return `/v1/providers/${encodeURIComponent(providerId)}`;
}

export const BALANCE_FILL_COLORS = {
  green: 'linear-gradient(90deg, rgba(16,185,129,0.15) 0%, rgba(16,185,129,0.04) 100%)',
  yellow: 'linear-gradient(90deg, rgba(234,179,8,0.18) 0%, rgba(234,179,8,0.04) 100%)',
  red: 'linear-gradient(90deg, rgba(239,68,68,0.18) 0%, rgba(239,68,68,0.04) 100%)',
};

export const TAG_CLASSES = {
  green: 'text-emerald-400 bg-emerald-500/12',
  yellow: 'text-yellow-400 bg-yellow-500/12',
  red: 'text-red-400 bg-red-500/12',
};

// ── 格式化倒计时 ──
export function formatCountdown(seconds: number) {
  if (seconds <= 0) return '即将恢复';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// ── SVG 圆角矩形路径生成 ──
export function buildRoundRectPath(x: number, y: number, w: number, h: number, r: number) {
  return [
    `M ${x + r} ${y}`,
    `L ${x + w - r} ${y}`,
    `A ${r} ${r} 0 0 1 ${x + w} ${y + r}`,
    `L ${x + w} ${y + h - r}`,
    `A ${r} ${r} 0 0 1 ${x + w - r} ${y + h}`,
    `L ${x + r} ${y + h}`,
    `A ${r} ${r} 0 0 1 ${x} ${y + h - r}`,
    `L ${x} ${y + r}`,
    `A ${r} ${r} 0 0 1 ${x + r} ${y}`,
    `Z`
  ].join(' ');
}

// 构建圆角矩形上半 path（从左中点顺时针到右中点）
// 上半 path：左中点 → 左上圆角 → 上边 → 右上圆角 → 右中点
// 0%=左中点，50%=上边中点，100%=右中点
export function buildTopHalfPath(x: number, y: number, w: number, h: number, r: number) {
  const my = y + h / 2;
  return [
    `M ${x} ${my}`,
    `L ${x} ${y + r}`,
    `A ${r} ${r} 0 0 1 ${x + r} ${y}`,
    `L ${x + w - r} ${y}`,
    `A ${r} ${r} 0 0 1 ${x + w} ${y + r}`,
    `L ${x + w} ${my}`,
  ].join(' ');
}

// 下半 path：左中点 → 左下圆角 → 下边 → 右下圆角 → 右中点
// 0%=左中点，50%=下边中点，100%=右中点
export function buildBottomHalfPath(x: number, y: number, w: number, h: number, r: number) {
  const my = y + h / 2;
  return [
    `M ${x} ${my}`,
    `L ${x} ${y + h - r}`,
    `A ${r} ${r} 0 0 0 ${x + r} ${y + h}`,
    `L ${x + w - r} ${y + h}`,
    `A ${r} ${r} 0 0 0 ${x + w} ${y + h - r}`,
    `L ${x + w} ${my}`,
  ].join(' ');
}

// 通用双额度边框叠加层 — 上半蓝色(quota_inner)、下半紫色(quota_outer)

export const uiSlotCache: Record<string, ((ctx: any) => void) | null> = {};

export function serializeSlotValue(value: any): string {
  // 修改原因：插槽 data/context 多数来自 React state，每次 render 都可能产生新对象引用，直接依赖对象会导致脚本反复执行。
  // 修改方式：把值序列化成内容签名作为 effect 依赖，实际执行时仍从 ref 读取最新值。
  // 目的：减少轮询、倒计时和输入聚焦造成的无意义插槽重渲染。
  try {
    return JSON.stringify(value ?? null);
  } catch {
    return String(value ?? '');
  }
}

export function getUiSlotValue(engine: string | undefined, slot: string): UiSlotValue | undefined {
  // 修改原因：后端 ui_slots 值可能是旧字符串，也可能是带 requires_plugin 条件的对象，读取逻辑不能散落在各挂载点。
  // 修改方式：集中按 engine 和 slot 从 window.__uiSlots 取原始值，其他 helper 在此基础上判断类型和条件。
  // 目的：避免每个插槽挂载点都重复理解后端输出格式。
  if (!engine) return undefined;
  return (window as any).__uiSlots?.[engine]?.[slot];
}

export function getEnabledPluginName(entry: EnabledPluginValue): string {
  // 修改原因：provider.preferences.enabled_plugins 现在同时支持旧字符串和结构化对象，requires_plugin 只比较插件名。
  // 修改方式：统一复用 parseEnabledPluginValue 读取名称，和后端 parse_plugin_entry 的匹配口径保持一致。
  // 目的：让 "oai_tier:xxx" 和 {name, params} 两类配置都能正确启用 UI slot。
  return parseEnabledPluginValue(entry).name.trim();
}

export function providerHasEnabledPlugin(enabledPlugins: EnabledPluginValue[] | undefined, pluginName: string | undefined): boolean {
  // 修改原因：带 requires_plugin 的 slot 必须按当前 provider 的 enabled_plugins 判断，不能只看 engine。
  // 修改方式：遍历当前 provider 插件列表，按插件名部分匹配 requires_plugin。
  // 目的：让同一 engine 下不同 provider 可以分别启用或隐藏同一个插件 UI slot。
  if (!pluginName || !Array.isArray(enabledPlugins)) return false;
  return enabledPlugins.some((entry) => getEnabledPluginName(entry) === pluginName);
}

export function getUiSlotScript(engine: string | undefined, slot: string, enabledPlugins?: EnabledPluginValue[]): string | null {
  const slotValue = getUiSlotValue(engine, slot);
  if (!slotValue) return null;
  if (typeof slotValue === 'string') return slotValue;
  if (typeof slotValue === 'object') {
    const requiredPlugin = slotValue.requires_plugin;
    if (requiredPlugin && !providerHasEnabledPlugin(enabledPlugins, requiredPlugin)) return null;
    return typeof slotValue.script === 'string' ? slotValue.script : null;
  }
  return null;
}

export function hasUiSlot(engine: string | undefined, slot: string, enabledPlugins?: EnabledPluginValue[]): boolean {
  return getUiSlotScript(engine, slot, enabledPlugins) !== null;
}

export const RACK_ARC_LENGTH = 350;
export const RACK_GAP_LENGTH = 10;
export const RACK_RING_PATH_LENGTH = RACK_ARC_LENGTH + RACK_GAP_LENGTH;

export function clampRackPercent(value: number | null | undefined): number | null {
  // 修改原因：余额、OAuth quota 和缓存字段都可能为空或越界，直接用于 stroke-dasharray 会造成异常弧线。
  // 修改方式：把不可用值统一收敛为 null，把可用数字裁剪到 0 到 100。
  // 目的：让机房模式圆环在各种数据状态下都能稳定渲染。
  if (value == null) return null;
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return Math.max(0, Math.min(100, n));
}

export function mixRackRgb(from: [number, number, number], to: [number, number, number], ratio: number): string {
  // 修改原因：机房模式要求普通 API Key 圆环使用绿到黄再到红的连续渐变，而旧 getBalanceColor 只返回三档名称。
  // 修改方式：按 0..1 比例在线性 RGB 空间混合两端颜色，供单环 stroke 直接使用。
  // 目的：保留现有百分比计算，同时让卡片圆环颜色变化更细腻。
  const r = Math.max(0, Math.min(1, ratio));
  const mixed = from.map((v, idx) => Math.round(v + (to[idx] - v) * r));
  return `rgb(${mixed[0]}, ${mixed[1]}, ${mixed[2]})`;
}

export function getRackUsageGradientColor(percent: number): string {
  // 修改原因：普通 Key 的机房圆环需要按百分比从绿到黄再到红过渡，不能只使用完整行的背景色。
  // 修改方式：0..50% 在绿色和黄色之间插值，50..100% 在黄色和红色之间插值。
  // 目的：让圆环颜色直接反映当前百分比强度。
  const p = Math.max(0, Math.min(1, percent / 100));
  // 0% = 红色（余额耗尽/危险），100% = 绿色（余额充足/安全）
  if (p < 0.5) return mixRackRgb([239, 68, 68], [234, 179, 8], p / 0.5);
  return mixRackRgb([234, 179, 8], [16, 185, 129], (p - 0.5) / 0.5);
}

export function formatRackKeyLabel(keyObj: ApiKeyObj): string {
  // 修改原因：机房卡片宽度固定，不能直接显示完整 Key，否则会挤占圆环和操作区域。
  // 修改方式：优先显示备注 label，没有备注时显示 Key 前后片段，空 OAuth 行显示占位文字。
  // 目的：保证卡片底部在 96px 宽度内仍能识别 Key。
  const label = keyObj.label?.trim();
  if (label) return label;
  const key = keyObj.key.trim();
  if (!key) return '空账号';
  if (key.length <= 12) return key;
  return `${key.slice(0, 6)}…${key.slice(-4)}`;
}

export function getRackBalanceTextClass(color: 'green' | 'yellow' | 'red' | null): string {
  // 修改原因：普通 Key 中心百分比仍复用 getBalanceColor 的三档语义，避免卡片文字和旧余额判断脱节。
  // 修改方式：把 green/yellow/red 映射成适合深色卡片的文字颜色。
  // 目的：在使用连续圆环颜色的同时保留原有余额分档含义。
  if (color === 'green') return 'text-emerald-600 dark:text-emerald-100';
  if (color === 'yellow') return 'text-yellow-600 dark:text-yellow-100';
  if (color === 'red') return 'text-red-600 dark:text-red-100';
  return 'text-muted-foreground';
}


export const QUOTA_GAUGE_TONE_STROKES: Record<string, string> = {
  // 修改原因：QuotaGauge 提供 tone 后，通用圆环需要在不知道具体渠道的情况下应用基础颜色。
  // 修改方式：将后端约定的 tone 映射为稳定 stroke 色，未指定时继续按余额百分比或双环默认色处理。
  // 目的：让普通余额、插件和 OAuth quota 都能复用 QuotaRings，而不再在渲染层增加渠道分支。
  blue: '#60a5fa',
  green: '#10b981',
  yellow: '#eab308',
  red: '#ef4444',
  gray: '#64748b',
};

export function normalizeQuotaTone(tone?: string | null): string | null {
  // 修改原因：后端或旧 fallback 可能传入空值、大小写不一致或未登记的 tone。
  // 修改方式：统一转小写，并只接受 QuotaRings 支持的标准颜色名。
  // 目的：删除通用 badge 组件后仍让 gauges 的 tone 能安全映射到圆环颜色，不拼接非法样式。
  const key = String(tone || '').toLowerCase();
  return key in QUOTA_GAUGE_TONE_STROKES ? key : null;
}

export function getQuotaGaugeStrokeColor(gauge: QuotaGauge | undefined, percent: number | null, fallback: string): string {
  // 修改原因：单环需要沿用余额渐变，双环需要沿用蓝紫默认色，同时允许后端 tone 覆盖颜色。
  // 修改方式：先读取标准 tone；没有 tone 且有百分比时使用旧余额渐变，否则使用调用方传入的默认色。
  // 目的：在保留 RackSingleRing/RackOAuthRings 视觉习惯的同时接入统一 QuotaGauge。
  const tone = normalizeQuotaTone(gauge?.tone);
  if (tone && QUOTA_GAUGE_TONE_STROKES[tone]) return QUOTA_GAUGE_TONE_STROKES[tone];
  if (percent != null && !fallback) return getRackUsageGradientColor(percent);
  return fallback || (percent == null ? '#334155' : getRackUsageGradientColor(percent));
}

export function getQuotaRingTextClass(gauge: QuotaGauge | undefined, percent: number | null): string {
  // 修改原因：圆环中心文字也需要随通用 tone 或旧余额百分比分档保持可读颜色。
  // 修改方式：优先按 tone 映射文字颜色，没有 tone 时复用旧 getBalanceColor 分档。
  // 目的：让 QuotaRings 替换单环和双环后，文字色不会退回固定样式。
  const tone = normalizeQuotaTone(gauge?.tone);
  if (tone === 'blue') return 'text-sky-700 dark:text-sky-100';
  if (tone === 'green') return 'text-emerald-600 dark:text-emerald-100';
  if (tone === 'yellow') return 'text-yellow-600 dark:text-yellow-100';
  if (tone === 'red') return 'text-red-600 dark:text-red-100';
  if (tone === 'gray') return 'text-muted-foreground';
  return getRackBalanceTextClass(getBalanceColor(percent));
}

export function getCurrencySymbol(currency: string | null | undefined): string {
  if (!currency) return '';
  const map: Record<string, string> = { USD: '$', CNY: '¥', EUR: '€', GBP: '£', JPY: '¥' };
  return map[currency.toUpperCase()] || `${currency} `;
}

export function formatQuotaAmount(available: number | null | undefined, unit: string | null | undefined): string {
  if (available == null) return '—';
  const prefix = getCurrencySymbol(unit);
  if (available >= 1000) return `${prefix}${formatCompactNumber(available)}`;
  return `${prefix}${available.toFixed(2)}`;
}

export function getQuotaRingText(gauge: QuotaGauge | undefined, percent: number | null): string {
  // quota 模式：显示金额
  if (gauge?.display_mode === 'quota') return formatQuotaAmount(gauge.available, gauge.unit);
  // amount 模式：优先 displayLabel（紧凑金额），否则百分比
  if (gauge?.displayLabel) return gauge.displayLabel;
  return percent == null ? '—' : `${Math.round(percent)}%`;
}
