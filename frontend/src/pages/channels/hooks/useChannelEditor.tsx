/* eslint-disable @typescript-eslint/no-explicit-any */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type Dispatch,
  type KeyboardEvent,
  type SetStateAction,
} from 'react';

import { apiFetch } from '../../../lib/api';
import { createApiKeyClientId } from '../../../lib/apiKeyClientId';
import type { EnabledPluginValue } from '../../../lib/pluginEntries';
import { toastError, toastSuccess, toastWarning, fmtErr } from '../../../components/Toast';
import { useChannelOAuth } from './useChannelOAuth';
import type { UseChannelsCoreResult } from './useChannelsCore';
import type {
  ApiKeyObj,
  HeaderEntry,
  ModelMapping,
  ProviderFormData,
  SubChannelFormData,
} from '../types';
import {
  buildProviderApiPath,
  readBooleanPreference,
  serializeChannelPreferences,
  sortProvidersByWeight,
} from '../utils';
export interface KeyTestOverride {
  engine: string;
  base_url: string;
  models: string[];
  title: string;
}

export interface OAuthManualState {
  idx: number;
  state: string;
  provider: string;
}

// 修改原因：Radix Dialog 的 react-remove-scroll 会用带 !important 的 CSS 改写 body 定位，普通内联样式不足以稳定保留移动端列表位置。
// 修改方式：保存 body 定位相关内联样式的值和优先级，恢复时连同优先级一并还原。
// 目的：让滚动锁可以使用 inline !important 对抗 Radix 样式，同时关闭弹窗后不污染页面原样式。
type ChannelModalBodyStyleSnapshot = {
  position: string;
  positionPriority: string;
  top: string;
  topPriority: string;
  width: string;
  widthPriority: string;
};

