/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';

import { apiFetch } from '../../../lib/api';
import { toastError, toastWarning, fmtErr } from '../../../components/Toast';
import type { ChannelOption, ProviderFormData } from '../types';
import { getOAuthQuota, getQuotaFromSource, normalizeOAuthAccountStateMap } from '../utils';

export interface OAuthManualState {
  idx: number;
  state: string;
  provider: string;
}

export interface UseChannelOAuthParams {
  token: string | null;
  formData: ProviderFormData | null;
  setFormData: Dispatch<SetStateAction<ProviderFormData | null>>;
  isModalOpen: boolean;
  channelTypes: ChannelOption[];
  setFocusedKeyIdx: Dispatch<SetStateAction<number | null>>;
}

export interface UseChannelOAuthResult {
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
  selectedChannelType: ChannelOption | undefined;
  isOAuthEngine: boolean;
  rawImportPlaceholderValue: any;
  rawImportPlaceholder: string | undefined;
  importPlaceholder: string;
  refreshOAuthAccounts: () => Promise<void>;
  handleOAuthKeyFocus: (idx: number, keyStr: string) => void;
  handleOAuthKeyBlur: (idx: number, newValue: string) => Promise<void>;
  openImportModal: (idx: number) => void;
  doImport: () => Promise<void>;
  startOAuthLogin: (idx: number) => Promise<void>;
  doManualExchange: () => Promise<void>;
  exportOAuthCredentials: () => Promise<void>;
}

