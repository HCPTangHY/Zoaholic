import { useEffect, useMemo, useRef, useState } from 'react';
import { Plus, Trash2, Shield, Layers3 } from 'lucide-react';
import {
  RATE_LIMIT_UNIT_OPTIONS,
  RateLimitRuleDraft,
  RateLimitScopeDraft,
  createEmptyRateLimitRule,
  createRateLimitScope,
  parseRateLimitConfig,
  serializeRateLimitConfig,
  serializeRateLimitExpression
} from '../lib/rateLimit';

interface RateLimitEditorProps {
  value: string | Record<string, string> | undefined;
  onChange: (value: string | Record<string, string> | undefined) => void;
  allowModelScopes?: boolean;
  title?: string;
  description?: string;
  className?: string;
}

function stableSignature(value: string | Record<string, string> | undefined): string {
  return JSON.stringify(value ?? null);
}

export function RateLimitEditor({
  value,
  onChange,
  allowModelScopes = false,
  title = 'Rate Limit 配置',
  description,
  className = ''
}: RateLimitEditorProps) {
  const externalSignature = useMemo(() => stableSignature(value), [value]);
  const lastEmittedSignatureRef = useRef<string | null>(null);
  const [scopes, setScopes] = useState<RateLimitScopeDraft[]>(() => parseRateLimitConfig(value, { allowModelScopes }));

  useEffect(() => {
    if (lastEmittedSignatureRef.current === externalSignature) {
      return;
    }
    setScopes(parseRateLimitConfig(value, { allowModelScopes }));
  }, [allowModelScopes, externalSignature, value]);

  const commitScopes = (nextScopes: RateLimitScopeDraft[]) => {
    setScopes(nextScopes);
    const serialized = serializeRateLimitConfig(nextScopes, { allowModelScopes });
    lastEmittedSignatureRef.current = stableSignature(serialized);
    onChange(serialized);
  };

  const updateScope = (scopeId: string, updater: (scope: RateLimitScopeDraft) => RateLimitScopeDraft) => {
    commitScopes(scopes.map(scope => (scope.id === scopeId ? updater(scope) : scope)));
  };

  const addRule = (scopeId: string) => {
    updateScope(scopeId, scope => ({
      ...scope,
      rules: [...scope.rules, createEmptyRateLimitRule(scope.rules[scope.rules.length - 1]?.unit || 'min')]
    }));
  };

  const updateRule = (scopeId: string, ruleId: string, patch: Partial<RateLimitRuleDraft>) => {
    updateScope(scopeId, scope => ({
      ...scope,
      rules: scope.rules.map(rule => (rule.id === ruleId ? { ...rule, ...patch } : rule))
    }));
  };

  const removeRule = (scopeId: string, ruleId: string) => {
    updateScope(scopeId, scope => {
      const nextRules = scope.rules.filter(rule => rule.id !== ruleId);
      return {
        ...scope,
        rules: nextRules.length ? nextRules : [createEmptyRateLimitRule()]
      };
    });
  };

  const addScope = () => {
    commitScopes([...scopes, createRateLimitScope('model-name')]);
  };

  const removeScope = (scopeId: string) => {
    const nextScopes = scopes.filter(scope => scope.id !== scopeId);
    commitScopes(nextScopes.length ? nextScopes : [createRateLimitScope('default')]);
  };

  const clearAll = () => {
    commitScopes([createRateLimitScope('default')]);
  };

  return (
    <div className={`space-y-4 ${className}`.trim()}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="text-sm font-semibold text-foreground flex items-center gap-2">
            <Shield className="w-4 h-4 text-emerald-500" /> {title}
          </div>
          {description ? <div className="text-xs text-muted-foreground mt-1">{description}</div> : null}
        </div>
        <div className="flex flex-wrap gap-2">
          {allowModelScopes ? (
            <button
              type="button"
              onClick={addScope}
              className="text-xs border border-border bg-muted hover:bg-muted/80 px-2.5 py-1.5 rounded-md text-foreground flex items-center gap-1"
            >
              <Layers3 className="w-3.5 h-3.5" /> 添加模型规则
            </button>
          ) : null}
          <button
            type="button"
            onClick={clearAll}
            className="text-xs border border-border bg-muted hover:bg-muted/80 px-2.5 py-1.5 rounded-md text-foreground"
          >
            重置
          </button>
        </div>
      </div>

      <div className="space-y-3">
        {scopes.map((scope, index) => {
          const isDefaultScope = (scope.scope || 'default').trim() === 'default';
          const expression = serializeRateLimitExpression(scope.rules);

          return (
            <div key={scope.id} className="rounded-xl border border-border bg-muted/30 p-4 space-y-3">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                <div className="flex-1 grid gap-3 md:grid-cols-[160px_1fr]">
                  <div>
                    <label className="text-xs uppercase tracking-wider text-muted-foreground mb-1 block">作用域</label>
                    {allowModelScopes ? (
                      <input
                        type="text"
                        value={scope.scope}
                        onChange={e => updateScope(scope.id, prev => ({ ...prev, scope: e.target.value }))}
                        disabled={isDefaultScope && index === 0}
                        placeholder="default / gpt-4o / claude"
                        className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground disabled:opacity-70"
                      />
                    ) : (
                      <div className="bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground">default</div>
                    )}
                  </div>

                  <div>
                    <label className="text-xs uppercase tracking-wider text-muted-foreground mb-1 block">当前表达式</label>
                    <div className="bg-background border border-border px-3 py-2 rounded-lg text-sm font-mono text-foreground break-all">
                      {expression || '未设置（留空则不写入配置）'}
                    </div>
                  </div>
                </div>

                {allowModelScopes && !(isDefaultScope && index === 0) ? (
                  <button
                    type="button"
                    onClick={() => removeScope(scope.id)}
                    className="self-start text-xs text-red-500 hover:text-red-400 border border-red-500/20 hover:bg-red-500/10 px-2.5 py-1.5 rounded-md flex items-center gap-1"
                  >
                    <Trash2 className="w-3.5 h-3.5" /> 删除作用域
                  </button>
                ) : null}
              </div>

              <div className="space-y-2">
                {scope.rules.map(rule => (
                  <div key={rule.id} className="grid gap-2 md:grid-cols-[1fr_180px_auto] items-center">
                    <input
                      type="number"
                      min="1"
                      value={rule.count}
                      onChange={e => updateRule(scope.id, rule.id, { count: e.target.value })}
                      placeholder={rule.unit === 'tpr' ? '例如 8192' : '例如 60'}
                      className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                    />
                    <select
                      value={rule.unit}
                      onChange={e => updateRule(scope.id, rule.id, { unit: e.target.value as RateLimitRuleDraft['unit'] })}
                      className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                    >
                      {RATE_LIMIT_UNIT_OPTIONS.map(option => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                    <button
                      type="button"
                      onClick={() => removeRule(scope.id, rule.id)}
                      className="text-muted-foreground hover:text-red-500 hover:bg-red-500/10 border border-border rounded-lg px-3 py-2 text-sm"
                      title="删除规则"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                ))}
              </div>

              <div className="flex items-center justify-between gap-3">
                <div className="text-xs text-muted-foreground">
                  {isDefaultScope ? 'default 作用于未命中的所有模型。' : '模型作用域支持精确名称或子串匹配。'}
                </div>
                <button
                  type="button"
                  onClick={() => addRule(scope.id)}
                  className="text-xs bg-background border border-border hover:bg-muted px-2.5 py-1.5 rounded-md text-foreground flex items-center gap-1"
                >
                  <Plus className="w-3.5 h-3.5" /> 添加规则
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
