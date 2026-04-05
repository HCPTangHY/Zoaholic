import { useEffect, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { apiFetch } from '../lib/api';
import { 
  Puzzle, 
  Settings2, 
  ChevronDown, 
  ChevronRight, 
  Check, 
  X,
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

export function InterceptorSheet({ open, onOpenChange, allPlugins, enabledPlugins, providerPreferences, onUpdate }: InterceptorSheetProps) {
  // 自行获取插件列表，防止父组件传入的 allPlugins 因 403 等原因为空
  const [localPlugins, setLocalPlugins] = useState<PluginOption[]>(allPlugins);

  // 优先使用自行获取的 localPlugins，回退到父组件传入的 allPlugins
  const effectivePlugins = localPlugins.length > 0 ? localPlugins : allPlugins;

  // Parsing helpers
  const parseEntry = (entry: string) => {
    // 约定：enabled_plugins 的单条配置使用“第一个冒号”分隔 name 与 options。
    // 这样 options 中可以包含冒号（例如 URL、JSON 等）。
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

  // Re-init when opening (important: same sheet instance is reused across different providers)
  useEffect(() => {
    if (!open) return;
    // 每次打开时刷新插件列表，避免首次 403 导致列表永远为空
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
    // 打开面板时重置本地状态，这是组件初始化的标准做法
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

  const selectAll = () => {
    const next = new Map(selected);
    effectivePlugins.forEach(p => {
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

      // empty => delete config key
      if (!t) {
        preferences_delete.push(meta.key);
        continue;
      }

      const configType = meta.type || 'json';
      if (configType === 'json') {
        try {
          preferences_patch[meta.key] = JSON.parse(t);
        } catch (e) {
          alert(`插件 ${plugin.plugin_name} 配置 JSON 格式错误：${e instanceof Error ? e.message : 'invalid json'}`);
          return;
        }
      } else {
        preferences_patch[meta.key] = t;
      }
    }

    onUpdate({ enabled_plugins: result, preferences_patch, preferences_delete });
    onOpenChange(false);
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-[60] animate-in fade-in" />
        <Dialog.Content className="fixed right-0 top-0 h-full w-[500px] bg-background border-l border-border shadow-2xl z-[70] flex flex-col animate-in slide-in-from-right duration-200">
          
          <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-muted/30">
            <Dialog.Title className="text-lg font-semibold text-foreground flex items-center gap-2">
              <Puzzle className="w-5 h-5 text-emerald-500" />
              配置插件拦截器
            </Dialog.Title>
            <Dialog.Close className="text-muted-foreground hover:text-foreground p-1 rounded-full hover:bg-muted transition-colors">
              <X className="w-5 h-5" />
            </Dialog.Close>
          </div>

          <div className="flex-1 overflow-y-auto p-6 space-y-4">
            <p className="text-sm text-muted-foreground">
              勾选要在本渠道启用的插件拦截器。可为每个插件配置参数（格式：plugin:options）。
            </p>

            {/* Toolbar */}
            <div className="flex items-center justify-between p-3 bg-muted/40 border border-border rounded-lg">
              <span className="text-sm text-muted-foreground">
                共 {effectivePlugins.length} 个插件，已选 <span className="text-foreground font-medium">{selected.size}</span> 个
              </span>
              <div className="flex gap-2">
                <button onClick={selectAll} className="text-xs font-medium text-emerald-500 hover:text-emerald-400 px-2 py-1 bg-emerald-500/10 rounded">全选</button>
                <button onClick={clearAll} className="text-xs font-medium text-red-500 hover:text-red-400 px-2 py-1 bg-red-500/10 rounded">全不选</button>
              </div>
            </div>

            {/* Plugin List (Accordion) */}
            <div className="space-y-2.5">
              {effectivePlugins.map(plugin => {
                const isSelected = selected.has(plugin.plugin_name);
                const isExpanded = expanded.has(plugin.plugin_name);
                const options = selected.get(plugin.plugin_name) || '';

                return (
                  <div key={plugin.plugin_name} className={`border rounded-lg transition-colors ${isSelected ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-border bg-card'}`}>
                    {/* Header */}
                    <div className="flex items-center justify-between p-3 cursor-pointer select-none" onClick={() => toggleExpand(plugin.plugin_name)}>
                      <div className="flex items-center gap-3">
                        <button 
                          onClick={(e) => { e.stopPropagation(); toggleSelect(plugin.plugin_name); }}
                          className={`w-5 h-5 rounded border flex items-center justify-center transition-colors ${isSelected ? 'bg-emerald-500 border-emerald-500 text-white' : 'bg-background border-border'}`}
                        >
                          {isSelected && <Check className="w-3.5 h-3.5" />}
                        </button>
                        <div>
                          <div className="flex items-center gap-2">
                            <span className={`text-sm font-medium ${isSelected ? 'text-foreground' : 'text-muted-foreground'}`}>{plugin.plugin_name}</span>
                            <span className="text-xs bg-muted text-muted-foreground px-1.5 py-0.5 rounded font-mono">v{plugin.version}</span>
        </div>
                          {options && <div className="text-xs text-muted-foreground font-mono mt-0.5 max-w-[200px] truncate">{options}</div>}
                        </div>
                      </div>
                      <div className="text-muted-foreground">{isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}</div>
                    </div>

                    {/* Details */}
                    {isExpanded && (
                      <div className="px-3 pb-3 pt-1 border-t border-border bg-muted/20">
                        <p className="text-xs text-muted-foreground mb-3 mt-2">{plugin.description}</p>
                        
                        <div className="space-y-1.5">
                          <label className="text-xs font-medium text-muted-foreground flex items-center gap-1"><Settings2 className="w-3.5 h-3.5" /> 插件参数</label>
                          <input
                            type="text"
                            value={options}
                            onChange={(e) => updateOptions(plugin.plugin_name, e.target.value)}
                            disabled={!isSelected}
                            placeholder={plugin.metadata?.params_hint || "留空使用默认值"}
                            className="w-full bg-background border border-border text-foreground focus:border-emerald-500 px-3 py-2 rounded-md text-sm font-mono disabled:opacity-50 outline-none"
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
                                    alert(`格式化失败：${e instanceof Error ? e.message : 'invalid json'}`);
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
              })}
            </div>

          </div>

          <div className="p-4 bg-muted/30 border-t border-border flex justify-end gap-3">
            <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
            <button onClick={handleSave} className="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary hover:bg-primary/90 rounded-lg">
              保存插件配置
            </button>
          </div>

        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}