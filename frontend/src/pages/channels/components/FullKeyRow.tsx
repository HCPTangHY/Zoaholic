/* eslint-disable @typescript-eslint/no-explicit-any */
import type { ClipboardEvent, Dispatch, SetStateAction } from 'react';
import { ClipboardPaste, LogIn, Play, ToggleLeft, ToggleRight, Trash2 } from 'lucide-react';

import { apiFetch } from '../../../lib/api';
import type { ApiKeyObj, BalanceResult, ProviderFormData } from '../types';
import {
  BALANCE_FILL_COLORS,
  TAG_CLASSES,
  buildRowQuota,
  buildRowQuotaSlotData,
  getBalanceColor,
  getBalanceLabel,
  getBalancePercent,
  getQuotaPairFromGauges,
  hasUiSlot,
} from '../utils';
import { QuotaBorderOverlay } from './QuotaComponents';
import { CoolingKeyRow, KeyLabelOverlay, UiSlot } from './KeyComponents';

export interface FullKeyRowProps {
  keyObj: ApiKeyObj;
  idx: number;
  formData: ProviderFormData;
  runtimeKeyStatus: Record<string, { auto_disabled: { key: string; remaining_seconds: number; duration: number; reason: string }[]; cooling: any[] }>;
  localCountdowns: Record<string, Record<string, { remaining: number; duration: number }>>;
  balanceResults: Record<string, BalanceResult>;
  oauthAccounts: Record<string, any>;
  isOAuthEngine: boolean;
  focusedKeyIdx: number | null;
  setFocusedKeyIdx: Dispatch<SetStateAction<number | null>>;
  setFormData: Dispatch<SetStateAction<ProviderFormData | null>>;
  token: string | null;
  refreshKeyStatus: () => Promise<void>;
  updateKey: (idx: number, keyStr: string) => void;
  handleKeyPaste: (event: ClipboardEvent<HTMLInputElement>, idx: number) => void;
  handleOAuthKeyFocus: (idx: number, keyStr: string) => void;
  handleOAuthKeyBlur: (idx: number, newValue: string) => Promise<void>;
  openImportModal: (idx: number) => void;
  startOAuthLogin: (idx: number) => Promise<void>;
  toggleKeyDisabled: (idx: number) => void;
  openKeyTestDialog: (initialIndex?: number | null) => void;
  deleteKey: (idx: number) => Promise<void>;
  showDecorationsWhileFocused?: boolean;
}

