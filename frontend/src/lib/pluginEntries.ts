export type EnabledPluginValue =
  | string
  | {
      name?: string;
      plugin?: string;
      plugin_name?: string;
      options?: string;
      params?: Record<string, unknown>;
    }
  | Record<string, unknown>;

export interface ParsedEnabledPlugin {
  name: string;
  opts?: string;
  hasOpts: boolean;
}

function stringifyParamValue(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (Array.isArray(value)) return value.map(item => String(item)).join('\n');
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${key}: ${Array.isArray(item) ? item.map(part => String(part)).join('|') : String(item ?? '')}`)
      .join('\n');
  }
  return String(value);
}

const KEY_GUARD_PARAM_KEYS = [
  'allowed_ua',
  'ua',
  'header_allow',
  'header_deny',
  'system_allow',
  'system_deny',
  'tools_policy',
  'tool_allow',
  'allowed_tools',
  'tool_deny',
  'denied_tools',
  'tool_choice_policy',
  'strip_tools',
  'tools',
  'reject_status',
  'reject_message',
];

const KEY_GUARD_LIST_KEYS = new Set(['allowed_ua', 'ua', 'system_allow', 'system_deny', 'tool_allow', 'allowed_tools', 'tool_deny', 'denied_tools']);
const KEY_GUARD_BOOL_KEYS = new Set(['strip_tools', 'tools']);
const KEY_GUARD_NUMBER_KEYS = new Set(['reject_status']);

function singleKeyObject(value: Record<string, unknown>): { name: string; params: unknown } | null {
  const keys = Object.keys(value);
  if (keys.length !== 1) return null;
  const name = keys[0];
  if (!name || ['name', 'plugin', 'plugin_name', 'options', 'params'].includes(name)) return null;
  return { name, params: value[name] };
}

export function paramsObjectToOptions(pluginName: string, params: unknown): string {
  if (!params || typeof params !== 'object') return stringifyParamValue(params);
  const obj = params as Record<string, unknown>;

  if (pluginName === 'key_guard') {
    const parts: string[] = [];
    const used = new Set<string>();
    const appendParam = (key: string, value: unknown) => {
      if (value === undefined || value === null) return;
      const text = stringifyParamValue(value);
      if (text) parts.push(`${key}=${text}`);
      used.add(key);
    };

    appendParam('allowed_ua', obj.allowed_ua ?? obj.ua);
    appendParam('header_allow', obj.header_allow);
    appendParam('header_deny', obj.header_deny);
    appendParam('system_allow', obj.system_allow);
    appendParam('system_deny', obj.system_deny);
    appendParam('tools_policy', obj.tools_policy);
    appendParam('tool_allow', obj.tool_allow ?? obj.allowed_tools);
    appendParam('tool_deny', obj.tool_deny ?? obj.denied_tools);
    appendParam('tool_choice_policy', obj.tool_choice_policy);
    appendParam('strip_tools', obj.strip_tools ?? obj.tools);
    appendParam('reject_status', obj.reject_status);
    appendParam('reject_message', obj.reject_message);

    Object.entries(obj).forEach(([key, value]) => {
      if (used.has(key) || KEY_GUARD_PARAM_KEYS.includes(key)) return;
      appendParam(key, value);
    });
    return parts.join(',');
  }

  return Object.entries(obj)
    .map(([key, value]) => {
      const text = stringifyParamValue(value);
      return text ? `${key}=${text}` : '';
    })
    .filter(Boolean)
    .join(',');
}

export function parseEnabledPluginValue(value: EnabledPluginValue): ParsedEnabledPlugin {
  if (typeof value === 'string') {
    const colonIndex = value.indexOf(':');
    if (colonIndex < 0) return { name: value, opts: undefined, hasOpts: false };
    const opts = value.slice(colonIndex + 1);
    return { name: value.slice(0, colonIndex), opts, hasOpts: true };
  }

  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    const directName = obj.name ?? obj.plugin ?? obj.plugin_name;
    if (typeof directName === 'string' && directName.trim()) {
      const name = directName.trim();
      if (typeof obj.options === 'string') {
        return { name, opts: obj.options, hasOpts: Boolean(obj.options) };
      }
      if ('params' in obj) {
        const opts = paramsObjectToOptions(name, obj.params);
        return { name, opts: opts || undefined, hasOpts: Boolean(opts) };
      }
      return { name, opts: undefined, hasOpts: false };
    }

    const single = singleKeyObject(obj);
    if (single) {
      const opts = paramsObjectToOptions(single.name, single.params);
      return { name: single.name, opts: opts || undefined, hasOpts: Boolean(opts) };
    }
  }

  return { name: '', opts: undefined, hasOpts: false };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function parseKeyGuardOptions(options: string): Record<string, string> {
  // 修改原因：key_guard 参数已经扩展到多个 textarea/select/toggle，值里可能出现逗号。
  // 修改方式：按已知 key 的边界解析 key=value，而不是直接用逗号切分。
  // 目的：保存结构化 params 时保留所有反滥用规则字段。
  const result: Record<string, string> = {};
  const matches: Array<{ key: string; boundaryStart: number; valueStart: number }> = [];

  for (const key of KEY_GUARD_PARAM_KEYS) {
    const re = new RegExp(`(^|,)${escapeRegExp(key)}=`, 'g');
    let match: RegExpExecArray | null;
    while ((match = re.exec(options)) !== null) {
      const prefix = match[1] || '';
      const keyStart = match.index + prefix.length;
      matches.push({ key, boundaryStart: match.index, valueStart: keyStart + key.length + 1 });
    }
  }

  matches.sort((a, b) => a.boundaryStart - b.boundaryStart);
  matches.forEach((match, index) => {
    const canonicalKey = match.key === 'ua' ? 'allowed_ua'
      : match.key === 'tools' ? 'strip_tools'
      : match.key === 'allowed_tools' ? 'tool_allow'
      : match.key === 'denied_tools' ? 'tool_deny'
      : match.key;
    const valueEnd = index + 1 < matches.length ? matches[index + 1].boundaryStart : options.length;
    result[canonicalKey] = options.slice(match.valueStart, valueEnd).trim();
  });

  for (const rawPart of options.split(',')) {
    const token = rawPart.trim();
    if (!token || token.includes('=')) continue;
    if (token.startsWith('ua:')) {
      const current = result.allowed_ua ? `${result.allowed_ua}\n` : '';
      result.allowed_ua = `${current}${token.slice(3).trim()}`.trim();
    } else if (token === 'no_tools') {
      result.strip_tools = 'false';
    } else if (token === 'strip_tools') {
      result.strip_tools = 'true';
    } else if (['allow', 'strip', 'deny', 'require'].includes(token)) {
      result.tools_policy = token;
    }
  }

  return result;
}

function parseBooleanText(value: string): boolean {
  return ['1', 'true', 'yes', 'on', 'strip_tools'].includes(String(value || '').trim().toLowerCase());
}

function splitLines(value: string): string[] {
  return String(value || '')
    .split(/\r?\n/)
    .map(item => item.trim())
    .filter(Boolean);
}

export function buildEnabledPluginValue(name: string, opts: string): EnabledPluginValue {
  const pluginName = String(name || '').trim();
  const trimmedOpts = String(opts || '').trim();
  if (!pluginName) return '';
  if (!trimmedOpts) return pluginName;

  if (pluginName === 'key_guard') {
    const parsed = parseKeyGuardOptions(trimmedOpts);
    const params: Record<string, unknown> = {};

    Object.entries(parsed).forEach(([key, value]) => {
      const text = String(value ?? '').trim();
      if (!text) return;
      if (KEY_GUARD_LIST_KEYS.has(key)) {
        const canonicalKey = key === 'ua' ? 'allowed_ua'
          : key === 'allowed_tools' ? 'tool_allow'
          : key === 'denied_tools' ? 'tool_deny'
          : key;
        params[canonicalKey] = splitLines(text);
      } else if (KEY_GUARD_BOOL_KEYS.has(key)) {
        params[key === 'tools' ? 'strip_tools' : key] = parseBooleanText(text);
      } else if (KEY_GUARD_NUMBER_KEYS.has(key)) {
        const numeric = Number(text);
        if (Number.isFinite(numeric)) params[key] = numeric;
      } else {
        params[key] = text;
      }
    });

    return Object.keys(params).length > 0 ? { name: pluginName, params } : pluginName;
  }

  return `${pluginName}:${trimmedOpts}`;
}
