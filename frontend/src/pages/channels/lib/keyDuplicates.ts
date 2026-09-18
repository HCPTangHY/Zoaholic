import type { ApiKeyObj } from '../types';

export interface KeyDuplicateSummary {
  groups: { rows: number[] }[];
  duplicateGroupCount: number;
  duplicateRowCount: number;
  checkableCount: number;
  emptyCount: number;
}

/** Compare configuration values only; never return keys, labels, or row objects. */
export function inspectKeyDuplicates(keys: readonly Readonly<ApiKeyObj>[]): KeyDuplicateSummary {
  const rowsByKey = new Map<string, number[]>();
  let emptyCount = 0;

  keys.forEach((entry, index) => {
    // Only surrounding whitespace is ignored. Disabled rows and mask-like literals
    // are ordinary configuration values, not credentials to resolve or validate.
    const key = entry.key.trim();
    if (!key) {
      emptyCount += 1;
      return;
    }
    const rows = rowsByKey.get(key);
    if (rows) rows.push(index + 1);
    else rowsByKey.set(key, [index + 1]);
  });

  const groups = Array.from(rowsByKey.values())
    .filter(rows => rows.length > 1)
    .map(rows => ({ rows }));

  return {
    groups,
    duplicateGroupCount: groups.length,
    duplicateRowCount: groups.reduce((count, group) => count + group.rows.length, 0),
    checkableCount: keys.length - emptyCount,
    emptyCount,
  };
}
