import { useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../lib/api';
import { toastSuccess, toastError, toastWarning, fmtErr } from '../components/Toast';
import { PluginParamsForm, type ParamSchema } from './PluginParamsForm';
import { 
  Puzzle, 
  Settings2, 
  ChevronDown, 
  ChevronRight, 
  Check, 
  X,
  ArrowLeft,
} from 'lucide-react';

interface PluginOption {
  plugin_name: string;
  version: string;
  description: string;
  enabled: boolean;
  request_interceptors: unknown[];
  response_interceptors: unknown[];
  metadata?: {
    params_hint?: string;
    params_schema?: ParamSchema[];
    provider_config?: {
      key: string;
      type?: 'json' | 'text';
      title?: string;
      description?: string;
      example?: unknown;
    };
  };
}

interface InterceptorSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  allPlugins: PluginOption[];
  enabledPlugins: string[]; // ["pluginA:config", "pluginB"]
  providerPreferences: Record<string, unknown>;
  onUpdate: (payload: { enabled_plugins: string[]; preferences_patch: Record<string, unknown>; preferences_delete: string[] }) => void;
}

type InterceptorTab = 'all' | 'request' | 'response';

// 修改原因：插件数量增多后，需要按请求或响应拦截方向快速缩小列表。
// 修改方式：用固定配置驱动小型 pill Tab，避免改变 InterceptorSheetProps。
// 目的：让筛选能力只停留在组件内部，不影响保存数据结构。
const INTERCEPTOR_TABS: Array<{ value: InterceptorTab; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'request', label: '请求拦截' },
  { value: 'response', label: '响应拦截' },
];

