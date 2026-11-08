export function keyTreeRows(keys, collapsed = new Set()) {
  const children = new Map();
  const ids = new Set(keys.map(key => key.id).filter(Boolean));
  keys.forEach((key, index) => {
    const parent = ids.has(key.parent_id) ? key.parent_id : '';
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push({ key, index });
  });
  const rows = [];
  const stack = [...(children.get('') || [])].reverse().map(row => ({ ...row, depth: 0 }));
  const seen = new Set();
  while (stack.length) {
    const row = stack.pop();
    if (seen.has(row.index)) continue;
    seen.add(row.index);
    const nested = children.get(row.key.id) || [];
    rows.push({ ...row, hasChildren: nested.length > 0 });
    if (!collapsed.has(row.key.id)) {
      for (let i = nested.length - 1; i >= 0; i--) stack.push({ ...nested[i], depth: row.depth + 1 });
    }
  }
  return rows;
}

export function inheritedKeyValue(parent, field) {
  if (field === 'preferences.quota' && parent?.preferences?.quota === undefined) {
    const prefs = parent?.preferences || {};
    const quota = typeof prefs.rate_limit === 'string' ? { default: prefs.rate_limit } : { ...prefs.rate_limit };
    if (typeof prefs.credits === 'number' && prefs.credits >= 0) quota.cost = `${prefs.credits}/inf`;
    return Object.keys(quota).length ? quota : undefined;
  }
  const value = field.startsWith('preferences.') ? parent?.preferences?.[field.slice(12)] : parent?.[field];
  return value === undefined ? undefined : structuredClone(value);
}

export function splitKeyQuota(quota) {
  const rules = [], models = [];
  for (const [key, value] of Object.entries(quota || {})) {
    if (/^(default|request|cost|token|token_in|token_out|ip)(:|$)/.test(key)) rules.push({ key, value });
    else models.push({ key, value });
  }
  return { rules, models };
}

export function ownKeyFields(key) {
  return new Set([
    ...Object.keys(key || {}).filter(field => !['preferences', 'api', 'id', 'parent_id', 'name', 'role', 'enabled'].includes(field)),
    ...Object.keys(key?.preferences || {}).map(field => `preferences.${field}`),
  ]);
}

export function retainKeyOverrides(target, fields) {
  const next = { ...target, preferences: { ...target.preferences } };
  for (const field of Object.keys(next)) {
    if (!['preferences', 'api', 'id', 'parent_id', 'name', 'role', 'enabled'].includes(field) && !fields.has(field)) delete next[field];
  }
  for (const field of Object.keys(next.preferences)) {
    if (!fields.has(`preferences.${field}`)) delete next.preferences[field];
  }
  if (!Object.keys(next.preferences).length) delete next.preferences;
  return next;
}
