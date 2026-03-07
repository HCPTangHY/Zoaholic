import { useEffect, useMemo, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { X, Plus, Trash2, CheckCircle2 } from 'lucide-react';

type Mode = 'deny' | 'allow';

export interface FilterRule {
  modelKey: string; // 'all' 或具体模型名
  enabled: boolean;
  mode: Mode;
  use_defaults: boolean;
  deny: string[];
  allow: string[];
}

export interface ParameterFilterEditorDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;

  availableModels: string[];

  /** 当前 provider.preferences.post_body_parameter_filter（可以是 object/list/null） */
  initialConfig: any;

  onSave: (config: any) => void;
}

const COMMON_FIELDS = [
  'thinking',
  'min_p',
  'top_k',
  'include_usage',
  'stream_options.include_usage',
  'chat_template_kwargs',
  'response_format',
  'tool_choice',
  'tools',
  'parallel_tool_calls',
];

function asStringList(v: any): string[] {
  if (!v) return [];
  if (Array.isArray(v)) return v.map(x => String(x)).map(s => s.trim()).filter(Boolean);
  if (typeof v === 'string') return [v.trim()].filter(Boolean);
  return [];
}

function normalizeRule(modelKey: string, raw: any): FilterRule {
  const obj = (raw && typeof raw === 'object') ? raw : {};
  const mode = (String(obj.mode || 'deny').toLowerCase() === 'allow' ? 'allow' : 'deny') as Mode;
  const use_defaults = obj.use_defaults === undefined ? true : Boolean(obj.use_defaults);
  const enabled = obj.enabled === false ? false : true;

  return {
    modelKey,
    enabled,
    mode,
    use_defaults,
    deny: asStringList(obj.deny),
    allow: asStringList(obj.allow),
  };
}

function parseConfigToRules(cfg: any): { globalRule: FilterRule; modelRules: FilterRule[] } {
  // list => global deny
  if (Array.isArray(cfg)) {
    return {
      globalRule: {
        modelKey: 'all',
        enabled: true,
        mode: 'deny',
        use_defaults: true,
        deny: asStringList(cfg),
        allow: [],
      },
      modelRules: [],
    };
  }

  if (!cfg || typeof cfg !== 'object') {
    return {
      globalRule: {
        modelKey: 'all',
        enabled: true,
        mode: 'deny',
        use_defaults: true,
        deny: [],
        allow: [],
      },
      modelRules: [],
    };
  }

  // 结构化 global
  const hasGlobalShape = ['deny', 'allow', 'mode', 'enabled', 'use_defaults'].some(k => Object.prototype.hasOwnProperty.call(cfg, k));
  if (hasGlobalShape) {
    return {
      globalRule: normalizeRule('all', cfg),
      modelRules: [],
    };
  }

  // all/* + per-model
  const globalRaw = (cfg.all ?? cfg['*']) ?? {};
  const globalRule = normalizeRule('all', globalRaw);

  const modelRules: FilterRule[] = [];
  for (const [k, v] of Object.entries(cfg)) {
    if (k === 'all' || k === '*') continue;
    if (!v || typeof v !== 'object') continue;
    modelRules.push(normalizeRule(k, v));
  }

  // 稳定排序
  modelRules.sort((a, b) => a.modelKey.localeCompare(b.modelKey));

  return { globalRule, modelRules };
}

function buildConfigFromRules(globalRule: FilterRule, modelRules: FilterRule[]): any {
  const compactRule = (r: FilterRule) => {
    const out: any = {
      mode: r.mode,
      use_defaults: Boolean(r.use_defaults),
    };
    if (r.enabled === false) out.enabled = false;
    if (r.deny.length) out.deny = r.deny;
    if (r.allow.length) out.allow = r.allow;
    return out;
  };

  const out: any = {};

  // 始终写入 all（用户更容易理解结构）
  out.all = compactRule(globalRule);

  for (const r of modelRules) {
    const key = String(r.modelKey || '').trim();
    if (!key) continue;
    out[key] = compactRule(r);
  }

  return out;
}