export interface UseChannelEditorResult {
  isModalOpen: boolean;
  setIsModalOpen: Dispatch<SetStateAction<boolean>>;
  originalIndex: number | null;
  setOriginalIndex: Dispatch<SetStateAction<number | null>>;
  formData: ProviderFormData | null;
  setFormData: Dispatch<SetStateAction<ProviderFormData | null>>;
  channelModalScrollYRef: React.MutableRefObject<number>;
  channelModalBodyStyleRef: React.MutableRefObject<ChannelModalBodyStyleSnapshot | null>;
  editingSubChannel: { parentIdx: number; subIdx: number } | null;
  setEditingSubChannel: Dispatch<SetStateAction<{ parentIdx: number; subIdx: number } | null>>;
  groupInput: string;
  setGroupInput: Dispatch<SetStateAction<string>>;
  modelInput: string;
  setModelInput: Dispatch<SetStateAction<string>>;
  fetchingModels: boolean;
  setFetchingModels: Dispatch<SetStateAction<boolean>>;
  copiedModels: boolean;
  setCopiedModels: Dispatch<SetStateAction<boolean>>;
  showPluginSheet: boolean;
  setShowPluginSheet: Dispatch<SetStateAction<boolean>>;
  testDialogOpen: boolean;
  setTestDialogOpen: Dispatch<SetStateAction<boolean>>;
  testingProvider: any;
  setTestingProvider: Dispatch<SetStateAction<any>>;
  headerEntries: HeaderEntry[];
  setHeaderEntries: Dispatch<SetStateAction<HeaderEntry[]>>;
  keyTestDialogOpen: boolean;
  setKeyTestDialogOpen: Dispatch<SetStateAction<boolean>>;
  keyTestInitialIndex: number | null;
  setKeyTestInitialIndex: Dispatch<SetStateAction<number | null>>;
  keyTestOverride: KeyTestOverride | null;
  setKeyTestOverride: Dispatch<SetStateAction<KeyTestOverride | null>>;
  overridesJson: string;
  setOverridesJson: Dispatch<SetStateAction<string>>;
  statusCodeOverridesJson: string;
  setStatusCodeOverridesJson: Dispatch<SetStateAction<string>>;
  modelDisplayKey: number;
  setModelDisplayKey: Dispatch<SetStateAction<number>>;
  analyticsOpen: boolean;
  setAnalyticsOpen: Dispatch<SetStateAction<boolean>>;
  analyticsProvider: string;
  setAnalyticsProvider: Dispatch<SetStateAction<string>>;
  oauthAccounts: Record<string, any>;
  setOauthAccounts: Dispatch<SetStateAction<Record<string, any>>>;
  oauthKeyFocusSnapshotRef: React.MutableRefObject<Record<number, string>>;
  importModalIdx: number | null;
  setImportModalIdx: Dispatch<SetStateAction<number | null>>;
  importToken: string;
  setImportToken: Dispatch<SetStateAction<string>>;
  importing: boolean;
  setImporting: Dispatch<SetStateAction<boolean>>;
  oauthManualState: OAuthManualState | null;
  setOauthManualState: Dispatch<SetStateAction<OAuthManualState | null>>;
  manualUrl: string;
  setManualUrl: Dispatch<SetStateAction<string>>;
  exchanging: boolean;
  setExchanging: Dispatch<SetStateAction<boolean>>;
  isOAuthOverlayOpen: boolean;
  selectedChannelType: UseChannelsCoreResult['channelTypes'][number] | undefined;
  isOAuthEngine: boolean;
  rawImportPlaceholderValue: any;
  rawImportPlaceholder: string | undefined;
  importPlaceholder: string;
  isFetchModelsOpen: boolean;
  setIsFetchModelsOpen: Dispatch<SetStateAction<boolean>>;
  fetchedModels: string[];
  setFetchedModels: Dispatch<SetStateAction<string[]>>;
  selectedModels: Set<string>;
  setSelectedModels: Dispatch<SetStateAction<Set<string>>>;
  modelSearchQuery: string;
  setModelSearchQuery: Dispatch<SetStateAction<string>>;
  allPlugins: UseChannelsCoreResult['allPlugins'];
  channelTypes: UseChannelsCoreResult['channelTypes'];
  globalModelPrice: UseChannelsCoreResult['globalModelPrice'];
  balanceResults: UseChannelsCoreResult['balanceResults'];
  setBalanceResults: UseChannelsCoreResult['setBalanceResults'];
  balanceLoading: boolean;
  focusedKeyIdx: number | null;
  setFocusedKeyIdx: UseChannelsCoreResult['setFocusedKeyIdx'];
  forceListMode: boolean;
  setForceListMode: UseChannelsCoreResult['setForceListMode'];
  runtimeKeyStatus: UseChannelsCoreResult['runtimeKeyStatus'];
  localCountdowns: UseChannelsCoreResult['localCountdowns'];
  token: string | null;
  refreshKeyStatus: UseChannelsCoreResult['refreshKeyStatus'];
  restoreChannelModalScrollLock: () => void;
  applyChannelModalScrollLock: () => void;
  refreshOAuthAccounts: () => Promise<void>;
  openModal: (provider?: any, index?: number | null) => Promise<void>;
  updateFormData: (field: keyof ProviderFormData, value: any) => void;
  updatePreference: (field: keyof ProviderFormData['preferences'], value: any) => void;
  updateModelPrefix: (value: string) => void;
  queryAllBalances: (silent?: boolean) => Promise<void>;
  addEmptyKey: () => void;
  updateKey: (idx: number, keyStr: string) => void;
  handleOAuthKeyFocus: (idx: number, keyStr: string) => void;
  handleOAuthKeyBlur: (idx: number, newValue: string) => Promise<void>;
  openImportModal: (idx: number) => void;
  doImport: () => Promise<void>;
  startOAuthLogin: (idx: number) => Promise<void>;
  doManualExchange: () => Promise<void>;
  toggleKeyDisabled: (idx: number) => void;
  deleteKey: (idx: number) => Promise<void>;
  handleKeyPaste: (event: ClipboardEvent<HTMLInputElement>, idx: number) => void;
  copyAllKeys: () => void;
  exportOAuthCredentials: () => Promise<void>;
  clearAllKeys: () => void;
  handleGroupInputKeyDown: (event: KeyboardEvent<HTMLInputElement>) => void;
  removeGroup: (groupToRemove: string) => void;
  handleModelInputKeyDown: (event: KeyboardEvent<HTMLInputElement>) => void;
  openFetchModelsDialog: () => Promise<void>;
  toggleModelSelect: (model: string) => void;
  filteredFetchedModels: string[];
  selectAllVisible: () => void;
  deselectAllVisible: () => void;
  confirmFetchModels: () => void;
  copyAllModels: () => void;
  getAliasMap: () => Map<string, string>;
  getModelDisplayName: (model: string) => string;
  formatJsonOnBlur: (value: string, setter: (value: string) => void, fieldName: string) => void;
  handleMappingChange: (idx: number, field: 'from' | 'to', value: string) => void;
  handlePluginSheetUpdate: (payload: { enabled_plugins: EnabledPluginValue[]; preferences_patch: Record<string, any>; preferences_delete: string[] }) => void;
  handleDeleteProvider: (idx: number) => Promise<void>;
  handleToggleProvider: (idx: number) => Promise<void>;
  handleCopyProvider: (provider: any) => void;
  handleToggleSubChannel: (parentIdx: number, subIdx: number) => Promise<void>;
  handleDeleteSubChannel: (parentIdx: number, subIdx: number) => Promise<void>;
  openSubChannelEdit: (parentIdx: number, subIdx: number) => Promise<void>;
  buildSubChannelProvider: (parentIdx: number, subIdx: number) => any | null;
  sortByWeight: (list: any[]) => any[];
  handleUpdateWeight: (idx: number, newWeight: number) => Promise<void>;
  openTestDialog: (provider: any) => void;
  openKeyTestDialog: (initialIndex?: number | null, subOverride?: KeyTestOverride) => void;
  buildProviderSnapshotForTest: () => any;
  getProviderModelNameListForUi: () => string[];
  disableKeysInForm: (indices: number[]) => void;
  handleSave: () => Promise<void>;
  getProviderModelNames: UseChannelsCoreResult['getProviderModelNames'];
  getProviderAnalyticsName: UseChannelsCoreResult['getProviderAnalyticsName'];
}

