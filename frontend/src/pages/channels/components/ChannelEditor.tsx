/* eslint-disable @typescript-eslint/no-explicit-any */
import { type KeyboardEvent, useState, useRef, useEffect } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import * as Switch from '@radix-ui/react-switch';
import {
  Plus, Edit, Brain, Trash2, ArrowRight, RefreshCw,
  Server, X, CheckCircle2, Settings2, Copy, ToggleRight, ToggleLeft,
  Folder, Puzzle, Network, CopyCheck, Power, Play,
  Check, Wallet, Link2, GripVertical, ChevronUp, ChevronDown,
  ClipboardPaste, LogIn, Download, LayoutList, LayoutGrid, MoreHorizontal, Package, FileUp
} from 'lucide-react';
import { InterceptorSheet } from '../../../components/InterceptorSheet';
import { ProviderLogo } from '../../../components/ProviderLogos';
import { PipelineView } from './PipelineView';
import {
  formatKeyRuleKeywordsInput,
  formatKeyRuleStatusInput,
  getKeyRuleRetryMode,
  parseKeyRuleKeywordsInput,
  parseKeyRuleStatusInput,
  setKeyRuleRetryMode,
  type KeyRuleRetryMode,
} from '../../../lib/keyRules';
import { summarizeVirtualChain } from '../../../lib/virtualModels';
import { apiFetch } from '../../../lib/api';
import { toastError, fmtErr } from '../../../components/Toast';
import type { ChannelOption } from '../types';
import { SCHEDULE_ALGORITHMS, getBalancePercent, hasUiSlot } from '../utils';
import { DeferredInput, RackCard, RackGrid, UiSlot } from './KeyComponents';
import { FullKeyRow } from './FullKeyRow';

import type { UseChannelEditorResult } from '../hooks/useChannelEditor';

// 修改原因：渠道编辑抽屉是 ChannelsPage 中最大的一块 JSX，需要独立为组件。
// 修改方式：先整体承接原 Dialog/Sheet 内容，并通过 props 注入 useChannelEditor 返回值。
// 目的：保留基础配置、Key、模型、映射、子渠道、路由和高级设置的原始行为。
export interface ChannelEditorProps {
  state: UseChannelEditorResult;
}