export function InterceptorSheet({ open, onOpenChange, allPlugins, enabledPlugins, providerPreferences, onUpdate }: InterceptorSheetProps) {
  // 自行获取插件列表，防止父组件传入的 allPlugins 因 403 等原因为空
  const [localPlugins, setLocalPlugins] = useState<PluginOption[]>(allPlugins);

  // 优先使用自行获取的 localPlugins，回退到父组件传入的 allPlugins
  const effectivePlugins = localPlugins.length > 0 ? localPlugins : allPlugins;

  // Parsing helpers
  const parseEntry = (entry: string) => {
    const colonIdx = entry.indexOf(':');
    if (colonIdx === -1) return { name: entry.trim(), options: '' };
    return { 
      name: entry.substring(0, colonIdx).trim(), 
      options: entry.substring(colonIdx + 1).trim() 
    };
  };

  // State
  const [selected, setSelected] = useState<Map<string, string>>(new Map());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [providerConfigText, setProviderConfigText] = useState<Map<string, string>>(new Map());
  const [activeTab, setActiveTab] = useState<InterceptorTab>('all');
  const [searchQuery, setSearchQuery] = useState('');

  const normalizedSearch = searchQuery.trim().toLowerCase();

  // 修改原因：原列表只能展示全部插件，插件变多后定位请求或响应插件成本较高。
  // 修改方式：先按 Tab 过滤，再按插件名和描述做实时模糊搜索。
  // 目的：让后续排序和渲染都基于用户当前看到的插件集合。
  const filteredPlugins = useMemo(() => {
    return effectivePlugins.filter(plugin => {
      const hasRequest = plugin.request_interceptors.length > 0;
      const hasResponse = plugin.response_interceptors.length > 0;

      if (activeTab === 'request' && !hasRequest) return false;
      if (activeTab === 'response' && !hasResponse) return false;
      if (!normalizedSearch) return true;

      return (
        plugin.plugin_name.toLowerCase().includes(normalizedSearch) ||
        plugin.description.toLowerCase().includes(normalizedSearch)
      );
    });
  }, [activeTab, effectivePlugins, normalizedSearch]);

  // 修改原因：启用中的插件通常更需要查看和微调，混在长列表里不易找到。
  // 修改方式：在 Tab 和搜索过滤之后，把已启用和未启用插件拆成两组。
  // 目的：既保持当前筛选结果，又让已启用插件稳定置顶。
  const groupedPlugins = useMemo(() => {
    const selectedPlugins: PluginOption[] = [];
    const unselectedPlugins: PluginOption[] = [];

    filteredPlugins.forEach(plugin => {
      if (selected.has(plugin.plugin_name)) selectedPlugins.push(plugin);
      else unselectedPlugins.push(plugin);
    });

    return {
      selectedPlugins,
      unselectedPlugins,
      total: selectedPlugins.length + unselectedPlugins.length,
    };
  }, [filteredPlugins, selected]);

  // Re-init when opening
  useEffect(() => {
    if (!open) return;
    // 修改原因：重新打开面板时应回到默认“全部”视图，避免沿用上一次的搜索或 Tab。
    // 修改方式：只在 open 变为 true 的初始化入口重置本地筛选状态。
    // 目的：保证默认入口符合“全部”Tab 的预期，同时不影响保存逻辑。
    setActiveTab('all');
    setSearchQuery('');
    const refreshPlugins = async () => {
      try {
        const res = await apiFetch('/v1/plugins/interceptors');
        if (res.ok) {
          const data = await res.json();
          const plugins = data.interceptor_plugins || [];
          if (plugins.length > 0) setLocalPlugins(plugins);
        }
      } catch { /* ignore */ }
    };
    refreshPlugins();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const m = new Map<string, string>();
    enabledPlugins.forEach(entry => {
      const { name, options } = parseEntry(entry);
      if (name) m.set(name, options);
    });
    // eslint-disable-next-line
    setSelected(m); setExpanded(new Set());

    const cfgMap = new Map<string, string>();
    effectivePlugins.forEach(p => {
      const meta = p.metadata?.provider_config;
      if (!meta?.key) return;

      const raw = (providerPreferences || {})[meta.key];
      if (raw === undefined || raw === null) {
        cfgMap.set(p.plugin_name, '');
      } else {
        try {
          cfgMap.set(p.plugin_name, JSON.stringify(raw, null, 2));
        } catch {
          cfgMap.set(p.plugin_name, String(raw));
        }
      }
    });
    setProviderConfigText(cfgMap);
  }, [open, enabledPlugins, effectivePlugins, providerPreferences]);

  // Handlers
  const toggleSelect = (pluginName: string) => {
    setSelected(prev => {
      const next = new Map(prev);
      if (next.has(pluginName)) next.delete(pluginName);
      else next.set(pluginName, '');
      return next;
    });
  };

  const updateOptions = (pluginName: string, options: string) => {
    setSelected(prev => {
      const next = new Map(prev);
      if (next.has(pluginName)) next.set(pluginName, options);
      return next;
    });
  };

  const toggleExpand = (pluginName: string) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(pluginName)) next.delete(pluginName);
      else next.add(pluginName);
      return next;
    });
  };

  const handlePluginRowClick = (pluginName: string) => {
    if (!selected.has(pluginName)) {
      // 修改原因：旧交互要求先点 checkbox 再展开，新用户在长列表中容易多一次操作。
      // 修改方式：点击未启用插件行时同步写入 selected，并把该行加入 expanded。
      // 目的：让“点击整行”成为启用并配置插件的主要入口。
      setSelected(prev => {
        const next = new Map(prev);
        next.set(pluginName, '');
        return next;
      });
      setExpanded(prev => {
        const next = new Set(prev);
        next.add(pluginName);
        return next;
      });
      return;
    }

    toggleExpand(pluginName);
  };

  const selectAll = () => {
    const next = new Map(selected);
    // 修改原因：加入 Tab 和搜索后，“全选”应作用于当前可见列表，而不是隐藏在筛选外的插件。
    // 修改方式：使用 filteredPlugins 作为批量选择来源，并保留已有参数值。
    // 目的：让批量操作与当前视图保持一致。
    filteredPlugins.forEach(p => {
      if (!next.has(p.plugin_name)) next.set(p.plugin_name, '');
    });
    setSelected(next);
  };

  const clearAll = () => {
    setSelected(new Map());
  };

  const updateProviderConfigText = (pluginName: string, text: string) => {
    setProviderConfigText(prev => {
      const next = new Map(prev);
      next.set(pluginName, text);
      return next;
    });
  };

  const formatJsonText = (text: string): string => {
    if (!text.trim()) return '';
    const obj = JSON.parse(text);
    return JSON.stringify(obj, null, 2);
  };

  const handleSave = () => {
    const result: string[] = [];
    selected.forEach((options, name) => {
      result.push(options ? `${name}:${options}` : name);
    });

    const preferences_patch: Record<string, unknown> = {};
    const preferences_delete: string[] = [];

    for (const plugin of effectivePlugins) {
      const meta = plugin.metadata?.provider_config;
      if (!meta?.key) continue;

      const text = providerConfigText.get(plugin.plugin_name) || '';
      const t = text.trim();

      if (!t) {
        preferences_delete.push(meta.key);
        continue;
      }

      const configType = meta.type || 'json';
      if (configType === 'json') {
        try {
          preferences_patch[meta.key] = JSON.parse(t);
        } catch (e) {
          toastWarning(`插件 ${plugin.plugin_name} 配置 JSON 格式错误：${e instanceof Error ? e.message : 'invalid json'}`);
          return;
        }
      } else {
        preferences_patch[meta.key] = t;
      }
    }

    onUpdate({ enabled_plugins: result, preferences_patch, preferences_delete });
    onOpenChange(false);
  };

  const renderPluginCard = (plugin: PluginOption) => {
    const isSelected = selected.has(plugin.plugin_name);
    const isExpanded = expanded.has(plugin.plugin_name);
    const options = selected.get(plugin.plugin_name) || '';
    const hasRequest = plugin.request_interceptors.length > 0;
    const hasResponse = plugin.response_interceptors.length > 0;
    const hasProviderConfig = Boolean(plugin.metadata?.provider_config?.key);
    const paramsSchema = Array.isArray(plugin.metadata?.params_schema) ? plugin.metadata.params_schema : [];

    return (
      <div key={plugin.plugin_name} className={`border rounded-lg transition-colors ${isSelected ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-border bg-card'}`}>
        {/* Card Header */}
        <div className="flex items-start justify-between p-3 cursor-pointer select-none" onClick={() => handlePluginRowClick(plugin.plugin_name)}>
          <div className="flex items-start gap-3">
            <button 
              type="button"
              onClick={(e) => { e.stopPropagation(); toggleSelect(plugin.plugin_name); }}
              className={`w-5 h-5 rounded border flex items-center justify-center transition-colors flex-shrink-0 mt-0.5 ${isSelected ? 'bg-emerald-500 border-emerald-500 text-white' : 'bg-background border-border'}`}
            >
              {isSelected && <Check className="w-3.5 h-3.5" />}
            </button>
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className={`text-sm font-medium ${isSelected ? 'text-foreground' : 'text-muted-foreground'}`}>{plugin.plugin_name}</span>
                <span className="text-xs bg-muted text-muted-foreground px-1.5 py-0.5 rounded font-mono">v{plugin.version}</span>
                {/* 修改原因：折叠状态需要直接看到已保存的冒号后参数，避免必须展开才能确认配置。
                    修改方式：保留原有 options pill，并把它放在卡片标题行里持续显示。
                    目的：让长列表中的参数状态一眼可见。 */}
                {options && <span className="text-xs bg-blue-500/10 text-blue-400 px-1.5 py-0.5 rounded font-mono max-w-[180px] truncate">{options}</span>}
                {/* 修改原因：用户需要在折叠状态判断插件是否还需要渠道级配置。
                    修改方式：当 metadata.provider_config.key 存在时显示带图标的小标签。
                    目的：减少展开检查每个插件配置项的次数。 */}
                {hasProviderConfig && (
                  <span title="有渠道配置" className="text-xs bg-amber-500/10 text-amber-500 px-1.5 py-0.5 rounded flex items-center gap-1">
                    <Settings2 className="w-3 h-3" />
                    有渠道配置
                  </span>
                )}
                {/* 修改原因：只含单一方向拦截器的插件需要在列表中快速识别类型。
                    修改方式：仅请求插件显示蓝色“请求”，仅响应插件显示紫色“响应”，双向插件不额外标记。
                    目的：避免全功能插件产生冗余标签，同时突出单向插件。 */}
                {hasRequest && !hasResponse && <span className="text-xs bg-blue-500/10 text-blue-400 px-1.5 py-0.5 rounded">请求</span>}
                {hasResponse && !hasRequest && <span className="text-xs bg-purple-500/10 text-purple-400 px-1.5 py-0.5 rounded">响应</span>}
              </div>
              <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{plugin.description}</p>
            </div>
          </div>
          <div className="text-muted-foreground flex-shrink-0 ml-2 mt-0.5">{isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}</div>
        </div>

        {/* Details */}
        {isExpanded && (
          <div className="px-3 pb-3 pt-1 border-t border-border bg-muted/20">
            <div className="space-y-1.5 mt-2">
              <label className="text-xs font-medium text-muted-foreground flex items-center gap-1"><Settings2 className="w-3.5 h-3.5" /> 插件参数</label>
              {/* 修改原因：后端插件已经通过 metadata.params_schema 描述参数，完整配置面板不应再只提供自由文本输入。
                  修改方式：有 schema 时渲染 select、text、number、toggle、multi-select 等控件；没有 schema 时仍回退原文本输入。
                  目的：降低插件配置出错率，同时保持旧插件和手写 options 的兼容性。 */}
              <PluginParamsForm
                options={options}
                schema={paramsSchema}
                onChange={(nextOptions) => updateOptions(plugin.plugin_name, nextOptions)}
                disabled={!isSelected}
                paramsHint={plugin.metadata?.params_hint}
                size="normal"
              />
            </div>

            {plugin.metadata?.provider_config?.key && (
              <div className="space-y-2 mt-4">
                <label className="text-xs font-medium text-muted-foreground flex items-center gap-1">
                  <Settings2 className="w-3.5 h-3.5" />
                  {plugin.metadata?.provider_config?.title || '渠道配置（JSON）'}
                </label>

                {plugin.metadata?.provider_config?.description && (
                  <p className="text-xs text-muted-foreground">{plugin.metadata.provider_config.description}</p>
                )}

                <textarea
                  value={providerConfigText.get(plugin.plugin_name) || ''}
                  onChange={(e) => updateProviderConfigText(plugin.plugin_name, e.target.value)}
                  disabled={!isSelected}
                  rows={6}
                  placeholder={
                    plugin.metadata?.provider_config?.example
                      ? JSON.stringify(plugin.metadata.provider_config.example, null, 2)
                      : '请输入 JSON'
                  }
                  className="w-full bg-background border border-border text-foreground focus:border-emerald-500 px-3 py-2 rounded-md text-sm font-mono disabled:opacity-50 outline-none"
                />

                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    disabled={!isSelected}
                    onClick={() => {
                      try {
                        updateProviderConfigText(plugin.plugin_name, formatJsonText(providerConfigText.get(plugin.plugin_name) || ''));
                      } catch (e: unknown) {
                        toastError(`格式化失败：${e instanceof Error ? e.message : 'invalid json'}`);
                      }
                    }}
                    className="text-xs font-medium text-muted-foreground hover:text-foreground px-2 py-1 bg-muted rounded disabled:opacity-50"
                  >
                    格式化
                  </button>

                  {plugin.metadata?.provider_config?.example != null && (
                    <button
                      type="button"
                      disabled={!isSelected}
                      onClick={() => updateProviderConfigText(plugin.plugin_name, JSON.stringify(plugin.metadata?.provider_config?.example, null, 2))}
                      className="text-xs font-medium text-emerald-600 dark:text-emerald-500 hover:text-emerald-500 px-2 py-1 bg-emerald-500/10 rounded disabled:opacity-50"
                    >
                      填入示例
                    </button>
                  )}

                  <button
                    type="button"
                    disabled={!isSelected}
                    onClick={() => updateProviderConfigText(plugin.plugin_name, '')}
                    className="text-xs font-medium text-red-600 dark:text-red-400 hover:text-red-500 px-2 py-1 bg-red-500/10 rounded disabled:opacity-50"
                  >
                    清空
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    /* 裁剪容器 — 限制动画在编辑面板范围内 */
    <div
      className="absolute inset-0 overflow-hidden z-[5]"
      style={{ pointerEvents: open ? 'auto' : 'none' }}
    >
      {/* 插件面板 — 从左向右滑入 */}
      <div
        className="absolute inset-0 bg-background border-l border-border flex flex-col transition-all duration-250 ease-out"
        style={{
          transform: open ? 'translateX(0)' : 'translateX(-100%)',
          opacity: open ? 1 : 0,
        }}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 sm:px-6 py-4 border-b border-border bg-muted/30 flex-shrink-0">
          <div className="flex items-center gap-3">
            <button
              onClick={() => onOpenChange(false)}
              className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-sm font-medium px-2 py-1 rounded-md hover:bg-muted transition-colors"
            >
              <ArrowLeft className="w-4 h-4" />
              返回编辑
            </button>
            <h3 className="text-lg font-semibold text-foreground flex items-center gap-2">
              <Puzzle className="w-5 h-5 text-emerald-500" />
              插件配置
            </h3>
          </div>
          <button onClick={() => onOpenChange(false)} className="text-muted-foreground hover:text-foreground p-1 rounded-full hover:bg-muted transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-4">
          <p className="text-sm text-muted-foreground">
            勾选要在本渠道启用的插件拦截器。可为每个插件配置参数（格式：plugin:options）。
          </p>

          {/* Toolbar */}
          <div className="space-y-2">
            {/* 修改原因：搜索入口应和插件统计、批量操作放在同一工具区，减少长列表中的查找成本。
                修改方式：把工具栏改成纵向布局，在统计行下方加入受控 input。
                目的：让插件名和描述可以实时参与列表过滤。 */}
            <div className="p-3 bg-muted/40 border border-border rounded-lg space-y-3">
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                <span className="text-sm text-muted-foreground">
                  显示 {groupedPlugins.total} / {effectivePlugins.length} 个插件，已选 <span className="text-foreground font-medium">{selected.size}</span> 个
                </span>
                <div className="flex gap-2">
                  <button type="button" onClick={selectAll} className="text-xs font-medium text-emerald-500 hover:text-emerald-400 px-2 py-1 bg-emerald-500/10 rounded">全选</button>
                  <button type="button" onClick={clearAll} className="text-xs font-medium text-red-500 hover:text-red-400 px-2 py-1 bg-red-500/10 rounded">全不选</button>
                </div>
              </div>
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="搜索插件..."
                className="w-full bg-background border border-border text-foreground placeholder:text-muted-foreground focus:border-emerald-500 px-3 py-1.5 rounded-md text-sm outline-none"
              />
            </div>

            {/* 修改原因：请求拦截和响应拦截插件混在一起时，用户需要按方向切换视图。
                修改方式：在工具栏下方新增小型 pill 按钮组，样式沿用项目内分段选择器。
                目的：保持“全部”为默认视图，同时提供请求和响应两个专门列表。 */}
            <div className="flex items-center bg-card border border-border rounded-lg p-1">
              {INTERCEPTOR_TABS.map(tab => (
                <button
                  key={tab.value}
                  type="button"
                  onClick={() => setActiveTab(tab.value)}
                  className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all flex-1 ${
                    activeTab === tab.value
                      ? 'bg-primary text-primary-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>

          {/* Plugin List */}
          <div className="space-y-2.5">
            {groupedPlugins.total === 0 ? (
              <div className="text-sm text-muted-foreground text-center py-10 border border-dashed border-border rounded-lg bg-card">
                没有符合条件的插件
              </div>
            ) : (
              <>
                {groupedPlugins.selectedPlugins.map(renderPluginCard)}
                {groupedPlugins.selectedPlugins.length > 0 && groupedPlugins.unselectedPlugins.length > 0 && (
                  <div className="flex items-center gap-2 py-1">
                    <div className="h-px bg-border flex-1" />
                    <span className="text-[10px] text-muted-foreground bg-muted border border-border px-2 py-0.5 rounded-full">未启用</span>
                    <div className="h-px bg-border flex-1" />
                  </div>
                )}
                {groupedPlugins.unselectedPlugins.map(renderPluginCard)}
              </>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="p-4 bg-muted/30 border-t border-border flex justify-end gap-3 flex-shrink-0">
          <button onClick={() => onOpenChange(false)} className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</button>
          <button onClick={handleSave} className="px-4 py-2 text-sm font-medium text-white bg-emerald-600 hover:bg-emerald-500 rounded-lg">
            保存插件配置
          </button>
        </div>
      </div>
    </div>
  );
}
