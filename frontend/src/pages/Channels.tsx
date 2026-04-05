/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState, KeyboardEvent, ClipboardEvent } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import {
  Plus, Edit, Brain, Trash2, ArrowRight, RefreshCw,
  Server, X, CheckCircle2, Settings2, Copy, ToggleRight, ToggleLeft,
  Folder, Puzzle, Network, CopyCheck, Power, Files, Play,
  Search, Check, BarChart3, Wallet
} from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import * as Switch from '@radix-ui/react-switch';
import { InterceptorSheet } from '../components/InterceptorSheet';
import { ChannelTestDialog } from '../components/ChannelTestDialog';
import { ApiKeyTestDialog } from '../components/ApiKeyTestDialog';
import { ChannelAnalyticsSheet } from '../components/ChannelAnalyticsSheet';
import { ProviderLogo } from '../components/ProviderLogos';

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

const SCHEDULE_ALGORITHMS = [
  { value: 'round_robin', label: '轮询 (Round Robin)' },
  { value: 'fixed_priority', label: '固定优先级 (Fixed)' },
  { value: 'random', label: '随机 (Random)' },
  { value: 'smart_round_robin', label: '智能轮询 (Smart)' },
];

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

// ── 冷却中 Key 行组件 ──
function CoolingKeyRow({ idx, keyObj, remainSec, focused, onFocus, onBlur, onRecover, onToggle, onTest, onDelete }: {
  idx: number; keyObj: { key: string; disabled: boolean }; remainSec: number;
  focused: boolean;
  onFocus: () => void; onBlur: () => void;
  onRecover: () => void; onToggle: () => void; onTest: () => void; onDelete: () => void;
}) {
  return (
    <div className={`relative flex items-center gap-2 px-3 py-2 rounded-lg border overflow-hidden transition-colors ${focused ? 'border-blue-500 bg-muted/50' : 'border-red-900/60 bg-zinc-900'}`}>
      <span className="text-xs text-muted-foreground w-4 text-right relative z-[2]">{idx + 1}</span>
      <div className="flex-1 min-w-0 relative z-[2]" style={!focused ? { WebkitMaskImage: 'linear-gradient(to right, black 0%, black 60%, transparent 100%)', maskImage: 'linear-gradient(to right, black 0%, black 60%, transparent 100%)' } : undefined}>
        <input
          type="text" value={keyObj.key || ''} readOnly placeholder="sk-..."
          onFocus={onFocus} onBlur={onBlur}
          className={`w-full bg-transparent border-none text-sm font-mono outline-none ${focused ? 'text-foreground' : 'text-red-300 line-through decoration-red-500/40'}`}
        />
      </div>
      {!focused && (
        <>
          <span className="flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded text-red-400 bg-red-500/10 relative z-[2]">
            {formatCountdown(remainSec)}
          </span>
          <button onClick={onRecover} className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 cursor-pointer flex-shrink-0 relative z-[2]">恢复</button>
        </>
      )}
      <div className="actions flex items-center gap-1 flex-shrink-0 relative z-[2]">
        <button onClick={onToggle} className="text-muted-foreground" title="禁用"><ToggleRight className="w-5 h-5" /></button>
        <button onClick={onTest} disabled={!keyObj.key.trim()} className="text-blue-600 dark:text-blue-400 disabled:opacity-50"><Play className="w-4 h-4" /></button>
        <button onClick={onDelete} className="text-red-500 hover:text-red-400 ml-1"><Trash2 className="w-4 h-4" /></button>
      </div>
    </div>
  );
}