export function useChannelEditor(core: UseChannelsCoreResult): UseChannelEditorResult {
  const {
    providers,
    setProviders,
    channelTypes,
    allPlugins,
    balanceResults,
    setBalanceResults,
    balanceLoading,
    setBalanceLoading,
    focusedKeyIdx,
    setFocusedKeyIdx,
    forceListMode,
    setForceListMode,
    globalModelPrice,
    runtimeKeyStatus,
    localCountdowns,
    token,
    refreshProviders,
    refreshSingleProvider,
    refreshKeyStatus,
    getProviderModelNames,
    getProviderAnalyticsName,
  } = core;

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [originalIndex, setOriginalIndex] = useState<number | null>(null);
  const [formData, setFormData] = useState<ProviderFormData | null>(null);
  const channelModalScrollYRef = useRef(0);
  const channelModalBodyStyleRef = useRef<ChannelModalBodyStyleSnapshot | null>(null);
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
  const [keyTestOverride, setKeyTestOverride] = useState<KeyTestOverride | null>(null);
  const [overridesJson, setOverridesJson] = useState('');
  const [statusCodeOverridesJson, setStatusCodeOverridesJson] = useState('');
  const [modelDisplayKey, setModelDisplayKey] = useState(0);
  const [analyticsOpen, setAnalyticsOpen] = useState(false);
  const [analyticsProvider, setAnalyticsProvider] = useState('');
  const [isFetchModelsOpen, setIsFetchModelsOpen] = useState(false);
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(() => new Set());
  const [modelSearchQuery, setModelSearchQuery] = useState('');

  const oauth = useChannelOAuth({
    // 修改原因：OAuth 状态和账号同步已迁到独立 hook，但其他编辑逻辑仍需要使用 isOAuthEngine 与账号操作。
    // 修改方式：useChannelEditor 只传入当前表单、弹窗状态、渠道类型和必要 setter，并把返回值合并到最终结果。
    // 目的：保持本轮只拆 OAuth 与 Key 行渲染，不扩大到 CRUD、Key 管理、子渠道和模型获取逻辑。
    token,
    formData,
    setFormData,
    isModalOpen,
    channelTypes,
    setFocusedKeyIdx,
  });
  const {
    oauthAccounts,
    setOauthAccounts,
    isOAuthEngine,
    importModalIdx,
    setImportModalIdx,
    importToken,
    setImportToken,
    importing,
    setImporting,
    oauthManualState,
    setOauthManualState,
    manualUrl,
    setManualUrl,
    exchanging,
    setExchanging,
  } = oauth;

  const restoreChannelModalScrollLock = useCallback(() => {
    if (!channelModalBodyStyleRef.current) return;
    channelModalBodyStyleRef.current = null;
    const scrollY = channelModalScrollYRef.current;
    // Radix Dialog 关闭后会异步恢复 body style，需要等它完成再滚动
    requestAnimationFrame(() => {
      window.scrollTo(0, scrollY);
    });
  }, []);

  const applyChannelModalScrollLock = useCallback(() => {
    if (channelModalBodyStyleRef.current) return;
    const currentScrollY = window.scrollY || window.pageYOffset || document.documentElement.scrollTop || 0;
    channelModalScrollYRef.current = currentScrollY;
    channelModalBodyStyleRef.current = { locked: true } as any;
  }, []);

  const isChannelScrollLockedDialogOpen = isModalOpen || testDialogOpen || keyTestDialogOpen;

  useEffect(() => {
    // 修改原因：旧逻辑把 restoreChannelModalScrollLock 放在依赖 isModalOpen 的 effect cleanup 中，false → true 打开弹窗时会先执行旧 cleanup，导致刚设置的滚动锁被立即清掉。
    // 修改方式：关闭状态变化只在所有受保护弹窗都关闭后恢复；组件卸载恢复放到独立 effect，避免打开流程触发恢复。
    // 目的：保证编辑面板、渠道测试面板和 Key 测试面板打开期间滚动锁持续存在。
    if (!isChannelScrollLockedDialogOpen) restoreChannelModalScrollLock();
  }, [isChannelScrollLockedDialogOpen, restoreChannelModalScrollLock]);

  useEffect(() => {
    // 修改原因：滚动锁仍需要在页面组件卸载时兜底恢复，但不能在每次打开状态变化时执行 cleanup。
    // 修改方式：使用只依赖稳定 restore 回调的独立 effect，cleanup 只会在组件卸载时运行。
    // 目的：避免路由切换残留 body 样式，同时不破坏弹窗打开时的锁定状态。
    return restoreChannelModalScrollLock;
  }, [restoreChannelModalScrollLock]);

  const queryAllBalances = useCallback(async (silent = false) => {
    // 修改原因：余额请求的 loading 和结果属于核心运行时数据，但当前表单和 OAuth 账号状态已迁入编辑 hook。
    // 修改方式：调用核心 hook 暴露的 queryAllBalances，并显式传入当前表单、OAuth 标记和账号状态 setter。
    // 目的：保留原并发查询和 OAuth quota 同步逻辑，同时避免把编辑状态放回核心 hook。
    await core.queryAllBalances({ formData, isOAuthEngine, setOauthAccounts }, silent);
  }, [core, formData, isOAuthEngine]);

  const openModal = async (provider: any = null, index: number | null = null) => {
    setOriginalIndex(index);
    setGroupInput('');
    setModelInput('');
    setShowPluginSheet(false);
    void refreshKeyStatus();
    setBalanceResults({});
    setBalanceLoading(false);
    setFocusedKeyIdx(null);

    if (provider) {
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

      const activeProvider = freshProvider;
      const parseApiKey = (raw: any): ApiKeyObj => {
        if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
          const entries = Object.entries(raw);
          if (entries.length === 1) {
            const [k, v] = entries[0];
            const trimmed = String(k).trim();
            const label = v ? String(v).trim() : undefined;
            if (trimmed.startsWith('!')) return { _clientId: createApiKeyClientId(), key: trimmed.substring(1), disabled: true, label };
            return { _clientId: createApiKeyClientId(), key: trimmed, disabled: false, label };
          }
        }
        const trimmed = String(raw).trim();
        if (trimmed.startsWith('!')) return { _clientId: createApiKeyClientId(), key: trimmed.substring(1), disabled: true };
        return { _clientId: createApiKeyClientId(), key: trimmed, disabled: false };
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
        else if (m && typeof m === 'object') Object.entries(m).forEach(([upstream, alias]) => mappings.push({ from: alias as string, to: upstream }));
      });

      let groups = ['default'];
      if (Array.isArray(activeProvider.groups) && activeProvider.groups.length > 0) groups = activeProvider.groups;
      else if (typeof activeProvider.group === 'string' && activeProvider.group.trim()) groups = [activeProvider.group.trim()];
      else if (activeProvider.preferences?.group) groups = [activeProvider.preferences.group.trim()];

      const pHeaders = activeProvider.preferences?.headers || {};
      const pOverrides = activeProvider.preferences?.post_body_parameter_overrides || {};
      const entries: HeaderEntry[] = [];
      Object.entries(pHeaders).forEach(([k, v]) => {
        if (Array.isArray(v)) v.forEach(item => entries.push({ key: k, value: String(item).trim() }));
        else entries.push({ key: k, value: String(v).trim() });
      });
      setHeaderEntries(entries);
      setOverridesJson(Object.keys(pOverrides).length > 0 ? JSON.stringify(pOverrides, null, 2) : '');
      const pStatusCodeOverrides = activeProvider.preferences?.status_code_overrides || {};
      setStatusCodeOverridesJson(Object.keys(pStatusCodeOverrides).length > 0 ? JSON.stringify(pStatusCodeOverrides, null, 2) : '');

      const basePreferences = activeProvider.preferences && typeof activeProvider.preferences === 'object' ? activeProvider.preferences : {};
      const rawSubChannels = Array.isArray(activeProvider.sub_channels) ? activeProvider.sub_channels : [];
      const subChannels: SubChannelFormData[] = rawSubChannels.map((sub: any) => {
        const subRawModels = Array.isArray(sub.model) ? sub.model : Array.isArray(sub.models) ? sub.models : [];
        const subModels: string[] = [];
        const subMappings: ModelMapping[] = [];
        subRawModels.forEach((m: any) => {
          if (typeof m === 'string') subModels.push(m);
          else if (m && typeof m === 'object') Object.entries(m).forEach(([upstream, alias]) => subMappings.push({ from: alias as string, to: upstream }));
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
          pool_sharing: readBooleanPreference(basePreferences.pool_sharing),
          enabled_plugins: Array.isArray(basePreferences.enabled_plugins) ? basePreferences.enabled_plugins : [],
        },
        sub_channels: subChannels,
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
        preferences: { weight: 10, api_key_schedule_algorithm: 'round_robin', tools: true, pool_sharing: false, enabled_plugins: [], key_rules: [{ match: { status: [401, 403] }, duration: -1 }, { match: 'default', duration: 3 }] },
        sub_channels: [],
      });
    }
    applyChannelModalScrollLock();
    setIsModalOpen(true);
  };

  const updateFormData = (field: keyof ProviderFormData, value: any) => {
    setFormData(prev => prev ? { ...prev, [field]: value } : prev);
  };

  const updatePreference = (field: keyof ProviderFormData['preferences'], value: any) => {
    setFormData(prev => prev ? { ...prev, preferences: { ...prev.preferences, [field]: value } } : prev);
  };

  const updateModelPrefix = (value: string) => {
    setFormData(prev => {
      if (!prev) return prev;
      const nextPreferences = { ...prev.preferences };
      if (!value.trim()) nextPreferences.pool_sharing = false;
      return { ...prev, model_prefix: value, preferences: nextPreferences };
    });
  };

  const addEmptyKey = () => {
    if (!formData) return;
    const newIdx = formData.api_keys.length;
    updateFormData('api_keys', [...formData.api_keys, { _clientId: createApiKeyClientId(), key: '', disabled: false }]);
    setTimeout(() => {
      setFocusedKeyIdx(newIdx);
      requestAnimationFrame(() => {
        const container = document.querySelector('[data-key-scroll]');
        if (container) container.scrollTop = container.scrollHeight;
      });
    }, 0);
  };

  const updateKey = (idx: number, keyStr: string) => {
    if (!formData) return;
    const newKeys = [...formData.api_keys];
    newKeys[idx] = { ...newKeys[idx], key: keyStr };
    updateFormData('api_keys', newKeys);
  };

  const toggleKeyDisabled = (idx: number) => {
    if (!formData) return;
    const newKeys = [...formData.api_keys];
    newKeys[idx] = { ...newKeys[idx], disabled: !newKeys[idx].disabled };
    updateFormData('api_keys', newKeys);
  };

  const deleteKey = async (idx: number) => {
    if (!formData) return;
    const keyValue = (formData.api_keys[idx]?.key || '').trim();
    const providerName = formData.provider.trim();
    if (isOAuthEngine && keyValue && providerName && oauthAccounts[keyValue]) {
      const confirmOAuthDelete = window.confirm(`确定要删除 OAuth 账号 ${keyValue} 吗？\n\n注意：这是即时生效的不可逆操作。删除后 token 将无法恢复，需要重新导入。`);
      if (!confirmOAuthDelete) return;
      try {
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
    // 删除中间项时同步修正聚焦下标，避免后继 Key 顶替被删行的展开/焦点状态。
    setFocusedKeyIdx(current => {
      if (current === null) return null;
      if (current === idx) return null;
      return current > idx ? current - 1 : current;
    });
    updateFormData('api_keys', formData.api_keys.filter((_, i) => i !== idx));
  };

  const handleKeyPaste = (e: ClipboardEvent<HTMLInputElement>, idx: number) => {
    const pastedText = e.clipboardData.getData('text');
    const lines = pastedText.split(/\r?\n|\r/).map(s => s.trim()).filter(Boolean);
    if (lines.length <= 1 || !formData) return;
    e.preventDefault();
    const newKeys = [...formData.api_keys];
    newKeys[idx] = { ...newKeys[idx], key: lines[0] };
    const existingSet = new Set(newKeys.map(k => k.key));
    const newKeyObjs = lines.slice(1).filter(k => !existingSet.has(k)).map(k => ({ _clientId: createApiKeyClientId(), key: k, disabled: false }));
    newKeys.splice(idx + 1, 0, ...newKeyObjs);
    updateFormData('api_keys', newKeys);
  };

  const copyAllKeys = () => {
    if (!formData) return;
    const activeKeys = formData.api_keys.filter(k => !k.disabled && k.key).map(k => k.key);
    if (!activeKeys.length) return;
    void navigator.clipboard.writeText(activeKeys.join('\n'));
    toastSuccess('已复制所有有效密钥');
  };

  const clearAllKeys = () => {
    if (!formData || formData.api_keys.length === 0) return;
    if (!confirm('确定要清空该渠道的全部密钥吗？此操作仅影响当前编辑中的渠道配置，保存后才会生效。')) return;
    updateFormData('api_keys', []);
  };

  const handleGroupInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && groupInput.trim()) {
      e.preventDefault();
      if (formData && !formData.groups.includes(groupInput.trim())) updateFormData('groups', [...formData.groups, groupInput.trim()]);
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
      if (formData) updateFormData('models', Array.from(new Set([...formData.models, ...newModels])));
      setModelInput('');
    }
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
        body: JSON.stringify({ engine: formData.engine, base_url: formData.base_url, api_key: firstKey.key, preferences: formData.preferences }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toastError(err, '获取模型失败');
        return;
      }
      const data = (await res.json()) as any;
      const rawModels: unknown[] = Array.isArray(data) ? data : Array.isArray(data?.models) ? data.models : Array.isArray(data?.data) ? data.data.map((m: any) => m?.id) : [];
      const uniqueModels = Array.from(new Set(rawModels.map(m => String(m)).filter(Boolean)));
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

  const filteredFetchedModels = useMemo(() => fetchedModels.filter(m => {
    if (!modelSearchQuery) return true;
    const q = modelSearchQuery.toLowerCase();
    const display = getModelDisplayName(m);
    return m.toLowerCase().includes(q) || display.toLowerCase().includes(q);
  }), [fetchedModels, modelSearchQuery, formData?.mappings]);

  const selectAllVisible = () => setSelectedModels(new Set(filteredFetchedModels));

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
    void navigator.clipboard.writeText(formData.models.join(', '));
    setCopiedModels(true);
    setTimeout(() => setCopiedModels(false), 2000);
  };

  const formatJsonOnBlur = (value: string, setter: (value: string) => void, fieldName: string) => {
    if (!value.trim()) return;
    try {
      setter(JSON.stringify(JSON.parse(value), null, 2));
    } catch (err: any) {
      toastWarning(`${fieldName} JSON 格式错误: ${err.message}`);
    }
  };

  const handleMappingChange = (idx: number, field: 'from' | 'to', value: string) => {
    if (!formData) return;
    const newMappings = [...formData.mappings];
    newMappings[idx] = { ...newMappings[idx], [field]: value };
    updateFormData('mappings', newMappings);
    setModelDisplayKey(prev => prev + 1);
  };

  const handlePluginSheetUpdate = (payload: { enabled_plugins: EnabledPluginValue[]; preferences_patch: Record<string, any>; preferences_delete: string[] }) => {
    setFormData(prev => {
      if (!prev) return prev;
      const nextPrefs: Record<string, any> = { ...(prev.preferences || {}) };
      nextPrefs.enabled_plugins = payload.enabled_plugins;
      for (const [k, v] of Object.entries(payload.preferences_patch || {})) nextPrefs[k] = v;
      for (const k of payload.preferences_delete || []) delete nextPrefs[k];
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
      const res = await apiFetch(buildProviderApiPath(providerId), { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } });
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
    const updatedProvider = { ...provider, enabled: provider.enabled === false ? true : false };
    try {
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(updatedProvider),
      });
      if (res.ok) await refreshSingleProvider(providerId);
      else {
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
    copy._copiedFrom = originalName;
    void openModal(copy, null);
    toastSuccess('已复制渠道配置，请修改后保存');
  };

  const handleToggleSubChannel = async (parentIdx: number, subIdx: number) => {
    const parent = providers[parentIdx];
    const providerId = String(parent?.provider || '').trim();
    if (!providerId) {
      toastError('操作失败：主渠道名为空');
      return;
    }
    const subs = [...(parent.sub_channels || [])];
    subs[subIdx] = { ...subs[subIdx], enabled: subs[subIdx].enabled === false ? true : false };
    try {
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ ...parent, sub_channels: subs }),
      });
      if (res.ok) await refreshSingleProvider(providerId);
      else toastError('操作失败');
    } catch {
      toastError('网络错误');
    }
  };

  const handleDeleteSubChannel = async (parentIdx: number, subIdx: number) => {
    const parent = providers[parentIdx];
    const providerId = String(parent?.provider || '').trim();
    if (!providerId) {
      toastError('删除失败：主渠道名为空');
      return;
    }
    const sub = (parent.sub_channels || [])[subIdx];
    const name = sub?.remark || sub?.engine || `子渠道 ${subIdx + 1}`;
    if (!confirm(`确定要删除子渠道 "${name}" 吗？`)) return;
    const subs = (parent.sub_channels || []).filter((_: any, i: number) => i !== subIdx);
    try {
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ ...parent, sub_channels: subs.length > 0 ? subs : undefined }),
      });
      if (res.ok) await refreshSingleProvider(providerId);
      else toastError('删除失败');
    } catch {
      toastError('网络错误');
    }
  };

  const openSubChannelEdit = async (parentIdx: number, subIdx: number) => {
    const parent = providers[parentIdx];
    const providerId = String(parent?.provider || '').trim();
    if (!parent || !providerId) {
      toastError('编辑失败：主渠道名为空');
      return;
    }
    let freshParent = parent;
    try {
      const res = await apiFetch(buildProviderApiPath(providerId), { method: 'GET', headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) {
        const data = await res.json();
        if (data?.provider) {
          freshParent = data.provider;
          setProviders(prev => prev.map((item, idx) => idx === parentIdx ? freshParent : item));
        } else toastWarning('获取主渠道最新数据失败，已使用页面缓存继续编辑');
      } else toastWarning('获取主渠道最新数据失败，已使用页面缓存继续编辑');
    } catch {
      toastWarning('获取主渠道最新数据失败，已使用页面缓存继续编辑');
    }
    const sub = (freshParent.sub_channels || [])[subIdx];
    if (!sub) {
      toastError('编辑失败：子渠道不存在或已被删除');
      return;
    }
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
      preferences: { ...(freshParent.preferences || {}), ...(sub.preferences || {}) },
      sub_channels: [],
    }, null);
  };

  const buildSubChannelProvider = (parentIdx: number, subIdx: number): any | null => {
    const parent = providers[parentIdx];
    const sub = (parent?.sub_channels || [])[subIdx];
    if (!parent || !sub) return null;
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

  const sortByWeight = sortProvidersByWeight;

  const handleUpdateWeight = async (idx: number, newWeight: number) => {
    const provider = providers[idx];
    const providerId = String(provider?.provider || '').trim();
    if (!providerId) {
      toastError('权重更新失败：渠道名为空');
      return;
    }
    const updatedProvider = { ...provider, preferences: { ...(provider.preferences || {}), weight: newWeight } };
    try {
      const res = await apiFetch(buildProviderApiPath(providerId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(updatedProvider),
      });
      if (res.ok) await refreshSingleProvider(providerId);
      else {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), '权重更新失败');
      }
    } catch (err: any) {
      toastError(err?.message || '权重更新失败');
    }
  };

  const openTestDialog = (provider: any) => {
    // 修改原因：渠道测试面板也是 Radix Dialog，从渠道列表直接打开时原来没有任何滚动位置保护。
    // 修改方式：打开测试面板前复用渠道弹窗滚动锁，先记录并固定当前列表位置。
    // 目的：关闭测试面板后恢复到打开前的渠道列表滚动位置。
    applyChannelModalScrollLock();
    setTestingProvider(provider);
    setTestDialogOpen(true);
  };

  const openKeyTestDialog = (initialIndex: number | null = null, subOverride?: KeyTestOverride) => {
    // 修改原因：Key 测试面板可能叠在编辑抽屉上，也可能由列表入口触发，不能只依赖编辑抽屉的打开流程。
    // 修改方式：打开前调用同一滚动锁；已有编辑抽屉锁时 applyChannelModalScrollLock 会直接返回，不覆盖原始 scrollY 快照。
    // 目的：统一保护测试面板关闭后的渠道列表位置，并避免嵌套弹窗互相覆盖恢复数据。
    applyChannelModalScrollLock();
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
    const finalApi = serializedKeys.length === 0 ? '' : serializedKeys.length === 1 ? serializedKeys[0] : serializedKeys;
    const finalModels: any[] = [...formData.models];
    formData.mappings.forEach(m => { if (m.from && m.to) finalModels.push({ [m.to]: m.from }); });
    let headersObj: any = undefined;
    let overridesObj: any = undefined;
    try {
      const h = headerEntries.reduce((acc: Record<string, string>, e) => {
        if (e.key.trim()) acc[e.key.trim()] = e.value.trim();
        return acc;
      }, {});
      if (Object.keys(h).length > 0) headersObj = h;
    } catch { /* ignore */ }
    try { if (overridesJson.trim()) overridesObj = JSON.parse(overridesJson); } catch { /* ignore */ }
    let statusCodeOverridesObj: Record<string, number> | undefined = undefined;
    try { if (statusCodeOverridesJson.trim()) statusCodeOverridesObj = JSON.parse(statusCodeOverridesJson); } catch { /* ignore */ }
    const normalizedPoolSharing = formData.model_prefix.trim() ? !!formData.preferences.pool_sharing : false;
    const serializedPreferences = serializeChannelPreferences(formData.preferences);
    return {
      provider: formData.provider,
      remark: formData.remark || undefined,
      base_url: formData.base_url,
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
      sub_channels: formData.sub_channels.filter(sub => sub.engine).map(sub => ({ engine: sub.engine, model: sub.models.length > 0 ? sub.models : undefined })) || undefined,
    };
  };

  const getProviderModelNameListForUi = (): string[] => {
    if (!formData) return [];
    const prefix = formData.model_prefix || '';
    const aliasMap = getAliasMap();
    const names: string[] = [];
    formData.models.forEach(upstream => {
      const alias = aliasMap.get(upstream);
      const name = alias || upstream;
      names.push(prefix && name !== '*' ? `${prefix}${name}` : name);
    });
    formData.mappings.forEach(m => { if (m.from) names.push(prefix ? `${prefix}${m.from}` : m.from); });
    return Array.from(new Set(names.map(s => String(s || '').trim()).filter(Boolean)));
  };

  const disableKeysInForm = (indices: number[]) => {
    if (!indices.length) return;
    const set = new Set(indices);
    setFormData(prev => prev ? { ...prev, api_keys: prev.api_keys.map((k, idx) => set.has(idx) ? { ...k, disabled: true } : k) } : prev);
  };

  const handleSave = async () => {
    if (!formData?.provider) {
      toastWarning('渠道名称为必填项');
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
    const finalApi = serializedKeys.length === 0 ? '' : serializedKeys.length === 1 ? serializedKeys[0] : serializedKeys;
    const finalModels: any[] = [...formData.models];
    formData.mappings.forEach(m => { if (m.from && m.to) finalModels.push({ [m.to]: m.from }); });

    let overridesObj: any;
    try { if (overridesJson.trim()) overridesObj = JSON.parse(overridesJson); } catch {
      toastWarning('高级配置 JSON 格式错误');
      return;
    }
    let statusCodeOverridesObj: Record<string, number> | undefined;
    try { if (statusCodeOverridesJson.trim()) statusCodeOverridesObj = JSON.parse(statusCodeOverridesJson) as Record<string, number>; } catch {
      toastWarning('错误码映射 JSON 格式错误');
      return;
    }
    const headersObj: Record<string, string | string[]> | undefined = headerEntries.some(e => e.key.trim())
      ? headerEntries.reduce((acc, e) => {
          const k = e.key.trim(), v = e.value.trim();
          if (!k) return acc;
          if (acc[k]) {
            const prev = acc[k];
            acc[k] = Array.isArray(prev) ? [...prev, v] : [prev, v];
          } else acc[k] = v;
          return acc;
        }, {} as Record<string, string | string[]>)
      : undefined;

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

    const normalizedPoolSharing = formData.model_prefix.trim() ? !!formData.preferences.pool_sharing : false;
    const serializedPreferences = serializeChannelPreferences(formData.preferences);
    const serializedSubChannels = formData.sub_channels.filter(sub => sub.engine).map(sub => {
      const subModels: any[] = [...sub.models];
      sub.mappings.forEach(m => { if (m.from && m.to) subModels.push({ [m.to]: m.from }); });
      const subObj: any = { engine: sub.engine, model: subModels.length > 0 ? subModels : undefined };
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
      const { parentIdx, subIdx } = editingSubChannel;
      const parent = providers[parentIdx];
      subChannelParentProviderId = String(parent.provider || '').trim();
      if (!subChannelParentProviderId) {
        toastError('保存失败：主渠道名为空');
        return;
      }
      const parentPrefs = parent.preferences || {};
      const subPrefs: Record<string, any> = {};
      const mergedPrefs = { ...serializedPreferences, pool_sharing: normalizedPoolSharing, model_price: cleanedModelPrice, headers: headersObj, post_body_parameter_overrides: overridesObj, status_code_overrides: statusCodeOverridesObj };
      for (const [k, v] of Object.entries(mergedPrefs)) {
        if (JSON.stringify(v) !== JSON.stringify(parentPrefs[k])) subPrefs[k] = v;
      }
      const subObj: any = { engine: formData.engine, model: finalModels.length > 0 ? finalModels : undefined, enabled: formData.enabled };
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
      const originalProviderId = String(providers[originalIndex]?.provider || '').trim();
      if (!originalProviderId) {
        toastError('保存失败：找不到原渠道名');
        return;
      }
      providerSavePath = buildProviderApiPath(originalProviderId);
      providerSaveMethod = 'PUT';
    }

    try {
      const res = editingSubChannel
        ? await apiFetch(buildProviderApiPath(subChannelParentProviderId), { method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify(newProviders![editingSubChannel.parentIdx]) })
        : await apiFetch(providerSavePath, { method: providerSaveMethod, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify(targetProvider) });
      if (res.ok) {
        if (!editingSubChannel && providerSaveMethod === 'POST' && formData._copiedFrom && isOAuthEngine) {
          const copyStateRes = await apiFetch('/v1/oauth/copy-provider', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
            body: JSON.stringify({ source_provider: formData._copiedFrom, target_provider: formData.provider }),
          });
          if (!copyStateRes.ok) {
            const err = await copyStateRes.json().catch(() => ({}));
            toastError(fmtErr(err, copyStateRes.status), 'OAuth state 复制失败');
          }
        }
        const savedProviderId = editingSubChannel ? subChannelParentProviderId : (providerSaveMethod === 'PUT' ? String(providers[originalIndex!]?.provider || '').trim() : '');
        if (savedProviderId) await refreshSingleProvider(savedProviderId);
        else await refreshProviders();
        setIsModalOpen(false);
        setEditingSubChannel(null);
      } else {
        const err = await res.json().catch(() => ({}));
        toastError(fmtErr(err, res.status), '保存失败');
      }
    } catch (err: any) {
      toastError(err?.message || '网络错误');
    }
  };

  return {
    ...oauth,
    isModalOpen, setIsModalOpen,
    originalIndex, setOriginalIndex,
    formData, setFormData,
    channelModalScrollYRef,
    channelModalBodyStyleRef,
    editingSubChannel, setEditingSubChannel,
    groupInput, setGroupInput,
    modelInput, setModelInput,
    fetchingModels, setFetchingModels,
    copiedModels, setCopiedModels,
    showPluginSheet, setShowPluginSheet,
    testDialogOpen, setTestDialogOpen,
    testingProvider, setTestingProvider,
    headerEntries, setHeaderEntries,
    keyTestDialogOpen, setKeyTestDialogOpen,
    keyTestInitialIndex, setKeyTestInitialIndex,
    keyTestOverride, setKeyTestOverride,
    overridesJson, setOverridesJson,
    statusCodeOverridesJson, setStatusCodeOverridesJson,
    modelDisplayKey, setModelDisplayKey,
    analyticsOpen, setAnalyticsOpen,
    analyticsProvider, setAnalyticsProvider,
    isFetchModelsOpen, setIsFetchModelsOpen,
    fetchedModels, setFetchedModels,
    selectedModels, setSelectedModels,
    modelSearchQuery, setModelSearchQuery,
    allPlugins,
    channelTypes,
    globalModelPrice,
    balanceResults,
    setBalanceResults,
    balanceLoading,
    focusedKeyIdx, setFocusedKeyIdx,
    forceListMode, setForceListMode,
    runtimeKeyStatus,
    localCountdowns,
    token,
    refreshKeyStatus,
    restoreChannelModalScrollLock,
    applyChannelModalScrollLock,
    openModal,
    updateFormData,
    updatePreference,
    updateModelPrefix,
    queryAllBalances,
    addEmptyKey,
    updateKey,
    toggleKeyDisabled,
    deleteKey,
    handleKeyPaste,
    copyAllKeys,
    clearAllKeys,
    handleGroupInputKeyDown,
    removeGroup,
    handleModelInputKeyDown,
    openFetchModelsDialog,
    toggleModelSelect,
    filteredFetchedModels,
    selectAllVisible,
    deselectAllVisible,
    confirmFetchModels,
    copyAllModels,
    getAliasMap,
    getModelDisplayName,
    formatJsonOnBlur,
    handleMappingChange,
    handlePluginSheetUpdate,
    handleDeleteProvider,
    handleToggleProvider,
    handleCopyProvider,
    handleToggleSubChannel,
    handleDeleteSubChannel,
    openSubChannelEdit,
    buildSubChannelProvider,
    sortByWeight,
    handleUpdateWeight,
    openTestDialog,
    openKeyTestDialog,
    buildProviderSnapshotForTest,
    getProviderModelNameListForUi,
    disableKeysInForm,
    handleSave,
    getProviderModelNames,
    getProviderAnalyticsName,
  };
}
