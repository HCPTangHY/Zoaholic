export const TIMEOUT_KEYS = ['stream_header_timeout', 'model_timeout', 'keepalive_interval'] as const;
export type TimeoutKey = typeof TIMEOUT_KEYS[number];
export type TimeoutValue = number | Record<string, number>;
export type TimeoutDefaults = Record<TimeoutKey, number>;
export type TimeoutMode = 'inherit' | 'value' | 'off';
export interface TimeoutRow { name: string; seconds: string; mode: 'value' | 'off' }
export interface TimeoutDraft { mode: TimeoutMode; seconds: string; rows: TimeoutRow[]; loadError?: string }
export const CODE_TIMEOUT_DEFAULTS: TimeoutDefaults = { model_timeout: 600, stream_header_timeout: 60, keepalive_interval: 15 };

export function readTimeoutValue(preferences: Record<string, unknown>, key: TimeoutKey): unknown {
  if (key === 'stream_header_timeout') return preferences[key] ?? preferences.header_timeout;
  return preferences[key];
}

export function timeoutDraft(value: unknown, key: TimeoutKey): TimeoutDraft {
  const draft: TimeoutDraft = { mode: 'inherit', seconds: '', rows: [] };
  if (value === undefined || value === null) return draft;
  const modeFor = (item: unknown): 'value' | 'off' => typeof item === 'number' && item <= 0 && key !== 'model_timeout' ? 'off' : 'value';
  if (typeof value === 'number') return { ...draft, mode: modeFor(value), seconds: String(value) };
  if (typeof value !== 'object' || Array.isArray(value)) return { ...draft, loadError: '已有配置不是秒数或模型规则，请清除后重新填写。' };
  for (const [name, seconds] of Object.entries(value)) {
    if (typeof seconds !== 'number') draft.loadError = '已有规则含非数值，请修正或清除后重新填写。';
    if (name === 'default') { draft.mode = modeFor(seconds); draft.seconds = String(seconds); }
    else draft.rows.push({ name, seconds: String(seconds), mode: modeFor(seconds) });
  }
  return draft;
}

function secondsError(input: string, key: TimeoutKey): string | undefined {
  if (!input.trim()) return '请输入秒数';
  const value = Number(input);
  if (!Number.isFinite(value)) return '请输入有限数值';
  if (key === 'model_timeout' ? value <= 0 : value < 0) return key === 'model_timeout' ? '读取超时必须大于 0' : '秒数不能小于 0';
  if (key === 'keepalive_interval' && !Number.isInteger(value)) return '心跳间隔必须是整数秒';
}

export function validateTimeoutDraft(draft: TimeoutDraft, key: TimeoutKey): { defaultError?: string; rowErrors: string[]; valid: boolean } {
  let defaultError = draft.loadError;
  if (draft.mode === 'value') defaultError = secondsError(draft.seconds, key) || defaultError;
  if (draft.mode === 'off' && key === 'model_timeout') defaultError = '读取超时不能关闭';
  const counts = new Map<string, number>();
  for (const row of draft.rows) { const name = row.name.trim().toLowerCase(); counts.set(name, (counts.get(name) || 0) + 1); }
  const rowErrors = draft.rows.map(row => {
    const name = row.name.trim().toLowerCase();
    if (!name) return '请输入模型名或匹配片段';
    if (name === 'default') return 'default 请在默认设置中编辑';
    if ((counts.get(name) || 0) > 1) return '模型规则重复（不区分大小写）';
    if (row.mode === 'off') return key === 'model_timeout' ? '读取超时不能关闭' : '';
    return secondsError(row.seconds, key) || '';
  });
  return { defaultError, rowErrors, valid: !defaultError && !rowErrors.some(Boolean) };
}

export function serializeTimeoutDraft(draft: TimeoutDraft, key: TimeoutKey): TimeoutValue | undefined {
  if (!validateTimeoutDraft(draft, key).valid) throw new Error('超时配置尚未填写完整');
  const seconds = draft.mode === 'off' ? 0 : draft.mode === 'value' ? Number(draft.seconds) : undefined;
  if (!draft.rows.length) return seconds;
  return Object.fromEntries([
    ...(seconds === undefined ? [] : [['default', seconds] as [string, number]]),
    ...draft.rows.map(row => [row.name.trim(), row.mode === 'off' ? 0 : Number(row.seconds)] as [string, number]),
  ]);
}

export function writeTimeoutValue<T extends Record<string, unknown>>(preferences: T, key: TimeoutKey, value: TimeoutValue | undefined): T {
  const next = { ...preferences };
  if (value === undefined) delete next[key]; else (next as Record<string, unknown>)[key] = value;
  if (key === 'stream_header_timeout') delete next.header_timeout;
  return next;
}

function table(value: unknown): Record<string, number> {
  if (typeof value === 'number') return { default: value };
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) return value as Record<string, number>;
  return {};
}
function match(table: Record<string, number>, model: string): { value: number; rule: string } | undefined {
  const lower = model.toLowerCase();
  if (Object.prototype.hasOwnProperty.call(table, lower)) return { value: table[lower], rule: lower };
  for (const [rule, value] of Object.entries(table)) {
    if (rule !== 'default' && lower.includes(rule.toLowerCase())) return { value, rule };
  }
  if (Object.prototype.hasOwnProperty.call(table, 'default')) return { value: table.default, rule: 'default' };
}

/** Mirrors get_preference's channel request → channel upstream → global upstream order. */
export function resolveTimeoutPreview(key: TimeoutKey, preferences: Record<string, unknown>, globalPreferences: Record<string, unknown>, defaults: TimeoutDefaults, scope: 'global' | 'channel', requestModel: string, upstreamModel = requestModel, parentPreferences: Record<string, unknown> = {}) {
  const global = { default: defaults[key], ...table(readTimeoutValue(scope === 'global' ? preferences : globalPreferences, key)) };
  if (scope === 'channel') {
    // Subchannels are expanded with a shallow preferences merge on the server.
    const merged = { ...parentPreferences, ...preferences };
    const local = table(readTimeoutValue(merged, key));
    // safe_get in get_preference substitutes the global table when the local table is empty.
    // That fallback tries the requested name first, including global.default before upstream.
    if (!Object.keys(local).length) {
      const globalMatch = match(global, requestModel)!;
      const rawGlobal = table(readTimeoutValue(globalPreferences, key));
      return { ...globalMatch, source: Object.prototype.hasOwnProperty.call(rawGlobal, globalMatch.rule) ? 'global' as const : 'default' as const };
    }
    const localMatch = match(local, requestModel) ?? match(local, upstreamModel);
    const activeField = key === 'stream_header_timeout' && merged[key] == null ? 'header_timeout' : key;
    const own = preferences[activeField] != null;
    if (localMatch) return { ...localMatch, source: own ? 'channel' as const : 'parent' as const };
  }
  const globalMatch = match(global, scope === 'global' ? requestModel : upstreamModel)!;
  const rawGlobal = table(readTimeoutValue(scope === 'global' ? preferences : globalPreferences, key));
  return { ...globalMatch, source: Object.prototype.hasOwnProperty.call(rawGlobal, globalMatch.rule) ? 'global' as const : 'default' as const };
}
