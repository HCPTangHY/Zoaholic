import { useState, useEffect, useMemo, type ReactNode } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import { toastSuccess, toastError, toastWarning } from '../components/Toast';
import {
  Key, Plus, RefreshCw, Copy, Trash2, Edit, Save, X, Search,
  Folder, CheckCircle2, AlertCircle, AlertTriangle,
  Wand2, Wallet, Brain, Download, Check, Ban, BarChart3,
  ShieldCheck, Puzzle, ArrowRight, Smartphone, PackageCheck,
  Globe, Zap
} from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import { KeyAnalyticsSheet } from '../components/KeyAnalyticsSheet';
import { QuotaArcs } from './channels/components/QuotaComponents';
import { InterceptorSheet } from '../components/InterceptorSheet';
import { PluginParamsForm, type ParamSchema } from '../components/PluginParamsForm';

// ========== Types ==========
interface ApiKeyData {
  api: string;
  name?: string;
  role?: string;
  groups?: string[];
  group?: string;
  model?: string[];
  ip_blacklist?: string[];
  preferences?: {
    credits?: number;
    created_at?: string;
    rate_limit?: string | Record<string, string>;
    // 修改原因：Phase 2 统一配额前端需要编辑 preferences.quota，而旧类型只声明了 credits 和 rate_limit。
    // 修改方式：在 preferences 类型中增加 quota 字段，并保留索引签名兼容其它旧配置项。
    // 目的：让 Admin 编辑器能以类型安全的方式读写统一配额规则。
    quota?: Record<string, string>;
    name?: string;
    group?: string;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    [key: string]: any;
  };
}

interface ApiKeyState {
  enabled: boolean;
  credits: number | null;
  total_cost: number;
  created_at: string;
}

interface QuotaDisplayItem {
  label: string;
  current: number;
  limit: number;
  remaining: number;
  scope: string;
  metric: string;
  qualifier: string;
  groupLabel: string;
}

interface QuotaDisplayGroup {
  label: string;
  items: QuotaDisplayItem[];
}

type QuotaScopeValue = 'key' | 'ip';
type QuotaMetricValue = 'request' | 'cost' | 'token' | 'token_in' | 'token_out' | 'unique_ip';

const QUOTA_SCOPE_OPTIONS: { value: QuotaScopeValue; label: string }[] = [
  { value: 'key', label: 'Key 级' },
  { value: 'ip', label: 'Per-IP' },
];

const QUOTA_METRIC_OPTIONS: { value: QuotaMetricValue; label: string; scopes: QuotaScopeValue[] }[] = [
  { value: 'request', label: '请求次数', scopes: ['key', 'ip'] },
  { value: 'cost', label: '金额', scopes: ['key', 'ip'] },
  { value: 'token', label: '总 Token', scopes: ['key', 'ip'] },
  { value: 'token_in', label: '输入 Token', scopes: ['key', 'ip'] },
  { value: 'token_out', label: '输出 Token', scopes: ['key', 'ip'] },
  { value: 'unique_ip', label: '不同 IP 数量', scopes: ['key'] },
];

const QUOTA_SCOPE_LABELS: Record<string, string> = {
  key: 'Key 级',
  ip: 'Per-IP',
  model: '模型',
};

const QUOTA_METRIC_LABELS: Record<string, string> = {
  request: '请求次数',
  cost: '金额',
  token: '总 Token',
  token_in: '输入 Token',
  token_out: '输出 Token',
  unique_ip: '不同 IP',
};

function parseQuotaConfigKey(key: string): { scope: QuotaScopeValue; metric: QuotaMetricValue; qualifier: string } {
  // 修改原因：quota 配置已经从扁平 key 迁移到 Scope × Metric，但旧 default、ip:rate、ip:max 仍要能打开编辑。
  // 修改方式：把配置 key 归一为前端表单使用的 scope、metric、qualifier，再由 buildQuotaConfigKey 写回新格式。
  // 目的：让旧配置自动进入新选择器界面，同时保存后生成后端 parser 可识别的正交规则。
  const raw = (key || '').trim();
  if (raw === 'ip:max') return { scope: 'key', metric: 'unique_ip', qualifier: 'default' };
  if (raw.startsWith('ip:')) {
    const metric = raw.slice(3) === 'rate' ? 'request' : raw.slice(3);
    return { scope: 'ip', metric: (metric || 'request') as QuotaMetricValue, qualifier: 'default' };
  }
  if (raw === 'default' || raw === 'request' || raw === '') return { scope: 'key', metric: 'request', qualifier: 'default' };
  const [metric, qualifier] = raw.split(':', 2);
  if (['cost', 'token', 'token_in', 'token_out'].includes(metric)) {
    return { scope: 'key', metric: metric as QuotaMetricValue, qualifier: qualifier || 'default' };
  }
  return { scope: 'key', metric: 'request', qualifier: raw || 'default' };
}

function buildQuotaConfigKey(scope: QuotaScopeValue, metric: QuotaMetricValue, qualifier = 'default'): string {
  // 修改原因：编辑器现在由 scope 和 metric 两个选择器驱动，不能再让用户手写容易出错的内部 key。
  // 修改方式：按后端 parser 的约定生成 request、cost、ip:request、ip:cost、ip:max 等配置 key。
  // 目的：保证 UI 选择结果与 Scope × Metric 配置格式一致。
  const q = (qualifier || 'default').trim() || 'default';
  if (metric === 'unique_ip') return 'ip:max';
  if (scope === 'ip') return `ip:${metric}`;
  if (metric === 'request') return q === 'default' ? 'request' : q;
  return q === 'default' ? metric : `${metric}:${q}`;
}

function parseQuotaStatusKey(key: string): { scope: string; metric: string; qualifier: string } {
  // 修改原因：后端新状态 key 是 scope:metric:qualifier，但旧运行时曾返回 request:default、ip_max:default 等格式。
  // 修改方式：优先解析三段式新 key，无法匹配时回退到旧 key 映射。
  // 目的：让 Admin 列表在后端升级期间兼容新旧响应。
  const parts = key.split(':');
  if (parts.length >= 3 && ['key', 'ip', 'model'].includes(parts[0])) {
    return { scope: parts[0], metric: parts[1], qualifier: parts.slice(2).join(':') || 'default' };
  }
  if (key.startsWith('ip_max:')) return { scope: 'key', metric: 'unique_ip', qualifier: 'default' };
  if (key.startsWith('ip_rate:')) return { scope: 'ip', metric: 'request', qualifier: 'default' };
  const [metric, qualifier] = key.split(':', 2);
  return { scope: 'key', metric: metric || 'request', qualifier: qualifier || 'default' };
}

function getQuotaStatusKeyFromConfig(key: string): string {
  const parsed = parseQuotaConfigKey(key);
  const metric = parsed.metric === 'unique_ip' ? 'unique_ip' : parsed.metric;
  return `${parsed.scope}:${metric}:${parsed.scope === 'ip' ? 'default' : parsed.qualifier}`;
}

// ── Key Pipeline 辅助组件（复制自 PipelineView.tsx） ──
// 修改原因：Admin.tsx 的 Key Pipeline 面板原先使用 select 下拉和简陋卡片，与渠道面板风格不一致。
// 修改方式：从 PipelineView.tsx 抄入 DetailPanel、PluginCard、PluginAddDropdown 及 parseEnabledPlugin/buildEnabledPluginValue。
// 目的：让 Key 的入站/出站面板与渠道 Pipeline 使用完全一致的 UI 组件。

function parseEnabledPlugin(value: string) {
  const colonIndex = value.indexOf(':');
  if (colonIndex < 0) return { name: value, opts: undefined, hasOpts: false };
  return { name: value.slice(0, colonIndex), opts: value.slice(colonIndex + 1), hasOpts: true };
}

function buildEnabledPluginValue(name: string, opts: string) {
  const trimmedOpts = opts.trim();
  return trimmedOpts ? `${name}:${trimmedOpts}` : name;
}

function DetailPanel({ icon, title, desc, children }: { icon: ReactNode; title: string; desc: string; children: ReactNode }) {
  return (
    <>
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border">
        {icon && <span className="text-muted-foreground">{icon}</span>}
        <span className="text-sm font-semibold">{title}</span>
        <span className="text-xs text-muted-foreground ml-auto">{desc}</span>
      </div>
      <div className="p-4">{children}</div>
    </>
  );
}

function PluginCard({ name, opts, hasOpts, description, paramsSchema, paramsHint, onRemove, onOptsChange }: {
  name: string;
  opts?: string;
  hasOpts: boolean;
  description?: string;
  paramsSchema?: ParamSchema[];
  paramsHint?: string;
  onRemove: () => void;
  onOptsChange: (opts: string) => void;
}) {
  const schema = Array.isArray(paramsSchema) ? paramsSchema : [];
  const shouldShowParams = schema.length > 0 || hasOpts;

  return (
    <div className="bg-card border border-border rounded-md px-3 py-2">
      <div className="flex items-start gap-1.5">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 flex-shrink-0 mt-1.5" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 min-w-0">
            <span className="text-xs font-semibold truncate">{name}</span>
            {description && <span className="text-[10px] text-muted-foreground truncate ml-auto">{description}</span>}
          </div>
          {opts && (
            <div className="flex gap-1 mt-1.5 flex-wrap">
              {opts.split(',').map((o: string, j: number) => (
                <span key={j} className="bg-primary/10 text-primary text-[10px] px-1.5 py-0.5 rounded font-mono">{o.trim()}</span>
              ))}
            </div>
          )}
          {shouldShowParams && (
            <div className="mt-1.5">
              <PluginParamsForm
                options={opts || ''}
                schema={schema}
                paramsHint={paramsHint}
                onChange={onOptsChange}
                size="compact"
              />
            </div>
          )}
        </div>
        <button type="button" onClick={onRemove} className="shrink-0 text-muted-foreground hover:text-destructive" title="移除插件">
          <X className="w-3 h-3" />
        </button>
      </div>
    </div>
  );
}

type KeyPluginStage = 'inbound' | 'outbound';

