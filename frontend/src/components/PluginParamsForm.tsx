import { useEffect, useMemo, useState } from 'react';
import * as Switch from '@radix-ui/react-switch';

export interface ParamOption {
  value: string;
  label: string;
}

export interface ParamSchema {
  key: string;
  label: string;
  type: 'select' | 'text' | 'textarea' | 'number' | 'toggle' | 'multi-select';
  options?: ParamOption[];
  default?: unknown;
  placeholder?: string;
  min?: number;
  max?: number;
  visible_when?: Record<string, string>;
  serialize?: 'positional' | 'key_value';
}

interface PluginParamsFormProps {
  options: string;
  schema?: ParamSchema[];
  onChange: (options: string) => void;
  disabled?: boolean;
  paramsHint?: string;
  size?: 'compact' | 'normal';
}

const KEY_VALUE_DEFAULT_KEYS = new Set(['cache']);
const MULTI_VALUE_SEPARATOR = '|';

function cleanSchema(schema?: ParamSchema[]): ParamSchema[] {
  return Array.isArray(schema) ? schema.filter(item => item && typeof item.key === 'string' && item.key.trim()) : [];
}

function stringifyParamValue(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
}

function hasExplicitKeyValueMode(options: string, schema: ParamSchema[]): boolean {
  // 修改原因：历史 enabled_plugins 同时支持 positional 格式和 key=value 格式，不能只按逗号位置解析。
  // 修改方式：只要 options 中出现当前 schema key 的 “key=” 前缀，就判定为 key=value 模式。
  // 目的：保留 claude_tools:cache=1h 这类已有配置，并支持完整面板与 Pipeline 面板反向解析。
  const trimmed = options.trim();
  if (!trimmed) return false;
  return schema.some(param => trimmed.startsWith(`${param.key}=`) || trimmed.includes(`,${param.key}=`));
}

function shouldSerializeAsKeyValue(currentOptions: string, schema: ParamSchema[]): boolean {
  if (hasExplicitKeyValueMode(currentOptions, schema)) return true;
  // 修改原因：部分插件有多个可视化参数，但后端 options 字符串必须保留字段名，否则 positional 格式无法表达含逗号的列表或布尔开关。
  // 修改方式：允许 params_schema 用 serialize=key_value 显式声明 key=value 序列化。
  // 目的：key_guard 等插件可以展示多个控件，同时不破坏既有单字符串 options 机制。
  if (schema.some(param => param.serialize === 'key_value')) return true;
  // 修改原因：metadata.params_schema 当前没有显式声明序列化格式，但 claude_tools 的 cache 参数必须保留 key=value。
  // 修改方式：对已知需要显示参数名的单参数 key 使用 key=value；其他单参数继续使用 positional 值。
  // 目的：兼容 cache=1h，同时不破坏 retries、mode、message 等既有短格式。
  return schema.length === 1 && KEY_VALUE_DEFAULT_KEYS.has(schema[0].key);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function parseKeyValueOptionsBySchema(options: string, schema: ParamSchema[]): Record<string, string> {
  const result: Record<string, string> = {};
  const keys = schema.map(param => param.key).filter(Boolean);
  const matches: Array<{ key: string; boundaryStart: number; valueStart: number }> = [];

  for (const key of keys) {
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
    const valueEnd = index + 1 < matches.length ? matches[index + 1].boundaryStart : options.length;
    result[match.key] = options.slice(match.valueStart, valueEnd).trim();
  });
  return result;
}

function valueWithDefaults(values: Record<string, string>, schema: ParamSchema[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const param of schema) {
    result[param.key] = values[param.key] ?? stringifyParamValue(param.default);
  }
  return result;
}

function visibleParams(schema: ParamSchema[], values: Record<string, string>): ParamSchema[] {
  return schema.filter(param => {
    if (!param.visible_when) return true;
    return Object.entries(param.visible_when).every(([key, expected]) => (values[key] ?? '') === String(expected));
  });
}

