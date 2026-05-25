/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, KeyboardEvent, ClipboardEvent, DragEvent } from 'react';
import { createPortal } from 'react-dom';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import { toastSuccess, toastError, toastWarning, fmtErr } from '../components/Toast';
import {
  Plus, Edit, Brain, Trash2, ArrowRight, RefreshCw,
  Server, X, CheckCircle2, Settings2, Copy, ToggleRight, ToggleLeft,
  Folder, Puzzle, Network, CopyCheck, Power, Files, Play,
  Search, Check, BarChart3, Wallet, XCircle, Link2, GripVertical, ChevronUp, ChevronDown,
  ClipboardPaste, LogIn, Download, LayoutList, LayoutGrid
} from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import * as Switch from '@radix-ui/react-switch';
import { InterceptorSheet } from '../components/InterceptorSheet';
import { ChannelTestDialog } from '../components/ChannelTestDialog';
import { ApiKeyTestDialog } from '../components/ApiKeyTestDialog';
import { ChannelAnalyticsSheet } from '../components/ChannelAnalyticsSheet';
import { ProviderLogo } from '../components/ProviderLogos';
import {
  buildProviderListItems,
  buildVirtualProviderEntries,
  buildVirtualProviderPanelItems,
  buildVirtualRouteTestProvider,
  buildVirtualRoutingProviderItems,
  getProviderWeight,
  summarizeVirtualChain,
} from '../lib/virtualModels';
import {
  formatKeyRuleKeywordsInput,
  formatKeyRuleStatusInput,
  getKeyRuleRetryMode,
  parseKeyRuleKeywordsInput,
  parseKeyRuleStatusInput,
  sanitizeKeyRulesForSave,
  setKeyRuleRetryMode,
  type KeyRuleRetryMode,
} from '../lib/keyRules';

// ========== DeferredInput ==========
// 本地 state 暂存输入，blur/Enter 时才写回外部，避免 parse+trim 吞空格
function DeferredInput({ value, onCommit, ...props }: Omit<React.InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'onBlur' | 'onKeyDown' | 'value'> & { value: string; onCommit: (v: string) => void }) {
  const [local, setLocal] = useState(value);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current !== document.activeElement) setLocal(value); }, [value]);
  return <input ref={ref} {...props} type="text" value={local} onChange={e => setLocal(e.target.value)} onBlur={() => onCommit(local)} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); onCommit(local); (e.target as HTMLInputElement).blur(); } }} />;
}

// 修改原因：Key 备注遮罩原先使用固定 30% 宽度，短备注会浪费输入空间，长备注又会显示不全。
// 修改方式：把备注覆盖层和 Key 输入层封装到独立组件中，用 ref 与 useLayoutEffect 测量真实渲染宽度，并直接写入 DOM mask 样式，避免通过 state 触发重绘闪烁。
// 目的：让 Key 输入内容的透明区域随备注文字宽度变化，同时保留右侧标签渐隐和无备注时的旧 60% 标签遮罩。
function KeyLabelOverlay({ label, hasTag, isFocused, children }: { label?: string; hasTag: boolean; isFocused: boolean; children: React.ReactNode }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const keyInputMaskRef = useRef<HTMLDivElement | null>(null);
  const labelSpanRef = useRef<HTMLSpanElement | null>(null);

  const setMaskImage = useCallback((el: HTMLElement, mask: string) => {
    el.style.maskImage = mask;
    el.style.setProperty('-webkit-mask-image', mask);
  }, []);

  const clearMaskImage = useCallback((el: HTMLElement) => {
    el.style.maskImage = '';
    el.style.removeProperty('-webkit-mask-image');
  }, []);

  const applyMasks = useCallback(() => {
    const keyInputMaskEl = keyInputMaskRef.current;
    if (!keyInputMaskEl) return;

    if (isFocused) {
      clearMaskImage(keyInputMaskEl);
      if (labelSpanRef.current) clearMaskImage(labelSpanRef.current);
      return;
    }

    const labelSpanEl = labelSpanRef.current;
    const containerEl = containerRef.current;
    if (label && labelSpanEl && containerEl && containerEl.clientWidth > 0) {
      const labelWidth = labelSpanEl.scrollWidth;
      const containerWidth = containerEl.clientWidth;
      const labelPct = Math.min(95, (labelWidth / containerWidth) * 100 + 5);
      const keyMask = hasTag
        ? `linear-gradient(to right, transparent 0%, transparent ${labelPct - 5}%, black ${labelPct + 15}%, black ${Math.max(65, labelPct + 15)}%, transparent 100%)`
        : `linear-gradient(to right, transparent 0%, transparent ${labelPct - 5}%, black ${labelPct + 15}%, black 100%)`;
      setMaskImage(keyInputMaskEl, keyMask);
      // label 不加 mask — 尽量完整显示，超出容器时靠 overflow-hidden 自然截断
      clearMaskImage(labelSpanEl);
      return;
    }

    if (labelSpanEl) clearMaskImage(labelSpanEl);
    if (hasTag) {
      setMaskImage(keyInputMaskEl, 'linear-gradient(to right, black 0%, black 60%, transparent 100%)');
    } else {
      clearMaskImage(keyInputMaskEl);
    }
  }, [clearMaskImage, hasTag, isFocused, label, setMaskImage]);

  useLayoutEffect(() => {
    applyMasks();

    const containerEl = containerRef.current;
    if (!containerEl || typeof ResizeObserver === 'undefined') return;

    const resizeObserver = new ResizeObserver(() => applyMasks());
    resizeObserver.observe(containerEl);
    if (labelSpanRef.current) resizeObserver.observe(labelSpanRef.current);

    return () => resizeObserver.disconnect();
  }, [applyMasks]);

  const bindLabelSpan = useCallback((el: HTMLSpanElement | null) => {
    labelSpanRef.current = el;
    applyMasks();
  }, [applyMasks]);

  const bindKeyInputMask = useCallback((el: HTMLDivElement | null) => {
    keyInputMaskRef.current = el;
    applyMasks();
  }, [applyMasks]);

  return (
    <div ref={containerRef} className="flex-1 min-w-0 relative z-[2]">
      {label && !isFocused && (
        <div className="absolute inset-y-0 left-0 right-0 flex items-center pointer-events-none z-[3] select-none overflow-hidden">
          <span
            ref={bindLabelSpan}
            className="text-sm leading-5 font-mono font-semibold text-amber-600 dark:text-amber-400 whitespace-nowrap"
          >
            {label}
          </span>
        </div>
      )}

      <div ref={bindKeyInputMask}>{children}</div>
    </div>
  );
}

// ========== Types ==========
interface ApiKeyObj {
  key: string;
  disabled: boolean;
  label?: string;
}

interface ModelMapping {
  from: string;
  to: string;
}

interface HeaderEntry {
  key: string;
  value: string;
}

interface SubChannelFormData {
  engine: string;
  models: string[];
  mappings: ModelMapping[];
  preferences: Record<string, any>;
  enabled?: boolean;
  remark?: string;
  base_url?: string;
  // 修改原因：OAuth 子渠道在完整编辑时也会复用同一份表单结构，需要保留独立 token endpoint 字段。
  // 修改方式：在子渠道表单数据中加入可选 token_url，并在序列化时只保存显式填写的值。
  // 目的：避免子渠道编辑时丢失用户配置的 OAuth token exchange/refresh 地址。
  token_url?: string;
  model_prefix?: string;
  _collapsed?: boolean;
}

interface ProviderFormData {
  provider: string;
  remark: string;
  engine: string;
  base_url: string;
  // 修改原因：OAuth 渠道的 API 地址和 token endpoint 需要分开保存，不能继续复用 base_url。
  // 修改方式：在主渠道表单数据中加入 token_url，保存时随 provider payload 一起提交。
  // 目的：编辑已有渠道可以回显 token_url，新建或保存渠道时也能持久化该字段。
  token_url: string;
  api_keys: ApiKeyObj[];
  model_prefix: string;
  enabled: boolean;
  groups: string[];
  models: string[];
  mappings: ModelMapping[];
  // 注意：preferences 允许包含任意插件的 per-provider 配置。
  // 因此这里用 Record<string, any>，避免为每个插件都在 Channels 页面硬编码字段。
  preferences: Record<string, any>;
  sub_channels: SubChannelFormData[];
  // 修改原因：复制 OAuth 渠道保存后需要知道来源 provider 才能复制后端 token state。
  // 修改方式：在表单态保留一个以下划线开头的临时字段，正式保存 payload 手工组装时不写入该字段。
  // 目的：让复制流程能调用后端 copy-provider API，同时避免把前端临时标记持久化到 api.yaml。
  _copiedFrom?: string;
}

type UiSlotValue = string | { script?: string; requires_plugin?: string };

interface ChannelOption {
  id: string;
  type_name: string;
  default_base_url: string;
  default_token_url?: string;
  description?: string;
  // 修改原因：后端渠道注册表新增 OAuth 标记，前端应优先使用服务端返回值判断管理 UI 分支。
  // 修改方式：在 ChannelOption 中加入可选 is_oauth 字段，兼容旧后端未返回该字段的情况。
  // 目的：余额按钮和配置面板不再只依赖硬编码 OAuth 引擎集合。
  is_oauth?: boolean;
  // 修改原因：后端会把需要 provider 插件开关的 slot 输出为 {script, requires_plugin}，不再只返回脚本字符串。
  // 修改方式：把 ui_slots 值类型扩展为 UiSlotValue，导入弹窗仍只把纯字符串 import_placeholder 当文本使用。
  // 目的：让前端既兼容旧无条件 slot，又能按当前 provider.enabled_plugins 判断插件门控 slot。
  ui_slots?: Record<string, UiSlotValue>;
  source?: string;
}

interface PluginOption {
  plugin_name: string;
  version: string;
  description: string;
  enabled: boolean;
  request_interceptors: any[];
  response_interceptors: any[];
  metadata?: any;
}

// 修改原因：前端需要编辑 preferences.virtual_models 中的优先级链条结构。
// 修改方式：为虚拟模型配置和链条节点补充本页面内使用的类型定义。
// 目的：让列表展示、弹窗编辑和保存 payload 使用同一份数据形状。
interface VirtualModelChainNode {
  type: 'model' | 'channel';
  value: string;
  model?: string;
}

interface VirtualModelConfig {
  enabled: boolean;
  chain: VirtualModelChainNode[];
}

// 修改原因：新的虚拟模型画布需要展示渠道模型的对外名和上游名。
// 修改方式：把 provider.model 中的 string 与 {upstream: alias} 统一整理为页面可直接渲染的结构。
// 目的：拖拽模型节点、渠道节点模型选择和链条说明都使用同一套模型名称解释。
interface ProviderModelOption {
  displayName: string;
  upstreamName: string;
  hasMapping: boolean;
}

// 修改原因：原生 Drag and Drop API 只能通过字符串传递拖拽数据。
// 修改方式：为左侧模型、左侧渠道和链条内部节点定义统一 payload 类型。
// 目的：drop 处理函数可以区分新建节点和链条排序，避免误把外部拖入当作内部排序。
type VirtualDragPayload =
  | { source: 'panel-model'; modelName: string }
  | { source: 'panel-channel'; providerName: string }
  | { source: 'chain-node'; virtualName: string; fromIndex: number };

const SCHEDULE_ALGORITHMS = [
  { value: 'round_robin', label: '轮询 (Round Robin)' },
  { value: 'fixed_priority', label: '固定优先级 (Fixed)' },
  { value: 'random', label: '随机 (Random)' },
  { value: 'smart_round_robin', label: '智能轮询 (Smart)' },
  { value: 'sticky_ip', label: 'IP 粘滞 (Sticky IP)' },
];

function readBooleanPreference(value: any): boolean {
  // 修改原因：后端新增 pool_sharing 布尔开关，旧配置中也可能存在字符串形式的布尔值。
  // 修改方式：统一把 true/1/yes/on 转为 true，其余值按 JavaScript 布尔语义处理。
  // 目的：让表单初始化时能够稳定显示共享路由池开关的实际状态。
  if (typeof value === 'string') return ['true', '1', 'yes', 'on'].includes(value.trim().toLowerCase());
  return Boolean(value);
}

function serializeChannelPreferences(preferences: Record<string, any>): Record<string, any> {
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
interface QuotaGauge {
  // 修改原因：P2 阶段前端圆环需要消费后端统一 gauges，而不是继续按 OAuth 或普通余额写两套展示字段。
  // 修改方式：在页面内补齐 QuotaGauge 类型，保留后端可选字段，并额外允许 legacy fallback 写入 displayLabel。
  // 目的：让 QuotaRings 能直接渲染新 API 字段，也能兼容旧 BalanceResult 计算出的兜底圆环。
  id: string;
  label: string;
  role?: string | null;
  percent?: number | null;
  total?: number | null;
  available?: number | null;
  used?: number | null;
  tone?: string | null;
  resets_at?: string | null;
  unit?: string | null;
  display_mode?: 'percent' | 'amount' | 'quota';
  displayLabel?: string | null;
}

interface RowQuota {
  // 修改原因：tier/plan 等标签必须由 quota_display 插槽决定，RowQuota 不应再承载可直接渲染的 badge 列表。
  // 修改方式：RowQuota 只保留圆环需要的 gauges，BalanceResult 原始字段仍会原样传给 slot 脚本。
  // 目的：删除通用 badge 展示路径，避免通用前端再次硬编码展示插件或渠道标签。
  gauges: QuotaGauge[];
}

interface BalanceResult {
  supported: boolean;
  status?: string | null;
  value_type?: 'amount' | 'percent' | 'quota';
  total?: number | null;
  used?: number | null;
  available?: number | null;
  percent?: number | null;
  currency?: string | null;
  expires_at?: string | null;
  // 修改原因：P0 后端会在旧 BalanceResult 旁追加统一额度 UI 字段，前端 P2 需要优先读取 gauges。
  // 修改方式：在 BalanceResult 中保留 gauges、badges、metrics 和 extensions 的兼容字段，但渲染层不再消费 badges。
  // 目的：slot 脚本仍能读取后端原始扩展数据，通用前端不再硬编码 tier 或 plan 标签。
  gauges?: QuotaGauge[];
  badges?: any[];
  metrics?: Record<string, any>;
  extensions?: Record<string, any>;
  // 修改原因：双额度不再只属于 OAuth，普通 balance 结果也可能返回两个标准 quota 百分比。
  // 修改方式：把 quota_inner、quota_outer 和逐账号 results 保留在通用 BalanceResult 中。
  // 目的：让同一条渲染路径同时服务 OAuth 账号和普通 Key 的双弧展示。
  quota_inner?: number | null;
  quota_outer?: number | null;
  results?: Record<string, BalanceResult>;
  // 修改原因：oai_tier 会通过 balance_enricher 在普通余额结果中补充被动检测到的 OpenAI Tier 信息。
  // 修改方式：在通用 BalanceResult 类型中保留 tier、tpm、rpm 和检测元数据字段，并只交给 slot 脚本展示。
  // 目的：兼容旧余额结果，同时避免 Channels.tsx 直接把 Tier 渲染成通用标签。
  tier?: string | null;
  tpm?: number | null;
  rpm?: number | null;
  tier_detected_at?: number | null;
  tier_model?: string | null;
  raw?: any;
  error?: string | null;
}

// 修改原因：双弧模板只需要两个标准 quota 百分比和可选原始数据，不应绑定具体渠道字段。
// 修改方式：保留轻量结构供 OAuth 账号和普通 balance 结果共同归一化使用。
// 目的：为 RackOAuthRings、QuotaBorderOverlay 和 quota_display 插槽提供统一输入。
interface OAuthQuota {
  quota_inner?: number;
  quota_outer?: number;
  raw?: any;
}

function formatCompactNumber(n: number): string {
  // 修改原因：完整列表行需要保留 available/total 原始语义，但大额数字直接显示会挤压输入区域。
  // 修改方式：按用户指定的 B、M、K 阶梯缩写数字，并统一保留一位小数。
  // 目的：让列表模式显示 178.8M / 250.0M 这类短文本，而不是改成百分比。
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(1);
}

function getBalancePercent(b: BalanceResult): number | null {
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

function getBalanceColor(pct: number | null): 'green' | 'yellow' | 'red' | null {
  if (pct == null) return null;
  if (pct >= 50) return 'green';
  if (pct >= 20) return 'yellow';
  return 'red';
}

function getBalanceLabel(b: BalanceResult): string | null {
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

function getBalanceCompactLabel(b: BalanceResult): string | null {
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

function normalizeQuotaPct(value: any): number | undefined {
  // 修改原因：双额度来源既可能是后端数字，也可能是缓存字符串，直接参与弧线长度计算会产生 NaN。
  // 修改方式：统一把空值转为 undefined，把可解析数字裁剪到 0 到 100。
  // 目的：保证通用双弧只接收稳定百分比，渠道专属计算由后端或 ui_slots 完成。
  if (value == null || value === '') return undefined;
  const n = Number(value);
  if (!Number.isFinite(n)) return undefined;
  return Math.max(0, Math.min(100, n));
}

function getQuotaFromSource(source: any, rawValue?: any): OAuthQuota | null {
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

function getOAuthQuota(account: any): OAuthQuota | null {
  // 修改原因：OAuth 账号状态会把后端 fetch_quota 的 raw 缓存在 quota_raw 中，但标准额度字段仍与普通 balance 相同。
  // 修改方式：复用通用双额度读取 helper，并把账号 raw 显式传给渠道插槽。
  // 目的：OAuth 渠道保留自定义额度展示能力，同时不再维护渠道专属字段猜测。
  return getQuotaFromSource(account, account?.quota_raw ?? account?.raw);
}

function normalizeOAuthAccountStateMap(accounts: Record<string, any> | null | undefined): Record<string, any> {
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

function getBalanceQuota(bal: BalanceResult | undefined): OAuthQuota | null {
  // 修改原因：普通 Key 的 balance 结果如果返回 quota_inner 和 quota_outer，也应进入默认双弧展示。
  // 修改方式：只从 BalanceResult 顶层标准字段读取双额度，不把普通 raw 响应误判成可渲染 quota。
  // 目的：让非 OAuth Key 复用 RackOAuthRings 和 QuotaBorderOverlay，同时避免所有普通余额都出现空双弧。
  if (!bal || !bal.supported || bal.error) return null;
  return getQuotaFromSource(bal);
}

function getQuotaPairFromGauges(gauges: QuotaGauge[]): OAuthQuota | null {
  // 修改原因：QuotaBorderOverlay 和旧 quota_display 插槽仍消费 quota_inner/quota_outer，但 P2 行模型已统一成 gauges。
  // 修改方式：从前两个 gauge 提取百分比并映射为 inner/outer，超过两个 gauge 时和 QuotaRings 一样只取前两个。
  // 目的：保留默认双额度边框和已有渠道插槽兼容性，同时不在渲染层重新按 OAuth 分支取值。
  if (!Array.isArray(gauges) || gauges.length < 2) return null;
  const quota_inner = normalizeQuotaPct(gauges[0]?.percent);
  const quota_outer = normalizeQuotaPct(gauges[1]?.percent);
  if (quota_inner == null && quota_outer == null) return null;
  return { quota_inner, quota_outer };
}

function buildRowQuota(bal: BalanceResult | undefined, oauthAccount: any, isOAuthEngine: boolean): RowQuota {
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

function buildRowQuotaSlotData(bal: BalanceResult | undefined, oauthAccount: any, rowQuota: RowQuota): any {
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

function withRackCompactBalanceFallback(gauges: QuotaGauge[], bal: BalanceResult | undefined): QuotaGauge[] {
  // 修改原因：旧 amount 余额没有 gauges 时，机房卡片圆心不能继续显示完整 available/total 长文本。
  // 修改方式：只在 legacy balance fallback 的单 gauge 上替换 displayLabel 为 getBalanceCompactLabel，后端新 gauges 不改写。
  // 目的：保留 P2 统一圆环入口，同时延续机房卡片的紧凑显示策略。
  if (bal?.gauges?.length || !bal || gauges.length !== 1 || gauges[0]?.id !== 'balance') return gauges;
  const compactLabel = bal.value_type !== 'percent' ? getBalanceCompactLabel(bal) : null;
  if (!compactLabel) return gauges;
  return [{ ...gauges[0], displayLabel: compactLabel }];
}

function sortProvidersByWeight(list: any[]): any[] {
  // 修改原因：保存后需要从后端重新获取 providers，并继续保持页面原有的权重降序显示。
  // 修改方式：把原先散落在加载和保存逻辑中的排序规则抽成纯 helper。
  // 目的：避免刷新、保存和权重更新使用不同排序实现导致列表顺序不一致。
  return [...list].sort((a, b) => {
    const weightA = a.preferences?.weight ?? a.weight ?? 0;
    const weightB = b.preferences?.weight ?? b.weight ?? 0;
    return weightB - weightA;
  });
}

function buildProviderApiPath(providerId: string): string {
  // 修改原因：provider 名可能包含空格、斜杠或其他需要转义的字符，直接拼 URL 会请求错误路径。
  // 修改方式：所有单渠道 PUT/DELETE 统一通过 encodeURIComponent 生成路径。
  // 目的：保证前端调用 /v1/providers/{provider_id} 时按真实渠道名定位。
  return `/v1/providers/${encodeURIComponent(providerId)}`;
}

const BALANCE_FILL_COLORS = {
  green: 'linear-gradient(90deg, rgba(16,185,129,0.15) 0%, rgba(16,185,129,0.04) 100%)',
  yellow: 'linear-gradient(90deg, rgba(234,179,8,0.18) 0%, rgba(234,179,8,0.04) 100%)',
  red: 'linear-gradient(90deg, rgba(239,68,68,0.18) 0%, rgba(239,68,68,0.04) 100%)',
};

const TAG_CLASSES = {
  green: 'text-emerald-400 bg-emerald-500/12',
  yellow: 'text-yellow-400 bg-yellow-500/12',
  red: 'text-red-400 bg-red-500/12',
};

// ── 格式化倒计时 ──
function formatCountdown(seconds: number) {
  if (seconds <= 0) return '即将恢复';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// ── SVG 圆角矩形路径生成 ──
function buildRoundRectPath(x: number, y: number, w: number, h: number, r: number) {
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
function buildTopHalfPath(x: number, y: number, w: number, h: number, r: number) {
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
function buildBottomHalfPath(x: number, y: number, w: number, h: number, r: number) {
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
function QuotaBorderOverlay({ quotaInner, quotaOuter }: {
  quotaInner?: number | null; quotaOuter?: number | null;
}) {
  const selfRef = useRef<HTMLDivElement>(null);
  const [svgViewBox, setSvgViewBox] = useState('');
  const [topPath, setTopPath] = useState('');
  const [bottomPath, setBottomPath] = useState('');

  useEffect(() => {
    const el = selfRef.current;
    if (!el) return;
    const update = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      if (w > 0 && h > 0) {
        setSvgViewBox(`0 0 ${w} ${h}`);
        setTopPath(buildTopHalfPath(1, 1, w - 2, h - 2, 7));
        setBottomPath(buildBottomHalfPath(1, 1, w - 2, h - 2, 7));
      }
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const qInner = quotaInner ?? 0;
  const qOuter = quotaOuter ?? 0;
  return (
    <div ref={selfRef} className="absolute inset-0 pointer-events-none z-[1]" style={{ overflow: 'visible' }}>
      {svgViewBox && (
        <svg className="absolute inset-0 w-full h-full" viewBox={svgViewBox} style={{ overflow: 'visible' }}>
          <title>{`inner: ${quotaInner ?? '?'}% \u00b7 outer: ${quotaOuter ?? '?'}%`}</title>
          {quotaInner != null && topPath && (
            <path d={topPath} pathLength={100} fill="none" stroke="#3b82f6" strokeWidth={2} strokeLinecap="round"
              style={{ strokeDasharray: `${qInner} 100`, strokeDashoffset: 0, transition: 'stroke-dasharray 0.5s ease' }} />
          )}
          {quotaOuter != null && bottomPath && (
            <path d={bottomPath} pathLength={100} fill="none" stroke="#8b5cf6" strokeWidth={2} strokeLinecap="round"
              style={{ strokeDasharray: `${qOuter} 100`, strokeDashoffset: 0, transition: 'stroke-dasharray 0.5s ease' }} />
          )}
        </svg>
      )}
    </div>
  );
}

// 兼容 QuotaArcs 调用点 — 用最小百分比的文字 tag
const QuotaArcs = ({ quotaInner, quotaOuter }: { quotaInner?: number; quotaOuter?: number }) => {
  if (quotaInner == null && quotaOuter == null) return null;
  const pct = Math.min(quotaInner ?? 100, quotaOuter ?? 100);
  const color = pct > 50 ? 'bg-emerald-500/15 text-emerald-500' : pct > 20 ? 'bg-amber-500/15 text-amber-600' : 'bg-red-500/15 text-red-500';
  return (
    <span
      className={`flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] cursor-default ${color}`}
      title={`inner: ${quotaInner ?? '?'}% · outer: ${quotaOuter ?? '?'}%`}
    >
      {Math.round(pct)}%
    </span>
  );
};

// ── 渠道自定义 UI 插槽 ──
// 修改原因：前端不能只支持 quota_display，也不能把渠道专属余额字段写死在 Channels.tsx 中。
// 修改方式：把原单一额度插槽泛化为 UiSlot，按 engine + slot 缓存渠道注册的内联 JavaScript，并把插槽上下文交给渠道脚本渲染。
// 目的：让 Channels.tsx 只提供通用挂载点，余额条、标签、汇总和边框等渠道差异都由 channel.py 的 ui_slots 声明。
const uiSlotCache: Record<string, ((ctx: any) => void) | null> = {};

function serializeSlotValue(value: any): string {
  // 修改原因：插槽 data/context 多数来自 React state，每次 render 都可能产生新对象引用，直接依赖对象会导致脚本反复执行。
  // 修改方式：把值序列化成内容签名作为 effect 依赖，实际执行时仍从 ref 读取最新值。
  // 目的：减少轮询、倒计时和输入聚焦造成的无意义插槽重渲染。
  try {
    return JSON.stringify(value ?? null);
  } catch {
    return String(value ?? '');
  }
}

function getUiSlotValue(engine: string | undefined, slot: string): UiSlotValue | undefined {
  // 修改原因：后端 ui_slots 值可能是旧字符串，也可能是带 requires_plugin 条件的对象，读取逻辑不能散落在各挂载点。
  // 修改方式：集中按 engine 和 slot 从 window.__uiSlots 取原始值，其他 helper 在此基础上判断类型和条件。
  // 目的：避免每个插槽挂载点都重复理解后端输出格式。
  if (!engine) return undefined;
  return (window as any).__uiSlots?.[engine]?.[slot];
}

function getEnabledPluginName(entry: string): string {
  // 修改原因：provider.preferences.enabled_plugins 支持 "plugin:options" 格式，requires_plugin 只比较插件名。
  // 修改方式：取冒号前的名称并去掉空白，和后端 parse_plugin_entry 的匹配口径保持一致。
  // 目的：让 "oai_tier:xxx" 这类配置也能正确启用 oai_tier 的 UI slot。
  return String(entry || '').split(':')[0].trim();
}

function providerHasEnabledPlugin(enabledPlugins: string[] | undefined, pluginName: string | undefined): boolean {
  // 修改原因：带 requires_plugin 的 slot 必须按当前 provider 的 enabled_plugins 判断，不能只看 engine。
  // 修改方式：遍历当前 provider 插件列表，按插件名部分匹配 requires_plugin。
  // 目的：让同一 engine 下不同 provider 可以分别启用或隐藏同一个插件 UI slot。
  if (!pluginName || !Array.isArray(enabledPlugins)) return false;
  return enabledPlugins.some((entry) => getEnabledPluginName(entry) === pluginName);
}

function getUiSlotScript(engine: string | undefined, slot: string, enabledPlugins?: string[]): string | null {
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

function hasUiSlot(engine: string | undefined, slot: string, enabledPlugins?: string[]): boolean {
  return getUiSlotScript(engine, slot, enabledPlugins) !== null;
}

const UiSlot = ({ engine, slot, data, context, className, element = 'span', fallbackText, enabledPlugins }: { engine: string; slot: string; data: any; context?: Record<string, any>; className?: string; element?: 'span' | 'div'; fallbackText?: string; enabledPlugins?: string[] }) => {
  const ref = useRef<HTMLElement | null>(null);
  const [loaded, setLoaded] = useState(false);
  const dataRef = useRef(data);
  const contextRef = useRef(context);
  dataRef.current = data;
  contextRef.current = context;
  const dataKey = useMemo(() => serializeSlotValue(data), [data]);
  const contextKey = useMemo(() => serializeSlotValue(context), [context]);
  // 修改原因：enabledPlugins 数组引用可能随表单更新重建，直接放入 effect 依赖会导致无意义重跑。
  // 修改方式：和 data/context 一样序列化为内容签名，只在插件列表内容变化时重新检查 requires_plugin。
  // 目的：让插件开关变化能正确影响 UiSlot，同时避免普通输入聚焦造成重复加载。
  const enabledPluginsKey = useMemo(() => serializeSlotValue(enabledPlugins || []), [enabledPlugins]);

  const bindRef = useCallback((node: HTMLElement | null) => {
    ref.current = node;
  }, []);

  useEffect(() => {
    if (!ref.current) return;
    const el = ref.current;
    const cacheKey = `${engine}:${slot}`;

    const run = async () => {
      try {
        // 修改原因：同一渠道的同一插槽会被多个 Key 行重复使用，重复 import 会浪费资源并增加闪烁概率。
        // 修改方式：按 `${engine}:${slot}` 缓存模块函数，命中时直接复用已加载的 render 函数。
        // 目的：保持列表渲染轻量，同时支持同一渠道注册多个彼此独立的插槽。
        if (fallbackText !== undefined) el.textContent = fallbackText;
        // 修改原因：后端可能返回 {script, requires_plugin}，且同一个 engine 的不同 provider 插件开关不同。
        // 修改方式：先按 enabledPlugins 提取可执行脚本，再决定是否使用缓存或动态 import。
        // 目的：避免未启用插件的 provider 误用已经缓存的同 engine slot 脚本。
        const slotValue = getUiSlotValue(engine, slot);
        const jsSrc = getUiSlotScript(engine, slot, enabledPlugins);
        if (!jsSrc) {
          if (!slotValue || typeof slotValue === 'string' || !slotValue.requires_plugin) uiSlotCache[cacheKey] = null;
          setLoaded(true);
          return;
        }
        if (cacheKey in uiSlotCache) {
          const fn = uiSlotCache[cacheKey];
          // 修改原因：新插槽脚本通过 ctx.context?.mode 区分完整行和机房卡片，但旧脚本仍直接读取 ctx.account 等扁平字段。
          // 修改方式：调用脚本时同时保留扁平展开字段，并额外传入原始 context 对象。
          // 目的：让 mode 分支生效，同时保持已有渠道和插件脚本兼容。
          if (fn) fn({ el, data: dataRef.current, ...(contextRef.current ?? {}), context: contextRef.current });
          setLoaded(true);
          return;
        }

        // 修改原因：插槽脚本来自渠道元数据，前端只负责按 slot 名加载，不应固定读取 quota_display。
        // 修改方式：从字符串 slot 或对象 slot.script 取内联 JS，再通过 Blob URL dynamic import 加载默认导出。
        // 目的：新增 key_border、key_background、balance_summary 等插槽时不再修改加载器。

        const blob = new Blob([jsSrc], { type: 'application/javascript' });
        const url = URL.createObjectURL(blob);
        try {
          const mod = await import(/* @vite-ignore */ url);
          const fn = mod.default || mod;
          uiSlotCache[cacheKey] = typeof fn === 'function' ? fn : null;
          // 修改原因：首次动态加载插槽脚本时也必须提供 nested context，否则新加载路径和缓存命中路径行为不一致。
          // 修改方式：与缓存命中分支一样传入 data、展开后的 context 字段，以及完整 context 对象。
          // 目的：保证 quota_display、key_background 和 key_border 都能稳定读取 ctx.context?.mode。
          if (uiSlotCache[cacheKey]) uiSlotCache[cacheKey]!({ el, data: dataRef.current, ...(contextRef.current ?? {}), context: contextRef.current });
        } finally {
          URL.revokeObjectURL(url);
        }
        setLoaded(true);
      } catch (e) {
        console.warn(`[UiSlot] Failed to load UI slot ${slot} for ${engine}:`, e);
        uiSlotCache[cacheKey] = null;
        // 修改原因：插槽脚本失败时继续显示旧 DOM 会误导用户，以为渠道仍在正常渲染。
        // 修改方式：有默认文本的插槽恢复默认文本，没有默认文本的插槽清空内容和 title。
        // 目的：让失败插槽安全静默降级，并把排查信息留在控制台。
        el.textContent = fallbackText ?? '';
        el.removeAttribute('title');
        setLoaded(true);
      }
    };

    run();
  }, [engine, slot, dataKey, contextKey, fallbackText, enabledPluginsKey]);

  if (element === 'div') {
    return <div ref={bindRef as React.RefCallback<HTMLDivElement>} data-loaded={loaded ? 'true' : 'false'} className={className} />;
  }
  return <span ref={bindRef as React.RefCallback<HTMLSpanElement>} data-loaded={loaded ? 'true' : 'false'} className={className} />;
};

// ── 冷却中 Key 行组件（SVG 边框进度） ──
function CoolingKeyRow({ idx, keyObj, remainSec, totalDuration, focused, onFocus, onBlur, onRecover, onToggle, onTest, onDelete, onLabelChange }: {
  idx: number; keyObj: { key: string; disabled: boolean; label?: string }; remainSec: number; totalDuration: number;
  focused: boolean;
  onFocus: () => void; onBlur: () => void;
  onRecover: () => void; onToggle: () => void; onTest: () => void; onDelete: () => void; onLabelChange?: (label: string) => void;
}) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [svgViewBox, setSvgViewBox] = useState('');
  const [pathD, setPathD] = useState('');

  // 计算进度百分比
  const progress = totalDuration > 0 ? Math.max(0, Math.min(100, (remainSec / totalDuration) * 100)) : 0;

  // 初始化 & resize 时更新 SVG path
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;
    const update = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      setSvgViewBox(`0 0 ${w} ${h}`);
      setPathD(buildRoundRectPath(1, 1, w - 2, h - 2, 7));
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // SVG stroke-dasharray / dashoffset
  const dasharray = progress > 0 ? `${progress} 100` : '0 100';
  const dashoffset = progress > 0 ? `${-(100 - progress)}` : '0';

  return (
    <div ref={wrapperRef} className="relative rounded-lg" style={{ isolation: 'isolate' }}>
      {/* SVG 边框进度 */}
      {!focused && pathD && (
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none z-[1]"
          viewBox={svgViewBox}
          style={{ overflow: 'visible' }}
        >
          <path
            d={pathD}
            pathLength={100}
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            className="text-red-500"
            style={{ strokeDasharray: dasharray, strokeDashoffset: dashoffset, transition: 'stroke-dasharray 1s linear, stroke-dashoffset 1s linear' }}
          />
        </svg>
      )}
      {/* 内容 */}
      <div className={`relative flex items-center gap-2 px-3 py-2 rounded-lg border-2 transition-colors ${focused ? 'border-blue-500 bg-muted/50' : 'border-border bg-background dark:bg-card'}`}>
        <span className="text-xs text-muted-foreground w-4 text-right relative z-[2]">{idx + 1}</span>
        {keyObj.label && !focused && (
          <span className="text-sm font-mono font-semibold text-amber-600 dark:text-amber-400 truncate max-w-[30%] flex-shrink-0 relative z-[2]">{keyObj.label}</span>
        )}
        <div className="flex-1 min-w-0 relative z-[2]">
          <input
            type="text" value={keyObj.key || ''} readOnly placeholder="sk-..."
            onFocus={onFocus} onBlur={e => { if (!wrapperRef.current?.contains(e.relatedTarget as Node)) onBlur(); }}
            className={`w-full bg-transparent border-none text-sm font-mono outline-none ${focused ? 'text-foreground' : 'text-red-400 dark:text-red-300 line-through decoration-red-500/40'}`}
          />
          {/* 倒计时叠加 */}
          {!focused && (
            <span className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 text-[11px] font-semibold font-mono text-red-500 bg-background/85 dark:bg-card/85 rounded px-2 py-0.5 pointer-events-none z-[3]">
              {formatCountdown(remainSec)}
            </span>
          )}
        </div>
        {!focused && (
          <button onClick={onRecover} className="text-[11px] px-2 py-0.5 rounded border border-emerald-500/50 bg-emerald-500/20 text-emerald-400 font-medium hover:bg-emerald-500/30 hover:border-emerald-400 cursor-pointer flex-shrink-0 relative z-[2] transition-colors">恢复</button>
        )}
        <div className="actions flex items-center gap-1 flex-shrink-0 relative z-[2]">
          <button onClick={onToggle} className="text-muted-foreground" title="禁用"><ToggleRight className="w-5 h-5" /></button>
          <button onClick={onTest} disabled={!keyObj.key.trim()} className="text-blue-600 dark:text-blue-400 disabled:opacity-50"><Play className="w-4 h-4" /></button>
          <button onClick={onDelete} className="text-red-500 hover:text-red-400 ml-1"><Trash2 className="w-4 h-4" /></button>
        </div>
        {/* Label 编辑：聚焦时在行底部展开 */}
        {focused && onLabelChange && (
          <div className="absolute left-0 right-0 -bottom-6 flex items-center gap-1 z-[5]">
            <span className="text-[10px] text-muted-foreground/50 pl-8">备注:</span>
            <input
              type="text"
              value={keyObj.label || ''}
              onChange={e => onLabelChange(e.target.value)}
              onFocus={onFocus}
              placeholder="点击添加备注"
              className="flex-1 bg-background/80 backdrop-blur-sm border border-border/50 rounded px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400 font-mono outline-none focus:border-amber-500/50 placeholder:text-muted-foreground/30"
            />
          </div>
        )}
      </div>
    </div>
  );
}



// 修改原因：Key 数量较多时，完整行模式会占用过多纵向空间，无法快速查看大量 Key 的状态。
// 修改方式：为机房模式集中定义固定尺寸卡片、350° 缺口圆环、tier 药丸和状态灯等纯展示 helper。
// 目的：在不改动现有完整行渲染代码的前提下，让 >= 10 个 Key 的渠道自动获得紧凑视图。
const RACK_ARC_LENGTH = 350;
const RACK_GAP_LENGTH = 10;
const RACK_RING_PATH_LENGTH = RACK_ARC_LENGTH + RACK_GAP_LENGTH;

function clampRackPercent(value: number | null | undefined): number | null {
  // 修改原因：余额、OAuth quota 和缓存字段都可能为空或越界，直接用于 stroke-dasharray 会造成异常弧线。
  // 修改方式：把不可用值统一收敛为 null，把可用数字裁剪到 0 到 100。
  // 目的：让机房模式圆环在各种数据状态下都能稳定渲染。
  if (value == null) return null;
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return Math.max(0, Math.min(100, n));
}

function mixRackRgb(from: [number, number, number], to: [number, number, number], ratio: number): string {
  // 修改原因：机房模式要求普通 API Key 圆环使用绿到黄再到红的连续渐变，而旧 getBalanceColor 只返回三档名称。
  // 修改方式：按 0..1 比例在线性 RGB 空间混合两端颜色，供单环 stroke 直接使用。
  // 目的：保留现有百分比计算，同时让卡片圆环颜色变化更细腻。
  const r = Math.max(0, Math.min(1, ratio));
  const mixed = from.map((v, idx) => Math.round(v + (to[idx] - v) * r));
  return `rgb(${mixed[0]}, ${mixed[1]}, ${mixed[2]})`;
}

function getRackUsageGradientColor(percent: number): string {
  // 修改原因：普通 Key 的机房圆环需要按百分比从绿到黄再到红过渡，不能只使用完整行的背景色。
  // 修改方式：0..50% 在绿色和黄色之间插值，50..100% 在黄色和红色之间插值。
  // 目的：让圆环颜色直接反映当前百分比强度。
  const p = Math.max(0, Math.min(1, percent / 100));
  // 0% = 红色（余额耗尽/危险），100% = 绿色（余额充足/安全）
  if (p < 0.5) return mixRackRgb([239, 68, 68], [234, 179, 8], p / 0.5);
  return mixRackRgb([234, 179, 8], [16, 185, 129], (p - 0.5) / 0.5);
}

function formatRackKeyLabel(keyObj: ApiKeyObj): string {
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

function getRackBalanceTextClass(color: 'green' | 'yellow' | 'red' | null): string {
  // 修改原因：普通 Key 中心百分比仍复用 getBalanceColor 的三档语义，避免卡片文字和旧余额判断脱节。
  // 修改方式：把 green/yellow/red 映射成适合深色卡片的文字颜色。
  // 目的：在使用连续圆环颜色的同时保留原有余额分档含义。
  if (color === 'green') return 'text-emerald-600 dark:text-emerald-100';
  if (color === 'yellow') return 'text-yellow-600 dark:text-yellow-100';
  if (color === 'red') return 'text-red-600 dark:text-red-100';
  return 'text-muted-foreground';
}

function RackGrid({ children, onClick }: { children: React.ReactNode; onClick?: React.MouseEventHandler<HTMLDivElement> }) {
  // 修改原因：机房模式需要让卡片横向排列并自动换行，而不能继续使用完整行的纵向间距布局。
  // 修改方式：用 flex flex-wrap 和固定 gap 包裹 RackCard，外层仍放在原滚动容器内。
  // 目的：在侧边编辑抽屉中以紧凑网格展示大量 Key。
  // 修改原因：选中卡片展开成完整行后，用户需要点击机房网格空白处取消选中。
  // 修改方式：让 RackGrid 接收并透传 onClick，只在调用方判断 target 是否为网格本身。
  // 目的：避免点击卡片或完整行内部控件时误取消编辑状态。
  return <div className="flex flex-wrap gap-1.5 pb-1" onClick={onClick}>{children}</div>;
}

function RackRingCircle({ radius, strokeWidth, percent, color, trackOpacity = 0.72 }: {
  radius: number;
  strokeWidth: number;
  percent: number | null;
  color: string;
  trackOpacity?: number;
}) {
  // 修改原因：单环和 OAuth 双环都需要相同的 350° 缺口圆环，重复写 SVG 容易让缺口角度不一致。
  // 修改方式：统一用 circle、pathLength 和 stroke-dasharray 绘制轨道与填充，并整体旋转 5° 让缺口居中在右侧。
  // 目的：确保普通 Key 和 OAuth Key 的机房圆环视觉一致。
  // 修改原因：圆环轨道原先使用过深的浅色主题颜色，导致浅色主题下几乎看不见。
  // 修改方式：轨道 circle 改用 Tailwind stroke 主题类，浅色主题使用 slate-300，深色主题保留原 #1a1a2e。
  // 目的：让同一个 SVG 轨道在浅色和深色主题中都有稳定对比度。
  const fillLength = percent == null ? 0 : (percent / 100) * RACK_ARC_LENGTH;
  return (
    <g style={{ transform: 'rotate(5deg)', transformOrigin: '32px 32px' }}>
      <circle
        cx="32"
        cy="32"
        r={radius}
        pathLength={RACK_RING_PATH_LENGTH}
        fill="none"
        className="stroke-slate-300 dark:stroke-[#1a1a2e]"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeOpacity={trackOpacity}
        strokeDasharray={`${RACK_ARC_LENGTH} ${RACK_GAP_LENGTH}`}
      />
      {percent != null && fillLength > 0 && (
        <circle
          cx="32"
          cy="32"
          r={radius}
          pathLength={RACK_RING_PATH_LENGTH}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={`${fillLength} ${RACK_RING_PATH_LENGTH - fillLength}`}
          className="transition-all duration-500"
        />
      )}
    </g>
  );
}

function RackSingleRing({ percent, textClassName, label }: { percent: number | null; textClassName: string; label?: string | null }) {
  const pct = clampRackPercent(percent);
  const stroke = pct == null ? '#334155' : getRackUsageGradientColor(pct);
  // 有金额标签时显示金额，否则显示百分比
  const displayText = label || (pct == null ? '—' : `${Math.round(pct)}%`);
  const textSize = label && label.length > 5 ? 'text-[8px]' : 'text-[11px]';
  return (
    <div className="relative flex h-12 w-12 items-center justify-center">
      <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
        <RackRingCircle radius={25} strokeWidth={6} percent={pct} color={stroke} trackOpacity={pct == null ? 0.45 : 0.8} />
      </svg>
      <span className={`absolute inset-0 flex items-center justify-center ${textSize} font-bold font-mono ${textClassName}`}>
        {displayText}
      </span>
    </div>
  );
}

function RackOAuthRings({ quota, hideText }: { quota: OAuthQuota | null; hideText?: boolean }) {
  const quotaInner = clampRackPercent(quota?.quota_inner);
  const quotaOuter = clampRackPercent(quota?.quota_outer);
  return (
    <div className="relative flex h-12 w-12 items-center justify-center">
      <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
        <RackRingCircle radius={26} strokeWidth={5} percent={quotaInner} color="#60a5fa" trackOpacity={quotaInner == null ? 0.42 : 0.74} />
        <RackRingCircle radius={18} strokeWidth={5} percent={quotaOuter} color="#a78bfa" trackOpacity={quotaOuter == null ? 0.35 : 0.68} />
      </svg>
      {!hideText && (
        <span className="absolute inset-0 flex items-center justify-center text-[11px] font-bold font-mono text-sky-700 dark:text-sky-100">
          {quotaInner == null ? '—' : `${Math.round(quotaInner)}%`}
        </span>
      )}
    </div>
  );
}

const QUOTA_GAUGE_TONE_STROKES: Record<string, string> = {
  // 修改原因：QuotaGauge 提供 tone 后，通用圆环需要在不知道具体渠道的情况下应用基础颜色。
  // 修改方式：将后端约定的 tone 映射为稳定 stroke 色，未指定时继续按余额百分比或双环默认色处理。
  // 目的：让普通余额、插件和 OAuth quota 都能复用 QuotaRings，而不再在渲染层增加渠道分支。
  blue: '#60a5fa',
  green: '#10b981',
  yellow: '#eab308',
  red: '#ef4444',
  gray: '#64748b',
};

function normalizeQuotaTone(tone?: string | null): string | null {
  // 修改原因：后端或旧 fallback 可能传入空值、大小写不一致或未登记的 tone。
  // 修改方式：统一转小写，并只接受 QuotaRings 支持的标准颜色名。
  // 目的：删除通用 badge 组件后仍让 gauges 的 tone 能安全映射到圆环颜色，不拼接非法样式。
  const key = String(tone || '').toLowerCase();
  return key in QUOTA_GAUGE_TONE_STROKES ? key : null;
}

function getQuotaGaugeStrokeColor(gauge: QuotaGauge | undefined, percent: number | null, fallback: string): string {
  // 修改原因：单环需要沿用余额渐变，双环需要沿用蓝紫默认色，同时允许后端 tone 覆盖颜色。
  // 修改方式：先读取标准 tone；没有 tone 且有百分比时使用旧余额渐变，否则使用调用方传入的默认色。
  // 目的：在保留 RackSingleRing/RackOAuthRings 视觉习惯的同时接入统一 QuotaGauge。
  const tone = normalizeQuotaTone(gauge?.tone);
  if (tone && QUOTA_GAUGE_TONE_STROKES[tone]) return QUOTA_GAUGE_TONE_STROKES[tone];
  if (percent != null && !fallback) return getRackUsageGradientColor(percent);
  return fallback || (percent == null ? '#334155' : getRackUsageGradientColor(percent));
}

function getQuotaRingTextClass(gauge: QuotaGauge | undefined, percent: number | null): string {
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

function getCurrencySymbol(currency: string | null | undefined): string {
  if (!currency) return '';
  const map: Record<string, string> = { USD: '$', CNY: '¥', EUR: '€', GBP: '£', JPY: '¥' };
  return map[currency.toUpperCase()] || `${currency} `;
}

function formatQuotaAmount(available: number | null | undefined, unit: string | null | undefined): string {
  if (available == null) return '—';
  const prefix = getCurrencySymbol(unit);
  if (available >= 1000) return `${prefix}${formatCompactNumber(available)}`;
  return `${prefix}${available.toFixed(2)}`;
}

function getQuotaRingText(gauge: QuotaGauge | undefined, percent: number | null): string {
  // quota 模式：显示金额
  if (gauge?.display_mode === 'quota') return formatQuotaAmount(gauge.available, gauge.unit);
  // amount 模式：优先 displayLabel（紧凑金额），否则百分比
  if (gauge?.displayLabel) return gauge.displayLabel;
  return percent == null ? '—' : `${Math.round(percent)}%`;
}

function QuotaRings({ gauges, hideText }: { gauges: QuotaGauge[]; hideText?: boolean }) {
  // 修改原因：RackSingleRing 与 RackOAuthRings 的调用点原先按 OAuth 与普通 Key 分支选择，后续 quota 类型会继续增加。
  // 修改方式：按 gauges 数量决定空态、单环或双环，三项以上沿用前两个 gauge，并在内部复用原有圆环样式参数。
  // 目的：让机房卡片和完整 Key 行都走同一个圆环渲染路径。
  const visibleGauges = Array.isArray(gauges) ? gauges.filter(Boolean).slice(0, 2) : [];

  if (visibleGauges.length === 0) {
    return (
      <div className="relative flex h-12 w-12 items-center justify-center" title="暂无额度数据">
        <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
          <RackRingCircle radius={25} strokeWidth={6} percent={null} color="#334155" trackOpacity={0.45} />
        </svg>
        {!hideText && <span className="absolute inset-0 flex items-center justify-center text-[11px] font-bold font-mono text-muted-foreground">—</span>}
      </div>
    );
  }

  if (visibleGauges.length === 1) {
    const gauge = visibleGauges[0];
    const mode = gauge.display_mode || (gauge.percent != null ? 'percent' : 'quota');
    const displayText = getQuotaRingText(gauge, gauge.percent ?? null);
    const textSize = displayText.length > 5 ? 'text-[8px]' : 'text-[11px]';

    if (mode === 'quota') {
      // quota 模式：available 对比 100 画弧线 + 金额文字
      const pct = clampRackPercent(gauge.percent);
      const stroke = getQuotaGaugeStrokeColor(gauge, pct, '');
      return (
        <div className="relative flex h-12 w-12 items-center justify-center" title={`余额: ${displayText}`}>
          <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
            <RackRingCircle radius={25} strokeWidth={6} percent={pct} color={stroke} trackOpacity={pct == null ? 0.45 : 0.8} />
          </svg>
          {!hideText && <span className={`absolute inset-0 flex items-center justify-center ${textSize} font-bold font-mono ${getQuotaRingTextClass(gauge, pct)}`}>{displayText}</span>}
        </div>
      );
    }

    // percent / amount 模式：正常弧线
    const pct = clampRackPercent(gauge.percent);
    const stroke = getQuotaGaugeStrokeColor(gauge, pct, '');
    return (
      <div className="relative flex h-12 w-12 items-center justify-center" title={`${gauge.label}: ${pct == null ? '未知' : `${pct.toFixed(1)}%`}`}>
        <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
          <RackRingCircle radius={25} strokeWidth={6} percent={pct} color={stroke} trackOpacity={pct == null ? 0.45 : 0.8} />
        </svg>
        {!hideText && (
          <span className={`absolute inset-0 flex items-center justify-center ${textSize} font-bold font-mono ${getQuotaRingTextClass(gauge, pct)}`}>
            {displayText}
          </span>
        )}
      </div>
    );
  }

  const quotaInner = clampRackPercent(visibleGauges[0]?.percent);
  const quotaOuter = clampRackPercent(visibleGauges[1]?.percent);
  const innerColor = getQuotaGaugeStrokeColor(visibleGauges[0], quotaInner, '#60a5fa');
  const outerColor = getQuotaGaugeStrokeColor(visibleGauges[1], quotaOuter, '#a78bfa');
  return (
    <div className="relative flex h-12 w-12 items-center justify-center" title={`${visibleGauges[0].label}: ${quotaInner ?? '?'}% · ${visibleGauges[1].label}: ${quotaOuter ?? '?'}%`}>
      <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
        <RackRingCircle radius={26} strokeWidth={5} percent={quotaInner} color={innerColor} trackOpacity={quotaInner == null ? 0.42 : 0.74} />
        <RackRingCircle radius={18} strokeWidth={5} percent={quotaOuter} color={outerColor} trackOpacity={quotaOuter == null ? 0.35 : 0.68} />
      </svg>
      {!hideText && (
        <span className={`absolute inset-0 flex items-center justify-center text-[11px] font-bold font-mono ${getQuotaRingTextClass(visibleGauges[0], quotaInner)}`}>
          {getQuotaRingText(visibleGauges[0], quotaInner)}
        </span>
      )}
    </div>
  );
}

function RackCoolingBorder({ remainSec, totalDuration }: { remainSec: number; totalDuration: number }) {
  // 在卡片圆角矩形边框上画红色冷却进度条 + 倒计时叠加层
  const ref = useRef<HTMLDivElement>(null);
  const [svgViewBox, setSvgViewBox] = useState('');
  const [pathD, setPathD] = useState('');
  const progress = totalDuration > 0 ? Math.max(0, Math.min(100, (remainSec / totalDuration) * 100)) : 0;
  const safeRemain = Math.max(0, Math.ceil(remainSec));

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      if (w > 0 && h > 0) {
        setSvgViewBox(`0 0 ${w} ${h}`);
        setPathD(buildRoundRectPath(1, 1, w - 2, h - 2, 7));
      }
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const dasharray = progress > 0 ? `${progress} 100` : '0 100';
  const dashoffset = progress > 0 ? `${-(100 - progress)}` : '0';

  return (
    <div ref={ref} className="absolute inset-0 z-[5] pointer-events-none">
      {pathD && (
        <svg className="absolute inset-0 w-full h-full" viewBox={svgViewBox} style={{ overflow: 'visible' }}>
          <path
            d={pathD}
            pathLength={100}
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            className="text-red-500"
            style={{ strokeDasharray: dasharray, strokeDashoffset: dashoffset, transition: 'stroke-dasharray 1s linear, stroke-dashoffset 1s linear' }}
          />
        </svg>
      )}
      <div className="absolute inset-x-0 bottom-[18px] flex items-center justify-center">
        <span className="text-[9px] font-bold font-mono text-red-500 bg-background/80 dark:bg-card/80 rounded px-1 py-0.5">
          {formatCountdown(safeRemain)}
        </span>
      </div>
    </div>
  );
}

function RackCard({ idx, keyObj, providerName, engine, enabledPlugins, runtimeKeyStatus, localCountdowns, balanceResults, oauthAccounts, isOAuthEngine, onFocus, onImport, onLogin }: {
  idx: number;
  keyObj: ApiKeyObj;
  providerName: string;
  engine: string;
  // 修改原因：机房卡片按 engine 判断 slot 时拿不到 formData，必须由调用处把当前 provider 启用插件列表传入。
  // 修改方式：在 RackCard props 中增加 enabledPlugins，并传给 hasUiSlot 与 UiSlot。
  // 目的：让 oai_tier 这类 requires_plugin 的 quota_display 在紧凑卡片中按当前 provider 正确生效。
  enabledPlugins: string[];
  runtimeKeyStatus: Record<string, { auto_disabled?: { key: string; remaining_seconds: number; duration?: number; reason?: string }[]; cooling?: any[] }>;
  localCountdowns: Record<string, Record<string, { remaining: number; duration: number }>>;
  balanceResults: Record<string, BalanceResult>;
  oauthAccounts: Record<string, any>;
  isOAuthEngine: boolean;
  onFocus: () => void;
  onImport: () => void;
  onLogin: () => void;
}) {
  // 修改原因：机房卡片需要复用完整行的数据来源，但完整行渲染分支不能被改写。
  // 修改方式：在独立 RackCard 中重新计算运行时禁用、冷却、余额、OAuth quota 和插槽状态。
  // 目的：让紧凑视图与完整行保持相同状态语义和操作回调。
  const rtDisabled = runtimeKeyStatus[providerName]?.auto_disabled || [];
  const rtEntry = !keyObj.disabled ? rtDisabled.find((d: any) => d.key === keyObj.key) : undefined;
  const isPermanent = !!rtEntry && rtEntry.remaining_seconds < 0;
  const isCooling = !!rtEntry && !isPermanent && rtEntry.remaining_seconds > 0;
  const countdown = localCountdowns[providerName]?.[keyObj.key];
  const remainSec = countdown?.remaining ?? (rtEntry?.remaining_seconds || 0);
  // 修改原因：冷却圆环需要用剩余时间除以总冷却时长，不能再只依赖卡片边框表达冷却状态。
  // 修改方式：沿用完整行 CoolingKeyRow 的数据来源，优先取本地倒计时 duration，其次取运行时 duration，最后用 remainSec 兜底。
  // 目的：让机房卡片的冷却弧线和完整行倒计时保持同一套时间语义。
  const totalDuration = countdown?.duration ?? rtEntry?.duration ?? remainSec;
  const isGrayed = keyObj.disabled || isPermanent;
  const status = isGrayed ? 'disabled' : isCooling ? 'cooling' : 'active';
  const bal = balanceResults[keyObj.key];
  const oauthAccount = oauthAccounts[keyObj.key];
  // 修改原因：机房卡片不应再按 OAuth 与普通 Key 选择两套圆环数据来源，也不应再渲染通用 badge 标签。
  // 修改方式：用 buildRowQuota 统一产出 gauges，并为旧 amount fallback 单独压缩圆心显示文本。
  // 目的：卡片渲染只消费 QuotaRings；tier/plan 等标签交给 quota_display 插槽。
  const rowQuota = buildRowQuota(bal, oauthAccount, isOAuthEngine);
  const rackGauges = withRackCompactBalanceFallback(rowQuota.gauges, bal);
  const rowQuotaHasValues = rowQuota.gauges.length > 0;
  const slotData = buildRowQuotaSlotData(bal, oauthAccount, rowQuota);
  // 修改原因：部分渠道插槽只读取账号或 balance 上下文，即使标准 quota 数字暂时没有返回也应挂载。
  // 修改方式：按 slotData、OAuth 账号和统一 rowQuota 是否有内容综合判断，不再使用 OAuth 展示分支。
  // 目的：让 key_background、quota_display 在机房模式下不因标准双额度缺失而失效。
  const slotPayloadAvailable = Boolean(slotData || oauthAccount || rowQuotaHasValues);
  const slotContext = { account: oauthAccount, keyObj, balance: bal };
  const hasKeyBorderSlot = hasUiSlot(engine, 'key_border', enabledPlugins);
  const hasKeyBackgroundSlot = hasUiSlot(engine, 'key_background', enabledPlugins);
  const hasQuotaDisplaySlot = hasUiSlot(engine, 'quota_display', enabledPlugins);
  const isOAuthEmpty = isOAuthEngine && !keyObj.key.trim();
  const labelText = formatRackKeyLabel(keyObj);
  const title = `${idx + 1}. ${keyObj.label || keyObj.key || '空账号'}${isCooling ? ` · 冷却 ${formatCountdown(remainSec)}` : ''}`;
  // 修改原因：冷却状态已经由圆环倒计时表达，继续显示黄色状态灯会让小卡片颜色过于集中。
  // 修改方式：状态灯只保留启用和禁用两类静态状态，冷却时不再渲染右上角黄色灯。
  // 目的：让用户主要通过中心倒计时和圆环进度识别冷却状态。
  const statusClass = isGrayed
    ? 'bg-red-500 shadow-[0_0_10px_rgba(239,68,68,0.6)]'
    : 'bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.65)]';
  // 修改原因：冷却卡片的黄色硬边框会和倒计时圆环重复表达，造成小卡片视觉拥挤。
  // 修改方式：所有机房卡片统一使用普通边框，冷却进度只由圆环承担。
  // 目的：去掉旧的黄色硬编码冷却边框，保留状态表达但降低干扰。
  const cardBorder = 'border-border';
  // 修改原因：机房卡片原先复用 muted 半透明背景，浅色主题下与滚动区域层次不够清楚。
  // 修改方式：浅色主题使用 card 表面，深色主题继续使用 muted 半透明表面以维持原暗色观感。
  // 目的：让大量机房卡片在两种主题下都有明确边界和稳定背景。
  const cardSurfaceClass = 'bg-card/90 dark:bg-muted/50';
  // 修改原因：空 OAuth 卡片圆环上的导入和登录操作层原先只使用深色按钮和遮罩。
  // 修改方式：把遮罩、导入按钮和登录按钮抽成包含 light/dark 变体的类名常量。
  // 目的：让浅色主题下的空账号操作入口保持可读，同时不改变深色主题视觉。
  const emptyOAuthOverlayClass = 'absolute inset-0 z-[6] flex flex-col items-center justify-center gap-1 rounded-full bg-white/90 dark:bg-[#0f0f12]/85';
  const emptyOAuthImportButtonClass = 'flex items-center gap-1 rounded border border-slate-300 bg-white/95 px-1.5 py-0.5 text-[9px] text-slate-700 hover:bg-slate-100 dark:border-[#1e1e22] dark:bg-slate-800/90 dark:text-slate-100 dark:hover:bg-slate-700';
  const emptyOAuthLoginButtonClass = 'flex items-center gap-1 rounded border border-blue-500/40 bg-blue-500/15 px-1.5 py-0.5 text-[9px] text-blue-700 hover:bg-blue-500/20 dark:text-blue-200 dark:hover:bg-blue-500/25';

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onFocus();
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      title={title}
      onClick={onFocus}
      onKeyDown={handleKeyDown}
      className={`relative h-[92px] overflow-hidden rounded-lg border ${cardSurfaceClass} text-foreground transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-blue-500/50 ${cardBorder} ${isGrayed ? 'opacity-50' : 'hover:border-muted-foreground/30'}`}
      style={{ width: 'calc((100% - 5 * 6px) / 6)', isolation: 'isolate' }}
    >
      {/* 修改原因：key_border 和 key_background 插槽在完整行模式已有挂载点，机房卡片也必须保留同等扩展能力；渠道脚本还需要知道当前是小卡片布局。
          修改方式：把 key_background 放在卡片背景层，把 key_border 放在外层绝对覆盖层，并透传 quota/account/balance 上下文和 mode: 'rack'。
          目的：保证渠道自定义边框、背景和额度装饰在紧凑视图中不丢失，同时避免完整行样式进入机房卡片。 */}
      {hasKeyBackgroundSlot && slotPayloadAvailable && (
        <UiSlot engine={engine} slot="key_background" data={slotData} context={{ ...slotContext, mode: 'rack' }} element="div" className="absolute inset-0 z-0 rounded-xl pointer-events-none" enabledPlugins={enabledPlugins} />
      )}
      {hasKeyBorderSlot && slotPayloadAvailable && (
        <UiSlot engine={engine} slot="key_border" data={slotData} context={{ ...slotContext, mode: 'rack' }} element="div" className="absolute inset-0 z-[3] pointer-events-none" enabledPlugins={enabledPlugins} />
      )}
      {isCooling && <RackCoolingBorder remainSec={remainSec} totalDuration={totalDuration} />}
      <span className={`absolute right-1.5 top-1.5 z-[4] h-2 w-2 rounded-full ring-2 ring-card ${statusClass}`} title={status} />
      <div className="relative z-[2] flex h-full flex-col items-center px-1 pb-1.5 pt-2">
        <div className="relative flex h-12 w-12 items-center justify-center">
          {/* 修改原因：机房卡片不能再按 OAuth 与普通 Key 分支选择 RackOAuthRings 或 RackSingleRing。
              修改方式：统一把 rowQuota.gauges 交给 QuotaRings，由组件内部按数量决定空环、单环或双环。
              目的：新增 quota 类型时不需要继续修改卡片渲染分支。 */}
          <QuotaRings gauges={rackGauges} hideText={hasQuotaDisplaySlot && slotPayloadAvailable} />
          {/* 修改原因：冷却圆环中心必须显示倒计时，quota_display 再覆盖上去会遮挡剩余秒数；OAuth 渠道完整行标签在圆环中心会溢出。
              修改方式：quota_display 仍挂载在非冷却卡片的圆环中心，并透传 mode: 'rack'，外层容器增加 overflow-hidden 和 max-w-full。
              目的：同时保证普通额度插槽可用、冷却状态倒计时清晰可见，并限制机房卡片中心文字宽度。 */}
          {hasQuotaDisplaySlot && slotPayloadAvailable && (
            <div className="absolute inset-0 z-[5] flex items-center justify-center overflow-hidden max-w-full">
              <UiSlot engine={engine} slot="quota_display" data={slotData} context={{ ...slotContext, mode: 'rack' }} element="div" className="flex items-center justify-center text-[10px]" enabledPlugins={enabledPlugins} />
            </div>
          )}

          {isOAuthEmpty && (
            <div className={emptyOAuthOverlayClass}>
              <button
                type="button"
                onClick={(event) => { event.stopPropagation(); onImport(); }}
                className={emptyOAuthImportButtonClass}
                title="粘贴 Refresh Token"
              >
                <ClipboardPaste className="h-2.5 w-2.5" /> 导入
              </button>
              <button
                type="button"
                onClick={(event) => { event.stopPropagation(); onLogin(); }}
                className={emptyOAuthLoginButtonClass}
                title="浏览器登录"
              >
                <LogIn className="h-2.5 w-2.5" /> 登录
              </button>
            </div>
          )}
        </div>
        <div className="mt-auto w-full text-center">

          <div className={`truncate text-[10px] font-semibold font-mono ${isCooling ? 'text-red-400 dark:text-red-300 line-through decoration-red-500/40' : 'text-foreground'}`} title={keyObj.label || keyObj.key || labelText}>{labelText}</div>
        </div>
      </div>
      {/* 修改原因：卡片选中后的编辑入口已改为展开完整行，旧的底部小按钮弹层太小且不能编辑 Key。
          修改方式：删除 focused popover，让所有操作按钮只出现在展开后的完整行中。
          目的：避免同一张卡片同时出现两套操作入口。 */}
    </div>
  );
}


export default function Channels() {
  const [providers, setProviders] = useState<any[]>([]);
  const [providerActivity, setProviderActivity] = useState<Record<string, string>>({});
  const [channelTypes, setChannelTypes] = useState<ChannelOption[]>([]);
  const [allPlugins, setAllPlugins] = useState<PluginOption[]>([]);
  const [loading, setLoading] = useState(true);

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [originalIndex, setOriginalIndex] = useState<number | null>(null);
  const [formData, setFormData] = useState<ProviderFormData | null>(null);
  // 修改原因：移动端浏览器在 Radix Dialog 给 body 设置 overflow:hidden 时可能把页面滚动位置重置到顶部。
  // 修改方式：用 ref 保存打开抽屉前的 scrollY 和 body 原有内联样式，避免用 state 触发重渲染或闭包拿到旧值。
  // 目的：关闭渠道编辑抽屉后准确恢复背后渠道列表的原始滚动位置。
  const channelModalScrollYRef = useRef(0);
  const channelModalBodyStyleRef = useRef<{ position: string; top: string; width: string } | null>(null);

  // 子渠道编辑模式：parentIdx = 主渠道在 providers 里的 index，subIdx = sub_channels 里的 index
  const [editingSubChannel, setEditingSubChannel] = useState<{ parentIdx: number; subIdx: number } | null>(null);

  const [groupInput, setGroupInput] = useState('');
  const [modelInput, setModelInput] = useState('');
  const [fetchingModels, setFetchingModels] = useState(false);
  const [copiedModels, setCopiedModels] = useState(false);
  const [showPluginSheet, setShowPluginSheet] = useState(false);
  const [testDialogOpen, setTestDialogOpen] = useState(false);
  const [testingProvider, setTestingProvider] = useState<any>(null);
  const [headerEntries, setHeaderEntries] = useState<HeaderEntry[]>([]);
  const [keyTestDialogOpen, setKeyTestDialogOpen] = useState(false);
  const [keyTestInitialIndex, setKeyTestInitialIndex] = useState<number | null>(null);
  const [keyTestOverride, setKeyTestOverride] = useState<{ engine: string; base_url: string; models: string[]; title: string } | null>(null);
  const [overridesJson, setOverridesJson] = useState('');
  const [statusCodeOverridesJson, setStatusCodeOverridesJson] = useState('');
  const [modelDisplayKey, setModelDisplayKey] = useState(0);
  const [analyticsOpen, setAnalyticsOpen] = useState(false);
  const [analyticsProvider, setAnalyticsProvider] = useState('');

  // ── 余额查询 ──
  const [balanceResults, setBalanceResults] = useState<Record<string, BalanceResult>>({});
  const [balanceLoading, setBalanceLoading] = useState(false);
  const [focusedKeyIdx, setFocusedKeyIdx] = useState<number | null>(null);
  const [forceListMode, setForceListMode] = useState(false);

  // 修改原因：OAuth 类型引擎需要展示已导入账号状态，并允许把 refresh_token 导入为账号标识。
  // 修改方式：新增账号列表、导入弹窗目标下标、待提交 token 和提交中状态。
  // 目的：让 OAuth Key 行可以在不暴露 token 明文的情况下完成账号导入和状态展示。
  const [oauthAccounts, setOauthAccounts] = useState<Record<string, any>>({});
  // 修改原因：OAuth key 输入框 onChange 会立即更新表单值，onBlur 时需要知道焦点进入前的旧标识符。
  // 修改方式：用 ref 按行下标保存 focus 时的 key 快照，不触发表单重渲染。
  // 目的：在用户把 OAuth 账号标识改名后，可以调用 rename API 同步 oauth_state.json。
  const oauthKeyFocusSnapshotRef = useRef<Record<number, string>>({});
  const [importModalIdx, setImportModalIdx] = useState<number | null>(null);
  const [importToken, setImportToken] = useState('');
  const [importing, setImporting] = useState(false);
  // 修改原因：manual OAuth 模式需要在弹窗登录后接收用户复制的 localhost 回调完整 URL。
  // 修改方式：保存当前 Key 行下标、state、用户粘贴的 URL 和交换中的提交状态。
  // 目的：替代旧的跨窗口 location 轮询和 prompt 降级，避免 COOP 或跨域策略导致登录不可用。
  const [oauthManualState, setOauthManualState] = useState<{ idx: number; state: string; provider: string } | null>(null);
  const [manualUrl, setManualUrl] = useState('');
  const [exchanging, setExchanging] = useState(false);
  // 修改原因：OAuth 导入和手动回调弹窗通过 document.body portal 渲染，仍会被编辑抽屉的 Radix Dialog 焦点锁拉回。
  // 修改方式：把两个 OAuth 弹窗状态合并成一个布尔值，供编辑抽屉外部焦点和外部交互事件共用。
  // 目的：只在 OAuth 覆盖弹窗打开期间放行 portal 焦点，关闭后恢复编辑抽屉原有的模态行为。
  const isOAuthOverlayOpen = importModalIdx !== null || oauthManualState !== null;
  const selectedChannelType = channelTypes.find(c => c.id === (formData?.engine || ''));
  // 修改原因：OAuth 引擎归属已经由后端渠道元数据声明，前端硬编码集合会与后端注册结果分歧。
  // 修改方式：只读取 selectedChannelType.is_oauth，字段缺失时按 false 处理。
  // 目的：让 Channels.tsx 不再猜测 engine 名称，OAuth 管理分支完全跟随后端配置。
  const isOAuthEngine = selectedChannelType?.is_oauth ?? false;
  // 修改原因：不同 OAuth 渠道的 refresh_token 样式不同，且 ui_slots 现在可能返回带 requires_plugin 的对象。
  // 修改方式：从 selectedChannelType.ui_slots.import_placeholder 只读取纯字符串；为空、对象或 export 脚本时使用通用兜底。
  // 目的：让后端渠道元数据控制占位符，同时避免把内联 JavaScript 或条件对象显示给用户。
  const rawImportPlaceholderValue = selectedChannelType?.ui_slots?.import_placeholder;
  const rawImportPlaceholder = typeof rawImportPlaceholderValue === 'string' ? rawImportPlaceholderValue : undefined;
  const importPlaceholder = rawImportPlaceholder && !rawImportPlaceholder.trimStart().startsWith('export')
    ? rawImportPlaceholder
    : 'refresh_token...';

  // ── 全局配置（用于价格提示等）──
  const [globalModelPrice, setGlobalModelPrice] = useState<Record<string, string>>({});

  // 修改原因：虚拟模型路由存储在全局 preferences.virtual_models 下，不属于单个真实渠道。
  // 修改方式：保留全局配置草稿，同时新增抽屉编辑态，列表只展示折叠卡片或行。
  // 目的：让虚拟模型混入渠道列表，避免顶部画布长期占用屏幕空间。
  const [virtualModels, setVirtualModels] = useState<Record<string, VirtualModelConfig>>({});
  const [virtualDraftName, setVirtualDraftName] = useState('');
  const [virtualDraftEnabled, setVirtualDraftEnabled] = useState(true);
  const [virtualModelsDirty, setVirtualModelsDirty] = useState(false);
  const [expandedVirtualModels, setExpandedVirtualModels] = useState<Set<string>>(() => new Set());
  const [expandedVirtualProviders, setExpandedVirtualProviders] = useState<Set<string>>(() => new Set());
  const [virtualAddNodeTypes, setVirtualAddNodeTypes] = useState<Record<string, 'model' | 'channel'>>({});
  const [isVirtualModalOpen, setIsVirtualModalOpen] = useState(false);
  const [editingVirtualName, setEditingVirtualName] = useState<string | null>(null);
  const [virtualEditorChain, setVirtualEditorChain] = useState<VirtualModelChainNode[]>([]);
  // 修改原因：虚拟模型抽屉左栏在大屏下长期占用过多横向空间。
  // 修改方式：新增左栏折叠状态，并默认折叠为窄侧边条。
  // 目的：打开抽屉时优先保证右侧链条编辑区空间充足，按需再展开渠道列表。
  const [isVirtualProviderPanelCollapsed, setIsVirtualProviderPanelCollapsed] = useState(false);
  // 修改原因：移动端渠道面板需要默认折叠，但又不能复用桌面端窄侧栏的折叠形态。
  // 修改方式：新增独立的移动端展开状态，只控制小屏顶部渠道面板的展开和收起。
  // 目的：手机上先显示链条编辑区，同时允许用户按需查看完整渠道列表。
  const [isVirtualMobileProviderPanelOpen, setIsVirtualMobileProviderPanelOpen] = useState(false);
  // 修改原因：渠道列表顶部需要一个统一的虚拟路由手风琴，而不是每个虚拟模型各自占一行。
  // 修改方式：新增独立展开状态，桌面表格和移动端卡片共用它控制子行或子卡片显示。
  // 目的：让虚拟模型置顶收纳，同时保留测试、启用、编辑和删除入口。
  const [isVirtualRoutesAccordionOpen, setIsVirtualRoutesAccordionOpen] = useState(false);

  const [isFetchModelsOpen, setIsFetchModelsOpen] = useState(false);
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(() => new Set());
  const [modelSearchQuery, setModelSearchQuery] = useState('');

  // ── Key 运行时状态 ──
  const [runtimeKeyStatus, setRuntimeKeyStatus] = useState<Record<string, { auto_disabled: { key: string; remaining_seconds: number; duration: number; reason: string }[]; cooling: any[] }>>({});
  const [localCountdowns, setLocalCountdowns] = useState<Record<string, Record<string, { remaining: number; duration: number }>>>({}); // provider -> key -> {remaining, duration}

  // ── 列表筛选 ──
  const [filterKeyword, setFilterKeyword] = useState('');
  const [filterEngine, setFilterEngine] = useState<string>(''); // '' = 全部
  const [filterGroup, setFilterGroup] = useState<string>('');   // '' = 全部
  const [filterStatus, setFilterStatus] = useState<'' | 'enabled' | 'disabled'>('');

  const { token } = useAuthStore();

  const restoreChannelModalScrollLock = useCallback(() => {
    // 修改原因：渠道编辑抽屉关闭或组件卸载时，body 仍可能保留 fixed 定位，导致页面停在错误位置。
    // 修改方式：只在存在样式快照时恢复 position、top、width，并用 ref 中保存的 scrollY 调回原列表位置。
    // 目的：让移动端关闭抽屉后回到打开前浏览的渠道卡片，而不是回到列表顶部。
    const previousStyle = channelModalBodyStyleRef.current;
    if (!previousStyle) return;

    const body = document.body;
    const scrollY = channelModalScrollYRef.current;
    body.style.position = previousStyle.position;
    body.style.top = previousStyle.top;
    body.style.width = previousStyle.width;
    channelModalBodyStyleRef.current = null;
    window.scrollTo(0, scrollY);
  }, []);

  // 修改原因：useEffect 跑得太晚——Radix Dialog mount 时内部 useLayoutEffect 先执行，给 body 加 overflow:hidden，
  //          移动端浏览器在此时就重置了 scrollTop，等我们的 useEffect 记录 scrollY 时已经是 0 了。
  // 修改方式：scroll lock 逻辑改为：锁定在 openModal 的 setIsModalOpen(true) 之前同步执行（见下方 applyChannelModalScrollLock），
  //          解锁仍在 useEffect 中监听 isModalOpen 变为 false 时恢复。
  // 目的：确保在 Radix Dialog mount 之前就固定 body，scrollY 不会被重置。
  const applyChannelModalScrollLock = useCallback(() => {
    if (channelModalBodyStyleRef.current) return;
    const body = document.body;
    const currentScrollY = window.scrollY || window.pageYOffset || document.documentElement.scrollTop || 0;
    channelModalScrollYRef.current = currentScrollY;
    channelModalBodyStyleRef.current = {
      position: body.style.position,
      top: body.style.top,
      width: body.style.width,
    };
    body.style.position = 'fixed';
    body.style.top = `-${currentScrollY}px`;
    body.style.width = '100%';
  }, []);

  useEffect(() => {
    if (!isModalOpen) {
      restoreChannelModalScrollLock();
    }
    return restoreChannelModalScrollLock;
  }, [isModalOpen, restoreChannelModalScrollLock]);

  const applyApiConfigData = (data: any, options: { syncVirtualModels?: boolean } = {}) => {
    // 修改原因：初始加载和单渠道保存后的刷新都要把 /v1/api_config 响应写回页面状态，但普通 provider 刷新不应覆盖未保存的虚拟路由草稿。
    // 修改方式：始终刷新 providers 和价格；只有初始加载显式传 syncVirtualModels 时才同步 virtual_models。
    // 目的：避免局部保存成功后继续使用本地拼接数组，同时保护用户正在编辑的虚拟模型配置。
    const rawProviders = data.providers || data.api_config?.providers || [];
    const sortedProviders = sortProvidersByWeight(Array.isArray(rawProviders) ? rawProviders : []);
    setProviders(sortedProviders);

    const globalPrefs = data.preferences || data.api_config?.preferences || {};
    setGlobalModelPrice(globalPrefs.model_price || {});
    if (!options.syncVirtualModels) return;

    // 修改原因：虚拟模型路由由 /v1/api_config 的全局 preferences 返回。
    // 修改方式：读取 preferences.virtual_models 并在缺失时回退为空对象。
    // 目的：页面初始加载后展示已配置的全局虚拟模型路由。
    const loadedVirtualModels = globalPrefs.virtual_models || {};
    setVirtualModels(loadedVirtualModels);
    setVirtualModelsDirty(false);
    // 修改原因：虚拟模型现在通过渠道列表卡片进入抽屉编辑，旧画布展开状态不再承担主要展示职责。
    // 修改方式：仍保留已有展开状态初始化，供历史函数和后续兼容逻辑使用。
    // 目的：减少 UI 重构对既有状态和 CRUD 函数的破坏范围。
    setExpandedVirtualModels(prev => prev.size > 0 ? prev : new Set(Object.keys(loadedVirtualModels).slice(0, 1)));
  };

  const refreshProviders = async (options: { syncVirtualModels?: boolean } = {}) => {
    // 修改原因：主渠道新增、更新、删除都改为单渠道 API，成功后不能再用本地数组推断最终状态。
    // 修改方式：重新请求 /v1/api_config，并通过 applyApiConfigData 写回后端最新 providers。
    // 目的：消除多浏览器并发保存时的陈旧状态风险。
    const res = await apiFetch('/v1/api_config', { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(fmtErr(err, res.status));
    }
    const data = await res.json();
    applyApiConfigData(data, options);
  };

  const refreshSingleProvider = async (providerId: string) => {
    // 单渠道局部刷新：GET 单个 provider 后替换本地数组对应项，避免拉全量 200KB
    try {
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'GET',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        // fallback 到全量刷新
        await refreshProviders();
        return;
      }
      const data = await res.json();
      if (!data?.provider) {
        await refreshProviders();
        return;
      }
      setProviders(prev => {
        const updated = prev.map(p =>
          String(p.provider || '') === providerId ? data.provider : p
        );
        return sortProvidersByWeight(updated);
      });
    } catch {
      await refreshProviders();
    }
  };

  const fetchInitialData = async () => {
    try {
      const headers = { Authorization: `Bearer ${token}` };
      // 同时获取运行时 Key 状态
      apiFetch('/v1/channels/key_status', { headers }).then(r => r.ok ? r.json() : {}).then(d => {
        const data = d || {};
        setRuntimeKeyStatus(data);
        // 初始化本地倒计时
        const countdowns: Record<string, Record<string, { remaining: number; duration: number }>> = {};
        for (const [prov, info] of Object.entries(data) as any) {
          countdowns[prov] = {};
          for (const item of (info.auto_disabled || [])) {
            countdowns[prov][item.key] = {
              remaining: item.remaining_seconds,
              duration: item.duration || 0,
            };
          }
        }
        setLocalCountdowns(countdowns);
      }).catch(() => {});

      const [_providersRefreshed, typesRes, pluginsRes, activityRes] = await Promise.all([
        refreshProviders({ syncVirtualModels: true }),
        apiFetch('/v1/channels', { headers }),
        apiFetch('/v1/plugins/interceptors', { headers }),
        apiFetch('/v1/stats/provider_activity', { headers }).catch(() => null),
      ]);

      if (typesRes.ok) {
        const data = await typesRes.json();
        const channelList = data.channels || [];
        // 修改原因：UiSlot 需要按 engine + 插槽名获取渠道内联 JS 脚本，且后端可能为插件门控 slot 返回条件对象。
        // 修改方式：在渠道列表加载成功后，把各渠道的 ui_slots 以 UiSlotValue 类型缓存到 window.__uiSlots。
        // 目的：后续插槽组件渲染时可按 engine、slot 名和 enabled_plugins 查找并动态加载。
        const uiSlots: Record<string, Record<string, UiSlotValue>> = {};
        for (const ch of channelList) {
          if (ch.ui_slots) uiSlots[ch.id] = ch.ui_slots;
        }
        (window as any).__uiSlots = uiSlots;
        setChannelTypes(channelList);
      }
      if (pluginsRes.ok) {
        const data = await pluginsRes.json();
        setAllPlugins(data.interceptor_plugins || []);
      }
      if (activityRes?.ok) {
        const data = await activityRes.json();
        setProviderActivity(data.activity || {});
      }
    } catch (err) {
      console.error('Failed to fetch initial data', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchInitialData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 刷新运行时 Key 状态（供按需调用：打开编辑面板、恢复 Key、倒计时归零时）
  const refreshKeyStatus = async () => {
    try {
      const res = await apiFetch('/v1/channels/key_status', { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok) return;
      const data = await res.json();
      setRuntimeKeyStatus(data || {});
      const countdowns: Record<string, Record<string, { remaining: number; duration: number }>> = {};
      for (const [prov, info] of Object.entries(data || {}) as any) {
        countdowns[prov] = {};
        for (const item of (info.auto_disabled || [])) {
          countdowns[prov][item.key] = {
            remaining: item.remaining_seconds,
            duration: item.duration || 0,
          };
        }
      }
      setLocalCountdowns(countdowns);
    } catch { /* ignore */ }
  };

  // 本地 1 秒倒计时，减少网络请求
  useEffect(() => {
    const timer = setInterval(() => {
      setLocalCountdowns(prev => {
        const next = { ...prev };
        let anyExpired = false;
        for (const prov of Object.keys(next)) {
          for (const key of Object.keys(next[prov])) {
            const entry = next[prov][key];
            if (entry.remaining > 0) {
              next[prov] = { ...next[prov], [key]: { ...entry, remaining: entry.remaining - 1 } };
              if (entry.remaining - 1 <= 0) anyExpired = true;
            }
          }
        }
        if (anyExpired) setTimeout(() => refreshKeyStatus(), 500);
        return next;
      });
    }, 1000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── 定期轮询 Key 禁用状态（每 15 秒），确保页面打开期间能及时反映后端变化 ──
  useEffect(() => {
    const pollTimer = setInterval(() => {
      refreshKeyStatus();
    }, 15000);
    return () => clearInterval(pollTimer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── 打开编辑面板时自动查询余额 ──
  useEffect(() => {
    // 修改原因：OAuth 编辑面板已有专门的账号列表 quota 拉取逻辑，不能再要求 preferences.balance。
    // 修改方式：自动余额查询只保留给普通渠道，OAuth 余额由账号列表和手动余额按钮触发。
    // 目的：避免打开 OAuth 面板时因为缺少普通余额配置产生无效请求。
    if (isModalOpen && !isOAuthEngine && formData?.base_url && formData?.api_keys?.some(k => k.key.trim() && !k.disabled)) {
      queryAllBalances(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isModalOpen]);

  const refreshOAuthAccounts = useCallback(async () => {
    // 修改原因：OAuth 账号列表既要在打开编辑面板时加载，也要在浏览器登录成功后刷新，且后端 state 已按 provider name 分层。
    // 修改方式：把 /v1/oauth/accounts?provider=当前渠道名 请求封装为 useCallback 函数，成功时写入 oauthAccounts，失败时清空。
    // 目的：避免加载其他渠道的同邮箱账号，降低账号状态不同步和跨渠道误展示风险。
    const providerName = (formData?.provider || '').trim();
    if (!providerName) {
      setOauthAccounts({});
      return;
    }
    try {
      const res = await apiFetch(`/v1/oauth/accounts?provider=${encodeURIComponent(providerName)}`, { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok) {
        setOauthAccounts({});
        return;
      }
      const data = await res.json();
      // 修改原因：后端账号状态可能仍带旧 quota_5h/quota_7d 字段，后续 UI 只应读取前端新字段。
      // 修改方式：写入 state 前统一转换为 quota_inner/quota_outer，保留其余账号状态。
      // 目的：避免旧后端字段穿透到渲染层造成字段名混用。
      setOauthAccounts(normalizeOAuthAccountStateMap(data));
    } catch {
      setOauthAccounts({});
    }
  }, [token, formData?.provider]);

  // ── 打开 OAuth 编辑面板时同步账号状态 ──
  useEffect(() => {
    // 修改原因：OAuth Key 行需要根据后端运行时状态展示账号是否连接以及配额数据。
    // 修改方式：仅在编辑面板打开且当前 engine 属于 OAuth 类型时调用 refreshOAuthAccounts。
    // 目的：避免普通渠道产生额外请求，同时保证 OAuth 账号列表来自后端最新状态。
    if (isModalOpen && isOAuthEngine) {
      refreshOAuthAccounts();
    } else if (!isModalOpen) {
      setOauthAccounts({});
    }
  }, [isModalOpen, isOAuthEngine, refreshOAuthAccounts]);

  useEffect(() => {
    // OAuth 弹窗打开时，对尚无 quota 的活跃账号批量调 /balance 查询额度。
    if (!isModalOpen || !isOAuthEngine) return;
    const providerName = (formData?.provider || '').trim();
    if (!providerName) return;
    const targets = Object.entries(oauthAccounts).filter(([, account]) => {
      // 修改原因：账号状态可能来自旧 quota_5h/quota_7d 缓存，判断是否缺少额度也要经过统一映射。
      // 修改方式：用 getOAuthQuota 读取 quota_inner/quota_outer，而不是直接访问账号上的历史字段。
      // 目的：避免旧字段导致前端重复触发或漏掉自动配额刷新。
      const accountQuota = getOAuthQuota(account);
      return (
        account?.status === 'active'
        && accountQuota?.quota_inner == null
        && accountQuota?.quota_outer == null
        && !account._quota_loading
        && !account._quota_unavailable
      );
    });
    if (targets.length === 0) return;

    // 标记所有目标为 loading
    setOauthAccounts(prev => {
      const next = { ...prev };
      for (const [keyId] of targets) {
        if (next[keyId]) next[keyId] = { ...next[keyId], _quota_loading: true };
      }
      return next;
    });

    // 一次 /balance 调用，后端会逐 key 查询并聚合返回
    apiFetch('/v1/channels/balance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        provider: formData?.provider,
        engine: formData?.engine,
        base_url: formData?.base_url,
        api_key: targets.map(([keyId]) => keyId),
        preferences: formData?.preferences,
      }),
    })
      .then(async res => {
        if (res.ok) return await res.json();
        try {
          const errBody = await res.json();
          toastWarning(errBody?.error || `HTTP ${res.status}`);
        } catch { /* ignore */ }
        return null;
      })
      .then(data => {
        setOauthAccounts(prev => {
          const next = { ...prev };
          const perAccount = data?.results || {};
          for (const [keyId] of targets) {
            const current = next[keyId];
            if (!current) continue;
            const { _quota_loading: _unusedLoading, ...accountWithoutLoading } = current;
            const result = perAccount[keyId] || data;
            if (result && typeof result === 'object') {
              // 修改原因：/balance 结果可能继续使用后端旧 quota_5h/quota_7d 字段，写入账号状态前必须转换。
              // 修改方式：复用 getQuotaFromSource 得到 quota_inner/quota_outer，再按新字段保存。
              // 目的：让后续渲染和插槽数据不再依赖旧字段名。
              const quotaResult = getQuotaFromSource(result);
              const hasQuota = quotaResult?.quota_inner != null || quotaResult?.quota_outer != null;
              next[keyId] = {
                ...accountWithoutLoading,
                ...(quotaResult?.quota_inner != null ? { quota_inner: quotaResult.quota_inner } : {}),
                ...(quotaResult?.quota_outer != null ? { quota_outer: quotaResult.quota_outer } : {}),
                ...(result.raw ? { quota_raw: result.raw } : {}),
                // 修改原因：渠道专属用量字段应由后端 raw 和 ui_slots 自行解释，通用账号状态不再复制具体字段名。
                // 修改方式：只按标准 quota_inner/quota_outer 字段判断本次是否拿到默认双弧可用数据。
                // 目的：避免 Channels.tsx 与某个渠道的私有额度结构继续耦合。
                _quota_unavailable: !hasQuota,
              };
            } else {
              next[keyId] = { ...accountWithoutLoading, _quota_unavailable: true };
            }
          }
          return next;
        });
      })
      .catch(() => {
        setOauthAccounts(prev => {
          const next = { ...prev };
          for (const [keyId] of targets) {
            const current = next[keyId];
            if (!current) continue;
            const { _quota_loading: _unusedLoading, ...accountWithoutLoading } = current;
            next[keyId] = { ...accountWithoutLoading, _quota_unavailable: true };
          }
          return next;
        });
      });
  }, [isModalOpen, isOAuthEngine, oauthAccounts, token, formData?.provider]);

  const openModal = async (provider: any = null, index: number | null = null) => {
    setOriginalIndex(index);
    setGroupInput('');
    setModelInput('');
    setShowPluginSheet(false);
    refreshKeyStatus();

    setBalanceResults({});
    setBalanceLoading(false);
    setFocusedKeyIdx(null);

    if (provider) {
      // 修改原因：编辑主渠道时，页面内存中的 providers 可能已经落后于后端配置。
      // 修改方式：只有真实主渠道编辑会带 index，此时先 GET 单个 provider；复制渠道的 index 为 null，继续使用本地副本。
      // 目的：避免用户打开编辑面板后用旧快照填表，保存时覆盖其他设备刚写入的新配置。
      let freshProvider = provider;
      if (provider && index !== null) {
        const providerId = String(provider.provider || '').trim();
        if (providerId) {
          try {
            const res = await apiFetch(buildProviderApiPath(providerId), {
              method: 'GET',
              headers: { Authorization: `Bearer ${token}` },
            });
            if (res.ok) {
              const data = await res.json();
              if (data?.provider) freshProvider = data.provider;
              else toastWarning('获取渠道最新数据失败，已使用页面缓存继续编辑');
            } else {
              toastWarning('获取渠道最新数据失败，已使用页面缓存继续编辑');
            }
          } catch {
            toastWarning('获取渠道最新数据失败，已使用页面缓存继续编辑');
          }
        } else {
          toastWarning('渠道名为空，已使用页面缓存继续编辑');
        }
      }

      // 修改原因：后续填表逻辑较长，如果继续直接读 provider 参数，容易遗漏旧快照引用。
      // 修改方式：统一把最终数据源命名为 activeProvider，GET 成功时它就是后端最新值，失败时是传入的回退值。
      // 目的：让 API Key、模型、偏好设置和子渠道都从同一个最新快照初始化。
      const activeProvider = freshProvider;
      const parseApiKey = (raw: any): ApiKeyObj => {
        // dict 格式: {"sk-xxx": "label"}
        if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
          const entries = Object.entries(raw);
          if (entries.length === 1) {
            const [k, v] = entries[0];
            const trimmed = String(k).trim();
            const label = v ? String(v).trim() : undefined;
            if (trimmed.startsWith('!')) return { key: trimmed.substring(1), disabled: true, label };
            return { key: trimmed, disabled: false, label };
          }
        }
        const trimmed = String(raw).trim();
        if (trimmed.startsWith('!')) return { key: trimmed.substring(1), disabled: true };
        return { key: trimmed, disabled: false };
      };


      const serializeApiKey = (k: ApiKeyObj): string | Record<string, string> => {
        const raw = k.disabled ? `!${k.key.trim()}` : k.key.trim();
        if (k.label) return { [raw]: k.label };
        return raw;
      };

      let parsedKeys: ApiKeyObj[] = [];
      if (Array.isArray(activeProvider.api)) parsedKeys = activeProvider.api.map(parseApiKey);
      else if (typeof activeProvider.api === 'string' && activeProvider.api.trim()) parsedKeys = [parseApiKey(activeProvider.api.trim())];
      else if (Array.isArray(activeProvider.api_keys)) parsedKeys = activeProvider.api_keys.map(parseApiKey);

      const rawModels = Array.isArray(activeProvider.model) ? activeProvider.model : Array.isArray(activeProvider.models) ? activeProvider.models : [];
      const models: string[] = [];
      const mappings: ModelMapping[] = [];

      rawModels.forEach((m: any) => {
        if (typeof m === 'string') models.push(m);
        else if (typeof m === 'object' && m !== null) {
          Object.entries(m).forEach(([upstream, alias]) => {
            mappings.push({ from: alias as string, to: upstream });
          });
        }
      });

      let groups = ["default"];
      if (Array.isArray(activeProvider.groups) && activeProvider.groups.length > 0) groups = activeProvider.groups;
      else if (typeof activeProvider.group === 'string' && activeProvider.group.trim()) groups = [activeProvider.group.trim()];
      else if (activeProvider.preferences?.group) groups = [activeProvider.preferences.group.trim()];

      const pHeaders = activeProvider.preferences?.headers || {};
      const pOverrides = activeProvider.preferences?.post_body_parameter_overrides || {};
      const entries: HeaderEntry[] = [];
      Object.entries(pHeaders).forEach(([k, v]) => {
        if (Array.isArray(v)) {
          v.forEach(item => entries.push({ key: k, value: String(item).trim() }));
        } else {
          entries.push({ key: k, value: String(v).trim() });
        }
      });
      setHeaderEntries(entries);
      setOverridesJson(Object.keys(pOverrides).length > 0 ? JSON.stringify(pOverrides, null, 2) : '');

      const pStatusCodeOverrides = activeProvider.preferences?.status_code_overrides || {};
      setStatusCodeOverridesJson(Object.keys(pStatusCodeOverrides).length > 0 ? JSON.stringify(pStatusCodeOverrides, null, 2) : '');

      const basePreferences = activeProvider.preferences && typeof activeProvider.preferences === 'object'
        ? activeProvider.preferences
        : {};

      // 解析子渠道
      const rawSubChannels = Array.isArray(activeProvider.sub_channels) ? activeProvider.sub_channels : [];
      const subChannels: SubChannelFormData[] = rawSubChannels.map((sub: any) => {
        const subRawModels = Array.isArray(sub.model) ? sub.model : Array.isArray(sub.models) ? sub.models : [];
        const subModels: string[] = [];
        const subMappings: ModelMapping[] = [];
        subRawModels.forEach((m: any) => {
          if (typeof m === 'string') subModels.push(m);
          else if (typeof m === 'object' && m !== null) {
            Object.entries(m).forEach(([upstream, alias]) => {
              subMappings.push({ from: alias as string, to: upstream });
            });
          }
        });
        return {
          engine: sub.engine || '',
          models: subModels,
          mappings: subMappings,
          preferences: sub.preferences && typeof sub.preferences === 'object' ? sub.preferences : {},
          enabled: sub.enabled,
          remark: sub.remark || '',
          base_url: sub.base_url || '',
          token_url: sub.token_url || '',
          model_prefix: sub.model_prefix || '',
          _collapsed: true,
        };
      });

      setFormData({
        provider: activeProvider.provider || activeProvider.name || '',
        remark: activeProvider.remark || '',
        engine: activeProvider.engine || '',
        base_url: activeProvider.base_url || '',
        token_url: activeProvider.token_url || '',
        api_keys: parsedKeys,
        model_prefix: activeProvider.model_prefix || '',
        enabled: activeProvider.enabled !== false,
        groups,
        models,
        mappings,
        preferences: {
          ...basePreferences,
          weight: basePreferences.weight ?? activeProvider.weight ?? 10,
          cooldown_period: basePreferences.cooldown_period ?? 3,
          api_key_schedule_algorithm: basePreferences.api_key_schedule_algorithm || 'round_robin',
          proxy: basePreferences.proxy || '',
          tools: basePreferences.tools !== false,
          system_prompt: basePreferences.system_prompt || '',
          // 修改原因：pool_sharing 是新增渠道级开关，旧配置没有该字段时必须保持默认关闭。
          // 修改方式：初始化时从 preferences 中读取并归一化为布尔值。
          // 目的：让表单保存时明确写入共享路由池状态，避免 undefined 被误解。
          pool_sharing: readBooleanPreference(basePreferences.pool_sharing),
          enabled_plugins: Array.isArray(basePreferences.enabled_plugins) ? basePreferences.enabled_plugins : [],
        },
        sub_channels: subChannels,
        // 修改原因：复制渠道打开编辑面板时，来源 provider 只存在于传入副本上。
        // 修改方式：初始化表单时把 _copiedFrom 带入表单态，后续保存成功后用于复制 OAuth state。
        // 目的：保留复制来源信息，同时不影响普通编辑渠道。
        _copiedFrom: activeProvider._copiedFrom || undefined,
      });
    } else {
      setHeaderEntries([]);
      setOverridesJson('');
      setStatusCodeOverridesJson('');
      setFormData({
        provider: '',
        remark: '',
        engine: channelTypes.length > 0 ? channelTypes[0].id : '',
        base_url: '',
        token_url: '',
        api_keys: [],
        model_prefix: '',
        enabled: true,
        groups: ['default'],
        models: [],
        mappings: [],
        // 修改原因：新增渠道默认不能加入共享路由池，必须由用户在有 model_prefix 时显式开启。
        // 修改方式：在默认 preferences 中写入 pool_sharing: false。
        // 目的：保证新增渠道和旧渠道的默认行为一致。
        preferences: { weight: 10, api_key_schedule_algorithm: 'round_robin', tools: true, pool_sharing: false, enabled_plugins: [], key_rules: [{ match: { status: [401, 403] }, duration: -1 }, { match: 'default', duration: 3 }] },
        sub_channels: [],
      });
    }
    applyChannelModalScrollLock();
    setIsModalOpen(true);
  };

  const updateFormData = (field: keyof ProviderFormData, value: any) => {
    setFormData(prev => prev ? { ...prev, [field]: value } : null);
  };

  const updatePreference = (field: keyof ProviderFormData['preferences'], value: any) => {
    setFormData(prev => prev ? { ...prev, preferences: { ...prev.preferences, [field]: value } } : null);
  };

  const updateModelPrefix = (value: string) => {
    // 修改原因：共享路由池只有在 model_prefix 存在时才有意义，清空前缀后不能保留隐藏的开启状态。
    // 修改方式：更新 model_prefix 时，如果新值为空，就同步把 preferences.pool_sharing 置为 false。
    // 目的：避免保存出无前缀但 pool_sharing=true 的冗余配置。
    setFormData(prev => {
      if (!prev) return null;
      if (value.trim()) return { ...prev, model_prefix: value };
      return { ...prev, model_prefix: value, preferences: { ...prev.preferences, pool_sharing: false } };
    });
  };

  // ── 查询所有 Key 余额 ──
  const queryAllBalances = async (silent = false) => {
    // 修改原因：OAuth 渠道的余额查询不依赖 Base URL 和 preferences.balance，而是由后端 OAuthManager 按账号标识查询。
    // 修改方式：仅普通渠道继续强制要求 base_url 和 balance 配置，OAuth 渠道直接进入 active key 查询。
    // 目的：让 Codex 等 OAuth 渠道的余额按钮可以点击并刷新 quota。
    if (!formData || (!isOAuthEngine && !formData.base_url)) return;
    const balanceCfg = formData.preferences?.balance;
    // balanceCfg 为空时后端会尝试根据 base_url 自动匹配模板，不再前端拦截

    const activeKeys = formData.api_keys.filter(k => k.key.trim() && !k.disabled);
    if (activeKeys.length === 0) { if (!silent) toastError('没有可用的 Key'); return; }

    setBalanceLoading(true);
    const results: Record<string, BalanceResult> = {};

    // 并发查询（最多 5 个并发）
    const concurrency = 5;
    const queue = [...activeKeys];
    const runNext = async () => {
      while (queue.length > 0) {
        const keyObj = queue.shift()!;
        try {
          const res = await apiFetch('/v1/channels/balance', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
            body: JSON.stringify({
              // 修改原因：OAuth 余额查询后端需要 provider name 作为 channel_id 定位分层 oauth_state。
              // 修改方式：余额请求随当前表单渠道名一起提交，普通渠道会忽略该字段。
              // 目的：避免同邮箱账号在不同 OAuth 渠道之间串读 quota。
              provider: formData.provider,
              engine: formData.engine,
              base_url: formData.base_url,
              api_key: keyObj.key,
              preferences: formData.preferences,
            }),
          });
          const data = await res.json().catch(() => ({ supported: false, error: '响应解析失败' }));
          let resultForKey: BalanceResult = isOAuthEngine ? (data?.results?.[keyObj.key] || data) : data;
          // 修改原因：balance_enricher 返回的 tier 字段需要随当前 Key 的余额状态一起保存，后续行渲染才能读取。
          // 修改方式：在写入 balanceResults 前把 tier 归一化为字符串，其余余额字段保持后端原样。
          // 目的：避免普通渠道的 Tier 标签因类型不明确而丢失。
          if (resultForKey?.tier != null) {
            resultForKey = { ...resultForKey, tier: String(resultForKey.tier) };
          }
          results[keyObj.key] = resultForKey;
          if (isOAuthEngine) {
            // 修改原因：OAuth Key 行不读取普通 balanceResults 标签，而是从 oauthAccounts 中渲染双弧 quota。
            // 修改方式：余额按钮拿到 OAuth quota 后同步写回对应账号状态，保留旧账号字段并清除加载标记。
            // 目的：用户手动点击余额后可以立即看到 OAuth 配额刷新结果。
            // 修改原因：手动余额刷新同样可能拿到后端旧 quota_5h/quota_7d 字段，不能直接写入前端状态。
            // 修改方式：复用 getQuotaFromSource 将结果映射为 quota_inner/quota_outer 后再保存。
            // 目的：保证按钮刷新和自动刷新使用同一套字段兼容逻辑。
            const quotaResult = getQuotaFromSource(resultForKey);
            const hasQuota = quotaResult?.quota_inner != null || quotaResult?.quota_outer != null;
            setOauthAccounts(prev => {
              const current = prev[keyObj.key];
              if (!hasQuota && !current) return prev;
              const { _quota_loading: _unusedLoading, ...accountWithoutLoading } = current || {};
              return {
                ...prev,
                [keyObj.key]: {
                  status: accountWithoutLoading.status || 'active',
                  ...accountWithoutLoading,
                  ...(quotaResult?.quota_inner != null ? { quota_inner: quotaResult.quota_inner } : {}),
                  ...(quotaResult?.quota_outer != null ? { quota_outer: quotaResult.quota_outer } : {}),
                  ...(resultForKey?.raw ? { quota_raw: resultForKey.raw } : {}),
                  // 修改原因：手动余额刷新同样不应复制渠道私有用量字段到通用账号状态。
                  // 修改方式：只同步标准 quota 字段和 raw，并用标准 quota 是否存在标记默认双弧可用性。
                  // 目的：让 claude-code 等渠道的额外展示完全通过 key_background 与 quota_display 插槽处理。
                  _quota_unavailable: !hasQuota,
                },
              };
            });
          }
        } catch (e: any) {
          results[keyObj.key] = { supported: false, error: e.message || '网络错误' };
        }
        setBalanceResults({ ...results });
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, activeKeys.length) }, () => runNext()));
    setBalanceLoading(false);
  };

  const addEmptyKey = () => {
    if (formData) {
      const newIdx = formData.api_keys.length;
      updateFormData('api_keys', [...formData.api_keys, { key: '', disabled: false }]);
      // setState 是异步批处理，用 setTimeout 确保在下一次渲染后设焦点
      setTimeout(() => {
        setFocusedKeyIdx(newIdx);
        // 再等一帧滚动到底部
        requestAnimationFrame(() => {
          const container = document.querySelector('[data-key-scroll]');
          if (container) container.scrollTop = container.scrollHeight;
        });
      }, 0);
    }
  };

  const updateKey = (idx: number, keyStr: string) => {
    if (!formData) return;
    const newKeys = [...formData.api_keys];
    newKeys[idx].key = keyStr;
    updateFormData('api_keys', newKeys);
  };

  const handleOAuthKeyFocus = (idx: number, keyStr: string) => {
    // 修改原因：rename API 需要旧 key_id，而受控输入框在 onChange 后只保留新值。
    // 修改方式：输入框获得焦点时按行下标记录旧值，并继续更新当前聚焦行。
    // 目的：onBlur 时可以准确判断是否需要同步 oauth_state.json。
    oauthKeyFocusSnapshotRef.current[idx] = keyStr;
    setFocusedKeyIdx(idx);
  };

  const handleOAuthKeyBlur = async (idx: number, newValue: string) => {
    // 修改原因：用户改 OAuth 账号标识符时，api.yaml 和 oauth_state.json 必须同时迁移到新 key。
    // 修改方式：对已存在于 oauthAccounts 的旧 key 调用后端 rename；失败时恢复输入框旧值并提示错误。
    // 目的：避免保存渠道后新 key 无法解析 access_token。
    setFocusedKeyIdx(null);
    const oldValue = (oauthKeyFocusSnapshotRef.current[idx] || '').trim();
    delete oauthKeyFocusSnapshotRef.current[idx];
    const nextValue = newValue.trim();
    const providerName = (formData?.provider || '').trim();
    if (!isOAuthEngine || !providerName || !oldValue || !nextValue || oldValue === nextValue || !oauthAccounts[oldValue]) return;

    try {
      const res = await apiFetch(`/v1/oauth/accounts/${encodeURIComponent(oldValue)}/rename`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ provider: providerName, new_key_id: nextValue }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        updateKey(idx, oldValue);
        toastError(fmtErr(err, res.status), 'OAuth 账号重命名失败');
        return;
      }
      setOauthAccounts(prev => {
        const account = prev[oldValue];
        if (!account) return prev;
        const next = { ...prev };
        delete next[oldValue];
        next[nextValue] = account;
        return next;
      });
      refreshOAuthAccounts();
    } catch (err: any) {
      updateKey(idx, oldValue);
      toastError(err?.message || '网络错误', 'OAuth 账号重命名失败');
    }
  };

  const openImportModal = (idx: number) => {
    // 修改原因：OAuth 空 Key 行需要一个明确入口接收 refresh_token，而不是把 token 直接保存进 api_keys。
    // 修改方式：记录当前行下标并清空上一次输入，随后由弹窗提交到 /v1/oauth/import。
    // 目的：让 api_keys 中最终只保存邮箱或后端返回的账号标识。
    setImportModalIdx(idx);
    setImportToken('');
  };

  const doImport = async () => {
    if (!importToken.trim() || importModalIdx === null || !formData) return;
    setImporting(true);
    try {
      const keyId = `account_${Date.now()}`;
      const providerName = formData.provider.trim();
      if (!providerName) {
        toastError('渠道名为空，无法导入 OAuth 凭证');
        return;
      }
      const res = await apiFetch('/v1/oauth/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        // 修改原因：后端 OAuth 导入已按 provider name 分层保存，body 必须携带当前渠道名。
        // 修改方式：在原 key_id/type/refresh_token 外增加 provider 字段。
        // 目的：导入同邮箱账号时只写入当前 OAuth 渠道。
        body: JSON.stringify({ provider: providerName, key_id: keyId, type: formData.engine, refresh_token: importToken.trim() }),
      });
      if (res.ok) {
        const data = await res.json();
        if (!data.already_exists) {
          updateKey(importModalIdx, data.key_id || keyId);
        }
        setOauthAccounts(prev => ({ ...prev, [data.key_id || keyId]: prev[data.key_id || keyId] || { type: formData.engine, status: 'active' } }));
        setImportModalIdx(null);
        setImportToken('');
      } else {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), '导入失败');
      }
    } finally {
      setImporting(false);
    }
  };

  const startOAuthLogin = async (idx: number) => {
    // 修改原因：后端现在会按 provider 返回 auto 或 manual 登录模式，前端不能再用单一的弹窗地址轮询流程。
    // 修改方式：authorize 成功后读取 mode；manual 显示粘贴弹窗，auto 监听 callback 成功页的 postMessage。
    // 目的：同时支持 Codex 固定 localhost 回调和 Antigravity/Gemini CLI 等可自定义回调的 OAuth provider。
    if (!formData) return;
    const providerName = formData.provider.trim();
    if (!providerName) {
      toastError('渠道名为空，无法发起 OAuth 登录');
      return;
    }
    try {
      const res = await apiFetch(`/v1/oauth/authorize?type=${encodeURIComponent(formData.engine)}&provider=${encodeURIComponent(providerName)}&origin=${encodeURIComponent(window.location.origin)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), '发起登录失败');
        return;
      }
      const { auth_url, state, mode } = await res.json();
      const authWindow = window.open(auth_url, '_blank', 'width=600,height=700');
      if (!authWindow) {
        toastError('无法打开弹出窗口，请允许弹窗后重试');
        return;
      }

      if (mode === 'manual') {
        // 修改原因：manual 模式的 provider 会跳转到 localhost 失败页，前端无法依赖跨窗口读取地址栏。
        // 修改方式：打开授权窗口后记录本次 state，并显示独立粘贴弹窗让用户提交完整回调 URL。
        // 目的：让 Codex 这类固定 localhost 回调的 OAuth 登录稳定完成 token 交换。
        setOauthManualState({ idx, state, provider: providerName });
        setManualUrl('');
        return;
      }

      const handler = (event: MessageEvent) => {
        // 修改原因：auto 模式由后端成功页通过 postMessage 把 key_id 传回管理前端。
        // 修改方式：只接受 oauth_callback_success 消息，并校验 state 与本次 authorize 返回值一致。
        // 目的：避免其他窗口消息误触发当前 Key 行更新。
        if (event.data?.type !== 'oauth_callback_success') return;
        if (event.data?.state && event.data.state !== state) return;
        if (event.data?.provider && event.data.provider !== providerName) return;
        window.removeEventListener('message', handler);
        const keyId = event.data.key_id;
        if (keyId && !event.data.already_exists) {
          updateKey(idx, keyId);
        }
        refreshOAuthAccounts();
        if (!authWindow.closed) {
          authWindow.close();
        }
      };
      window.addEventListener('message', handler);
      window.setTimeout(() => {
        // 修改原因：后端 pending flow 只保存 5 分钟，过期后继续监听会造成误导。
        // 修改方式：5 分钟后移除本次 postMessage 监听器。
        // 目的：让前端生命周期与后端授权状态有效期保持一致。
        window.removeEventListener('message', handler);
      }, 300000);
    } catch (e) {
      toastError(e instanceof Error ? e.message : String(e), '登录出错');
    }
  };

  const doManualExchange = async () => {
    // 修改原因：manual OAuth 模式需要用户粘贴 localhost 回调 URL 后再由前端提交 code。
    // 修改方式：解析完整 URL 中的 authorization code，校验 state 后调用 /v1/oauth/exchange。
    // 目的：取代 prompt 和跨窗口 location 轮询，减少浏览器安全策略对登录流程的影响。
    if (!oauthManualState || !manualUrl.trim()) return;
    setExchanging(true);
    try {
      const url = new URL(manualUrl.trim());
      const code = url.searchParams.get('code');
      const callbackState = url.searchParams.get('state');
      if (!code) {
        toastError('URL 中未找到 authorization code');
        return;
      }
      if (callbackState && callbackState !== oauthManualState.state) {
        toastError('state 不匹配，可能不是本次登录的回调');
        return;
      }

      const res = await apiFetch('/v1/oauth/exchange', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        // 修改原因：manual exchange 发生在 authorize 之后，后端需要 provider 验证 state 属于当前渠道。
        // 修改方式：oauthManualState 保存发起登录时的 provider，并随 code/state 一起提交。
        // 目的：避免用户粘贴其他渠道的回调 URL 后写入错误 OAuth state 分组。
        body: JSON.stringify({ provider: oauthManualState.provider, code, state: oauthManualState.state }),
      });
      if (res.ok) {
        const data = await res.json();
        if (!data.already_exists) {
          updateKey(oauthManualState.idx, data.key_id || '');
        }
        await refreshOAuthAccounts();
        setOauthManualState(null);
        setManualUrl('');
      } else {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), 'Token 交换失败');
      }
    } catch (e) {
      toastError(e instanceof Error ? e.message : String(e), 'URL 解析失败');
    } finally {
      setExchanging(false);
    }
  };

  const toggleKeyDisabled = (idx: number) => {
    if (!formData) return;
    const newKeys = [...formData.api_keys];
    newKeys[idx].disabled = !newKeys[idx].disabled;
    updateFormData('api_keys', newKeys);
  };

  const deleteKey = async (idx: number) => {
    if (!formData) return;
    const keyValue = (formData.api_keys[idx]?.key || '').trim();
    const providerName = formData.provider.trim();
    if (isOAuthEngine && keyValue && providerName && oauthAccounts[keyValue]) {
      // 修改原因：OAuth 账号删除会立即调用后端清除 refresh_token，和普通表单删行不同，误触后无法恢复。
      // 修改方式：在 DELETE 请求前使用 window.confirm 展示账号标识和不可逆提醒，用户取消时直接返回。
      // 目的：让管理员确认风险后才执行即时删除，避免误删账号 token。
      const confirmOAuthDelete = window.confirm(
        `确定要删除 OAuth 账号 ${keyValue} 吗？\n\n注意：这是即时生效的不可逆操作。删除后 token 将无法恢复，需要重新导入。`,
      );
      if (!confirmOAuthDelete) return;

      try {
        // 修改原因：OAuth Key 删除不只要移出 api.yaml 表单，还要清理当前渠道下的 oauth_state 凭据。
        // 修改方式：删除表单行前调用 DELETE /v1/oauth/accounts/{key}?provider=当前渠道名，失败则保留表单行。
        // 目的：避免保存渠道后残留无用 refresh_token，也避免删除其他渠道同邮箱凭据。
        const res = await apiFetch(`/v1/oauth/accounts/${encodeURIComponent(keyValue)}?provider=${encodeURIComponent(providerName)}`, {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          toastError(fmtErr(err, res.status), 'OAuth 账号删除失败');
          return;
        }
        setOauthAccounts(prev => {
          const next = { ...prev };
          delete next[keyValue];
          return next;
        });
      } catch (err: any) {
        toastError(err?.message || '网络错误', 'OAuth 账号删除失败');
        return;
      }
    }
    updateFormData('api_keys', formData.api_keys.filter((_, i) => i !== idx));
  };

  const handleKeyPaste = (e: ClipboardEvent<HTMLInputElement>, idx: number) => {
    const pastedText = e.clipboardData.getData('text');
    const lines = pastedText.split(/\r?\n|\r/).map(s => s.trim()).filter(Boolean);
    if (lines.length <= 1 || !formData) return;

    e.preventDefault();
    const newKeys = [...formData.api_keys];
    newKeys[idx].key = lines[0];

    const existingSet = new Set(newKeys.map(k => k.key));
    const newKeyObjs = lines.slice(1).filter(k => !existingSet.has(k)).map(k => ({ key: k, disabled: false }));

    newKeys.splice(idx + 1, 0, ...newKeyObjs);
    updateFormData('api_keys', newKeys);
  };

  const copyAllKeys = () => {
    if (!formData) return;
    const activeKeys = formData.api_keys.filter(k => !k.disabled && k.key).map(k => k.key);
    if (!activeKeys.length) return;
    navigator.clipboard.writeText(activeKeys.join('\n'));
    toastSuccess('已复制所有有效密钥');
  };

  const exportOAuthCredentials = async () => {
    if (!formData) return;
    const providerName = formData.provider.trim();
    if (!providerName) {
      toastError('渠道名为空，无法导出 OAuth 凭证');
      return;
    }
    try {
      // 修改原因：OAuth 凭据导出是显式备份操作，后端要求 provider query 来限定导出范围。
      // 修改方式：调用 /v1/oauth/export?provider=当前渠道名，拿到 JSON 后生成本地下载文件。
      // 目的：让管理员可迁移指定渠道的 refresh_token，同时不把其他渠道凭据混入导出文件。
      const res = await apiFetch(`/v1/oauth/export?provider=${encodeURIComponent(providerName)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), '导出失败');
        return;
      }
      const data = await res.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `oauth-${providerName.replace(/[^a-zA-Z0-9._-]+/g, '_')}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      toastSuccess('OAuth 凭证导出已开始');
    } catch (err: any) {
      toastError(err?.message || '网络错误', '导出失败');
    }
  };

  const clearAllKeys = () => {
    if (!formData) return;
    if (formData.api_keys.length === 0) return;
    if (!confirm('确定要清空该渠道的全部密钥吗？此操作仅影响当前编辑中的渠道配置，保存后才会生效。')) return;
    updateFormData('api_keys', []);
  };

  const handleGroupInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && groupInput.trim()) {
      e.preventDefault();
      if (formData && !formData.groups.includes(groupInput.trim())) {
        updateFormData('groups', [...formData.groups, groupInput.trim()]);
      }
      setGroupInput('');
    }
  };

  const removeGroup = (groupToRemove: string) => {
    if (!formData) return;
    const newGroups = formData.groups.filter(g => g !== groupToRemove);
    updateFormData('groups', newGroups.length ? newGroups : ['default']);
  };

  const handleModelInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && modelInput.trim()) {
      e.preventDefault();
      const newModels = modelInput.split(/[, \s]+/).map(s => s.trim()).filter(Boolean);
      if (formData) {
        updateFormData('models', Array.from(new Set([...formData.models, ...newModels])));
      }
      setModelInput('');
    }
  };

  const openFetchModelsDialog = async () => {
    const firstKey = formData?.api_keys.find(k => k.key.trim() && !k.disabled);
    if (!formData?.base_url || !firstKey) {
      toastWarning('请先填写 Base URL 和至少一个启用的 API Key');
      return;
    }

    setFetchingModels(true);
    setModelSearchQuery('');

    try {
      const res = await apiFetch('/v1/channels/fetch_models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          engine: formData.engine,
          base_url: formData.base_url,
          api_key: firstKey.key,
          preferences: formData.preferences,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toastError(err, "获取模型失败");
        return;
      }

      const data = (await res.json()) as any;

      const rawModels: unknown[] = Array.isArray(data)
        ? data
        : Array.isArray(data?.models)
          ? data.models
          : Array.isArray(data?.data)
            ? data.data.map((m: any) => m?.id)
            : [];

      const models: string[] = rawModels
        .map(m => String(m))
        .filter((m): m is string => Boolean(m));

      const uniqueModels: string[] = Array.from(new Set(models));
      if (uniqueModels.length === 0) {
        toastError('未获取到任何模型');
        return;
      }

      setFetchedModels(uniqueModels);
      const existing = new Set(formData.models);
      setSelectedModels(new Set(uniqueModels.filter(m => existing.has(m))));
      setIsFetchModelsOpen(true);
    } catch (err: any) {
      toastError(`获取模型失败: ${err?.message || (typeof err === 'object' ? JSON.stringify(err) : String(err))}`);
    } finally {
      setFetchingModels(false);
    }
  };

  const toggleModelSelect = (model: string) => {
    const newSet = new Set(selectedModels);
    if (newSet.has(model)) newSet.delete(model);
    else newSet.add(model);
    setSelectedModels(newSet);
  };

  const filteredFetchedModels = fetchedModels.filter(m => {
    if (!modelSearchQuery) return true;
    const q = modelSearchQuery.toLowerCase();
    const display = getModelDisplayName(m);
    return m.toLowerCase().includes(q) || display.toLowerCase().includes(q);
  });

  const selectAllVisible = () => {
    setSelectedModels(new Set(filteredFetchedModels));
  };

  const deselectAllVisible = () => {
    const visible = new Set(filteredFetchedModels);
    const newSet = new Set(selectedModels);
    visible.forEach(m => newSet.delete(m));
    setSelectedModels(newSet);
  };

  const confirmFetchModels = () => {
    updateFormData('models', Array.from(selectedModels));
    setIsFetchModelsOpen(false);
  };

  const copyAllModels = () => {
    if (!formData || formData.models.length === 0) return;
    navigator.clipboard.writeText(formData.models.join(', '));
    setCopiedModels(true);
    setTimeout(() => setCopiedModels(false), 2000);
  };

  function getAliasMap(): Map<string, string> {
    const map = new Map<string, string>();
    formData?.mappings.forEach(m => {
      if (m.from && m.to) map.set(m.to, m.from);
    });
    return map;
  }

  function getModelDisplayName(model: string): string {
    const aliasMap = getAliasMap();
    return aliasMap.get(model) || model;
  }

  const formatJsonOnBlur = (value: string, setter: (v: string) => void, fieldName: string) => {
    if (!value.trim()) return;
    try {
      const obj = JSON.parse(value);
      const pretty = JSON.stringify(obj, null, 2);
      setter(pretty);
    } catch (err: any) {
      toastWarning(`${fieldName} JSON 格式错误: ${err.message}`);
    }
  };

  const handleMappingChange = (idx: number, field: 'from' | 'to', value: string) => {
    if (!formData) return;
    const newMappings = [...formData.mappings];
    newMappings[idx][field] = value;
    updateFormData('mappings', newMappings);
    setModelDisplayKey(prev => prev + 1);
  };

  const handlePluginSheetUpdate = (payload: { enabled_plugins: string[]; preferences_patch: Record<string, any>; preferences_delete: string[] }) => {
    setFormData(prev => {
      if (!prev) return prev;
      const nextPrefs: Record<string, any> = { ...(prev.preferences || {}) };
      nextPrefs.enabled_plugins = payload.enabled_plugins;
      for (const [k, v] of Object.entries(payload.preferences_patch || {})) {
        nextPrefs[k] = v;
      }
      for (const k of payload.preferences_delete || []) {
        delete nextPrefs[k];
      }
      return { ...prev, preferences: nextPrefs };
    });
  };

  const handleDeleteProvider = async (idx: number) => {
    const provider = providers[idx];
    const providerId = String(provider?.provider || '').trim();
    const name = providerId || `渠道 ${idx + 1}`;
    if (!providerId) {
      toastError('删除失败：渠道名为空');
      return;
    }
    if (!confirm(`确定要删除渠道 "${name}" 吗？此操作不可撤销。`)) return;

    try {
      // 修改原因：删除主渠道不能再提交删除后的完整 providers 数组，否则会覆盖其他浏览器的新配置。
      // 修改方式：调用 DELETE /v1/providers/{provider_id}，成功后统一 refreshProviders 获取后端最新列表。
      // 目的：让删除操作只影响一个渠道，并保留并发修改。
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        await refreshProviders();
        toastError(`已删除渠道 "${name}"`);
      } else {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), '删除失败');
      }
    } catch (err: any) {
      toastError(err?.message || '网络错误');
    }
  };

  const handleToggleProvider = async (idx: number) => {
    const provider = providers[idx];
    const providerId = String(provider?.provider || '').trim();
    if (!providerId) {
      toastError('操作失败：渠道名为空');
      return;
    }
    const newEnabled = provider.enabled === false ? true : false;
    const updatedProvider = { ...provider, enabled: newEnabled };

    try {
      // 修改原因：启用或禁用只改一个 provider，继续全量提交会覆盖其他设备上的渠道改动。
      // 修改方式：把修改 enabled 后的 provider 对象 PUT 到对应 provider_id，再刷新完整列表。
      // 目的：保持开关操作的影响范围仅限当前渠道。
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(updatedProvider),
      });
      if (res.ok) {
        await refreshSingleProvider(providerId);
      } else {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), '操作失败');
      }
    } catch (err: any) {
      toastError(err?.message || '网络错误');
    }
  };

  const handleCopyProvider = (provider: any) => {
    const copy = JSON.parse(JSON.stringify(provider));
    const originalName = copy.provider || 'channel';
    copy.provider = `${originalName}_copy`;
    // 修改原因：OAuth 渠道复制后还需要复制后端按 provider 分组保存的 token state。
    // 修改方式：在复制出的表单对象上写入临时 _copiedFrom 字段，保存成功后据此调用后端复制接口。
    // 目的：只在前端流程中保存来源信息，不把临时字段写入 api.yaml。
    copy._copiedFrom = originalName;
    openModal(copy, null);
    toastSuccess('已复制渠道配置，请修改后保存');
  };

  // ── 子渠道操作 ──
  const handleToggleSubChannel = async (parentIdx: number, subIdx: number) => {
    const parent = providers[parentIdx];
    const providerId = String(parent.provider || '').trim();
    if (!providerId) {
      toastError('操作失败：主渠道名为空');
      return;
    }
    const subs = [...(parent.sub_channels || [])];
    subs[subIdx] = { ...subs[subIdx], enabled: subs[subIdx].enabled === false ? true : false };
    const updatedParent = { ...parent, sub_channels: subs };
    try {
      // 修改原因：子渠道开关实际只修改所属主渠道的 sub_channels 字段，全量 POST 会覆盖其他渠道的并发改动。
      // 修改方式：构造更新后的主渠道对象，并通过 PUT /v1/providers/{provider_id} 保存单个主渠道。
      // 目的：让子渠道启用状态变更只影响所属主渠道，成功后再从后端刷新最新列表。
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(updatedParent),
      });
      if (res.ok) await refreshSingleProvider(providerId);
      else toastError('操作失败');
    } catch { toastError('网络错误'); }
  };

  const handleDeleteSubChannel = async (parentIdx: number, subIdx: number) => {
    const parent = providers[parentIdx];
    const providerId = String(parent.provider || '').trim();
    if (!providerId) {
      toastError('删除失败：主渠道名为空');
      return;
    }
    const sub = (parent.sub_channels || [])[subIdx];
    const name = sub?.remark || sub?.engine || `子渠道 ${subIdx + 1}`;
    if (!confirm(`确定要删除子渠道 "${name}" 吗？`)) return;
    const subs = (parent.sub_channels || []).filter((_: any, i: number) => i !== subIdx);
    const updatedParent = { ...parent, sub_channels: subs.length > 0 ? subs : undefined };
    try {
      // 修改原因：删除子渠道同样只改所属主渠道的 sub_channels 字段，不能再把完整 providers 数组写回后端。
      // 修改方式：把删除后的主渠道对象 PUT 到单个 provider 路径，并保留空数组清理为 undefined 的旧行为。
      // 目的：降低子渠道删除操作的写入范围，避免覆盖其他渠道的新配置。
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(updatedParent),
      });
      if (res.ok) { await refreshSingleProvider(providerId); }
      else toastError('删除失败');
    } catch { toastError('网络错误'); }
  };

  const openSubChannelEdit = async (parentIdx: number, subIdx: number) => {
    const parent = providers[parentIdx];
    const providerId = String(parent?.provider || '').trim();
    if (!parent || !providerId) {
      toastError('编辑失败：主渠道名为空');
      return;
    }

    // 修改原因：子渠道编辑保存会把子渠道写回所属主渠道，如果这里继续使用旧 parent，仍可能覆盖其他设备刚改过的主渠道字段。
    // 修改方式：先按主渠道 provider id 请求最新主渠道，成功后同步替换 providers 中对应项，再从 freshParent 取子渠道填表。
    // 目的：让子渠道编辑和主渠道编辑一样，以后端最新配置作为表单初始化来源。
    let freshParent = parent;
    try {
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'GET',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const data = await res.json();
        if (data?.provider) {
          freshParent = data.provider;
          setProviders(prev => prev.map((item, idx) => idx === parentIdx ? freshParent : item));
        } else {
          toastWarning('获取主渠道最新数据失败，已使用页面缓存继续编辑');
        }
      } else {
        toastWarning('获取主渠道最新数据失败，已使用页面缓存继续编辑');
      }
    } catch {
      toastWarning('获取主渠道最新数据失败，已使用页面缓存继续编辑');
    }

    const sub = (freshParent.sub_channels || [])[subIdx];
    if (!sub) {
      toastError('编辑失败：子渠道不存在或已被删除');
      return;
    }
    // 构造一个虚拟 provider 给 openModal，合并主渠道的 key 等
    setEditingSubChannel({ parentIdx, subIdx });
    await openModal({
      provider: `${freshParent.provider}:${sub.engine || 'sub'}`,
      engine: sub.engine || '',
      base_url: sub.base_url || freshParent.base_url || '',
      token_url: sub.token_url || freshParent.token_url || '',
      api: freshParent.api,
      model: sub.model || sub.models || [],
      model_prefix: sub.model_prefix || freshParent.model_prefix || '',
      enabled: sub.enabled !== false,
      remark: sub.remark || '',
      groups: freshParent.groups || ['default'],
      preferences: {
        ...(freshParent.preferences || {}),
        ...(sub.preferences || {}),
      },
      sub_channels: [], // 子渠道不能再分子渠道
    }, null);
  };

  // 构建子渠道的虚拟 provider 对象（用于测试等场景）
  const buildSubChannelProvider = (parentIdx: number, subIdx: number): any | null => {
    const parent = providers[parentIdx];
    const sub = (parent.sub_channels || [])[subIdx];
    if (!sub) return null;
    return {
      provider: `${parent.provider}:${sub.engine || 'sub'}`,
      engine: sub.engine || '',
      base_url: sub.base_url || parent.base_url || '',
      token_url: sub.token_url || parent.token_url || '',
      api: parent.api,
      model: sub.model || sub.models || [],
      model_prefix: sub.model_prefix || parent.model_prefix || '',
      enabled: sub.enabled !== false,
      groups: parent.groups || ['default'],
      preferences: { ...(parent.preferences || {}), ...(sub.preferences || {}) },
    };
  };

  // 修改原因：历史代码多处调用 sortByWeight，本次只把实现统一委托给组件外的纯 helper。
  // 修改方式：保留原函数名作为局部别名，减少保存子渠道等无关逻辑的改动范围。
  // 目的：在引入 refreshProviders 的同时保持既有调用点稳定。
  const sortByWeight = sortProvidersByWeight;

  const handleUpdateWeight = async (idx: number, newWeight: number) => {
    const provider = providers[idx];
    const providerId = String(provider?.provider || '').trim();
    if (!providerId) {
      toastError('权重更新失败：渠道名为空');
      return;
    }
    const updatedProvider = {
      ...provider,
      preferences: { ...(provider.preferences || {}), weight: newWeight },
    };

    try {
      // 修改原因：权重更新只改变当前 provider.preferences.weight，全量保存会覆盖并发修改。
      // 修改方式：PUT 单个 provider 后调用 refreshProviders，让后端最新排序结果回到页面。
      // 目的：保持权重编辑的局部性，同时继续按权重降序展示。
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(updatedProvider),
      });
      if (res.ok) {
        await refreshSingleProvider(providerId);
      } else {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), '权重更新失败');
      }
    } catch (err: any) {
      toastError(err?.message || '权重更新失败');
    }
  };

  const getVirtualProviderWeight = (provider: any): number => {
    // 修改原因：左侧渠道面板要求按 preferences.weight 降序展示，并把禁用渠道放到底部。
    // 修改方式：统一读取 preferences.weight，缺失时回退到 provider.weight，最后回退为 0。
    // 目的：避免不同位置重复实现权重读取规则导致排序不一致。
    return Number(provider?.preferences?.weight ?? provider?.weight ?? 0) || 0;
  };

  const getProviderModelOptions = (provider: any): ProviderModelOption[] => {
    // 修改原因：虚拟模型画布需要从真实渠道配置中提取可拖拽的对外模型名。
    // 修改方式：遍历 provider.model 或 provider.models，将字符串模型和 {upstream: alias} 映射都转成 displayName/upstreamName。
    // 目的：左栏模型列表、渠道节点下拉和节点说明使用一致的数据源。
    const rawModels = Array.isArray(provider?.model) ? provider.model : Array.isArray(provider?.models) ? provider.models : [];
    const prefix = String(provider?.model_prefix || '').trim();
    const options: ProviderModelOption[] = [];
    const seen = new Set<string>();

    const appendOption = (displayName: string, upstreamName: string) => {
      const cleanDisplay = String(displayName || '').trim();
      const cleanUpstream = String(upstreamName || '').trim();
      if (!cleanDisplay || !cleanUpstream) return;
      const key = `${cleanDisplay}\u0000${cleanUpstream}`;
      if (seen.has(key)) return;
      seen.add(key);
      options.push({ displayName: cleanDisplay, upstreamName: cleanUpstream, hasMapping: cleanDisplay !== cleanUpstream });
    };

    rawModels.forEach((model: any) => {
      if (typeof model === 'string') {
        const upstream = model.trim();
        if (!upstream) return;
        appendOption(model === '*' || !prefix ? upstream : `${prefix}${upstream}`, upstream);
      } else if (model && typeof model === 'object') {
        Object.entries(model).forEach(([upstream, alias]) => {
          const aliasText = String(alias || '').trim();
          const upstreamText = String(upstream || '').trim();
          if (!aliasText || !upstreamText) return;
          appendOption(prefix ? `${prefix}${aliasText}` : aliasText, upstreamText);
        });
      }
    });

    return options;
  };

  const findProviderModelOption = (provider: any, modelName: string): ProviderModelOption | null => {
    // 修改原因：模型节点和渠道节点都需要显示它们最终会匹配到哪个渠道模型。
    // 修改方式：先按对外名精确匹配，再按 pool_sharing 规则尝试 prefix + modelName。
    // 目的：前端展示的“匹配到 N 个渠道”与后端虚拟路由匹配规则保持一致。
    const requested = String(modelName || '').trim();
    if (!requested) return null;
    const options = getProviderModelOptions(provider);
    const direct = options.find(option => option.displayName === requested);
    if (direct) return direct;

    const prefix = String(provider?.model_prefix || '').trim();
    const poolSharing = readBooleanPreference(provider?.preferences?.pool_sharing);
    if (!prefix || !poolSharing || requested.startsWith(prefix)) return null;
    return options.find(option => option.displayName === `${prefix}${requested}`) || null;
  };

  const getProviderByName = (providerName: string): any | null => {
    // 修改原因：渠道节点现在可以引用运行时展开后的子渠道 provider 名。
    // 修改方式：改为在前端展开后的 virtualRoutingProviderItems 中按 provider 字段精确查找。
    // 目的：让右侧链条、渠道模型下拉和节点说明都能识别子渠道。
    return virtualRoutingProviderItems.find(provider => String(provider?.provider || '') === providerName) || null;
  };

  const getMatchingProviderCount = (modelName: string): number => {
    // 修改原因：模型节点的全局匹配范围应与后端运行时 providers 一致，必须包含子渠道。
    // 修改方式：复用 findProviderModelOption，并在前端展开后的 provider 列表中统计启用项。
    // 目的：让“匹配到 N 个渠道”的提示不会漏掉子渠道模型。
    return virtualRoutingProviderItems.filter(provider => provider?.enabled !== false && findProviderModelOption(provider, modelName)).length;
  };

  const formatProviderModelOption = (option: ProviderModelOption): string => {
    // 修改原因：模型列表需要同时表达对外模型名和上游原名。
    // 修改方式：两者不同时使用“对外名 → 上游名”，相同时只显示一个名称。
    // 目的：让 mapping 和 prefix 造成的名称差异可以直接被看见。
    return option.hasMapping ? `${option.displayName} → ${option.upstreamName}` : option.displayName;
  };

  const describeVirtualChannelNode = (node: VirtualModelChainNode, virtualName: string): string => {
    // 修改原因：渠道节点需要展示“渠道名 + 使用的模型”，而不是只展示渠道名。
    // 修改方式：使用节点 model 覆盖值或虚拟模型名回查渠道模型映射。
    // 目的：用户可以在链条中直接确认该渠道节点会把请求发给哪个上游模型。
    const provider = getProviderByName(node.value);
    if (!provider) return '渠道未找到';
    const requestedModel = String(node.model || virtualName || '').trim();
    const matched = findProviderModelOption(provider, requestedModel);
    if (!matched) return requestedModel ? `使用模型：${requestedModel}（未匹配）` : '未指定模型';
    return `使用模型：${formatProviderModelOption(matched)}`;
  };

  const updateVirtualModelsDraft = (updater: (prev: Record<string, VirtualModelConfig>) => Record<string, VirtualModelConfig>) => {
    // 修改原因：画布上的编辑应先成为本地草稿，点击保存后再写回后端。
    // 修改方式：集中包装 setVirtualModels，并同步设置 dirty 标记。
    // 目的：避免每一次拖拽或输入都立即请求后端，也提醒用户保存更改。
    setVirtualModels(prev => updater(prev));
    setVirtualModelsDirty(true);
  };

  const serializeVirtualModels = (source: Record<string, VirtualModelConfig>): Record<string, VirtualModelConfig> => {
    // 修改原因：保存前必须把画布草稿清理成 preferences.virtual_models 所需格式。
    // 修改方式：修剪模型名、移除空节点，并只为 channel 节点保留有效 model 覆盖。
    // 目的：保证 POST /v1/api_config/update 收到的 chain 数组结构简洁、稳定。
    const cleaned: Record<string, VirtualModelConfig> = {};
    Object.entries(source).forEach(([rawName, config]) => {
      const name = String(rawName || '').trim();
      if (!name || !config || typeof config !== 'object') return;
      const chain = (Array.isArray(config.chain) ? config.chain : [])
        .map(node => ({
          type: node.type === 'channel' ? 'channel' as const : 'model' as const,
          value: String(node.value || '').trim(),
          model: node.type === 'channel' && node.model ? String(node.model).trim() : undefined,
        }))
        .filter(node => node.value)
        .map(node => {
          if (node.type === 'channel' && node.model) return node;
          const { model: _unused, ...rest } = node;
          return rest;
        });
      cleaned[name] = { enabled: config.enabled !== false, chain };
    });
    return cleaned;
  };

  const saveVirtualModels = async (nextVirtualModels: Record<string, VirtualModelConfig>) => {
    // 修改原因：虚拟模型配置保存在全局 preferences 下，不能通过 providers 保存接口混入渠道列表。
    // 修改方式：向 /v1/api_config/update 只提交 preferences.virtual_models 字段。
    // 目的：减少保存影响范围，并复用后端现有配置持久化流程。
    const res = await apiFetch('/v1/api_config/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ preferences: { virtual_models: nextVirtualModels } }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(fmtErr(err, res.status));
    }
    setVirtualModels(nextVirtualModels);
    setVirtualModelsDirty(false);
  };

  const handleSaveVirtualModelsDraft = async () => {
    // 修改原因：新 UI 支持一次编辑多个虚拟模型和链条节点。
    // 修改方式：保存按钮统一序列化当前 virtualModels 草稿后提交 preferences.virtual_models。
    // 目的：让拖拽排序、节点删除和启用状态可以一次性落盘。
    try {
      const cleaned = serializeVirtualModels(virtualModels);
      await saveVirtualModels(cleaned);
      toastSuccess('虚拟模型路由已保存');
    } catch (err: any) {
      toastError(`保存失败: ${err?.message || err}`);
    }
  };

  const handleAddVirtualModel = () => {
    // 修改原因：右侧编辑区顶部需要直接新建虚拟模型，不再打开弹窗。
    // 修改方式：读取虚拟模型名输入框和启用开关，创建空链条草稿并默认展开。
    // 目的：用户可以创建后立即从左侧拖拽模型或渠道到链条中。
    const name = virtualDraftName.trim();
    if (!name) {
      toastWarning('虚拟模型名为必填项');
      return;
    }
    if (virtualModels[name]) {
      toastWarning('该虚拟模型已存在');
      return;
    }
    updateVirtualModelsDraft(prev => ({ ...prev, [name]: { enabled: virtualDraftEnabled, chain: [] } }));
    setExpandedVirtualModels(prev => new Set(prev).add(name));
    setVirtualDraftName('');
    setVirtualDraftEnabled(true);
  };

  const updateVirtualModelConfig = (name: string, patch: Partial<VirtualModelConfig>) => {
    // 修改原因：启用状态和链条内容都在折叠卡片内直接编辑。
    // 修改方式：按虚拟模型名合并更新局部字段，并保持其他模型不变。
    // 目的：避免局部编辑覆盖同一页面中的其他虚拟模型草稿。
    updateVirtualModelsDraft(prev => ({
      ...prev,
      [name]: { enabled: prev[name]?.enabled !== false, chain: Array.isArray(prev[name]?.chain) ? prev[name].chain : [], ...patch },
    }));
  };

  const updateVirtualNode = (virtualName: string, idx: number, patch: Partial<VirtualModelChainNode>) => {
    // 修改原因：节点类型、模型名、渠道名和渠道模型覆盖都需要在画布内直接调整。
    // 修改方式：按虚拟模型名和节点索引更新链条，并在切回 model 类型时删除 model 覆盖字段。
    // 目的：保存到 preferences.virtual_models 的节点结构保持清晰。
    const current = virtualModels[virtualName];
    if (!current) return;
    const nextChain = (Array.isArray(current.chain) ? current.chain : []).map((node, nodeIdx) => {
      if (nodeIdx !== idx) return node;
      const nextNode = { ...node, ...patch };
      if (patch.type === 'model') delete nextNode.model;
      return nextNode;
    });
    updateVirtualModelConfig(virtualName, { chain: nextChain });
  };

  const moveVirtualNode = (virtualName: string, fromIdx: number, toIdx: number) => {
    // 修改原因：链条节点顺序就是虚拟路由优先级，需要支持原生拖拽重排。
    // 修改方式：在指定虚拟模型的 chain 数组中移动元素，不立即保存到后端。
    // 目的：用户可以调整完整链条后再统一保存。
    const current = virtualModels[virtualName];
    if (!current || fromIdx === toIdx) return;
    const nextChain = [...(Array.isArray(current.chain) ? current.chain : [])];
    if (fromIdx < 0 || fromIdx >= nextChain.length) return;
    const [item] = nextChain.splice(fromIdx, 1);
    const safeToIdx = Math.max(0, Math.min(toIdx, nextChain.length));
    nextChain.splice(safeToIdx, 0, item);
    updateVirtualModelConfig(virtualName, { chain: nextChain });
  };

  const insertVirtualNode = (virtualName: string, node: VirtualModelChainNode, insertIndex?: number) => {
    // 修改原因：左侧拖拽和底部添加按钮都需要把新节点插入某个虚拟模型链条。
    // 修改方式：复制目标 chain 后按传入索引插入，未传索引时追加到末尾。
    // 目的：统一外部拖入、手动添加和后续扩展的节点创建行为。
    const current = virtualModels[virtualName];
    if (!current) return;
    const nextChain = [...(Array.isArray(current.chain) ? current.chain : [])];
    const targetIndex = insertIndex == null ? nextChain.length : Math.max(0, Math.min(insertIndex, nextChain.length));
    nextChain.splice(targetIndex, 0, node);
    updateVirtualModelConfig(virtualName, { chain: nextChain });
    setExpandedVirtualModels(prev => new Set(prev).add(virtualName));
  };

  const appendVirtualNodeByType = (virtualName: string) => {
    // 修改原因：链条底部需要一个保底添加入口，便于不使用拖拽时也能编辑。
    // 修改方式：读取当前虚拟模型的添加类型选择，追加空 model 或 channel 节点。
    // 目的：满足键盘输入和移动端场景下的节点创建需求。
    const nodeType = virtualAddNodeTypes[virtualName] || 'model';
    insertVirtualNode(virtualName, { type: nodeType, value: '' });
  };

  const toggleVirtualModelExpanded = (name: string) => {
    // 修改原因：右侧虚拟模型列表需要可折叠，减少多链条同时展开时的页面高度。
    // 修改方式：用 Set 保存已展开的虚拟模型名，点击标题时切换存在状态。
    // 目的：让用户可以专注编辑单个虚拟链条。
    setExpandedVirtualModels(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  };

  const toggleVirtualProviderExpanded = (name: string) => {
    // 修改原因：左侧渠道面板可能包含大量模型，需要按渠道折叠展示。
    // 修改方式：用 Set 保存已展开的渠道名，点击渠道头部时切换。
    // 目的：保持画布轻量，同时让模型列表可按需查看和拖拽。
    setExpandedVirtualProviders(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  };

  const getPreferredVirtualTarget = (): string | null => {
    // 修改原因：左侧模型行的“+”按钮需要知道要添加到哪个虚拟模型。
    // 修改方式：优先选择当前已展开的第一个虚拟模型，否则选择列表中的第一个虚拟模型。
    // 目的：在不弹出额外选择器的情况下提供快速添加能力。
    const names = Object.keys(virtualModels);
    return names.find(name => expandedVirtualModels.has(name)) || names[0] || null;
  };

  const handlePanelModelQuickAdd = (modelName: string) => {
    // 修改原因：左栏每个模型除了可拖拽，也需要提供轻量的“+”添加入口。
    // 修改方式：把模型节点追加到当前优先目标虚拟模型链条。
    // 目的：用户不方便拖拽时仍可快速构造 model 节点。
    const target = getPreferredVirtualTarget();
    if (!target) {
      toastWarning('请先新建虚拟模型');
      return;
    }
    insertVirtualNode(target, { type: 'model', value: modelName });
  };

  const handlePanelChannelQuickAdd = (providerName: string) => {
    // 修改原因：渠道卡片整体可拖拽，但也需要按钮形式的备用入口。
    // 修改方式：把 channel 节点追加到当前优先目标虚拟模型链条。
    // 目的：兼顾不使用拖拽的操作方式。
    const target = getPreferredVirtualTarget();
    if (!target) {
      toastWarning('请先新建虚拟模型');
      return;
    }
    insertVirtualNode(target, { type: 'channel', value: providerName });
  };

  const handleDeleteVirtualModel = async (name: string) => {
    // 修改原因：虚拟模型现在是渠道列表中的特殊卡片，删除操作应与普通渠道一样直接生效。
    // 修改方式：确认后从 preferences.virtual_models 草稿中移除目标项，并复用 saveVirtualModels 提交后端。
    // 目的：避免用户删除列表卡片后还需要寻找旧画布中的“保存全部”按钮。
    const displayName = name || '未命名虚拟模型';
    if (!confirm(`确定要删除虚拟模型 "${displayName}" 吗？此操作会立即写回配置。`)) return;
    const nextVirtualModels = { ...virtualModels };
    delete nextVirtualModels[name];
    try {
      await saveVirtualModels(serializeVirtualModels(nextVirtualModels));
      setExpandedVirtualModels(prev => {
        const next = new Set(prev);
        next.delete(name);
        return next;
      });
      if (editingVirtualName === name) setIsVirtualModalOpen(false);
      toastWarning(`已删除虚拟模型 "${displayName}"`);
    } catch (err: any) {
      toastError(`删除失败: ${err?.message || err}`);
    }
  };

  const setVirtualDragPayload = (e: DragEvent<HTMLElement>, payload: VirtualDragPayload) => {
    // 修改原因：原生拖拽事件的数据通道只接受字符串。
    // 修改方式：把拖拽来源、模型名、渠道名或节点索引序列化为 JSON 存入 dataTransfer。
    // 目的：drop 时可以可靠地区分从左栏创建节点和链条内部排序。
    const raw = JSON.stringify(payload);
    e.dataTransfer.setData('application/json', raw);
    e.dataTransfer.setData('text/plain', raw);
    e.dataTransfer.effectAllowed = payload.source === 'chain-node' ? 'move' : 'copy';
  };

  const readVirtualDragPayload = (e: DragEvent<HTMLElement>): VirtualDragPayload | null => {
    // 修改原因：不同浏览器对自定义 MIME 类型支持存在差异。
    // 修改方式：优先读 application/json，失败时回退 text/plain，并捕获 JSON 解析错误。
    // 目的：让原生拖拽在更多浏览器中稳定工作。
    const raw = e.dataTransfer.getData('application/json') || e.dataTransfer.getData('text/plain');
    if (!raw) return null;
    try {
      return JSON.parse(raw) as VirtualDragPayload;
    } catch {
      return null;
    }
  };

  const handlePanelModelDragStart = (e: DragEvent<HTMLElement>, modelName: string) => {
    // 修改原因：从左栏模型名拖入右侧链条时需要创建 model 节点。
    // 修改方式：把模型对外名写入拖拽 payload。
    // 目的：drop 到任意虚拟链条后可以直接生成 {type: 'model', value: modelName}。
    e.stopPropagation();
    setVirtualDragPayload(e, { source: 'panel-model', modelName });
  };

  const handlePanelChannelDragStart = (e: DragEvent<HTMLElement>, providerName: string) => {
    // 修改原因：从左栏渠道卡片拖入右侧链条时需要创建 channel 节点。
    // 修改方式：把渠道名写入拖拽 payload。
    // 目的：drop 后自动填入渠道名，用户再按需选择模型覆盖。
    setVirtualDragPayload(e, { source: 'panel-channel', providerName });
  };

  const handleChainNodeDragStart = (e: DragEvent<HTMLElement>, virtualName: string, fromIndex: number) => {
    // 修改原因：链条内部节点可拖拽重排，但不能与左栏拖入混淆。
    // 修改方式：payload 中保存虚拟模型名和原始索引。
    // 目的：drop 时只在同一个虚拟模型链条内执行移动。
    setVirtualDragPayload(e, { source: 'chain-node', virtualName, fromIndex });
  };

  const handleVirtualDrop = (e: DragEvent<HTMLElement>, virtualName: string, insertIndex?: number) => {
    // 修改原因：右侧链条既接收左栏拖入的新节点，也接收内部重排。
    // 修改方式：根据 payload.source 分别执行插入 model、插入 channel 或移动节点。
    // 目的：用原生 HTML5 Drag and Drop API 完成可视化链条编辑，避免引入拖拽库。
    e.preventDefault();
    const payload = readVirtualDragPayload(e);
    if (!payload) return;

    if (payload.source === 'panel-model') {
      insertVirtualNode(virtualName, { type: 'model', value: payload.modelName }, insertIndex);
    } else if (payload.source === 'panel-channel') {
      insertVirtualNode(virtualName, { type: 'channel', value: payload.providerName }, insertIndex);
    } else if (payload.source === 'chain-node' && payload.virtualName === virtualName) {
      const currentLength = virtualModels[virtualName]?.chain?.length || 0;
      const targetIndex = insertIndex == null ? Math.max(0, currentLength - 1) : insertIndex;
      moveVirtualNode(virtualName, payload.fromIndex, targetIndex);
    }
  };

  const openVirtualModelModal = (name: string | null = null) => {
    // 修改原因：虚拟模型从内联画布迁移到独立抽屉，需要在打开时准备一份可取消的编辑草稿。
    // 修改方式：把原始名称、名称输入、启用状态和 chain 深拷贝到抽屉状态中。
    // 目的：用户点击列表中的编辑或新建按钮后，可以在抽屉中完成完整链条编辑再保存。
    const config = name ? virtualModels[name] : null;
    setEditingVirtualName(name);
    setVirtualDraftName(name || '');
    setVirtualDraftEnabled(config?.enabled !== false);
    setVirtualEditorChain((Array.isArray(config?.chain) ? config!.chain : []).map(node => ({ ...node })));
    setVirtualModelsDirty(false);
    // 修改原因：移动端渠道面板的默认状态应为折叠，避免重新打开抽屉时沿用上一次展开状态。
    // 修改方式：每次打开虚拟模型抽屉时重置移动端面板展开状态。
    // 目的：保证用户进入抽屉后优先看到链条编辑区。
    setIsVirtualMobileProviderPanelOpen(false);
    setIsVirtualModalOpen(true);
  };

  const updateVirtualEditorChainDraft = (updater: (prev: VirtualModelChainNode[]) => VirtualModelChainNode[]) => {
    // 修改原因：抽屉编辑应先修改本地 chain 草稿，不能在保存前污染全局 virtualModels。
    // 修改方式：集中包装 setVirtualEditorChain，并同步记录未保存状态。
    // 目的：让取消关闭抽屉时可以丢弃本次编辑，保存时再提交 preferences.virtual_models。
    setVirtualEditorChain(prev => updater(prev));
    setVirtualModelsDirty(true);
  };

  const updateVirtualEditorNode = (idx: number, patch: Partial<VirtualModelChainNode>) => {
    // 修改原因：抽屉右栏节点需要独立编辑类型、渠道、模型和模型覆盖。
    // 修改方式：按索引合并补丁，切回 model 节点时移除 channel 专用的 model 字段。
    // 目的：保证保存后的 chain 结构仍符合后端的 preferences.virtual_models 格式。
    updateVirtualEditorChainDraft(prev => prev.map((node, nodeIdx) => {
      if (nodeIdx !== idx) return node;
      const nextNode = { ...node, ...patch };
      if (patch.type === 'model') delete nextNode.model;
      return nextNode;
    }));
  };

  const insertVirtualEditorNode = (node: VirtualModelChainNode, insertIndex?: number) => {
    // 修改原因：左栏加号、拖拽投放和底部添加按钮都需要向抽屉 chain 插入节点。
    // 修改方式：复制当前 chain，并把新节点插入到指定位置，缺省时追加到末尾。
    // 目的：统一所有入口的节点创建行为，避免不同 UI 产生不同数据结构。
    updateVirtualEditorChainDraft(prev => {
      const next = [...prev];
      const targetIndex = insertIndex == null ? next.length : Math.max(0, Math.min(insertIndex, next.length));
      next.splice(targetIndex, 0, node);
      return next;
    });
  };

  const moveVirtualEditorNode = (fromIdx: number, toIdx: number) => {
    // 修改原因：虚拟模型链条顺序表示路由优先级，抽屉中仍需要支持拖拽排序。
    // 修改方式：在本地 chain 草稿内移动数组元素，并限制目标索引边界。
    // 目的：不引入新依赖也能保留原生 HTML5 拖拽排序能力。
    updateVirtualEditorChainDraft(prev => {
      if (fromIdx === toIdx || fromIdx < 0 || fromIdx >= prev.length) return prev;
      const next = [...prev];
      const [item] = next.splice(fromIdx, 1);
      const safeToIdx = Math.max(0, Math.min(toIdx, next.length));
      next.splice(safeToIdx, 0, item);
      return next;
    });
  };

  const swapVirtualEditorNode = (idx: number, direction: -1 | 1) => {
    // 修改原因：触摸屏不会触发 HTML5 原生 Drag and Drop 事件，手机端需要不依赖拖拽的排序入口。
    // 修改方式：根据上移或下移方向计算相邻目标索引，并在本地 chain 草稿中交换两个节点。
    // 目的：在保留桌面拖拽排序的同时，让移动端用户也能调整虚拟模型链条优先级。
    updateVirtualEditorChainDraft(prev => {
      const targetIdx = idx + direction;
      if (idx < 0 || idx >= prev.length || targetIdx < 0 || targetIdx >= prev.length) return prev;
      const next = [...prev];
      [next[idx], next[targetIdx]] = [next[targetIdx], next[idx]];
      return next;
    });
  };

  const appendVirtualEditorNodeByType = () => {
    // 修改原因：移动端或不使用拖拽的场景仍需要显式添加节点入口。
    // 修改方式：读取当前虚拟模型的添加类型状态，向抽屉 chain 追加空节点。
    // 目的：满足用户通过表单逐项填写 model 或 channel 节点的操作方式。
    const key = virtualDraftName.trim() || editingVirtualName || '__new_virtual_model__';
    const nodeType = virtualAddNodeTypes[key] || 'model';
    insertVirtualEditorNode({ type: nodeType, value: '' });
  };

  const handleVirtualEditorDrop = (e: DragEvent<HTMLElement>, insertIndex?: number) => {
    // 修改原因：抽屉右栏只编辑当前虚拟模型，需要单独处理左栏拖入和内部重排。
    // 修改方式：识别拖拽 payload，外部模型或渠道创建新节点，内部节点只在本抽屉内移动。
    // 目的：让新的抽屉编辑器继续使用原生 HTML5 drag and drop，不增加依赖。
    e.preventDefault();
    const payload = readVirtualDragPayload(e);
    if (!payload) return;

    if (payload.source === 'panel-model') {
      insertVirtualEditorNode({ type: 'model', value: payload.modelName }, insertIndex);
    } else if (payload.source === 'panel-channel') {
      insertVirtualEditorNode({ type: 'channel', value: payload.providerName }, insertIndex);
    } else if (payload.source === 'chain-node' && payload.virtualName === '__virtual_editor__') {
      const targetIndex = insertIndex == null ? Math.max(0, virtualEditorChain.length - 1) : insertIndex;
      moveVirtualEditorNode(payload.fromIndex, targetIndex);
    }
  };

  const handleSaveVirtualEditor = async () => {
    // 修改原因：抽屉保存需要把当前名称作为 preferences.virtual_models 的 key，同时支持新建和重命名。
    // 修改方式：校验名称冲突后合并到 virtualModels，再复用 serializeVirtualModels 和 saveVirtualModels 提交后端。
    // 目的：保持后端交互和数据格式不变，只改变前端编辑入口。
    const name = virtualDraftName.trim();
    if (!name) {
      toastWarning('虚拟模型名为必填项');
      return;
    }
    if (editingVirtualName !== name && virtualModels[name]) {
      toastWarning('该虚拟模型已存在');
      return;
    }

    const nextVirtualModels = { ...virtualModels };
    if (editingVirtualName && editingVirtualName !== name) delete nextVirtualModels[editingVirtualName];
    nextVirtualModels[name] = { enabled: virtualDraftEnabled, chain: virtualEditorChain };

    try {
      const cleaned = serializeVirtualModels(nextVirtualModels);
      await saveVirtualModels(cleaned);
      setEditingVirtualName(name);
      setVirtualEditorChain(cleaned[name]?.chain || []);
      setVirtualModelsDirty(false);
      setIsVirtualModalOpen(false);
      toastSuccess('虚拟模型路由已保存');
    } catch (err: any) {
      toastError(`保存失败: ${err?.message || err}`);
    }
  };

  const handleToggleVirtualModelCard = async (name: string, enabled: boolean) => {
    // 修改原因：虚拟模型折叠卡片需要像普通渠道一样提供启用开关。
    // 修改方式：只修改目标虚拟模型的 enabled 字段，并立即提交 preferences.virtual_models。
    // 目的：列表中的快速开关不依赖打开抽屉，也不会影响真实渠道保存接口。
    const current = virtualModels[name];
    if (!current) return;
    const nextVirtualModels = serializeVirtualModels({ ...virtualModels, [name]: { ...current, enabled } });
    try {
      await saveVirtualModels(nextVirtualModels);
      toastSuccess(enabled ? '虚拟模型已启用' : '虚拟模型已禁用');
    } catch (err: any) {
      toastError(`操作失败: ${err?.message || err}`);
    }
  };

  const openTestDialog = (provider: any) => {
    setTestingProvider(provider);
    setTestDialogOpen(true);
  };

  const openKeyTestDialog = (initialIndex: number | null = null, subOverride?: { engine: string; base_url: string; models: string[]; title: string }) => {
    setKeyTestInitialIndex(initialIndex);
    setKeyTestOverride(subOverride ?? null);
    setKeyTestDialogOpen(true);
  };

  const buildProviderSnapshotForTest = (): any => {
    if (!formData) return null;

    const serializedKeys: (string | Record<string, string>)[] = formData.api_keys
      .map(k => {
        const raw = k.disabled ? `!${k.key.trim()}` : k.key.trim();
        if (!raw) return null;
        if (k.label) return { [raw]: k.label } as Record<string, string>;
        return raw;
      })
      .filter((x): x is string | Record<string, string> => x !== null);
    const finalApi = serializedKeys.length === 0 ? "" : serializedKeys.length === 1 ? serializedKeys[0] : serializedKeys;

    const finalModels: any[] = [...formData.models];
    formData.mappings.forEach(m => {
      if (m.from && m.to) finalModels.push({ [m.to]: m.from });
    });

    let headersObj: any = undefined;
    let overridesObj: any = undefined;
    try {
      const h = headerEntries.reduce((acc: Record<string, string>, e) => {
        if (e.key.trim()) acc[e.key.trim()] = e.value.trim();
        return acc;
      }, {});
      if (Object.keys(h).length > 0) headersObj = h;
    } catch { /* ignore */ }
    try {
      if (overridesJson.trim()) overridesObj = JSON.parse(overridesJson);
    } catch { /* ignore */ }
    let statusCodeOverridesObj: Record<string, number> | undefined = undefined;
    try {
      if (statusCodeOverridesJson.trim()) statusCodeOverridesObj = JSON.parse(statusCodeOverridesJson);
    } catch { /* ignore */ }

    // 修改原因：pool_sharing 只应在 model_prefix 存在时保存为 true。
    // 修改方式：构造预览/测试 payload 时同步归一化该字段。
    // 目的：避免测试渠道时使用与正式保存不同的路由池共享状态。
    const normalizedPoolSharing = formData.model_prefix.trim() ? !!formData.preferences.pool_sharing : false;
    const serializedPreferences = serializeChannelPreferences(formData.preferences);

    return {
      provider: formData.provider,
      remark: formData.remark || undefined,
      base_url: formData.base_url,
      // 修改原因：OAuth token endpoint 已独立为 token_url，测试快照必须与正式保存 payload 保持相同字段语义。
      // 修改方式：直接提交 formData.token_url，包括空字符串，避免 JSON.stringify 删除 undefined 字段。
      // 目的：确保测试弹窗、保存请求和回显链路都能表达用户填写或清空 token_url 的真实状态。
      token_url: formData.token_url,
      model_prefix: formData.model_prefix || undefined,
      api: finalApi,
      model: finalModels,
      engine: formData.engine || undefined,
      enabled: formData.enabled,
      groups: formData.groups,
      preferences: {
        ...serializedPreferences,
        pool_sharing: normalizedPoolSharing,
        headers: headersObj,
        post_body_parameter_overrides: overridesObj,
        status_code_overrides: statusCodeOverridesObj,
      },
      sub_channels: formData.sub_channels
        .filter(sub => sub.engine)
        .map(sub => ({
          engine: sub.engine,
          model: sub.models.length > 0 ? sub.models : undefined,
        })) || undefined,
    };
  };

  const getProviderModelNameListForUi = (): string[] => {
    if (!formData) return [];
    const prefix = (formData as any).model_prefix || '';
    const aliasMap = getAliasMap();
    const names: string[] = [];
    formData.models.forEach(upstream => {
      const alias = aliasMap.get(upstream);
      const name = alias || upstream;
      names.push(prefix && name !== '*' ? `${prefix}${name}` : name);
    });
    formData.mappings.forEach(m => {
      if (m.from) names.push(prefix ? `${prefix}${m.from}` : m.from);
    });
    return Array.from(new Set(names.map(s => String(s || '').trim()).filter(Boolean)));
  };

  const disableKeysInForm = (indices: number[]) => {
    if (!indices.length) return;
    const set = new Set(indices);
    setFormData(prev => {
      if (!prev) return prev;
      const next = prev.api_keys.map((k, idx) => set.has(idx) ? ({ ...k, disabled: true }) : k);
      return { ...prev, api_keys: next };
    });
  };

  const handleSave = async () => {
    if (!formData?.provider) {
      toastWarning("渠道名称为必填项");
      return;
    }

    const serializedKeys: (string | Record<string, string>)[] = formData.api_keys
      .map(k => {
        const raw = k.disabled ? `!${k.key.trim()}` : k.key.trim();
        if (!raw) return null;
        if (k.label) return { [raw]: k.label } as Record<string, string>;
        return raw;
      })
      .filter((x): x is string | Record<string, string> => x !== null);
    const finalApi = serializedKeys.length === 0 ? "" : serializedKeys.length === 1 ? serializedKeys[0] : serializedKeys;

    const finalModels: any[] = [...formData.models];
    formData.mappings.forEach(m => {
      if (m.from && m.to) finalModels.push({ [m.to]: m.from });
    });

    let overridesObj;
    try {
      if (overridesJson.trim()) overridesObj = JSON.parse(overridesJson);
    } catch {
      toastWarning("高级配置 JSON 格式错误");
      return;
    }

    let statusCodeOverridesObj: Record<string, number> | undefined;
    try {
      if (statusCodeOverridesJson.trim()) statusCodeOverridesObj = JSON.parse(statusCodeOverridesJson) as Record<string, number>;
    } catch {
      toastWarning("错误码映射 JSON 格式错误");
      return;
    }

    const headersObj: Record<string, string | string[]> | undefined = headerEntries.some(e => e.key.trim())
      ? headerEntries.reduce((acc, e) => {
          const k = e.key.trim(), v = e.value.trim();
          if (!k) return acc;
          if (acc[k]) {
            const prev = acc[k];
            acc[k] = Array.isArray(prev) ? [...prev, v] : [prev, v];
          } else {
            acc[k] = v;
          }
          return acc;
        }, {} as Record<string, string | string[]>)
      : undefined;

    // 校验并清理渠道级 model_price：去掉空前缀条目，检查价格值合法性
    let cleanedModelPrice = formData.preferences.model_price;
    if (cleanedModelPrice && typeof cleanedModelPrice === 'object') {
      const validEntries: [string, string][] = [];
      for (const [prefix, priceStr] of Object.entries(cleanedModelPrice)) {
        const trimmed = prefix.trim();
        if (!trimmed) continue;
        const parts = String(priceStr || '').split(',').map(s => s.trim());
        const inp = parts[0] || '0';
        const out = parts[1] || '0';
        if (isNaN(Number(inp)) || isNaN(Number(out))) {
          toastWarning(`模型价格「${trimmed}」的价格值无效，请填写数字`);
          return;
        }
        validEntries.push([trimmed, `${inp},${out}`]);
      }
      cleanedModelPrice = validEntries.length > 0 ? Object.fromEntries(validEntries) : undefined;
    }

    // 修改原因：pool_sharing 是依赖 model_prefix 的渠道级开关，清空前缀后应强制关闭。
    // 修改方式：保存前统一计算 normalizedPoolSharing，并覆盖 preferences 中的同名字段。
    // 目的：保证后端收到的配置不会出现无前缀但共享路由池开启的状态。
    const normalizedPoolSharing = formData.model_prefix.trim() ? !!formData.preferences.pool_sharing : false;
    const serializedPreferences = serializeChannelPreferences(formData.preferences);

    // 序列化子渠道
    const serializedSubChannels = formData.sub_channels
      .filter(sub => sub.engine) // 过滤掉空的
      .map(sub => {
        const subModels: any[] = [...sub.models];
        sub.mappings.forEach(m => {
          if (m.from && m.to) subModels.push({ [m.to]: m.from });
        });
        const subObj: any = {
          engine: sub.engine,
          model: subModels.length > 0 ? subModels : undefined,
        };
        if (sub.base_url) subObj.base_url = sub.base_url;
        if (sub.token_url) subObj.token_url = sub.token_url;
        if (sub.model_prefix) subObj.model_prefix = sub.model_prefix;
        if (sub.remark) subObj.remark = sub.remark;
        if (sub.enabled === false) subObj.enabled = false;
        const serializedSubPreferences = serializeChannelPreferences(sub.preferences || {});
        if (Object.keys(serializedSubPreferences).length > 0) subObj.preferences = serializedSubPreferences;
        return subObj;
      });

    const targetProvider: any = {
      provider: formData.provider,
      remark: formData.remark || undefined,
      base_url: formData.base_url,
      // 修改原因：PUT /v1/providers/{id} 会整体替换 provider，不提交 token_url 会把已保存的值删除。
      // 修改方式：正式保存 payload 始终携带 formData.token_url，空字符串也作为显式清空值发送。
      // 目的：保证保存后 api.yaml、单渠道 GET 和再次打开编辑面板都能回显同一份 token_url。
      token_url: formData.token_url,
      model_prefix: formData.model_prefix || undefined,
      api: finalApi,
      model: finalModels,
      engine: formData.engine || undefined,
      enabled: formData.enabled,
      groups: formData.groups,
      preferences: {
        ...serializedPreferences,
        pool_sharing: normalizedPoolSharing,
        model_price: cleanedModelPrice,
        headers: headersObj,
        post_body_parameter_overrides: overridesObj,
        status_code_overrides: statusCodeOverridesObj,
      },
      sub_channels: serializedSubChannels.length > 0 ? serializedSubChannels : undefined,
    };

    let newProviders: any[] | null = null;
    let providerSavePath = '/v1/providers';
    let providerSaveMethod: 'POST' | 'PUT' = 'POST';
    let subChannelParentProviderId = '';

    if (editingSubChannel) {
      // 修改原因：子渠道编辑保存只需要更新所属主渠道的 sub_channels 字段，不能继续全量提交 providers。
      // 修改方式：先记录父渠道 provider_id，后续把更新后的父渠道对象 PUT 到单个 provider 路径。
      // 目的：把子渠道编辑的写入范围限制在所属主渠道，避免覆盖其他渠道的并发变更。
      const { parentIdx, subIdx } = editingSubChannel;
      const parent = providers[parentIdx];
      subChannelParentProviderId = String(parent.provider || '').trim();
      if (!subChannelParentProviderId) {
        toastError('保存失败：主渠道名为空');
        return;
      }
      const parentPrefs = parent.preferences || {};

      // 计算子渠道 diff preferences（只保存和主渠道不同的部分）
      const subPrefs: Record<string, any> = {};
      const mergedPrefs = {
        ...serializedPreferences,
        pool_sharing: normalizedPoolSharing,
        model_price: cleanedModelPrice,
        headers: headersObj,
        post_body_parameter_overrides: overridesObj,
        status_code_overrides: statusCodeOverridesObj,
      };
      for (const [k, v] of Object.entries(mergedPrefs)) {
        if (JSON.stringify(v) !== JSON.stringify(parentPrefs[k])) {
          subPrefs[k] = v;
        }
      }

      const subObj: any = {
        engine: formData.engine,
        model: finalModels.length > 0 ? finalModels : undefined,
        enabled: formData.enabled,
      };
      if (formData.base_url && formData.base_url !== (parent.base_url || '')) subObj.base_url = formData.base_url;
      if (formData.token_url && formData.token_url !== (parent.token_url || '')) subObj.token_url = formData.token_url;
      if (formData.model_prefix && formData.model_prefix !== (parent.model_prefix || '')) subObj.model_prefix = formData.model_prefix;
      if (formData.remark) subObj.remark = formData.remark;
      if (Object.keys(subPrefs).length > 0) subObj.preferences = subPrefs;

      const subs = [...(parent.sub_channels || [])];
      subs[subIdx] = subObj;
      newProviders = [...providers];
      newProviders[parentIdx] = { ...parent, sub_channels: subs };
    } else if (originalIndex !== null) {
      // 修改原因：编辑主渠道时路径必须使用打开弹窗时对应的原 provider_id，才能支持重命名渠道。
      // 修改方式：从 originalIndex 读取当前列表中的原渠道名作为 PUT 路径，body 仍保存 targetProvider 的完整对象。
      // 目的：只替换旧渠道对象，不把整个 providers 数组发回后端。
      const originalProviderId = String(providers[originalIndex]?.provider || '').trim();
      if (!originalProviderId) {
        toastError('保存失败：找不到原渠道名');
        return;
      }
      providerSavePath = buildProviderApiPath(originalProviderId);
      providerSaveMethod = 'PUT';
    }

    try {
      // 修改原因：主渠道和子渠道都已有可限定写入范围的单渠道 API，继续对子渠道全量保存会覆盖并发修改。
      // 修改方式：子渠道编辑保存 PUT 更新后的父渠道对象；主渠道新增走 POST /v1/providers，编辑走 PUT /v1/providers/{provider_id}。
      // 目的：所有渠道保存成功后统一 refreshProviders，以后端最新配置作为页面状态来源。
      const res = editingSubChannel
        ? await apiFetch(buildProviderApiPath(subChannelParentProviderId), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          body: JSON.stringify(newProviders![editingSubChannel.parentIdx]),
        })
        : await apiFetch(providerSavePath, {
          method: providerSaveMethod,
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          body: JSON.stringify(targetProvider),
        });

      if (res.ok) {
        if (!editingSubChannel && providerSaveMethod === 'POST' && formData._copiedFrom && isOAuthEngine) {
          // 修改原因：复制 OAuth 渠道保存后，api.yaml 已有新渠道账号 key，但 OAuthManager 中还没有目标 provider 的 token state。
          // 修改方式：创建 provider 成功后调用 /v1/oauth/copy-provider，把来源 provider 下缺失的账号 state 深拷贝到新 provider。
          // 目的：让复制出的 OAuth 渠道不需要重新登录或重新导入 token 即可正常请求。
          const copyStateRes = await apiFetch('/v1/oauth/copy-provider', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
            body: JSON.stringify({
              source_provider: formData._copiedFrom,
              target_provider: formData.provider,
            }),
          });
          if (!copyStateRes.ok) {
            const err = await copyStateRes.json().catch(() => ({}));
            // 修改原因：provider 创建已经成功，若此处直接 return，用户会停留在无法再次 POST 保存的旧弹窗状态。
            // 修改方式：仅提示 OAuth state 复制失败，随后继续刷新后端 provider 列表并关闭弹窗。
            // 目的：让界面反映已保存的新渠道，同时明确告知 token state 复制需要排查。
            toastError(fmtErr(err, copyStateRes.status), 'OAuth state 复制失败');
          }
        }
        // PUT 编辑走单渠道刷新；POST 新增走全量刷新（需要获取后端分配的完整对象）
        const savedProviderId = editingSubChannel
          ? subChannelParentProviderId
          : (providerSaveMethod === 'PUT' ? String(providers[originalIndex!]?.provider || '').trim() : '');
        if (savedProviderId) {
          await refreshSingleProvider(savedProviderId);
        } else {
          await refreshProviders();
        }
        setIsModalOpen(false);
        setEditingSubChannel(null);
      } else {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), "保存失败");
      }
    } catch (err: any) {
      toastError(err?.message || "网络错误");
    }
  };

  // Mobile Card Component
  const ProviderCard = ({ p, idx }: { p: any; idx: number }) => {
    const isEnabled = p.enabled !== false;
    const groups = Array.isArray(p.groups) ? p.groups : p.group ? [p.group] : ['default'];
    const plugins = p.preferences?.enabled_plugins || [];
    const weight = p.preferences?.weight ?? p.weight ?? 0;

    return (
      <div className={`bg-card border border-border rounded-xl p-4 ${!isEnabled && 'opacity-60'}`}>
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-3">
            <ProviderLogo name={p.provider} engine={p.engine} baseUrl={p.base_url} />
            <div>
              <div className={`font-medium ${isEnabled ? 'text-foreground' : 'text-muted-foreground'}`}>{p.provider}</div>
              <div className="text-xs text-muted-foreground font-mono">{p.engine || 'openai'}</div>
              {p.remark && (
                <div className="mt-1 text-xs text-muted-foreground break-words whitespace-pre-wrap max-w-full">
                  {p.remark}
                </div>
              )}
            </div>
          </div>
          <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${isEnabled ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-500' : 'bg-red-500/10 text-red-600 dark:text-red-500'}`}>
            {isEnabled ? <CheckCircle2 className="w-3 h-3" /> : <X className="w-3 h-3" />}
            {isEnabled ? '启用' : '禁用'}
          </span>
        </div>

        <div className="flex flex-wrap gap-1 mb-3">
          {groups.map((g: string, i: number) => (
            <span key={i} className="flex items-center gap-1 bg-muted text-foreground px-2 py-0.5 rounded text-xs"><Folder className="w-3 h-3" />{g}</span>
          ))}
          {plugins.length > 0 && (
            <span className="bg-primary/10 text-primary px-2 py-0.5 rounded text-xs flex items-center gap-1"><Puzzle className="w-3 h-3" /> {plugins.length}</span>
          )}
        </div>

        <div className="flex items-center justify-between pt-3 border-t border-border gap-2">
          <div className="flex items-center gap-1.5 flex-shrink-0">
            <span className="text-xs text-muted-foreground">权重:</span>
            <input
              type="number"
              value={weight}
              onChange={e => handleUpdateWeight(idx, parseInt(e.target.value) || 0)}
              className="w-12 bg-muted border border-border rounded px-1.5 py-1 text-center font-mono text-xs text-foreground"
            />
          </div>
          <div className="flex items-center gap-0.5 flex-shrink-0">
            <button onClick={() => { setAnalyticsProvider(getProviderAnalyticsName(p)); setAnalyticsOpen(true); }} className="p-1.5 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-500/10 rounded-md transition-colors" title="分析">
              <BarChart3 className="w-4 h-4" />
            </button>
            <button onClick={() => openTestDialog(p)} className="p-1.5 text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 rounded-md transition-colors" title="测试">
              <Play className="w-4 h-4" />
            </button>
            <button onClick={() => handleToggleProvider(idx)} className={`p-1.5 rounded-md transition-colors ${isEnabled ? 'text-emerald-600 dark:text-emerald-500 hover:bg-emerald-500/10' : 'text-muted-foreground hover:bg-muted'}`} title={isEnabled ? '禁用' : '启用'}>
              <Power className="w-4 h-4" />
            </button>
            <button onClick={() => handleCopyProvider(p)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors" title="复制">
              <Files className="w-4 h-4" />
            </button>
            <button onClick={() => openModal(p, idx)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors" title="编辑">
              <Edit className="w-4 h-4" />
            </button>
            <button onClick={() => handleDeleteProvider(idx)} className="p-1.5 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md transition-colors" title="删除">
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* 子渠道列表 */}
        {(p.sub_channels || []).length > 0 && (
          <div className="mt-3 pt-3 border-t border-border space-y-2">
            <div className="text-[10px] text-muted-foreground font-medium uppercase tracking-wider">子渠道</div>
            {(p.sub_channels || []).map((sub: any, subIdx: number) => {
              const subEnabled = sub.enabled !== false;
              const subModels = Array.isArray(sub.model) ? sub.model : Array.isArray(sub.models) ? sub.models : [];
              return (
                <div key={subIdx} className={`flex items-center justify-between bg-muted/30 rounded-lg px-3 py-2 ${!subEnabled && 'opacity-50'}`}>
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-muted-foreground text-xs">└</span>
                    <div className="min-w-0">
                      <div className="text-xs font-medium text-foreground truncate">{sub.remark || sub.engine || '?'}</div>
                      <div className="text-[10px] text-muted-foreground">{subModels.length} 模型</div>
                    </div>
                    {!subEnabled && <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-500 flex-shrink-0">禁用</span>}
                  </div>
                  <div className="flex items-center gap-0.5 flex-shrink-0">
                    <button onClick={() => { const sp = buildSubChannelProvider(idx, subIdx); if (sp) openTestDialog(sp); }} className="p-1 text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 rounded-md transition-colors" title="测试子渠道">
                      <Play className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => handleToggleSubChannel(idx, subIdx)} className={`p-1 rounded-md transition-colors ${subEnabled ? 'text-emerald-600 dark:text-emerald-500 hover:bg-emerald-500/10' : 'text-muted-foreground hover:bg-muted'}`} title={subEnabled ? '禁用' : '启用'}>
                      <Power className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => openSubChannelEdit(idx, subIdx)} className="p-1 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors" title="编辑子渠道">
                      <Edit className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => handleDeleteSubChannel(idx, subIdx)} className="p-1 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md transition-colors" title="删除子渠道">
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  // ── 从 provider 对象中提取所有模型名（别名 + 上游） ──
  const getProviderModelNames = (p: any): string[] => {
    const rawModels = Array.isArray(p.model) ? p.model : Array.isArray(p.models) ? p.models : [];
    const prefix = (p.model_prefix || '').trim();
    const names: string[] = [];
    rawModels.forEach((m: any) => {
      if (typeof m === 'string') {
        names.push(m);
        if (prefix) names.push(`${prefix}${m}`);
      }
      else if (typeof m === 'object' && m !== null) {
        Object.entries(m).forEach(([upstream, alias]) => {
          names.push(String(alias));
          names.push(upstream);
          if (prefix) {
            names.push(`${prefix}${String(alias)}`);
            names.push(`${prefix}${upstream}`);
          }
        });
      }
    });
    // 子渠道模型也纳入搜索
    (p.sub_channels || []).forEach((sub: any) => {
      const subModels = Array.isArray(sub.model) ? sub.model : [];
      subModels.forEach((m: any) => {
        if (typeof m === 'string') {
          names.push(m);
        } else if (typeof m === 'object' && m !== null) {
          Object.entries(m).forEach(([upstream, alias]) => {
            names.push(String(alias));
            names.push(upstream);
          });
        }
      });
    });
    return names;
  };

  // ── 虚拟模型列表和渠道下拉选项 ──
  const virtualProviderEntries = useMemo(() => {
    // 修改原因：虚拟模型现在由独立手风琴置顶渲染，不能再依赖主渠道列表生成伪行。
    // 修改方式：单独调用 buildVirtualProviderEntries，把 preferences.virtual_models 转成稳定排序的 provider-like entries。
    // 目的：桌面表格、移动端卡片和测试弹窗共用同一份虚拟模型数据源。
    return buildVirtualProviderEntries(virtualModels);
  }, [virtualModels]);

  const virtualRoutingProviderItems = useMemo(() => {
    // 修改原因：/v1/api_config 返回的是持久化配置，子渠道不会作为独立 provider 出现在 providers state 中。
    // 修改方式：在前端按后端展开规则生成“主渠道 + 子渠道”的虚拟路由 provider 列表。
    // 目的：虚拟模型的 channel 节点、模型匹配统计和左栏数据源都能引用子渠道。
    return buildVirtualRoutingProviderItems(providers);
  }, [providers]);

  const virtualProviderPanelItems = useMemo(() => {
    // 修改原因：移动端渠道面板和渠道节点下拉都需要更直观的排序，子渠道不能被全局排序拆离主渠道。
    // 修改方式：使用纯 helper 按主渠道 weight 降序分组，再让子渠道按 weight 降序紧跟所属主渠道。
    // 目的：左侧面板、移动端面板和下拉列表共享同一份有层级的渠道顺序。
    return buildVirtualProviderPanelItems(providers);
  }, [providers]);

  const providerNames = useMemo(() => {
    // 修改原因：渠道节点下拉框需要沿用渠道面板的直观排序，而不是按名称字母序打散子渠道。
    // 修改方式：从已排序的 virtualProviderPanelItems 提取 provider 字段，并保持当前链条中的旧值兜底显示。
    // 目的：手动选择 channel 节点时也能看到“主渠道后跟子渠道”的顺序。
    return Array.from(new Set(virtualProviderPanelItems.map(p => String(p.provider || '').trim()).filter(Boolean)));
  }, [virtualProviderPanelItems]);

  const providerListItems = useMemo(() => {
    // 修改原因：虚拟模型已由手风琴单独收纳，主列表只应该参与真实渠道排序和真实渠道操作。
    // 修改方式：调用 helper 只生成真实渠道条目，并保留原始 providers 下标。
    // 目的：避免虚拟模型进入不活跃分段、真实渠道编辑和删除逻辑。
    return buildProviderListItems(providers);
  }, [providers]);

  // ── 可用引擎列表和分组列表（从当前渠道数据中提取） ──
  const availableEngines = useMemo(() => {
    // 修改原因：虚拟模型从主列表移到手风琴后，筛选器仍然需要能只看虚拟路由。
    // 修改方式：存在虚拟 entries 时加入“虚拟路由”，真实渠道仍读取原 engine。
    // 目的：用户可以通过引擎筛选快速只看虚拟路由或真实渠道。
    const set = new Set<string>();
    if (virtualProviderEntries.length > 0) set.add('虚拟路由');
    providers.forEach(p => set.add(p.engine || 'openai'));
    return Array.from(set).sort();
  }, [providers, virtualProviderEntries]);

  const availableGroups = useMemo(() => {
    const set = new Set<string>();
    providers.forEach(p => {
      const groups = Array.isArray(p.groups) ? p.groups : p.group ? [p.group] : ['default'];
      groups.forEach((g: string) => set.add(g));
    });
    return Array.from(set).sort();
  }, [providers]);

  // ── 工具函数：拼接主渠道+子渠道名（逗号分隔，用于统计聚合） ──
  const getProviderAnalyticsName = (p: any): string => {
    const names = [p.provider];
    const subs = p.sub_channels || [];
    subs.forEach((sub: any, i: number) => {
      const subEngine = sub.engine || '';
      if (subEngine) names.push(`${p.provider}:${subEngine}`);
    });
    return names.join(',');
  };

  const filteredVirtualProviderEntries = useMemo(() => {
    // 修改原因：虚拟模型已经脱离主列表，但搜索、状态和引擎筛选仍应作用到手风琴内容。
    // 修改方式：对虚拟 entries 单独执行筛选；分组筛选只属于真实渠道，因此有分组条件时隐藏虚拟模型。
    // 目的：筛选统计和置顶手风琴保持一致，不把虚拟模型重新混入真实渠道 segments。
    const kw = filterKeyword.trim().toLowerCase();
    return virtualProviderEntries.filter(p => {
      const enabled = p.enabled !== false;
      if (filterStatus === 'enabled' && !enabled) return false;
      if (filterStatus === 'disabled' && enabled) return false;
      if (filterEngine && filterEngine !== '虚拟路由') return false;
      if (filterGroup) return false;
      if (kw) {
        const nameMatch = (p.provider || '').toLowerCase().includes(kw);
        const chainMatch = summarizeVirtualChain(p.chain, p.provider, Number.MAX_SAFE_INTEGER).toLowerCase().includes(kw);
        if (!nameMatch && !chainMatch) return false;
      }
      return true;
    });
  }, [virtualProviderEntries, filterKeyword, filterEngine, filterGroup, filterStatus]);

  // ── 筛选后的真实渠道列表（真实行保留原始 index 用于操作） ──
  const filteredProviders = useMemo(() => {
    // 修改原因：虚拟模型从主列表移出后，真实渠道筛选不再需要理解 _isVirtual 分支。
    // 修改方式：只按真实渠道的启用状态、引擎、分组、备注和模型名筛选 providerListItems。
    // 目的：真实渠道 segments 保持简单，避免虚拟模型误传给真实渠道操作函数。
    const kw = filterKeyword.trim().toLowerCase();
    return providerListItems.filter(({ p }) => {
      const enabled = p.enabled !== false;
      if (filterStatus === 'enabled' && !enabled) return false;
      if (filterStatus === 'disabled' && enabled) return false;

      const engineName = p.engine || 'openai';
      if (filterEngine && engineName !== filterEngine) return false;

      if (filterGroup) {
        const groups = Array.isArray(p.groups) ? p.groups : p.group ? [p.group] : ['default'];
        if (!groups.includes(filterGroup)) return false;
      }

      if (kw) {
        const nameMatch = (p.provider || '').toLowerCase().includes(kw);
        const remarkMatch = (p.remark || '').toLowerCase().includes(kw);
        const modelNames = getProviderModelNames(p);
        const modelMatch = modelNames.some(n => n.toLowerCase().includes(kw));
        if (!nameMatch && !remarkMatch && !modelMatch) return false;
      }
      return true;
    });
  }, [providerListItems, filterKeyword, filterEngine, filterGroup, filterStatus]);

  // 关键词是否命中了某个 provider 的模型（用于高亮提示）
  const getMatchedModels = (p: any): string[] => {
    const kw = filterKeyword.trim().toLowerCase();
    if (!kw) return [];
    return getProviderModelNames(p).filter(n => n.toLowerCase().includes(kw));
  };

  const hasActiveFilters = filterKeyword || filterEngine || filterGroup || filterStatus;
  // 修改原因：虚拟模型现在不在 providerListItems 中，但空状态和筛选统计仍要把手风琴条目算进去。
  // 修改方式：分别统计真实渠道条目和虚拟模型 entries，再在页面判断中使用合计值。
  // 目的：当页面只有虚拟模型或筛选只命中虚拟模型时，列表不会错误显示为空。
  const totalListItemCount = providerListItems.length + virtualProviderEntries.length;
  const visibleListItemCount = filteredProviders.length + filteredVirtualProviderEntries.length;

  // 按活跃度标记：30天内有请求的为活跃
  const INACTIVE_DAYS = 30;
  const isProviderInactive = (provider: any): boolean => {
    const hasActivityData = Object.keys(providerActivity).length > 0;
    if (!hasActivityData) return false;
    const lastSeen = providerActivity[provider.provider];
    if (!lastSeen) return false;
    return (Date.now() / 1000 - Number(lastSeen)) > INACTIVE_DAYS * 86400;
  };

  // 折叠状态
  const [expandedInactiveGroups, setExpandedInactiveGroups] = useState<Set<number>>(new Set());
  const toggleInactiveGroup = (groupKey: number) => {
    setExpandedInactiveGroups(prev => {
      const next = new Set(prev);
      if (next.has(groupKey)) next.delete(groupKey); else next.add(groupKey);
      return next;
    });
  };

  // 分段：连续不活跃的合并成一个可折叠 group
  type Segment = { type: 'active'; item: typeof filteredProviders[0] } | { type: 'inactive'; items: typeof filteredProviders; startIndex: number };
  const segments: Segment[] = useMemo(() => {
    const segs: Segment[] = [];
    let buf: typeof filteredProviders = [];
    let bufStart = 0;
    const flush = () => {
      if (buf.length > 0) {
        segs.push({ type: 'inactive', items: [...buf], startIndex: bufStart });
        buf = [];
      }
    };
    filteredProviders.forEach((item, i) => {
      if (isProviderInactive(item.p)) {
        if (buf.length === 0) bufStart = i;
        buf.push(item);
      } else {
        flush();
        segs.push({ type: 'active', item });
      }
    });
    flush();
    return segs;
  }, [filteredProviders, providerActivity]);

  const renderVirtualProviderPanelCollapsedRail = () => {
    // 修改原因：桌面端仍需要保留原来的窄侧栏，方便在不展开面板时快速添加渠道节点。
    // 修改方式：把原先 aside 折叠分支抽成渲染函数，供桌面端专用容器调用。
    // 目的：移动端可以复用完整列表渲染，同时不破坏桌面端既有交互。
    return (
      <div className="space-y-2">
        <div className="text-[10px] text-muted-foreground text-center">{virtualProviderPanelItems.length}</div>
        {virtualProviderPanelItems.length === 0 ? (
          <div className="text-[10px] text-muted-foreground text-center border border-dashed border-border rounded-lg px-1 py-3">无</div>
        ) : virtualProviderPanelItems.map(provider => {
          const providerName = String(provider?.provider || '未命名渠道');
          const isSubChannel = provider?._is_sub_channel === true;
          return (
            <div
              key={providerName}
              draggable
              onDragStart={e => handlePanelChannelDragStart(e, providerName)}
              className="relative"
            >
              <button
                type="button"
                onClick={() => insertVirtualEditorNode({ type: 'channel', value: providerName })}
                className={`w-full h-11 rounded-lg border bg-background hover:bg-purple-500/10 flex items-center justify-center transition-colors ${isSubChannel ? 'border-cyan-500/30' : 'border-border'}`}
                title={`添加渠道节点：${providerName}`}
              >
                <ProviderLogo name={providerName} engine={provider?.engine} baseUrl={provider?.base_url || formData?.base_url} />
              </button>
              {isSubChannel && <span className="absolute -right-0.5 -top-0.5 w-2 h-2 rounded-full bg-cyan-500" />}
            </div>
          );
        })}
      </div>
    );
  };

  const renderVirtualProviderPanelList = () => {
    // 修改原因：移动端展开态需要展示与桌面展开态一致的完整渠道列表，避免再依赖底部下拉理解顺序。
    // 修改方式：把原先 aside 展开分支抽成渲染函数，由移动端展开区域和桌面展开侧栏共同复用。
    // 目的：保证渠道内容、拖拽入口、快速添加入口和模型展开行为在不同屏幕宽度下一致。
    return (
      <div className="space-y-2">
        {/* 修改原因：子渠道在后端是独立 provider，虚拟模型链条也需要把它当作可拖拽渠道。
            修改方式：左栏把子渠道渲染成带“子渠道”标记和缩进的紧凑卡片，模型列表继续默认折叠。
            目的：用户能直接拖拽或点击子渠道卡片，同时减少左栏常驻高度和宽度。 */}
        {virtualProviderPanelItems.length === 0 ? (
          <div className="text-sm text-muted-foreground text-center border border-dashed border-border rounded-lg p-4">暂无可用渠道。</div>
        ) : virtualProviderPanelItems.map(provider => {
          const providerName = String(provider?.provider || '未命名渠道');
          const isSubChannel = provider?._is_sub_channel === true;
          const parentProviderName = String(provider?._parent_provider || '').trim();
          const isExpanded = expandedVirtualProviders.has(providerName);
          const weight = getProviderWeight(provider);
          const modelOptions = getProviderModelOptions(provider);
          return (
            <div
              key={providerName}
              draggable
              onDragStart={e => handlePanelChannelDragStart(e, providerName)}
              className={`border rounded-lg bg-background transition-colors ${isSubChannel ? 'ml-2 border-cyan-500/30' : 'border-border'}`}
            >
              <div className="flex items-center gap-2 px-2.5 py-2">
                <GripVertical className="w-3.5 h-3.5 text-muted-foreground cursor-grab flex-shrink-0" />
                <button
                  type="button"
                  onClick={() => toggleVirtualProviderExpanded(providerName)}
                  className="flex-1 min-w-0 flex items-center gap-2 text-left"
                >
                  <ProviderLogo name={providerName} engine={provider?.engine} baseUrl={provider?.base_url || formData?.base_url} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1 min-w-0">
                      <span className="text-xs font-medium text-foreground truncate">{providerName}</span>
                      {isSubChannel && <span className="text-[10px] px-1 rounded bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 flex-shrink-0">子</span>}
                      <span className="text-[10px] text-muted-foreground font-mono flex-shrink-0 ml-auto">{modelOptions.length}</span>
                    </div>
                    {provider?.remark && <div className="text-[10px] text-muted-foreground truncate mt-0.5">{provider.remark}</div>}
                  </div>
                  <span className="text-[10px] text-muted-foreground flex-shrink-0">{isExpanded ? '收起' : '模型'}</span>
                </button>
                <button
                  type="button"
                  onClick={e => { e.stopPropagation(); insertVirtualEditorNode({ type: 'channel', value: providerName }); }}
                  className="p-1 text-purple-600 dark:text-purple-400 hover:bg-purple-500/10 rounded-md transition-colors flex-shrink-0"
                  title="添加为渠道节点"
                >
                  <Plus className="w-3.5 h-3.5" />
                </button>
              </div>

              {isExpanded && (
                <div className="border-t border-border px-2.5 py-2 space-y-1.5">
                  {modelOptions.length === 0 ? (
                    <div className="text-xs text-muted-foreground italic">该渠道未配置模型。</div>
                  ) : modelOptions.map(option => (
                    <div
                      key={`${providerName}-${option.displayName}-${option.upstreamName}`}
                      draggable
                      onDragStart={e => handlePanelModelDragStart(e, option.displayName)}
                      className="group flex items-center gap-2 rounded-md border border-border bg-muted/30 px-2 py-1.5 hover:bg-muted/60 transition-colors cursor-grab"
                    >
                      <GripVertical className="w-3 h-3 text-muted-foreground flex-shrink-0" />
                      <div className="min-w-0 flex-1 font-mono text-[11px]">
                        <div className="truncate text-foreground" title={option.displayName}>{option.displayName}</div>
                        {option.hasMapping && <div className="truncate text-[10px] text-muted-foreground" title={option.upstreamName}>→ {option.upstreamName}</div>}
                      </div>
                      <button
                        type="button"
                        onClick={e => { e.stopPropagation(); insertVirtualEditorNode({ type: 'model', value: option.displayName }); }}
                        className="p-1 text-purple-600 dark:text-purple-400 hover:bg-purple-500/10 rounded-md opacity-80 group-hover:opacity-100 transition-colors"
                        title="添加为模型节点"
                      >
                        <Plus className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    );
  };

  const getFullVirtualChainSummary = (p: any): string => {
    // 修改原因：渠道列表中的虚拟模型摘要不再使用 tooltip，必须直接显示完整链条文本。
    // 修改方式：调用 summarizeVirtualChain 时传入最大安全整数，禁用默认的 6 段截断。
    // 目的：桌面表格和移动端子卡片都能通过换行完整展示 chain 摘要。
    return summarizeVirtualChain(p.chain, p.provider, Number.MAX_SAFE_INTEGER);
  };

  const openVirtualRouteTestDialog = (entries: any[]) => {
    // 修改原因：虚拟路由手风琴标题行要测试全部虚拟模型，子行要只测试当前虚拟模型。
    // 修改方式：用 buildVirtualRouteTestProvider 生成 ChannelTestDialog 可识别的临时 provider，再复用 openTestDialog。
    // 目的：测试请求直接使用虚拟模型名作为 model，并携带 _virtual_route_test 标记触发后端虚拟链路。
    const testProvider = buildVirtualRouteTestProvider(entries);
    if (!testProvider) {
      toastWarning('暂无可测试的虚拟模型');
      return;
    }
    openTestDialog(testProvider);
  };

  const renderDesktopVirtualRoutesAccordionRows = () => {
    // 修改原因：桌面端虚拟模型需要置顶收纳在表格内，而不是作为普通 provider 行参与 segments。
    // 修改方式：先渲染一个跨 7 列的标题行，展开后再渲染 7 列子行复用渠道表格列宽。
    // 目的：保持普通渠道表格结构不变，同时让虚拟模型操作按钮与普通渠道右侧按钮对齐。
    if (filteredVirtualProviderEntries.length === 0) return null;
    return (
      <>
        <tr className="bg-purple-500/5">
          <td colSpan={7} className="p-0">
            <div className="flex items-center gap-2 px-4 py-3 border-l-4 border-l-purple-500/70">
              <button
                type="button"
                onClick={() => setIsVirtualRoutesAccordionOpen(prev => !prev)}
                aria-expanded={isVirtualRoutesAccordionOpen}
                className="flex-1 min-w-0 flex items-center gap-2 text-left text-sm font-medium text-purple-700 dark:text-purple-300"
              >
                <span className="truncate">🔗 虚拟路由 ({filteredVirtualProviderEntries.length})</span>
              </button>
              <button
                type="button"
                onClick={e => { e.stopPropagation(); openVirtualRouteTestDialog(filteredVirtualProviderEntries); }}
                className="p-1.5 text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 rounded-md transition-colors flex-shrink-0"
                title="测试全部虚拟模型"
              >
                <Play className="w-4 h-4" />
              </button>
              <button
                type="button"
                onClick={() => setIsVirtualRoutesAccordionOpen(prev => !prev)}
                className="px-2 py-1 text-xs text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors flex-shrink-0"
                title={isVirtualRoutesAccordionOpen ? '收起虚拟路由' : '展开虚拟路由'}
              >
                {isVirtualRoutesAccordionOpen ? '▲' : '▼'}
              </button>
            </div>
          </td>
        </tr>
        {isVirtualRoutesAccordionOpen && filteredVirtualProviderEntries.map(p => {
          const isEnabled = p.enabled !== false;
          const chainSummary = getFullVirtualChainSummary(p);
          return (
            <tr key={`virtual-${p.provider}`} className={`transition-colors border-l-4 border-l-purple-500/40 bg-purple-500/[0.03] hover:bg-purple-500/10 ${!isEnabled ? 'opacity-60' : ''}`}>
              <td className="px-4 py-3 align-top">
                <div className="font-medium text-purple-700 dark:text-purple-300 break-words">{p.provider}</div>
              </td>
              <td className="px-4 py-3 align-top">
                <span className="inline-flex items-center gap-1 w-fit bg-purple-500/10 text-purple-700 dark:text-purple-300 px-1.5 py-0.5 rounded text-xs">
                  <Link2 className="w-3 h-3" /> 虚拟路由
                </span>
              </td>
              <td className="px-4 py-3 text-center align-top">
                <span className="text-xs font-mono text-muted-foreground">{Array.isArray(p.chain) ? p.chain.length : 0} 节点</span>
              </td>
              <td className="px-4 py-3 align-top">
                <span className="block text-xs text-foreground font-mono truncate max-w-[280px] cursor-help" title={chainSummary}>{chainSummary}</span>
              </td>
              <td className="px-4 py-3 text-center align-top"><span className="text-muted-foreground/50">—</span></td>
              <td className="px-4 py-3 text-center align-top">
                <span className="font-mono text-sm text-purple-700 dark:text-purple-300">∞</span>
              </td>
              <td className="px-4 py-3 text-right align-top">
                <div className="flex items-center justify-end gap-1">
                  {/* 修改原因：普通渠道行的按钮是“分析/测试/开关/复制/编辑/删除”，虚拟行没有分析和复制。
                      修改方式：用不可见占位对齐缺失的分析和复制按钮，但可见按钮仍保持“测试/开关/编辑/删除”的顺序。
                      目的：让虚拟模型测试按钮与普通渠道测试按钮大致处于同一横向位置。 */}
                  <span className="w-7 flex-shrink-0" aria-hidden="true" />
                  <button onClick={() => openVirtualRouteTestDialog([p])} className="p-1.5 text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 rounded-md transition-colors" title="测试虚拟模型">
                    <Play className="w-4 h-4" />
                  </button>
                  <button onClick={() => handleToggleVirtualModelCard(p.provider, !isEnabled)} className={`p-1.5 rounded-md transition-colors ${isEnabled ? 'text-emerald-600 dark:text-emerald-500 hover:bg-emerald-500/10' : 'text-muted-foreground hover:bg-muted'}`} title={isEnabled ? '禁用虚拟模型' : '启用虚拟模型'}>
                    <Power className="w-4 h-4" />
                  </button>
                  <span className="w-7 flex-shrink-0" aria-hidden="true" />
                  <button onClick={() => openVirtualModelModal(p.provider)} className="p-1.5 text-muted-foreground hover:text-purple-600 dark:hover:text-purple-300 hover:bg-purple-500/10 rounded-md transition-colors" title="编辑虚拟模型">
                    <Edit className="w-4 h-4" />
                  </button>
                  <button onClick={() => handleDeleteVirtualModel(p.provider)} className="p-1.5 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md transition-colors" title="删除虚拟模型">
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </td>
            </tr>
          );
        })}
      </>
    );
  };

  const renderMobileVirtualRoutesAccordion = () => {
    // 修改原因：移动端虚拟模型也需要收纳，避免多个独立紫色卡片挤占渠道列表顶部空间。
    // 修改方式：折叠态显示紫色边框标题卡片，展开后为每个虚拟模型渲染紧凑子卡片和操作按钮。
    // 目的：手机上保留名称、完整 chain 摘要和测试/开关/编辑/删除入口，同时减少默认高度。
    if (filteredVirtualProviderEntries.length === 0) return null;
    return (
      <div className="border border-purple-500/40 bg-purple-500/5 rounded-xl overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-3">
          <button
            type="button"
            onClick={() => setIsVirtualRoutesAccordionOpen(prev => !prev)}
            aria-expanded={isVirtualRoutesAccordionOpen}
            className="flex-1 min-w-0 text-left text-sm font-medium text-purple-700 dark:text-purple-300"
          >
            <span className="truncate block">🔗 虚拟路由 ({filteredVirtualProviderEntries.length})</span>
          </button>
          <button
            type="button"
            onClick={e => { e.stopPropagation(); openVirtualRouteTestDialog(filteredVirtualProviderEntries); }}
            className="p-1.5 text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 rounded-md transition-colors flex-shrink-0"
            title="测试全部虚拟模型"
          >
            <Play className="w-4 h-4" />
          </button>
          <button
            type="button"
            onClick={() => setIsVirtualRoutesAccordionOpen(prev => !prev)}
            className="px-2 py-1 text-xs text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors flex-shrink-0"
            title={isVirtualRoutesAccordionOpen ? '收起虚拟路由' : '展开虚拟路由'}
          >
            {isVirtualRoutesAccordionOpen ? '▲' : '▼'}
          </button>
        </div>

        {isVirtualRoutesAccordionOpen && (
          <div className="border-t border-purple-500/20 p-2 space-y-2">
            {filteredVirtualProviderEntries.map(p => {
              const isEnabled = p.enabled !== false;
              const chainSummary = getFullVirtualChainSummary(p);
              return (
                <div key={`mobile-virtual-${p.provider}`} className={`rounded-lg border border-purple-500/25 bg-background/70 p-3 ${!isEnabled ? 'opacity-60' : ''}`}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="font-medium text-sm text-purple-700 dark:text-purple-300 break-words">{p.provider}</div>
                      <div className="mt-1 text-xs text-foreground font-mono truncate" title={chainSummary}>{chainSummary}</div>
                    </div>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-700 dark:text-purple-300 flex-shrink-0">虚拟路由</span>
                  </div>
                  <div className="mt-3 pt-2 border-t border-purple-500/15 flex items-center justify-end gap-0.5">
                    <button onClick={() => openVirtualRouteTestDialog([p])} className="p-1.5 text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 rounded-md transition-colors" title="测试虚拟模型">
                      <Play className="w-4 h-4" />
                    </button>
                    <button onClick={() => handleToggleVirtualModelCard(p.provider, !isEnabled)} className={`p-1.5 rounded-md transition-colors ${isEnabled ? 'text-emerald-600 dark:text-emerald-500 hover:bg-emerald-500/10' : 'text-muted-foreground hover:bg-muted'}`} title={isEnabled ? '禁用虚拟模型' : '启用虚拟模型'}>
                      <Power className="w-4 h-4" />
                    </button>
                    <button onClick={() => openVirtualModelModal(p.provider)} className="p-1.5 text-muted-foreground hover:text-purple-600 dark:hover:text-purple-300 hover:bg-purple-500/10 rounded-md transition-colors" title="编辑虚拟模型">
                      <Edit className="w-4 h-4" />
                    </button>
                    <button onClick={() => handleDeleteVirtualModel(p.provider)} className="p-1.5 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md transition-colors" title="删除虚拟模型">
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };


  const renderFullKeyRow = (keyObj: ApiKeyObj, idx: number, options: { showDecorationsWhileFocused?: boolean } = {}) => {
    // 修改原因：机房模式选中卡片后必须显示与原完整行模式一致的编辑能力，而不是维护第二套简化操作。
    // 修改方式：把原 else 分支中的完整 Key 行渲染集中到这个函数，RackGrid 展开项和普通完整行列表都调用它，并允许机房展开行在聚焦时保留插槽装饰。
    // 目的：保留 CoolingKeyRow、OAuth 导入/登录按钮、全部操作按钮和 UiSlot 挂载点，并避免两处分支后续不一致。
    if (!formData) return null;
      const providerName = formData.provider;
      const rtDisabled = runtimeKeyStatus[providerName]?.auto_disabled || [];
      const rtEntry = !keyObj.disabled ? rtDisabled.find((d: any) => d.key === keyObj.key) : undefined;
      const isRtDisabled = !!rtEntry;
      const isPermanent = isRtDisabled && rtEntry.remaining_seconds < 0;
      const isCooling = isRtDisabled && !isPermanent && rtEntry.remaining_seconds > 0;
      const countdown = localCountdowns[providerName]?.[keyObj.key];
      const remainSec = countdown?.remaining ?? (rtEntry?.remaining_seconds || 0);

      // 永久自动禁用和配置禁用都用同样的变灰样式
      const isGrayed = keyObj.disabled || isPermanent;

      const isFocused = focusedKeyIdx === idx;
      // 修改原因：机房展开行使用 focusedKeyIdx 作为“展开”状态，但完整行原本会在 focused 时隐藏边框、背景和额度插槽。
      // 修改方式：增加 showDecorationsWhileFocused 选项，只有机房展开行开启时才在聚焦状态继续渲染这些插槽。
      // 目的：让 key_border、key_background 和 quota_display 在机房展开行中不被误隐藏。
      const showRowDecorations = !isFocused || options.showDecorationsWhileFocused;
      const bal = balanceResults[keyObj.key];
      const oauthAccount = oauthAccounts[keyObj.key];
      // 修改原因：完整 Key 行的额度展示不应再先判断 OAuth 或普通余额，也不应再渲染通用 badge 标签。
      // 修改方式：统一调用 buildRowQuota 得到 gauges，再从 gauges 派生默认边框需要的 inner/outer 兼容数据。
      // 目的：QuotaRings、QuotaBorderOverlay 和 UiSlot 共享同一个行模型，tier/plan 展示交给 quota_display。
      const rowQuota = buildRowQuota(bal, oauthAccount, isOAuthEngine);
      const rowQuotaPair = getQuotaPairFromGauges(rowQuota.gauges);
      const rowQuotaHasValues = rowQuota.gauges.length > 0;
      const slotData = buildRowQuotaSlotData(bal, oauthAccount, rowQuota);
      const slotPayloadAvailable = Boolean(slotData || oauthAccount || rowQuotaHasValues);
      const slotContext = { account: oauthAccount, keyObj, balance: bal };
      const enabledPlugins = formData.preferences.enabled_plugins || [];
      // 修改原因：Key 行只应知道通用插槽名，不能在渲染层读取渠道专属字段。
      // 修改方式：按当前 engine 和 provider.enabled_plugins 查询各插槽是否存在，后续只决定挂载点和默认回退。
      // 目的：把边框、背景和额度标签的渠道差异全部交给 ui_slots 脚本，并正确处理 requires_plugin 条件。
      const hasKeyBorderSlot = hasUiSlot(formData.engine, 'key_border', enabledPlugins);
      const hasKeyBackgroundSlot = hasUiSlot(formData.engine, 'key_background', enabledPlugins);
      const hasQuotaDisplaySlot = hasUiSlot(formData.engine, 'quota_display', enabledPlugins);

      if (isCooling) {
        return (
          <CoolingKeyRow
            key={idx}
            idx={idx}
            keyObj={keyObj}
            remainSec={remainSec}
            totalDuration={countdown?.duration ?? rtEntry?.duration ?? remainSec}
            focused={isFocused}
            onFocus={() => setFocusedKeyIdx(idx)}
            onBlur={() => setFocusedKeyIdx(null)}
            onRecover={async () => { await apiFetch('/v1/channels/key_status/re_enable', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ provider: providerName, key: keyObj.key }) }); refreshKeyStatus(); }}
            onToggle={() => toggleKeyDisabled(idx)}
            onTest={() => openKeyTestDialog(idx)}
            onDelete={() => deleteKey(idx)}
            onLabelChange={(label) => {
              const newKeys = [...formData.api_keys];
              newKeys[idx] = { ...newKeys[idx], label: label || undefined };
              setFormData(prev => prev ? { ...prev, api_keys: newKeys } : prev);
            }}
          />
        );
      }

      const balPct = bal ? getBalancePercent(bal) : null;
      const balColor = getBalanceColor(balPct);
      const hasTag = !isGrayed && (rowQuotaHasValues || isPermanent || (hasQuotaDisplaySlot && slotPayloadAvailable) || !!oauthAccount || !!bal);

      return (
        <div
          key={idx}
          onBlur={e => {
            // 修改原因：完整行在机房模式中是临时展开状态，焦点离开整行后应回到紧凑网格。
            // 修改方式：利用 React 冒泡的 onBlur 检查 relatedTarget 是否仍在当前行内，离开时清空 focusedKeyIdx。
            // 目的：保留行内按钮和输入框切换焦点的编辑体验，同时支持失焦自动收起。
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocusedKeyIdx(null);
          }}
          className={`relative flex items-center gap-2 px-3 py-2 rounded-lg border transition-colors ${isFocused ? 'border-blue-500' : 'border-border'} ${isGrayed ? (isFocused ? 'bg-muted/30' : 'bg-muted/30 opacity-50') : 'bg-muted/50'}`}
        >
          {/* 修改原因：key 行边框可能由渠道自定义，但默认双弧边框已经是平台通用能力；渠道脚本需要知道这里是完整行布局。
              修改方式：存在 key_border 插槽时挂载 absolute div 并传入当前行上下文和 mode: 'row'；不存在时用 rowQuotaPair 渲染 QuotaBorderOverlay。
              目的：让 OAuth 和普通 Key 的双 gauge 都能使用同一套默认边框，并保持完整行样式不受机房卡片逻辑影响。 */}
          {showRowDecorations && rowQuotaPair && (
            hasKeyBorderSlot
              ? <UiSlot engine={formData.engine} slot="key_border" data={slotData} context={{ ...slotContext, mode: 'row' }} element="div" className="absolute inset-0 pointer-events-none z-[1]" enabledPlugins={enabledPlugins} />
              : <QuotaBorderOverlay quotaInner={rowQuotaPair.quota_inner} quotaOuter={rowQuotaPair.quota_outer} />
          )}
          {/* 修改原因：背景装饰属于渠道自定义 UI，通用前端不应读取任何渠道私有字段或计算专属颜色；完整行背景条应继续横向填充。
              修改方式：只要渠道注册 key_background 插槽且当前行有可用上下文，就挂载覆盖整行的 absolute div，并传入 mode: 'row'。
              目的：让各渠道在自己的 ui_slots 中实现背景条，Channels.tsx 只提供挂载点，同时保留完整行旧布局。 */}
          {showRowDecorations && slotPayloadAvailable && hasKeyBackgroundSlot && (
            <UiSlot engine={formData.engine} slot="key_background" data={slotData} context={{ ...slotContext, mode: 'row' }} element="div" className="absolute inset-0 pointer-events-none rounded-[7px] z-0 transition-all duration-500" enabledPlugins={enabledPlugins} />
          )}
          {/* 修改原因：普通余额背景条是默认回退背景；如果渠道提供 key_background 插槽，应由插槽接管背景表达。
              修改方式：普通背景条增加 !hasKeyBackgroundSlot 条件，避免两层背景互相覆盖。
              目的：在保留默认余额条的同时，让自定义背景拥有明确优先级。 */}
          {!hasKeyBackgroundSlot && !isFocused && balColor && balPct != null && (
            <div className="absolute left-0 top-0 bottom-0 rounded-[7px] z-0 pointer-events-none transition-all duration-500"
                 style={{ width: `${Math.max(1, balPct)}%`, background: BALANCE_FILL_COLORS[balColor] }} />
          )}
          <span className="text-xs text-muted-foreground w-4 text-right relative z-[2]">{idx + 1}</span>

          {/* 修改原因：Key 备注遮罩需要按备注真实渲染宽度计算，不能继续使用固定 30% 宽度。
              修改方式：把备注覆盖层和输入框交给 KeyLabelOverlay 统一处理，由组件测量 label 后直接写入两个 mask。
              目的：短备注少占输入空间，长备注尽量完整显示，并保持右侧标签渐隐逻辑。 */}
          <KeyLabelOverlay label={keyObj.label} hasTag={hasTag} isFocused={isFocused}>
            <input
              type="text"
              value={keyObj.key}
              onChange={e => updateKey(idx, e.target.value)}
              onPaste={e => handleKeyPaste(e, idx)}
              onFocus={() => isOAuthEngine ? handleOAuthKeyFocus(idx, keyObj.key) : setFocusedKeyIdx(idx)}
              onBlur={e => { if (isOAuthEngine && !e.currentTarget.closest('[tabindex]')?.contains(e.relatedTarget as Node)) handleOAuthKeyBlur(idx, e.currentTarget.value); }}
              placeholder={isOAuthEngine ? "邮箱或标识符" : "sk-..."}
              className={`w-full bg-transparent border-none text-sm leading-5 font-mono outline-none min-w-0 ${isGrayed ? 'text-muted-foreground line-through' : 'text-foreground'}`}
            />
          </KeyLabelOverlay>
          {isOAuthEngine && !keyObj.key && (
            <>
              {/* 修改原因：OAuth 新增空行需要把账号导入和后续浏览器登录入口放在输入框右侧。 */}
              <button onClick={() => openImportModal(idx)} className="text-xs px-2 py-1 rounded border border-border bg-muted hover:bg-muted/80 text-foreground flex items-center gap-1 relative z-[2]" title="粘贴 Refresh Token">
                <ClipboardPaste className="w-3 h-3" /> 导入
              </button>
              <button onClick={() => startOAuthLogin(idx)} className="text-xs px-2 py-1 rounded border border-primary/50 bg-primary/10 hover:bg-primary/20 text-primary flex items-center gap-1 relative z-[2]" title="浏览器登录">
                <LogIn className="w-3 h-3" /> 登录
              </button>
            </>
          )}
          {/* 修改原因：完整行额度展示需要继续使用渠道的完整标签样式，不能被机房卡片短文本分支影响。
              修改方式：quota_display 插槽优先，并在上下文中传入 mode: 'row'；没有插槽时才用内置默认。
              目的：保持完整行 plan、tier、金额和百分比标签的旧展示能力。 */}
          {showRowDecorations && hasQuotaDisplaySlot && slotPayloadAvailable && (
            <UiSlot engine={formData.engine} slot="quota_display" data={slotData} context={{ ...slotContext, mode: 'row' }} className="flex-shrink-0 relative z-[2]" enabledPlugins={enabledPlugins} />
          )}
          {showRowDecorations && !isFocused && bal && !hasQuotaDisplaySlot && (
            <span className={`flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] ${TAG_CLASSES[getBalanceColor(getBalancePercent(bal)) || 'green']}`}>{getBalanceLabel(bal)}</span>
          )}

          {isOAuthEngine && !isFocused && oauthAccount && !rowQuotaHasValues && !hasQuotaDisplaySlot && (
            <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-500 relative z-[2]">
              {oauthAccount.status === 'active' ? '已连接' : oauthAccount.status === 'error' ? '刷新失败' : '冷却中'}
            </span>
          )}
          {!isFocused && isPermanent && <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/15 text-red-500 dark:text-red-400 font-medium flex-shrink-0 relative z-[2]">永久禁用</span>}
          {!isFocused && isPermanent && (
            <button onClick={async () => { await apiFetch('/v1/channels/key_status/re_enable', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ provider: providerName, key: keyObj.key }) }); refreshKeyStatus(); }} className="text-[11px] px-2 py-0.5 rounded border border-emerald-500/50 bg-emerald-500/20 text-emerald-400 font-medium hover:bg-emerald-500/30 hover:border-emerald-400 cursor-pointer flex-shrink-0 relative z-[2] transition-colors">恢复</button>
          )}

          <button onClick={() => toggleKeyDisabled(idx)} className={`relative z-[2] ${isGrayed ? 'text-muted-foreground' : 'text-emerald-500'}`} title={keyObj.disabled ? "启用" : "禁用"}>
            {keyObj.disabled ? <ToggleLeft className="w-5 h-5" /> : <ToggleRight className="w-5 h-5" />}
          </button>
          <button onClick={() => openKeyTestDialog(idx)} disabled={!keyObj.key.trim()} className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 disabled:opacity-50 disabled:cursor-not-allowed relative z-[2]" title="测试此 Key">
            <Play className="w-4 h-4" />
          </button>
          <button onClick={() => deleteKey(idx)} className="text-red-500 hover:text-red-400 ml-1 relative z-[2]"><Trash2 className="w-4 h-4" /></button>
          {/* Label 编辑：聚焦时在行底部展开 */}
          {isFocused && (
            <div className="absolute left-0 right-0 -bottom-6 flex items-center gap-1 z-[5]">
              <span className="text-[10px] text-muted-foreground/50 pl-8">备注:</span>
              <input
                type="text"
                value={keyObj.label || ''}
                onChange={e => {
                  const newKeys = [...formData.api_keys];
                  newKeys[idx] = { ...newKeys[idx], label: e.target.value || undefined };
                  setFormData(prev => prev ? { ...prev, api_keys: newKeys } : prev);
                }}
                onFocus={() => setFocusedKeyIdx(idx)}
                placeholder="点击添加备注"
                className="flex-1 bg-background/80 backdrop-blur-sm border border-border/50 rounded px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400 font-mono outline-none focus:border-amber-500/50 placeholder:text-muted-foreground/30"
              />
            </div>
          )}
        </div>
      );

  };

  return (
    <div className="space-y-6 animate-in fade-in duration-500 font-sans">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-foreground">渠道配置</h1>
          <p className="text-muted-foreground mt-1 text-sm sm:text-base">管理上游大模型 API 提供商及流量分发路由</p>
        </div>
        {/* 修改原因：虚拟模型的新建入口需要与普通渠道入口并列展示。
            修改方式：保留原“添加渠道”按钮，并新增紫色“新建虚拟模型”按钮打开抽屉。
            目的：用户不再需要到顶部画布内创建虚拟模型，列表顶部即可进入新建流程。 */}
        <div className="flex flex-col sm:flex-row gap-2 w-full sm:w-auto">
          <button onClick={() => openModal()} className="bg-primary hover:bg-primary/90 text-primary-foreground px-4 py-2 rounded-lg flex items-center gap-2 font-medium transition-colors w-full sm:w-auto justify-center">
            <Plus className="w-4 h-4" />
            添加渠道
          </button>
          <button onClick={() => openVirtualModelModal()} className="border border-purple-500/40 bg-purple-500/10 hover:bg-purple-500/15 text-purple-700 dark:text-purple-300 px-4 py-2 rounded-lg flex items-center gap-2 font-medium transition-colors w-full sm:w-auto justify-center">
            <Link2 className="w-4 h-4" />
            新建虚拟模型
          </button>
        </div>
      </div>

      {/* 修改原因：虚拟模型路由已改为下方列表顶部手风琴，顶部两栏画布会占用过多屏幕空间。
          修改方式：删除原画布渲染区，保留状态、保存函数和拖拽编辑能力供抽屉复用。
          目的：让用户进入页面后先看到渠道列表，并通过虚拟路由手风琴编辑虚拟模型。 */}

      {/* ── Filter Bar ── */}
      {!loading && totalListItemCount > 0 && (
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2">
          {/* 搜索框 */}
          <div className="relative flex-1 min-w-0">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
            <input
              type="text"
              value={filterKeyword}
              onChange={e => setFilterKeyword(e.target.value)}
              placeholder="搜索渠道名、备注、模型名…"
              className="w-full bg-background border border-border rounded-lg pl-9 pr-8 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary outline-none"
            />
            {filterKeyword && (
              <button
                onClick={() => setFilterKeyword('')}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                <XCircle className="w-4 h-4" />
              </button>
            )}
          </div>

          {/* 引擎筛选 */}
          <select
            value={filterEngine}
            onChange={e => setFilterEngine(e.target.value)}
            className="bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground min-w-[120px]"
          >
            <option value="">全部引擎</option>
            {availableEngines.map(eng => (
              <option key={eng} value={eng}>{eng}</option>
            ))}
          </select>

          {/* 分组筛选 */}
          <select
            value={filterGroup}
            onChange={e => setFilterGroup(e.target.value)}
            className="bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground min-w-[120px]"
          >
            <option value="">全部分组</option>
            {availableGroups.map(g => (
              <option key={g} value={g}>{g}</option>
            ))}
          </select>

          {/* 状态筛选 */}
          <select
            value={filterStatus}
            onChange={e => setFilterStatus(e.target.value as '' | 'enabled' | 'disabled')}
            className="bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground min-w-[100px]"
          >
            <option value="">全部状态</option>
            <option value="enabled">已启用</option>
            <option value="disabled">已禁用</option>
          </select>

          {/* 清除筛选 */}
          {hasActiveFilters && (
            <button
              onClick={() => { setFilterKeyword(''); setFilterEngine(''); setFilterGroup(''); setFilterStatus(''); }}
              className="flex items-center gap-1 px-3 py-2 text-xs text-muted-foreground hover:text-foreground bg-muted hover:bg-muted/80 rounded-lg transition-colors flex-shrink-0"
            >
              <X className="w-3 h-3" /> 清除
            </button>
          )}
        </div>
      )}

      {/* 筛选结果统计 */}
      {!loading && hasActiveFilters && (
        <div className="text-xs text-muted-foreground">
          筛选结果：{visibleListItemCount}/{totalListItemCount} 个条目
          {filterKeyword && visibleListItemCount > 0 && (
            <span className="ml-2 text-primary">含模型名或链条匹配</span>
          )}
        </div>
      )}

      {/* Mobile Card List */}
      <div className="md:hidden space-y-4">
        {loading ? (
          <div className="p-8 text-center text-muted-foreground">加载中...</div>
        ) : visibleListItemCount === 0 ? (
          <div className="p-12 text-center text-muted-foreground">{totalListItemCount === 0 ? '暂无渠道配置，点击上方按钮添加。' : '没有符合筛选条件的渠道。'}</div>
        ) : (
          <>
            {renderMobileVirtualRoutesAccordion()}
            {segments.map((seg, si) => seg.type === 'active' ? (
              <ProviderCard key={`a-${seg.item.idx}-${seg.item.p.provider || si}`} p={seg.item.p} idx={seg.item.idx} />
            ) : (
              <div key={`i-${seg.startIndex}`} className="border border-border rounded-xl overflow-hidden">
                <button onClick={() => toggleInactiveGroup(seg.startIndex)} className="w-full flex items-center justify-between px-4 py-3 bg-muted/30 hover:bg-muted/50 transition-colors text-sm">
                  <span className="text-muted-foreground">不活跃渠道 ({seg.items.length})</span>
                  <span className="text-xs text-muted-foreground">{expandedInactiveGroups.has(seg.startIndex) ? '▲' : '▼'}</span>
                </button>
                {expandedInactiveGroups.has(seg.startIndex) && (
                  <div className="space-y-4 p-2 opacity-70">
                    {seg.items.map(({ p, idx }) => <ProviderCard key={idx} p={p} idx={idx} />)}
                  </div>
                )}
              </div>
            ))}
          </>
        )}
      </div>

      {/* Desktop Table */}
      <div className="hidden md:block bg-card border border-border rounded-xl overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-muted-foreground">加载中...</div>
        ) : visibleListItemCount === 0 ? (
          <div className="p-12 text-center text-muted-foreground">{totalListItemCount === 0 ? '暂无渠道配置，点击右上角添加。' : '没有符合筛选条件的渠道。'}</div>
        ) : (
          <table className="w-full text-left border-collapse table-fixed">
            <thead className="bg-muted border-b border-border text-muted-foreground text-sm font-medium">
              <tr>
                <th className="px-4 py-3 w-[18%]">名称</th>
                <th className="px-4 py-3 w-[15%]">分组 / 类型</th>
                <th className="px-4 py-3 w-[8%] text-center">Keys</th>
                <th className="px-4 py-3 w-[10%]">模型 / 插件</th>
                <th className="px-4 py-3 w-[10%] text-center">状态</th>
                <th className="px-4 py-3 w-[10%] text-center">权重</th>
                <th className="px-4 py-3 w-[29%] text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border text-sm">
              {renderDesktopVirtualRoutesAccordionRows()}
              {(() => {
                const rows: any[] = [];
                segments.forEach((seg) => {
                  if (seg.type === 'active') {
                    rows.push({ p: seg.item.p, idx: seg.item.idx, inactive: false });
                  } else {
                    rows.push({ type: 'collapse-btn', startIndex: seg.startIndex, count: seg.items.length });
                    if (expandedInactiveGroups.has(seg.startIndex)) {
                      seg.items.forEach(item => rows.push({ p: item.p, idx: item.idx, inactive: true }));
                    }
                  }
                });
                return rows.map((row, ri) => {
                  if (row.type === 'collapse-btn') {
                    return (
                      <tr key={`ig-${row.startIndex}`}>
                        <td colSpan={7} className="p-0">
                          <button onClick={() => toggleInactiveGroup(row.startIndex)} className="w-full flex items-center justify-between px-4 py-2 bg-muted/20 hover:bg-muted/40 transition-colors">
                            <span className="text-muted-foreground text-xs">不活跃渠道 ({row.count})</span>
                            <span className="text-xs text-muted-foreground">{expandedInactiveGroups.has(row.startIndex) ? '▲' : '▼'}</span>
                          </button>
                        </td>
                      </tr>
                    );
                  }
                  const { p, idx, inactive: isInactive } = row;
                  const isEnabled = p.enabled !== false;
                  const groups = Array.isArray(p.groups) ? p.groups : p.group ? [p.group] : ['default'];
                  const plugins = p.preferences?.enabled_plugins || [];
                  const weight = p.preferences?.weight ?? p.weight ?? 0;

                  // Key 统计
                  const apiRaw = Array.isArray(p.api) ? p.api : (typeof p.api === 'string' && p.api.trim() ? [p.api] : []);
                  const totalKeys = apiRaw.length;
                  const configDisabledKeys = apiRaw.filter((k: any) => {
                    if (typeof k === 'string') return k.startsWith('!');
                    if (k && typeof k === 'object') { const key = Object.keys(k)[0] || ''; return key.startsWith('!'); }
                    return false;
                  }).length;
                  const rtStatus = runtimeKeyStatus[p.provider];
                  const rtDisabledCount = rtStatus?.auto_disabled?.length || 0;
                  const enabledKeys = totalKeys - configDisabledKeys;
                  const effectiveEnabled = Math.max(0, enabledKeys - rtDisabledCount);
                  const hasKeyIssue = configDisabledKeys > 0 || rtDisabledCount > 0;

                  // 模型名匹配高亮
                  const matchedModels = getMatchedModels(p);

                  return (<>
                  <tr key={idx} className={`transition-colors ${isInactive ? 'opacity-50' : ''} ${isEnabled ? 'hover:bg-muted/50' : 'bg-muted/30 opacity-60'}`}>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <ProviderLogo name={p.provider} engine={p.engine} baseUrl={p.base_url} />
                        <div className="min-w-0">
                          <div className={`font-medium truncate ${isEnabled ? 'text-foreground' : 'text-muted-foreground'}`}>{p.provider}</div>
                          {p.remark && (
                            <div className="text-xs text-muted-foreground truncate max-w-xs" title={p.remark}>
                              {p.remark}
                            </div>
                          )}
                          {matchedModels.length > 0 && (
                            <div className="flex flex-wrap gap-0.5 mt-0.5">
                              {matchedModels.slice(0, 2).map((m, i) => (
                                <span key={i} className="text-[10px] font-mono px-1 py-px rounded bg-primary/10 text-primary truncate max-w-[120px]" title={m}>{m}</span>
                              ))}
                              {matchedModels.length > 2 && <span className="text-[10px] text-muted-foreground">+{matchedModels.length - 2}</span>}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-col gap-1">
                        <div className="flex gap-1 flex-wrap">
                          {groups.slice(0, 2).map((g: string, i: number) => (
                            <span key={i} className="bg-muted text-foreground px-1.5 py-0.5 rounded text-xs truncate max-w-[80px]" title={g}>{g}</span>
                          ))}
                          {groups.length > 2 && <span className="text-xs text-muted-foreground">+{groups.length - 2}</span>}
                        </div>
                        <span className="text-xs text-muted-foreground font-mono">{p.engine || 'openai'}</span>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-center">
                      {totalKeys > 0 ? (
                        <span
                          className={`text-xs font-mono px-1.5 py-0.5 rounded ${
                            hasKeyIssue ? 'bg-orange-500/10 text-orange-600 dark:text-orange-400' : 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-500'
                          }`}
                          title={`可用: ${effectiveEnabled} / 总计: ${totalKeys}${configDisabledKeys > 0 ? ` (配置禁用: ${configDisabledKeys})` : ''}${rtDisabledCount > 0 ? ` (自动禁用: ${rtDisabledCount})` : ''}`}
                        >
                          {effectiveEnabled}/{totalKeys}
                        </span>
                      ) : (
                        <span className="text-muted-foreground/50">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {plugins.length > 0 ? (
                        <span className="bg-primary/10 text-primary px-1.5 py-0.5 rounded text-xs">
                          {plugins.length} 个
                        </span>
                      ) : <span className="text-muted-foreground/50">—</span>}
                    </td>
                    <td className="px-4 py-3 text-center">
                      <span className={`inline-flex items-center justify-center w-6 h-6 rounded-full ${isEnabled ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-500' : 'bg-red-500/10 text-red-600 dark:text-red-500'}`} title={isEnabled ? '已启用' : '已禁用'}>
                        {isEnabled ? <CheckCircle2 className="w-4 h-4" /> : <X className="w-4 h-4" />}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <input
                        type="number"
                        value={weight}
                        onChange={e => handleUpdateWeight(idx, parseInt(e.target.value) || 0)}
                        onClick={e => e.stopPropagation()}
                        className="w-14 bg-muted border border-border rounded px-1 py-1 text-center font-mono text-sm text-foreground focus:border-primary outline-none"
                      />
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1">
                        <button onClick={() => { setAnalyticsProvider(getProviderAnalyticsName(p)); setAnalyticsOpen(true); }} className="p-1.5 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-500/10 rounded-md transition-colors" title="分析">
                          <BarChart3 className="w-4 h-4" />
                        </button>
                        <button onClick={() => openTestDialog(p)} className="p-1.5 text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 rounded-md transition-colors" title="测试">
                          <Play className="w-4 h-4" />
                        </button>
                        <button onClick={() => handleToggleProvider(idx)} className={`p-1.5 rounded-md transition-colors ${isEnabled ? 'text-emerald-600 dark:text-emerald-500 hover:bg-emerald-500/10' : 'text-muted-foreground hover:bg-muted'}`} title={isEnabled ? '禁用' : '启用'}>
                          <Power className="w-4 h-4" />
                        </button>
                        <button onClick={() => handleCopyProvider(p)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors" title="复制">
                          <Files className="w-4 h-4" />
                        </button>
                        <button onClick={() => openModal(p, idx)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors" title="编辑">
                          <Edit className="w-4 h-4" />
                        </button>
                        <button onClick={() => handleDeleteProvider(idx)} className="p-1.5 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md transition-colors" title="删除">
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                  {/* 子渠道二级行 */}
                  {(p.sub_channels || []).map((sub: any, subIdx: number) => {
                    const subEnabled = sub.enabled !== false;
                    const subModels = Array.isArray(sub.model) ? sub.model : Array.isArray(sub.models) ? sub.models : [];
                    const subModelCount = subModels.filter((m: any) => typeof m === 'string').length;
                    const subPlugins = sub.preferences?.enabled_plugins || [];
                    return (
                      <tr key={`${idx}-sub-${subIdx}`} className={`transition-colors bg-muted/20 ${!subEnabled && 'opacity-50'}`}>
                        <td className="px-4 py-2 pl-10" colSpan={1}>
                          <div className="flex items-center gap-2">
                            <span className="text-muted-foreground text-xs">└</span>
                            <span className="text-xs font-medium text-foreground">{sub.remark || sub.engine || '?'}</span>
                            <span className="text-[10px] text-muted-foreground">({subModelCount} 模型)</span>
                          </div>
                        </td>
                        <td className="px-4 py-2">
                          <span className="text-xs text-muted-foreground font-mono">{sub.remark || sub.engine || '-'}</span>
                        </td>
                        <td className="px-4 py-2 text-center">
                          <span className="text-xs text-muted-foreground">共享</span>
                        </td>
                        <td className="px-4 py-2">
                          {subPlugins.length > 0 ? (
                            <span className="bg-primary/10 text-primary px-1.5 py-0.5 rounded text-[10px]">{subPlugins.length}</span>
                          ) : <span className="text-[10px] text-muted-foreground">继承</span>}
                        </td>
                        <td className="px-4 py-2 text-center">
                          <span className={`inline-flex items-center justify-center w-5 h-5 rounded-full ${subEnabled ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-500' : 'bg-red-500/10 text-red-600 dark:text-red-500'}`}>
                            {subEnabled ? <CheckCircle2 className="w-3.5 h-3.5" /> : <X className="w-3.5 h-3.5" />}
                          </span>
                        </td>
                        <td className="px-4 py-2 text-center">
                          <span className="text-xs text-muted-foreground">{sub.preferences?.weight ?? '—'}</span>
                        </td>
                        <td className="px-4 py-2 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <button onClick={() => { const sp = buildSubChannelProvider(idx, subIdx); if (sp) openTestDialog(sp); }} className="p-1 text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 rounded-md transition-colors" title="测试子渠道">
                              <Play className="w-3.5 h-3.5" />
                            </button>
                            <button onClick={() => handleToggleSubChannel(idx, subIdx)} className={`p-1 rounded-md transition-colors ${subEnabled ? 'text-emerald-600 dark:text-emerald-500 hover:bg-emerald-500/10' : 'text-muted-foreground hover:bg-muted'}`} title={subEnabled ? '禁用' : '启用'}>
                              <Power className="w-3.5 h-3.5" />
                            </button>
                            <button onClick={() => openSubChannelEdit(idx, subIdx)} className="p-1 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors" title="编辑子渠道">
                              <Edit className="w-3.5 h-3.5" />
                            </button>
                            <button onClick={() => handleDeleteSubChannel(idx, subIdx)} className="p-1 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md transition-colors" title="删除子渠道">
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </>
                );
              });
              })()}
            </tbody>
          </table>
        )}
      </div>

      {/* 修改原因：虚拟模型编辑从顶部内联画布迁移到抽屉，列表卡片只负责折叠展示。
          修改方式：复用原有渠道模型数据源和原生拖拽逻辑，在 Dialog 中布局左侧数据源和右侧链条编辑器。
          目的：保留完整编辑能力，同时把主页面空间还给渠道列表。 */}
      <Dialog.Root open={isVirtualModalOpen} onOpenChange={(open) => { setIsVirtualModalOpen(open); if (!open) setVirtualModelsDirty(false); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40 animate-in fade-in duration-200" />
          <Dialog.Content className="fixed right-0 top-0 h-full w-full xl:w-[1040px] max-w-full bg-background border-l border-border shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300">
            <div className="p-4 sm:p-5 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
              <div className="min-w-0">
                <Dialog.Title className="text-lg sm:text-xl font-bold text-foreground flex items-center gap-2">
                  <Link2 className="w-5 h-5 text-purple-500" />
                  {editingVirtualName ? `编辑虚拟模型: ${editingVirtualName}` : '新建虚拟模型'}
                </Dialog.Title>
                <Dialog.Description className="text-xs text-muted-foreground mt-1">
                  移动端可展开上方渠道面板查看完整渠道列表；桌面端可展开左侧渠道面板拖拽添加。
                </Dialog.Description>
              </div>
              <Dialog.Close className="text-muted-foreground hover:text-foreground"><X className="w-5 h-5" /></Dialog.Close>
            </div>

            {/* 修改原因：移动端也需要看到完整渠道列表，但默认展开会挤压链条编辑区。
                修改方式：小屏使用上方可折叠面板，展开时限制 max-height；xl 以上继续使用桌面左右两栏。
                目的：让手机用户按需查看渠道列表，同时保证链条编辑区不会被完全推走。 */}
            <div className={`flex-1 min-h-0 grid grid-cols-1 grid-rows-[auto_minmax(0,1fr)] ${isVirtualProviderPanelCollapsed ? 'xl:grid-cols-[76px_1fr]' : 'xl:grid-cols-[300px_1fr]'} xl:grid-rows-[minmax(0,1fr)] xl:divide-x divide-border overflow-hidden`}>
              <aside className={`min-h-0 bg-muted/10 border-b border-border xl:border-b-0 xl:overflow-y-auto ${isVirtualProviderPanelCollapsed ? 'xl:p-2' : 'xl:p-3'}`}>
                <div className="xl:hidden">
                  {/* 修改原因：移动端默认折叠时需要保留一行明确入口，告诉用户渠道面板仍然可用。
                      修改方式：显示渠道数量和展开箭头，点击后打开同一份完整渠道列表。
                      目的：不再完全隐藏渠道面板，同时避免默认占用链条编辑空间。 */}
                  <button
                    type="button"
                    onClick={() => setIsVirtualMobileProviderPanelOpen(prev => !prev)}
                    aria-expanded={isVirtualMobileProviderPanelOpen}
                    className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left bg-muted/20 hover:bg-muted/40 transition-colors"
                  >
                    <span className="text-sm font-medium text-foreground">📦 渠道面板 ({virtualProviderPanelItems.length}个渠道)</span>
                    <span className="text-xs text-muted-foreground">{isVirtualMobileProviderPanelOpen ? '▲' : '▼'}</span>
                  </button>
                  {isVirtualMobileProviderPanelOpen && (
                    <div className="max-h-[50vh] overflow-y-auto border-t border-border p-3">
                      <div className="flex items-center justify-between gap-2 mb-2">
                        <div className="min-w-0">
                          <h3 className="text-sm font-semibold text-foreground">渠道和模型</h3>
                          <p className="text-[11px] text-muted-foreground mt-0.5 truncate">已启用渠道，按权重降序，子渠道跟在主渠道后。</p>
                        </div>
                        <button
                          type="button"
                          onClick={() => setIsVirtualMobileProviderPanelOpen(false)}
                          className="px-2 py-1 rounded-md text-xs text-muted-foreground hover:text-foreground hover:bg-muted transition-colors flex-shrink-0"
                        >
                          收起
                        </button>
                      </div>
                      {renderVirtualProviderPanelList()}
                    </div>
                  )}
                </div>

                <div className="hidden xl:block">
                  <div className={`flex items-center gap-2 mb-2 ${isVirtualProviderPanelCollapsed ? 'justify-center' : 'justify-between'}`}>
                    {isVirtualProviderPanelCollapsed ? (
                      <button
                        type="button"
                        onClick={() => setIsVirtualProviderPanelCollapsed(false)}
                        className="w-11 h-10 rounded-lg border border-border bg-background hover:bg-muted/60 text-muted-foreground hover:text-foreground flex items-center justify-center transition-colors"
                        title="展开渠道面板"
                      >
                        <Server className="w-4 h-4" />
                        <span className="sr-only">展开渠道面板</span>
                      </button>
                    ) : (
                      <>
                        <div className="min-w-0">
                          <h3 className="text-sm font-semibold text-foreground">渠道和模型</h3>
                          <p className="text-[11px] text-muted-foreground mt-0.5 truncate">已启用渠道，按权重降序，子渠道跟在主渠道后。</p>
                        </div>
                        <button
                          type="button"
                          onClick={() => setIsVirtualProviderPanelCollapsed(true)}
                          className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors flex-shrink-0"
                          title="收起渠道面板"
                        >
                          <ArrowRight className="w-4 h-4" />
                        </button>
                      </>
                    )}
                  </div>

                  {isVirtualProviderPanelCollapsed ? renderVirtualProviderPanelCollapsedRail() : renderVirtualProviderPanelList()}
                </div>
              </aside>

              <section className="min-h-0 flex flex-col overflow-hidden">
                <div className="p-4 border-b border-border bg-background/60 flex-shrink-0">
                  <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_auto] gap-3 items-center">
                    <div>
                      <label className="text-xs font-medium text-muted-foreground mb-1.5 block">虚拟模型名</label>
                      <input
                        type="text"
                        value={virtualDraftName}
                        onChange={e => { setVirtualDraftName(e.target.value); setVirtualModelsDirty(true); }}
                        placeholder="例如 deepseek-chat"
                        className="w-full bg-background border border-border focus:border-purple-500 px-3 py-2 rounded-lg text-sm font-mono outline-none text-foreground"
                      />
                    </div>
                    <label className="flex items-center justify-between gap-3 px-3 py-2 bg-muted/50 rounded-lg border border-border min-w-[120px] self-end">
                      <span className="text-sm font-medium text-foreground">启用</span>
                      <Switch.Root checked={virtualDraftEnabled} onCheckedChange={val => { setVirtualDraftEnabled(val); setVirtualModelsDirty(true); }} className="w-10 h-5 bg-muted rounded-full relative data-[state=checked]:bg-purple-500 transition-colors">
                        <Switch.Thumb className="block w-4 h-4 bg-white rounded-full shadow-md transition-transform translate-x-0.5 data-[state=checked]:translate-x-[20px]" />
                      </Switch.Root>
                    </label>
                  </div>
                  {virtualModelsDirty && <div className="mt-3 text-xs text-amber-600 dark:text-amber-400 bg-amber-500/10 px-2 py-1 rounded-lg inline-flex">有未保存更改</div>}
                </div>

                <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3" onClick={() => setFocusedKeyIdx(null)}>
                  <div
                    onDragOver={e => e.preventDefault()}
                    onDrop={e => handleVirtualEditorDrop(e)}
                    className="min-h-[260px] border border-dashed border-border rounded-xl bg-muted/10 p-3"
                  >
                    {virtualEditorChain.length === 0 ? (
                      <div className="h-56 flex items-center justify-center text-sm text-muted-foreground text-center">将左侧模型或渠道拖到这里，或使用底部按钮添加节点。</div>
                    ) : (
                      <div className="space-y-3">
                        {virtualEditorChain.map((node, idx) => {
                          const isChannel = node.type === 'channel';
                          const provider = isChannel ? getProviderByName(node.value) : null;
                          const channelModelOptions = provider ? getProviderModelOptions(provider) : [];
                          const displayVirtualName = virtualDraftName.trim() || editingVirtualName || '当前虚拟模型';
                          const channelModelLabel = node.model || displayVirtualName;
                          const matchCount = !isChannel ? getMatchingProviderCount(node.value) : 0;
                          return (
                            <div
                              key={`virtual-editor-${idx}-${node.type}-${node.value}`}
                              draggable
                              onDragStart={e => handleChainNodeDragStart(e, '__virtual_editor__', idx)}
                              onDragOver={e => e.preventDefault()}
                              onDrop={e => { e.stopPropagation(); handleVirtualEditorDrop(e, idx); }}
                              className="relative flex gap-3"
                            >
                              {idx < virtualEditorChain.length - 1 && <div className="absolute left-[18px] top-10 bottom-[-14px] w-px bg-border" />}
                              <div className={`relative z-[1] w-9 h-9 rounded-full flex items-center justify-center border ${isChannel ? 'bg-emerald-500/10 border-emerald-500/25 text-emerald-600 dark:text-emerald-400' : 'bg-blue-500/10 border-blue-500/25 text-blue-600 dark:text-blue-400'}`}>
                                <span className={`w-2.5 h-2.5 rounded-full ${isChannel ? 'bg-emerald-500' : 'bg-blue-500'}`} />
                              </div>
                              <div className={`flex-1 min-w-0 border rounded-xl p-3 bg-background ${isChannel ? 'border-emerald-500/20' : 'border-blue-500/20'}`}>
                                <div className="flex items-start justify-between gap-3 mb-3">
                                  <div className="min-w-0">
                                    <div className="flex items-center gap-2 flex-wrap">
                                      <GripVertical className="w-4 h-4 text-muted-foreground cursor-grab flex-shrink-0" />
                                      <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${isChannel ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' : 'bg-blue-500/10 text-blue-600 dark:text-blue-400'}`}>{isChannel ? '渠道节点' : '模型节点'}</span>
                                      <span className="text-xs text-muted-foreground">优先级 {idx + 1}</span>
                                    </div>
                                    {isChannel ? (
                                      <div className="mt-2">
                                        <div className="font-mono text-sm text-foreground truncate">{node.value ? `${node.value}: ${channelModelLabel}` : '未选择渠道'}</div>
                                        <div className="text-xs text-muted-foreground mt-0.5">{describeVirtualChannelNode(node, displayVirtualName)}</div>
                                      </div>
                                    ) : (
                                      <div className="mt-2">
                                        <div className="font-mono text-sm text-foreground truncate">{node.value || '未填写模型名'}</div>
                                        <div className="text-xs text-muted-foreground mt-0.5">匹配到 {matchCount} 个渠道</div>
                                      </div>
                                    )}
                                  </div>
                                  {/* 修改原因：移动端无法使用 HTML5 原生拖拽排序，节点卡片需要额外提供触摸可点的排序控件。
                                      修改方式：在删除按钮左侧加入上移和下移小按钮，禁用首尾无法移动的方向，并调用相邻交换函数。
                                      目的：不移除桌面拖拽能力的前提下，保证手机端也能调整链条节点顺序。 */}
                                  <div className="flex items-center gap-1 flex-shrink-0">
                                    <button
                                      type="button"
                                      onClick={() => swapVirtualEditorNode(idx, -1)}
                                      disabled={idx === 0}
                                      className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                                      title="上移节点"
                                      aria-label={`上移第 ${idx + 1} 个节点`}
                                    >
                                      <ChevronUp className="w-4 h-4" />
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => swapVirtualEditorNode(idx, 1)}
                                      disabled={idx === virtualEditorChain.length - 1}
                                      className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                                      title="下移节点"
                                      aria-label={`下移第 ${idx + 1} 个节点`}
                                    >
                                      <ChevronDown className="w-4 h-4" />
                                    </button>
                                    <button type="button" onClick={() => updateVirtualEditorChainDraft(prev => prev.filter((_, removeIdx) => removeIdx !== idx))} className="p-1.5 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md transition-colors" title="删除节点">
                                      <X className="w-4 h-4" />
                                    </button>
                                  </div>
                                </div>

                                {isChannel ? (
                                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                                    <select
                                      value={node.value}
                                      onChange={e => updateVirtualEditorNode(idx, { value: e.target.value })}
                                      className="w-full bg-background border border-border px-3 py-2 rounded-lg text-xs text-foreground outline-none focus:border-purple-500"
                                    >
                                      <option value="">选择渠道</option>
                                      {node.value && !providerNames.includes(node.value) && <option value={node.value}>{node.value}</option>}
                                      {providerNames.map(providerName => <option key={providerName} value={providerName}>{providerName}</option>)}
                                    </select>
                                    <select
                                      value={node.model || ''}
                                      onChange={e => updateVirtualEditorNode(idx, { model: e.target.value || undefined })}
                                      className="w-full bg-background border border-border px-3 py-2 rounded-lg text-xs text-foreground outline-none focus:border-purple-500"
                                    >
                                      <option value="">使用虚拟模型名：{displayVirtualName}</option>
                                      {node.model && !channelModelOptions.some(option => option.displayName === node.model) && <option value={node.model}>{node.model}</option>}
                                      {channelModelOptions.map(option => <option key={`${option.displayName}-${option.upstreamName}`} value={option.displayName}>{formatProviderModelOption(option)}</option>)}
                                    </select>
                                  </div>
                                ) : (
                                  <input
                                    value={node.value}
                                    onChange={e => updateVirtualEditorNode(idx, { value: e.target.value })}
                                    placeholder="模型名，例如 deepseek-chat"
                                    className="w-full bg-background border border-border px-3 py-2 rounded-lg text-xs font-mono text-foreground outline-none focus:border-purple-500"
                                  />
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-2">
                    <p className="text-xs text-muted-foreground">节点从上到下依次解析。模型节点全局匹配，渠道节点只匹配指定渠道。</p>
                    <div className="flex items-center gap-2">
                      <select
                        value={virtualAddNodeTypes[virtualDraftName.trim() || editingVirtualName || '__new_virtual_model__'] || 'model'}
                        onChange={e => {
                          const key = virtualDraftName.trim() || editingVirtualName || '__new_virtual_model__';
                          setVirtualAddNodeTypes(prev => ({ ...prev, [key]: e.target.value as 'model' | 'channel' }));
                        }}
                        className="bg-background border border-border rounded-lg px-2 py-2 text-xs text-foreground"
                      >
                        <option value="model">模型节点</option>
                        <option value="channel">渠道节点</option>
                      </select>
                      <button onClick={appendVirtualEditorNodeByType} className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg flex items-center gap-1.5 text-xs font-medium transition-colors">
                        <Plus className="w-3.5 h-3.5" /> 添加节点
                      </button>
                    </div>
                  </div>
                </div>
              </section>
            </div>

            <div className="p-4 bg-muted/30 border-t border-border flex justify-end gap-3 flex-shrink-0">
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              <button onClick={handleSaveVirtualEditor} className="px-4 py-2 text-sm font-medium text-white bg-purple-600 hover:bg-purple-700 rounded-lg flex items-center gap-1.5">
                <CheckCircle2 className="w-4 h-4" /> 保存虚拟模型
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>


      {/* Editor Side Sheet - Responsive */}
      {/* 修改原因：OAuth portal 弹窗打开时，编辑抽屉仍会接收到外部交互并尝试关闭。
          修改方式：复用 isOAuthOverlayOpen 判断，在 OAuth 覆盖弹窗存在时忽略抽屉关闭请求。
          目的：让用户处理 OAuth 弹窗时，底层编辑面板保持原状。 */}
      <Dialog.Root open={isModalOpen} modal={!isOAuthOverlayOpen} onOpenChange={(open) => { if (!open && isOAuthOverlayOpen) return; setIsModalOpen(open); if (!open) setEditingSubChannel(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40 animate-in fade-in duration-200" />
          {/* 修改原因：OAuth portal 弹窗位于 Dialog.Content 外部，Radix 会把外部焦点重新拉回编辑抽屉。
              修改方式：OAuth 覆盖弹窗打开时，阻止外部焦点和外部交互事件的默认处理。
              目的：允许 portal 弹窗中的 textarea 或 input 接收焦点，同时避免点击 OAuth 遮罩关闭底层抽屉。 */}
          <Dialog.Content
            className="fixed right-0 top-0 h-full w-full sm:w-[560px] bg-background border-l border-border shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300"
            onFocusOutside={(e) => {
              if (isOAuthOverlayOpen) {
                e.preventDefault();
              }
            }}
            onInteractOutside={(e) => {
              if (isOAuthOverlayOpen) {
                e.preventDefault();
              }
            }}
          >
            <div className="p-4 sm:p-5 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
              <Dialog.Title className="text-lg sm:text-xl font-bold text-foreground flex items-center gap-2">
                <Server className="w-5 h-5 text-primary" />
                {editingSubChannel ? `编辑子渠道: ${formData?.remark || formData?.engine || ''}` : originalIndex !== null ? `编辑: ${formData?.provider}` : '新增渠道'}
              </Dialog.Title>
              <Dialog.Close className="text-muted-foreground hover:text-foreground"><X className="w-5 h-5" /></Dialog.Close>
            </div>

            {formData && (
              <div className="flex-1 overflow-y-auto p-4 sm:p-5 space-y-6" onClick={(e) => { if (!(e.target as HTMLElement).closest('[data-key-scroll]')) setFocusedKeyIdx(null); }}>
                {/* 1. 基础配置 */}
                <section>
                  <div className="flex items-center gap-2 text-sm font-semibold text-foreground mb-4 border-b border-border pb-2">
                    <Server className="w-4 h-4 text-primary" /> 基础配置
                  </div>
                  <div className="space-y-4">
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className="text-sm font-medium text-foreground mb-1.5 block">{editingSubChannel ? '子渠道名称' : '渠道标识 (Provider)'}</label>
                        {editingSubChannel ? (
                          <input type="text" value={formData.remark} onChange={e => updateFormData('remark', e.target.value)} placeholder="给子渠道起个名字" className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm outline-none text-foreground" />
                        ) : (
                          <input type="text" value={formData.provider} onChange={e => updateFormData('provider', e.target.value)} placeholder="e.g. openai" className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm outline-none text-foreground" />
                        )}
                      </div>
                      <div>
                        <label className="text-sm font-medium text-foreground mb-1.5 block">核心引擎 (Engine)</label>
                        <select value={formData.engine} onChange={e => {
                          const val = e.target.value;
                          updateFormData('engine', val);
                          const sel = channelTypes.find(c => c.id === val);
                          if (sel?.default_base_url && !formData.base_url) updateFormData('base_url', sel.default_base_url);
                          if (sel?.default_token_url && !formData.token_url) updateFormData('token_url', sel.default_token_url);
                        }} className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm outline-none text-foreground">
                          <option value="">默认 (自动推断)</option>
                          {(() => {
                            const sort = (a: ChannelOption, b: ChannelOption) => {
                              if (a.id === 'openai') return -1;
                              if (b.id === 'openai') return 1;
                              return (a.description || a.id).localeCompare(b.description || b.id);
                            };
                            const builtIn = channelTypes.filter(c => !c.is_oauth && c.source !== 'plugin').sort(sort);
                            const oauth = channelTypes.filter(c => c.is_oauth && c.source !== 'plugin').sort(sort);
                            const plugin = channelTypes.filter(c => c.source === 'plugin').sort(sort);
                            return (<>
                              <optgroup label="内置通用">{builtIn.map(c => <option key={c.id} value={c.id}>{c.description || c.id}</option>)}</optgroup>
                              {oauth.length > 0 && <optgroup label="内置 OAuth">{oauth.map(c => <option key={c.id} value={c.id}>{c.description || c.id}</option>)}</optgroup>}
                              {plugin.length > 0 && <optgroup label="插件渠道">{plugin.map(c => <option key={c.id} value={c.id}>{c.description || c.id}</option>)}</optgroup>}
                            </>);
                          })()}
                        </select>
                      </div>
                    </div>
                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">API 地址 (Base URL)</label>
                      <input type="text" value={formData.base_url} onChange={e => updateFormData('base_url', e.target.value)} placeholder="留空则使用渠道默认地址，末尾加 # 则不拼接路径后缀" className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono outline-none text-foreground" />
                      <span className="text-xs text-muted-foreground mt-1 block">{'末尾加 # 可直接使用完整地址，不拼接路径后缀（如 https://example.com/v1/chat#）'}</span>
                      {hasUiSlot(formData.engine, 'base_url_hint', formData.preferences.enabled_plugins) && (
                        <>
                          {/* 修改原因：Base URL 的补充说明可能因渠道而异，通用前端不能写死具体渠道提示文案。 */}
                          {/* 修改方式：在 Base URL 输入框下方提供 base_url_hint 挂载点，仅当当前 engine 注册该插槽时渲染 UiSlot。 */}
                          {/* 目的：让渠道自行写入 Base URL 提示，未注册时不显示任何额外 DOM 或空白。 */}
                          <UiSlot engine={formData.engine} slot="base_url_hint" data={null} element="div" className="text-xs text-muted-foreground mt-1" enabledPlugins={formData.preferences.enabled_plugins || []} />
                        </>
                      )}
                    </div>
                    {/* 修改原因：OAuth 引擎需要单独配置 token exchange/refresh 地址，不能再把 Base URL 当作 token endpoint。
                        修改方式：仅在 OAuth 类型引擎下显示 token_url 输入框，并直接写入 formData.token_url。
                        目的：用户可以为 Codex、Claude Code、Antigravity 配置反代 token endpoint，留空时仍使用 provider 默认值。 */}
                    {isOAuthEngine && (
                      <div className="space-y-1">
                        <label className="text-xs font-medium text-muted-foreground">Token URL</label>
                        <input
                          type="text"
                          value={formData.token_url || ''}
                          onChange={e => setFormData(prev => prev ? { ...prev, token_url: e.target.value } : prev)}
                          placeholder={channelTypes.find(c => c.id === formData.engine)?.default_token_url || '留空使用默认地址（如需反代可填写）'}
                          className="w-full bg-muted border border-border rounded-lg p-2.5 text-sm outline-none focus:border-primary"
                        />
                        <p className="text-[10px] text-muted-foreground">OAuth token exchange 地址，用于换取和刷新 token。不填则使用各 provider 内置默认值。</p>
                        {hasUiSlot(formData.engine, 'token_url_hint', formData.preferences.enabled_plugins) && (
                          <UiSlot engine={formData.engine} slot="token_url_hint" data={null} element="div" className="text-xs text-muted-foreground mt-1" enabledPlugins={formData.preferences.enabled_plugins || []} />
                        )}
                      </div>
                    )}
                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">备注</label>
                      <textarea
                        value={formData.remark}
                        onChange={e => updateFormData('remark', e.target.value)}
                        rows={3} maxLength={500} placeholder="填写该渠道的用途、来源、限制说明等" className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm outline-none text-foreground"
                      />
                    </div>
                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">模型前缀 (可选)</label>
                      <div className="flex items-center gap-2">
                        <input type="text" value={formData.model_prefix} onChange={e => updateModelPrefix(e.target.value)} placeholder="例如 azure- 或 aws/" className="flex-1 bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono outline-none text-foreground" />
                        {formData.model_prefix.trim() && (
                          <label className="flex items-center gap-1.5 text-xs text-muted-foreground whitespace-nowrap cursor-pointer" title="开启后，该渠道的模型去掉前缀后也可被无前缀请求匹配到">
                            <Switch.Root checked={!!formData.preferences.pool_sharing} onCheckedChange={val => updatePreference('pool_sharing', val)} className="w-9 h-5 bg-muted rounded-full relative data-[state=checked]:bg-emerald-500 transition-colors flex-shrink-0">
                              <Switch.Thumb className="block w-4 h-4 bg-white rounded-full shadow-md transition-transform translate-x-0.5 data-[state=checked]:translate-x-[18px]" />
                            </Switch.Root>
                            共享路由池
                          </label>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center justify-between p-3 bg-muted/50 rounded-lg border border-border">
                      <span className="text-sm font-medium text-foreground">启用该渠道</span>
                      <Switch.Root checked={formData.enabled} onCheckedChange={val => updateFormData('enabled', val)} className="w-11 h-6 bg-muted rounded-full relative data-[state=checked]:bg-emerald-500 transition-colors">
                        <Switch.Thumb className="block w-5 h-5 bg-white rounded-full shadow-md transition-transform translate-x-0.5 data-[state=checked]:translate-x-[22px]" />
                      </Switch.Root>
                    </div>
                    {!editingSubChannel && <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">分组 (Groups)</label>
                      <div className="flex flex-wrap gap-2 mb-2 p-2 bg-muted/50 border border-border rounded-lg min-h-[40px]">
                        {formData.groups.map(g => (
                          <span key={g} className="bg-background border border-border text-foreground px-2 py-1 rounded text-xs flex items-center gap-1">
                            <Folder className="w-3 h-3" /> {g}
                            <button onClick={() => removeGroup(g)} className="ml-1 text-muted-foreground hover:text-red-500"><X className="w-3 h-3" /></button>
                          </span>
                        ))}
                      </div>
                      <input type="text" value={groupInput} onChange={e => setGroupInput(e.target.value)} onKeyDown={handleGroupInputKeyDown} placeholder="输入分组名并按回车..." className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm outline-none text-foreground" />
                    </div>}
                  </div>
                </section>

                {/* 2a. 子渠道模式：简化的 Key 测试入口 */}
                {editingSubChannel && (
                  <section>
                    <div className="flex items-center justify-between text-sm font-semibold text-foreground mb-2 border-b border-border pb-2">
                      <span className="flex items-center gap-2">
                        <Settings2 className="w-4 h-4 text-emerald-500" /> API Keys
                        <span className="text-xs font-normal text-muted-foreground">（继承主渠道）</span>
                      </span>
                    </div>
                    <div className="flex items-center gap-3 text-xs text-muted-foreground">
                      <span>共 <span className="font-mono text-foreground">{formData.api_keys.filter(k => !k.disabled).length}</span>/{formData.api_keys.length} 个可用 Key</span>
                      <button
                        onClick={() => openKeyTestDialog(null, {
                          engine: formData.engine || 'openai',
                          base_url: formData.base_url || '',
                          models: formData.models || [],
                          title: `测试 API Keys: ${formData.provider}`,
                        })}
                        disabled={formData.api_keys.length === 0}
                        className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 flex items-center gap-1 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        <Play className="w-3 h-3" /> 多key测试
                      </button>
                    </div>
                  </section>
                )}

                {/* 2. API Keys (子渠道模式隐藏) */}
                {!editingSubChannel && <section>
                  <div className="flex items-center justify-between text-sm font-semibold text-foreground mb-2 border-b border-border pb-2">
                    <span className="flex items-center gap-2">
                      <Settings2 className="w-4 h-4 text-emerald-500" /> API Keys
                      {formData.api_keys.length > 0 && (() => {
                        const cfgEnabled = formData.api_keys.filter(k => !k.disabled).length;
                        const rtCount = runtimeKeyStatus[formData.provider]?.auto_disabled?.length || 0;
                        const eff = Math.max(0, cfgEnabled - rtCount);
                        const issue = formData.api_keys.some(k => k.disabled) || rtCount > 0;
                        return <span className={`text-xs font-normal font-mono px-1.5 py-0.5 rounded ${issue ? 'bg-orange-500/10 text-orange-600 dark:text-orange-400' : 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-500'}`}>{eff}/{formData.api_keys.length}</span>;
                      })()}
                    </span>
                    <div className="flex items-center gap-2 text-xs">
                      <button onClick={copyAllKeys} className="text-muted-foreground hover:text-foreground flex items-center gap-1"><Copy className="w-3 h-3" /> 复制全部</button>
                      <button
                        onClick={() => queryAllBalances()}
                        disabled={balanceLoading}
                        className="text-emerald-600 dark:text-emerald-400 hover:text-emerald-700 dark:hover:text-emerald-300 flex items-center gap-1 disabled:opacity-50 disabled:cursor-not-allowed"
                        title={isOAuthEngine ? '查询所有 OAuth 账号的额度' : '查询所有 Key 的余额'}
                      >
                        <Wallet className={`w-3 h-3 ${balanceLoading ? 'animate-pulse' : ''}`} /> {balanceLoading ? '查询中...' : (() => {
                          if (isOAuthEngine) {
                            // 修改原因：OAuth 渠道的余额汇总文本可能来自渠道专属字段，通用前端不应读取具体字段。
                            // 修改方式：存在 balance_summary 插槽时只传入全部 OAuth 账号作为 context，没有插槽时显示平台默认文本。
                            // 目的：让各渠道自行汇总余额，Channels.tsx 只保留通用按钮挂载点。
                            return hasUiSlot(formData.engine, 'balance_summary', formData.preferences.enabled_plugins)
                              ? <UiSlot engine={formData.engine} slot="balance_summary" data={null} context={{ accounts: oauthAccounts }} className="inline" fallbackText="余额" enabledPlugins={formData.preferences.enabled_plugins || []} />
                              : '余额';
                          } else {
                            const vals = Object.values(balanceResults).filter((b: any) => b?.supported && !b?.error);
                            if (vals.length > 0) {
                              const hasAmount = vals.some((b: any) => b.available != null);
                              if (!hasAmount) {
                                const pcts = vals.map((b: any) => getBalancePercent(b)).filter((p): p is number => p != null);
                                if (pcts.length > 0) { const avg = pcts.reduce((s, p) => s + p, 0) / pcts.length; return <span title={`${pcts.length} 个 Key 平均`}>余额 <span className="font-mono">{avg.toFixed(0)}%</span></span>; }
                              } else {
                                const total = vals.reduce((s: number, b: any) => s + (b.available ?? 0), 0);
                                return <span title={`${vals.length} 个 Key 合计`}>余额 <span className="font-mono">{total.toFixed(2)}</span></span>;
                              }
                            }
                          }
                          return '余额';
                        })()}
                      </button>
                      <button
                        onClick={() => openKeyTestDialog(null)}
                        disabled={formData.api_keys.length === 0}
                        className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 flex items-center gap-1 disabled:opacity-50 disabled:cursor-not-allowed"
                        title="测试该渠道中的全部 Key（可选自动禁用失效 Key）"
                      >
                        <Play className="w-3 h-3" /> 多key测试
                      </button>
                      <button
                        onClick={clearAllKeys}
                        disabled={formData.api_keys.length === 0}
                        className="text-red-600 dark:text-red-500 hover:text-red-700 dark:hover:text-red-400 flex items-center gap-1 disabled:opacity-50 disabled:cursor-not-allowed"
                        title="一键清空该渠道的全部密钥"
                      >
                        <Trash2 className="w-3 h-3" /> 清空
                      </button>
                      <button onClick={addEmptyKey} className="text-primary hover:text-primary/80 flex items-center gap-1"><Plus className="w-3 h-3" /> 添加密钥</button>
                      {formData.api_keys.length >= 12 && (
                        <button
                          onClick={() => { setForceListMode(prev => !prev); setFocusedKeyIdx(null); }}
                          className="text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
                          title={forceListMode ? '切换到机房模式' : '切换到完整行模式'}
                        >
                          {forceListMode ? <LayoutGrid className="w-3.5 h-3.5" /> : <LayoutList className="w-3.5 h-3.5" />}
                        </button>
                      )}
                    </div>
                  </div>
                  {hasUiSlot(formData.engine, 'key_hint', formData.preferences.enabled_plugins) && (
                    <>
                      {/* 修改原因：Key 列表附近的充值或使用提示属于渠道专属信息，通用前端不能硬编码具体链接或说明。 */}
                      {/* 修改方式：在 Key 列表标题下方提供 key_hint 挂载点，仅当当前 engine 注册该插槽时渲染 UiSlot。 */}
                      {/* 目的：让渠道自行写入 Key 区域提示，未注册时不显示任何额外 DOM 或空白。 */}
                      <UiSlot engine={formData.engine} slot="key_hint" data={null} element="div" className="text-xs text-muted-foreground" enabledPlugins={formData.preferences.enabled_plugins || []} />
                    </>
                  )}
                  <div data-key-scroll className="space-y-2 max-h-64 overflow-y-auto pr-1" onClick={e => { if (e.target === e.currentTarget) setFocusedKeyIdx(null); }}>
                    {/* 修改原因：当 Key 数量达到 10 个时，完整行模式会让编辑抽屉过长且难以快速浏览状态。
                        修改方式：在原滚动容器内按数量阈值切换 RackGrid/RackCard；未达到阈值时把原完整行 map 原样保留在 else 分支。
                        目的：让机房模式和完整行模式共用同一份数据、滚动区域与操作回调，同时避免改动现有完整行渲染。 */}
                    {formData.api_keys.length >= 12 && !forceListMode ? (
                      <RackGrid onClick={e => { if (e.target === e.currentTarget) setFocusedKeyIdx(null); }}>
                        {formData.api_keys.map((keyObj, idx) => {
                          if (focusedKeyIdx === idx) {
                            return (
                              <div key={`full-${idx}`} className="w-full basis-full">
                                {/* 修改原因：机房模式中被选中的卡片需要展开为原完整行，才能编辑完整 Key、备注和全部操作。
                                    修改方式：在 flex-wrap 网格中用 w-full basis-full 包裹共用完整行渲染，让展开项独占一整行。
                                    目的：其他未选中卡片继续保持紧凑排列，选中项上下自然换行。 */}
                                {renderFullKeyRow(keyObj, idx, { showDecorationsWhileFocused: true })}
                              </div>
                            );
                          }

                          return (
                            <RackCard
                              key={idx}
                              idx={idx}
                              keyObj={keyObj}
                              providerName={formData.provider}
                              engine={formData.engine}
                              enabledPlugins={formData.preferences.enabled_plugins || []}
                              runtimeKeyStatus={runtimeKeyStatus}
                              localCountdowns={localCountdowns}
                              balanceResults={balanceResults}
                              oauthAccounts={oauthAccounts}
                              isOAuthEngine={isOAuthEngine}
                              onFocus={() => setFocusedKeyIdx(idx)}
                              onImport={() => openImportModal(idx)}
                              onLogin={() => startOAuthLogin(idx)}
                            />
                          );
                        })}
                      </RackGrid>
                    ) : (
                      <>
                        {formData.api_keys.map((keyObj, idx) => renderFullKeyRow(keyObj, idx))}
                      </>
                    )}
                    {formData.api_keys.length === 0 && <div className="text-center p-4 text-sm text-muted-foreground italic">暂无密钥</div>}
                  </div>
                  {isOAuthEngine && (
                    <div className="mt-2 flex justify-end">
                      {/* 修改原因：OAuth 凭据需要一个管理员显式导出入口，用于迁移或备份当前渠道。 */}
                      {/* 修改方式：在 OAuth Key 列表底部增加小按钮，点击后下载 /v1/oauth/export 返回的 JSON。 */}
                      {/* 目的：避免在普通账号列表中暴露 refresh_token，同时保留受控导出能力。 */}
                      <button
                        type="button"
                        onClick={exportOAuthCredentials}
                        className="text-xs text-amber-600 dark:text-amber-400 hover:text-amber-700 dark:hover:text-amber-300 flex items-center gap-1"
                      >
                        <Download className="w-3 h-3" /> 导出全部凭证
                      </button>
                    </div>
                  )}
                </section>}

                {/* 3. 模型配置 */}
                <section>
                  <div className="flex items-center gap-2 text-sm font-semibold text-foreground mb-4 border-b border-border pb-2">
                    <Brain className="w-4 h-4 text-purple-500" /> 模型配置
                  </div>
                  <div className="mb-6">
                    <div className="flex flex-wrap justify-between items-center gap-2 mb-1.5">
                      <span className="text-sm font-medium text-foreground">支持的模型列表 ({formData.models.length})</span>
                      <div className="flex gap-2">
                        <button onClick={copyAllModels} disabled={formData.models.length === 0} className="text-xs bg-muted text-foreground px-2 py-1 rounded flex items-center gap-1 hover:bg-muted/80 disabled:opacity-50">
                          {copiedModels ? <CopyCheck className="w-3 h-3 text-emerald-500" /> : <Copy className="w-3 h-3" />}
                          {copiedModels ? '已复制' : '复制'}
                        </button>
                        <button onClick={() => updateFormData('models', [])} className="text-xs bg-red-500/10 text-red-600 dark:text-red-500 px-2 py-1 rounded">清空</button>
                        <button onClick={openFetchModelsDialog} disabled={fetchingModels} className="text-xs bg-primary/10 text-primary px-2 py-1 rounded flex items-center gap-1">
                          <RefreshCw className={`w-3 h-3 ${fetchingModels ? 'animate-spin' : ''}`} /> 获取
                        </button>
                      </div>
                    </div>
                    <div className="bg-muted/50 border border-border rounded-lg p-2 min-h-[100px]">
                      <div className="flex flex-wrap gap-2 mb-2 max-h-[200px] overflow-y-auto pr-1">
                        {formData.models.map((model, idx) => {
                          const displayName = getModelDisplayName(model);
                          const hasAlias = displayName !== model;
                          return (
                            <span
                              key={`${idx}-${modelDisplayKey}`}
                              className="group bg-background border border-border text-foreground text-xs font-mono px-2 py-1 rounded flex items-center gap-1.5 cursor-pointer hover:bg-muted transition-colors"
                              onClick={() => { navigator.clipboard.writeText(displayName); }}
                              title={hasAlias ? `点击复制: ${displayName} (原名: ${model})` : "点击复制模型名"}
                            >
                              <span className="truncate max-w-[120px] sm:max-w-none">{displayName}</span>
                              {hasAlias && <span className="text-muted-foreground text-[10px] hidden sm:inline">({model})</span>}
                              <button onClick={(e) => { e.stopPropagation(); updateFormData('models', formData.models.filter(m => m !== model)); }} className="text-muted-foreground hover:text-red-500"><X className="w-3 h-3" /></button>
                            </span>
                          );
                        })}
                      </div>
                      <input type="text" value={modelInput} onChange={e => setModelInput(e.target.value)} onKeyDown={handleModelInputKeyDown} placeholder="输入模型名并按回车..." className="w-full bg-transparent border-t border-border pt-2 px-1 text-sm font-mono outline-none text-foreground" />
                    </div>
                  </div>
                </section>

                {/* 4. 模型重定向 */}
                <section>
                  <div className="flex items-center gap-2 text-sm font-semibold text-foreground mb-4 border-b border-border pb-2">
                    <ArrowRight className="w-4 h-4 text-blue-400" /> 模型重定向
                  </div>
                  <div className="flex justify-end mb-3">
                    <button onClick={() => updateFormData('mappings', [...formData.mappings, { from: '', to: '' }])} className="text-xs border border-border text-foreground px-2 py-1 rounded">+ 添加映射</button>
                  </div>
                  <div className="space-y-2">
                    {formData.mappings.length === 0 ? (
                      <div className="text-sm text-muted-foreground italic p-4 text-center border border-dashed border-border rounded-lg">暂无映射</div>
                    ) : (
                      formData.mappings.map((m, idx) => (
                        <div key={idx} className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 bg-muted/50 p-2 rounded-lg border border-border">
                          <input value={m.from} onChange={e => handleMappingChange(idx, 'from', e.target.value)} placeholder="请求模型 (Alias)" className="flex-1 bg-background border border-border px-2 py-1.5 rounded text-xs font-mono text-foreground" />
                          <ArrowRight className="w-4 h-4 text-muted-foreground hidden sm:block" />
                          <input value={m.to} onChange={e => handleMappingChange(idx, 'to', e.target.value)} placeholder="真实模型 (Upstream)" className="flex-1 bg-background border border-border px-2 py-1.5 rounded text-xs font-mono text-foreground" />
                          <button onClick={() => { updateFormData('mappings', formData.mappings.filter((_, i) => i !== idx)); setModelDisplayKey(prev => prev + 1); }} className="text-red-500 p-1 self-end sm:self-auto"><Trash2 className="w-4 h-4" /></button>
                        </div>
                      ))
                    )}
                  </div>
                </section>

                {/* 4.5 子渠道 (子渠道模式隐藏) */}
                {!editingSubChannel && <section>
                  <div className="flex items-center justify-between text-sm font-semibold text-foreground mb-4 border-b border-border pb-2">
                    <span className="flex items-center gap-2">
                      <Server className="w-4 h-4 text-cyan-500" /> 子渠道
                      {formData.sub_channels.length > 0 && (
                        <span className="text-xs font-normal font-mono px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-600 dark:text-cyan-400">{formData.sub_channels.length}</span>
                      )}
                    </span>
                    <button
                      onClick={() => updateFormData('sub_channels', [...formData.sub_channels, { engine: '', models: [], mappings: [], preferences: {}, _collapsed: false }])}
                      className="text-xs text-primary hover:text-primary/80 flex items-center gap-1"
                    >
                      <Plus className="w-3 h-3" /> 添加子渠道
                    </button>
                  </div>
                  <p className="text-xs text-muted-foreground mb-3">子渠道继承主渠道的 API Key、Base URL 等配置，可单独指定引擎和模型。适用于同一 Key 支持多种 API 格式的场景。</p>
                  <div className="space-y-3">
                    {formData.sub_channels.length === 0 ? (
                      <div className="text-sm text-muted-foreground italic p-4 text-center border border-dashed border-border rounded-lg">暂无子渠道</div>
                    ) : (
                      formData.sub_channels.map((sub, subIdx) => (
                        <div key={subIdx} className="border border-border rounded-lg overflow-hidden">
                          {/* 子渠道头部 */}
                          <div
                            className="flex items-center justify-between px-3 py-2 bg-muted/50 cursor-pointer hover:bg-muted/70 transition-colors"
                            onClick={() => {
                              const next = [...formData.sub_channels];
                              next[subIdx] = { ...next[subIdx], _collapsed: !next[subIdx]._collapsed };
                              updateFormData('sub_channels', next);
                            }}
                          >
                            <div className="flex items-center gap-2 text-sm">
                              <span className="text-muted-foreground">{sub._collapsed ? '▶' : '▼'}</span>
                              <span className="font-medium text-foreground">{sub.engine || '未选择引擎'}</span>
                              <span className="text-xs text-muted-foreground">({sub.models.length} 模型)</span>
                              {sub.enabled === false && <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-500">已禁用</span>}
                            </div>
                            <div className="flex items-center gap-1">
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  openKeyTestDialog(null, {
                                    engine: sub.engine || formData.engine || 'openai',
                                    base_url: sub.base_url || formData.base_url || '',
                                    models: sub.models || [],
                                    title: `测试 API Keys: ${formData.provider}:${sub.engine || 'sub'}`,
                                  });
                                }}
                                disabled={formData.api_keys.length === 0 || !sub.engine}
                                className="text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 p-1 rounded-md transition-colors disabled:opacity-30"
                                title="用子渠道引擎测试全部 Key"
                              >
                                <Play className="w-4 h-4" />
                              </button>
                              {originalIndex !== null && (
                                <button
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setIsModalOpen(false);
                                    setTimeout(() => openSubChannelEdit(originalIndex, subIdx), 150);
                                  }}
                                  className="text-primary hover:text-primary/80 p-1"
                                  title="完整编辑"
                                >
                                  <Edit className="w-4 h-4" />
                                </button>
                              )}
                              <button
                                onClick={(e) => { e.stopPropagation(); updateFormData('sub_channels', formData.sub_channels.filter((_, i) => i !== subIdx)); }}
                                className="text-red-500 hover:text-red-400 p-1"
                                title="删除子渠道"
                              >
                                <Trash2 className="w-4 h-4" />
                              </button>
                            </div>
                          </div>

                          {/* 子渠道展开内容 */}
                          {!sub._collapsed && (
                            <div className="p-3 space-y-3 border-t border-border">
                              {/* 引擎选择 */}
                              <div>
                                <label className="text-xs font-medium text-foreground mb-1 block">引擎 (Engine)</label>
                                <select
                                  value={sub.engine}
                                  onChange={e => {
                                    const next = [...formData.sub_channels];
                                    next[subIdx] = { ...next[subIdx], engine: e.target.value };
                                    updateFormData('sub_channels', next);
                                  }}
                                  className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs text-foreground"
                                >
                                  <option value="">选择引擎</option>
                                  {(() => {
                            const sort = (a: ChannelOption, b: ChannelOption) => {
                              if (a.id === 'openai') return -1;
                              if (b.id === 'openai') return 1;
                              return (a.description || a.id).localeCompare(b.description || b.id);
                            };
                            const builtIn = channelTypes.filter(c => !c.is_oauth && c.source !== 'plugin').sort(sort);
                            const oauth = channelTypes.filter(c => c.is_oauth && c.source !== 'plugin').sort(sort);
                            const plugin = channelTypes.filter(c => c.source === 'plugin').sort(sort);
                            return (<>
                              <optgroup label="内置通用">{builtIn.map(c => <option key={c.id} value={c.id}>{c.description || c.id}</option>)}</optgroup>
                              {oauth.length > 0 && <optgroup label="内置 OAuth">{oauth.map(c => <option key={c.id} value={c.id}>{c.description || c.id}</option>)}</optgroup>}
                              {plugin.length > 0 && <optgroup label="插件渠道">{plugin.map(c => <option key={c.id} value={c.id}>{c.description || c.id}</option>)}</optgroup>}
                            </>);
                          })()}
                                </select>
                              </div>

                              {/* 模型列表 */}
                              <div>
                                <div className="flex items-center justify-between mb-1">
                                  <label className="text-xs font-medium text-foreground">模型列表 ({sub.models.length})</label>
                                  <button
                                    onClick={async () => {
                                      const firstKey = formData.api_keys.find(k => k.key.trim() && !k.disabled);
                                      const baseUrl = sub.base_url || formData.base_url;
                                      if (!baseUrl || !firstKey) { toastError('需要 Base URL 和至少一个启用的 API Key'); return; }
                                      try {
                                        const res = await apiFetch('/v1/channels/fetch_models', {
                                          method: 'POST',
                                          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                                          body: JSON.stringify({ engine: sub.engine, base_url: baseUrl, api_key: firstKey.key, preferences: sub.preferences }),
                                        });
                                        if (!res.ok) { const err = await res.json().catch(() => ({})); toastError(`获取失败: ${fmtErr(err, res.status)}`); return; }
                                        const data = (await res.json()) as any;
                                        const rawModels: unknown[] = Array.isArray(data) ? data : Array.isArray(data?.models) ? data.models : Array.isArray(data?.data) ? data.data.map((m: any) => m?.id) : [];
                                        const models = rawModels.map(m => String(m)).filter(Boolean);
                                        if (models.length === 0) { toastError('未获取到任何模型'); return; }
                                        const next = [...formData.sub_channels];
                                        next[subIdx] = { ...next[subIdx], models: Array.from(new Set([...sub.models, ...models])) };
                                        updateFormData('sub_channels', next);
                                      } catch (err: any) { toastError(`获取失败: ${err?.message || (typeof err === 'object' ? JSON.stringify(err) : String(err))}`); }
                                    }}
                                    disabled={!sub.engine}
                                    className="text-[10px] bg-primary/10 text-primary px-1.5 py-0.5 rounded flex items-center gap-1 disabled:opacity-50"
                                  >
                                    <RefreshCw className="w-3 h-3" /> 获取
                                  </button>
                                </div>
                                <div className="bg-muted/50 border border-border rounded-lg p-2">
                                  <div className="flex flex-wrap gap-1.5 mb-2 max-h-[100px] overflow-y-auto">
                                    {sub.models.map((model, mIdx) => (
                                      <span key={mIdx} className="bg-background border border-border text-foreground text-xs font-mono px-1.5 py-0.5 rounded flex items-center gap-1">
                                        {model}
                                        <button onClick={() => {
                                          const next = [...formData.sub_channels];
                                          next[subIdx] = { ...next[subIdx], models: sub.models.filter((_, i) => i !== mIdx) };
                                          updateFormData('sub_channels', next);
                                        }} className="text-muted-foreground hover:text-red-500"><X className="w-3 h-3" /></button>
                                      </span>
                                    ))}
                                  </div>
                                  <input
                                    type="text"
                                    placeholder="输入模型名并按回车..."
                                    className="w-full bg-transparent border-t border-border pt-1.5 px-1 text-xs font-mono outline-none text-foreground"
                                    onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
                                      if (e.key === 'Enter') {
                                        e.preventDefault();
                                        const val = (e.target as HTMLInputElement).value.trim();
                                        if (val && !sub.models.includes(val)) {
                                          const next = [...formData.sub_channels];
                                          next[subIdx] = { ...next[subIdx], models: [...sub.models, val] };
                                          updateFormData('sub_channels', next);
                                          (e.target as HTMLInputElement).value = '';
                                        }
                                      }
                                    }}
                                  />
                                </div>
                              </div>

                              {/* 模型重定向 */}
                              <div>
                                <div className="flex items-center justify-between mb-1">
                                  <label className="text-xs font-medium text-foreground">模型重定向</label>
                                  <button onClick={() => {
                                    const next = [...formData.sub_channels];
                                    next[subIdx] = { ...next[subIdx], mappings: [...sub.mappings, { from: '', to: '' }] };
                                    updateFormData('sub_channels', next);
                                  }} className="text-[10px] border border-border text-foreground px-1.5 py-0.5 rounded">+ 映射</button>
                                </div>
                                {sub.mappings.length > 0 && (
                                  <div className="space-y-1.5">
                                    {sub.mappings.map((m, mIdx) => (
                                      <div key={mIdx} className="flex items-center gap-1.5">
                                        <input value={m.from} onChange={e => {
                                          const next = [...formData.sub_channels];
                                          const newMappings = [...sub.mappings];
                                          newMappings[mIdx] = { ...newMappings[mIdx], from: e.target.value };
                                          next[subIdx] = { ...next[subIdx], mappings: newMappings };
                                          updateFormData('sub_channels', next);
                                        }} placeholder="Alias" className="flex-1 bg-background border border-border px-2 py-1 rounded text-xs font-mono text-foreground" />
                                        <ArrowRight className="w-3 h-3 text-muted-foreground flex-shrink-0" />
                                        <input value={m.to} onChange={e => {
                                          const next = [...formData.sub_channels];
                                          const newMappings = [...sub.mappings];
                                          newMappings[mIdx] = { ...newMappings[mIdx], to: e.target.value };
                                          next[subIdx] = { ...next[subIdx], mappings: newMappings };
                                          updateFormData('sub_channels', next);
                                        }} placeholder="Upstream" className="flex-1 bg-background border border-border px-2 py-1 rounded text-xs font-mono text-foreground" />
                                        <button onClick={() => {
                                          const next = [...formData.sub_channels];
                                          next[subIdx] = { ...next[subIdx], mappings: sub.mappings.filter((_, i) => i !== mIdx) };
                                          updateFormData('sub_channels', next);
                                        }} className="text-red-500 p-0.5"><X className="w-3 h-3" /></button>
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </div>

                              {/* 覆盖配置 */}
                              <div className="grid grid-cols-2 gap-2">
                                <div>
                                  <label className="text-xs font-medium text-foreground mb-1 block">Base URL 覆盖</label>
                                  <input
                                    type="text" value={sub.base_url || ''}
                                    onChange={e => {
                                      const next = [...formData.sub_channels];
                                      next[subIdx] = { ...next[subIdx], base_url: e.target.value };
                                      updateFormData('sub_channels', next);
                                    }}
                                    placeholder={`留空继承: ${formData.base_url || '(未设置)'}`}
                                    className="w-full bg-background border border-border px-2 py-1.5 rounded text-xs font-mono text-foreground"
                                  />
                                </div>
                                <div>
                                  <label className="text-xs font-medium text-foreground mb-1 block">模型前缀覆盖</label>
                                  <input
                                    type="text" value={sub.model_prefix || ''}
                                    onChange={e => {
                                      const next = [...formData.sub_channels];
                                      next[subIdx] = { ...next[subIdx], model_prefix: e.target.value };
                                      updateFormData('sub_channels', next);
                                    }}
                                    placeholder={`留空继承: ${formData.model_prefix || '(无)'}`}
                                    className="w-full bg-background border border-border px-2 py-1.5 rounded text-xs font-mono text-foreground"
                                  />
                                </div>
                              </div>

                              {/* 插件配置 */}
                              <div>
                                <div className="flex items-center justify-between mb-1">
                                  <label className="text-xs font-medium text-foreground flex items-center gap-1">
                                    <Puzzle className="w-3 h-3 text-emerald-500" /> 插件
                                    <span className="text-[10px] text-muted-foreground font-normal">(留空继承主渠道)</span>
                                  </label>
                                </div>
                                <div className="bg-muted/50 border border-border rounded-lg p-2">
                                  <div className="flex flex-wrap gap-1.5 mb-1.5">
                                    {(sub.preferences.enabled_plugins as string[] || []).length === 0 ? (
                                      <span className="text-[10px] text-muted-foreground italic">继承主渠道 ({(formData.preferences.enabled_plugins || []).length} 个插件)</span>
                                    ) : (
                                      (sub.preferences.enabled_plugins as string[]).map((p: string, pIdx: number) => (
                                        <span key={pIdx} className="bg-emerald-500/10 border border-emerald-500/20 text-emerald-600 dark:text-emerald-500 px-1.5 py-0.5 rounded text-[10px] font-mono flex items-center gap-1">
                                          {p}
                                          <button onClick={() => {
                                            const next = [...formData.sub_channels];
                                            const plugins = [...(sub.preferences.enabled_plugins || [])];
                                            plugins.splice(pIdx, 1);
                                            next[subIdx] = { ...next[subIdx], preferences: { ...sub.preferences, enabled_plugins: plugins.length > 0 ? plugins : undefined } };
                                            updateFormData('sub_channels', next);
                                          }} className="text-emerald-500 hover:text-red-500"><X className="w-2.5 h-2.5" /></button>
                                        </span>
                                      ))
                                    )}
                                  </div>
                                  <input
                                    type="text"
                                    placeholder="输入插件名按回车 (如 oai_tools)"
                                    className="w-full bg-transparent border-t border-border pt-1 px-1 text-[10px] font-mono outline-none text-foreground"
                                    onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
                                      if (e.key === 'Enter') {
                                        e.preventDefault();
                                        const val = (e.target as HTMLInputElement).value.trim();
                                        if (val) {
                                          const next = [...formData.sub_channels];
                                          const plugins = [...(sub.preferences.enabled_plugins || []), val];
                                          next[subIdx] = { ...next[subIdx], preferences: { ...sub.preferences, enabled_plugins: plugins } };
                                          updateFormData('sub_channels', next);
                                          (e.target as HTMLInputElement).value = '';
                                        }
                                      }
                                    }}
                                  />
                                </div>
                              </div>

                              {/* 请求体覆写 & 权重 */}
                              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                                <div>
                                  <label className="text-xs font-medium text-foreground mb-1 block">请求体覆写 (JSON)</label>
                                  <textarea
                                    value={sub.preferences.post_body_parameter_overrides ? JSON.stringify(sub.preferences.post_body_parameter_overrides, null, 2) : ''}
                                    onChange={e => {
                                      const next = [...formData.sub_channels];
                                      let val: any = undefined;
                                      try { if (e.target.value.trim()) val = JSON.parse(e.target.value); } catch { val = e.target.value; }
                                      next[subIdx] = { ...next[subIdx], preferences: { ...sub.preferences, post_body_parameter_overrides: val || undefined } };
                                      updateFormData('sub_channels', next);
                                    }}
                                    rows={2}
                                    placeholder={`留空继承主渠道`}
                                    className="w-full bg-background border border-border px-2 py-1.5 rounded text-[10px] font-mono text-foreground outline-none"
                                  />
                                </div>
                                <div>
                                  <label className="text-xs font-medium text-foreground mb-1 block">权重</label>
                                  <input
                                    type="number"
                                    value={sub.preferences.weight ?? ''}
                                    onChange={e => {
                                      const next = [...formData.sub_channels];
                                      next[subIdx] = { ...next[subIdx], preferences: { ...sub.preferences, weight: e.target.value ? Number(e.target.value) : undefined } };
                                      updateFormData('sub_channels', next);
                                    }}
                                    placeholder={`继承: ${formData.preferences.weight ?? 10}`}
                                    className="w-full bg-background border border-border px-2 py-1.5 rounded text-xs font-mono text-foreground"
                                  />
                                </div>
                              </div>
                            </div>
                          )}
                        </div>
                      ))
                    )}
                  </div>
                </section>}

                {/* 5. 路由与限流 (子渠道模式隐藏) */}
                {!editingSubChannel && <section>
                  <div className="flex items-center gap-2 text-sm font-semibold text-foreground mb-4 border-b border-border pb-2">
                    <Network className="w-4 h-4 text-yellow-500" /> 路由与限流
                  </div>
                  <div className="space-y-4">


                    {/* 权重 + 调度策略 并排 */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className="text-sm font-medium text-foreground mb-1.5 block">渠道权重 (Weight)</label>
                        <input type="number" value={formData.preferences.weight || ''} onChange={e => updatePreference('weight', Number(e.target.value))} className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
                      </div>
                      <div>
                        <label className="text-sm font-medium text-foreground mb-1.5 block">Key 调度策略</label>
                        <select value={formData.preferences.api_key_schedule_algorithm} onChange={e => updatePreference('api_key_schedule_algorithm', e.target.value)} className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground">
                          {SCHEDULE_ALGORITHMS.map(a => <option key={a.value} value={a.value}>{a.label}</option>)}
                        </select>
                      </div>
                    </div>

                    {/* Key 错误处理规则 */}
                    <div className="border-t border-border pt-4">
                      <div className="flex items-center justify-between mb-3">
                        <label className="text-sm font-medium text-foreground flex items-center gap-1.5">
                          <Power className="w-3.5 h-3.5 text-red-500" /> Key 错误处理规则
                        </label>
                        <div className="flex gap-1.5">
                          {[{ label: '标准', rules: [
                            { match: { status: [429] }, duration: 30 },
                            { match: { status: [401, 403] }, duration: -1 },
                            { match: 'default', duration: 60 },
                          ]}, { label: '激进', rules: [
                            { match: { status: [429] }, duration: 10 },
                            { match: { status: [401, 403, 500] }, duration: -1 },
                            { match: 'default', duration: 30 },
                          ]}, { label: '宽松', rules: [
                            { match: { status: [429] }, duration: 60 },
                            { match: { status: [401, 403] }, duration: -1 },
                          ]}].map(tpl => (
                            <button
                              key={tpl.label}
                              type="button"
                              onClick={() => updatePreference('key_rules', tpl.rules)}
                              className="text-[10px] font-medium px-2 py-1 rounded bg-muted hover:bg-muted/80 text-muted-foreground hover:text-foreground transition-colors"
                            >
                              {tpl.label}
                            </button>
                          ))}
                        </div>
                      </div>
                      <p className="text-xs text-muted-foreground mb-3">
                        按顺序匹配，首条命中生效。Key 处理：冷却=暂时停用，永久禁用=需手动恢复。重试：自动=沿用内置逻辑(4xx不重试，5xx/429重试)，换Key=强制用其他Key重试，报错=跳过重试直接返回客户端。
                      </p>

                      {/* 规则列表 */}
                      {/* 修改原因：旧版 Key Rules 使用序号列和两行卡片，导致规则区域过高且条件与动作被割裂。 */}
                      {/* 修改方式：改为桌面端单行、移动端两行的紧凑列表，并用底部分隔线替代卡片背景。 */}
                      {/* 目的：保留原有编辑、重试三态和 remap 折叠功能，同时减少纵向空间占用。 */}
                      <div className="space-y-1">
                        {(formData.preferences.key_rules || []).map((rule: any, idx: number) => {
                          const rules = formData.preferences.key_rules || [];
                          const replaceRule = (r: any) => { const n = [...rules]; n[idx] = r; updatePreference('key_rules', n); };
                          const updateRule = (p: any) => replaceRule({ ...rules[idx], ...p });
                          const clearField = (f: 'remap' | 'retry') => { const r = { ...rules[idx] }; delete r[f]; replaceRule(r); };
                          const removeRule = () => updatePreference('key_rules', rules.filter((_: any, i: number) => i !== idx));
                          const mt = rule.match === 'default' ? 'default' : rule.match?.keyword ? 'keyword' : 'status';
                          const retryMode = getKeyRuleRetryMode(rule);
                          const durationMode = rule.duration === -1 ? '-1' : Number(rule.duration) > 0 ? 'cd' : '0';
                          const controlClass = 'h-6 bg-background border border-border rounded px-1 py-0 text-[11px] leading-none text-foreground';
                          const inputClass = `${controlClass} font-mono`;
                          const retryOptions: [KeyRuleRetryMode, string, string][] = [['default', '自动', '沿用内置重试逻辑'], ['force', '换Key', '强制用其他Key重试'], ['disable', '报错', '跳过重试直接返回']];
                          return (
                            <div key={idx} className="border-b border-border py-1 text-[11px]">
                              <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-1 sm:flex-nowrap">
                                <div className="flex min-w-0 items-center gap-1 sm:shrink-0">
                                  <select value={mt} onChange={e => { const v = e.target.value; if (v === 'default') updateRule({ match: 'default' }); else if (v === 'status') updateRule({ match: { status: [429] } }); else updateRule({ match: { keyword: [''] } }); }} className={`${controlClass} w-[64px] sm:w-[66px]`}>
                                    <option value="status">状态码</option>
                                    <option value="keyword">关键词</option>
                                    <option value="default">default</option>
                                  </select>
                                  {mt === 'status' && <DeferredInput inputMode="numeric" value={formatKeyRuleStatusInput(rule.match?.status)} onCommit={v => updateRule({ match: { status: parseKeyRuleStatusInput(v) } })} placeholder="429" className={`${inputClass} w-[68px]`} />}
                                  {mt === 'keyword' && <DeferredInput value={formatKeyRuleKeywordsInput(rule.match?.keyword)} onCommit={v => updateRule({ match: { keyword: parseKeyRuleKeywordsInput(v) } })} placeholder="quota, rate limit" className={`${inputClass} w-[110px] sm:w-[118px]`} />}
                                  <button type="button" onClick={removeRule} className="ml-auto inline-flex h-6 w-6 items-center justify-center text-red-500/60 hover:text-red-500 sm:hidden" title="删除"><X className="h-3 w-3" /></button>
                                </div>
                                <span className="hidden h-5 w-px bg-border sm:block" aria-hidden="true" />
                                <div className="flex min-w-0 flex-wrap items-center gap-1 sm:flex-1 sm:flex-nowrap">
                                  <select value={durationMode} onChange={e => { const v = e.target.value; if (v === '-1') updateRule({ duration: -1 }); else if (v === '0') updateRule({ duration: 0 }); else updateRule({ duration: Number(rule.duration) > 0 ? Number(rule.duration) : 60 }); }} className={`${controlClass} w-[72px]`}>
                                    <option value="cd">冷却</option>
                                    <option value="-1">永久禁用</option>
                                    <option value="0">不处理</option>
                                  </select>
                                  {Number(rule.duration) > 0 && <><input type="number" min={1} value={rule.duration} onChange={e => updateRule({ duration: Math.max(1, parseInt(e.target.value, 10) || 1) })} className={`${inputClass} w-[46px]`} /><span className="text-[10px] text-muted-foreground">s</span></>}
                                  <div className="inline-flex h-6 overflow-hidden rounded border border-border" title="重试控制">
                                    {retryOptions.map(([v, l, tip]) => (
                                      <button key={v} type="button" title={tip} onClick={() => replaceRule(setKeyRuleRetryMode(rule, v))} className={`px-1.5 text-[10px] leading-none transition-colors ${retryMode === v ? v === 'force' ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 font-semibold' : v === 'disable' ? 'bg-red-500/15 text-red-600 dark:text-red-400 font-semibold' : 'bg-muted text-muted-foreground font-semibold' : 'text-muted-foreground hover:bg-muted/50 hover:text-foreground'}`}>{l}</button>
                                    ))}
                                  </div>
                                  {rule.remap != null ? (<span className="inline-flex h-6 items-center gap-0.5"><span className="text-[10px] text-muted-foreground">↔</span><input type="number" min={100} max={599} value={rule.remap} onChange={e => { const raw = e.target.value.trim(); if (!raw) clearField('remap'); else updateRule({ remap: raw }); }} placeholder="码" title="错误码映射" className={`${inputClass} w-[42px]`} /><button type="button" onClick={() => clearField('remap')} className="inline-flex h-6 w-4 items-center justify-center text-[10px] text-muted-foreground hover:text-foreground" title="移除映射">×</button></span>) : (<button type="button" onClick={() => updateRule({ remap: '' })} className="inline-flex h-6 w-6 items-center justify-center rounded border border-transparent text-[11px] text-muted-foreground hover:border-border hover:text-foreground" title="添加错误码映射">↔</button>)}
                                </div>
                                <button type="button" onClick={removeRule} className="ml-auto hidden h-6 w-6 shrink-0 items-center justify-center text-red-500/60 hover:text-red-500 sm:inline-flex" title="删除"><X className="h-3 w-3" /></button>
                              </div>
                            </div>
                          );
                        })}
                      </div>

                      {/* 添加规则 */}
                      <button
                        type="button"
                        onClick={() => {
                          const rules = formData.preferences.key_rules || [];
                          updatePreference('key_rules', [...rules, { match: { status: [429] }, duration: 30 }]);
                        }}
                        className="text-xs text-primary hover:text-primary/80 flex items-center gap-1 mt-2"
                      >
                        <Plus className="w-3 h-3" /> 添加规则
                      </button>
                    </div>
                  </div>
                </section>}

                {/* 6. 高级设置 */}
                <section>
                  <div className="flex items-center gap-2 text-sm font-semibold text-foreground mb-4 border-b border-border pb-2">
                    <Settings2 className="w-4 h-4 text-muted-foreground" /> 高级设置
                  </div>
                  <div className="space-y-4">
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <label className="text-sm font-medium text-foreground flex items-center gap-1.5"><Puzzle className="w-3.5 h-3.5 text-emerald-500" /> 拦截器插件</label>
                        <span className="text-xs text-muted-foreground hidden sm:inline">格式: plugin_name[:config]</span>
                      </div>
                      <div className="bg-muted/50 border border-border rounded-lg p-3">
                        <div className="flex flex-wrap gap-2 mb-3">
                          {(!formData.preferences.enabled_plugins || formData.preferences.enabled_plugins.length === 0) ? (
                            <span className="text-sm text-muted-foreground italic">未启用任何插件</span>
                          ) : (
                            (formData.preferences.enabled_plugins as string[]).map((p: string, idx: number) => {
                              const [name, opts] = p.split(':');
                              return (
                                <span key={idx} className="bg-emerald-500/10 border border-emerald-500/20 text-emerald-600 dark:text-emerald-500 px-2 py-1 rounded text-xs font-mono flex items-center gap-1">
                                  <Puzzle className="w-3 h-3" />
                                  {name} {opts && <span className="opacity-60">({opts})</span>}
                                </span>
                              );
                            })
                          )}
                        </div>
                        <button onClick={() => setShowPluginSheet(true)} className="text-xs bg-muted text-foreground hover:bg-muted/80 px-3 py-1.5 rounded-md flex items-center gap-1.5 transition-colors">
                          <Settings2 className="w-3 h-3" /> 配置插件 ({formData.preferences.enabled_plugins?.length || 0})
                        </button>
                      </div>
                    </div>

                    <div className="flex gap-3 items-end">
                      <div className="flex-1 min-w-0">
                        <label className="text-sm font-medium text-foreground mb-1.5 block">代理 (Proxy)</label>
                        <input type="url" value={formData.preferences.proxy || ''} onChange={e => updatePreference('proxy', e.target.value)} placeholder="http://127.0.0.1:7890" className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
                      </div>
                      {/* 流式模式三段式 switch — 紧凑版 */}
                      <div className="shrink-0">
                        <label className="text-sm font-medium text-foreground mb-1.5 block">流式</label>
                        <div className="flex items-center gap-0.5 bg-muted rounded-lg p-0.5" title={
                          (formData.preferences.stream_mode || 'auto') === 'auto'
                            ? '跟随客户端请求'
                            : formData.preferences.stream_mode === 'force_stream'
                            ? '非流式→内部走流式打上游→拼装返回'
                            : '流式→内部走非流打上游→拆SSE返回'
                        }>
                          {[
                            { value: 'force_non_stream', label: '非流', tip: '强制非流：流式请求→非流打上游→拆SSE返回' },
                            { value: 'auto', label: '自动', tip: '跟随客户端请求' },
                            { value: 'force_stream', label: '强流', tip: '强制流：非流请求→流式打上游→拼装返回' },
                          ].map(opt => {
                            const current = formData.preferences.stream_mode || 'auto';
                            const isActive = current === opt.value;
                            return (
                              <button
                                key={opt.value}
                                title={opt.tip}
                                onClick={() => updatePreference('stream_mode', opt.value)}
                                className={`px-2.5 py-1.5 rounded-md text-[11px] font-medium transition-all ${
                                  isActive
                                    ? opt.value === 'force_stream'
                                      ? 'bg-blue-500/20 text-blue-400 border border-blue-500/30'
                                      : opt.value === 'force_non_stream'
                                      ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                                      : 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                                    : 'text-muted-foreground hover:text-foreground hover:bg-muted-foreground/10 border border-transparent'
                                }`}
                              >
                                {opt.label}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">系统提示词 (System Prompt)</label>
                      <textarea value={formData.preferences.system_prompt || ''} onChange={e => updatePreference('system_prompt', e.target.value)} rows={3} className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
                    </div>
                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">自定义请求头</label>
                      <div className="space-y-2">
                        {headerEntries.map((entry, idx) => (
                          <div key={idx} className="flex gap-2 items-center">
                            <input
                              value={entry.key}
                              onChange={e => {
                                const next = [...headerEntries];
                                next[idx] = { ...next[idx], key: e.target.value };
                                setHeaderEntries(next);
                              }}
                              placeholder="Header-Name"
                              className="flex-1 bg-background border border-border px-3 py-1.5 rounded-lg text-sm font-mono text-foreground"
                            />
                            <input
                              value={entry.value}
                              onChange={e => {
                                const next = [...headerEntries];
                                next[idx] = { ...next[idx], value: e.target.value };
                                setHeaderEntries(next);
                              }}
                              placeholder="Value"
                              className="flex-1 bg-background border border-border px-3 py-1.5 rounded-lg text-sm font-mono text-foreground"
                            />
                            <button onClick={() => setHeaderEntries(headerEntries.filter((_, i) => i !== idx))} className="text-muted-foreground hover:text-destructive transition-colors">
                              <X className="w-4 h-4" />
                            </button>
                          </div>
                        ))}
                        <button onClick={() => setHeaderEntries([...headerEntries, { key: '', value: '' }])} className="text-xs text-primary hover:text-primary/80 flex items-center gap-1">
                          <Plus className="w-3 h-3" /> 添加请求头
                        </button>
                      </div>
                      <p className="text-xs text-muted-foreground mt-1">支持同名 Header，每条单独发送</p>
                    </div>
                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">请求体覆写 (JSON)</label>
                      <textarea
                        value={overridesJson}
                        onChange={e => setOverridesJson(e.target.value)}
                        onBlur={() => formatJsonOnBlur(overridesJson, setOverridesJson, '请求体覆写')}
                        rows={3}
                        placeholder='{"all": {"temperature": 0.1}}'
                        className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm font-mono focus:border-primary outline-none text-foreground"
                      />
                      <p className="text-xs text-muted-foreground mt-1">
                        失焦时自动格式化。key 为 <code className="px-1 py-0.5 bg-muted rounded text-[11px]">all</code> 或 <code className="px-1 py-0.5 bg-muted rounded text-[11px]">*</code> 全局生效，模型名精确匹配。dict 递归合并，数组整体覆写，key 加 <code className="px-1 py-0.5 bg-muted rounded text-[11px]">+</code> 前缀追加数组（如 <code className="px-1 py-0.5 bg-muted rounded text-[11px]">+tools</code>）。值为 <code className="px-1 py-0.5 bg-muted rounded text-[11px]">null</code> 删除该字段。
                      </p>
                      {hasUiSlot(formData.engine, 'override_hint', formData.preferences.enabled_plugins) && (
                        <>
                          {/* 修改原因：请求体覆写格式可能因渠道协议不同而不同，通用前端不能写死 Antigravity 等渠道的专属提醒。 */}
                          {/* 修改方式：在请求体覆写说明文字下方提供 override_hint 挂载点，仅当当前 engine 注册该插槽时渲染 UiSlot。 */}
                          {/* 目的：让渠道自行写入参数覆写提示，未注册时不显示任何额外 DOM 或空白。 */}
                          <UiSlot engine={formData.engine} slot="override_hint" data={null} element="div" className="text-xs text-amber-600 dark:text-amber-400 mt-1" enabledPlugins={formData.preferences.enabled_plugins || []} />
                        </>
                      )}
                    </div>

                    <div className="flex items-center justify-between p-3 bg-muted/50 rounded-lg border border-border">
                      <span className="text-sm text-foreground">启用 Tools (函数调用)</span>
                      <Switch.Root checked={formData.preferences.tools} onCheckedChange={val => updatePreference('tools', val)} className="w-11 h-6 bg-muted rounded-full data-[state=checked]:bg-primary">
                        <Switch.Thumb className="block w-5 h-5 bg-white rounded-full transition-transform data-[state=checked]:translate-x-[22px]" />
                      </Switch.Root>
                    </div>

                    {/* 模型价格（渠道级） */}
                    <div className="border-t border-border pt-4">
                      <div className="flex items-center justify-between mb-3">
                        <label className="text-sm font-medium text-foreground flex items-center gap-1.5">
                          <Wallet className="w-3.5 h-3.5 text-amber-500" /> 模型价格
                        </label>
                        <button
                          onClick={() => {
                            const mp = { ...(formData.preferences.model_price || {}) };
                            const entries = Object.entries(mp);
                            entries.push(['', '']);
                            updatePreference('model_price', Object.fromEntries(entries));
                          }}
                          className="text-xs text-primary hover:text-primary/80 flex items-center gap-1"
                        >
                          <Plus className="w-3 h-3" /> 添加
                        </button>
                      </div>
                      <p className="text-xs text-muted-foreground mb-3">渠道级价格优先于全局配置。未配置的模型回退到全局价格；全局也未配置则不计费。</p>
                      {Object.keys(formData.preferences.model_price || {}).length > 0 && (
                        <div className="space-y-2">
                          <div className="grid grid-cols-[1fr_4.5rem_4.5rem_1.5rem] gap-1.5 text-[10px] text-muted-foreground font-medium px-0.5">
                            <span>模型名 / 前缀</span>
                            <span className="text-center">输入$/M</span>
                            <span className="text-center">输出$/M</span>
                            <span></span>
                          </div>
                          {Object.entries(formData.preferences.model_price || {}).map(([prefix, priceStr], idx) => {
                            const parts = String(priceStr || '').split(',').map(s => s.trim());
                            const inputPrice = parts[0] || '';
                            const outputPrice = parts[1] || '';
                            // 检查全局是否有同名价格
                            const globalEntry = globalModelPrice[prefix];
                            return (
                              <div key={idx}>
                                <div className="grid grid-cols-[1fr_4.5rem_4.5rem_1.5rem] gap-1.5 items-center">
                                  <input
                                    type="text"
                                    value={prefix}
                                    onChange={e => {
                                      const entries = Object.entries(formData.preferences.model_price || {});
                                      entries[idx] = [e.target.value, entries[idx][1]];
                                      updatePreference('model_price', Object.fromEntries(entries));
                                    }}
                                    placeholder="gpt-4o / default"
                                    className="bg-background border border-border px-2 py-1 rounded text-xs font-mono text-foreground focus:border-primary outline-none"
                                  />
                                  <input
                                    type="text"
                                    value={inputPrice}
                                    onChange={e => {
                                      const entries = Object.entries(formData.preferences.model_price || {});
                                      entries[idx] = [prefix, `${e.target.value},${outputPrice}`];
                                      updatePreference('model_price', Object.fromEntries(entries));
                                    }}
                                    placeholder="0.3"
                                    className="bg-background border border-border px-1.5 py-1 rounded text-xs font-mono text-center text-foreground focus:border-primary outline-none"
                                  />
                                  <input
                                    type="text"
                                    value={outputPrice}
                                    onChange={e => {
                                      const entries = Object.entries(formData.preferences.model_price || {});
                                      entries[idx] = [prefix, `${inputPrice},${e.target.value}`];
                                      updatePreference('model_price', Object.fromEntries(entries));
                                    }}
                                    placeholder="1.0"
                                    className="bg-background border border-border px-1.5 py-1 rounded text-xs font-mono text-center text-foreground focus:border-primary outline-none"
                                  />
                                  <button
                                    onClick={() => {
                                      const entries = Object.entries(formData.preferences.model_price || {});
                                      entries.splice(idx, 1);
                                      updatePreference('model_price', entries.length > 0 ? Object.fromEntries(entries) : undefined);
                                    }}
                                    className="p-0.5 text-muted-foreground hover:text-destructive transition-colors"
                                  >
                                    <X className="w-3.5 h-3.5" />
                                  </button>
                                </div>
                                {globalEntry && prefix && (
                                  <p className="text-[10px] text-amber-500/70 mt-0.5 ml-0.5">覆盖全局: {globalEntry}</p>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                      {Object.keys(globalModelPrice).length > 0 && Object.keys(formData.preferences.model_price || {}).length === 0 && (
                        <div className="text-xs text-muted-foreground bg-muted/50 rounded-lg p-2 mt-2">
                          当前使用全局价格配置（{Object.keys(globalModelPrice).length} 条规则）。点击「添加」可为该渠道单独设定价格。
                        </div>
                      )}
                    </div>

                    {/* 修改原因：OAuth 渠道余额由后端 OAuthManager 自动查询，不需要用户配置 endpoint、template 或字段映射。
                        修改方式：仅普通渠道渲染余额查询配置块，OAuth 渠道完全隐藏这一区域。
                        目的：避免用户在 OAuth 引擎下看到无效的普通余额配置项。 */}
                    {!isOAuthEngine && <div className="border-t border-border pt-4">
                      <div className="flex items-center justify-between mb-3">
                        <label className="text-sm font-medium text-foreground flex items-center gap-1.5">
                          <Wallet className="w-3.5 h-3.5 text-emerald-500" /> 余额查询
                        </label>
                        <Switch.Root
                          checked={!!formData.preferences.balance}
                          onCheckedChange={val => {
                            if (val) {
                              updatePreference('balance', { template: 'new-api' });
                            } else {
                              // eslint-disable-next-line @typescript-eslint/no-unused-vars
                              const { balance: _, ...rest } = formData.preferences;
                              updateFormData('preferences', rest);
                              setBalanceResults({});
                            }
                          }}
                          className="w-9 h-5 bg-muted rounded-full relative data-[state=checked]:bg-emerald-500 transition-colors"
                        >
                          <Switch.Thumb className="block w-4 h-4 bg-white rounded-full shadow-md transition-transform translate-x-0.5 data-[state=checked]:translate-x-[18px]" />
                        </Switch.Root>
                      </div>
                      <p className="text-xs text-muted-foreground mb-3">启用后可查询每个 Key 的余额。选择预置模板或手动配置接口地址和字段映射。</p>
                      {formData.preferences.balance && (() => {
                        const bal = formData.preferences.balance as Record<string, any>;
                        const isCustom = !bal.template;
                        return (
                          <div className="space-y-3 pl-1">
                            <div>
                              <label className="text-xs font-medium text-muted-foreground mb-1 block">模式</label>
                              <select
                                value={bal.template || '_custom'}
                                onChange={e => {
                                  const v = e.target.value;
                                  if (v === '_custom') {
                                    updatePreference('balance', { endpoint: '', mapping: { total: '', used: '', available: '', value_type: "'amount'" } });
                                  } else {
                                    updatePreference('balance', { template: v });
                                  }
                                  setBalanceResults({});
                                }}
                                className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs focus:border-primary outline-none text-foreground"
                              >
                                <option value="new-api">new-api（/api/usage/token）</option>
                                <option value="openrouter">OpenRouter</option>
                                <option value="_custom">自定义</option>
                              </select>
                            </div>
                            {isCustom && (
                              <>
                                <div>
                                  <label className="text-xs font-medium text-muted-foreground mb-1 block">接口地址 (endpoint)</label>
                                  <input
                                    type="text"
                                    value={bal.endpoint || ''}
                                    onChange={e => updatePreference('balance', { ...bal, endpoint: e.target.value })}
                                    placeholder="/api/usage/token 或 https://example.com/balance"
                                    className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs font-mono focus:border-primary outline-none text-foreground"
                                  />
                                  <p className="text-xs text-muted-foreground mt-1">相对路径拼接到域名下，绝对 URL 直接使用</p>
                                </div>
                                <div>
                                  <label className="text-xs font-medium text-muted-foreground mb-1 block">请求方式</label>
                                  <select
                                    value={bal.method || 'GET'}
                                    onChange={e => updatePreference('balance', { ...bal, method: e.target.value })}
                                    className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs focus:border-primary outline-none text-foreground"
                                  >
                                    <option value="GET">GET</option>
                                    <option value="POST">POST</option>
                                  </select>
                                </div>
                                <div>
                                  <label className="text-xs font-medium text-muted-foreground mb-1 block">值类型</label>
                                  <select
                                    value={bal.mapping?.value_type === "'percent'" ? 'percent' : bal.mapping?.value_type === "'quota'" ? 'quota' : 'amount'}
                                    onChange={e => {
                                      const vtMap: Record<string, string> = { percent: "'percent'", quota: "'quota'", amount: "'amount'" };
                                      const vt = vtMap[e.target.value] || "'amount'";
                                      updatePreference('balance', { ...bal, mapping: { ...(bal.mapping || {}), value_type: vt } });
                                    }}
                                    className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs focus:border-primary outline-none text-foreground"
                                  >
                                    <option value="amount">数额（total / used / available）</option>
                                    <option value="percent">百分比（percent）</option>
                                    <option value="quota">纯额度（以 100 为基准显示颜色）</option>
                                  </select>
                                </div>
                                <div>
                                  <label className="text-xs font-medium text-muted-foreground mb-1 block">字段映射（dot notation）</label>
                                  <div className="space-y-2">
                                    {(bal.mapping?.value_type === "'percent'" ? [
                                      { key: 'percent', label: 'percent', placeholder: 'data.remaining_percent' },
                                    ] : bal.mapping?.value_type === "'quota'" ? [
                                      { key: 'available', label: 'available', placeholder: 'balance_infos.0.total_balance' },
                                      { key: 'currency', label: 'currency (可选)', placeholder: 'balance_infos.0.currency' },
                                    ] : [
                                      { key: 'total', label: 'total', placeholder: 'data.totalQuota' },
                                      { key: 'used', label: 'used', placeholder: 'data.usedQuota' },
                                      { key: 'available', label: 'available', placeholder: 'data.remainQuota' },
                                    ]).map(field => (
                                      <div key={field.key} className="flex items-center gap-2">
                                        <span className="text-[10px] text-muted-foreground w-16 flex-shrink-0 text-right font-mono">{field.label}</span>
                                        <input
                                          type="text"
                                          value={bal.mapping?.[field.key] || ''}
                                          onChange={e => updatePreference('balance', { ...bal, mapping: { ...(bal.mapping || {}), [field.key]: e.target.value } })}
                                          placeholder={field.placeholder}
                                          className="flex-1 bg-background border border-border px-2 py-1 rounded text-xs font-mono focus:border-primary outline-none text-foreground"
                                        />
                                      </div>
                                    ))}
                                  </div>
                                  <p className="text-xs text-muted-foreground mt-2">数额模式填 2 个即可，第 3 个自动算</p>
                                </div>
                                {bal.mapping?.value_type === "'percent'" && (
                                  <div>
                                    <label className="text-xs font-medium text-muted-foreground mb-1 block">百分比乘数</label>
                                    <input
                                      type="number"
                                      value={bal.percent_multiplier ?? ''}
                                      onChange={e => updatePreference('balance', { ...bal, percent_multiplier: e.target.value ? Number(e.target.value) : undefined })}
                                      placeholder="1（接口返回 0~1 则填 100）"
                                      className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs font-mono focus:border-primary outline-none text-foreground"
                                    />
                                  </div>
                                )}
                              </>
                            )}
                            {!isCustom && bal.template && (
                              <p className="text-xs text-muted-foreground">使用 <code className="text-foreground">{bal.template}</code> 模板预设。如需微调切换为「自定义」。</p>
                            )}
                          </div>
                        );
                      })()}
                    </div>}

                  </div>
                </section>

                <div className="h-10"></div>
              </div>
            )}

            <div className="p-4 bg-muted/30 border-t border-border flex justify-end gap-3 flex-shrink-0">
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              <button onClick={handleSave} className="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary hover:bg-primary/90 rounded-lg flex items-center gap-1.5">
                <CheckCircle2 className="w-4 h-4" /> 保存配置
              </button>
            </div>

            {/* Plugin Tab Button — 编辑面板左边缘凸出 */}
            {formData && !showPluginSheet && (
              <button
                onClick={() => setShowPluginSheet(true)}
                className="absolute hidden sm:flex flex-col items-center gap-1.5 py-4 w-8 bg-muted border border-border border-r-0 rounded-l-lg cursor-pointer transition-all hover:bg-emerald-500/10 hover:w-9"
                style={{ left: 0, top: '25%', transform: 'translate(-100%, -50%)', writingMode: 'vertical-rl', textOrientation: 'mixed' }}
              >
                <Puzzle className="w-4 h-4 text-emerald-500" style={{ writingMode: 'horizontal-tb' }} />
                <span className="text-xs font-semibold text-emerald-500 tracking-wider">插件</span>
                <span className="text-[10px] font-medium bg-emerald-500 text-white rounded-full px-1.5 min-w-[18px] text-center" style={{ writingMode: 'horizontal-tb' }}>
                  {formData.preferences.enabled_plugins?.length || 0}
                </span>
              </button>
            )}

            {/* Plugin Sheet — 从左向右滑入覆盖编辑面板 */}
            {formData && (
              <InterceptorSheet
                open={showPluginSheet}
                onOpenChange={setShowPluginSheet}
                allPlugins={allPlugins}
                enabledPlugins={formData.preferences.enabled_plugins || []}
                providerPreferences={formData.preferences || {}}
                onUpdate={handlePluginSheetUpdate}
              />
            )}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* ========== Fetch Models Dialog ========== */}
      <Dialog.Root open={isFetchModelsOpen} onOpenChange={setIsFetchModelsOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-[60]" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] max-w-[95vw] max-h-[80vh] bg-background border border-border rounded-xl shadow-2xl z-[70] flex flex-col">
            <div className="p-5 border-b border-border">
              <Dialog.Title className="text-lg font-bold text-foreground">选择模型</Dialog.Title>
              <Dialog.Description className="text-sm text-muted-foreground mt-1">
                当前渠道: {formData?.provider || '未命名'}
              </Dialog.Description>
            </div>

            <div className="p-4 border-b border-border">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <input
                  type="text"
                  value={modelSearchQuery}
                  onChange={e => setModelSearchQuery(e.target.value)}
                  placeholder="搜索模型名称..."
                  className="w-full bg-muted border border-border pl-10 pr-4 py-2.5 rounded-full text-sm text-foreground"
                />
              </div>
            </div>

            <div className="p-4 border-b border-border flex items-center justify-between">
              <span className="text-sm text-muted-foreground">
                显示 {filteredFetchedModels.length} / {fetchedModels.length} 个模型，已选 {selectedModels.size} 个
              </span>
              <div className="flex gap-2">
                <button onClick={selectAllVisible} className="text-sm text-primary hover:underline">全选</button>
                <button onClick={deselectAllVisible} className="text-sm text-muted-foreground hover:text-foreground">全不选</button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto max-h-[360px]">
              {filteredFetchedModels.map(model => {
                const isSelected = selectedModels.has(model);
                const isExisting = !!formData?.models.includes(model);
                const displayName = getModelDisplayName(model);
                const hasAlias = displayName !== model;

                return (
                  <div
                    key={model}
                    onClick={() => toggleModelSelect(model)}
                    className="px-4 py-2.5 flex items-center hover:bg-muted cursor-pointer border-b border-border last:border-b-0"
                    title={hasAlias ? `上游: ${model}` : undefined}
                  >
                    <div className={`w-5 h-5 rounded border-2 flex items-center justify-center mr-3 transition-colors ${isSelected ? 'bg-primary border-primary' : 'border-muted-foreground/50'}`}>
                      {isSelected && <Check className="w-3 h-3 text-primary-foreground" />}
                    </div>

                    <span className="flex-1 font-mono text-sm text-foreground truncate">
                      {displayName}
                      {hasAlias && <span className="text-muted-foreground"> ({model})</span>}
                    </span>

                    {isExisting && <span className="text-xs bg-primary/20 text-primary px-2 py-0.5 rounded">已添加</span>}
                  </div>
                );
              })}
            </div>

            <div className="p-4 border-t border-border flex justify-end gap-3">
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              <button
                onClick={confirmFetchModels}
                className="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary hover:bg-primary/90 rounded-lg"
              >
                确认选择 ({selectedModels.size})
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {formData && (
        <ApiKeyTestDialog
          open={keyTestDialogOpen}
          onOpenChange={(v) => { setKeyTestDialogOpen(v); if (!v) setKeyTestOverride(null); }}
          title={keyTestOverride?.title || `测试 API Keys: ${formData.provider || '未命名渠道'}`}
          engine={keyTestOverride?.engine || formData.engine || 'openai'}
          base_url={keyTestOverride?.base_url || formData.base_url || ''}
          provider_snapshot={buildProviderSnapshotForTest()}
          apiKeys={formData.api_keys}
          availableModels={keyTestOverride?.models || getProviderModelNameListForUi()}
          initialKeyIndex={keyTestInitialIndex}
          onDisableKeys={disableKeysInForm}
        />
      )}

      <ChannelTestDialog
        open={testDialogOpen}
        onOpenChange={setTestDialogOpen}
        provider={testingProvider}
      />

      <ChannelAnalyticsSheet
        open={analyticsOpen}
        onOpenChange={setAnalyticsOpen}
        providerName={analyticsProvider}
      />

      {/* 修改原因：OAuth 导入弹窗需要脱离编辑抽屉层级，并提供可聚焦容器用于焦点回退。
          修改方式：继续使用 createPortal 渲染到 body，并在遮罩容器添加 tabIndex={-1}。
          目的：让导入弹窗输入框能在 Radix 编辑抽屉存在时稳定获得焦点。 */}
      {importModalIdx !== null && createPortal(
        <div tabIndex={-1} className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50" onClick={() => setImportModalIdx(null)}>
          <div className="bg-background border border-border rounded-xl p-6 w-[400px] max-w-[90vw] space-y-4" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold">导入 Refresh Token</h3>
            <p className="text-xs text-muted-foreground">粘贴 refresh_token 到下方</p>
            <textarea
              value={importToken}
              onChange={e => setImportToken(e.target.value)}
              placeholder={importPlaceholder}
              className="w-full bg-muted border border-border rounded-lg p-3 text-sm font-mono outline-none focus:border-primary min-h-[80px] resize-none"
              autoFocus
            />
            <div className="flex justify-end gap-2">
              <button onClick={() => setImportModalIdx(null)} className="text-sm px-3 py-1.5 rounded border border-border hover:bg-muted">取消</button>
              <button onClick={doImport} disabled={!importToken.trim() || importing} className="text-sm px-3 py-1.5 rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
                {importing ? '导入中...' : '导入'}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* 修改原因：OAuth 手动回调弹窗同样位于编辑抽屉外部，需要避免焦点回退到 Dialog.Content。
          修改方式：继续使用 createPortal 渲染到 body，并在遮罩容器添加 tabIndex={-1}。
          目的：让用户粘贴完整回调 URL 时，输入框可以正常点击和输入。 */}
      {oauthManualState !== null && createPortal(
        <div tabIndex={-1} className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50" onClick={() => { setOauthManualState(null); setManualUrl(''); }}>
          <div className="bg-background border border-border rounded-xl p-6 w-[480px] max-w-[90vw] space-y-4" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold">完成 OAuth 登录</h3>
            <div className="text-xs text-muted-foreground space-y-2">
              <p>1. 在弹出的窗口中完成登录</p>
              <p>2. 登录后浏览器会跳转到一个<strong>无法访问</strong>的页面，这是正常的</p>
              <p>3. 复制该页面地址栏的<strong>完整 URL</strong>，粘贴到下方</p>
            </div>
            <input
              type="text"
              value={manualUrl}
              onChange={e => setManualUrl(e.target.value)}
              placeholder="http://localhost:1455/auth/callback?code=..."
              className="w-full bg-muted border border-border rounded-lg p-3 text-sm font-mono outline-none focus:border-primary"
              autoFocus
            />
            <div className="flex justify-end gap-2">
              <button onClick={() => { setOauthManualState(null); setManualUrl(''); }} className="text-sm px-3 py-1.5 rounded border border-border hover:bg-muted">取消</button>
              <button onClick={doManualExchange} disabled={!manualUrl.trim() || exchanging} className="text-sm px-3 py-1.5 rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
                {exchanging ? '验证中...' : '完成登录'}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
