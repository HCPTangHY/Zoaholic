import { Settings2 } from 'lucide-react';
import { toastError } from './Toast';
import { PluginParamsForm, type ParamSchema } from './PluginParamsForm';

export interface PluginProviderConfigMetadata {
  key: string;
  type?: 'json' | 'text';
  title?: string;
  description?: string;
  example?: unknown;
}

export interface PluginConfigMetadata {
  params_hint?: string;
  params_schema?: ParamSchema[];
  provider_config?: PluginProviderConfigMetadata;
}

interface PluginProviderConfigFieldsProps {
  providerConfig?: PluginProviderConfigMetadata;
  selected: boolean;
  providerConfigText: string;
  onProviderConfigTextChange: (text: string) => void;
}

/** “插件配置”与 Pipeline 紧凑卡片共用的渠道级高级配置区域。 */
export function PluginProviderConfigFields({
  providerConfig,
  selected,
  providerConfigText,
  onProviderConfigTextChange,
}: PluginProviderConfigFieldsProps) {
  if (!providerConfig?.key) return null;

  const formatJsonText = (text: string): string => {
    if (!text.trim()) return '';
    return JSON.stringify(JSON.parse(text), null, 2);
  };

  return (
    <div className="space-y-2 mt-4" data-plugin-provider-config={providerConfig.key}>
      <label className="text-xs font-medium text-muted-foreground flex items-center gap-1">
        <Settings2 className="w-3.5 h-3.5" />
        {providerConfig.title || '渠道配置（JSON）'}
      </label>

      {providerConfig.description && (
        <p className="text-xs text-muted-foreground">{providerConfig.description}</p>
      )}

      <textarea
        value={providerConfigText}
        onChange={(event) => onProviderConfigTextChange(event.target.value)}
        disabled={!selected}
        rows={6}
        placeholder={providerConfig.example != null ? JSON.stringify(providerConfig.example, null, 2) : '请输入 JSON'}
        className="w-full bg-background border border-border text-foreground focus:border-emerald-500 px-3 py-2 rounded-md text-sm font-mono disabled:opacity-50 outline-none"
      />

      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={!selected}
          onClick={() => {
            try {
              onProviderConfigTextChange(formatJsonText(providerConfigText));
            } catch (error: unknown) {
              toastError(`格式化失败：${error instanceof Error ? error.message : 'invalid json'}`);
            }
          }}
          className="text-xs font-medium text-muted-foreground hover:text-foreground px-2 py-1 bg-muted rounded disabled:opacity-50"
        >
          格式化
        </button>

        {providerConfig.example != null && (
          <button
            type="button"
            disabled={!selected}
            onClick={() => onProviderConfigTextChange(JSON.stringify(providerConfig.example, null, 2))}
            className="text-xs font-medium text-emerald-600 dark:text-emerald-500 hover:text-emerald-500 px-2 py-1 bg-emerald-500/10 rounded disabled:opacity-50"
          >
            填入示例
          </button>
        )}

        <button
          type="button"
          disabled={!selected}
          onClick={() => onProviderConfigTextChange('')}
          className="text-xs font-medium text-red-600 dark:text-red-400 hover:text-red-500 px-2 py-1 bg-red-500/10 rounded disabled:opacity-50"
        >
          清空
        </button>
      </div>
    </div>
  );
}

interface PluginConfigFieldsProps {
  pluginName: string;
  metadata?: PluginConfigMetadata;
  selected: boolean;
  options: string;
  providerConfigText: string;
  onOptionsChange: (options: string) => void;
  onProviderConfigTextChange: (text: string) => void;
}

export function PluginConfigFields({
  pluginName,
  metadata,
  selected,
  options,
  providerConfigText,
  onOptionsChange,
  onProviderConfigTextChange,
}: PluginConfigFieldsProps) {
  const paramsSchema = Array.isArray(metadata?.params_schema) ? metadata.params_schema : [];

  return (
    <div className="px-3 pb-3 pt-1 border-t border-border bg-muted/20" data-plugin-config-fields={pluginName}>
      <div className="space-y-1.5 mt-2">
        <label className="text-xs font-medium text-muted-foreground flex items-center gap-1">
          <Settings2 className="w-3.5 h-3.5" /> 插件参数
        </label>
        <PluginParamsForm
          options={options}
          schema={paramsSchema}
          onChange={onOptionsChange}
          disabled={!selected}
          paramsHint={metadata?.params_hint}
          size="normal"
        />
      </div>

      <PluginProviderConfigFields
        providerConfig={metadata?.provider_config}
        selected={selected}
        providerConfigText={providerConfigText}
        onProviderConfigTextChange={onProviderConfigTextChange}
      />
    </div>
  );
}