export function parsePluginOptions(options: string, schemaInput: ParamSchema[]): Record<string, string> {
  const schema = cleanSchema(schemaInput);
  const trimmed = (options || '').trim();
  const result: Record<string, string> = {};
  if (!trimmed || schema.length === 0) return result;

  if (hasExplicitKeyValueMode(trimmed, schema)) {
    if (schema.length === 1) {
      const keyPrefix = `${schema[0].key}=`;
      result[schema[0].key] = trimmed.startsWith(keyPrefix) ? trimmed.slice(keyPrefix.length) : trimmed;
      return result;
    }

    return parseKeyValueOptionsBySchema(trimmed, schema);
  }

  if (schema.some(param => param.key === 'allowed_ua') && schema.some(param => param.key === 'strip_tools')) {
    // 修改原因：key_guard 旧配置使用 ua:xxx,no_tools 这类 token 语法，升级为多控件后仍需正确回显旧值。
    // 修改方式：识别 ua:、no_tools、strip_tools token，映射到 allowed_ua 与 strip_tools 字段。
    // 目的：旧配置打开后可以自然迁移为 key=value 多参数格式，而不是被 positional 解析错位。
    const allowedUa: string[] = [];
    for (const part of trimmed.split(',')) {
      const token = part.trim();
      if (!token) continue;
      if (token.startsWith('ua:')) {
        const keyword = token.slice(3).trim();
        if (keyword) allowedUa.push(keyword);
      } else if (token === 'no_tools') {
        result.strip_tools = 'false';
      } else if (token === 'strip_tools') {
        result.strip_tools = 'true';
      }
    }
    if (allowedUa.length > 0) result.allowed_ua = allowedUa.join(MULTI_VALUE_SEPARATOR);
    if (allowedUa.length > 0 || result.strip_tools !== undefined) return result;
  }

  // 修改原因：单参数插件的 options 可能是完整自由文本，文本中可能包含逗号，不能按逗号拆分。
  // 修改方式：schema 只有一个参数时把整个 options 作为该参数值；多个参数才按 positional 顺序拆分。
  // 目的：兼容 error_mask:自定义提示语 这类自由文本参数。
  if (schema.length === 1) {
    result[schema[0].key] = trimmed;
    return result;
  }

  const positionalParts = trimmed.split(',').map(value => value.trim());
  if (positionalParts.length === 1) {
    const firstParam = schema[0];
    const firstOptions = firstParam.options || [];
    const matchesFirstSelect = firstParam.type !== 'select' || firstOptions.some(option => option.value === positionalParts[0]);
    const fallbackTextParam = schema.find(param => {
      if (param.type !== 'text' || !param.visible_when) return false;
      return Object.entries(param.visible_when).some(([key, expected]) => key === firstParam.key && expected === stringifyParamValue(firstParam.default));
    });

    if (!matchesFirstSelect && fallbackTextParam) {
      // 修改原因：image_filter 等旧插件允许直接写 “自定义占位文本”，但新 schema 第一项是 mode select。
      // 修改方式：当单个 positional 值不属于第一个 select 的选项，并且存在依赖默认 mode 的 text 参数时，把该值映射到 text 参数。
      // 目的：让旧配置在可视化表单里正确显示为自定义文本，而不是错误地落到 select 字段。
      result[firstParam.key] = stringifyParamValue(firstParam.default);
      result[fallbackTextParam.key] = positionalParts[0];
      return result;
    }
  }

  positionalParts.forEach((value, index) => {
    const param = schema[index];
    if (param) result[param.key] = value;
  });
  return result;
}

export function serializePluginOptions(values: Record<string, string>, schemaInput: ParamSchema[], currentOptions = ''): string {
  const schema = cleanSchema(schemaInput);
  if (schema.length === 0) return '';

  const defaultsForVisibility = valueWithDefaults(values, schema);
  const activeParams = visibleParams(schema, defaultsForVisibility);
  const readValue = (param: ParamSchema) => (values[param.key] ?? '').trim();

  if (shouldSerializeAsKeyValue(currentOptions, schema)) {
    const parts = activeParams
      .map(param => {
        const value = readValue(param);
        return value ? `${param.key}=${value}` : '';
      })
      .filter(Boolean);
    return parts.join(',');
  }

  if (schema.length === 1) {
    const only = activeParams[0];
    return only ? readValue(only) : '';
  }

  return activeParams.map(readValue).filter(Boolean).join(',');
}

function splitMultiValue(value: string): string[] {
  if (!value) return [];
  return value.includes(MULTI_VALUE_SEPARATOR)
    ? value.split(MULTI_VALUE_SEPARATOR).map(item => item.trim()).filter(Boolean)
    : value.split(',').map(item => item.trim()).filter(Boolean);
}