export function FullKeyRow({
  keyObj,
  idx,
  formData,
  runtimeKeyStatus,
  localCountdowns,
  balanceResults,
  oauthAccounts,
  isOAuthEngine,
  focusedKeyIdx,
  setFocusedKeyIdx,
  setFormData,
  token,
  refreshKeyStatus,
  updateKey,
  handleKeyPaste,
  handleOAuthKeyFocus,
  handleOAuthKeyBlur,
  openImportModal,
  startOAuthLogin,
  toggleKeyDisabled,
  openKeyTestDialog,
  deleteKey,
  showDecorationsWhileFocused = false,
}: FullKeyRowProps) {
  // 修改原因：hook 不应该返回 JSX，原 useChannelEditor 中的完整 Key 行渲染让业务 hook 同时承担渲染职责。
  // 修改方式：把完整 Key 行 JSX 挪到组件，并通过明确 props 注入状态和 handler，不传入整个 editor 对象。
  // 目的：保留 Key 编辑、禁用、测试、删除、OAuth 导入登录、额度和渠道自定义插槽展示能力，同时缩小 hook 职责。
  const providerName = formData.provider;
  const rtDisabled = runtimeKeyStatus[providerName]?.auto_disabled || [];
  const rtEntry = !keyObj.disabled ? rtDisabled.find((d: any) => d.key === keyObj.key) : undefined;
  const isRtDisabled = !!rtEntry;
  const isPermanent = isRtDisabled && rtEntry.remaining_seconds < 0;
  const isCooling = isRtDisabled && !isPermanent && rtEntry.remaining_seconds > 0;
  const countdown = localCountdowns[providerName]?.[keyObj.key];
  const remainSec = countdown?.remaining ?? (rtEntry?.remaining_seconds || 0);
  const isGrayed = keyObj.disabled || isPermanent;
  const isFocused = focusedKeyIdx === idx;
  const showRowDecorations = !isFocused || showDecorationsWhileFocused;
  const bal = balanceResults[keyObj.key];
  const oauthAccount = oauthAccounts[keyObj.key];
  const rowQuota = buildRowQuota(bal, oauthAccount, isOAuthEngine);
  const rowQuotaPair = getQuotaPairFromGauges(rowQuota.gauges);
  const rowQuotaHasValues = rowQuota.gauges.length > 0;
  const slotData = buildRowQuotaSlotData(bal, oauthAccount, rowQuota);
  const slotPayloadAvailable = Boolean(slotData || oauthAccount || rowQuotaHasValues);
  const slotContext = { account: oauthAccount, keyObj, balance: bal };
  const enabledPlugins = formData.preferences.enabled_plugins || [];
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
        onRecover={async () => { await apiFetch('/v1/channels/key_status/re_enable', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ provider: providerName, key: keyObj.key }) }); void refreshKeyStatus(); }}
        onToggle={() => toggleKeyDisabled(idx)}
        onTest={() => openKeyTestDialog(idx)}
        onDelete={() => void deleteKey(idx)}
        onLabelChange={(label) => {
          const newKeys = [...formData.api_keys];
          newKeys[idx] = { ...newKeys[idx], label: label || undefined };
          setFormData(prev => prev ? { ...prev, api_keys: newKeys } : prev);
        }}
        alwaysShowRecover={showDecorationsWhileFocused}
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
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocusedKeyIdx(null);
      }}
      className={`relative flex items-center gap-2 px-3 py-2 rounded-lg border transition-colors ${isFocused ? 'border-blue-500' : 'border-border'} ${isGrayed ? (isFocused ? 'bg-muted/30' : 'bg-muted/30 opacity-50') : 'bg-muted/50'}`}
    >
      {showRowDecorations && rowQuotaPair && (
        hasKeyBorderSlot
          ? <UiSlot engine={formData.engine} slot="key_border" data={slotData} context={{ ...slotContext, mode: 'row' }} element="div" className="absolute inset-0 pointer-events-none z-[1]" enabledPlugins={enabledPlugins} />
          : <QuotaBorderOverlay quotaInner={rowQuotaPair.quota_inner} quotaOuter={rowQuotaPair.quota_outer} />
      )}
      {showRowDecorations && slotPayloadAvailable && hasKeyBackgroundSlot && (
        <UiSlot engine={formData.engine} slot="key_background" data={slotData} context={{ ...slotContext, mode: 'row' }} element="div" className="absolute inset-0 pointer-events-none rounded-[7px] z-0 transition-all duration-500" enabledPlugins={enabledPlugins} />
      )}
      {!hasKeyBackgroundSlot && !isFocused && balColor && balPct != null && (
        <div className="absolute left-0 top-0 bottom-0 rounded-[7px] z-0 pointer-events-none transition-all duration-500" style={{ width: `${Math.max(1, balPct)}%`, background: BALANCE_FILL_COLORS[balColor] }} />
      )}
      <span className="text-xs text-muted-foreground w-4 text-right relative z-[2]">{idx + 1}</span>
      <KeyLabelOverlay label={keyObj.label} hasTag={hasTag} isFocused={isFocused}>
        <input
          type="text"
          value={keyObj.key}
          onChange={e => updateKey(idx, e.target.value)}
          onPaste={e => handleKeyPaste(e, idx)}
          onFocus={() => isOAuthEngine ? handleOAuthKeyFocus(idx, keyObj.key) : setFocusedKeyIdx(idx)}
          onBlur={e => { if (isOAuthEngine && !e.currentTarget.closest('[tabindex]')?.contains(e.relatedTarget as Node)) void handleOAuthKeyBlur(idx, e.currentTarget.value); }}
          placeholder={isOAuthEngine ? '邮箱或标识符' : 'sk-...'}
          className={`w-full bg-transparent border-none text-sm leading-5 font-mono outline-none min-w-0 ${isGrayed ? 'text-muted-foreground line-through' : 'text-foreground'}`}
        />
      </KeyLabelOverlay>
      {isOAuthEngine && !keyObj.key && (
        <>
          <button onClick={() => openImportModal(idx)} className="text-xs px-2 py-1 rounded border border-border bg-muted hover:bg-muted/80 text-foreground flex items-center gap-1 relative z-[2]" title="粘贴 Refresh Token"><ClipboardPaste className="w-3 h-3" /> 导入</button>
          <button onClick={() => void startOAuthLogin(idx)} className="text-xs px-2 py-1 rounded border border-primary/50 bg-primary/10 hover:bg-primary/20 text-primary flex items-center gap-1 relative z-[2]" title="浏览器登录"><LogIn className="w-3 h-3" /> 登录</button>
        </>
      )}
      {showRowDecorations && hasQuotaDisplaySlot && slotPayloadAvailable && (
        <UiSlot engine={formData.engine} slot="quota_display" data={slotData} context={{ ...slotContext, mode: 'row' }} className="flex-shrink-0 relative z-[2]" enabledPlugins={enabledPlugins} />
      )}
      {showRowDecorations && !isFocused && bal && !hasQuotaDisplaySlot && (
        <span className={`flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] ${TAG_CLASSES[getBalanceColor(getBalancePercent(bal)) || 'green']}`}>{getBalanceLabel(bal)}</span>
      )}
      {isOAuthEngine && !isFocused && oauthAccount && !rowQuotaHasValues && !hasQuotaDisplaySlot && (
        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-500 relative z-[2]">{oauthAccount.status === 'active' ? '已连接' : oauthAccount.status === 'error' ? '刷新失败' : '冷却中'}</span>
      )}
      {!isFocused && isPermanent && <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/15 text-red-500 dark:text-red-400 font-medium flex-shrink-0 relative z-[2]">永久禁用</span>}
      {!isFocused && isPermanent && (
        <button onClick={async () => { await apiFetch('/v1/channels/key_status/re_enable', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ provider: providerName, key: keyObj.key }) }); void refreshKeyStatus(); }} className="text-[11px] px-2 py-0.5 rounded border border-emerald-500/50 bg-emerald-500/20 text-emerald-400 font-medium hover:bg-emerald-500/30 hover:border-emerald-400 cursor-pointer flex-shrink-0 relative z-[2] transition-colors">恢复</button>
      )}
      <button onClick={() => toggleKeyDisabled(idx)} className={`relative z-[2] ${isGrayed ? 'text-muted-foreground' : 'text-emerald-500'}`} title={keyObj.disabled ? '启用' : '禁用'}>{keyObj.disabled ? <ToggleLeft className="w-5 h-5" /> : <ToggleRight className="w-5 h-5" />}</button>
      <button onClick={() => openKeyTestDialog(idx)} disabled={!keyObj.key.trim()} className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 disabled:opacity-50 disabled:cursor-not-allowed relative z-[2]" title="测试此 Key"><Play className="w-4 h-4" /></button>
      <button onClick={() => void deleteKey(idx)} className="text-red-500 hover:text-red-400 ml-1 relative z-[2]"><Trash2 className="w-4 h-4" /></button>
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
}
