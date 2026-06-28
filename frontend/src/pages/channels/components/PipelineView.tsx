/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  Smartphone, SlidersHorizontal, Puzzle, PackageCheck,
  CheckCircle2, ShieldCheck, Plus, X, Power
} from 'lucide-react';
import { ProviderLogo } from '../../../components/ProviderLogos';
import { buildEnabledPluginValue as buildPluginEntryValue, parseEnabledPluginValue, type EnabledPluginValue } from '../../../lib/pluginEntries';
import { DeferredInput, UiSlot } from './KeyComponents';
import { hasUiSlot } from '../utils';
import { PluginParamsForm, type ParamSchema } from '../../../components/PluginParamsForm';
import {
  formatKeyRuleKeywordsInput,
  formatKeyRuleStatusInput,
  getKeyRuleRetryMode,
  parseKeyRuleKeywordsInput,
  parseKeyRuleStatusInput,
  setKeyRuleRetryMode,
  type KeyRuleRetryMode,
} from '../../../lib/keyRules';

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
  onPluginsChange: (plugins: EnabledPluginValue[]) => void;
  // 修改原因：System Prompt 已从高级设置迁移到 Pipeline 的上游节点，需要由 PipelineView 回写 preferences。
  // 修改方式：新增 onSystemPromptChange，由 ChannelEditor 传入 updatePreference('system_prompt', value)。
  // 目的：保持数据仍由 ChannelEditor 管理，同时让上游节点成为系统提示词入口。
  onSystemPromptChange?: (value: string) => void;
  // 修改原因：Key Rules 要作为独立 Pipeline 节点编辑，不能继续留在路由与限流区域。
  // 修改方式：新增 keyRules 和 onKeyRulesChange，把规则数组作为受控数据传入 PipelineView。
  // 目的：让规则节点能直接复用原编辑器，并把变更写回 preferences.key_rules。
  keyRules?: any[];
  onKeyRulesChange?: (rules: any[]) => void;
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
    <div className="flex items-center h-10 min-w-[4px] flex-1 max-w-[14px] relative mx-0 shrink">
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

type PluginStage = 'channel_inbound' | 'request' | 'response' | 'channel_outbound' | 'key_outbound';
type OverrideTab = 'params' | 'headers';

type EnabledPluginEntry = {
  name: string;
  opts?: string;
  hasOpts: boolean;
  description?: string;
  paramsSchema?: ParamSchema[];
  paramsHint?: string;
  entryIndex: number;
};

function parseEnabledPlugin(value: EnabledPluginValue) {
  return parseEnabledPluginValue(value);
}

