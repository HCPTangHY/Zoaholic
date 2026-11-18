import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import {
  CODE_TIMEOUT_DEFAULTS, TIMEOUT_KEYS, readTimeoutValue, serializeTimeoutDraft,
  timeoutDraft, validateTimeoutDraft, writeTimeoutValue, resolveTimeoutPreview,
  type TimeoutDefaults, type TimeoutDraft, type TimeoutKey, type TimeoutMode,
} from '../lib/timeoutSettings';

export interface TimeoutSettingsProps {
  preferences: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  scope: 'global' | 'channel';
  globalPreferences?: Record<string, unknown>;
  parentPreferences?: Record<string, unknown>;
  defaults?: TimeoutDefaults;
  modelSuggestions?: string[];
  onValidityChange?: (valid: boolean) => void;
  inheritanceLabel?: string;
}

const fieldInfo: Record<TimeoutKey, { title: string; off: string }> = {
  stream_header_timeout: {
    title: '首包等待上限',
    off: '关闭首包限制',
  },
  model_timeout: {
    title: '网络读取超时',
    off: '',
  },
  keepalive_interval: {
    title: 'SSE 心跳间隔',
    off: '关闭心跳',
  },
};
const inputClass = 'min-w-0 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-primary';
const buttonClass = 'inline-flex items-center justify-center gap-2 rounded-lg border border-border px-3 py-2 text-sm transition-all duration-150 hover:bg-muted active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-50';

