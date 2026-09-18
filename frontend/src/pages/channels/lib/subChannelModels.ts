import type { ProviderFormData, SubChannelFormData } from '../types';

/** Match runtime sub-channel inheritance without changing the parent's draft. */
export function buildSubChannelModelsRequest(
  parent: Pick<ProviderFormData, 'base_url' | 'api_keys' | 'preferences'>,
  sub: Pick<SubChannelFormData, 'engine' | 'base_url' | 'preferences'>,
) {
  const firstKey = parent.api_keys.find(item => item.key.trim() && !item.disabled);
  const baseUrl = sub.base_url || parent.base_url;
  if (!baseUrl || !firstKey) return null;

  return {
    engine: sub.engine,
    base_url: baseUrl,
    api_key: firstKey.key,
    // Runtime inheritance is shallow: child headers/plugins override the whole parent value.
    preferences: { ...(parent.preferences || {}), ...(sub.preferences || {}) },
  };
}