export function PluginParamsForm({ options, schema: schemaInput, onChange, disabled = false, paramsHint, size = 'normal' }: PluginParamsFormProps) {
  const schema = useMemo(() => cleanSchema(schemaInput), [schemaInput]);
  const parsedValues = useMemo(() => parsePluginOptions(options || '', schema), [options, schema]);
  const [values, setValues] = useState<Record<string, string>>(parsedValues);
  const compact = size === 'compact';

  useEffect(() => {
    // 修改原因：同一插件参数可能在 Pipeline 面板、完整配置面板或外部保存后更新。
    // 修改方式：当 options 或 schema 变化时重新解析并同步本地表单值。
    // 目的：避免可视化控件显示旧值。
    setValues(parsedValues);
  }, [parsedValues]);

  if (schema.length === 0) {
    return (
      <input
        type="text"
        value={options || ''}
        onChange={event => onChange(event.target.value)}
        disabled={disabled}
        placeholder={paramsHint || '留空使用默认值'}
        className={compact
          ? 'h-6 w-full rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-mono text-primary outline-none ring-1 ring-transparent placeholder:text-primary/50 focus:ring-primary/40 disabled:opacity-50'
          : 'w-full bg-background border border-border text-foreground focus:border-emerald-500 px-3 py-2 rounded-md text-sm font-mono disabled:opacity-50 outline-none'
        }
      />
    );
  }

  const displayValues = valueWithDefaults(values, schema);
  const shownParams = visibleParams(schema, displayValues);

  const commitValues = (nextValues: Record<string, string>) => {
    onChange(serializePluginOptions(nextValues, schema, options || ''));
  };

  const updateValue = (key: string, value: string) => {
    // 修改原因：表单控件更新后仍要写回 plugin_name:options 的字符串格式。
    // 修改方式：先更新参数值 map，再按 schema 顺序序列化为 key=value 或 positional 字符串。
    // 目的：让后端保持读取单一 options 字符串，前端提供可视化编辑体验。
    const nextValues = { ...values, [key]: value };
    setValues(nextValues);
    commitValues(nextValues);
  };

  const labelClass = compact ? 'text-[10px] text-muted-foreground' : 'text-xs font-medium text-muted-foreground';
  const controlBaseClass = compact
    ? 'h-6 rounded border border-border bg-background px-1.5 text-[11px] text-foreground outline-none focus:border-primary disabled:opacity-50'
    : 'rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-emerald-500 disabled:opacity-50';
  const rowClass = compact ? 'grid grid-cols-[76px_minmax(0,1fr)] items-center gap-1.5' : 'grid grid-cols-[140px_minmax(0,1fr)] items-center gap-3';

  return (
    <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
      {shownParams.length === 0 ? (
        <div className={compact ? 'text-[10px] text-muted-foreground' : 'text-xs text-muted-foreground'}>当前条件下没有可配置参数。</div>
      ) : shownParams.map(param => {
        const value = displayValues[param.key] ?? '';
        const commonProps = {
          disabled,
          className: controlBaseClass,
        };

        return (
          <label key={param.key} className={rowClass}>
            <span className={labelClass}>{param.label || param.key}</span>
            {param.type === 'select' && (
              <select {...commonProps} value={value} onChange={event => updateValue(param.key, event.target.value)}>
                {(param.options || [{ value: '', label: '默认' }]).map(option => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            )}
            {param.type === 'text' && (
              <input
                {...commonProps}
                type="text"
                value={value}
                onChange={event => updateValue(param.key, event.target.value)}
                placeholder={param.placeholder || paramsHint || '留空使用默认值'}
              />
            )}
            {param.type === 'textarea' && (
              <textarea
                {...commonProps}
                value={value}
                onChange={event => updateValue(param.key, event.target.value)}
                placeholder={param.placeholder || paramsHint || '留空使用默认值'}
                rows={compact ? 3 : 5}
                className={`${controlBaseClass} ${compact ? 'h-16 py-1' : 'min-h-[96px] py-2'}`}
              />
            )}
            {param.type === 'number' && (
              <input
                {...commonProps}
                type="number"
                value={value}
                min={param.min}
                max={param.max}
                onChange={event => updateValue(param.key, event.target.value)}
                placeholder={param.placeholder || stringifyParamValue(param.default)}
              />
            )}
            {param.type === 'toggle' && (
              <Switch.Root
                checked={value === 'true' || value === '1' || value === 'yes'}
                onCheckedChange={checked => updateValue(param.key, checked ? 'true' : 'false')}
                disabled={disabled}
                className="w-9 h-5 bg-muted rounded-full relative data-[state=checked]:bg-emerald-500 transition-colors disabled:opacity-50"
              >
                <Switch.Thumb className="block w-4 h-4 bg-white rounded-full shadow-md transition-transform translate-x-0.5 data-[state=checked]:translate-x-[18px]" />
              </Switch.Root>
            )}
            {param.type === 'multi-select' && (
              <select
                {...commonProps}
                multiple
                value={splitMultiValue(value)}
                onChange={event => {
                  const selectedValues = Array.from(event.currentTarget.selectedOptions).map(option => option.value);
                  updateValue(param.key, selectedValues.join(MULTI_VALUE_SEPARATOR));
                }}
                className={`${controlBaseClass} ${compact ? 'h-16' : 'min-h-[86px]'}`}
              >
                {(param.options || []).map(option => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            )}
          </label>
        );
      })}
    </div>
  );
}