export function ChannelEditor({ state }: ChannelEditorProps) {
  const {
    isModalOpen, setIsModalOpen, originalIndex, formData, setFormData, editingSubChannel, setEditingSubChannel, isOAuthOverlayOpen, showPluginSheet,
    setShowPluginSheet, allPlugins, handlePluginSheetUpdate, channelTypes, selectedChannelType, isOAuthEngine, updateFormData,
    updatePreference, updateModelPrefix, groupInput, setGroupInput, modelInput, setModelInput, fetchingModels, copiedModels, headerEntries, setHeaderEntries,
    overridesJson, setOverridesJson, statusCodeOverridesJson, setStatusCodeOverridesJson, modelDisplayKey, setModelDisplayKey, oauthAccounts,
    importModalIdx, importToken, importing, oauthManualState, manualUrl, exchanging, isFetchModelsOpen, fetchedModels,
    selectedModels, modelSearchQuery, testDialogOpen, testingProvider, keyTestDialogOpen, keyTestInitialIndex, keyTestOverride,
    analyticsOpen, analyticsProvider, openModal, addEmptyKey, updateKey, handleOAuthKeyFocus, handleOAuthKeyBlur, openImportModal,
    doImport, startOAuthLogin, doManualExchange, toggleKeyDisabled, deleteKey, handleKeyPaste, copyAllKeys, exportOAuthCredentials,
    clearAllKeys, handleGroupInputKeyDown, removeGroup, handleModelInputKeyDown, openFetchModelsDialog, toggleModelSelect,
    filteredFetchedModels, selectAllVisible, deselectAllVisible, confirmFetchModels, copyAllModels, getAliasMap, getModelDisplayName,
    formatJsonOnBlur, handleMappingChange, handleDeleteProvider, handleToggleProvider, handleCopyProvider, handleToggleSubChannel,
    handleDeleteSubChannel, openSubChannelEdit, buildSubChannelProvider, handleUpdateWeight, openTestDialog, openKeyTestDialog,
    buildProviderSnapshotForTest, getProviderModelNameListForUi, disableKeysInForm, handleSave, refreshOAuthAccounts, getProviderModelNames,
    getProviderAnalyticsName, queryAllBalances, balanceResults, setBalanceResults, balanceLoading, focusedKeyIdx, setFocusedKeyIdx, forceListMode,
    setForceListMode, runtimeKeyStatus, localCountdowns, globalModelPrice, token, importPlaceholder, setImportModalIdx, setImportToken,
    setOauthManualState, setManualUrl, refreshKeyStatus,
  } = state;

  const [keyMoreMenuOpen, setKeyMoreMenuOpen] = useState(false);
  const [batchImportOpen, setBatchImportOpen] = useState(false);
  const [batchPasteOpen, setBatchPasteOpen] = useState(false);
  const [batchJsonText, setBatchJsonText] = useState('');
  const [batchPasteText, setBatchPasteText] = useState('');
  const [batchPasteSep, setBatchPasteSep] = useState('newline');
  const [customSep, setCustomSep] = useState('');
  const [batchImportResult, setBatchImportResult] = useState<any>(null);
  const [batchImportLoading, setBatchImportLoading] = useState(false);
  const [batchImportError, setBatchImportError] = useState('');
  const keyMoreMenuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!keyMoreMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (keyMoreMenuRef.current && !keyMoreMenuRef.current.contains(e.target as Node)) setKeyMoreMenuOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [keyMoreMenuOpen]);

  return (
    <>
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
                      <span>共 <span className="font-mono text-foreground">{formData.api_keys.filter(k => !k.disabled && k.key.trim()).length}</span>/{formData.api_keys.filter(k => k.key.trim()).length} 个可用 Key</span>
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
                        const cfgEnabled = formData.api_keys.filter(k => !k.disabled && k.key.trim()).length;
                        const rtCount = runtimeKeyStatus[formData.provider]?.auto_disabled?.length || 0;
                        const eff = Math.max(0, cfgEnabled - rtCount);
                        const issue = formData.api_keys.some(k => k.disabled) || rtCount > 0;
                        return <span className={`text-xs font-normal font-mono px-1.5 py-0.5 rounded ${issue ? 'bg-orange-500/10 text-orange-600 dark:text-orange-400' : 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-500'}`}>{eff}/{formData.api_keys.filter(k => k.key.trim()).length}</span>;
                      })()}
                    </span>
                    <div className="flex items-center gap-2 text-xs">
                      <button onClick={addEmptyKey} className="text-primary hover:text-primary/80 flex items-center gap-1"><Plus className="w-3 h-3" /> 添加密钥</button>
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
                      <div ref={keyMoreMenuRef} className="relative">
                        <button
                          type="button"
                          onClick={(e) => { e.stopPropagation(); setKeyMoreMenuOpen(prev => !prev); }}
                          className="text-muted-foreground hover:text-foreground rounded p-0.5"
                          title="更多操作"
                        >
                          <MoreHorizontal className="w-3.5 h-3.5" />
                        </button>
                        {keyMoreMenuOpen && (
                          <div className="absolute right-0 top-full z-20 mt-1 min-w-[120px] bg-card border border-border rounded-lg shadow-lg p-1">
                            <button type="button" onClick={() => { copyAllKeys(); setKeyMoreMenuOpen(false); }} className="w-full px-3 py-1.5 hover:bg-muted rounded text-xs flex items-center gap-2 text-left text-foreground"><Copy className="w-3 h-3" /> 复制全部</button>
                            <button type="button" onClick={() => { clearAllKeys(); setKeyMoreMenuOpen(false); }} disabled={formData.api_keys.length === 0} className="w-full px-3 py-1.5 hover:bg-muted rounded text-xs flex items-center gap-2 text-left text-red-600 dark:text-red-500 disabled:opacity-50"><Trash2 className="w-3 h-3" /> 清空</button>
                          </div>
                        )}
                      </div>
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
                                                                <FullKeyRow
                                  keyObj={keyObj}
                                  idx={idx}
                                  formData={formData}
                                  runtimeKeyStatus={runtimeKeyStatus}
                                  localCountdowns={localCountdowns}
                                  balanceResults={balanceResults}
                                  oauthAccounts={oauthAccounts}
                                  isOAuthEngine={isOAuthEngine}
                                  focusedKeyIdx={focusedKeyIdx}
                                  setFocusedKeyIdx={setFocusedKeyIdx}
                                  setFormData={setFormData}
                                  token={token}
                                  refreshKeyStatus={refreshKeyStatus}
                                  updateKey={updateKey}
                                  handleKeyPaste={handleKeyPaste}
                                  handleOAuthKeyFocus={handleOAuthKeyFocus}
                                  handleOAuthKeyBlur={handleOAuthKeyBlur}
                                  openImportModal={openImportModal}
                                  startOAuthLogin={startOAuthLogin}
                                  toggleKeyDisabled={toggleKeyDisabled}
                                  openKeyTestDialog={openKeyTestDialog}
                                  deleteKey={deleteKey}
                                  showDecorationsWhileFocused
                                />
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
                        <div
                          className="relative h-[92px] overflow-hidden rounded-lg border border-dashed border-border/60 bg-card/50 text-foreground transition-all duration-200 hover:border-primary/40 flex flex-col items-center justify-center gap-1.5"
                          style={{ width: 'calc((100% - 5 * 6px) / 6)' }}
                        >
                          <button type="button" onClick={addEmptyKey} className="flex items-center gap-1 rounded px-2 py-1 text-[10px] text-primary hover:bg-muted"><Plus className="w-3 h-3" /> 添加</button>
                          {isOAuthEngine ? (
                            <button type="button" onClick={() => { setBatchImportOpen(true); setBatchPasteOpen(false); }} className="flex items-center gap-1 rounded px-2 py-1 text-[10px] text-muted-foreground hover:bg-muted hover:text-primary"><Package className="w-3 h-3" /> 导入</button>
                          ) : (
                            <button type="button" onClick={() => { setBatchPasteOpen(true); setBatchImportOpen(false); }} className="flex items-center gap-1 rounded px-2 py-1 text-[10px] text-muted-foreground hover:bg-muted hover:text-primary"><ClipboardPaste className="w-3 h-3" /> 粘贴</button>
                          )}
                        </div>
                      </RackGrid>
                    ) : (
                      <>
                        {formData.api_keys.map((keyObj, idx) => (
                          <FullKeyRow
                            key={idx}
                            keyObj={keyObj}
                            idx={idx}
                            formData={formData}
                            runtimeKeyStatus={runtimeKeyStatus}
                            localCountdowns={localCountdowns}
                            balanceResults={balanceResults}
                            oauthAccounts={oauthAccounts}
                            isOAuthEngine={isOAuthEngine}
                            focusedKeyIdx={focusedKeyIdx}
                            setFocusedKeyIdx={setFocusedKeyIdx}
                            setFormData={setFormData}
                            token={token}
                            refreshKeyStatus={refreshKeyStatus}
                            updateKey={updateKey}
                            handleKeyPaste={handleKeyPaste}
                            handleOAuthKeyFocus={handleOAuthKeyFocus}
                            handleOAuthKeyBlur={handleOAuthKeyBlur}
                            openImportModal={openImportModal}
                            startOAuthLogin={startOAuthLogin}
                            toggleKeyDisabled={toggleKeyDisabled}
                            openKeyTestDialog={openKeyTestDialog}
                            deleteKey={deleteKey}
                          />
                        ))}
                      </>
                    )}
                    {formData.api_keys.length > 0 && (formData.api_keys.length < 12 || forceListMode) && (
                      <div className="flex justify-center gap-3 pt-2 text-xs">
                        <button type="button" onClick={addEmptyKey} className="text-primary hover:text-primary/80 flex items-center gap-1"><Plus className="w-3 h-3" /> 添加密钥</button>
                        {isOAuthEngine ? (
                          <button type="button" onClick={() => { setBatchImportOpen(true); setBatchPasteOpen(false); }} className="text-muted-foreground hover:text-primary flex items-center gap-1"><Package className="w-3 h-3" /> 批量导入</button>
                        ) : (
                          <button type="button" onClick={() => { setBatchPasteOpen(true); setBatchImportOpen(false); }} className="text-muted-foreground hover:text-primary flex items-center gap-1"><ClipboardPaste className="w-3 h-3" /> 批量粘贴</button>
                        )}
                      </div>
                    )}
                    {formData.api_keys.length === 0 && (
                      <div className="text-center p-6 space-y-2">
                        <p className="text-sm text-muted-foreground italic">暂无密钥</p>
                        <div className="flex justify-center gap-3 text-xs">
                          <button type="button" onClick={addEmptyKey} className="text-primary hover:text-primary/80 flex items-center gap-1"><Plus className="w-3.5 h-3.5" /> 添加密钥</button>
                          {isOAuthEngine ? (
                            <button type="button" onClick={() => { setBatchImportOpen(true); setBatchPasteOpen(false); }} className="text-muted-foreground hover:text-primary flex items-center gap-1"><Package className="w-3.5 h-3.5" /> 批量导入</button>
                          ) : (
                            <button type="button" onClick={() => { setBatchPasteOpen(true); setBatchImportOpen(false); }} className="text-muted-foreground hover:text-primary flex items-center gap-1"><ClipboardPaste className="w-3.5 h-3.5" /> 批量粘贴</button>
                          )}
                          <button type="button" onClick={() => setFormData(prev => prev ? ({...prev, api_keys: [...prev.api_keys, {key: '*', disabled: false}]}) : prev)} className="text-muted-foreground hover:text-foreground flex items-center gap-1">* BYOK</button>
                        </div>
                      </div>
                    )}
                  </div>

                  {/* OAuth 批量导入面板 */}
                  {batchImportOpen && isOAuthEngine && (
                    <div className="mt-3 space-y-3 rounded-lg border border-border bg-muted/30 p-3">
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium text-foreground">批量导入 OAuth 凭证</span>
                        <button type="button" onClick={() => { setBatchImportOpen(false); setBatchImportResult(null); setBatchImportError(''); setBatchJsonText(''); }} className="text-muted-foreground hover:text-foreground"><X className="w-4 h-4" /></button>
                      </div>
                      <p className="text-xs text-muted-foreground">支持 sub2api 导出 JSON（含 accounts 数组）、CPA 单文件、CPA 多文件数组。也可上传 .json 或 .zip 文件。</p>
                      <div className="flex gap-2">
                        <label className="cursor-pointer text-xs text-primary hover:text-primary/80 flex items-center gap-1 border border-border rounded px-2 py-1">
                          <FileUp className="w-3 h-3" /> 上传文件
                          <input type="file" accept=".json,.zip" className="hidden" onChange={async (e) => {
                            const file = e.target.files?.[0];
                            if (!file) return;
                            if (file.name.endsWith('.zip')) {
                              try {
                                const JSZip = (await import('jszip')).default;
                                const zip = await JSZip.loadAsync(file);
                                const jsons: any[] = [];
                                for (const [name, entry] of Object.entries(zip.files)) {
                                  if (!name.endsWith('.json') || (entry as any).dir) continue;
                                  const text = await (entry as any).async('text');
                                  try { jsons.push(JSON.parse(text)); } catch {}
                                }
                                setBatchJsonText(JSON.stringify(jsons, null, 2));
                              } catch { setBatchImportError('ZIP 解析失败'); }
                            } else {
                              const text = await file.text();
                              setBatchJsonText(text);
                            }
                            e.target.value = '';
                          }} />
                        </label>
                      </div>
                      <textarea
                        value={batchJsonText}
                        onChange={e => { setBatchJsonText(e.target.value); setBatchImportResult(null); setBatchImportError(''); }}
                        placeholder='{"accounts": [...]} 或 [{...}, {...}] 或单个 {"access_token": ...}'
                        className="w-full h-32 bg-background border border-border rounded-lg px-3 py-2 text-xs font-mono outline-none text-foreground resize-y"
                      />
                      {batchJsonText && (() => {
                        try {
                          const d = JSON.parse(batchJsonText);
                          // normalize to preview items
                          type PreviewItem = { email: string; format: string; hasRefresh: boolean; expiresAt: string; expired: boolean; hasToken: boolean };
                          const items: PreviewItem[] = [];
                          const parseOne = (obj: any): PreviewItem => {
                            if (obj?.credentials) {
                              // sub2api
                              const c = obj.credentials;
                              const exp = c.expires_at ? (typeof c.expires_at === 'number' ? new Date(c.expires_at * 1000).toLocaleString() : String(c.expires_at)) : '-';
                              const expTs = typeof c.expires_at === 'number' ? c.expires_at * 1000 : (c.expires_at ? Date.parse(c.expires_at) : NaN);
                              return { email: obj.name || c.email || '未知', format: 'sub2api', hasRefresh: !!c.refresh_token, expiresAt: exp, expired: !isNaN(expTs) && expTs < Date.now(), hasToken: !!c.access_token };
                            }
                            // CPA formats
                            const email = obj?.user?.email || obj?.account?.email_address || obj?.email || '未知';
                            let fmt = 'unknown';
                            if (obj?.user?.email) fmt = 'CPA-codex';
                            else if (obj?.account?.email_address) fmt = 'CPA-claude';
                            else if (obj?.email && obj?.expiry) fmt = 'CPA-gemini';
                            else if (obj?.access_token) fmt = 'CPA';
                            const exp = obj?.expires_at || obj?.expiry || (obj?.expires_in ? `${obj.expires_in}s` : '-');
                            const expStr = typeof exp === 'number' ? new Date(exp * 1000).toLocaleString() : String(exp);
                            const expTs = typeof exp === 'number' ? exp * 1000 : (typeof exp === 'string' && exp !== '-' ? Date.parse(exp) : NaN);
                            return { email, format: fmt, hasRefresh: !!obj?.refresh_token, expiresAt: expStr, expired: !isNaN(expTs) && expTs < Date.now(), hasToken: !!obj?.access_token };
                          };
                          if (d?.accounts) { d.accounts.forEach((a: any) => items.push(parseOne(a))); }
                          else if (Array.isArray(d)) { d.forEach((a: any) => items.push(parseOne(a))); }
                          else if (d?.access_token) { items.push(parseOne(d)); }
                          if (items.length === 0) return <p className="text-xs text-amber-600">⚠ 无法识别格式</p>;
                          const fmtLabel = d?.accounts ? 'sub2api' : Array.isArray(d) ? 'CPA 多文件' : 'CPA 单文件';
                          return (
                            <div className="space-y-2">
                              <p className="text-xs text-emerald-600">✓ {fmtLabel}，共 {items.length} 个账号
                                {(() => {
                                  const expired = items.filter(i => i.expired).length;
                                  const noToken = items.filter(i => !i.hasToken).length;
                                  const noRefresh = items.filter(i => !i.hasRefresh && !i.expired && i.hasToken).length;
                                  const valid = items.length - expired - noToken;
                                  return <span className="ml-2">
                                    {valid > 0 && <span className="text-emerald-600">{valid} 可用</span>}
                                    {expired > 0 && <span className="text-red-500 ml-1">{expired} 已过期</span>}
                                    {noToken > 0 && <span className="text-red-500 ml-1">{noToken} 缺token</span>}
                                    {noRefresh > 0 && <span className="text-amber-600 ml-1">{noRefresh} 无refresh</span>}
                                  </span>;
                                })()}
                              </p>
                              <div className="max-h-40 overflow-y-auto text-xs border border-border rounded">
                                <table className="w-full">
                                  <thead className="bg-muted/50 sticky top-0"><tr>
                                    <th className="px-2 py-1 text-left font-medium">#</th>
                                    <th className="px-2 py-1 text-left font-medium">邮箱/标识</th>
                                    <th className="px-2 py-1 text-left font-medium">格式</th>
                                    <th className="px-2 py-1 text-left font-medium">refresh</th>
                                    <th className="px-2 py-1 text-left font-medium">过期时间</th>
                                    <th className="px-2 py-1 text-left font-medium">状态</th>
                                  </tr></thead>
                                  <tbody>{items.map((it, i) => (
                                    <tr key={i} className="border-t border-border/50">
                                      <td className="px-2 py-1 text-muted-foreground">{i+1}</td>
                                      <td className="px-2 py-1 font-mono truncate max-w-[180px]">{it.email}</td>
                                      <td className="px-2 py-1">{it.format}</td>
                                      <td className="px-2 py-1">{it.hasRefresh ? <span className="text-emerald-600">✓</span> : <span className="text-red-500">✗</span>}</td>
                                      <td className="px-2 py-1 text-muted-foreground">{it.expiresAt}</td>
                                      <td className="px-2 py-1">{!it.hasToken ? <span className="text-red-500">缺token</span> : it.expired ? <span className="text-red-500">已过期</span> : it.hasRefresh ? <span className="text-emerald-600">可刷新</span> : <span className="text-amber-600">无refresh</span>}</td>
                                    </tr>
                                  ))}</tbody>
                                </table>
                              </div>
                            </div>
                          );
                        } catch { return <p className="text-xs text-red-500">JSON 格式错误</p>; }
                      })()}
                      {batchImportError && <p className="text-xs text-red-500">{batchImportError}</p>}
                      <div className="flex gap-2">
                        <button
                          type="button"
                          disabled={!batchJsonText.trim() || batchImportLoading}
                          onClick={async () => {
                            setBatchImportLoading(true); setBatchImportError(''); setBatchImportResult(null);
                            try {
                              const data = JSON.parse(batchJsonText);
                              const res = await apiFetch('/v1/oauth/batch_import', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                                body: JSON.stringify({ provider: formData.provider, type: formData.engine, data }),
                              });
                              const json = await res.json();
                              if (!res.ok) { setBatchImportError(json?.error || '导入失败'); } else { setBatchImportResult(json); refreshOAuthAccounts?.(); }
                            } catch (err: any) { setBatchImportError(err.message || '请求失败'); }
                            setBatchImportLoading(false);
                          }}
                          className="bg-primary text-primary-foreground px-3 py-1.5 rounded text-xs font-medium disabled:opacity-50"
                        >{batchImportLoading ? '导入中...' : '开始导入'}</button>
                      </div>
                      {batchImportResult && (
                        <div className="space-y-2">
                          <p className="text-xs font-medium">结果：✅ {batchImportResult.success} 成功 {batchImportResult.failed > 0 ? `❌ ${batchImportResult.failed} 失败` : ''} {batchImportResult.skipped > 0 ? `⚠️ ${batchImportResult.skipped} 跳过` : ''}</p>
                          <div className="max-h-40 overflow-y-auto text-xs space-y-1">
                            {batchImportResult.results?.map((r: any, i: number) => (
                              <div key={i} className={`flex items-center gap-2 px-2 py-1 rounded ${r.status === 'success' ? 'bg-emerald-500/10' : r.status === 'failed' ? 'bg-red-500/10' : 'bg-amber-500/10'}`}>
                                <span>{r.status === 'success' ? '✅' : r.status === 'failed' ? '❌' : '⚠️'}</span>
                                <span className="font-mono truncate flex-1">{r.key_id}</span>
                                {r.already_exists && <span className="text-amber-600 text-[10px]">已存在</span>}
                                {r.error && <span className="text-red-500 text-[10px] truncate max-w-[200px]">{r.error}</span>}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )}

                  {/* 普通 Key 批量粘贴面板 */}
                  {batchPasteOpen && !isOAuthEngine && (
                    <div className="mt-3 space-y-3 rounded-lg border border-border bg-muted/30 p-3">
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium text-foreground">批量粘贴 API Key</span>
                        <button type="button" onClick={() => { setBatchPasteOpen(false); setBatchPasteText(''); }} className="text-muted-foreground hover:text-foreground"><X className="w-4 h-4" /></button>
                      </div>
                      <textarea
                        value={batchPasteText}
                        onChange={e => setBatchPasteText(e.target.value)}
                        placeholder="每行一个 Key，或选择其他分隔符"
                        className="w-full h-28 bg-background border border-border rounded-lg px-3 py-2 text-xs font-mono outline-none text-foreground resize-y"
                      />
                      <div className="flex items-center gap-3 text-xs flex-wrap">
                        <span className="text-muted-foreground">分隔符：</span>
                        {[['newline','换行'],['comma','逗号'],['semicolon','分号'],['space','空格'],['custom','自定义']].map(([v,l]) => (
                          <label key={v} className="flex items-center gap-1 cursor-pointer">
                            <input type="radio" name="sep" value={v} checked={batchPasteSep===v} onChange={() => setBatchPasteSep(v)} className="w-3 h-3" />{l}
                          </label>
                        ))}
                        {batchPasteSep === 'custom' && (
                          <input
                            type="text"
                            value={customSep}
                            onChange={e => setCustomSep(e.target.value)}
                            placeholder="输入分隔符"
                            className="w-16 bg-background border border-border rounded px-1.5 py-0.5 text-xs font-mono outline-none text-foreground"
                          />
                        )}
                      </div>
                      {batchPasteText && (() => {
                        const sepMap: Record<string,RegExp> = { newline: /\n/, comma: /,/, semicolon: /;/, space: /\s+/ };
                        const sep = batchPasteSep === 'custom' && customSep ? new RegExp(customSep.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g') : (sepMap[batchPasteSep] || /\n/);
                        const rawKeys = batchPasteText.split(sep).map(s => s.trim()).filter(Boolean);
                        const keys = [...new Set(rawKeys)];
                        const existingKeys = new Set(formData.api_keys.map(k => k.key));
                        const newKeys = keys.filter(k => !existingKeys.has(k));
                        const emptyCount = batchPasteText.split(sep).filter(s => !s.trim()).length;
                        const dupCount = rawKeys.length - keys.length;
                        const existCount = keys.length - newKeys.length;
                        return (
                          <div className="space-y-1">
                            <div className="flex items-center justify-between">
                              <div className="text-xs text-muted-foreground space-x-2">
                                <span>解析 <span className="font-mono text-foreground">{rawKeys.length}</span> 条</span>
                                {dupCount > 0 && <span className="text-amber-600">重复 {dupCount}</span>}
                                {emptyCount > 0 && <span className="text-muted-foreground/60">空行 {emptyCount}</span>}
                                {existCount > 0 && <span className="text-amber-600">已存在 {existCount}</span>}
                                <span className="text-emerald-600">有效 <span className="font-mono">{newKeys.length}</span></span>
                              </div>
                            <button
                              type="button"
                              disabled={newKeys.length === 0}
                              onClick={() => {
                                setFormData(prev => prev ? ({
                                  ...prev,
                                  api_keys: [...prev.api_keys, ...newKeys.map(k => ({ key: k, disabled: false }))],
                                }) : prev);
                                setBatchPasteOpen(false); setBatchPasteText('');
                              }}
                              className="bg-primary text-primary-foreground px-3 py-1.5 rounded text-xs font-medium disabled:opacity-50"
                            >添加 {newKeys.length} 个</button>
                            </div>
                          </div>
                        );
                      })()}
                    </div>
                  )}

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
                    {/* 请求处理流水线 — 可视化 Pipeline */}
                    <PipelineView
                      formData={formData}
                      allPlugins={allPlugins}
                      overridesJson={overridesJson}
                      setOverridesJson={setOverridesJson}
                      headerEntries={headerEntries}
                      setHeaderEntries={setHeaderEntries}
                      onOpenPluginSheet={() => setShowPluginSheet(true)}
                      onPluginsChange={(plugins) => {
                        // 修改原因：PipelineView 现在支持 inline 增删插件和编辑参数，需要直接写回当前渠道表单。
                        // 修改方式：仅更新 preferences.enabled_plugins，保留其他 preferences 字段不变。
                        // 目的：让常用插件操作无需打开 InterceptorSheet，也不影响插件表单的完整配置能力。
                        setFormData(prev => {
                          if (!prev) return prev;
                          return { ...prev, preferences: { ...prev.preferences, enabled_plugins: plugins } };
                        });
                      }}
                      formatJsonOnBlur={formatJsonOnBlur}
                    />

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

    </>
  );
}