export default function Channels() {
  const [providers, setProviders] = useState<any[]>([]);
  const [channelTypes, setChannelTypes] = useState<ChannelOption[]>([]);
  const [allPlugins, setAllPlugins] = useState<PluginOption[]>([]);
  const [loading, setLoading] = useState(true);

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [originalIndex, setOriginalIndex] = useState<number | null>(null);
  const [formData, setFormData] = useState<ProviderFormData | null>(null);

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
  const [overridesJson, setOverridesJson] = useState('');
  const [statusCodeOverridesJson, setStatusCodeOverridesJson] = useState('');
  const [modelDisplayKey, setModelDisplayKey] = useState(0);
  const [analyticsOpen, setAnalyticsOpen] = useState(false);
  const [analyticsProvider, setAnalyticsProvider] = useState('');

  // ── 余额查询 ──
  const [balanceResults, setBalanceResults] = useState<Record<string, BalanceResult>>({});
  const [balanceLoading, setBalanceLoading] = useState(false);
  const [focusedKeyIdx, setFocusedKeyIdx] = useState<number | null>(null);

  const [isFetchModelsOpen, setIsFetchModelsOpen] = useState(false);
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(() => new Set());
  const [modelSearchQuery, setModelSearchQuery] = useState('');

  // ── Key 运行时状态 ──
  const [runtimeKeyStatus, setRuntimeKeyStatus] = useState<Record<string, { auto_disabled: { key: string; remaining_seconds: number; duration: number; reason: string }[]; cooling: any[] }>>({});
  const [localCountdowns, setLocalCountdowns] = useState<Record<string, Record<string, { remaining: number; duration: number }>>>({}); // provider -> key -> {remaining, duration}

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

      const [configRes, typesRes, pluginsRes] = await Promise.all([
        apiFetch('/v1/api_config', { headers }),
        apiFetch('/v1/channels', { headers }),
        apiFetch('/v1/plugins/interceptors', { headers })
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
      }
      if (typesRes.ok) {
        const data = await typesRes.json();
        setChannelTypes(data.channels || []);
      }
      if (pluginsRes.ok) {
        const data = await pluginsRes.json();
        setAllPlugins(data.interceptor_plugins || []);
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
          enabled_plugins: Array.isArray(basePreferences.enabled_plugins) ? basePreferences.enabled_plugins : [],
        },
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
        preferences: { weight: 10, cooldown_period: 3, api_key_schedule_algorithm: 'round_robin', tools: true, enabled_plugins: [] }
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

  // ── 查询所有 Key 余额 ──
  const queryAllBalances = async (silent = false) => {
    if (!formData || !formData.base_url) return;
    const balanceCfg = formData.preferences?.balance;
    if (!balanceCfg) { if (!silent) alert('该渠道未配置余额查询（preferences.balance）'); return; }

    const activeKeys = formData.api_keys.filter(k => k.key.trim() && !k.disabled);
    if (activeKeys.length === 0) { if (!silent) alert('没有可用的 Key'); return; }

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
    alert('已复制所有有效密钥');
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
      alert('请先填写 Base URL 和至少一个启用的 API Key');
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
        alert(`获取模型失败: ${err.detail || err.error || err.message || res.status}`);
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
        alert('未获取到任何模型');
        return;
      }

      setFetchedModels(uniqueModels);
      const existing = new Set(formData.models);
      setSelectedModels(new Set(uniqueModels.filter(m => existing.has(m))));
      setIsFetchModelsOpen(true);
    } catch (err: any) {
      alert(`获取模型失败: ${err?.message || '网络错误'}`);
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
      alert(`${fieldName} JSON 格式错误: ${err.message}`);
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
        alert(`已删除渠道 "${name}"`);
      } else {
        alert('删除失败');
      }
    } catch {
      alert('网络错误');
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
        alert('操作失败');
      }
    } catch {
      alert('网络错误');
    }
  };

  const handleCopyProvider = (provider: any) => {
    const copy = JSON.parse(JSON.stringify(provider));
    const originalName = copy.provider || 'channel';
    copy.provider = `${originalName}_copy`;
    openModal(copy, null);
    alert('已复制渠道配置，请修改后保存');
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

  const openTestDialog = (provider: any) => {
    setTestingProvider(provider);
    setTestDialogOpen(true);
  };

  const openKeyTestDialog = (initialIndex: number | null = null) => {
    setKeyTestInitialIndex(initialIndex);
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
        headers: headersObj,
        post_body_parameter_overrides: overridesObj,
        status_code_overrides: statusCodeOverridesObj,
      },
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
    if (!formData || !indices.length) return;
    const set = new Set(indices);
    const next = formData.api_keys.map((k, idx) => set.has(idx) ? ({ ...k, disabled: true }) : k);
    updateFormData('api_keys', next);
  };

  const handleSave = async () => {
    if (!formData?.provider) {
      alert("渠道名称为必填项");
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
      alert("高级配置 JSON 格式错误");
      return;
    }

    let statusCodeOverridesObj: Record<string, number> | undefined;
    try {
      if (statusCodeOverridesJson.trim()) statusCodeOverridesObj = JSON.parse(statusCodeOverridesJson) as Record<string, number>;
    } catch {
      alert("错误码映射 JSON 格式错误");
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
        headers: headersObj,
        post_body_parameter_overrides: overridesObj,
        status_code_overrides: statusCodeOverridesObj,
      },
    };

    const newProviders = [...providers];
    if (originalIndex !== null) newProviders[originalIndex] = targetProvider;
    else newProviders.push(targetProvider);

    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ providers: newProviders }),
      });

      if (res.ok) {
        // 保存后重新排序
        setProviders(sortByWeight(newProviders));
        setIsModalOpen(false);
      } else {
        alert("保存失败");
      }
    } catch {
      alert("网络错误");
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
            <button onClick={() => { setAnalyticsProvider(p.provider); setAnalyticsOpen(true); }} className="p-1.5 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-500/10 rounded-md transition-colors" title="分析">
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
        <button onClick={() => openModal()} className="bg-primary hover:bg-primary/90 text-primary-foreground px-4 py-2 rounded-lg flex items-center gap-2 font-medium transition-colors w-full sm:w-auto justify-center">
          <Plus className="w-4 h-4" />
          添加渠道
        </button>
      </div>

      {/* Mobile Card List */}
      <div className="md:hidden space-y-4">
        {loading ? (
          <div className="p-8 text-center text-muted-foreground">加载中...</div>
        ) : providers.length === 0 ? (
          <div className="p-12 text-center text-muted-foreground">暂无渠道配置，点击上方按钮添加。</div>
        ) : (
          providers.map((p, idx) => <ProviderCard key={idx} p={p} idx={idx} />)
        )}
      </div>

      {/* Desktop Table */}
      <div className="hidden md:block bg-card border border-border rounded-xl overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-muted-foreground">加载中...</div>
        ) : providers.length === 0 ? (
          <div className="p-12 text-center text-muted-foreground">暂无渠道配置，点击右上角添加。</div>
        ) : (
          <table className="w-full text-left border-collapse table-fixed">
            <thead className="bg-muted border-b border-border text-muted-foreground text-sm font-medium">
              <tr>
                <th className="px-4 py-3 w-[18%]">名称</th>
                <th className="px-4 py-3 w-[15%]">分组 / 类型</th>
                <th className="px-4 py-3 w-[8%] text-center">Keys</th>
                <th className="px-4 py-3 w-[10%]">插件</th>
                <th className="px-4 py-3 w-[10%] text-center">状态</th>
                <th className="px-4 py-3 w-[10%] text-center">权重</th>
                <th className="px-4 py-3 w-[29%] text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border text-sm">
              {providers.map((p, idx) => {
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

                return (
                  <tr key={idx} className={`transition-colors ${isEnabled ? 'hover:bg-muted/50' : 'bg-muted/30 opacity-60'}`}>
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
                        <button onClick={() => { setAnalyticsProvider(p.provider); setAnalyticsOpen(true); }} className="p-1.5 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-500/10 rounded-md transition-colors" title="分析">
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
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Editor Side Sheet - Responsive */}
      <Dialog.Root open={isModalOpen} onOpenChange={setIsModalOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40 animate-in fade-in duration-200" />
          <Dialog.Content className="fixed right-0 top-0 h-full w-full sm:w-[560px] bg-background border-l border-border shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300">
            <div className="p-4 sm:p-5 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
              <Dialog.Title className="text-lg sm:text-xl font-bold text-foreground flex items-center gap-2">
                <Server className="w-5 h-5 text-primary" />
                {originalIndex !== null ? `编辑: ${formData?.provider}` : '新增渠道'}
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
                        <label className="text-sm font-medium text-foreground mb-1.5 block">渠道标识 (Provider)</label>
                        <input type="text" value={formData.provider} onChange={e => updateFormData('provider', e.target.value)} placeholder="e.g. openai" className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm outline-none text-foreground" />
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
                      <input type="text" value={formData.model_prefix} onChange={e => updateFormData('model_prefix', e.target.value)} placeholder="例如 azure- 或 aws/" className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono outline-none text-foreground" />
                    </div>
                    <div className="flex items-center justify-between p-3 bg-muted/50 rounded-lg border border-border">
                      <span className="text-sm font-medium text-foreground">启用该渠道</span>
                      <Switch.Root checked={formData.enabled} onCheckedChange={val => updateFormData('enabled', val)} className="w-11 h-6 bg-muted rounded-full relative data-[state=checked]:bg-emerald-500 transition-colors">
                        <Switch.Thumb className="block w-5 h-5 bg-white rounded-full shadow-md transition-transform translate-x-0.5 data-[state=checked]:translate-x-[22px]" />
                      </Switch.Root>
                    </div>
                    <div>
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
                    </div>
                  </div>
                </section>

                {/* 2. API Keys */}
                <section>
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
                          {!isFocused && isPermanent && <span className="text-[10px] px-1.5 py-0.5 rounded bg-zinc-700/50 text-zinc-400 flex-shrink-0 relative z-[2]">永久禁用</span>}
                          {!isFocused && isPermanent && (
                            <button onClick={async () => { await apiFetch('/v1/channels/key_status/re_enable', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ provider: providerName, key: keyObj.key }) }); refreshKeyStatus(); }} className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 cursor-pointer flex-shrink-0 relative z-[2]">恢复</button>
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
                </section>

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

                {/* 5. 路由与限流 */}
                <section>
                  <div className="flex items-center gap-2 text-sm font-semibold text-foreground mb-4 border-b border-border pb-2">
                    <Network className="w-4 h-4 text-yellow-500" /> 路由与限流
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">渠道权重 (Weight)</label>
                      <input type="number" value={formData.preferences.weight || ''} onChange={e => updatePreference('weight', Number(e.target.value))} className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
                    </div>
                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">错误冷却 (秒)</label>
                      <input type="number" value={formData.preferences.cooldown_period || ''} onChange={e => updatePreference('cooldown_period', Number(e.target.value))} className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
                    </div>
                    <div className="col-span-1 sm:col-span-2">
                      <label className="text-sm font-medium text-foreground mb-1.5 block">Key 调度策略</label>
                      <select value={formData.preferences.api_key_schedule_algorithm} onChange={e => updatePreference('api_key_schedule_algorithm', e.target.value)} className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground">
                        {SCHEDULE_ALGORITHMS.map(a => <option key={a.value} value={a.value}>{a.label}</option>)}
                      </select>
                    </div>
                    <div className="col-span-1 sm:col-span-2">
                      <label className="text-sm font-medium text-foreground mb-1.5 block">错误码映射 (JSON)</label>
                      <textarea
                        value={statusCodeOverridesJson}
                        onChange={e => setStatusCodeOverridesJson(e.target.value)}
                        onBlur={() => formatJsonOnBlur(statusCodeOverridesJson, setStatusCodeOverridesJson, '错误码映射')}
                        rows={2}
                        placeholder='{"529": 429, "520": 502}'
                        className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm font-mono focus:border-primary outline-none text-foreground"
                      />
                      <p className="text-xs text-muted-foreground mt-1">将上游非标准状态码映射为标准码以触发正确的重试策略。例如 529→429 使其按限流退避处理。</p>
                    </div>
                    {/* 自动禁用配置 */}
                    <div className="col-span-1 sm:col-span-2 border-t border-border pt-4">
                      <div className="flex items-center justify-between mb-3">
                        <label className="text-sm font-medium text-foreground flex items-center gap-1.5">
                          <Power className="w-3.5 h-3.5 text-red-500" /> Key 自动禁用
                        </label>
                        <Switch.Root
                          checked={!!formData.preferences.auto_disable_key}
                          onCheckedChange={val => {
                            if (val) {
                              updatePreference('auto_disable_key', { status_codes: [401, 403], keywords: [], duration: 0 });
                            } else {
                              // eslint-disable-next-line @typescript-eslint/no-unused-vars
                              const { auto_disable_key: _, ...rest } = formData.preferences;
                              updateFormData('preferences', rest);
                            }
                          }}
                          className="w-9 h-5 bg-muted rounded-full relative data-[state=checked]:bg-red-500 transition-colors"
                        >
                          <Switch.Thumb className="block w-4 h-4 bg-white rounded-full shadow-md transition-transform translate-x-0.5 data-[state=checked]:translate-x-[18px]" />
                        </Switch.Root>
                      </div>
                      <p className="text-xs text-muted-foreground mb-3">当 Key 请求返回指定错误码或响应包含指定关键词时，自动将其禁用一段时间或永久禁用。运行时状态不持久化，重启后重置。</p>
                      {formData.preferences.auto_disable_key && (
                        <div className="space-y-3 pl-1">
                          <div>
                            <label className="text-xs font-medium text-muted-foreground mb-1 block">触发状态码</label>
                            <input
                              type="text"
                              value={(formData.preferences.auto_disable_key.status_codes || []).join(', ')}
                              onChange={e => {
                                const codes = e.target.value.split(/[,，\s]+/).map(s => parseInt(s.trim())).filter(n => !isNaN(n));
                                updatePreference('auto_disable_key', { ...formData.preferences.auto_disable_key, status_codes: codes });
                              }}
                              placeholder="401, 403"
                              className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs font-mono focus:border-primary outline-none text-foreground"
                            />
                          </div>
                          <div>
                            <label className="text-xs font-medium text-muted-foreground mb-1 block">触发关键词（响应体包含，逗号分隔）</label>
                            <input
                              type="text"
                              value={(formData.preferences.auto_disable_key.keywords || []).join(', ')}
                              onChange={e => {
                                const kws = e.target.value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
                                updatePreference('auto_disable_key', { ...formData.preferences.auto_disable_key, keywords: kws });
                              }}
                              placeholder="insufficient_quota, billing"
                              className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs font-mono focus:border-primary outline-none text-foreground"
                            />
                          </div>
                          <div>
                            <label className="text-xs font-medium text-muted-foreground mb-1 block">禁用时长（秒，0 = 永久）</label>
                            <input
                              type="number"
                              value={formData.preferences.auto_disable_key.duration ?? 0}
                              onChange={e => {
                                updatePreference('auto_disable_key', { ...formData.preferences.auto_disable_key, duration: Math.max(0, parseInt(e.target.value) || 0) });
                              }}
                              min={0}
                              placeholder="0"
                              className="w-full bg-background border border-border px-3 py-1.5 rounded-lg text-xs font-mono focus:border-primary outline-none text-foreground"
                            />
                            <p className="text-xs text-muted-foreground mt-1">设为 0 表示永久禁用（需手动恢复）。设为正数则为冷却秒数，到期后自动恢复。</p>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </section>

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

                    <div>
                      <label className="text-sm font-medium text-foreground mb-1.5 block">代理 (Proxy)</label>
                      <input type="url" value={formData.preferences.proxy || ''} onChange={e => updatePreference('proxy', e.target.value)} placeholder="http://127.0.0.1:7890" className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
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
        <InterceptorSheet
          open={showPluginSheet}
          onOpenChange={setShowPluginSheet}
          allPlugins={allPlugins}
          enabledPlugins={formData.preferences.enabled_plugins || []}
          providerPreferences={formData.preferences || {}}
          onUpdate={handlePluginSheetUpdate}
        />
      )}

      {formData && (
        <ApiKeyTestDialog
          open={keyTestDialogOpen}
          onOpenChange={setKeyTestDialogOpen}
          title={`测试 API Keys: ${formData.provider || '未命名渠道'}`}
          engine={formData.engine || 'openai'}
          base_url={formData.base_url || ''}
          provider_snapshot={buildProviderSnapshotForTest()}
          apiKeys={formData.api_keys}
          availableModels={getProviderModelNameListForUi()}
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