function FieldEditor({
  title,
  values,
  onChange,
}: {
  title: string;
  values: string[];
  onChange: (next: string[]) => void;
}) {
  const [input, setInput] = useState('');

  const addOne = (v: string) => {
    const s = String(v || '').trim();
    if (!s) return;
    onChange(Array.from(new Set([...values, s])));
    setInput('');
  };

  const removeOne = (v: string) => {
    onChange(values.filter(x => x !== v));
  };

  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground">{title}</div>

      <div className="flex flex-wrap gap-2">
        {values.map(v => (
          <span key={v} className="bg-muted border border-border text-foreground text-xs font-mono px-2 py-1 rounded flex items-center gap-1">
            {v}
            <button className="text-muted-foreground hover:text-red-500" onClick={() => removeOne(v)} title="移除">
              <X className="w-3 h-3" />
            </button>
          </span>
        ))}
        {values.length === 0 && <span className="text-xs text-muted-foreground italic">(空)</span>}
      </div>

      <div className="flex flex-col sm:flex-row gap-2">
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="输入字段名（支持 dot-path，例如 stream_options.include_usage）"
          className="flex-1 bg-background border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground"
        />
        <button
          type="button"
          onClick={() => addOne(input)}
          className="px-3 py-2 rounded-lg text-sm bg-primary/10 text-primary hover:bg-primary/20"
        >
          <Plus className="w-4 h-4 inline-block mr-1" /> 添加
        </button>
      </div>

      <div className="flex flex-wrap gap-2">
        {COMMON_FIELDS.map(f => (
          <button
            key={f}
            type="button"
            onClick={() => addOne(f)}
            className="text-xs font-mono px-2 py-1 rounded bg-muted hover:bg-muted/80 border border-border text-foreground"
            title="点击添加"
          >
            {f}
          </button>
        ))}
      </div>
    </div>
  );
}

