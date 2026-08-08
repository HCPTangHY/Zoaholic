/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react';
import { useAuthStore } from '../../../store/authStore';
import { apiFetch } from '../../../lib/api';
import { toastSuccess, toastError, toastWarning, fmtErr } from '../../../components/Toast';
import {
  Plus, Edit, Trash2, ArrowRight, RefreshCw,
  Server, X, CheckCircle2, Settings2, Copy, ToggleRight, ToggleLeft,
  Folder, Puzzle, Power, Files, Play,
  Check, BarChart3, Wallet, Link2, GripVertical,
  ClipboardPaste, LogIn, Download, LayoutList, LayoutGrid
} from 'lucide-react';
import { ProviderLogo } from '../../../components/ProviderLogos';
import {
  buildProviderListItems,
  buildVirtualProviderEntries,
  buildVirtualProviderPanelItems,
  buildVirtualRouteTestProvider,
  buildVirtualRoutingProviderItems,
  getProviderWeight,
  summarizeVirtualChain,
} from '../../../lib/virtualModels';
import {
  formatKeyRuleKeywordsInput,
  formatKeyRuleStatusInput,
  getKeyRuleRetryMode,
  parseKeyRuleKeywordsInput,
  parseKeyRuleStatusInput,
  setKeyRuleRetryMode,
  type KeyRuleRetryMode,
} from '../../../lib/keyRules';
import type {
  ApiKeyObj,
  BalanceResult,
  ChannelOption,
  HeaderEntry,
  ModelMapping,
  PluginOption,
  ProviderFormData,
  ProviderModelOption,
  Segment,
  SubChannelFormData,
  UiSlotValue,
  VirtualDragPayload,
  VirtualModelChainNode,
  VirtualModelConfig,
} from '../types';
import {
  BALANCE_FILL_COLORS,
  SCHEDULE_ALGORITHMS,
  TAG_CLASSES,
  buildProviderApiPath,
  buildRowQuota,
  buildRowQuotaSlotData,
  getBalanceColor,
  getBalanceLabel,
  getBalancePercent,
  getOAuthQuota,
  getQuotaFromSource,
  getQuotaPairFromGauges,
  hasUiSlot,
  normalizeOAuthAccountStateMap,
  readBooleanPreference,
  serializeChannelPreferences,
  sortProvidersByWeight,
} from '../utils';
import { QuotaBorderOverlay } from '../components/QuotaComponents';
import {
  CoolingKeyRow,
  DeferredInput,
  KeyLabelOverlay,
  RackCard,
  RackGrid,
  UiSlot,
} from '../components/KeyComponents';

// 修改原因：ChannelsPage 需要先精简为页面骨架，但原页面状态和处理函数数量很多。
// 修改方式：本 hook 现在只保留核心数据、运行时 Key 状态、余额状态和真实渠道列表筛选。
// 目的：编辑器与虚拟模型逻辑迁入各自 hook 后，核心 hook 仍提供页面组合所需的稳定基础数据。
export interface RuntimeKeyStatusMap {
  [provider: string]: { auto_disabled: { key: string; remaining_seconds: number; duration: number; reason: string }[]; cooling: any[] };
}

export interface LocalCountdownMap {
  [provider: string]: Record<string, { remaining: number; duration: number }>;
}

export interface BalanceQueryContext {
  formData: ProviderFormData | null;
  isOAuthEngine: boolean;
  setOauthAccounts?: Dispatch<SetStateAction<Record<string, any>>>;
}

