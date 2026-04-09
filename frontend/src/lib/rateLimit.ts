export type RateLimitUnit = 'sec' | 'min' | 'hour' | 'day' | 'month' | 'year' | 'tpr';

export interface RateLimitRuleDraft {
  id: string;
  count: string;
  unit: RateLimitUnit;
}

export interface RateLimitScopeDraft {
  id: string;
  scope: string;
  rules: RateLimitRuleDraft[];
}

export interface ParseRateLimitConfigOptions {
  allowModelScopes?: boolean;
}

const UNIT_ALIAS_MAP: Record<string, RateLimitUnit> = {
  s: 'sec',
  sec: 'sec',
  second: 'sec',
  m: 'min',
  min: 'min',
  minute: 'min',
  h: 'hour',
  hr: 'hour',
  hour: 'hour',
  d: 'day',
  day: 'day',
  mo: 'month',
  month: 'month',
  y: 'year',
  year: 'year',
  tpr: 'tpr'
};

export const RATE_LIMIT_UNIT_OPTIONS: { value: RateLimitUnit; label: string; description: string }[] = [
  { value: 'sec', label: '每秒', description: 'requests / second' },
  { value: 'min', label: '每分钟', description: 'requests / minute' },
  { value: 'hour', label: '每小时', description: 'requests / hour' },
  { value: 'day', label: '每天', description: 'requests / day' },
  { value: 'month', label: '每月', description: 'requests / month' },
  { value: 'year', label: '每年', description: 'requests / year' },
  { value: 'tpr', label: '单次 Token 上限', description: 'tokens per request' }
];

function makeId(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

export function normalizeRateLimitUnit(unit: string): RateLimitUnit {
  return UNIT_ALIAS_MAP[String(unit || '').trim().toLowerCase()] || 'min';
}

export function createEmptyRateLimitRule(unit: RateLimitUnit = 'min'): RateLimitRuleDraft {
  return {
    id: makeId('rule'),
    count: '',
    unit
  };
}

export function createRateLimitScope(scope = 'default', rules?: RateLimitRuleDraft[]): RateLimitScopeDraft {
  return {
    id: makeId('scope'),
    scope,
    rules: rules && rules.length ? rules : [createEmptyRateLimitRule()]
  };
}

export function parseRateLimitExpression(expression: unknown): RateLimitRuleDraft[] {
  if (typeof expression !== 'string') {
    return [];
  }

  const chunks = expression
    .split(',')
    .map(item => item.trim())
    .filter(Boolean);

  const rules = chunks
    .map(chunk => {
      const match = chunk.match(/^(\d+)\/(\w+)$/);
      if (!match) {
        return null;
      }
      const [, count, unit] = match;
      return {
        id: makeId('rule'),
        count,
        unit: normalizeRateLimitUnit(unit)
      } satisfies RateLimitRuleDraft;
    })
    .filter((item): item is RateLimitRuleDraft => Boolean(item));

  return rules;
}

export function serializeRateLimitExpression(rules: RateLimitRuleDraft[]): string | undefined {
  const normalized = rules
    .map(rule => ({
      count: String(rule.count || '').trim(),
      unit: normalizeRateLimitUnit(rule.unit)
    }))
    .filter(rule => /^\d+$/.test(rule.count) && Number(rule.count) > 0);

  if (normalized.length === 0) {
    return undefined;
  }

  return normalized.map(rule => `${rule.count}/${rule.unit}`).join(',');
}

export function parseRateLimitConfig(
  value: unknown,
  options: ParseRateLimitConfigOptions = {}
): RateLimitScopeDraft[] {
  const allowModelScopes = options.allowModelScopes ?? false;

  if (allowModelScopes && value && typeof value === 'object' && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>);
    const scopes = entries.map(([scope, expression]) => {
      const rules = parseRateLimitExpression(expression);
      return createRateLimitScope(scope || 'default', rules.length ? rules : [createEmptyRateLimitRule()]);
    });
    scopes.sort((a, b) => {
      return a.scope === 'default' ? -1 : b.scope === 'default' ? 1 : 0;
    });
    return scopes.length ? scopes : [createRateLimitScope('default')];
  }

  const rules = parseRateLimitExpression(value);
  return [createRateLimitScope('default', rules.length ? rules : [createEmptyRateLimitRule()])];
}

export function serializeRateLimitConfig(
  scopes: RateLimitScopeDraft[],
  options: ParseRateLimitConfigOptions = {}
): string | Record<string, string> | undefined {
  const allowModelScopes = options.allowModelScopes ?? false;

  const normalizedScopes = scopes
    .map(scope => ({
      scope: (scope.scope || 'default').trim() || 'default',
      expression: serializeRateLimitExpression(scope.rules || [])
    }))
    .filter(scope => Boolean(scope.expression));

  if (normalizedScopes.length === 0) {
    return undefined;
  }

  if (!allowModelScopes) {
    return normalizedScopes[0].expression;
  }

  const hasModelScopes = normalizedScopes.some(scope => scope.scope !== 'default');
  if (!hasModelScopes && normalizedScopes.length === 1) {
    return normalizedScopes[0].expression;
  }

  return Object.fromEntries(normalizedScopes.map(scope => [scope.scope, scope.expression as string]));
}

export function summarizeRateLimitConfig(value: unknown): string {
  if (!value) {
    return '未设置（使用默认无限制配置）';
  }

  if (typeof value === 'string') {
    return value;
  }

  if (typeof value === 'object' && !Array.isArray(value)) {
    const parts = Object.entries(value as Record<string, unknown>)
      .map(([scope, expression]) => `${scope}: ${String(expression)}`);
    return parts.join(' | ');
  }

  return String(value);
}