function PluginAddDropdown({ stage, allPlugins, enabledPluginNames, openMenu, setOpenMenu, onAdd, onOpenPluginSheet }: {
  stage: KeyPluginStage;
  allPlugins: any[];
  enabledPluginNames: Set<string>;
  openMenu: string | null;
  setOpenMenu: (stage: string | null) => void;
  onAdd: (pluginName: string) => void;
  onOpenPluginSheet: () => void;
}) {
  const isOpen = openMenu === stage;
  const candidates = useMemo(() => {
    // 修改原因：入站面板过滤有 inbound_interceptors 或 channel_inbound_interceptors 的插件；出站面板过滤有 key_outbound_interceptors 的插件。
    // 修改方式：根据 stage 选择过滤字段。
    // 目的：让 Key Pipeline 的快速添加菜单与渠道面板一样按阶段过滤。
    return allPlugins.filter((plugin: any) => {
      const pluginName = String(plugin?.plugin_name || plugin?.name || '').trim();
      if (!pluginName || enabledPluginNames.has(pluginName)) return false;
      if (stage === 'inbound') {
        return (plugin.inbound_interceptors?.length ?? 0) > 0 || (plugin.channel_inbound_interceptors?.length ?? 0) > 0;
      }
      return (plugin.key_outbound_interceptors?.length ?? 0) > 0;
    });
  }, [allPlugins, enabledPluginNames, stage]);

  return (
    <div className="relative inline-flex" data-plugin-add-menu>
      <button type="button" onClick={() => setOpenMenu(isOpen ? null : stage)} className="text-xs text-primary hover:text-primary/80 flex items-center gap-1">
        <Plus className="w-3.5 h-3.5" /> 添加
      </button>
      {isOpen && (
        <div className="absolute left-0 top-full z-30 mt-1 w-72 rounded-lg border border-border bg-card p-1 shadow-lg">
          <div className="max-h-56 overflow-y-auto py-1">
            {candidates.length === 0 ? (
              <div className="px-3 py-2 text-xs text-muted-foreground">没有可添加的插件。</div>
            ) : candidates.map((plugin: any) => (
              <button
                key={plugin.plugin_name || plugin.name}
                type="button"
                onClick={() => onAdd(plugin.plugin_name || plugin.name)}
                className="w-full rounded px-3 py-2 text-left hover:bg-muted"
              >
                <div className="text-xs font-medium text-foreground">{plugin.plugin_name || plugin.name}</div>
                {plugin.description && <div className="mt-0.5 truncate text-[10px] text-muted-foreground">{plugin.description}</div>}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => { setOpenMenu(null); onOpenPluginSheet(); }}
            className="mt-1 w-full rounded border-t border-border px-3 py-2 text-left text-xs text-primary hover:bg-muted"
          >
            完整配置 →
          </button>
        </div>
      )}
    </div>
  );
}

export default function Admin() {
  const { token } = useAuthStore();
  const [keys, setKeys] = useState<ApiKeyData[]>([]);
  const [keyStates, setKeyStates] = useState<Record<string, ApiKeyState>>({});
  // 修改原因：列表额度展示要优先使用 Phase 2 统一配额运行时状态，而不是旧 credits 余额状态。
  // 修改方式：单独保存 /v1/api_keys_states 返回的 quota_states，按 API Key 查询展示。
  // 目的：让配置了 quota 的 Key 可以显示 cost、request、token 等统一维度的剩余额度。
  const [quotaStates, setQuotaStates] = useState<Record<string, Record<string, any>>>({});
  const [loading, setLoading] = useState(true);
  const [analyticsKey, setAnalyticsKey] = useState<{api: string; name?: string} | null>(null);
  // 修改原因：Key Analytics 需要与 Channel Analytics 一样由列表按钮打开侧滑 Sheet，而不是用 analyticsKey 是否为空隐式控制。
  // 修改方式：增加独立 open 状态，关闭 Sheet 时再清空当前 Key。
  // 目的：让打开、关闭和切换 Key 的状态更清晰，避免后续增加关闭动画或复用组件时互相影响。
  const [analyticsOpen, setAnalyticsOpen] = useState(false);


  // Edit Sheet
  const [isSheetOpen, setIsSheetOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  // Form State
  const [formApi, setFormApi] = useState('');
  const [formName, setFormName] = useState('');
  const [formRole, setFormRole] = useState('');
  const [formGroups, setFormGroups] = useState<string[]>(['default']);
  const [formModels, setFormModels] = useState<string[]>([]);
  // 修改原因：credits、全局 rate_limit 和模型 rate_limit 已统一迁移到 preferences.quota。
  // 修改方式：编辑表单只维护 key/value 规则列表，保存时组装为 quota 对象。
  // 目的：让一个入口同时覆盖请求数、金额、Token 和 IP 等统一配额维度。
  const [formQuotaRules, setFormQuotaRules] = useState<{key: string; value: string}[]>([]);
  const [formModelLimits, setFormModelLimits] = useState<{key: string; value: string}[]>([]);
  const [formExcludedChannels, setFormExcludedChannels] = useState<string[]>([]);
  const [formExcludedModels, setFormExcludedModels] = useState<string[]>([]);
  const [formIpBlacklistText, setFormIpBlacklistText] = useState('');
  const [formEnabledPlugins, setFormEnabledPlugins] = useState<string[]>([]);
  const [formBasePreferences, setFormBasePreferences] = useState<Record<string, unknown>>({});
  const [formPluginPreferenceOverrides, setFormPluginPreferenceOverrides] = useState<Record<string, unknown>>({});
  const [formPluginPreferenceDeletes, setFormPluginPreferenceDeletes] = useState<string[]>([]);
  const [allPlugins, setAllPlugins] = useState<any[]>([]);
  const [activeKeyNode, setActiveKeyNode] = useState<string | null>(null);
  const [openAddMenu, setOpenAddMenu] = useState<string | null>(null);
  const [showPluginSheet, setShowPluginSheet] = useState(false);

  // Input states
  const [groupInput, setGroupInput] = useState('');
  const [modelInput, setModelInput] = useState('');
  const [excludedChannelInput, setExcludedChannelInput] = useState('');
  const [excludedModelInput, setExcludedModelInput] = useState('');

  // Credits Dialog
  const [isCreditsOpen, setIsCreditsOpen] = useState(false);
  const [creditsAmount, setCreditsAmount] = useState('');
  const [creditsTargetKey, setCreditsTargetKey] = useState('');

  // Fetch Models Dialog
  const [isFetchModelsOpen, setIsFetchModelsOpen] = useState(false);
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [modelSearchQuery, setModelSearchQuery] = useState('');
  const [fetchingModels, setFetchingModels] = useState(false);

  // ========== Data Loading ==========
  const parseIpBlacklistText = (text: string): string[] => {
    // 修改原因：IP 黑名单输入使用 textarea，每行一个规则，同时需要兼容用户粘贴逗号分隔内容。
    // 修改方式：按换行和逗号拆分，去除空白并去重后保存为字符串数组。
    // 目的：让前端提交结构与 api.yaml 中的 ip_blacklist 数组一致。
    return [...new Set(text.split(/[\n,]+/).map(s => s.trim()).filter(Boolean))];
  };

  const formatIpBlacklistText = (items: unknown): string => {
    // 修改原因：后端返回的 ip_blacklist 可能来自旧配置字符串或新配置数组。
    // 修改方式：数组按行展示，字符串原样展示，其他值视为空。
    // 目的：编辑时清晰展示一行一个 IP/CIDR，并保留旧配置兼容性。
    if (Array.isArray(items)) return items.map(item => String(item).trim()).filter(Boolean).join('\n');
    if (typeof items === 'string') return items.trim();
    return '';
  };

  const validateIpBlacklistEntries = (entries: string[]): boolean => {
    // 修改原因：浏览器端无法完整复用 Python ipaddress，过严校验会误伤 IPv6 压缩格式。
    // 修改方式：仅拦截空白、逗号和明显缺少 IP 主体的条目，严格校验交给后端 update_config。
    // 目的：避免前端拒绝合法 IPv6，同时仍减少一部分明显误输入。
    const invalid = entries.find(entry => /\s|,/.test(entry) || entry.startsWith('/') || entry.endsWith('/'));
    if (invalid) {
      toastWarning(`IP 黑名单条目格式不正确：${invalid}`);
      return false;
    }
    return true;
  };

  const fetchData = async () => {
    if (!token) return;
    setLoading(true);
    try {
      const headers = { Authorization: `Bearer ${token}` };
      const [configRes, statesRes, pluginsRes] = await Promise.all([
        apiFetch('/v1/api_config', { headers }),
        apiFetch('/v1/api_keys_states', { headers }),
        // 修改原因：Key Pipeline 需要读取各阶段拦截器和 metadata.params_schema，普通 /v1/plugins 不含这些字段。
        // 修改方式：与渠道面板一样使用 /v1/plugins/interceptors 作为插件能力来源。
        // 目的：让 Key 面板的添加菜单、阶段过滤和参数表单与渠道 Pipeline 完全一致。
        apiFetch('/v1/plugins/interceptors', { headers }).catch(() => null)
      ]);

      if (configRes.ok) {
        const config = await configRes.json();
        const apiConfig = config.api_config || config;
        setKeys(apiConfig.api_keys || []);

      }
      if (statesRes.ok) {
        const states = await statesRes.json();
        setKeyStates(states.api_keys_states || {});
        // 修改原因：后端现在会在旧 api_keys_states 外附加 quota_states，前端列表要同步缓存新状态。
        // 修改方式：读取 states.quota_states，不存在时回退为空对象，避免旧后端响应报错。
        // 目的：让列表展示可以优先使用统一配额运行时数据。
        setQuotaStates(states.quota_states || {});
      }
      if (pluginsRes?.ok) {
        const pd = await pluginsRes.json();
        setAllPlugins(Array.isArray(pd.interceptor_plugins) ? pd.interceptor_plugins : Array.isArray(pd.plugins) ? pd.plugins : Array.isArray(pd) ? pd : []);
      }
    } catch (err) {
      console.error('Failed to load API keys:', err);
    } finally {
      setLoading(false);
    }
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { fetchData(); }, []);

  // 修改原因：快速添加菜单用 absolute 定位弹出，需要点击外部时自动关闭。
  // 修改方式：openAddMenu 打开时监听 document mousedown，点击不在 data-plugin-add-menu 容器内则关闭。
  // 目的：保持与渠道 Pipeline 一致的 dropdown 交互体验。
  useEffect(() => {
    if (!openAddMenu) return;
    const closeOnOutsideClick = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target?.closest('[data-plugin-add-menu]')) setOpenAddMenu(null);
    };
    document.addEventListener('mousedown', closeOnOutsideClick);
    return () => document.removeEventListener('mousedown', closeOnOutsideClick);
  }, [openAddMenu]);

  const currentKeyPluginPreferences = useMemo(() => {
    // 修改原因：完整插件配置面板会写入 metadata.provider_config 指定的 preferences 字段。
    // 修改方式：以打开编辑器时的 preferences 为基线，叠加本次插件面板修改，并应用删除列表。
    // 目的：在保存 API Key 前，插件面板和主编辑表单共享同一份待保存 preferences。
    const merged: Record<string, unknown> = { ...formBasePreferences, ...formPluginPreferenceOverrides };
    formPluginPreferenceDeletes.forEach(key => { delete merged[key]; });
    return merged;
  }, [formBasePreferences, formPluginPreferenceDeletes, formPluginPreferenceOverrides]);

  const handleKeyPluginSheetUpdate = (payload: { enabled_plugins: string[]; preferences_patch: Record<string, unknown>; preferences_delete: string[] }) => {
    // 修改原因：Key 面板现在复用完整插件配置 Sheet，需要把启用插件和插件级 preferences 先写回本地表单状态。
    // 修改方式：enabled_plugins 直接替换；preferences_patch 进入 override；preferences_delete 从 override 中清除并加入删除列表。
    // 目的：让用户点击「保存插件配置」后仍可继续编辑 Key，最终由 API Key 保存按钮统一落盘。
    setFormEnabledPlugins(payload.enabled_plugins);
    const patchedKeys = Object.keys(payload.preferences_patch || {});
    setFormPluginPreferenceOverrides(prev => {
      const next = { ...prev, ...(payload.preferences_patch || {}) };
      (payload.preferences_delete || []).forEach(key => { delete next[key]; });
      return next;
    });
    setFormPluginPreferenceDeletes(prev => {
      const next = new Set(prev.filter(key => !patchedKeys.includes(key)));
      (payload.preferences_delete || []).forEach(key => {
        if (!patchedKeys.includes(key)) next.add(key);
      });
      return Array.from(next);
    });
  };

  const handleEditSheetOpenChange = (open: boolean) => {
    setIsSheetOpen(open);
    if (!open) {
      setShowPluginSheet(false);
      setOpenAddMenu(null);
      setActiveKeyNode(null);
    }
  };

  // ========== Sheet Handlers ==========
  const openSheet = (index: number | null = null, copyFrom: ApiKeyData | null = null) => {
    setEditingIndex(index);
    setGroupInput('');
    setModelInput('');
    setExcludedChannelInput('');
    setExcludedModelInput('');
    setOpenAddMenu(null);
    setActiveKeyNode(null);
    setShowPluginSheet(false);
    setFormPluginPreferenceOverrides({});
    setFormPluginPreferenceDeletes([]);

    let source: ApiKeyData | null = null;
    if (copyFrom) {
      source = JSON.parse(JSON.stringify(copyFrom));
      source!.api = '';
      source!.name = `${source!.name || 'Key'}_Copy`;
    } else if (index !== null) {
      source = keys[index];
    }

    const basePreferences = source?.preferences && typeof source.preferences === 'object'
      ? { ...source.preferences }
      : {};
    setFormBasePreferences(basePreferences);

    if (source) {
      setFormApi(source.api || '');
      setFormName(source.name || source.preferences?.name || '');
      setFormRole(source.role || '');

      // Parse groups
      let groups: string[] = ['default'];
      if (Array.isArray(source.groups) && source.groups.length > 0) {
        groups = source.groups;
      } else if (typeof source.group === 'string' && source.group.trim()) {
        groups = [source.group.trim()];
      } else if (source.preferences?.group) {
        groups = [source.preferences.group];
      }
      setFormGroups(groups);

      setFormModels(Array.isArray(source.model) ? [...source.model] : []);
      // 修改原因：编辑时需要优先加载新 quota 配置，同时兼容旧 credits 和 rate_limit 配置。
      // 修改方式：preferences.quota 存在时直接转成规则列表；否则把旧 rate_limit 和 credits 映射成 quota 规则。
      // 目的：旧配置打开后也能在统一配额编辑器中继续调整，并在保存时写入 preferences.quota。
      const quota = source.preferences?.quota;
      const rateLimitLegacy = source.preferences?.rate_limit;
      const creditsLegacy = source.preferences?.credits;

      const QUOTA_KEYS = new Set(['default', 'request', 'cost', 'token', 'token_in', 'token_out', 'ip:max', 'ip:rate', 'ip:request', 'ip:cost', 'ip:token', 'ip:token_in', 'ip:token_out']);
      const quotaRules: {key: string; value: string}[] = [];
      const modelLimits: {key: string; value: string}[] = [];
      if (quota && typeof quota === 'object') {
        Object.entries(quota).forEach(([k, v]) => {
          if (typeof v === 'string') {
            // 修改原因：quota key 已扩展为 Scope × Metric，旧判断漏掉 ip:cost、ip:token_out 和 request。
            // 修改方式：显式识别 key scope metric、ip scope metric 和 key scope 带 qualifier 的 metric。
            // 目的：编辑旧配置时把正交 quota 规则放入配额编辑器，仍把模型名规则放入模型限速栏。
            const isQuota = QUOTA_KEYS.has(k) || k.startsWith('cost:') || k.startsWith('token:') || k.startsWith('token_in:') || k.startsWith('token_out:');
            if (isQuota) {
              quotaRules.push({ key: k, value: v });
            } else {
              modelLimits.push({ key: k, value: v });
            }
          }
        });
      } else {
        if (typeof rateLimitLegacy === 'string') {
          quotaRules.push({ key: 'default', value: rateLimitLegacy });
        } else if (typeof rateLimitLegacy === 'object' && rateLimitLegacy) {
          Object.entries(rateLimitLegacy).forEach(([k, v]) => {
            if (typeof v === 'string') {
              if (k === 'default') {
                quotaRules.push({ key: k, value: v });
              } else {
                modelLimits.push({ key: k, value: v });
              }
            }
          });
        }
        if (creditsLegacy !== undefined && creditsLegacy !== null && creditsLegacy >= 0) {
          quotaRules.push({ key: 'cost', value: `${creditsLegacy}/inf` });
        }
      }
      setFormQuotaRules(quotaRules);
      setFormModelLimits(modelLimits);
      // 黑名单
      const ec = source.preferences?.excluded_channels;
      setFormExcludedChannels(Array.isArray(ec) ? [...ec] : (typeof ec === 'string' && ec.trim() ? ec.split(',').map((s: string) => s.trim()).filter(Boolean) : []));
      const em = source.preferences?.excluded_models;
      setFormExcludedModels(Array.isArray(em) ? [...em] : (typeof em === 'string' && em.trim() ? em.split(',').map((s: string) => s.trim()).filter(Boolean) : []));
      setFormIpBlacklistText(formatIpBlacklistText(source.ip_blacklist));
      setFormEnabledPlugins(Array.isArray(source.preferences?.enabled_plugins) ? [...source.preferences.enabled_plugins] : []);
    } else {
      setFormApi('');
      setFormName('');
      setFormRole('');
      setFormGroups(['default']);
      setFormModels([]);
      setFormQuotaRules([]);
      setFormModelLimits([]);
      setFormExcludedChannels([]);
      setFormExcludedModels([]);
      setFormIpBlacklistText('');
      setFormEnabledPlugins([]);
      setFormBasePreferences({});
    }

    setIsSheetOpen(true);
    // 打开面板时刷新 quota 状态
    const { token } = useAuthStore.getState();
    if (token) {
      apiFetch('/v1/api_keys_states', { headers: { Authorization: `Bearer ${token}` } })
        .then(r => r.ok ? r.json() : null)
        .then(data => { if (data) setQuotaStates(data.quota_states || {}); })
        .catch(() => {});
    }
  };

  // ========== Generate Key ==========
  const generateKey = async () => {
    try {
      const res = await apiFetch('/v1/generate-api-key', {
        headers: { Authorization: `Bearer ${token}` }
      });
      const data = await res.json();
      if (data.api_key) {
        setFormApi(data.api_key);
      }
    } catch {
      toastError('生成密钥失败');
    }
  };

  // ========== Groups ==========
  const addGroup = () => {
    const val = groupInput.trim();
    if (val && !formGroups.includes(val)) {
      setFormGroups([...formGroups, val]);
    }
    setGroupInput('');
  };

  const removeGroup = (g: string) => {
    const newGroups = formGroups.filter(x => x !== g);
    setFormGroups(newGroups.length ? newGroups : ['default']);
  };

  // ========== Models ==========
  const addModelsFromInput = () => {
    const parts = modelInput.split(/[,\s]+/).map(s => s.trim()).filter(Boolean);
    if (parts.length > 0) {
      const newModels = [...new Set([...formModels, ...parts])];
      setFormModels(newModels);
    }
    setModelInput('');
  };

  const removeModel = (m: string) => {
    setFormModels(formModels.filter(x => x !== m));
  };

  const clearAllModels = () => {
    if (formModels.length === 0) return;
    if (confirm('确定要清空所有模型规则吗？')) {
      setFormModels([]);
    }
  };

  // ========== Fetch Models by Groups ==========
  const openFetchModelsDialog = async () => {
    const groups = formGroups.length > 0 ? formGroups : ['default'];
    setFetchingModels(true);
    setModelSearchQuery('');

    try {
      const res = await apiFetch('/v1/channels/models_by_groups', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ groups })
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toastError(err, "获取模型失败");
        return;
      }

      const data = await res.json();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const models = (data.models || []).map((m: any) => m.id || m).filter(Boolean);

      if (models.length === 0) {
        toastError('当前分组下没有可用模型');
        return;
      }

      setFetchedModels(models);
      // Pre-select existing models
      const existing = new Set(formModels);
      setSelectedModels(new Set(models.filter((m: string) => existing.has(m))));
      setIsFetchModelsOpen(true);
    } catch {
      toastError('获取模型失败');
    } finally {
      setFetchingModels(false);
    }
  };

  const toggleModelSelect = (model: string) => {
    const newSet = new Set(selectedModels);
    if (newSet.has(model)) {
      newSet.delete(model);
    } else {
      newSet.add(model);
    }
    setSelectedModels(newSet);
  };

  const selectAllVisible = () => {
    const filtered = fetchedModels.filter(m =>
      !modelSearchQuery || m.toLowerCase().includes(modelSearchQuery.toLowerCase())
    );
    setSelectedModels(new Set(filtered));
  };

  const deselectAllVisible = () => {
    const filtered = new Set(fetchedModels.filter(m =>
      !modelSearchQuery || m.toLowerCase().includes(modelSearchQuery.toLowerCase())
    ));
    const newSet = new Set(selectedModels);
    filtered.forEach(m => newSet.delete(m));
    setSelectedModels(newSet);
  };

  const confirmFetchModels = () => {
    const existingSet = new Set(formModels);
    selectedModels.forEach(m => existingSet.add(m));
    setFormModels(Array.from(existingSet));
    setIsFetchModelsOpen(false);
  };

  const filteredFetchedModels = fetchedModels.filter(m =>
    !modelSearchQuery || m.toLowerCase().includes(modelSearchQuery.toLowerCase())
  );

  // ========== Save ==========
  const handleSave = async () => {
    if (!formApi.trim()) {
      toastWarning('API Key 不能为空');
      return;
    }

    const keyIpBlacklist = parseIpBlacklistText(formIpBlacklistText);
    if (!validateIpBlacklistEntries(keyIpBlacklist)) return;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const target: any = { api: formApi.trim() };

    if (formName.trim()) target.name = formName.trim();
    if (formRole.trim()) target.role = formRole.trim();
    target.groups = formGroups.length > 0 ? formGroups : ['default'];
    if (formModels.length > 0) target.model = formModels;
    // 修改原因：Key 级 IP 黑名单需要保存在 api_keys 条目的顶层字段，而不是 preferences 中。
    // 修改方式：textarea 解析为数组后赋给 target.ip_blacklist，空数组也保留，方便清空旧配置。
    // 目的：让每个 API Key 独立的 IP 黑名单随 API Key 保存一起持久化并热更新。
    target.ip_blacklist = keyIpBlacklist;

    // Preferences
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const prefs: any = { ...currentKeyPluginPreferences };
    // 修改原因：Key 插件完整配置会修改 preferences 中的 provider_config 字段，保存时不能只写配额和黑名单。
    // 修改方式：以当前插件面板合并后的 preferences 为基线，再由主表单字段显式覆盖或删除受控字段。
    // 目的：同时支持完整插件配置、清空插件列表、清空 quota 和保留旧兼容字段。
    const allRules = [...formQuotaRules, ...formModelLimits].filter(r => r.key.trim() && r.value.trim());
    if (allRules.length > 0) {
      const quotaObj: Record<string, string> = {};
      allRules.forEach(r => { quotaObj[r.key.trim()] = r.value.trim(); });
      prefs.quota = quotaObj;
    } else {
      delete prefs.quota;
    }
    if (formExcludedChannels.length > 0) {
      prefs.excluded_channels = formExcludedChannels;
    } else {
      delete prefs.excluded_channels;
    }
    if (formExcludedModels.length > 0) {
      prefs.excluded_models = formExcludedModels;
    } else {
      delete prefs.excluded_models;
    }
    if (formEnabledPlugins.length > 0) {
      prefs.enabled_plugins = formEnabledPlugins;
    } else {
      delete prefs.enabled_plugins;
    }
    if (Object.keys(prefs).length > 0) target.preferences = prefs;

    const newKeys = [...keys];
    if (editingIndex !== null) {
      target.ip_blacklist = keyIpBlacklist;
      newKeys[editingIndex] = target;
    } else {
      newKeys.push(target);
    }

    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ api_keys: newKeys })
      });
      if (res.ok) {
        setKeys(newKeys);
        setIsSheetOpen(false);
        fetchData();
      } else {
        toastError('保存失败');
      }
    } catch {
      toastError('网络错误');
    }
  };



  // ========== Delete ==========
  const handleDelete = async (index: number) => {
    const keyObj = keys[index];
    const name = keyObj.name || keyObj.api?.slice(0, 12) + '...';
    if (!confirm(`确定要删除 API Key "${name}" 吗？`)) return;

    const newKeys = keys.filter((_, i) => i !== index);
    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ api_keys: newKeys })
      });
      if (res.ok) {
        setKeys(newKeys);
        fetchData();
      } else {
        toastError('删除失败');
      }
    } catch {
      toastError('网络错误');
    }
  };


  // ========== Clear All Keys ==========
  const handleClearAllKeys = async () => {
    if (!token) return;
    if (keys.length === 0) return;
    if (!confirm(`确定要清空全部 API Keys 吗？（共 ${keys.length} 个，将无法恢复）`)) return;

    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ api_keys: [] })
      });
      if (res.ok) {
        setKeys([]);
        fetchData();
      } else {
        const data = await res.json().catch(() => ({}));
        toastError(data, "清空失败");
      }
    } catch {
      toastError('网络错误');
    }
  };

  // ========== Add Credits ==========
  const openCreditsDialog = (key: string) => {
    setCreditsTargetKey(key);
    setCreditsAmount('');
    setIsCreditsOpen(true);
  };

  const handleAddCredits = async () => {
    const amount = parseFloat(creditsAmount);
    if (isNaN(amount) || amount <= 0) {
      toastWarning('请输入大于 0 的有效数字');
      return;
    }

    try {
      const res = await apiFetch(`/v1/add_credits?paid_key=${encodeURIComponent(creditsTargetKey)}&amount=${amount}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` }
      });
      if (res.ok) {
        setIsCreditsOpen(false);
        fetchData();
      } else {
        const data = await res.json().catch(() => ({}));
        toastError(data, "充值失败");
      }
    } catch {
      toastError('网络错误');
    }
  };

  // ========== Helpers ==========
  const getStatusInfo = (keyStr: string) => {
    const state = keyStates[keyStr];
    if (!state) return { icon: <AlertCircle className="w-3.5 h-3.5" />, label: '未知', cls: 'bg-muted text-muted-foreground' };
    if (state.enabled) return { icon: <CheckCircle2 className="w-3.5 h-3.5" />, label: '启用中', cls: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-500 border border-emerald-500/20' };
    return { icon: <AlertTriangle className="w-3.5 h-3.5" />, label: '已停用', cls: 'bg-red-500/10 text-red-600 dark:text-red-500 border border-red-500/20' };
  };

  const getQuotaInfo = (keyStr: string) => {
    // 修改原因：后端 quota_states 已从 request:default 迁移到 key:request:default，需要前端理解 scope、metric、qualifier 三段结构。
    // 修改方式：解析每个状态 key，按 scope 分组，并用所有维度的最低剩余百分比作为主弧线。
    // 目的：列表既能兼容旧状态格式，又能展示 Key 级、Per-IP 和模型级的正交配额状态。
    const qs = quotaStates[keyStr];
    if (!qs || Object.keys(qs).length === 0) {
      return { text: '无配额', items: [] as QuotaDisplayItem[], groups: [] as QuotaDisplayGroup[] };
    }

    const scopeOrder: Record<string, number> = { key: 0, ip: 1, model: 2 };
    const metricOrder: Record<string, number> = { cost: 0, request: 1, token_out: 2, token_in: 3, token: 4, unique_ip: 5 };
    const items: QuotaDisplayItem[] = [];
    for (const [statusKey, value] of Object.entries(qs)) {
      if (!value || typeof value !== 'object') continue;
      const parsed = parseQuotaStatusKey(statusKey);
      const current = Number((value as any).current || 0);
      const limit = Number((value as any).limit || 0);
      const remainingRaw = (value as any).remaining;
      const remaining = Number(remainingRaw !== undefined ? remainingRaw : Math.max(0, limit - current));
      const groupLabel = QUOTA_SCOPE_LABELS[parsed.scope] || parsed.scope;
      const metricLabel = QUOTA_METRIC_LABELS[parsed.metric] || parsed.metric;
      const qualifierLabel = parsed.qualifier && parsed.qualifier !== 'default' ? ` · ${parsed.qualifier}` : '';
      items.push({
        label: `${metricLabel}${qualifierLabel}`,
        current,
        limit,
        remaining,
        scope: parsed.scope,
        metric: parsed.metric,
        qualifier: parsed.qualifier,
        groupLabel,
      });
    }

    items.sort((a, b) => (scopeOrder[a.scope] ?? 99) - (scopeOrder[b.scope] ?? 99) || (metricOrder[a.metric] ?? 99) - (metricOrder[b.metric] ?? 99));
    const groups = items.reduce<QuotaDisplayGroup[]>((acc, item) => {
      let group = acc.find(g => g.label === item.groupLabel);
      if (!group) {
        group = { label: item.groupLabel, items: [] };
        acc.push(group);
      }
      group.items.push(item);
      return acc;
    }, []);

    let percent: number | undefined;
    const pcts = items
      .filter(i => i.limit > 0)
      .map(i => Math.max(0, (i.remaining / i.limit) * 100));
    if (pcts.length > 0) percent = Math.min(...pcts);

    const fmtNum = (n: number) => n >= 1e6 ? `${(n/1e6).toFixed(1)}M` : n >= 1e3 ? `${(n/1e3).toFixed(1)}K` : `${Math.round(n)}`;
    const keyCost = items.find(i => i.scope === 'key' && i.metric === 'cost') || items.find(i => i.metric === 'cost');
    const keyReq = items.find(i => i.scope === 'key' && i.metric === 'request') || items.find(i => i.metric === 'request');
    const tokenOut = items.find(i => i.scope === 'key' && i.metric === 'token_out') || items.find(i => i.metric === 'token_out');
    const tokenIn = items.find(i => i.scope === 'key' && i.metric === 'token_in') || items.find(i => i.metric === 'token_in');
    const tokenItem = items.find(i => i.scope === 'key' && i.metric === 'token') || items.find(i => i.metric === 'token');
    const uniqueIp = items.find(i => i.metric === 'unique_ip');

    if (keyCost) return { text: `$${keyCost.remaining.toFixed(2)}`, items, groups, percent };
    if (keyReq) return { text: `${fmtNum(keyReq.remaining)}/${fmtNum(keyReq.limit)} 次`, items, groups, percent };
    if (tokenOut) return { text: `${fmtNum(tokenOut.remaining)}/${fmtNum(tokenOut.limit)} out`, items, groups, percent };
    if (tokenIn) return { text: `${fmtNum(tokenIn.remaining)}/${fmtNum(tokenIn.limit)} in`, items, groups, percent };
    if (tokenItem) return { text: `${fmtNum(tokenItem.remaining)}/${fmtNum(tokenItem.limit)} tok`, items, groups, percent };
    if (uniqueIp) return { text: `${Math.round(uniqueIp.current)}/${Math.round(uniqueIp.limit)} IP`, items, groups, percent };

    return { text: '已配置', items, groups, percent };
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  const openKeyAnalytics = (keyObj: ApiKeyData) => {
    // 修改原因：分析入口需要从当前 Key 行传入完整 Key 和可读名称，Sheet 内部再计算 key_hash。
    // 修改方式：优先使用顶层 name，兼容 preferences.name，然后显式打开侧滑 Sheet。
    // 目的：保持 Admin 列表操作区只负责选择 Key，具体分析请求由 KeyAnalyticsSheet 负责。
    setAnalyticsKey({ api: keyObj.api, name: keyObj.name || keyObj.preferences?.name });
    setAnalyticsOpen(true);
  };

  const handleAnalyticsOpenChange = (open: boolean) => {
    // 修改原因：Radix Dialog 关闭时只会回传 open=false，需要同步清理已选 Key。
    // 修改方式：先更新 open 状态，关闭时再把 analyticsKey 置空。
    // 目的：防止下次打开前短暂显示上一次 Key 的标题或数据。
    setAnalyticsOpen(open);
    if (!open) setAnalyticsKey(null);
  };

  // ── Key Pipeline 插件管理辅助 ──
  // 修改原因：Panel 需要按 entryIndex 精确定位插件条目（而非按插件名），以支持同名多实例。
  // 修改方式：从 formEnabledPlugins 派生条目，带上 paramsSchema/description 等插件元数据。
  // 目的：让 PluginCard 能展示描述、参数表单，与渠道 Pipeline 完全一致。
  const enabledPluginNames = useMemo(
    () => new Set(formEnabledPlugins.map(v => parseEnabledPlugin(v).name).filter(Boolean)),
    [formEnabledPlugins]
  );

  const enabledPluginEntries = useMemo(
    () => formEnabledPlugins.map((value, entryIndex) => {
      const parsed = parseEnabledPlugin(value);
      const info = allPlugins.find((plugin: any) => (plugin.plugin_name || plugin.name) === parsed.name);
      const paramsSchema = Array.isArray(info?.metadata?.params_schema) ? info.metadata.params_schema : [];
      return { ...parsed, description: info?.description, paramsSchema, paramsHint: info?.metadata?.params_hint, entryIndex, info };
    }),
    [formEnabledPlugins, allPlugins]
  );

  const removePluginAt = (entryIndex: number) => {
    setFormEnabledPlugins(prev => prev.filter((_, index) => index !== entryIndex));
  };

  const updatePluginOptsAt = (entryIndex: number, name: string, opts: string) => {
    setFormEnabledPlugins(prev => {
      const next = [...prev];
      next[entryIndex] = buildEnabledPluginValue(name, opts);
      return next;
    });
  };

  const addPlugin = (pluginName: string) => {
    setFormEnabledPlugins(prev => [...prev, pluginName]);
    setOpenAddMenu(null);
  };

  // ── Quota 编辑器渲染辅助 ──
  // 修改原因：旧配额编辑器把 Key 级、Per-IP 和模型限速混成扁平列表，管理员难以理解规则作用域。
  // 修改方式：在保存结构不变的前提下，把 formQuotaRules 按 scope 分成卡片，并把 formModelLimits 合并到同一个 Quota section。
  // 目的：用可视分组表达 Scope × Metric 正交模型，同时继续输出原有 preferences.quota 对象。
  const fmtQuotaNumber = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : `${Math.round(n)}`;

  const getQuotaRuleStatus = (configKey: string) => {
    const editStates = formApi ? (quotaStates[formApi] || {}) : {};
    const parsed = parseQuotaConfigKey(configKey);
    const exactKey = getQuotaStatusKeyFromConfig(configKey);
    if ((editStates as any)[exactKey]) return (editStates as any)[exactKey];
    return Object.entries(editStates).find(([statusKey]) => {
      const status = parseQuotaStatusKey(statusKey);
      return status.scope === parsed.scope && status.metric === parsed.metric && status.qualifier === (parsed.scope === 'ip' ? 'default' : parsed.qualifier);
    })?.[1] as any;
  };

  const quotaMetricCanRepeat = (scope: QuotaScopeValue, metric: QuotaMetricValue) => {
    // 修改原因：Key 级 cost 可以通过 qualifier 表达 default、daily 等多条规则，其它 metric 重复会互相覆盖或语义不清。
    // 修改方式：只允许 key/cost 重复；其它 scope × metric 组合在添加和切换时置灰。
    // 目的：防止保存时同名 quota key 被后一个规则覆盖。
    return scope === 'key' && metric === 'cost';
  };

  const isQuotaMetricTaken = (scope: QuotaScopeValue, metric: QuotaMetricValue, excludeIndex?: number) => {
    if (quotaMetricCanRepeat(scope, metric)) return false;
    return formQuotaRules.some((rule, index) => {
      if (excludeIndex !== undefined && index === excludeIndex) return false;
      const parsed = parseQuotaConfigKey(rule.key);
      return parsed.scope === scope && parsed.metric === metric;
    });
  };

  const nextCostQualifier = () => {
    const existing = new Set(formQuotaRules.map(rule => rule.key));
    if (!existing.has('cost')) return 'default';
    if (!existing.has('cost:daily')) return 'daily';
    let index = 2;
    while (existing.has(`cost:rule${index}`)) index += 1;
    return `rule${index}`;
  };

  const buildNewQuotaRuleKey = (scope: QuotaScopeValue, metric: QuotaMetricValue) => {
    const qualifier = scope === 'key' && metric === 'cost' ? nextCostQualifier() : 'default';
    return buildQuotaConfigKey(scope, metric, qualifier);
  };

  const updateQuotaRuleKeyAt = (entryIndex: number, scope: QuotaScopeValue, metric: QuotaMetricValue, qualifier: string) => {
    setFormQuotaRules(prev => {
      const next = [...prev];
      next[entryIndex] = { ...next[entryIndex], key: buildQuotaConfigKey(scope, metric, qualifier) };
      return next;
    });
  };

  const updateQuotaRuleValueAt = (entryIndex: number, value: string) => {
    setFormQuotaRules(prev => {
      const next = [...prev];
      next[entryIndex] = { ...next[entryIndex], value };
      return next;
    });
  };

  const addQuotaRule = (scope: QuotaScopeValue, metric: QuotaMetricValue) => {
    if (isQuotaMetricTaken(scope, metric)) return;
    setFormQuotaRules(prev => [...prev, { key: buildNewQuotaRuleKey(scope, metric), value: '' }]);
  };

  const removeQuotaRuleAt = (entryIndex: number) => {
    setFormQuotaRules(prev => prev.filter((_, index) => index !== entryIndex));
  };

  const renderQuotaProgress = (status: any, metric: QuotaMetricValue) => {
    if (!status) return <span className="hidden sm:block w-28" />;
    const limit = Number(status.limit || 0);
    const remaining = Number(status.remaining || 0);
    const ratio = limit > 0 ? remaining / limit : 1;
    return (
      <div className="flex items-center gap-2 min-w-[120px] flex-1 sm:flex-none">
        <div className="h-1.5 flex-1 bg-muted rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${
              ratio < 0.1 ? 'bg-red-500' : ratio < 0.3 ? 'bg-yellow-500' : 'bg-emerald-500'
            }`}
            style={{ width: `${limit > 0 ? Math.max(1, ratio * 100) : 100}%` }}
          />
        </div>
        <span className="text-[10px] text-muted-foreground font-mono whitespace-nowrap">
          {metric === 'cost' ? `$${remaining.toFixed(2)}/$${limit.toFixed(2)}` : `${fmtQuotaNumber(remaining)}/${fmtQuotaNumber(limit)}`}
        </span>
      </div>
    );
  };

  const renderQuotaMetricPills = (
    scope: QuotaScopeValue,
    selectedMetric: QuotaMetricValue | null,
    onSelect: (metric: QuotaMetricValue) => void,
    excludeIndex?: number,
  ) => {
    // 修改原因：Scope × Metric 编辑不应再通过 select 下拉隐藏可选项，添加规则时也需要直观看到哪些 metric 已占用。
    // 修改方式：把当前 scope 可用的 metric 渲染成 pill 按钮组，已存在且不可重复的 metric 禁用并置灰。
    // 目的：让管理员通过点击完成 metric 选择，符合正交化卡片布局。
    return (
      <div className="flex flex-wrap gap-1.5">
        {QUOTA_METRIC_OPTIONS.filter(option => option.scopes.includes(scope)).map(option => {
          const disabled = isQuotaMetricTaken(scope, option.value, excludeIndex);
          const active = selectedMetric === option.value;
          return (
            <button
              key={option.value}
              type="button"
              disabled={disabled && !active}
              onClick={() => onSelect(option.value)}
              className={`rounded-full border px-2 py-1 text-[10px] font-medium transition-colors ${
                active
                  ? 'border-primary bg-primary/10 text-primary'
                  : disabled
                    ? 'border-border bg-muted/40 text-muted-foreground/50 cursor-not-allowed'
                    : 'border-border bg-background text-muted-foreground hover:text-foreground hover:border-primary/50'
              }`}
              title={disabled && !active ? '该 Metric 已存在' : option.label}
            >
              {option.label}
            </button>
          );
        })}
      </div>
    );
  };

  const renderQuotaRuleCard = (scope: QuotaScopeValue, title: string, icon: ReactNode) => {
    const scopedRules = formQuotaRules
      .map((rule, entryIndex) => ({ rule, entryIndex, parsed: parseQuotaConfigKey(rule.key) }))
      .filter(item => item.parsed.scope === scope);

    return (
      <div className="bg-card border border-border rounded-xl p-4">
        <div className="flex items-center gap-2 mb-3">
          <span className="text-muted-foreground">{icon}</span>
          <span className="text-sm font-semibold text-foreground">{title}</span>
          <span className="ml-auto text-[10px] text-muted-foreground">{scopedRules.length} 条规则</span>
        </div>
        <div className="space-y-2">
          {scopedRules.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">暂无规则</p>
          ) : scopedRules.map(({ rule, entryIndex, parsed }) => {
            const status = getQuotaRuleStatus(rule.key);
            return (
              <div key={entryIndex} className="rounded-lg border border-border bg-muted/20 p-2 space-y-1.5">
                <div className="grid grid-cols-2 gap-2 items-center">
                  <div className="flex flex-wrap gap-1 min-w-0">
                    {renderQuotaMetricPills(
                      scope,
                      parsed.metric,
                      (metric) => updateQuotaRuleKeyAt(entryIndex, scope, metric, scope === 'key' && metric === 'cost' ? parsed.qualifier : 'default'),
                      entryIndex,
                    )}
                    {scope === 'key' && parsed.metric === 'cost' && (
                      <input
                        value={parsed.qualifier === 'default' ? '' : parsed.qualifier}
                        onChange={event => updateQuotaRuleKeyAt(entryIndex, scope, parsed.metric, event.target.value || 'default')}
                        placeholder="标签"
                        className="w-14 bg-background border border-border px-1.5 py-0.5 rounded text-[10px] text-foreground"
                      />
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <input
                      value={rule.value}
                      onChange={event => updateQuotaRuleValueAt(entryIndex, event.target.value)}
                      placeholder="100/5h:fixed"
                      className="flex-1 min-w-0 bg-background border border-border px-2 py-1 rounded text-xs font-mono text-foreground"
                    />
                    <button
                      type="button"
                      onClick={() => removeQuotaRuleAt(entryIndex)}
                      className="text-muted-foreground hover:text-destructive transition-colors flex-shrink-0"
                      title="删除规则"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
                {status && (
                  <div className="flex items-center gap-2 w-full">
                    <div className="h-1.5 flex-1 bg-muted rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all ${(status.limit > 0 && status.remaining / status.limit < 0.1) ? 'bg-red-500' : (status.limit > 0 && status.remaining / status.limit < 0.3) ? 'bg-yellow-500' : 'bg-emerald-500'}`}
                        style={{ width: `${status.limit > 0 ? Math.max(1, (status.remaining / status.limit) * 100) : 100}%` }}
                      />
                    </div>
                    <span className="text-[10px] text-muted-foreground font-mono whitespace-nowrap">
                      {parsed.metric === 'cost' ? `$${Number(status.remaining).toFixed(2)}/$${Number(status.limit).toFixed(2)}` : `${fmtQuotaNumber(status.remaining)}/${fmtQuotaNumber(status.limit)}`}
                    </span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <div className="mt-3 border-t border-border pt-3">
          <div className="mb-1 text-[10px] text-muted-foreground">+ 添加 {title} 规则</div>
          {renderQuotaMetricPills(scope, null, (metric) => addQuotaRule(scope, metric))}
        </div>
      </div>
    );
  };

  const renderModelLimitCard = () => {
    // 修改原因：模型限速本质是 model scope 的 request metric，继续独立 section 会割裂统一 quota 编辑体验。
    // 修改方式：把 formModelLimits 渲染为 Quota section 的第三张卡片，但保留原有 key/value 数据结构和保存逻辑。
    // 目的：让三个 scope 分组在同一处完成配置，不改变后端读取 preferences.quota 的方式。
    return (
      <div className="bg-card border border-border rounded-xl p-4">
        <div className="flex items-center gap-2 mb-3">
          <Zap className="w-4 h-4 text-muted-foreground" />
          <span className="text-sm font-semibold text-foreground">模型限速</span>
          <span className="ml-auto text-[10px] text-muted-foreground">{formModelLimits.length} 条规则</span>
        </div>
        <div className="space-y-2">
          {formModelLimits.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">暂无规则</p>
          ) : formModelLimits.map((rule, index) => (
            <div key={index} className="flex flex-col gap-2 rounded-lg border border-border bg-muted/20 p-2 sm:flex-row sm:items-center">
              <input
                value={rule.key}
                onChange={event => {
                  const next = [...formModelLimits];
                  next[index] = { ...next[index], key: event.target.value };
                  setFormModelLimits(next);
                }}
                placeholder="模型名，如 claude-opus-4"
                className="flex-1 bg-background border border-border px-2 py-1.5 rounded-lg text-xs font-mono text-foreground"
              />
              <input
                value={rule.value}
                onChange={event => {
                  const next = [...formModelLimits];
                  next[index] = { ...next[index], value: event.target.value };
                  setFormModelLimits(next);
                }}
                placeholder="10/5h:fixed"
                className="w-full sm:w-36 bg-background border border-border px-2 py-1.5 rounded-lg text-xs font-mono text-foreground"
              />
              <button
                type="button"
                onClick={() => setFormModelLimits(formModelLimits.filter((_, i) => i !== index))}
                className="self-end sm:self-auto text-muted-foreground hover:text-destructive transition-colors"
                title="删除模型限速"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
        <button
          type="button"
          onClick={() => setFormModelLimits([...formModelLimits, { key: '', value: '' }])}
          className="mt-3 text-xs text-primary hover:text-primary/80 flex items-center gap-1"
        >
          <Plus className="w-3 h-3" /> 添加模型
        </button>
      </div>
    );
  };

  return (
    <div className="space-y-6 animate-in fade-in duration-500 font-sans">
      {/* Header */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-foreground">API 密钥管理</h1>
          <p className="text-muted-foreground mt-1 text-sm sm:text-base">管理调用 Zoaholic 网关的下游 API Key、额度与权限</p>
        </div>
        <div className="flex gap-2 w-full sm:w-auto">
          <button onClick={fetchData} className="p-2 text-muted-foreground hover:text-foreground bg-card border border-border rounded-lg flex-shrink-0">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={handleClearAllKeys}
            disabled={keys.length === 0}
            className="px-3 py-2 text-sm font-medium bg-red-500/10 border border-red-500/30 text-red-600 dark:text-red-400 hover:bg-red-500/20 rounded-lg flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed flex-1 sm:flex-none justify-center"
            title="一键清空全部 API Keys"
          >
            <Trash2 className="w-4 h-4" /> <span className="hidden sm:inline">清空全部</span><span className="sm:hidden">清空</span>
          </button>
          <button onClick={() => openSheet()} className="bg-primary hover:bg-primary/90 text-primary-foreground px-4 py-2 rounded-lg flex items-center gap-2 font-medium flex-1 sm:flex-none justify-center">
            <Plus className="w-4 h-4" /> <span className="hidden sm:inline">新增 API Key</span><span className="sm:hidden">新增</span>
          </button>
        </div>
      </div>

      {/* Table */}
      {/* Mobile Card List */}
      <div className="md:hidden space-y-4">
        {loading && keys.length === 0 ? (
          <div className="p-8 text-center text-muted-foreground">加载密钥数据...</div>
        ) : keys.length === 0 ? (
          <div className="p-12 text-center text-muted-foreground">
            <Key className="w-12 h-12 mb-3 opacity-50 mx-auto" />
            <h3 className="text-lg font-medium text-foreground">暂无 API 密钥</h3>
            <p className="text-sm mt-1 mb-4">创建您的第一个密钥以允许客户端接入</p>
            <button onClick={() => openSheet()} className="text-primary hover:underline text-sm font-medium">+ 新增 API Key</button>
          </div>
        ) : (
          keys.map((keyObj, idx) => {
            const status = getStatusInfo(keyObj.api);
            const quota = getQuotaInfo(keyObj.api);
            const name = keyObj.name || keyObj.preferences?.name || '未命名密钥';
            const groups = keyObj.groups || (keyObj.group ? [keyObj.group] : ['default']);
            return (
              <div key={idx} className="bg-card border border-border rounded-xl p-4">
                <div className="flex items-start justify-between mb-2">
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-foreground truncate">{name}</div>
                    <div className="text-xs text-muted-foreground font-mono mt-1 flex items-center gap-1.5">
                      <Key className="w-3 h-3 flex-shrink-0" />
                      {keyObj.api.slice(0, 7)}...{keyObj.api.slice(-4)}
                      <button onClick={() => copyToClipboard(keyObj.api)} className="text-muted-foreground/60 hover:text-foreground"><Copy className="w-3 h-3" /></button>
                    </div>
                  </div>
                  <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium flex-shrink-0 ${status.cls}`}>{status.icon} {status.label}</span>
                </div>
                <div className="flex flex-wrap items-center gap-2 mb-3 text-xs">
                  <span className={`px-2 py-0.5 rounded font-medium ${keyObj.role === 'admin' ? 'bg-purple-500/10 text-purple-600 dark:text-purple-400' : 'bg-muted text-muted-foreground'}`}>{keyObj.role || 'user'}</span>
                  {groups.map(g => (<span key={g} className="flex items-center gap-1 bg-muted text-foreground px-1.5 py-0.5 rounded"><Folder className="w-3 h-3" />{g}</span>))}
                  {quota.percent !== undefined && <QuotaArcs quotaInner={quota.percent} />}
                  <span className="text-muted-foreground">{quota.text}</span>
                </div>
                <div className="flex items-center justify-end gap-1 pt-3 border-t border-border">
                  <button onClick={() => openCreditsDialog(keyObj.api)} className="p-1.5 text-emerald-600 dark:text-emerald-500 hover:bg-emerald-500/10 rounded-md" title="充值"><Wallet className="w-4 h-4" /></button>
                  <button onClick={() => openKeyAnalytics(keyObj)} className="p-1.5 text-primary hover:bg-primary/10 rounded-md" title="用量分析"><BarChart3 className="w-4 h-4" /></button>
                  <button onClick={() => openSheet(null, keyObj)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md" title="复制"><Copy className="w-4 h-4" /></button>
                  <button onClick={() => openSheet(idx)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md" title="编辑"><Edit className="w-4 h-4" /></button>
                  <button onClick={() => handleDelete(idx)} className="p-1.5 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md" title="删除"><Trash2 className="w-4 h-4" /></button>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Desktop Table */}
      <div className="hidden md:block bg-card border border-border rounded-xl overflow-hidden">
        {loading && keys.length === 0 ? (
          <div className="p-12 flex flex-col items-center justify-center text-muted-foreground">
            <RefreshCw className="w-8 h-8 animate-spin mb-3" />
            <p>加载密钥数据...</p>
          </div>
        ) : keys.length === 0 ? (
          <div className="p-16 flex flex-col items-center justify-center text-muted-foreground">
            <Key className="w-12 h-12 mb-3 opacity-50" />
            <h3 className="text-lg font-medium text-foreground">暂无 API 密钥</h3>
            <p className="text-sm mt-1 mb-4">创建您的第一个密钥以允许客户端接入</p>
            <button onClick={() => openSheet()} className="text-primary hover:underline text-sm font-medium">+ 新增 API Key</button>
          </div>
        ) : (
          <table className="w-full text-left border-collapse">
            <thead className="bg-muted border-b border-border text-muted-foreground text-sm font-medium">
              <tr>
                <th className="px-6 py-4">名称 / Key</th>
                <th className="px-6 py-4">角色</th>
                <th className="px-6 py-4 text-center">配额状态</th>
                <th className="px-6 py-4">模型规则</th>
                <th className="px-6 py-4 text-center">状态</th>
                <th className="px-6 py-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border text-sm">
              {keys.map((keyObj, idx) => {
                const status = getStatusInfo(keyObj.api);
                const quota = getQuotaInfo(keyObj.api);
                const state = keyStates[keyObj.api];
                const name = keyObj.name || keyObj.preferences?.name || '未命名密钥';
                const groups = keyObj.groups || (keyObj.group ? [keyObj.group] : ['default']);
                const models = keyObj.model || [];
                const modelText = models.length === 0 ? '默认: all' :
                  (models.length === 1 && models[0] === 'all') ? '全部模型 (all)' :
                    models.length > 3 ? `${models.slice(0, 3).join(', ')} 等 ${models.length} 条` : models.join(', ');

                return (
                  <tr key={idx} className="hover:bg-muted/50 transition-colors group">
                    <td className="px-6 py-4">
                      <div className="font-medium text-foreground">{name}</div>
                      <div className="text-xs text-muted-foreground font-mono mt-1 flex items-center gap-1.5">
                        <Key className="w-3 h-3" />
                        {keyObj.api.slice(0, 7)}...{keyObj.api.slice(-4)}
                        <button onClick={() => copyToClipboard(keyObj.api)} className="text-muted-foreground/60 hover:text-foreground">
                          <Copy className="w-3 h-3" />
                        </button>
                      </div>
                      {state?.created_at && (
                        <div className="text-xs text-muted-foreground/60 mt-1">创建: {state.created_at}</div>
                      )}
                      {keyObj.preferences?.rate_limit && (
                        <div className="text-[10px] text-muted-foreground/50 mt-0.5">限流: {typeof keyObj.preferences.rate_limit === 'object'
                          ? `${(keyObj.preferences.rate_limit as any).default || '无全局'} + ${Object.keys(keyObj.preferences.rate_limit).filter(k => k !== 'default').length} 条模型规则`
                          : keyObj.preferences.rate_limit}</div>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      <span className={`px-2 py-1 rounded text-xs font-medium ${keyObj.role === 'admin' ? 'bg-purple-500/10 text-purple-600 dark:text-purple-400 border border-purple-500/20' : 'bg-muted text-muted-foreground'}`}>
                        {keyObj.role || 'user'}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-center">
                      {/* 修改原因：quota_states 已按 scope 输出，桌面列表需要展示 Key 级、Per-IP 等分组，而不是只显示扁平维度名。 */}
                      {/* 修改方式：主行展示最低剩余额度摘要，明细按 scope 分组显示前两个分组和每组前两个 metric。 */}
                      {/* 目的：保持表格紧凑，同时让管理员能区分 Key 级配额和 Per-IP 配额。 */}
                      <div className="flex items-center justify-center gap-2 max-w-[200px]">
                        {quota.percent !== undefined && (
                          <QuotaArcs quotaInner={quota.percent} />
                        )}
                        <div className="min-w-0 flex-1">
                          <div className="text-foreground font-mono text-sm truncate">{quota.text}</div>
                          {quota.groups.length > 0 && (
                            <div className="text-[10px] text-muted-foreground mt-0.5 space-y-0.5 text-left">
                              {quota.groups.slice(0, 2).map((group) => (
                                <div key={group.label} className="truncate">
                                  <span className="text-foreground/70">{group.label}</span>: {group.items.slice(0, 2).map(item => `${item.label} ${Math.round(item.current)}/${Math.round(item.limit)}`).join(' · ')}
                                </div>
                              ))}
                              {quota.groups.length > 2 && <div>+{quota.groups.length - 2} 组更多</div>}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="px-6 py-4 max-w-[200px]">
                      <div className="text-xs text-muted-foreground truncate mb-1" title={models.join(', ')}>{modelText}</div>
                      <div className="flex flex-wrap gap-1">
                        {groups.map(g => (
                          <span key={g} className="flex items-center gap-1 text-[11px] bg-muted text-foreground px-1.5 py-0.5 rounded">
                            <Folder className="w-3 h-3" />{g}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="px-6 py-4 text-center">
                      <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium ${status.cls}`}>
                        {status.icon} {status.label}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        <button onClick={() => openCreditsDialog(keyObj.api)} className="p-1.5 text-emerald-600 dark:text-emerald-500 hover:bg-emerald-500/10 rounded-md" title="充值额度">
                          <Wallet className="w-4 h-4" />
                        </button>
                        <button onClick={() => openKeyAnalytics(keyObj)} className="p-1.5 text-primary hover:bg-primary/10 rounded-md" title="用量分析">
                          <BarChart3 className="w-4 h-4" />
                        </button>
                        <button onClick={() => openSheet(null, keyObj)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md" title="复制配置">
                          <Copy className="w-4 h-4" />
                        </button>
                        <button onClick={() => openSheet(idx)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md" title="编辑">
                          <Edit className="w-4 h-4" />
                        </button>
                        <button onClick={() => handleDelete(idx)} className="p-1.5 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md" title="删除">
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

      {/* ========== Edit Side Sheet ========== */}
      <Dialog.Root open={isSheetOpen} onOpenChange={handleEditSheetOpenChange}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40" />
          <Dialog.Content className="fixed right-0 top-0 h-full w-full sm:w-[560px] bg-background border-l border-border shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300">
            <div className="p-5 border-b border-border flex justify-between items-center bg-muted/30">
              <Dialog.Title className="text-lg font-bold text-foreground flex items-center gap-2">
                <Key className="w-5 h-5 text-primary" />
                {editingIndex !== null ? '编辑 API Key' : '新增 API Key'}
              </Dialog.Title>
              <Dialog.Close className="text-muted-foreground hover:text-foreground"><X className="w-5 h-5" /></Dialog.Close>
            </div>

            <div className="flex-1 overflow-y-auto p-5 space-y-6">
              {/* Basic Info Section */}
              <section className="space-y-4">
                <div className="text-sm font-semibold text-foreground border-b border-border pb-2 flex items-center gap-2">
                  <Key className="w-4 h-4 text-primary" /> 基础信息
                </div>

                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">Key 名称</label>
                  <input
                    type="text" value={formName} onChange={e => setFormName(e.target.value)}
                    placeholder="例如 生产环境Key、测试用Key"
                    className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm text-foreground"
                  />
                  <p className="text-xs text-muted-foreground mt-1">为此 API Key 设置一个友好的显示名称</p>
                </div>

                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">API Key</label>
                  <div className="flex gap-2">
                    <input
                      type="text" value={formApi} onChange={e => setFormApi(e.target.value)}
                      placeholder="zk-xxx..."
                      className="flex-1 bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono text-foreground"
                    />
                    <button onClick={generateKey} className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg flex items-center gap-1.5 text-sm">
                      <Wand2 className="w-4 h-4" /> 生成
                    </button>
                  </div>
                  {/* 修改原因：管理员需要知道以星号结尾的 API Key 可作为 BYOK 通配符模板。 */}
                  {/* 修改方式：仅补充说明文字和代码样式示例，不改变 API Key 输入、生成或保存逻辑。 */}
                  {/* 目的：让 BYOK 模式的配置方式在管理页可见，减少误配置。 */}
                  <p className="text-xs text-muted-foreground mt-1">建议使用以 zk- 开头的随机字符串。BYOK 模式：以 * 结尾作为通配符模板（如 <code className="font-mono bg-muted px-1 rounded">byok-gemini-*</code>），用户拼接真实上游 Key 后使用</p>
                </div>

                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">角色 (role)</label>
                  <input
                    type="text" value={formRole} onChange={e => setFormRole(e.target.value)}
                    placeholder="例如 admin, paid 或 user"
                    className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm text-foreground"
                  />
                  <p className="text-xs text-muted-foreground mt-1">包含 'admin' 的 Key 将被视为管理 Key</p>
                </div>

                {/* Groups */}
                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">分组</label>
                  <div className="flex flex-wrap gap-2 mb-2 p-2 bg-muted/50 border border-border rounded-lg min-h-[40px]">
                    {formGroups.map(g => (
                      <span key={g} className="bg-background border border-border text-foreground px-2 py-1 rounded text-xs flex items-center gap-1">
                        <Folder className="w-3 h-3" /> {g}
                        <button onClick={() => removeGroup(g)} className="ml-1 text-muted-foreground hover:text-red-500"><X className="w-3 h-3" /></button>
                      </span>
                    ))}
                  </div>
                  <div className="flex gap-2">
                    <input
                      type="text" value={groupInput} onChange={e => setGroupInput(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addGroup(); } }}
                      placeholder="输入分组名..."
                      className="flex-1 bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm text-foreground"
                    />
                    <button onClick={addGroup} className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg text-sm">添加</button>
                  </div>
                </div>
              </section>

              {/* Quota Section */}
              <section className="space-y-4">
                <div className="text-sm font-semibold text-foreground border-b border-border pb-2 flex items-center gap-2">
                  <Wallet className="w-4 h-4 text-emerald-500" /> 配额 (Quota)
                </div>
                {/* 修改原因：配额编辑需要按 Scope 分组，而不是把 Key 级、Per-IP 和模型规则混在一个扁平列表中。 */}
                {/* 修改方式：同一个 section 内渲染三张卡片：Key 级、Per-IP、模型限速。 */}
                {/* 目的：让 Scope × Metric 关系在界面中直接可见，同时保持 formQuotaRules + formModelLimits 的保存逻辑不变。 */}
                <div className="space-y-3">
                  {renderQuotaRuleCard('key', 'Key 级', <Key className="w-4 h-4" />)}
                  {renderQuotaRuleCard('ip', 'Per-IP', <Globe className="w-4 h-4" />)}
                  {renderModelLimitCard()}
                  <div className="text-[10px] text-muted-foreground space-y-0.5">
                    <div>格式: <code className="bg-muted px-1 rounded">数量/时间</code>，可选 <code className="bg-muted px-1 rounded">:fixed</code> 后缀。支持 <code className="bg-muted px-1 rounded">K</code> <code className="bg-muted px-1 rounded">M</code> 缩写。</div>
                    <div>Key 级作用于整个 API Key；Per-IP 作用于每个客户端 IP；模型限速等价于 model scope 的 request metric。</div>
                  </div>
                </div>
              </section>

              {/* Key Pipeline Section */}
              <section className="space-y-4">
                <div className="text-sm font-semibold text-foreground border-b border-border pb-2 flex items-center gap-2">
                  <ArrowRight className="w-4 h-4 text-cyan-500" /> 请求流水线
                </div>
                {(() => {
                  const toggleKN = (id: string) => setActiveKeyNode(prev => prev === id ? null : id);
                  const inboundCount = formEnabledPlugins.filter(p => {
                    const n = p.includes(':') ? p.slice(0, p.indexOf(':')) : p;
                    const info = allPlugins.find((pl: any) => (pl.plugin_name || pl.name) === n);
                    return !info || (info.inbound_interceptors?.length ?? 0) > 0;
                  }).length;
                  const outboundCount = formEnabledPlugins.filter(p => {
                    const n = p.includes(':') ? p.slice(0, p.indexOf(':')) : p;
                    const info = allPlugins.find((pl: any) => (pl.plugin_name || pl.name) === n);
                    return info && (info.key_outbound_interceptors?.length ?? 0) > 0;
                  }).length;
                  return (
                    <div className="bg-muted/50 border border-border rounded-xl px-4 pt-4 pb-3 overflow-visible">
                      <div className="flex items-center justify-center gap-0">
                        {/* 入 */}
                        <div className="flex flex-col items-center flex-shrink-0">
                          <div className="w-8 h-8 rounded-full flex items-center justify-center border border-border bg-muted text-muted-foreground">
                            <Smartphone className="w-3.5 h-3.5" />
                          </div>
                          <span className="mt-1.5 text-[10px] text-muted-foreground">入</span>
                        </div>
                        {/* → */}
                        <div className="flex items-center h-10 min-w-[8px] flex-1 max-w-[20px] relative mx-0.5 flex-shrink-0">
                          <div className="absolute top-1/2 left-0 right-[5px] h-px bg-border" />
                          <div className="absolute right-0 top-1/2 -translate-y-1/2 border-t-[3px] border-t-transparent border-b-[3px] border-b-transparent border-l-[5px] border-l-border" />
                        </div>
                        {/* 入站拦截 */}
                        <div className="flex flex-col items-center cursor-pointer group flex-shrink-0" onClick={() => toggleKN('key_inbound')}>
                          <div className={`relative w-10 h-10 rounded-xl flex items-center justify-center border-[1.5px] transition-all ${
                            activeKeyNode === 'key_inbound'
                              ? 'border-primary bg-primary/10 shadow-[0_0_14px_rgba(99,102,241,0.2)]'
                              : 'border-border bg-muted group-hover:border-primary/50'}`}>
                            <span className={`transition-colors ${activeKeyNode === 'key_inbound' ? 'text-primary' : 'text-muted-foreground group-hover:text-foreground'}`}>
                              <ShieldCheck className="w-4 h-4" />
                            </span>
                            <span className={`absolute -top-1.5 -right-1.5 text-[9px] font-bold w-4 h-4 rounded-full flex items-center justify-center border-[1.5px] border-card ${
                              inboundCount === 0 ? 'bg-muted-foreground/50 text-card' : 'bg-primary text-primary-foreground'}`}>
                              {inboundCount}
                            </span>
                          </div>
                          <span className={`mt-1.5 text-[10px] font-medium transition-colors whitespace-nowrap ${activeKeyNode === 'key_inbound' ? 'text-foreground' : 'text-muted-foreground group-hover:text-foreground'}`}>入站拦截</span>
                        </div>
                        {/* → */}
                        <div className="flex items-center h-10 min-w-[8px] flex-1 max-w-[20px] relative mx-0.5 flex-shrink-0">
                          <div className="absolute top-1/2 left-0 right-[5px] h-px bg-border" />
                          <div className="absolute right-0 top-1/2 -translate-y-1/2 border-t-[3px] border-t-transparent border-b-[3px] border-b-transparent border-l-[5px] border-l-border" />
                        </div>
                        {/* 渠道 */}
                        <div className="flex flex-col items-center flex-shrink-0">
                          <div className="w-10 h-10 rounded-xl flex items-center justify-center border-[1.5px] border-dashed border-cyan-400/40 bg-muted text-cyan-500">
                            <Puzzle className="w-4 h-4" />
                          </div>
                          <span className="mt-1.5 text-[10px] font-medium text-muted-foreground">渠道</span>
                        </div>
                        {/* → */}
                        <div className="flex items-center h-10 min-w-[8px] flex-1 max-w-[20px] relative mx-0.5 flex-shrink-0">
                          <div className="absolute top-1/2 left-0 right-[5px] h-px bg-border" />
                          <div className="absolute right-0 top-1/2 -translate-y-1/2 border-t-[3px] border-t-transparent border-b-[3px] border-b-transparent border-l-[5px] border-l-border" />
                        </div>
                        {/* 出站拦截 */}
                        <div className="flex flex-col items-center cursor-pointer group flex-shrink-0" onClick={() => toggleKN('key_outbound')}>
                          <div className={`relative w-10 h-10 rounded-xl flex items-center justify-center border-[1.5px] transition-all ${
                            activeKeyNode === 'key_outbound'
                              ? 'border-orange-500 bg-orange-500/10 shadow-[0_0_14px_rgba(249,115,22,0.2)]'
                              : 'border-border bg-muted group-hover:border-orange-500/50'}`}>
                            <span className={`transition-colors ${activeKeyNode === 'key_outbound' ? 'text-orange-500' : 'text-muted-foreground group-hover:text-foreground'}`}>
                              <PackageCheck className="w-4 h-4" />
                            </span>
                            <span className={`absolute -top-1.5 -right-1.5 text-[9px] font-bold w-4 h-4 rounded-full flex items-center justify-center border-[1.5px] border-card ${
                              outboundCount === 0 ? 'bg-muted-foreground/50 text-card' : 'bg-orange-500 text-white'}`}>
                              {outboundCount}
                            </span>
                          </div>
                          <span className={`mt-1.5 text-[10px] font-medium transition-colors whitespace-nowrap ${activeKeyNode === 'key_outbound' ? 'text-foreground' : 'text-muted-foreground group-hover:text-foreground'}`}>出站拦截</span>
                        </div>
                        {/* → */}
                        <div className="flex items-center h-10 min-w-[8px] flex-1 max-w-[20px] relative mx-0.5 flex-shrink-0">
                          <div className="absolute top-1/2 left-0 right-[5px] h-px bg-border" />
                          <div className="absolute right-0 top-1/2 -translate-y-1/2 border-t-[3px] border-t-transparent border-b-[3px] border-b-transparent border-l-[5px] border-l-border" />
                        </div>
                        {/* 出 */}
                        <div className="flex flex-col items-center flex-shrink-0">
                          <div className="w-8 h-8 rounded-full flex items-center justify-center border border-border bg-muted text-muted-foreground">
                            <CheckCircle2 className="w-3.5 h-3.5" />
                          </div>
                          <span className="mt-1.5 text-[10px] text-muted-foreground">出</span>
                        </div>
                      </div>

                      {activeKeyNode && (
                        <div className="mt-3 bg-muted/50 border border-border rounded-lg overflow-visible animate-in fade-in slide-in-from-top-1 duration-150">
                          {activeKeyNode === 'key_inbound' && (
                            <DetailPanel icon={<ShieldCheck className="w-4 h-4" />} title="Key 入站拦截" desc="鉴权后 · 分配前">
                              {(() => {
                                const inboundEntries = enabledPluginEntries.filter(e => {
                                  const info = e.info;
                                  return !info || (info.inbound_interceptors?.length ?? 0) > 0 || (info.channel_inbound_interceptors?.length ?? 0) > 0;
                                });
                                return inboundEntries.length === 0 ? (
                                  <p className="text-xs text-muted-foreground italic">未启用任何入站拦截器</p>
                                ) : (
                                  <div className="space-y-2 mb-3">
                                    {inboundEntries.map((p) => (
                                      <PluginCard
                                        key={p.entryIndex}
                                        name={p.name}
                                        opts={p.opts}
                                        hasOpts={p.hasOpts}
                                        description={p.description}
                                        paramsSchema={p.paramsSchema}
                                        paramsHint={p.paramsHint}
                                        onRemove={() => removePluginAt(p.entryIndex)}
                                        onOptsChange={(opts) => updatePluginOptsAt(p.entryIndex, p.name, opts)}
                                      />
                                    ))}
                                  </div>
                                );
                              })()}
                              <PluginAddDropdown
                                stage="inbound"
                                allPlugins={allPlugins}
                                enabledPluginNames={enabledPluginNames}
                                openMenu={openAddMenu}
                                setOpenMenu={setOpenAddMenu}
                                onAdd={addPlugin}
                                onOpenPluginSheet={() => setShowPluginSheet(true)}
                              />
                            </DetailPanel>
                          )}
                          {activeKeyNode === 'key_outbound' && (
                            <DetailPanel icon={<PackageCheck className="w-4 h-4" />} title="Key 出站拦截" desc="渠道返回后 · 客户端前">
                              {(() => {
                                const outboundEntries = enabledPluginEntries.filter(e => {
                                  const info = e.info;
                                  return info && (info.key_outbound_interceptors?.length ?? 0) > 0;
                                });
                                return outboundEntries.length === 0 ? (
                                  <p className="text-xs text-muted-foreground italic">未启用任何出站拦截器</p>
                                ) : (
                                  <div className="space-y-2 mb-3">
                                    {outboundEntries.map((p) => (
                                      <PluginCard
                                        key={p.entryIndex}
                                        name={p.name}
                                        opts={p.opts}
                                        hasOpts={p.hasOpts}
                                        description={p.description}
                                        paramsSchema={p.paramsSchema}
                                        paramsHint={p.paramsHint}
                                        onRemove={() => removePluginAt(p.entryIndex)}
                                        onOptsChange={(opts) => updatePluginOptsAt(p.entryIndex, p.name, opts)}
                                      />
                                    ))}
                                  </div>
                                );
                              })()}
                              <PluginAddDropdown
                                stage="outbound"
                                allPlugins={allPlugins}
                                enabledPluginNames={enabledPluginNames}
                                openMenu={openAddMenu}
                                setOpenMenu={setOpenAddMenu}
                                onAdd={addPlugin}
                                onOpenPluginSheet={() => setShowPluginSheet(true)}
                              />
                            </DetailPanel>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })()}
              </section>

              {/* Access Control Section */}
              <section className="space-y-4">
                <div className="text-sm font-semibold text-foreground border-b border-border pb-2 flex items-center gap-2">
                  <Ban className="w-4 h-4 text-red-500" /> 访问控制
                </div>

                <div className="space-y-3">
                  <label className="text-sm font-medium text-foreground mb-1.5 block">IP 黑名单</label>
                  {/* 修改原因：每个 API Key 需要独立维护 IP 黑名单，且字段存放在 api_keys 条目顶层。 */}
                  {/* 修改方式：在访问控制区域增加 textarea，一行一个 IP/CIDR，保存时解析到 target.ip_blacklist。 */}
                  {/* 目的：当前 Key 命中黑名单时在鉴权层直接返回 ip_blocked。 */}
                  <textarea
                    value={formIpBlacklistText}
                    onChange={e => setFormIpBlacklistText(e.target.value)}
                    placeholder={'例如：\n1.2.3.4\n5.6.7.0/24'}
                    className="w-full min-h-[96px] bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono text-foreground"
                  />
                  <p className="text-xs text-muted-foreground">仅影响当前 API Key。全局黑名单会先于 Key 级黑名单检查。</p>
                </div>

                <div className="space-y-3">
                  <label className="text-sm font-medium text-foreground mb-1.5 block">排除渠道</label>
                  <div className="bg-muted/50 border border-border rounded-lg p-3 min-h-[48px] max-h-[150px] overflow-y-auto">
                    {formExcludedChannels.length === 0 ? (
                      <div className="text-center text-muted-foreground text-xs py-1">未设置排除渠道</div>
                    ) : (
                      <div className="flex flex-wrap gap-2">
                        {formExcludedChannels.map((item, idx) => (
                          <span key={idx} className="bg-red-500/10 border border-red-500/30 text-red-600 dark:text-red-400 text-xs font-mono px-2 py-1 rounded flex items-center gap-1">
                            {item}
                            <button
                              title="移除"
                              onClick={() => setFormExcludedChannels(formExcludedChannels.filter((_, i) => i !== idx))}
                              className="opacity-60 hover:opacity-100 ml-0.5"
                            >
                              <X className="w-3 h-3" />
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={excludedChannelInput}
                      onChange={e => setExcludedChannelInput(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          const parts = excludedChannelInput.split(/[,]+/).map(s => s.trim()).filter(Boolean);
                          if (parts.length > 0) {
                            setFormExcludedChannels([...new Set([...formExcludedChannels, ...parts])]);
                            setExcludedChannelInput('');
                          }
                        }
                      }}
                      placeholder="输入渠道名，逗号分隔"
                      className="flex-1 bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono text-foreground"
                    />
                    <button
                      onClick={() => {
                        const parts = excludedChannelInput.split(/[,]+/).map(s => s.trim()).filter(Boolean);
                        if (parts.length > 0) {
                          setFormExcludedChannels([...new Set([...formExcludedChannels, ...parts])]);
                          setExcludedChannelInput('');
                        }
                      }}
                      className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg text-sm"
                    >
                      添加
                    </button>
                  </div>
                  <p className="text-xs text-muted-foreground">命中后将直接排除整个渠道。</p>
                </div>

                <div className="space-y-3">
                  <label className="text-sm font-medium text-foreground mb-1.5 block">排除模型</label>
                  <div className="bg-muted/50 border border-border rounded-lg p-3 min-h-[48px] max-h-[150px] overflow-y-auto">
                    {formExcludedModels.length === 0 ? (
                      <div className="text-center text-muted-foreground text-xs py-1">未设置排除模型</div>
                    ) : (
                      <div className="flex flex-wrap gap-2">
                        {formExcludedModels.map((item, idx) => (
                          <span key={idx} className="bg-orange-500/10 border border-orange-500/30 text-orange-600 dark:text-orange-400 text-xs font-mono px-2 py-1 rounded flex items-center gap-1">
                            {item}
                            <button
                              title="移除"
                              onClick={() => setFormExcludedModels(formExcludedModels.filter((_, i) => i !== idx))}
                              className="opacity-60 hover:opacity-100 ml-0.5"
                            >
                              <X className="w-3 h-3" />
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={excludedModelInput}
                      onChange={e => setExcludedModelInput(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          const parts = excludedModelInput.split(/[,]+/).map(s => s.trim()).filter(Boolean);
                          if (parts.length > 0) {
                            setFormExcludedModels([...new Set([...formExcludedModels, ...parts])]);
                            setExcludedModelInput('');
                          }
                        }
                      }}
                      placeholder="输入模型名、模型前缀* 或 渠道名/模型名"
                      className="flex-1 bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono text-foreground"
                    />
                    <button
                      onClick={() => {
                        const parts = excludedModelInput.split(/[,]+/).map(s => s.trim()).filter(Boolean);
                        if (parts.length > 0) {
                          setFormExcludedModels([...new Set([...formExcludedModels, ...parts])]);
                          setExcludedModelInput('');
                        }
                      }}
                      className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg text-sm"
                    >
                      添加
                    </button>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    支持三种格式：
                    <code className="bg-muted px-1 rounded">模型名</code>
                    、<code className="bg-muted px-1 rounded">模型名前缀*</code>
                    、<code className="bg-muted px-1 rounded">渠道名/模型名</code>
                    。渠道名/模型名同样支持 <code className="bg-muted px-1 rounded">*</code> 前缀通配。
                  </p>
                </div>
              </section>


              {/* Models Section */}
              <section className="space-y-4">
                <div className="text-sm font-semibold text-foreground border-b border-border pb-2 flex items-center gap-2">
                  <Brain className="w-4 h-4 text-purple-500" /> 模型配置
                </div>

                {/* Actions */}
                <div className="flex items-center gap-2">
                  <button
                    onClick={openFetchModelsDialog}
                    disabled={fetchingModels}
                    className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg text-sm flex items-center gap-1.5 disabled:opacity-50"
                  >
                    <Download className={`w-4 h-4 ${fetchingModels ? 'animate-spin' : ''}`} /> 获取模型
                  </button>
                  <button onClick={clearAllModels} className="bg-red-500/10 text-red-600 dark:text-red-500 px-3 py-2 rounded-lg text-sm">清空全部</button>
                </div>

                {/* Model Chips */}
                <div className="bg-muted/50 border border-border rounded-lg p-3 min-h-[100px] max-h-[200px] overflow-y-auto">
                  {formModels.length === 0 ? (
                    <div className="text-center text-muted-foreground text-sm py-4">暂无模型规则，点击「获取模型」或手动添加。留空表示默认 all。</div>
                  ) : (
                    <div className="flex flex-wrap gap-2">
                      {formModels.map((m, idx) => (
                        <span
                          key={idx}
                          className="bg-background border border-border text-foreground text-xs font-mono px-2 py-1 rounded flex items-center gap-1 cursor-pointer hover:bg-muted"
                          onClick={() => copyToClipboard(m)}
                          title="点击复制"
                        >
                          {m}
                          <button onClick={(e) => { e.stopPropagation(); removeModel(m); }} className="text-muted-foreground hover:text-red-500 ml-1">
                            <X className="w-3 h-3" />
                          </button>
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                {/* Manual Input */}
                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">手动输入模型规则</label>
                  <div className="flex gap-2">
                    <input
                      type="text" value={modelInput} onChange={e => setModelInput(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addModelsFromInput(); } }}
                      placeholder="例如 all, gpt-4o 用空格/逗号分隔"
                      className="flex-1 bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono text-foreground"
                    />
                    <button onClick={addModelsFromInput} className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg text-sm">添加</button>
                  </div>
                  {/* 修改原因：BYOK 渠道需要通过“渠道名/*”这类模型规则授权全部模型。 */}
                  {/* 修改方式：仅扩展手动输入模型规则下方的说明文字，并加入代码样式示例。 */}
                  {/* 目的：让管理员在配置模型访问规则时能直接看到 BYOK 授权写法。 */}
                  <p className="text-xs text-muted-foreground mt-1">多个用逗号或空格分隔，按回车快速添加。BYOK 用户可用 <code className="font-mono bg-muted px-1 rounded">渠道名/*</code> 授权访问指定 BYOK 渠道的所有模型</p>
                </div>
              </section>
            </div>

            {/* Footer */}
            <div className="p-4 bg-muted/30 border-t border-border flex justify-end gap-3">
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              <button onClick={handleSave} className="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary hover:bg-primary/90 rounded-lg flex items-center gap-1.5">
                <Save className="w-4 h-4" /> 保存
              </button>
            </div>

            {/* Plugin Tab Button — 与渠道编辑面板保持一致 */}
            {!showPluginSheet && (
              <button
                onClick={() => setShowPluginSheet(true)}
                className="absolute hidden sm:flex flex-col items-center gap-1.5 py-4 w-8 bg-muted border border-border border-r-0 rounded-l-lg cursor-pointer transition-all hover:bg-emerald-500/10 hover:w-9"
                style={{ left: 0, top: '25%', transform: 'translate(-100%, -50%)', writingMode: 'vertical-rl', textOrientation: 'mixed' }}
              >
                <Puzzle className="w-4 h-4 text-emerald-500" style={{ writingMode: 'horizontal-tb' }} />
                <span className="text-xs font-semibold text-emerald-500 tracking-wider">插件</span>
                <span className="text-[10px] font-medium bg-emerald-500 text-white rounded-full px-1.5 min-w-[18px] text-center" style={{ writingMode: 'horizontal-tb' }}>
                  {formEnabledPlugins.length}
                </span>
              </button>
            )}

            <InterceptorSheet
              open={showPluginSheet}
              onOpenChange={setShowPluginSheet}
              allPlugins={allPlugins}
              enabledPlugins={formEnabledPlugins}
              providerPreferences={currentKeyPluginPreferences}
              title="Key 插件配置"
              description="勾选要在本 API Key 启用的插件拦截器。可为每个插件配置参数，保存后会先写回当前 Key 编辑表单。"
              returnLabel="返回 Key 编辑"
              onUpdate={handleKeyPluginSheetUpdate}
            />
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* ========== Fetch Models Dialog ========== */}
      <Dialog.Root open={isFetchModelsOpen} onOpenChange={setIsFetchModelsOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] max-w-[95vw] max-h-[80vh] bg-background border border-border rounded-xl shadow-2xl z-50 flex flex-col">
            <div className="p-5 border-b border-border">
              <Dialog.Title className="text-lg font-bold text-foreground">选择模型</Dialog.Title>
              <p className="text-sm text-muted-foreground mt-1">当前分组: {formGroups.join(', ')}</p>
            </div>

            <div className="p-4 border-b border-border">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <input
                  type="text" value={modelSearchQuery} onChange={e => setModelSearchQuery(e.target.value)}
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
                const isExisting = formModels.includes(model);
                return (
                  <div
                    key={model}
                    onClick={() => toggleModelSelect(model)}
                    className="px-4 py-2.5 flex items-center hover:bg-muted cursor-pointer border-b border-border last:border-b-0"
                  >
                    <div className={`w-5 h-5 rounded border-2 flex items-center justify-center mr-3 transition-colors ${isSelected ? 'bg-primary border-primary' : 'border-muted-foreground/50'}`}>
                      {isSelected && <Check className="w-3 h-3 text-primary-foreground" />}
                    </div>
                    <span className="flex-1 font-mono text-sm text-foreground truncate">{model}</span>
                    {isExisting && <span className="text-xs bg-primary/20 text-primary px-2 py-0.5 rounded">已添加</span>}
                  </div>
                );
              })}
            </div>

            <div className="p-4 border-t border-border flex justify-end gap-3">
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              <button onClick={confirmFetchModels} className="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary hover:bg-primary/90 rounded-lg">
                确认选择 ({selectedModels.size})
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* ========== Add Credits Dialog ========== */}
      <Dialog.Root open={isCreditsOpen} onOpenChange={setIsCreditsOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[400px] max-w-[95vw] bg-background border border-border rounded-xl shadow-2xl z-50 p-6">
            <Dialog.Title className="text-lg font-bold text-foreground flex items-center gap-2 mb-4">
              <Wallet className="w-5 h-5 text-emerald-500" /> 为 API Key 添加额度
            </Dialog.Title>

            <div className="text-sm text-muted-foreground break-all bg-muted p-3 rounded-lg font-mono border border-border mb-4">
              目标: {creditsTargetKey.slice(0, 15)}...
            </div>

            <div className="mb-6">
              <label className="text-sm font-medium text-foreground mb-1.5 block">增加额度</label>
              <input
                type="number" value={creditsAmount} onChange={e => setCreditsAmount(e.target.value)}
                placeholder="例如 100"
                autoFocus
                className="w-full bg-background border border-border focus:border-emerald-500 px-3 py-2.5 rounded-lg text-sm text-foreground"
              />
              <p className="text-xs text-muted-foreground mt-2">单位与统计模块中的 credits 相同，必须为正数</p>
            </div>

            <div className="flex justify-end gap-3">
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              <button onClick={handleAddCredits} className="px-4 py-2 text-sm font-medium text-white bg-emerald-600 hover:bg-emerald-500 rounded-lg">确认添加</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Key Analytics Sheet */}
      <KeyAnalyticsSheet
        open={analyticsOpen}
        onOpenChange={handleAnalyticsOpenChange}
        apiKeyValue={analyticsKey?.api || ''}
        apiKeyName={analyticsKey?.name}
      />
    </div>
  );
}