function TimeoutField({ setting, value, globalValue, defaultSeconds, scope, suggestions, inheritanceLabel, onUpdate, onValidity }: {
  setting: TimeoutKey; value: unknown; globalValue: unknown; defaultSeconds: number;
  scope: 'global' | 'channel'; suggestions: string[]; inheritanceLabel: string;
  onUpdate: (draft: TimeoutDraft) => void; onValidity: (key: TimeoutKey, valid: boolean) => void;
}) {
  const [draft, setDraft] = useState(() => timeoutDraft(value, setting));
  const observed = useRef(JSON.stringify(value));
  const id = useId();
  const info = fieldInfo[setting];
  const validation = validateTimeoutDraft(draft, setting);
  const signature = JSON.stringify(value);
  useEffect(() => {
    if (observed.current !== signature) {
      observed.current = signature;
      // External reloads must reset this field; controlled echoes retain its unfinished text.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setDraft(timeoutDraft(value, setting));
    }
  }, [signature, value, setting]);
  useEffect(() => { onValidity(setting, validation.valid); }, [setting, validation.valid, onValidity]);
  const edit = (next: TimeoutDraft) => {
    const updated = { ...next, loadError: undefined };
    setDraft(updated);
    const valid = validateTimeoutDraft(updated, setting).valid;
    onValidity(setting, valid);
    if (valid) {
      // Ignore the controlled-value echo: preserve in-progress text such as "1.".
      observed.current = JSON.stringify(serializeTimeoutDraft(updated, setting));
      onUpdate(updated);
    }
  };
  const setMode = (mode: TimeoutMode) => edit({ ...draft, mode, seconds: mode === 'value' && (!draft.seconds || Number(draft.seconds) <= 0) ? String(defaultSeconds) : draft.seconds });
  const inherited = timeoutDraft(globalValue, setting);
  const inheritedDefault = inherited.mode === 'off' ? 0 : inherited.mode === 'value' ? Number(inherited.seconds) : defaultSeconds;
  const defaultDescription = inheritedDefault <= 0 && setting !== 'model_timeout' ? fieldInfo[setting].off : `${inheritedDefault} 秒`;
  return <section className="space-y-3 rounded-xl border border-border bg-background/50 p-3.5">
    <div className="flex items-center justify-between gap-2">
      <h4 className="text-sm font-medium text-foreground">{info.title}</h4>
      {draft.mode !== 'value' && (
        <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
          {draft.mode === 'off' ? '已关闭 (0s)' : `默认 ${defaultDescription}`}
        </span>
      )}
    </div>
    <div className={draft.mode === 'value' ? 'grid grid-cols-1 gap-3 sm:grid-cols-2' : 'grid grid-cols-1'}>
      <label className="block text-xs font-medium text-muted-foreground">默认设置
        <select aria-label={`${info.title}默认设置`} className={`${inputClass} mt-1`} value={draft.mode} onChange={event => setMode(event.target.value as TimeoutMode)}>
          <option value="inherit">{scope === 'channel' ? inheritanceLabel : '使用服务默认'}</option>
          <option value="value">指定秒数</option>
          {setting !== 'model_timeout' && <option value="off">{info.off}</option>}
        </select>
      </label>
      {draft.mode === 'value' && <label className="block text-xs font-medium text-muted-foreground">秒数
        <input aria-label={`${info.title}默认秒数`} aria-invalid={Boolean(validation.defaultError)} type="number" step={setting === 'keepalive_interval' ? '1' : 'any'} min={setting === 'model_timeout' ? undefined : '0'} value={draft.seconds} onChange={event => edit({ ...draft, seconds: event.target.value })} className={`${inputClass} mt-1`} />
      </label>}
    </div>
    {validation.defaultError && <p role="alert" className="text-xs text-destructive">{validation.defaultError} <button type="button" className="rounded-lg px-2 py-1 underline underline-offset-2 transition-all duration-150 hover:bg-muted active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary" onClick={() => edit({ mode: 'inherit', seconds: '', rows: [] })}>清除此项配置</button></p>}
    <details open={draft.rows.length > 0 || undefined} className="border-t border-border pt-2.5">
      <summary className="cursor-pointer rounded-lg text-xs font-medium text-muted-foreground transition-all duration-150 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">按模型覆盖{draft.rows.length > 0 ? ` (${draft.rows.length})` : ''}</summary>
      <div className="mt-2.5 space-y-2.5">
        <datalist id={`${id}-models`}>{suggestions.map(model => <option key={model} value={model} />)}</datalist>
        {draft.rows.map((row, index) => <div key={index} className="space-y-2 rounded-lg bg-muted/40 p-2.5">
          <div className="flex items-center gap-2">
            <input aria-label={`${info.title}模型规则 ${index + 1}`} aria-invalid={Boolean(validation.rowErrors[index])} list={`${id}-models`} placeholder="模型名或子串（不含 *）" value={row.name} className={inputClass} onChange={event => edit({ ...draft, rows: draft.rows.map((item, i) => i === index ? { ...item, name: event.target.value } : item) })} />
            <button type="button" className={`${buttonClass} shrink-0 px-2`} aria-label={`删除${info.title}模型规则 ${index + 1}`} onClick={() => edit({ ...draft, rows: draft.rows.filter((_, i) => i !== index) })}><Trash2 className="h-4 w-4" aria-hidden="true" /></button>
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <select aria-label={`${info.title}模型规则 ${index + 1}方式`} className={inputClass} value={row.mode} onChange={event => edit({ ...draft, rows: draft.rows.map((item, i) => i === index ? { ...item, mode: event.target.value as 'value' | 'off', seconds: event.target.value === 'value' && (!item.seconds || Number(item.seconds) <= 0) ? String(defaultSeconds) : item.seconds } : item) })}>
              <option value="value">指定秒数</option>{setting !== 'model_timeout' && <option value="off">{info.off}</option>}
            </select>
            {row.mode === 'value' ? <input aria-label={`${info.title}模型规则 ${index + 1}秒数`} type="number" step={setting === 'keepalive_interval' ? '1' : 'any'} min={setting === 'model_timeout' ? undefined : '0'} placeholder="秒" className={inputClass} value={row.seconds} onChange={event => edit({ ...draft, rows: draft.rows.map((item, i) => i === index ? { ...item, seconds: event.target.value } : item) })} /> : <p className="self-center text-xs text-muted-foreground">0 秒</p>}
          </div>
          {validation.rowErrors[index] && <p role="alert" className="text-xs text-destructive">{validation.rowErrors[index]}</p>}
        </div>)}
        <button type="button" className={buttonClass} onClick={() => edit({ ...draft, rows: [...draft.rows, { name: '', mode: 'value', seconds: String(defaultSeconds) }] })}><Plus className="h-4 w-4" aria-hidden="true" />添加模型规则</button>
      </div>
    </details>
  </section>;
}

