export type BatchProvider = {
  provider: string;
  enabled?: boolean;
  model?: Array<string | Record<string, string>>;
  preferences?: Record<string, unknown>;
  [key: string]: unknown;
};

// Copy the complete rule family: retaining legacy values can change fallback behavior.
export const COOLING_FIELDS = [
  'cooldown_period', 'api_key_cooldown_period', 'key_rules',
  'auto_disable_key', 'status_code_overrides',
] as const;

export type ProviderBatchAction =
  | { kind: 'enabled'; enabled: boolean }
  | { kind: 'copy'; source: BatchProvider; cooling: boolean; models: 'none' | 'merge' | 'replace' };

export function mergeProviderModels(
  target: NonNullable<BatchProvider['model']>, source: NonNullable<BatchProvider['model']>,
): NonNullable<BatchProvider['model']> {
  const result = structuredClone(target);
  const names = new Set(target.flatMap(item => typeof item === 'string' ? [item] : Object.keys(item)));
  for (const item of source) {
    if (typeof item === 'string') {
      if (!names.has(item)) result.push(item);
      names.add(item);
    } else {
      const additions = Object.fromEntries(Object.entries(item).filter(([name]) => !names.has(name)));
      if (Object.keys(additions).length) result.push(additions);
      Object.keys(item).forEach(name => names.add(name));
    }
  }
  return result;
}

export function applyProviderBatchAction(target: BatchProvider, action: ProviderBatchAction): BatchProvider {
  const next = structuredClone(target);
  if (action.kind === 'enabled') {
    next.enabled = action.enabled;
    return next;
  }
  if (action.cooling) {
    next.preferences = { ...next.preferences };
    for (const field of COOLING_FIELDS) {
      if (Object.prototype.hasOwnProperty.call(action.source.preferences ?? {}, field)) {
        next.preferences[field] = structuredClone(action.source.preferences![field]);
      } else {
        delete next.preferences[field];
      }
    }
  }
  if (action.models !== 'none') {
    const sourceModels = action.source.model ?? [];
    next.model = action.models === 'replace'
      ? structuredClone(sourceModels)
      : mergeProviderModels(target.model ?? [], sourceModels);
  }
  return next;
}

export function selectedProviderNames(visible: string[], selected: Set<string>): string[] {
  return [...new Set(visible)].filter(name => selected.has(name));
}

// ponytail: existing API replaces one provider at a time; no server transaction/ETag.
// Read each target immediately before PUT, retain successes, and report partial failure.
export async function executeProviderBatch(
  names: string[], action: ProviderBatchAction,
  read: (name: string) => Promise<BatchProvider>,
  write: (name: string, provider: BatchProvider) => Promise<void>,
): Promise<{ succeeded: string[]; failed: Array<{ name: string; error: string }> }> {
  const succeeded: string[] = [];
  const failed: Array<{ name: string; error: string }> = [];
  for (const name of new Set(names)) {
    try {
      const latest = await read(name);
      if (latest.provider !== name) throw new Error('渠道名称已变化，请刷新后重试');
      await write(name, applyProviderBatchAction(latest, action));
      succeeded.push(name);
    } catch (error) {
      failed.push({ name, error: error instanceof Error ? error.message : String(error) });
    }
  }
  return { succeeded, failed };
}
