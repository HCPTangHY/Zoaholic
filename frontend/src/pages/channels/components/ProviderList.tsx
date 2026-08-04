import { BarChart3, CheckCircle2, Edit, Files, Folder, Play, Power, Puzzle, Search, Trash2, X, XCircle } from 'lucide-react';
import type React from 'react';
import { useEffect, useState } from 'react';

import { ProviderLogo } from '../../../components/ProviderLogos';
import type { ProviderSortMode, Segment } from '../types';

// 修改原因：原权重输入框每次 onChange 都会调用 handleUpdateWeight，触发保存+按权重重排，
//           用户在输入中途（例如 10 前加 1 变成 110 的过程）就会看到列表跳序。
// 修改方式：抽出受控的本地输入组件，只有在失焦（blur）或按下 Enter 时才把最终值提交给上层。
// 目的：让权重编辑“确认后再排序”，输入过程中列表保持稳定。
function WeightInput({
  value,
  onCommit,
  className,
  onClick,
}: {
  value: number;
  onCommit: (newWeight: number) => void;
  className?: string;
  onClick?: (e: React.MouseEvent) => void;
}) {
  const [draft, setDraft] = useState<string>(String(value));

  // 外部权重变化（例如保存成功后刷新）时，同步回本地草稿，避免显示陈旧值。
  useEffect(() => {
    setDraft(String(value));
  }, [value]);

  const commit = () => {
    const parsed = parseInt(draft, 10);
    const next = Number.isNaN(parsed) ? 0 : parsed;
    if (next !== value) {
      onCommit(next);
    } else {
      // 值没变（或被清空后仍等于原值）时，纠正草稿显示。
      setDraft(String(value));
    }
  };

  return (
    <input
      type="number"
      value={draft}
      onChange={e => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={e => {
        if (e.key === 'Enter') {
          e.preventDefault();
          (e.target as HTMLInputElement).blur();
        } else if (e.key === 'Escape') {
          setDraft(String(value));
          (e.target as HTMLInputElement).blur();
        }
      }}
      onClick={onClick}
      className={className}
    />
  );
}

// 修改原因：渠道列表渲染占据 ChannelsPage 大部分 JSX，需要独立成组件。
// 修改方式：把原筛选栏、移动端卡片、桌面表格和不活跃分组整体迁移，并用 props 注入 handlers。
// 目的：保持列表交互不变，同时让页面骨架更清晰。

export interface ProviderListProps {
  loading: boolean;
  totalListItemCount: number;
  visibleListItemCount: number;
  filterKeyword: string;
  setFilterKeyword: (value: string) => void;
  filterEngine: string;
  setFilterEngine: (value: string) => void;
  filterGroup: string;
  setFilterGroup: (value: string) => void;
  filterStatus: '' | 'enabled' | 'disabled';
  setFilterStatus: (value: '' | 'enabled' | 'disabled') => void;
  sortMode: ProviderSortMode;
  setSortMode: (value: ProviderSortMode) => void;
  availableEngines: string[];
  availableGroups: string[];
  hasActiveFilters: string | boolean;
  segments: Segment[];
  expandedInactiveGroups: Set<number>;
  toggleInactiveGroup: (groupKey: number) => void;
  runtimeKeyStatus: Record<string, { auto_disabled: { key: string; remaining_seconds: number; duration: number; reason: string }[]; cooling: any[] }>;
  getMatchedModels: (provider: any) => string[];
  getProviderAnalyticsName: (provider: any) => string;
  setAnalyticsProvider: (providerName: string) => void;
  setAnalyticsOpen: (open: boolean) => void;
  openTestDialog: (provider: any) => void;
  handleToggleProvider: (idx: number) => void;
  handleCopyProvider: (provider: any) => void;
  openModal: (provider?: any, index?: number | null) => void;
  handleDeleteProvider: (idx: number) => void;
  handleUpdateWeight: (idx: number, newWeight: number) => void;
  buildSubChannelProvider: (parentIdx: number, subIdx: number) => any | null;
  handleToggleSubChannel: (parentIdx: number, subIdx: number) => void;
  openSubChannelEdit: (parentIdx: number, subIdx: number) => void;
  handleDeleteSubChannel: (parentIdx: number, subIdx: number) => void;
  renderMobileVirtualRoutesAccordion: () => React.ReactNode;
  renderDesktopVirtualRoutesAccordionRows: () => React.ReactNode;
}

export function ProviderList({
  loading, totalListItemCount, visibleListItemCount, filterKeyword, setFilterKeyword, filterEngine, setFilterEngine,
  filterGroup, setFilterGroup, filterStatus, setFilterStatus, sortMode, setSortMode, availableEngines, availableGroups, hasActiveFilters,
  segments, expandedInactiveGroups, toggleInactiveGroup, runtimeKeyStatus, getMatchedModels, getProviderAnalyticsName,
  setAnalyticsProvider, setAnalyticsOpen, openTestDialog, handleToggleProvider, handleCopyProvider, openModal, handleDeleteProvider,
  handleUpdateWeight, buildSubChannelProvider, handleToggleSubChannel, openSubChannelEdit, handleDeleteSubChannel,
  renderMobileVirtualRoutesAccordion, renderDesktopVirtualRoutesAccordionRows,
}: ProviderListProps) {
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
            <WeightInput
              value={weight}
              onCommit={newWeight => handleUpdateWeight(idx, newWeight)}
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


  return (
    <>
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

          {/* 排序方式：支持权重降序 / 启用优先 / 名称 */}
          <select
            value={sortMode}
            onChange={e => setSortMode(e.target.value as ProviderSortMode)}
            className="bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground min-w-[120px]"
            title="列表排序方式"
          >
            <option value="weight">权重降序</option>
            <option value="enabled">启用优先</option>
            <option value="name">名称</option>
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
                      <WeightInput
                        value={weight}
                        onCommit={newWeight => handleUpdateWeight(idx, newWeight)}
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
    </>
  );
}
