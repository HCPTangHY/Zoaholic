/* eslint-disable @typescript-eslint/no-explicit-any */
import { type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import * as Dialog from '@radix-ui/react-dialog';
import * as Switch from '@radix-ui/react-switch';
import {
  Plus, Edit, Brain, Trash2, ArrowRight, RefreshCw,
  Server, X, CheckCircle2, Settings2, Copy, ToggleRight, ToggleLeft,
  Folder, Puzzle, Network, CopyCheck, Power, Play,
  Search, Check, Wallet, Link2, GripVertical, ChevronUp, ChevronDown,
  ClipboardPaste, LogIn, Download, LayoutList, LayoutGrid
} from 'lucide-react';
import { apiFetch } from '../../lib/api';
import { toastError, fmtErr } from '../../components/Toast';
import { InterceptorSheet } from '../../components/InterceptorSheet';
import { ChannelTestDialog } from '../../components/ChannelTestDialog';
import { ApiKeyTestDialog } from '../../components/ApiKeyTestDialog';
import { ChannelAnalyticsSheet } from '../../components/ChannelAnalyticsSheet';
import { ProviderLogo } from '../../components/ProviderLogos';
import {
  formatKeyRuleKeywordsInput,
  formatKeyRuleStatusInput,
  getKeyRuleRetryMode,
  parseKeyRuleKeywordsInput,
  parseKeyRuleStatusInput,
  setKeyRuleRetryMode,
  type KeyRuleRetryMode,
} from '../../lib/keyRules';
import { summarizeVirtualChain } from '../../lib/virtualModels';
import { useChannelsCore } from './hooks/useChannelsCore';
import { useChannelEditor } from './hooks/useChannelEditor';
import { useVirtualModels } from './hooks/useVirtualModels';
import { SCHEDULE_ALGORITHMS, getBalancePercent, hasUiSlot } from './utils';
import type { ChannelOption } from './types';
import { DeferredInput, RackCard, RackGrid, UiSlot } from './components/KeyComponents';
import { ChannelEditor } from './components/ChannelEditor';
import { ProviderList } from './components/ProviderList';
import { VirtualModels } from './components/VirtualModels';

// 修改原因：原 Channels.tsx 文件已拆分到 channels 目录，入口页面需要成为组合层。
// 修改方式：ChannelsPage 调用核心 hook，并把列表交给 ProviderList，其他弹窗按原 JSX 保留。
// 目的：先完成结构迁移，再继续把编辑器和虚拟模型弹窗拆成独立组件。

export default function ChannelsPage() {
  // 修改原因：页面主体已拆到 useChannelsCore，ChannelsPage 只负责组合组件和全局弹窗。
  // 修改方式：一次性解构 hook 返回值，后续 JSX 保持原引用名以降低迁移风险。
  // 目的：精简入口页面，同时不改变原有业务逻辑和事件处理。
  const core = useChannelsCore();
  const channelEditor = useChannelEditor(core);
  const virtualModelsState = useVirtualModels({
    providers: core.providers,
    token: core.token,
    loadedVirtualModels: core.loadedVirtualModels,
    loadedVirtualModelsVersion: core.loadedVirtualModelsVersion,
    filterKeyword: core.filterKeyword,
    filterEngine: core.filterEngine,
    filterGroup: core.filterGroup,
    filterStatus: core.filterStatus,
    availableEngines: core.availableEngines,
    availableGroups: core.availableGroups,
    totalListItemCount: core.totalListItemCount,
    visibleListItemCount: core.visibleListItemCount,
    openTestDialog: channelEditor.openTestDialog,
  });
  // 修改原因：编辑器和虚拟模型状态已从核心 hook 拆出，页面仍保留原变量名以减少 JSX 改动。
  // 修改方式：按职责合并 core、channelEditor 和 virtualModelsState 的返回值，后续向子组件传具体来源。
  // 目的：恢复丢失交互逻辑，同时不再让 useChannelsCore 返回编辑器或虚拟模型字段。
  const state = { ...core, ...channelEditor, ...virtualModelsState };
  const {
    providers, setProviders,
    providerActivity, setProviderActivity,
    channelTypes, setChannelTypes,
    allPlugins, setAllPlugins,
    loading, setLoading,
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
    balanceResults, setBalanceResults,
    balanceLoading, setBalanceLoading,
    focusedKeyIdx, setFocusedKeyIdx,
    forceListMode, setForceListMode,
    oauthAccounts, setOauthAccounts,
    oauthKeyFocusSnapshotRef,
    importModalIdx, setImportModalIdx,
    importToken, setImportToken,
    importing, setImporting,
    oauthManualState, setOauthManualState,
    manualUrl, setManualUrl,
    exchanging, setExchanging,
    isOAuthOverlayOpen,
    selectedChannelType,
    isOAuthEngine,
    rawImportPlaceholderValue,
    rawImportPlaceholder,
    importPlaceholder,
    globalModelPrice, setGlobalModelPrice,
    virtualModels, setVirtualModels,
    virtualDraftName, setVirtualDraftName,
    virtualDraftEnabled, setVirtualDraftEnabled,
    virtualModelsDirty, setVirtualModelsDirty,
    expandedVirtualModels, setExpandedVirtualModels,
    expandedVirtualProviders, setExpandedVirtualProviders,
    virtualAddNodeTypes, setVirtualAddNodeTypes,
    isVirtualModalOpen, setIsVirtualModalOpen,
    editingVirtualName, setEditingVirtualName,
    virtualEditorChain, setVirtualEditorChain,
    isVirtualProviderPanelCollapsed, setIsVirtualProviderPanelCollapsed,
    isVirtualMobileProviderPanelOpen, setIsVirtualMobileProviderPanelOpen,
    isVirtualRoutesAccordionOpen, setIsVirtualRoutesAccordionOpen,
    isFetchModelsOpen, setIsFetchModelsOpen,
    fetchedModels, setFetchedModels,
    selectedModels, setSelectedModels,
    modelSearchQuery, setModelSearchQuery,
    runtimeKeyStatus, setRuntimeKeyStatus,
    localCountdowns, setLocalCountdowns,
    filterKeyword, setFilterKeyword,
    filterEngine, setFilterEngine,
    filterGroup, setFilterGroup,
    filterStatus, setFilterStatus,
    token,
    restoreChannelModalScrollLock,
    applyChannelModalScrollLock,
    applyApiConfigData,
    refreshProviders,
    refreshSingleProvider,
    fetchInitialData,
    refreshKeyStatus,
    refreshOAuthAccounts,
    openModal,
    updateFormData,
    updatePreference,
    updateModelPrefix,
    queryAllBalances,
    addEmptyKey,
    updateKey,
    handleOAuthKeyFocus,
    handleOAuthKeyBlur,
    openImportModal,
    doImport,
    startOAuthLogin,
    doManualExchange,
    toggleKeyDisabled,
    deleteKey,
    handleKeyPaste,
    copyAllKeys,
    exportOAuthCredentials,
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
    getVirtualProviderWeight,
    getProviderModelOptions,
    findProviderModelOption,
    getProviderByName,
    getMatchingProviderCount,
    formatProviderModelOption,
    describeVirtualChannelNode,
    updateVirtualModelsDraft,
    serializeVirtualModels,
    saveVirtualModels,
    handleSaveVirtualModelsDraft,
    handleAddVirtualModel,
    updateVirtualModelConfig,
    updateVirtualNode,
    moveVirtualNode,
    insertVirtualNode,
    appendVirtualNodeByType,
    toggleVirtualModelExpanded,
    toggleVirtualProviderExpanded,
    getPreferredVirtualTarget,
    handlePanelModelQuickAdd,
    handlePanelChannelQuickAdd,
    handleDeleteVirtualModel,
    setVirtualDragPayload,
    readVirtualDragPayload,
    handlePanelModelDragStart,
    handlePanelChannelDragStart,
    handleChainNodeDragStart,
    handleVirtualDrop,
    openVirtualModelModal,
    updateVirtualEditorChainDraft,
    updateVirtualEditorNode,
    insertVirtualEditorNode,
    moveVirtualEditorNode,
    swapVirtualEditorNode,
    appendVirtualEditorNodeByType,
    handleVirtualEditorDrop,
    handleSaveVirtualEditor,
    handleToggleVirtualModelCard,
    openTestDialog,
    openKeyTestDialog,
    buildProviderSnapshotForTest,
    getProviderModelNameListForUi,
    disableKeysInForm,
    handleSave,
    getProviderModelNames,
    virtualProviderEntries,
    virtualRoutingProviderItems,
    virtualProviderPanelItems,
    providerNames,
    providerListItems,
    availableEngines,
    availableGroups,
    getProviderAnalyticsName,
    filteredVirtualProviderEntries,
    filteredProviders,
    getMatchedModels,
    hasActiveFilters,
    totalListItemCount,
    visibleListItemCount,
    isProviderInactive,
    expandedInactiveGroups, setExpandedInactiveGroups,
    toggleInactiveGroup,
    segments,
    renderVirtualProviderPanelCollapsedRail,
    renderVirtualProviderPanelList,
    getFullVirtualChainSummary,
    openVirtualRouteTestDialog,
    renderDesktopVirtualRoutesAccordionRows,
    renderMobileVirtualRoutesAccordion
  } = state;

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

      <ProviderList
        loading={core.loading}
        totalListItemCount={virtualModelsState.totalListItemCount}
        visibleListItemCount={virtualModelsState.visibleListItemCount}
        filterKeyword={core.filterKeyword}
        setFilterKeyword={core.setFilterKeyword}
        filterEngine={core.filterEngine}
        setFilterEngine={core.setFilterEngine}
        filterGroup={core.filterGroup}
        setFilterGroup={core.setFilterGroup}
        filterStatus={core.filterStatus}
        setFilterStatus={core.setFilterStatus}
        availableEngines={virtualModelsState.availableEngines}
        availableGroups={virtualModelsState.availableGroups}
        hasActiveFilters={core.hasActiveFilters}
        segments={core.segments}
        expandedInactiveGroups={core.expandedInactiveGroups}
        toggleInactiveGroup={core.toggleInactiveGroup}
        runtimeKeyStatus={core.runtimeKeyStatus}
        getMatchedModels={core.getMatchedModels}
        getProviderAnalyticsName={core.getProviderAnalyticsName}
        setAnalyticsProvider={channelEditor.setAnalyticsProvider}
        setAnalyticsOpen={channelEditor.setAnalyticsOpen}
        openTestDialog={channelEditor.openTestDialog}
        handleToggleProvider={channelEditor.handleToggleProvider}
        handleCopyProvider={channelEditor.handleCopyProvider}
        openModal={channelEditor.openModal}
        handleDeleteProvider={channelEditor.handleDeleteProvider}
        handleUpdateWeight={channelEditor.handleUpdateWeight}
        buildSubChannelProvider={channelEditor.buildSubChannelProvider}
        handleToggleSubChannel={channelEditor.handleToggleSubChannel}
        openSubChannelEdit={channelEditor.openSubChannelEdit}
        handleDeleteSubChannel={channelEditor.handleDeleteSubChannel}
        renderMobileVirtualRoutesAccordion={virtualModelsState.renderMobileVirtualRoutesAccordion}
        renderDesktopVirtualRoutesAccordionRows={virtualModelsState.renderDesktopVirtualRoutesAccordionRows}
      />

      <VirtualModels state={virtualModelsState} />

      <ChannelEditor state={channelEditor} />

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
              <p>2. 登录后页面会显示 Authentication Code</p>
              <p>3. 复制代码粘贴到下方（也支持粘贴完整回调 URL）</p>
            </div>
            <input
              type="text"
              value={manualUrl}
              onChange={e => setManualUrl(e.target.value)}
              placeholder="粘贴 Authentication Code 或完整回调 URL"
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