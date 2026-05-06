/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useMemo, useRef, useState, KeyboardEvent, ClipboardEvent, DragEvent } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import { toastSuccess, toastError, toastWarning, fmtErr } from '../components/Toast';
import {
  Plus, Edit, Brain, Trash2, ArrowRight, RefreshCw,
  Server, X, CheckCircle2, Settings2, Copy, ToggleRight, ToggleLeft,
  Folder, Puzzle, Network, CopyCheck, Power, Files, Play,
  Search, Check, BarChart3, Wallet, XCircle, Link2, GripVertical
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

// ========== Types ==========
interface ApiKeyObj {
  key: string;
  disabled: boolean;
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
  model_prefix?: string;
  _collapsed?: boolean;
}

interface ProviderFormData {
  provider: string;
  remark: string;
  engine: string;
  base_url: string;
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
}

interface ChannelOption {
  id: string;
  type_name: string;
  default_base_url: string;
  description?: string;
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
];

function readBooleanPreference(value: any): boolean {
  // 修改原因：后端新增 pool_sharing 布尔开关，旧配置中也可能存在字符串形式的布尔值。
  // 修改方式：统一把 true/1/yes/on 转为 true，其余值按 JavaScript 布尔语义处理。
  // 目的：让表单初始化时能够稳定显示共享路由池开关的实际状态。
  if (typeof value === 'string') return ['true', '1', 'yes', 'on'].includes(value.trim().toLowerCase());
  return Boolean(value);
}

// ── 余额类型 ──
interface BalanceResult {
  supported: boolean;
  value_type?: 'amount' | 'percent';
  total?: number | null;
  used?: number | null;
  available?: number | null;
  percent?: number | null;
  raw?: any;
  error?: string | null;
}

function getBalancePercent(b: BalanceResult): number | null {
  if (!b.supported || b.error) return null;
  if (b.value_type === 'percent' && b.percent != null) return b.percent;
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
  if (!b.supported || b.error) return null;
  if (b.value_type === 'percent' && b.percent != null) return `${b.percent.toFixed(1)}%`;
  if (b.available != null && b.total != null) return `${b.available.toFixed(1)} / ${b.total.toFixed(1)}`;
  if (b.available != null) return `${b.available.toFixed(1)}`;
  return null;
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

// ── 冷却中 Key 行组件（SVG 边框进度） ──
function CoolingKeyRow({ idx, keyObj, remainSec, totalDuration, focused, onFocus, onBlur, onRecover, onToggle, onTest, onDelete }: {
  idx: number; keyObj: { key: string; disabled: boolean }; remainSec: number; totalDuration: number;
  focused: boolean;
  onFocus: () => void; onBlur: () => void;
  onRecover: () => void; onToggle: () => void; onTest: () => void; onDelete: () => void;
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
        <div className="flex-1 min-w-0 relative z-[2]">
          <input
            type="text" value={keyObj.key || ''} readOnly placeholder="sk-..."
            onFocus={onFocus} onBlur={onBlur}
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
      </div>
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
  const [isVirtualProviderPanelCollapsed, setIsVirtualProviderPanelCollapsed] = useState(true);
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

      const [configRes, typesRes, pluginsRes, activityRes] = await Promise.all([
        apiFetch('/v1/api_config', { headers }),
        apiFetch('/v1/channels', { headers }),
        apiFetch('/v1/plugins/interceptors', { headers }),
        apiFetch('/v1/stats/provider_activity', { headers }).catch(() => null),
      ]);

      if (configRes.ok) {
        const data = await configRes.json();
        const rawProviders = data.providers || data.api_config?.providers || [];
        // 按权重降序排序
        const sortedProviders = [...rawProviders].sort((a, b) => {
          const weightA = a.preferences?.weight ?? a.weight ?? 0;
          const weightB = b.preferences?.weight ?? b.weight ?? 0;
          return weightB - weightA;
        });
        setProviders(sortedProviders);
        const globalPrefs = data.preferences || data.api_config?.preferences || {};
        setGlobalModelPrice(globalPrefs.model_price || {});
        // 修改原因：虚拟模型路由由 /v1/api_config 的全局 preferences 返回。
        // 修改方式：读取 preferences.virtual_models 并在缺失时回退为空对象。
        // 目的：页面加载后立即展示已配置的全局虚拟模型路由。
        const loadedVirtualModels = globalPrefs.virtual_models || {};
        setVirtualModels(loadedVirtualModels);
        setVirtualModelsDirty(false);
        // 修改原因：虚拟模型现在通过渠道列表卡片进入抽屉编辑，旧画布展开状态不再承担主要展示职责。
        // 修改方式：仍保留已有展开状态初始化，供历史函数和后续兼容逻辑使用。
        // 目的：减少本次 UI 重构对既有状态和 CRUD 函数的破坏范围。
        setExpandedVirtualModels(prev => prev.size > 0 ? prev : new Set(Object.keys(loadedVirtualModels).slice(0, 1)));
      }
      if (typesRes.ok) {
        const data = await typesRes.json();
        setChannelTypes(data.channels || []);
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
    if (isModalOpen && formData?.preferences?.balance && formData.base_url && formData.api_keys.some(k => k.key.trim() && !k.disabled)) {
      queryAllBalances(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isModalOpen]);

  const openModal = (provider: any = null, index: number | null = null) => {
    setOriginalIndex(index);
    setGroupInput('');
    setModelInput('');
    setShowPluginSheet(false);
    refreshKeyStatus();

    setBalanceResults({});
    setBalanceLoading(false);
    setFocusedKeyIdx(null);

    if (provider) {
      const parseApiKey = (keyStr: string) => {
        const trimmed = String(keyStr).trim();
        if (trimmed.startsWith('!')) return { key: trimmed.substring(1), disabled: true };
        return { key: trimmed, disabled: false };
      };

      let parsedKeys: ApiKeyObj[] = [];
      if (Array.isArray(provider.api)) parsedKeys = provider.api.map(parseApiKey);
      else if (typeof provider.api === 'string' && provider.api.trim()) parsedKeys = [parseApiKey(provider.api.trim())];
      else if (Array.isArray(provider.api_keys)) parsedKeys = provider.api_keys.map(parseApiKey);

      const rawModels = Array.isArray(provider.model) ? provider.model : Array.isArray(provider.models) ? provider.models : [];
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
      if (Array.isArray(provider.groups) && provider.groups.length > 0) groups = provider.groups;
      else if (typeof provider.group === 'string' && provider.group.trim()) groups = [provider.group.trim()];
      else if (provider.preferences?.group) groups = [provider.preferences.group.trim()];

      const pHeaders = provider.preferences?.headers || {};
      const pOverrides = provider.preferences?.post_body_parameter_overrides || {};
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

      const pStatusCodeOverrides = provider.preferences?.status_code_overrides || {};
      setStatusCodeOverridesJson(Object.keys(pStatusCodeOverrides).length > 0 ? JSON.stringify(pStatusCodeOverrides, null, 2) : '');

      const basePreferences = provider.preferences && typeof provider.preferences === 'object'
        ? provider.preferences
        : {};

      // 解析子渠道
      const rawSubChannels = Array.isArray(provider.sub_channels) ? provider.sub_channels : [];
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
          model_prefix: sub.model_prefix || '',
          _collapsed: true,
        };
      });

      setFormData({
        provider: provider.provider || provider.name || '',
        remark: provider.remark || '',
        engine: provider.engine || '',
        base_url: provider.base_url || '',
        api_keys: parsedKeys,
        model_prefix: provider.model_prefix || '',
        enabled: provider.enabled !== false,
        groups,
        models,
        mappings,
        preferences: {
          ...basePreferences,
          weight: basePreferences.weight ?? provider.weight ?? 10,
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
    if (!formData || !formData.base_url) return;
    const balanceCfg = formData.preferences?.balance;
    if (!balanceCfg) { if (!silent) toastWarning('该渠道未配置余额查询（preferences.balance）'); return; }

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
              engine: formData.engine,
              base_url: formData.base_url,
              api_key: keyObj.key,
              preferences: formData.preferences,
            }),
          });
          const data = await res.json().catch(() => ({ supported: false, error: '响应解析失败' }));
          results[keyObj.key] = data;
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
    if (formData) updateFormData('api_keys', [...formData.api_keys, { key: '', disabled: false }]);
  };

  const updateKey = (idx: number, keyStr: string) => {
    if (!formData) return;
    const newKeys = [...formData.api_keys];
    newKeys[idx].key = keyStr;
    updateFormData('api_keys', newKeys);
  };

  const toggleKeyDisabled = (idx: number) => {
    if (!formData) return;
    const newKeys = [...formData.api_keys];
    newKeys[idx].disabled = !newKeys[idx].disabled;
    updateFormData('api_keys', newKeys);
  };

  const deleteKey = (idx: number) => {
    if (!formData) return;
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
    const name = provider?.provider || `渠道 ${idx + 1}`;
    if (!confirm(`确定要删除渠道 "${name}" 吗？此操作不可撤销。`)) return;

    const newProviders = providers.filter((_, i) => i !== idx);
    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ providers: newProviders }),
      });
      if (res.ok) {
        setProviders(newProviders);
        toastError(`已删除渠道 "${name}"`);
      } else {
        toastError('删除失败');
      }
    } catch {
      toastError('网络错误');
    }
  };

  const handleToggleProvider = async (idx: number) => {
    const provider = providers[idx];
    const newEnabled = provider.enabled === false ? true : false;
    const newProviders = [...providers];
    newProviders[idx] = { ...provider, enabled: newEnabled };

    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ providers: newProviders }),
      });
      if (res.ok) {
        setProviders(newProviders);
      } else {
        toastError('操作失败');
      }
    } catch {
      toastError('网络错误');
    }
  };

  const handleCopyProvider = (provider: any) => {
    const copy = JSON.parse(JSON.stringify(provider));
    const originalName = copy.provider || 'channel';
    copy.provider = `${originalName}_copy`;
    openModal(copy, null);
    toastSuccess('已复制渠道配置，请修改后保存');
  };

  // ── 子渠道操作 ──
  const handleToggleSubChannel = async (parentIdx: number, subIdx: number) => {
    const parent = providers[parentIdx];
    const subs = [...(parent.sub_channels || [])];
    subs[subIdx] = { ...subs[subIdx], enabled: subs[subIdx].enabled === false ? true : false };
    const newProviders = [...providers];
    newProviders[parentIdx] = { ...parent, sub_channels: subs };
    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ providers: newProviders }),
      });
      if (res.ok) setProviders(newProviders);
      else toastError('操作失败');
    } catch { toastError('网络错误'); }
  };

  const handleDeleteSubChannel = async (parentIdx: number, subIdx: number) => {
    const parent = providers[parentIdx];
    const sub = (parent.sub_channels || [])[subIdx];
    const name = sub?.remark || sub?.engine || `子渠道 ${subIdx + 1}`;
    if (!confirm(`确定要删除子渠道 "${name}" 吗？`)) return;
    const subs = (parent.sub_channels || []).filter((_: any, i: number) => i !== subIdx);
    const newProviders = [...providers];
    newProviders[parentIdx] = { ...parent, sub_channels: subs.length > 0 ? subs : undefined };
    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ providers: newProviders }),
      });
      if (res.ok) { setProviders(newProviders); }
      else toastError('删除失败');
    } catch { toastError('网络错误'); }
  };

  const openSubChannelEdit = (parentIdx: number, subIdx: number) => {
    const parent = providers[parentIdx];
    const sub = (parent.sub_channels || [])[subIdx];
    if (!sub) return;
    // 构造一个虚拟 provider 给 openModal，合并主渠道的 key 等
    setEditingSubChannel({ parentIdx, subIdx });
    openModal({
      provider: `${parent.provider}:${sub.engine || 'sub'}`,
      engine: sub.engine || '',
      base_url: sub.base_url || parent.base_url || '',
      api: parent.api,
      model: sub.model || sub.models || [],
      model_prefix: sub.model_prefix || parent.model_prefix || '',
      enabled: sub.enabled !== false,
      remark: sub.remark || '',
      groups: parent.groups || ['default'],
      preferences: {
        ...(parent.preferences || {}),
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
      api: parent.api,
      model: sub.model || sub.models || [],
      model_prefix: sub.model_prefix || parent.model_prefix || '',
      enabled: sub.enabled !== false,
      groups: parent.groups || ['default'],
      preferences: { ...(parent.preferences || {}), ...(sub.preferences || {}) },
    };
  };

  // 排序函数
  const sortByWeight = (list: any[]) => {
    return [...list].sort((a, b) => {
      const weightA = a.preferences?.weight ?? a.weight ?? 0;
      const weightB = b.preferences?.weight ?? b.weight ?? 0;
      return weightB - weightA;
    });
  };

  const handleUpdateWeight = async (idx: number, newWeight: number) => {
    const newProviders = [...providers];
    if (!newProviders[idx].preferences) newProviders[idx].preferences = {};
    newProviders[idx].preferences.weight = newWeight;

    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ providers: newProviders }),
      });
      if (res.ok) {
        // 更新后重新排序
        setProviders(sortByWeight(newProviders));
      }
    } catch {
      console.error('Failed to update weight');
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

    const serializedKeys = formData.api_keys
      .map(k => k.disabled ? `!${k.key.trim()}` : k.key.trim())
      .filter(Boolean);
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

    return {
      provider: formData.provider,
      remark: formData.remark || undefined,
      base_url: formData.base_url,
      model_prefix: formData.model_prefix || undefined,
      api: finalApi,
      model: finalModels,
      engine: formData.engine || undefined,
      enabled: formData.enabled,
      groups: formData.groups,
      preferences: {
        ...formData.preferences,
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
    const aliasMap = getAliasMap();
    const names: string[] = [];
    formData.models.forEach(upstream => {
      const alias = aliasMap.get(upstream);
      names.push(alias || upstream);
    });
    formData.mappings.forEach(m => {
      if (m.from) names.push(m.from);
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

    const serializedKeys = formData.api_keys
      .map(k => k.disabled ? `!${k.key.trim()}` : k.key.trim())
      .filter(Boolean);
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
        if (sub.model_prefix) subObj.model_prefix = sub.model_prefix;
        if (sub.remark) subObj.remark = sub.remark;
        if (sub.enabled === false) subObj.enabled = false;
        if (Object.keys(sub.preferences).length > 0) subObj.preferences = sub.preferences;
        return subObj;
      });

    const targetProvider: any = {
      provider: formData.provider,
      remark: formData.remark || undefined,
      base_url: formData.base_url,
      model_prefix: formData.model_prefix || undefined,
      api: finalApi,
      model: finalModels,
      engine: formData.engine || undefined,
      enabled: formData.enabled,
      groups: formData.groups,
      preferences: {
        ...formData.preferences,
        pool_sharing: normalizedPoolSharing,
        model_price: cleanedModelPrice,
        headers: headersObj,
        post_body_parameter_overrides: overridesObj,
        status_code_overrides: statusCodeOverridesObj,
      },
      sub_channels: serializedSubChannels.length > 0 ? serializedSubChannels : undefined,
    };

    let newProviders: any[];

    if (editingSubChannel) {
      // 子渠道模式：保存回 parent.sub_channels
      const { parentIdx, subIdx } = editingSubChannel;
      const parent = providers[parentIdx];
      const parentPrefs = parent.preferences || {};

      // 计算子渠道 diff preferences（只保存和主渠道不同的部分）
      const subPrefs: Record<string, any> = {};
      const mergedPrefs = {
        ...formData.preferences,
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
      if (formData.model_prefix && formData.model_prefix !== (parent.model_prefix || '')) subObj.model_prefix = formData.model_prefix;
      if (formData.remark) subObj.remark = formData.remark;
      if (Object.keys(subPrefs).length > 0) subObj.preferences = subPrefs;

      const subs = [...(parent.sub_channels || [])];
      subs[subIdx] = subObj;
      newProviders = [...providers];
      newProviders[parentIdx] = { ...parent, sub_channels: subs };
    } else {
      // 主渠道模式
      newProviders = [...providers];
      if (originalIndex !== null) newProviders[originalIndex] = targetProvider;
      else newProviders.push(targetProvider);
    }

    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ providers: newProviders }),
      });

      if (res.ok) {
        setProviders(sortByWeight(newProviders));
        setIsModalOpen(false);
        setEditingSubChannel(null);
      } else {
        toastError("保存失败");
      }
    } catch {
      toastError("网络错误");
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
            <ProviderLogo name={p.provider} engine={p.engine} />
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
                <ProviderLogo name={providerName} engine={provider?.engine} />
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
                  <ProviderLogo name={providerName} engine={provider?.engine} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="text-xs font-medium text-foreground truncate">{providerName}</span>
                      {isSubChannel && <span className="text-[10px] px-1 py-0.5 rounded bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 flex-shrink-0">子</span>}
                      <span className="text-[10px] px-1 py-0.5 rounded bg-muted text-muted-foreground font-mono flex-shrink-0">{provider?.engine || 'openai'}</span>
                    </div>
                    <div className="text-[10px] text-muted-foreground mt-0.5 truncate">
                      {isSubChannel && parentProviderName ? `${parentProviderName} · ` : ''}权重 {weight} · {modelOptions.length} 模型
                    </div>
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
                  const configDisabledKeys = apiRaw.filter((k: any) => typeof k === 'string' && k.startsWith('!')).length;
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
                        <ProviderLogo name={p.provider} engine={p.engine} />
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

                <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
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
                                  <button onClick={() => updateVirtualEditorChainDraft(prev => prev.filter((_, removeIdx) => removeIdx !== idx))} className="p-1.5 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md transition-colors" title="删除节点">
                                    <X className="w-4 h-4" />
                                  </button>
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
      <Dialog.Root open={isModalOpen} onOpenChange={(open) => { setIsModalOpen(open); if (!open) setEditingSubChannel(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40 animate-in fade-in duration-200" />
          <Dialog.Content className="fixed right-0 top-0 h-full w-full sm:w-[560px] bg-background border-l border-border shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300">
            <div className="p-4 sm:p-5 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
              <Dialog.Title className="text-lg sm:text-xl font-bold text-foreground flex items-center gap-2">
                <Server className="w-5 h-5 text-primary" />
                {editingSubChannel ? `编辑子渠道: ${formData?.remark || formData?.engine || ''}` : originalIndex !== null ? `编辑: ${formData?.provider}` : '新增渠道'}
              </Dialog.Title>
              <Dialog.Close className="text-muted-foreground hover:text-foreground"><X className="w-5 h-5" /></Dialog.Close>
            </div>

            {formData && (
              <div className="flex-1 overflow-y-auto p-4 sm:p-5 space-y-6">
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
                        }} className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm outline-none text-foreground">
                          <option value="">默认 (自动推断)</option>
                          {channelTypes.map(c => <option key={c.id} value={c.id}>{c.description || c.id}</option>)}
                        </select>
                      </div>
                    </div>
                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">API 地址 (Base URL)</label>
                      <input type="text" value={formData.base_url} onChange={e => updateFormData('base_url', e.target.value)} placeholder="留空则使用渠道默认地址，末尾加 # 则不拼接路径后缀" className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono outline-none text-foreground" />
                      <span className="text-xs text-muted-foreground mt-1 block">{'末尾加 # 可直接使用完整地址，不拼接路径后缀（如 https://example.com/v1/chat#）'}</span>
                    </div>
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
                        disabled={balanceLoading || !formData.preferences?.balance}
                        className="text-emerald-600 dark:text-emerald-400 hover:text-emerald-700 dark:hover:text-emerald-300 flex items-center gap-1 disabled:opacity-50 disabled:cursor-not-allowed"
                        title={formData.preferences?.balance ? '查询所有 Key 的余额' : '未配置余额查询（在高级设置中配置 preferences.balance）'}
                      >
                        <Wallet className={`w-3 h-3 ${balanceLoading ? 'animate-pulse' : ''}`} /> {balanceLoading ? '查询中...' : '余额'}
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
                    </div>
                  </div>
                  <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                    {formData.api_keys.map((keyObj, idx) => {
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
                      const bal = balanceResults[keyObj.key];

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
                          />
                        );
                      }

                      const balPct = bal ? getBalancePercent(bal) : null;
                      const balColor = getBalanceColor(balPct);
                      const balLabel = bal ? getBalanceLabel(bal) : null;
                      const hasTag = !isGrayed && (!!balLabel || isPermanent);

                      return (
                        <div key={idx} className={`relative flex items-center gap-2 px-3 py-2 rounded-lg border overflow-hidden transition-colors ${isFocused ? 'border-blue-500' : 'border-border'} ${isGrayed ? 'bg-muted/30 opacity-50' : 'bg-muted/50'}`}>
                          {/* 余额进度条背景 */}
                          {!isFocused && balColor && balPct != null && (
                            <div className="absolute left-0 top-0 bottom-0 rounded-[7px] z-0 pointer-events-none transition-all duration-500"
                                 style={{ width: `${Math.max(1, balPct)}%`, background: BALANCE_FILL_COLORS[balColor] }} />
                          )}
                          <span className="text-xs text-muted-foreground w-4 text-right relative z-[2]">{idx + 1}</span>
                          <div className="flex-1 min-w-0 relative z-[2]" style={hasTag && !isFocused ? { WebkitMaskImage: 'linear-gradient(to right, black 0%, black 60%, transparent 100%)', maskImage: 'linear-gradient(to right, black 0%, black 60%, transparent 100%)' } : undefined}>
                            <input
                              type="text"
                              value={keyObj.key}
                              onChange={e => updateKey(idx, e.target.value)}
                              onPaste={e => handleKeyPaste(e, idx)}
                              onFocus={() => setFocusedKeyIdx(idx)}
                              onBlur={() => setFocusedKeyIdx(null)}
                              placeholder="sk-..."
                              className={`w-full bg-transparent border-none text-sm font-mono outline-none min-w-0 ${isGrayed ? 'text-muted-foreground line-through' : 'text-foreground'}`}
                            />
                          </div>
                          {!isFocused && balLabel && balColor && (
                            <span className={`flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] ${TAG_CLASSES[balColor]}`}>{balLabel}</span>
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
                        </div>
                      );
                    })}
                    {formData.api_keys.length === 0 && <div className="text-center p-4 text-sm text-muted-foreground italic">暂无密钥</div>}
                  </div>
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
                                  {channelTypes.map(c => <option key={c.id} value={c.id}>{c.description || c.id}</option>)}
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
                        按顺序匹配，第一个命中的规则生效。duration: -1 = 永久禁用，0 = 仅映射不处理 Key，&gt;0 = 冷却秒数。
                      </p>

                      {/* 规则列表 */}
                      <div className="space-y-2">
                        {(formData.preferences.key_rules || []).map((rule: any, idx: number) => {
                          const rules = formData.preferences.key_rules || [];
                          const updateRule = (patch: any) => {
                            const next = [...rules];
                            next[idx] = { ...next[idx], ...patch };
                            updatePreference('key_rules', next);
                          };
                          const removeRule = () => {
                            updatePreference('key_rules', rules.filter((_: any, i: number) => i !== idx));
                          };
                          const matchType = rule.match === 'default' ? 'default' : rule.match?.status ? 'status' : rule.match?.keyword ? 'keyword' : 'status';
                          const matchValue = matchType === 'status'
                            ? (rule.match?.status || []).join(', ')
                            : matchType === 'keyword'
                            ? (Array.isArray(rule.match?.keyword) ? rule.match.keyword.join(', ') : rule.match?.keyword || '')
                            : '';

                          return (
                            <div key={idx} className="flex items-center gap-2 bg-muted/30 border border-border rounded-lg px-3 py-2">
                              {/* 匹配类型 */}
                              <select
                                value={matchType}
                                onChange={e => {
                                  const t = e.target.value;
                                  if (t === 'default') updateRule({ match: 'default' });
                                  else if (t === 'status') updateRule({ match: { status: [429] } });
                                  else if (t === 'keyword') updateRule({ match: { keyword: [''] } });
                                }}
                                className="bg-background border border-border rounded px-2 py-1 text-xs text-foreground w-20 flex-shrink-0"
                              >
                                <option value="status">状态码</option>
                                <option value="keyword">关键词</option>
                                <option value="default">默认</option>
                              </select>

                              {/* 匹配值 */}
                              {matchType !== 'default' && (
                                <input
                                  type="text"
                                  value={matchValue}
                                  onChange={e => {
                                    const val = e.target.value;
                                    if (matchType === 'status') {
                                      const codes = val.split(/[,\s]+/).map(s => parseInt(s.trim())).filter(n => !isNaN(n));
                                      updateRule({ match: { status: codes } });
                                    } else {
                                      const kws = val.split(/[,]/).map(s => s.trim()).filter(Boolean);
                                      updateRule({ match: { keyword: kws.length > 0 ? kws : [''] } });
                                    }
                                  }}
                                  placeholder={matchType === 'status' ? '429, 500' : 'quota, billing'}
                                  className="flex-1 min-w-0 bg-background border border-border rounded px-2 py-1 text-xs font-mono text-foreground"
                                />
                              )}
                              {matchType === 'default' && <div className="flex-1 text-xs text-muted-foreground italic">兜底规则</div>}

                              {/* 箭头 */}
                              <span className="text-muted-foreground text-xs flex-shrink-0">→</span>

                              {/* Duration */}
                              <select
                                value={rule.duration === -1 ? '-1' : rule.duration > 0 ? 'custom' : '0'}
                                onChange={e => {
                                  const v = e.target.value;
                                  if (v === '-1') updateRule({ duration: -1 });
                                  else if (v === '0') updateRule({ duration: 0 });
                                  else updateRule({ duration: rule.duration > 0 ? rule.duration : 60 });
                                }}
                                className="bg-background border border-border rounded px-2 py-1 text-xs text-foreground w-24 flex-shrink-0"
                              >
                                <option value="-1">永久禁用</option>
                                <option value="custom">冷却</option>
                                <option value="0">仅映射</option>
                              </select>

                              {rule.duration > 0 && (
                                <div className="flex items-center gap-1 flex-shrink-0">
                                  <input
                                    type="number"
                                    value={rule.duration}
                                    onChange={e => updateRule({ duration: Math.max(1, parseInt(e.target.value) || 1) })}
                                    min={1}
                                    className="w-16 bg-background border border-border rounded px-2 py-1 text-xs font-mono text-foreground"
                                  />
                                  <span className="text-xs text-muted-foreground">秒</span>
                                </div>
                              )}

                              {/* Remap (可选) */}
                              {matchType === 'status' && (
                                <div className="flex items-center gap-1 flex-shrink-0">
                                  <span className="text-[10px] text-muted-foreground">映射</span>
                                  <input
                                    type="number"
                                    value={rule.remap || ''}
                                    onChange={e => {
                                      const v = parseInt(e.target.value);
                                      if (!isNaN(v) && v >= 100 && v <= 599) updateRule({ remap: v });
                                      else { const { remap: _, ...rest } = rules[idx]; const next = [...rules]; next[idx] = rest; updatePreference('key_rules', next); }
                                    }}
                                    placeholder="-"
                                    className="w-14 bg-background border border-border rounded px-1.5 py-1 text-xs font-mono text-foreground"
                                  />
                                </div>
                              )}

                              {/* 删除 */}
                              <button onClick={removeRule} className="text-red-500 hover:text-red-400 flex-shrink-0 p-0.5">
                                <X className="w-3.5 h-3.5" />
                              </button>
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
                      <p className="text-xs text-muted-foreground mt-1">失焦时自动格式化</p>
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

                    {/* 余额查询配置 */}
                    <div className="border-t border-border pt-4">
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
                                <option value="new-api">new-api（/api/status）</option>
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
                                    placeholder="/api/status 或 https://example.com/balance"
                                    className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs font-mono focus:border-primary outline-none text-foreground"
                                  />
                                  <p className="text-xs text-muted-foreground mt-1">相对路径拼接到域名下，绝对 URL 直接使用</p>
                                </div>
                                <div>
                                  <label className="text-xs font-medium text-muted-foreground mb-1 block">值类型</label>
                                  <select
                                    value={bal.mapping?.value_type === "'percent'" ? 'percent' : 'amount'}
                                    onChange={e => {
                                      const vt = e.target.value === 'percent' ? "'percent'" : "'amount'";
                                      updatePreference('balance', { ...bal, mapping: { ...(bal.mapping || {}), value_type: vt } });
                                    }}
                                    className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs focus:border-primary outline-none text-foreground"
                                  >
                                    <option value="amount">数额（total / used / available）</option>
                                    <option value="percent">百分比（percent）</option>
                                  </select>
                                </div>
                                <div>
                                  <label className="text-xs font-medium text-muted-foreground mb-1 block">字段映射（dot notation）</label>
                                  <div className="space-y-2">
                                    {(bal.mapping?.value_type === "'percent'" ? [
                                      { key: 'percent', label: 'percent', placeholder: 'data.remaining_percent' },
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
                    </div>

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
    </div>
  );
}
