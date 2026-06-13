/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  Smartphone, SlidersHorizontal, Puzzle, PackageCheck,
  CheckCircle2, ShieldCheck, Plus, X
} from 'lucide-react';
import { ProviderLogo } from '../../../components/ProviderLogos';
import { UiSlot } from './KeyComponents';
import { hasUiSlot } from '../utils';
import { PluginParamsForm, type ParamSchema } from '../../../components/PluginParamsForm';

interface PipelineViewProps {
  formData: any;
  allPlugins: any[];
  overridesJson: string;
  setOverridesJson: (v: string) => void;
  headerEntries: { key: string; value: string }[];
  setHeaderEntries: (v: { key: string; value: string }[]) => void;
  onOpenPluginSheet: () => void;
  // 修改原因：Pipeline 面板需要支持 inline 增删插件和编辑参数，不能只打开 InterceptorSheet。
  // 修改方式：新增 onPluginsChange，把更新后的 enabled_plugins 数组交回 ChannelEditor 写入 formData。
  // 目的：让常用插件操作可以在 Pipeline 面板内直接完成。
  onPluginsChange: (plugins: string[]) => void;
  formatJsonOnBlur: (json: string, setter: (v: string) => void, label: string) => void;
}

/* ── 节点组件 ── */

function EndpointDot({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <div className="flex flex-col items-center flex-shrink-0">
      <div className="w-8 h-8 rounded-full flex items-center justify-center border border-border bg-muted text-muted-foreground">
        {icon}
      </div>
      <span className="mt-1.5 text-[10px] text-muted-foreground">{label}</span>
    </div>
  );
}

function PipeNode({ icon, label, badge, badgeEmpty, active, onClick }: {
  icon: ReactNode; label: string; badge?: number; badgeEmpty?: boolean;
  active?: boolean; onClick?: () => void;
}) {
  return (
    <div className="flex flex-col items-center cursor-pointer group flex-shrink-0" onClick={onClick}>
      <div className={`relative w-10 h-10 rounded-xl flex items-center justify-center border-[1.5px] transition-all
        ${active
          ? 'border-primary bg-primary/10 shadow-[0_0_14px_rgba(99,102,241,0.2)]'
          : 'border-border bg-muted group-hover:border-primary/50'}`}
      >
        <span className={`transition-colors ${active ? 'text-primary' : 'text-muted-foreground group-hover:text-foreground'}`}>{icon}</span>
        {badge !== undefined && (
          <span className={`absolute -top-1.5 -right-1.5 text-[9px] font-bold w-4 h-4 rounded-full flex items-center justify-center border-[1.5px] border-card
            ${badgeEmpty ? 'bg-muted-foreground/50 text-card' : 'bg-primary text-primary-foreground'}`}>
            {badge}
          </span>
        )}
      </div>
      <span className={`mt-1.5 text-[10px] font-medium transition-colors whitespace-nowrap ${active ? 'text-foreground' : 'text-muted-foreground group-hover:text-foreground'}`}>
        {label}
      </span>
    </div>
  );
}

function Connector() {
  return (
    <div className="flex items-center h-10 min-w-[14px] flex-1 max-w-[32px] relative mx-0.5 flex-shrink-0">
      <div className="absolute top-1/2 left-0 right-[5px] h-px bg-border" />
      <div className="absolute right-0 top-1/2 -translate-y-1/2 border-t-[3px] border-t-transparent border-b-[3px] border-b-transparent border-l-[5px] border-l-border" />
    </div>
  );
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

function InfoCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-card rounded-md px-2.5 py-2">
      <div className="text-[10px] text-muted-foreground">{label}</div>
      <div className="text-xs font-mono truncate">{value}</div>
    </div>
  );
}

type PluginStage = 'request' | 'response';

type EnabledPluginEntry = {
  name: string;
  opts?: string;
  hasOpts: boolean;
  description?: string;
  paramsSchema?: ParamSchema[];
  paramsHint?: string;
  entryIndex: number;
};