export interface UseChannelsCoreResult {
  providers: any[];
  setProviders: Dispatch<SetStateAction<any[]>>;
  providerActivity: Record<string, string>;
  setProviderActivity: Dispatch<SetStateAction<Record<string, string>>>;
  channelTypes: ChannelOption[];
  setChannelTypes: Dispatch<SetStateAction<ChannelOption[]>>;
  allPlugins: PluginOption[];
  setAllPlugins: Dispatch<SetStateAction<PluginOption[]>>;
  loading: boolean;
  setLoading: Dispatch<SetStateAction<boolean>>;
  balanceResults: Record<string, BalanceResult>;
  setBalanceResults: Dispatch<SetStateAction<Record<string, BalanceResult>>>;
  balanceLoading: boolean;
  setBalanceLoading: Dispatch<SetStateAction<boolean>>;
  focusedKeyIdx: number | null;
  setFocusedKeyIdx: Dispatch<SetStateAction<number | null>>;
  forceListMode: boolean;
  setForceListMode: Dispatch<SetStateAction<boolean>>;
  globalModelPrice: Record<string, string>;
  setGlobalModelPrice: Dispatch<SetStateAction<Record<string, string>>>;
  apiConfigPreferences: Record<string, any>;
  setApiConfigPreferences: Dispatch<SetStateAction<Record<string, any>>>;
  loadedVirtualModels: Record<string, VirtualModelConfig>;
  setLoadedVirtualModels: Dispatch<SetStateAction<Record<string, VirtualModelConfig>>>;
  loadedVirtualModelsVersion: number;
  runtimeKeyStatus: RuntimeKeyStatusMap;
  setRuntimeKeyStatus: Dispatch<SetStateAction<RuntimeKeyStatusMap>>;
  localCountdowns: LocalCountdownMap;
  setLocalCountdowns: Dispatch<SetStateAction<LocalCountdownMap>>;
  filterKeyword: string;
  setFilterKeyword: Dispatch<SetStateAction<string>>;
  filterEngine: string;
  setFilterEngine: Dispatch<SetStateAction<string>>;
  filterGroup: string;
  setFilterGroup: Dispatch<SetStateAction<string>>;
  filterStatus: '' | 'enabled' | 'disabled';
  setFilterStatus: Dispatch<SetStateAction<'' | 'enabled' | 'disabled'>>;
  token: string | null;
  applyApiConfigData: (data: any, options?: { syncVirtualModels?: boolean }) => void;
  refreshProviders: (options?: { syncVirtualModels?: boolean }) => Promise<void>;
  refreshSingleProvider: (providerId: string) => Promise<void>;
  fetchInitialData: () => Promise<void>;
  refreshKeyStatus: () => Promise<void>;
  queryAllBalances: (context: BalanceQueryContext | null, silent?: boolean) => Promise<void>;
  getProviderModelNames: (provider: any) => string[];
  providerListItems: { p: any; idx: number }[];
  availableEngines: string[];
  availableGroups: string[];
  getProviderAnalyticsName: (provider: any) => string;
  filteredProviders: { p: any; idx: number }[];
  getMatchedModels: (provider: any) => string[];
  hasActiveFilters: string | boolean;
  totalListItemCount: number;
  visibleListItemCount: number;
  isProviderInactive: (provider: any) => boolean;
  expandedInactiveGroups: Set<number>;
  setExpandedInactiveGroups: Dispatch<SetStateAction<Set<number>>>;
  toggleInactiveGroup: (groupKey: number) => void;
  segments: Segment[];
}