function RuleCard({
  rule,
  availableModels,
  onChange,
  onDelete,
}: {
  rule: FilterRule;
  availableModels: string[];
  onChange: (next: FilterRule) => void;
  onDelete?: () => void;
}) {
  return (
    <div className="border border-border rounded-xl p-4 bg-card space-y-3">
      <div className="flex flex-col sm:flex-row gap-3 sm:items-center sm:justify-between">
        <div className="flex-1">
          <label className="text-xs text-muted-foreground block mb-1">模型</label>
          {rule.modelKey === 'all' ? (
            <input
              value={rule.modelKey}
              disabled
              className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground opacity-80"
              title="全局规则固定为 all"
            />
          ) : (
            <div className="flex flex-col sm:flex-row gap-2">
              <select
                value={availableModels.includes(rule.modelKey) ? rule.modelKey : ''}
                onChange={e => onChange({ ...rule, modelKey: e.target.value })}
                className="sm:w-64 w-full bg-background border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground"
                title="快速选择渠道中已启用的模型"
              >
                <option value="">(请选择模型)</option>
                {availableModels.map(m => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
              <input
                value={rule.modelKey}
                onChange={e => onChange({ ...rule, modelKey: e.target.value })}
                placeholder="也可手动输入自定义模型名"
                className="flex-1 bg-background border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground"
              />
            </div>
          )}
        </div>

        <div className="flex items-center gap-3">
          <label className="text-sm inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={rule.enabled}
              onChange={e => onChange({ ...rule, enabled: e.target.checked })}
            />
            <span>启用</span>
          </label>

          {onDelete && (
            <button
              type="button"
              onClick={onDelete}
              className="text-red-600 dark:text-red-400 hover:text-red-500"
              title="删除该模型规则"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-muted-foreground block mb-1">模式</label>
          <select
            value={rule.mode}
            onChange={e => onChange({ ...rule, mode: (e.target.value === 'allow' ? 'allow' : 'deny') as Mode })}
            className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground"
          >
            <option value="deny">deny（移除这些字段）</option>
            <option value="allow">allow（只保留这些字段 + 必要字段）</option>
          </select>
        </div>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">use_defaults</label>
          <select
            value={rule.use_defaults ? 'true' : 'false'}
            onChange={e => onChange({ ...rule, use_defaults: e.target.value === 'true' })}
            className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground"
          >
            <option value="true">true（叠加内置默认过滤）</option>
            <option value="false">false（仅使用自定义规则）</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <FieldEditor title="deny" values={rule.deny} onChange={deny => onChange({ ...rule, deny })} />
        <FieldEditor title="allow" values={rule.allow} onChange={allow => onChange({ ...rule, allow })} />
      </div>

      <p className="text-xs text-muted-foreground">
        提示：配置键会同时匹配 <span className="font-mono">model</span>（别名）与上游原始模型名（original_model）。
      </p>
    </div>
  );
}

export function ParameterFilterEditorDialog({
  open,
  onOpenChange,
  availableModels,
  initialConfig,
  onSave,
}: ParameterFilterEditorDialogProps) {
  const [globalRule, setGlobalRule] = useState<FilterRule>(() => normalizeRule('all', {}));
  const [modelRules, setModelRules] = useState<FilterRule[]>([]);

  const modelOptions = useMemo(() => {
    const set = new Set<string>();
    (availableModels || []).forEach(m => {
      const s = String(m || '').trim();
      if (s) set.add(s);
    });
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [availableModels]);

  useEffect(() => {
    if (!open) return;
    const { globalRule, modelRules } = parseConfigToRules(initialConfig);
    setGlobalRule(globalRule);
    setModelRules(modelRules);
  }, [open]);

  const addModelRule = () => {
    setModelRules(prev => {
      const next = [...prev];
      next.push({
        modelKey: '',
        enabled: true,
        mode: 'deny',
        use_defaults: true,
        deny: [],
        allow: [],
      });
      return next;
    });
  };

  const updateModelRule = (idx: number, nextRule: FilterRule) => {
    setModelRules(prev => {
      const next = [...prev];
      next[idx] = nextRule;
      return next;
    });
  };

  const deleteModelRule = (idx: number) => {
    setModelRules(prev => prev.filter((_, i) => i !== idx));
  };

  const handleSave = () => {
    // 基础校验：modelKey 不能为空
    for (const r of modelRules) {
      if (!String(r.modelKey || '').trim()) {
        alert('存在空的模型名规则，请填写或删除。');
        return;
      }
    }

    const config = buildConfigFromRules(globalRule, modelRules);
    onSave(config);
    onOpenChange(false);
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-[90] animate-in fade-in duration-200" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[980px] max-w-[96vw] max-h-[92vh] bg-background border border-border rounded-xl shadow-2xl z-[100] flex flex-col">
          <div className="p-5 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
            <Dialog.Title className="text-lg font-bold text-foreground">请求体参数过滤 · 高级编辑器</Dialog.Title>
            <Dialog.Close className="text-muted-foreground hover:text-foreground">
              <X className="w-5 h-5" />
            </Dialog.Close>
          </div>

          <div className="flex-1 overflow-y-auto p-5 space-y-6">
            <section className="space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-foreground">全局规则（all）</h3>
                <span className="text-xs text-muted-foreground">对所有模型生效（可被单模型规则叠加）</span>
              </div>
              <RuleCard rule={globalRule} availableModels={modelOptions} onChange={setGlobalRule} />
            </section>

            <section className="space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-foreground">按模型规则</h3>
                <button
                  type="button"
                  onClick={addModelRule}
                  className="text-sm bg-primary/10 text-primary hover:bg-primary/20 px-3 py-2 rounded-lg"
                >
                  <Plus className="w-4 h-4 inline-block mr-1" /> 新增规则
                </button>
              </div>

              {modelRules.length === 0 ? (
                <div className="text-sm text-muted-foreground italic p-4 border border-dashed border-border rounded-lg text-center">
                  暂无按模型规则，点击右上角“新增规则”。
                </div>
              ) : (
                <div className="space-y-4">
                  {modelRules.map((r, idx) => (
                    <RuleCard
                      key={`${idx}-${r.modelKey}`}
                      rule={r}
                      availableModels={modelOptions}
                      onChange={next => updateModelRule(idx, next)}
                      onDelete={() => deleteModelRule(idx)}
                    />
                  ))}
                </div>
              )}
            </section>

            <section className="text-xs text-muted-foreground space-y-1">
              <div>字段名支持 dot-path（例如 <span className="font-mono">stream_options.include_usage</span>）。</div>
              <div>输出配置会写入 <span className="font-mono">provider.preferences.post_body_parameter_filter</span>。</div>
            </section>
          </div>

          <div className="p-4 bg-muted/30 border-t border-border flex justify-end gap-3 flex-shrink-0">
            <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
            <button
              onClick={handleSave}
              className="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary hover:bg-primary/90 rounded-lg flex items-center gap-2"
            >
              <CheckCircle2 className="w-4 h-4" /> 保存规则
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