function parseEnabledPlugin(value: string) {
  // 修改原因：enabled_plugins 允许使用 plugin:opts 格式，直接 split(':') 会丢失参数中的冒号。
  // 修改方式：只按第一个冒号拆分，保留后续字符串作为完整参数。
  // 目的：inline 编辑参数时不破坏已有插件配置。
  const colonIndex = value.indexOf(':');
  if (colonIndex < 0) return { name: value, opts: undefined, hasOpts: false };
  return { name: value.slice(0, colonIndex), opts: value.slice(colonIndex + 1), hasOpts: true };
}

function buildEnabledPluginValue(name: string, opts: string) {
  // 修改原因：参数输入框允许用户清空 opts，清空后应回到纯插件名格式。
  // 修改方式：提交时 trim 参数，非空写 plugin:opts，空值只写 plugin。
  // 目的：保持 enabled_plugins 数组简洁，并兼容无参数插件。
  const trimmedOpts = opts.trim();
  return trimmedOpts ? `${name}:${trimmedOpts}` : name;
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
              {/* 修改原因：插件参数已由 metadata.params_schema 描述，继续使用纯文本输入会让常用参数难以理解。
                  修改方式：有 schema 时渲染紧凑可视化控件；没有 schema 时回退到原 options 文本输入。
                  目的：让 Pipeline 面板内直接完成大多数插件参数配置，同时保持旧插件兼容。 */}
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

function PluginAddDropdown({ stage, allPlugins, enabledPluginNames, openMenu, setOpenMenu, onAdd, onOpenPluginSheet }: {
  stage: PluginStage;
  allPlugins: any[];
  enabledPluginNames: Set<string>;
  openMenu: PluginStage | null;
  setOpenMenu: (stage: PluginStage | null) => void;
  onAdd: (pluginName: string) => void;
  onOpenPluginSheet: () => void;
}) {
  const isOpen = openMenu === stage;
  const candidates = useMemo(() => {
    // 修改原因：快速添加菜单只应展示当前节点可用、且尚未启用的插件。
    // 修改方式：按 request_interceptors 或 response_interceptors 过滤插件，并排除 enabled_plugins 中已有的插件名。
    // 目的：减少选择干扰，让出站和响应节点只显示相关插件。
    return allPlugins.filter((plugin: any) => {
      const pluginName = String(plugin?.plugin_name || '').trim();
      if (!pluginName || enabledPluginNames.has(pluginName)) return false;
      if (stage === 'request') return (plugin.request_interceptors?.length ?? 0) > 0;
      return (plugin.response_interceptors?.length ?? 0) > 0;
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
                key={plugin.plugin_name}
                type="button"
                onClick={() => onAdd(plugin.plugin_name)}
                className="w-full rounded px-3 py-2 text-left hover:bg-muted"
              >
                <div className="text-xs font-medium text-foreground">{plugin.plugin_name}</div>
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

/* ── 主组件 ── */

export function PipelineView({
  formData, allPlugins, overridesJson, setOverridesJson,
  headerEntries, setHeaderEntries, onOpenPluginSheet, onPluginsChange, formatJsonOnBlur,
}: PipelineViewProps) {
  const [activeNode, setActiveNode] = useState<string | null>(null);
  const [openAddMenu, setOpenAddMenu] = useState<PluginStage | null>(null);
  const enabledPlugins: string[] = formData.preferences?.enabled_plugins || [];

  useEffect(() => {
    // 修改原因：快速添加菜单使用 absolute 定位弹出，需要点击外部时自动关闭。
    // 修改方式：菜单打开时监听 document mousedown，点击不在 data-plugin-add-menu 容器内则关闭。
    // 目的：不引入新 UI 库，同时保持常见 dropdown 交互。
    if (!openAddMenu) return;
    const closeOnOutsideClick = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target?.closest('[data-plugin-add-menu]')) setOpenAddMenu(null);
    };
    document.addEventListener('mousedown', closeOnOutsideClick);
    return () => document.removeEventListener('mousedown', closeOnOutsideClick);
  }, [openAddMenu]);

  const enabledPluginNames = useMemo(() => {
    // 修改原因：快速添加要过滤已启用插件，enabled_plugins 可能包含参数。
    // 修改方式：先用 parseEnabledPlugin 取冒号前的插件名，再放入 Set。
    // 目的：避免同一插件通过快速入口被重复添加。
    return new Set(enabledPlugins.map(value => parseEnabledPlugin(value).name).filter(Boolean));
  }, [enabledPlugins]);

  const enabledPluginEntries = useMemo(() => enabledPlugins.map((value, entryIndex) => {
    const parsed = parseEnabledPlugin(value);
    const info = allPlugins.find((plugin: any) => plugin.plugin_name === parsed.name);
    // 修改原因：Pipeline inline 参数表单需要读取后端插件返回的 metadata.params_schema。
    // 修改方式：在 enabled_plugins 派生条目中带上 paramsSchema 和 paramsHint，PluginCard 负责渲染。
    // 目的：让卡片保留原 options pill，同时提供可视化参数编辑。
    const paramsSchema = Array.isArray(info?.metadata?.params_schema) ? info.metadata.params_schema : [];
    return { ...parsed, description: info?.description, paramsSchema, paramsHint: info?.metadata?.params_hint, entryIndex, info };
  }), [enabledPlugins, allPlugins]);

  // 按 interceptor 类型分类已启用插件
  const { requestPlugins, responsePlugins } = useMemo(() => {
    const req: EnabledPluginEntry[] = [];
    const res: EnabledPluginEntry[] = [];
    for (const entry of enabledPluginEntries) {
      const info = entry.info;
      const hasReq = !info || (info.request_interceptors?.length ?? 0) > 0;
      const hasRes = info && (info.response_interceptors?.length ?? 0) > 0;
      if (hasReq) req.push(entry);
      if (hasRes) res.push(entry);
    }
    return { requestPlugins: req, responsePlugins: res };
  }, [enabledPluginEntries]);

  const overrideCount = useMemo(() => {
    try {
      const obj = JSON.parse(overridesJson || '{}');
      let n = 0;
      for (const v of Object.values(obj)) {
        if (v && typeof v === 'object') n += Object.keys(v as object).length;
      }
      return n;
    } catch { return 0; }
  }, [overridesJson]);

  const headerCount = headerEntries.filter(e => e.key.trim()).length;
  const toggle = (id: string) => setActiveNode(prev => prev === id ? null : id);

  const removePluginAt = (entryIndex: number) => {
    // 修改原因：PluginCard 的删除按钮需要删除 enabled_plugins 中对应的原始条目，而不是按插件名删除所有同名项。
    // 修改方式：通过派生列表保存 entryIndex，删除时按数组下标过滤。
    // 目的：兼容未来同插件多参数实例的情况。
    onPluginsChange(enabledPlugins.filter((_, index) => index !== entryIndex));
  };

  const updatePluginOptsAt = (entryIndex: number, name: string, opts: string) => {
    // 修改原因：inline 参数输入只改当前插件条目，不能影响其他插件和其他 preferences。
    // 修改方式：复制 enabled_plugins 数组，替换指定 index 的 plugin:opts 字符串。
    // 目的：保持 Pipeline 面板内参数编辑的最小更新范围。
    const nextPlugins = [...enabledPlugins];
    nextPlugins[entryIndex] = buildEnabledPluginValue(name, opts);
    onPluginsChange(nextPlugins);
  };

  const addPlugin = (pluginName: string) => {
    // 修改原因：快速添加菜单点击插件后应立即启用，并关闭当前 dropdown。
    // 修改方式：把插件名追加到 enabled_plugins，随后清空 openAddMenu。
    // 目的：让常用插件启用流程在 Pipeline 面板内一步完成。
    onPluginsChange([...enabledPlugins, pluginName]);
    setOpenAddMenu(null);
  };

  return (
    <div className="bg-card border border-border rounded-xl px-4 pt-4 pb-3">
      {/* Pipeline flow — 上游居中 */}
      <div className="grid grid-cols-[1fr_auto_1fr] items-start">
        {/* 左侧: start → inbound → overrides → request → */}
        <div className="flex items-center justify-end gap-0">
          <EndpointDot icon={<Smartphone className="w-3.5 h-3.5" />} label="入" />
          <Connector />
          <PipeNode icon={<ShieldCheck className="w-4 h-4" />} label="入站" badge={0} badgeEmpty active={activeNode === 'inbound'} onClick={() => toggle('inbound')} />
          <Connector />
          <PipeNode icon={<SlidersHorizontal className="w-4 h-4" />} label="覆写" badge={overrideCount} badgeEmpty={overrideCount === 0} active={activeNode === 'overrides'} onClick={() => toggle('overrides')} />
          <Connector />
          <PipeNode icon={<Puzzle className="w-4 h-4" />} label="出站" badge={requestPlugins.length} badgeEmpty={requestPlugins.length === 0} active={activeNode === 'request'} onClick={() => toggle('request')} />
          <Connector />
        </div>

        {/* 中间: upstream — 使用 ProviderLogo */}
        <div className="flex flex-col items-center cursor-pointer group flex-shrink-0 mx-1" onClick={() => toggle('upstream')}>
          <div className={`relative w-10 h-10 rounded-xl flex items-center justify-center border-[1.5px] border-dashed transition-all overflow-hidden
            ${activeNode === 'upstream'
              ? 'border-cyan-400 bg-cyan-400/5 shadow-[0_0_14px_rgba(34,211,238,0.15)]'
              : 'border-cyan-400/40 bg-muted group-hover:border-cyan-400'}`}
          >
            <div className="scale-[0.8]">
              <ProviderLogo name={formData.provider || ''} engine={formData.engine} baseUrl={formData.base_url} />
            </div>
            {headerCount > 0 && (
              <span className="absolute -top-1.5 -right-1.5 text-[9px] font-bold w-4 h-4 rounded-full flex items-center justify-center border-[1.5px] border-card bg-cyan-500 text-white">
                {headerCount}
              </span>
            )}
          </div>
          <span className={`mt-1.5 text-[10px] font-medium transition-colors ${activeNode === 'upstream' ? 'text-foreground' : 'text-muted-foreground group-hover:text-foreground'}`}>
            上游
          </span>
        </div>

        {/* 右侧: → response → end */}
        <div className="flex items-center justify-start gap-0">
          <Connector />
          <PipeNode icon={<PackageCheck className="w-4 h-4" />} label="响应" badge={responsePlugins.length} badgeEmpty={responsePlugins.length === 0} active={activeNode === 'response'} onClick={() => toggle('response')} />
          <Connector />
          <EndpointDot icon={<CheckCircle2 className="w-3.5 h-3.5" />} label="出" />
        </div>
      </div>

      {/* 展开详情面板 */}
      {activeNode && (
        <div className="mt-3 bg-muted/50 border border-border rounded-lg overflow-visible animate-in fade-in slide-in-from-top-1 duration-150">
          {activeNode === 'inbound' && (
            <DetailPanel icon={<ShieldCheck className="w-4 h-4" />} title="入站拦截" desc="鉴权后 · 分配前">
              <p className="text-xs text-muted-foreground mb-3">暂无入站拦截器。</p>
              <button onClick={onOpenPluginSheet} className="text-xs text-primary hover:text-primary/80 flex items-center gap-1">
                <Plus className="w-3.5 h-3.5" /> 配置插件
              </button>
            </DetailPanel>
          )}

          {activeNode === 'overrides' && (
            <DetailPanel icon={<SlidersHorizontal className="w-4 h-4" />} title="参数覆写" desc="请求体参数">
              <textarea
                value={overridesJson}
                onChange={e => setOverridesJson(e.target.value)}
                onBlur={() => formatJsonOnBlur(overridesJson, setOverridesJson, '请求体覆写')}
                rows={5}
                placeholder='{"all": {"temperature": 0.1}}'
                className="w-full bg-background border border-border px-3 py-2 rounded-lg text-xs font-mono focus:border-primary outline-none text-foreground resize-y min-h-[80px]"
              />
              <p className="text-[11px] text-muted-foreground mt-1.5">
                key 为 <code className="px-1 py-0.5 bg-background rounded text-[10px]">all</code> 或 <code className="px-1 py-0.5 bg-background rounded text-[10px]">*</code> 全局生效，模型名精确匹配。值为 <code className="px-1 py-0.5 bg-background rounded text-[10px]">null</code> 删除字段。<code className="px-1 py-0.5 bg-background rounded text-[10px]">+</code> 前缀追加数组。
              </p>
              {hasUiSlot(formData.engine, 'override_hint', enabledPlugins) && (
                <UiSlot engine={formData.engine} slot="override_hint" data={null} element="div" className="text-[11px] text-amber-600 dark:text-amber-400 mt-1" enabledPlugins={enabledPlugins} />
              )}
            </DetailPanel>
          )}

          {activeNode === 'request' && (
            <DetailPanel icon={<Puzzle className="w-4 h-4" />} title="出站拦截" desc="发往上游前">
              {requestPlugins.length === 0 ? (
                <p className="text-xs text-muted-foreground italic">未启用任何请求拦截器</p>
              ) : (
                <div className="space-y-2 mb-3">
                  {requestPlugins.map((p) => (
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
              )}
              <PluginAddDropdown
                stage="request"
                allPlugins={allPlugins}
                enabledPluginNames={enabledPluginNames}
                openMenu={openAddMenu}
                setOpenMenu={setOpenAddMenu}
                onAdd={addPlugin}
                onOpenPluginSheet={onOpenPluginSheet}
              />
            </DetailPanel>
          )}

          {activeNode === 'upstream' && (
            <DetailPanel icon={null} title="上游端点" desc={formData.engine || 'openai'}>
              <div className="grid grid-cols-2 gap-2 mb-4">
                <InfoCell label="Base URL" value={formData.base_url || '默认'} />
                <InfoCell label="Engine" value={formData.engine || 'openai'} />
              </div>
              <div className="text-xs font-medium text-foreground mb-2">自定义请求头</div>
              <div className="space-y-2">
                {headerEntries.map((entry, idx) => (
                  <div key={idx} className="flex gap-2 items-center">
                    <input value={entry.key} onChange={e => { const n = [...headerEntries]; n[idx] = { ...n[idx], key: e.target.value }; setHeaderEntries(n); }}
                      placeholder="Header-Name" className="flex-1 bg-background border border-border px-2.5 py-1.5 rounded text-xs font-mono text-foreground focus:border-primary outline-none min-w-0" />
                    <input value={entry.value} onChange={e => { const n = [...headerEntries]; n[idx] = { ...n[idx], value: e.target.value }; setHeaderEntries(n); }}
                      placeholder="Value" className="flex-1 bg-background border border-border px-2.5 py-1.5 rounded text-xs font-mono text-foreground focus:border-primary outline-none min-w-0" />
                    <button onClick={() => setHeaderEntries(headerEntries.filter((_, i) => i !== idx))} className="text-muted-foreground hover:text-destructive flex-shrink-0">
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                ))}
                <button onClick={() => setHeaderEntries([...headerEntries, { key: '', value: '' }])} className="text-xs text-primary hover:text-primary/80 flex items-center gap-1">
                  <Plus className="w-3 h-3" /> 添加请求头
                </button>
              </div>
            </DetailPanel>
          )}

          {activeNode === 'response' && (
            <DetailPanel icon={<PackageCheck className="w-4 h-4" />} title="响应拦截" desc="返回客户端前">
              {responsePlugins.length === 0 ? (
                <p className="text-xs text-muted-foreground">当前无响应拦截器。</p>
              ) : (
                <div className="space-y-2 mb-3">
                  {responsePlugins.map((p) => (
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
              )}
              <PluginAddDropdown
                stage="response"
                allPlugins={allPlugins}
                enabledPluginNames={enabledPluginNames}
                openMenu={openAddMenu}
                setOpenMenu={setOpenAddMenu}
                onAdd={addPlugin}
                onOpenPluginSheet={onOpenPluginSheet}
              />
            </DetailPanel>
          )}
        </div>
      )}
    </div>
  );
}