export function useChannelOAuth({
  token,
  formData,
  setFormData,
  isModalOpen,
  channelTypes,
  setFocusedKeyIdx,
}: UseChannelOAuthParams): UseChannelOAuthResult {
  // 修改原因：OAuth 状态原先混在 useChannelEditor 中，使编辑 hook 同时承担账号同步、导入、登录和 UI 派生职责。
  // 修改方式：把 OAuth 账号、导入弹窗、手动回调和交换状态集中放入独立 hook，由 useChannelEditor 组合返回。
  // 目的：在不继续拆分 CRUD、Key、子渠道等逻辑的前提下，降低 useChannelEditor 的体积和职责范围。
  const [oauthAccounts, setOauthAccounts] = useState<Record<string, any>>({});
  const oauthKeyFocusSnapshotRef = useRef<Record<number, string>>({});
  const [importModalIdx, setImportModalIdx] = useState<number | null>(null);
  const [importToken, setImportToken] = useState('');
  const [importing, setImporting] = useState(false);
  const [oauthManualState, setOauthManualState] = useState<OAuthManualState | null>(null);
  const [manualUrl, setManualUrl] = useState('');
  const [exchanging, setExchanging] = useState(false);

  const isOAuthOverlayOpen = importModalIdx !== null || oauthManualState !== null;
  const selectedChannelType = channelTypes.find(c => c.id === (formData?.engine || ''));
  const isOAuthEngine = selectedChannelType?.is_oauth ?? false;
  const rawImportPlaceholderValue = selectedChannelType?.ui_slots?.import_placeholder;
  const rawImportPlaceholder = typeof rawImportPlaceholderValue === 'string' ? rawImportPlaceholderValue : undefined;
  const importPlaceholder = rawImportPlaceholder && !rawImportPlaceholder.trimStart().startsWith('export')
    ? rawImportPlaceholder
    : 'refresh_token...';

  const updateKeyAtIndex = useCallback((idx: number, keyStr: string) => {
    // 修改原因：OAuth 导入、登录和手动交换成功后需要写回指定 Key 行，但 updateKey 留在 useChannelEditor 中。
    // 修改方式：OAuth hook 直接通过 setFormData 定位 api_keys 下标并替换 key 字符串。
    // 目的：避免让 useChannelOAuth 反向依赖 useChannelEditor 内部 handler，同时保持原有写回行为。
    setFormData(prev => {
      if (!prev) return prev;
      const newKeys = [...prev.api_keys];
      if (!newKeys[idx]) return prev;
      newKeys[idx] = { ...newKeys[idx], key: keyStr };
      return { ...prev, api_keys: newKeys };
    });
  }, [setFormData]);

  const refreshOAuthAccounts = useCallback(async () => {
    // 修改原因：OAuth 账号列表按 provider name 分层保存，打开编辑面板和登录成功后都要读取当前渠道分组。
    // 修改方式：请求 /v1/oauth/accounts?provider=当前渠道名，成功后统一归一化 quota 字段。
    // 目的：避免不同 OAuth 渠道之间串读同邮箱账号状态。
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
      setOauthAccounts(normalizeOAuthAccountStateMap(data));
    } catch {
      setOauthAccounts({});
    }
  }, [token, formData?.provider]);

  useEffect(() => {
    // 修改原因：OAuth 账号状态只在 OAuth 编辑面板打开时有意义，关闭后继续保留会造成渠道间状态串扰。
    // 修改方式：弹窗打开且当前渠道为 OAuth 引擎时刷新账号；弹窗关闭时清空账号缓存。
    // 目的：保持账号列表随当前 provider 同步，并减少无效状态残留。
    if (isModalOpen && isOAuthEngine) {
      void refreshOAuthAccounts();
    } else if (!isModalOpen) {
      setOauthAccounts({});
    }
  }, [isModalOpen, isOAuthEngine, refreshOAuthAccounts]);

  useEffect(() => {
    // 修改原因：OAuth 面板打开后，已有账号如果缺少标准 quota，需要自动批量补查一次余额。
    // 修改方式：筛出 active 且无 quota 的账号，调用 /v1/channels/balance 后只写入标准 quota_inner/quota_outer 和 raw。
    // 目的：让 OAuth Key 行无需用户手动点击余额也能显示后端可得的配额信息。
    if (!isModalOpen || !isOAuthEngine) return;
    const providerName = (formData?.provider || '').trim();
    if (!providerName) return;
    const targets = Object.entries(oauthAccounts).filter(([, account]) => {
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

    setOauthAccounts(prev => {
      const next = { ...prev };
      for (const [keyId] of targets) {
        if (next[keyId]) next[keyId] = { ...next[keyId], _quota_loading: true };
      }
      return next;
    });

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
              const quotaResult = getQuotaFromSource(result);
              const hasQuota = quotaResult?.quota_inner != null || quotaResult?.quota_outer != null;
              next[keyId] = {
                ...accountWithoutLoading,
                ...(quotaResult?.quota_inner != null ? { quota_inner: quotaResult.quota_inner } : {}),
                ...(quotaResult?.quota_outer != null ? { quota_outer: quotaResult.quota_outer } : {}),
                ...(result.raw ? { quota_raw: result.raw } : {}),
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

  const handleOAuthKeyFocus = (idx: number, keyStr: string) => {
    oauthKeyFocusSnapshotRef.current[idx] = keyStr;
    setFocusedKeyIdx(idx);
  };

  const handleOAuthKeyBlur = async (idx: number, newValue: string) => {
    // 修改原因：OAuth Key 文本框展示的是账号标识，用户改名后必须同步后端 oauth_state。
    // 修改方式：失焦时比较 focus 前快照和当前值，必要时调用 rename 接口，失败则回滚输入框。
    // 目的：保持表单中的账号标识和后端保存的 OAuth 账号一致。
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
        updateKeyAtIndex(idx, oldValue);
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
      void refreshOAuthAccounts();
    } catch (err: any) {
      updateKeyAtIndex(idx, oldValue);
      toastError(err?.message || '网络错误', 'OAuth 账号重命名失败');
    }
  };

  const openImportModal = (idx: number) => {
    setImportModalIdx(idx);
    setImportToken('');
  };

  const doImport = async () => {
    // 修改原因：手动导入 refresh_token 只属于 OAuth 渠道，放在独立 hook 后仍需写回当前 Key 行。
    // 修改方式：导入成功后用 updateKeyAtIndex 替换目标行，并更新本地 oauthAccounts 缓存。
    // 目的：保持旧导入行为，同时让 useChannelEditor 不再持有导入状态和提交逻辑。
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
        body: JSON.stringify({ provider: providerName, key_id: keyId, type: formData.engine, refresh_token: importToken.trim() }),
      });
      if (res.ok) {
        const data = await res.json();
        if (!data.already_exists) updateKeyAtIndex(importModalIdx, data.key_id || keyId);
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
    // 修改原因：浏览器 OAuth 登录包含 authorize 请求、弹窗和 postMessage 回调，必须整体随 OAuth hook 迁移。
    // 修改方式：在 hook 内注册 message 监听器，成功或超时后移除监听器，并把 key_id 写回当前 Key 行。
    // 目的：避免拆分后丢失回调清理逻辑，防止重复监听导致重复写入。
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
        setOauthManualState({ idx, state, provider: providerName });
        setManualUrl('');
        return;
      }
      let timeoutId: number | undefined;
      const handler = (event: MessageEvent) => {
        if (event.data?.type !== 'oauth_callback_success') return;
        if (event.data?.state && event.data.state !== state) return;
        if (event.data?.provider && event.data.provider !== providerName) return;
        window.removeEventListener('message', handler);
        if (timeoutId !== undefined) window.clearTimeout(timeoutId);
        const keyId = event.data.key_id;
        if (keyId && !event.data.already_exists) updateKeyAtIndex(idx, keyId);
        void refreshOAuthAccounts();
        if (!authWindow.closed) authWindow.close();
      };
      window.addEventListener('message', handler);
      timeoutId = window.setTimeout(() => window.removeEventListener('message', handler), 300000);
    } catch (e) {
      toastError(e instanceof Error ? e.message : String(e), '登录出错');
    }
  };

  const doManualExchange = async () => {
    // 修改原因：manual OAuth 模式由用户粘贴回调 URL 完成，状态和提交逻辑属于 OAuth hook。
    // 修改方式：解析 code/state 后调用 exchange 接口，成功后写回 Key 行并刷新账号列表。
    // 目的：保持无法自动回调时的登录路径可用。
    if (!oauthManualState || !manualUrl.trim()) return;
    setExchanging(true);
    try {
      // 修改原因：Anthropic 新的 redirect_uri (platform.claude.com) 不再在 URL 参数中携带 code，
      //   而是在页面上显示纯文本的 authentication code 让用户复制。
      // 修改方式：先尝试解析为 URL（兼容旧流程），失败则将整个输入作为纯 auth code 处理。
      // 目的：同时支持粘贴完整回调 URL 和纯 authentication code 文本。
      const input = manualUrl.trim();
      let code: string | null = null;
      let callbackState: string | null = null;
      try {
        const url = new URL(input);
        code = url.searchParams.get('code');
        callbackState = url.searchParams.get('state');
      } catch {
        // 不是合法 URL，将整个输入作为 auth code（可能包含 #state 后缀）
        code = input;
      }
      if (!code) {
        toastError('未找到 authorization code');
        return;
      }
      if (callbackState && callbackState !== oauthManualState.state) {
        toastError('state 不匹配，可能不是本次登录的回调');
        return;
      }
      const res = await apiFetch('/v1/oauth/exchange', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ provider: oauthManualState.provider, code, state: oauthManualState.state }),
      });
      if (res.ok) {
        const data = await res.json();
        if (!data.already_exists) updateKeyAtIndex(oauthManualState.idx, data.key_id || '');
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

  const exportOAuthCredentials = async () => {
    // 修改原因：OAuth 凭据导出只服务 OAuth 账号迁移和备份，不应留在通用编辑 hook 中。
    // 修改方式：按当前 provider 请求导出接口，成功后生成本地 JSON 下载。
    // 目的：保持导出能力不变，同时让 OAuth 相关后端交互集中维护。
    if (!formData) return;
    const providerName = formData.provider.trim();
    if (!providerName) {
      toastError('渠道名为空，无法导出 OAuth 凭证');
      return;
    }
    try {
      const res = await apiFetch(`/v1/oauth/export?provider=${encodeURIComponent(providerName)}`, { headers: { Authorization: `Bearer ${token}` } });
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
    } catch (err: any) {
      toastError(err?.message || '网络错误', '导出失败');
    }
  };

  return {
    oauthAccounts,
    setOauthAccounts,
    oauthKeyFocusSnapshotRef,
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
    isOAuthOverlayOpen,
    selectedChannelType,
    isOAuthEngine,
    rawImportPlaceholderValue,
    rawImportPlaceholder,
    importPlaceholder,
    refreshOAuthAccounts,
    handleOAuthKeyFocus,
    handleOAuthKeyBlur,
    openImportModal,
    doImport,
    startOAuthLogin,
    doManualExchange,
    exportOAuthCredentials,
  };
}