export function useChannelsCore(): UseChannelsCoreResult {
  const [providers, setProviders] = useState<any[]>([]);
  const [providerActivity, setProviderActivity] = useState<Record<string, string>>({});
  const [channelTypes, setChannelTypes] = useState<ChannelOption[]>([]);
  const [allPlugins, setAllPlugins] = useState<PluginOption[]>([]);
  const [loading, setLoading] = useState(true);

  // ── 余额查询 ──
  const [balanceResults, setBalanceResults] = useState<Record<string, BalanceResult>>({});
  const [balanceLoading, setBalanceLoading] = useState(false);
  const [focusedKeyIdx, setFocusedKeyIdx] = useState<number | null>(null);
  const [forceListMode, setForceListMode] = useState(false);

  // ── 全局配置（用于价格提示等）──
  const [globalModelPrice, setGlobalModelPrice] = useState<Record<string, string>>({});
  // 修改原因：虚拟模型状态已迁出核心 hook，但初始加载仍由核心 hook 请求 /v1/api_config。
  // 修改方式：核心 hook 保存最近一次全局 preferences 和 virtual_models 快照，并用版本号通知 useVirtualModels 同步。
  // 目的：保持初始化请求不重复，同时让虚拟模型草稿由专用 hook 独立维护。
  const [apiConfigPreferences, setApiConfigPreferences] = useState<Record<string, any>>({});
  const [loadedVirtualModels, setLoadedVirtualModels] = useState<Record<string, VirtualModelConfig>>({});
  const [loadedVirtualModelsVersion, setLoadedVirtualModelsVersion] = useState(0);

  // ── Key 运行时状态 ──
  const [runtimeKeyStatus, setRuntimeKeyStatus] = useState<Record<string, { auto_disabled: { key: string; remaining_seconds: number; duration: number; reason: string }[]; cooling: any[] }>>({});
  const [localCountdowns, setLocalCountdowns] = useState<Record<string, Record<string, { remaining: number; duration: number }>>>({}); // provider -> key -> {remaining, duration}

  // ── 列表筛选 ──
  const [filterKeyword, setFilterKeyword] = useState('');
  const [filterEngine, setFilterEngine] = useState<string>(''); // '' = 全部
  const [filterGroup, setFilterGroup] = useState<string>('');   // '' = 全部
  const [filterStatus, setFilterStatus] = useState<'' | 'enabled' | 'disabled'>('');

  const { token } = useAuthStore();

  const applyApiConfigData = (data: any, options: { syncVirtualModels?: boolean } = {}) => {
    // 修改原因：初始加载和单渠道保存后的刷新都要把 /v1/api_config 响应写回页面状态，但普通 provider 刷新不应覆盖未保存的虚拟路由草稿。
    // 修改方式：始终刷新 providers、全局价格和全局 preferences；只有显式 syncVirtualModels 时才更新虚拟模型快照。
    // 目的：核心 hook 不直接持有虚拟模型编辑状态，同时仍能把初始配置交给 useVirtualModels。
    const rawProviders = data.providers || data.api_config?.providers || [];
    const sortedProviders = sortProvidersByWeight(Array.isArray(rawProviders) ? rawProviders : []);
    setProviders(sortedProviders);

    const globalPrefs = data.preferences || data.api_config?.preferences || {};
    setApiConfigPreferences(globalPrefs);
    setGlobalModelPrice(globalPrefs.model_price || {});
    if (!options.syncVirtualModels) return;

    const loaded = globalPrefs.virtual_models || {};
    setLoadedVirtualModels(loaded);
    setLoadedVirtualModelsVersion(prev => prev + 1);
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
        let changed = false;
        for (const prov of Object.keys(next)) {
          for (const key of Object.keys(next[prov])) {
            const entry = next[prov][key];
            if (entry.remaining > 0) {
              next[prov] = { ...next[prov], [key]: { ...entry, remaining: entry.remaining - 1 } };
              changed = true;
              if (entry.remaining - 1 <= 0) anyExpired = true;
            }
          }
        }
        // 修改原因：倒计时每秒都返回新对象，即使没有任何冷却中的 key 也会触发整页重渲染。
        // 修改方式：没有实际递减时返回原引用，保持 state 不变。
        // 目的：消除每秒一次的无意义整页重渲染。
        if (!changed) return prev;
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


  const queryAllBalances = async (context: { formData: ProviderFormData | null; isOAuthEngine: boolean; setOauthAccounts?: any } | null, silent = false) => {
    // 修改原因：编辑表单状态已迁移到 useChannelEditor，但余额查询的 loading 与结果仍属于核心运行时数据。
    // 修改方式：由调用方传入当前 formData、OAuth 标记和可选账号状态 setter，核心函数继续负责请求、并发和结果归一化。
    // 目的：在不改变余额业务逻辑的前提下，解除 useChannelsCore 对编辑器表单 state 的直接依赖。
    const formData = context?.formData ?? null;
    const isOAuthEngine = !!context?.isOAuthEngine;
    const setOauthAccounts: any = context?.setOauthAccounts || (() => undefined);
    if (silent && formData?.provider) {
      // 修改原因：编辑面板打开时不再自动请求余额，但仍需要保留最近一次手动查询的展示结果。
      // 修改方式：silent 调用只读取 provider 维度的 localStorage 缓存，10 分钟内命中则直接写回 balanceResults 并结束。
      // 目的：避免打开面板触发 /v1/channels/balance，同时让已有余额信息可以快速恢复显示。
      const cacheKey = `zoaholic_balance_${formData.provider}`;
      const cached = localStorage.getItem(cacheKey);
      if (cached) {
        try {
          const { results: cachedResults, timestamp } = JSON.parse(cached);
          if (Date.now() - timestamp < 10 * 60 * 1000) {
            setBalanceResults(cachedResults);
            return;
          }
        } catch { /* 缓存损坏忽略 */ }
      }
    }
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
            setOauthAccounts((prev: Record<string, any>) => {
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
    if (formData.provider) {
      // 修改原因：余额查询改为纯手动触发后，下一次打开编辑面板应复用最近一次手动查询结果。
      // 修改方式：手动查询完成后把当前 provider 的结果和写入时间存到 localStorage。
      // 目的：让手动查询按钮继续负责刷新数据，面板打开只负责读取缓存而不发起网络请求。
      const cacheKey = `zoaholic_balance_${formData.provider}`;
      try {
        localStorage.setItem(cacheKey, JSON.stringify({ results, timestamp: Date.now() }));
      } catch { /* localStorage 不可写时忽略，余额手动查询结果仍已显示 */ }
    }
    setBalanceLoading(false);
  };


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

  const providerListItems = useMemo(() => {
    // 修改原因：虚拟模型已由手风琴单独收纳，主列表只应该参与真实渠道排序和真实渠道操作。
    // 修改方式：调用 helper 只生成真实渠道条目，并保留原始 providers 下标。
    // 目的：避免虚拟模型进入不活跃分段、真实渠道编辑和删除逻辑。
    return buildProviderListItems(providers);
  }, [providers]);

  // ── 可用引擎列表和分组列表（从当前真实渠道数据中提取）──
  const availableEngines = useMemo(() => {
    // 修改原因：虚拟模型状态已迁移到 useVirtualModels，核心 hook 只应该统计真实 provider 的 engine。
    // 修改方式：这里不再读取 virtualProviderEntries，虚拟 hook 会在页面组合层追加“虚拟路由”筛选项。
    // 目的：让 useChannelsCore 与虚拟模型草稿状态解耦，同时保留真实渠道筛选逻辑。
    const set = new Set<string>();
    providers.forEach(p => set.add(p.engine || 'openai'));
    return Array.from(set).sort();
  }, [providers]);

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
  const totalListItemCount = providerListItems.length;
  const visibleListItemCount = filteredProviders.length;

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


  return {
    providers, setProviders,
    providerActivity, setProviderActivity,
    channelTypes, setChannelTypes,
    allPlugins, setAllPlugins,
    loading, setLoading,
    balanceResults, setBalanceResults,
    balanceLoading, setBalanceLoading,
    focusedKeyIdx, setFocusedKeyIdx,
    forceListMode, setForceListMode,
    globalModelPrice, setGlobalModelPrice,
    apiConfigPreferences, setApiConfigPreferences,
    loadedVirtualModels, setLoadedVirtualModels,
    loadedVirtualModelsVersion,
    runtimeKeyStatus, setRuntimeKeyStatus,
    localCountdowns, setLocalCountdowns,
    filterKeyword, setFilterKeyword,
    filterEngine, setFilterEngine,
    filterGroup, setFilterGroup,
    filterStatus, setFilterStatus,
    token,
    applyApiConfigData,
    refreshProviders,
    refreshSingleProvider,
    fetchInitialData,
    refreshKeyStatus,
    queryAllBalances,
    getProviderModelNames,
    providerListItems,
    availableEngines,
    availableGroups,
    getProviderAnalyticsName,
    filteredProviders,
    getMatchedModels,
    hasActiveFilters,
    totalListItemCount,
    visibleListItemCount,
    isProviderInactive,
    expandedInactiveGroups, setExpandedInactiveGroups,
    toggleInactiveGroup,
    segments,
  };
}