function buildEnabledPluginValue(name: string, opts: string): EnabledPluginValue {
  return buildPluginEntryValue(name, opts);
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
  // ponytail: 旧插件无 params_schema 时仍需显示文本输入框，PluginParamsForm 已有 schema=[] 回退逻辑
  const shouldShowParams = true;

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
    // 修改原因：后端新增 channel_inbound、channel_outbound 和 key_outbound 阶段，快速添加菜单不能只按 request/response 过滤。
    // 修改方式：用阶段到后端数组字段的映射筛选候选插件，并排除 enabled_plugins 中已有的插件名。
    // 目的：让 Pipeline 中每个插件节点只显示适用于当前阶段的插件。
    const stageFieldMap: Record<PluginStage, string> = {
      channel_inbound: 'channel_inbound_interceptors',
      request: 'request_interceptors',
      response: 'response_interceptors',
      channel_outbound: 'channel_outbound_interceptors',
      key_outbound: 'key_outbound_interceptors',
    };
    return allPlugins.filter((plugin: any) => {
      const pluginName = String(plugin?.plugin_name || '').trim();
      if (!pluginName || enabledPluginNames.has(pluginName)) return false;
      const stageField = stageFieldMap[stage];
      return (plugin?.[stageField]?.length ?? 0) > 0;
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

const KEY_RULE_TEMPLATES = [
  { label: '标准', rules: [
    { match: { status: [429] }, duration: 30 },
    { match: { status: [401, 403] }, duration: -1 },
    { match: 'default', duration: 60 },
  ]},
  { label: '激进', rules: [
    { match: { status: [429] }, duration: 10 },
    { match: { status: [401, 403, 500] }, duration: -1 },
    { match: 'default', duration: 30 },
  ]},
  { label: '宽松', rules: [
    { match: { status: [429] }, duration: 60 },
    { match: { status: [401, 403] }, duration: -1 },
  ]},
];

function cloneRules(rules: any[]) {
  // 修改原因：预设模板会被后续输入控件修改，直接复用常量对象会污染模板。
  // 修改方式：通过 JSON 深拷贝生成新的规则数组，再交给上层保存。
  // 目的：保证标准、激进、宽松模板每次点击都从干净数据开始。
  return JSON.parse(JSON.stringify(rules));
}

function KeyRulesPanel({ rules, onKeyRulesChange }: { rules: any[]; onKeyRulesChange?: (rules: any[]) => void }) {
  const safeRules = Array.isArray(rules) ? rules : [];
  const applyRules = (nextRules: any[]) => onKeyRulesChange?.(nextRules);

  return (
    <div>
      {/* 修改原因：Key Rules 从 ChannelEditor 的路由与限流区迁移到 Pipeline，需要保留原有预设、规则列表和添加入口。 */}
      {/* 修改方式：把原 JSX 改为受控面板，所有 updatePreference('key_rules', ...) 替换为 onKeyRulesChange?.(...)。 */}
      {/* 目的：让规则节点成为 Pipeline 中第六个可编辑节点，同时不改变 key_rules 的保存结构。 */}
      <div className="flex items-center justify-between mb-3">
        <label className="text-sm font-medium text-foreground flex items-center gap-1.5">
          <Power className="w-3.5 h-3.5 text-red-500" /> Key 错误处理规则
        </label>
        <div className="flex gap-1.5">
          {KEY_RULE_TEMPLATES.map(tpl => (
            <button
              key={tpl.label}
              type="button"
              onClick={() => applyRules(cloneRules(tpl.rules))}
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

      <div className="space-y-1">
        {safeRules.map((rule: any, idx: number) => {
          const replaceRule = (r: any) => { const n = [...safeRules]; n[idx] = r; applyRules(n); };
          const updateRule = (p: any) => replaceRule({ ...safeRules[idx], ...p });
          const clearField = (f: 'remap' | 'retry') => { const r = { ...safeRules[idx] }; delete r[f]; replaceRule(r); };
          const removeRule = () => applyRules(safeRules.filter((_: any, i: number) => i !== idx));
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

      <button
        type="button"
        onClick={() => applyRules([...safeRules, { match: { status: [429] }, duration: 30 }])}
        className="text-xs text-primary hover:text-primary/80 flex items-center gap-1 mt-2"
      >
        <Plus className="w-3 h-3" /> 添加规则
      </button>
    </div>
  );
}

/* ── 主组件 ── */

export function PipelineView({
  formData, allPlugins, overridesJson, setOverridesJson,
  headerEntries, setHeaderEntries, onOpenPluginSheet, onPluginsChange,
  onSystemPromptChange, keyRules = [], onKeyRulesChange, formatJsonOnBlur,
}: PipelineViewProps) {
  const [activeNode, setActiveNode] = useState<string | null>(null);
  const [openAddMenu, setOpenAddMenu] = useState<PluginStage | null>(null);
  const [overrideTab, setOverrideTab] = useState<OverrideTab>('params');
  const enabledPlugins: EnabledPluginValue[] = formData.preferences?.enabled_plugins || [];

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
  const { channelInboundPlugins, requestPlugins, responsePlugins, channelOutboundPlugins, keyOutboundPlugins } = useMemo(() => {
    // 修改原因：Pipeline 已经有渠道入站和渠道出站节点，后端新增阶段后不能继续显示“暂无”。
    // 修改方式：按后端返回的五个阶段数组分类 enabled_plugins；未知插件仍按旧逻辑放到请求拦截，兼容旧接口。
    // 目的：让新阶段插件可以在对应节点中直接删除、编辑参数和快速添加。
    const chIn: EnabledPluginEntry[] = [];
    const req: EnabledPluginEntry[] = [];
    const res: EnabledPluginEntry[] = [];
    const chOut: EnabledPluginEntry[] = [];
    const keyOut: EnabledPluginEntry[] = [];
    for (const entry of enabledPluginEntries) {
      const info = entry.info;
      const hasChannelInbound = info && (info.channel_inbound_interceptors?.length ?? 0) > 0;
      const hasReq = !info || (info.request_interceptors?.length ?? 0) > 0;
      const hasRes = info && (info.response_interceptors?.length ?? 0) > 0;
      const hasChannelOutbound = info && (info.channel_outbound_interceptors?.length ?? 0) > 0;
      const hasKeyOutbound = info && (info.key_outbound_interceptors?.length ?? 0) > 0;
      if (hasChannelInbound) chIn.push(entry);
      if (hasReq) req.push(entry);
      if (hasRes) res.push(entry);
      if (hasChannelOutbound) chOut.push(entry);
      if (hasKeyOutbound) keyOut.push(entry);
    }
    return {
      channelInboundPlugins: chIn,
      requestPlugins: req,
      responsePlugins: res,
      channelOutboundPlugins: chOut,
      keyOutboundPlugins: keyOut,
    };
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
  const overrideAndHeaderCount = overrideCount + headerCount;
  const systemPrompt = formData?.preferences?.system_prompt || '';
  const hasSystemPrompt = systemPrompt.trim().length > 0;
  const safeKeyRules = Array.isArray(keyRules) ? keyRules : [];
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
    <div className="bg-card border border-border rounded-xl px-4 pt-4 pb-3 overflow-visible">
      {/* Pipeline flow — 上游居中 */}
      <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-start w-full min-w-0 gap-x-1">
        {/* 左侧: start → inbound → overrides → request → */}
        <div className="flex min-w-0 items-center justify-end gap-0">
          <EndpointDot icon={<Smartphone className="w-3.5 h-3.5" />} label="入" />
          <Connector />
          <PipeNode icon={<ShieldCheck className="w-4 h-4" />} label="渠道入站" badge={channelInboundPlugins.length} badgeEmpty={channelInboundPlugins.length === 0} active={activeNode === 'inbound'} onClick={() => toggle('inbound')} />
          <Connector />
          <PipeNode icon={<SlidersHorizontal className="w-4 h-4" />} label="覆写" badge={overrideAndHeaderCount} badgeEmpty={overrideAndHeaderCount === 0} active={activeNode === 'overrides'} onClick={() => toggle('overrides')} />
          <Connector />
          <PipeNode icon={<Puzzle className="w-4 h-4" />} label="请求拦截" badge={requestPlugins.length} badgeEmpty={requestPlugins.length === 0} active={activeNode === 'request'} onClick={() => toggle('request')} />
          <Connector />
        </div>

        {/* 中间: upstream — 使用 ProviderLogo */}
        <div className="flex flex-col items-center cursor-pointer group flex-shrink-0 mx-0.5" onClick={() => toggle('upstream')}>
          <div className={`relative w-10 h-10 rounded-xl flex items-center justify-center border-[1.5px] border-dashed transition-all overflow-visible
            ${activeNode === 'upstream'
              ? 'border-cyan-400 bg-cyan-400/5 shadow-[0_0_14px_rgba(34,211,238,0.15)]'
              : 'border-cyan-400/40 bg-muted group-hover:border-cyan-400'}`}
          >
            <div className="w-full h-full rounded-[10px] overflow-hidden flex items-center justify-center">
              <div className="scale-[0.8]">
                <ProviderLogo name={formData.provider || ''} engine={formData.engine} baseUrl={formData.base_url} />
              </div>
            </div>
            {hasSystemPrompt && (
              <span className="absolute -top-1.5 -right-1.5 z-10 text-[8px] font-bold min-w-5 h-4 px-1 rounded-full flex items-center justify-center border-[1.5px] border-card bg-cyan-500 text-white">
                SP
              </span>
            )}
          </div>
          <span className={`mt-1.5 text-[10px] font-medium transition-colors ${activeNode === 'upstream' ? 'text-foreground' : 'text-muted-foreground group-hover:text-foreground'}`}>
            上游
          </span>
        </div>

        {/* 右侧: → response → key rules → end */}
        <div className="flex min-w-0 items-center justify-start gap-0">
          <Connector />
          <PipeNode icon={<PackageCheck className="w-4 h-4" />} label="响应" badge={responsePlugins.length} badgeEmpty={responsePlugins.length === 0} active={activeNode === 'response'} onClick={() => toggle('response')} />
          <Connector />
          <PipeNode icon={<Power className="w-4 h-4" />} label="规则" badge={safeKeyRules.length} badgeEmpty={safeKeyRules.length === 0} active={activeNode === 'keyrules'} onClick={() => toggle('keyrules')} />
          <Connector />
          <PipeNode icon={<PackageCheck className="w-4 h-4" />} label="渠道出站" badge={channelOutboundPlugins.length} badgeEmpty={channelOutboundPlugins.length === 0} active={activeNode === 'ch_outbound'} onClick={() => toggle('ch_outbound')} />
          <Connector />
          <EndpointDot icon={<CheckCircle2 className="w-3.5 h-3.5" />} label="出" />
        </div>
      </div>

      {/* 展开详情面板 */}
      {activeNode && (
        <div className="mt-3 bg-muted/50 border border-border rounded-lg overflow-visible animate-in fade-in slide-in-from-top-1 duration-150">
          {activeNode === 'inbound' && (
            <DetailPanel icon={<ShieldCheck className="w-4 h-4" />} title="渠道入站拦截" desc="分配后 · 转格式前">
              {/* 修改原因：后端新增 channel_inbound_interceptors 后，渠道入站节点应展示和编辑对应插件。 */}
              {/* 修改方式：复用 PluginCard 和 PluginAddDropdown，stage 传 channel_inbound。 */}
              {/* 目的：让渠道入站插件可在 Pipeline 内完成常用增删和参数编辑。 */}
              {channelInboundPlugins.length === 0 ? (
                <p className="text-xs text-muted-foreground italic">未启用任何渠道入站拦截器</p>
              ) : (
                <div className="space-y-2 mb-3">
                  {channelInboundPlugins.map((p) => (
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
                stage="channel_inbound"
                allPlugins={allPlugins}
                enabledPluginNames={enabledPluginNames}
                openMenu={openAddMenu}
                setOpenMenu={setOpenAddMenu}
                onAdd={addPlugin}
                onOpenPluginSheet={onOpenPluginSheet}
              />
            </DetailPanel>
          )}

          {activeNode === 'overrides' && (
            <DetailPanel icon={<SlidersHorizontal className="w-4 h-4" />} title="覆写与请求头" desc="请求体参数 · 请求头">
              {/* 修改原因：请求头编辑器从上游节点迁移到覆写节点，需要和参数覆写共用同一个详情面板。 */}
              {/* 修改方式：在覆写详情内增加两个轻量 tab，分别展示 overridesJson textarea 和 headerEntries 编辑器。 */}
              {/* 目的：让覆写节点徽标同时代表参数覆写和请求头数量，上游节点专注展示端点与系统提示词。 */}
              <div className="mb-3 inline-flex rounded-lg border border-border bg-background p-0.5 text-xs">
                {[
                  ['params', '参数覆写'],
                  ['headers', '请求头'],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setOverrideTab(value as OverrideTab)}
                    className={`rounded-md px-3 py-1.5 transition-colors ${overrideTab === value ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}`}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {overrideTab === 'params' ? (
                <>
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
                </>
              ) : (
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
              )}
            </DetailPanel>
          )}

          {activeNode === 'request' && (
            <DetailPanel icon={<Puzzle className="w-4 h-4" />} title="请求拦截" desc="发往上游前">
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
              <div>
                {/* 修改原因：System Prompt 已迁移到 Pipeline 的上游节点，旧高级设置区域不再保留此输入框。 */}
                {/* 修改方式：通过 onSystemPromptChange 受控更新 preferences.system_prompt，并安全读取 formData?.preferences?.system_prompt。 */}
                {/* 目的：让上游节点集中展示端点信息和上游请求相关的系统提示词配置。 */}
                <label className="text-xs font-medium text-foreground mb-2 block">系统提示词 (System Prompt)</label>
                <textarea
                  value={systemPrompt}
                  onChange={e => onSystemPromptChange?.(e.target.value)}
                  rows={4}
                  className="w-full bg-background border border-border px-3 py-2 rounded-lg text-xs text-foreground resize-y min-h-[88px] focus:border-primary outline-none"
                />
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

          {activeNode === 'keyrules' && (
            <DetailPanel icon={<Power className="w-4 h-4" />} title="规则" desc="Key 错误处理">
              <KeyRulesPanel rules={safeKeyRules} onKeyRulesChange={onKeyRulesChange} />
            </DetailPanel>
          )}

          {activeNode === 'ch_outbound' && (
            <DetailPanel icon={<PackageCheck className="w-4 h-4" />} title="渠道出站拦截" desc="规则之后 · Key 出站之前">
              {/* 修改原因：后端新增 channel_outbound_interceptors 后，渠道出站节点应展示和编辑对应插件。 */}
              {/* 修改方式：复用 PluginCard 和 PluginAddDropdown，stage 传 channel_outbound。 */}
              {/* 目的：让渠道级最终响应处理可以在 Pipeline 内直接管理。 */}
              {channelOutboundPlugins.length === 0 ? (
                <p className="text-xs text-muted-foreground italic">未启用任何渠道出站拦截器</p>
              ) : (
                <div className="space-y-2 mb-3">
                  {channelOutboundPlugins.map((p) => (
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
                stage="channel_outbound"
                allPlugins={allPlugins}
                enabledPluginNames={enabledPluginNames}
                openMenu={openAddMenu}
                setOpenMenu={setOpenAddMenu}
                onAdd={addPlugin}
                onOpenPluginSheet={onOpenPluginSheet}
              />
            </DetailPanel>
          )}

          {activeNode === 'key_outbound' && (
            <DetailPanel icon={<ShieldCheck className="w-4 h-4" />} title="Key 出站拦截" desc="最终返回客户端前">
              {/* 修改原因：新增 key_outbound 阶段后，Key 级出站插件需要独立于渠道出站展示。 */}
              {/* 修改方式：新增 key_outbound 详情面板，复用现有 PluginCard 和添加菜单。 */}
              {/* 目的：让按下游 API Key 配置的最终响应处理有清晰入口。 */}
              {keyOutboundPlugins.length === 0 ? (
                <p className="text-xs text-muted-foreground italic">未启用任何 Key 出站拦截器</p>
              ) : (
                <div className="space-y-2 mb-3">
                  {keyOutboundPlugins.map((p) => (
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
                stage="key_outbound"
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