export function TimeoutSettings({ preferences, onChange, scope, globalPreferences = {}, parentPreferences = {}, defaults, modelSuggestions = [], onValidityChange, inheritanceLabel = '沿用全局配置' }: TimeoutSettingsProps) {
  const fallback = defaults ?? CODE_TIMEOUT_DEFAULTS;
  const [previewModel, setPreviewModel] = useState('');
  const [upstreamModel, setUpstreamModel] = useState('');
  const [validity, setValidity] = useState<Record<TimeoutKey, boolean>>({ stream_header_timeout: true, model_timeout: true, keepalive_interval: true });
  const updateValidity = useCallback((key: TimeoutKey, valid: boolean) => setValidity(previous => previous[key] === valid ? previous : { ...previous, [key]: valid }), []);
  const valid = TIMEOUT_KEYS.every(key => validity[key]);
  useEffect(() => { onValidityChange?.(valid); }, [valid, onValidityChange]);
  const suggestions = [...new Set(modelSuggestions)].filter(Boolean);
  const inheritedDefaultValue = (key: TimeoutKey) => {
    // A child model map replaces the parent map, including its default.
    if (readTimeoutValue(preferences, key) != null) return readTimeoutValue(globalPreferences, key);
    const parent = readTimeoutValue(parentPreferences, key);
    if (typeof parent === 'number') return parent;
    if (parent && typeof parent === 'object' && !Array.isArray(parent) && 'default' in parent) return parent.default;
    return readTimeoutValue(globalPreferences, key);
  };
  return <div className="space-y-3">
    {TIMEOUT_KEYS.map(key => <TimeoutField key={key} setting={key} value={readTimeoutValue(preferences, key)} globalValue={inheritedDefaultValue(key)} defaultSeconds={fallback[key]} scope={scope} suggestions={suggestions} inheritanceLabel={Object.keys(parentPreferences).length > 0 && readTimeoutValue(preferences, key) != null ? '沿用全局（本项已独立）' : inheritanceLabel} onValidity={updateValidity} onUpdate={draft => onChange(writeTimeoutValue(preferences, key, serializeTimeoutDraft(draft, key)))} />)}
    <details className="rounded-xl border border-border p-3.5">
      <summary className="cursor-pointer rounded-lg text-xs font-medium text-muted-foreground transition-all duration-150 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">生效值预览</summary>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="text-xs text-muted-foreground">请求模型名<input aria-label="预览请求模型名" value={previewModel} onChange={event => setPreviewModel(event.target.value)} className={`${inputClass} mt-1`} placeholder="客户端请求模型名" /></label>
        <label className="text-xs text-muted-foreground">上游模型名（可选）<input aria-label="预览上游模型名" value={upstreamModel} onChange={event => setUpstreamModel(event.target.value)} className={`${inputClass} mt-1`} placeholder="映射后的模型名" /></label>
      </div>
      {!valid ? <p className="mt-3 text-xs text-destructive">请先修正上方输入项。</p> : previewModel.trim() ? (() => {
        const result = Object.fromEntries(TIMEOUT_KEYS.map(key => [key, resolveTimeoutPreview(key, preferences, globalPreferences, fallback, scope, previewModel.trim(), upstreamModel.trim() || previewModel.trim(), parentPreferences)])) as Record<TimeoutKey, ReturnType<typeof resolveTimeoutPreview>>;
        const read = result.model_timeout.value;
        const header = result.stream_header_timeout.value;
        const heartbeat = result.keepalive_interval.value;
        const sources = { channel: '本渠道', parent: '主渠道', global: '全局', default: '默认' };
        return <div className="mt-3 space-y-2">
          <dl className="space-y-1.5 text-xs">{TIMEOUT_KEYS.map(key => <div key={key} className="flex flex-wrap justify-between gap-2"><dt>{fieldInfo[key].title}</dt><dd className="min-w-0 break-all text-muted-foreground">{result[key].value <= 0 && key !== 'model_timeout' ? '关闭' : `${result[key].value} 秒`} · {sources[result[key].source]} · {result[key].rule === 'default' ? '默认' : result[key].rule}</dd></div>)}</dl>
          {header > 0 && read > 0 && header > read && <p className="text-xs text-muted-foreground">首包上限受网络超时限制，实际按 {read} 秒执行。</p>}
          {heartbeat > 0 && read > 0 && heartbeat >= read && <p className="text-xs text-destructive">心跳间隔不小于网络超时，当前不会启用心跳。</p>}
        </div>;
      })() : null}
    </details>
  </div>;
}
