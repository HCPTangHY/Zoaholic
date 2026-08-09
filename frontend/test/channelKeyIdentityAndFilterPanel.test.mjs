import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const editorSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/ChannelEditor.tsx'), 'utf8');
const hookSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useChannelEditor.tsx'), 'utf8');
const typesSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/types.ts'), 'utf8');
const interceptorSource = readFileSync(path.resolve(frontendRoot, 'src/components/InterceptorSheet.tsx'), 'utf8');
const pipelineSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/PipelineView.tsx'), 'utf8');
const sharedFieldsSource = readFileSync(path.resolve(frontendRoot, 'src/components/PluginConfigFields.tsx'), 'utf8');

// Key 行身份必须独立于数组下标，防止删除中间项后复用被删项的 DeferredInput/交互状态。
assert.match(typesSource, /_clientId: string;/, 'ApiKeyObj 应包含仅前端稳定身份');
assert.match(hookSource, /createApiKeyClientId\(\)/, '读取和新增 Key 时应生成稳定身份');
assert.match(editorSource, /key=\{keyObj\._clientId\}/, '完整行和机房卡片应使用稳定身份作为 React key');
assert.match(editorSource, /full-\$\{keyObj\._clientId\}/, '机房模式展开行应使用稳定身份');
assert.doesNotMatch(editorSource, /<FullKeyRow\s+key=\{idx\}/, '完整 Key 行不应继续使用数组下标作为 React key');
assert.doesNotMatch(editorSource, /<RackCard\s+key=\{idx\}/, '机房 Key 卡片不应继续使用数组下标作为 React key');
assert.match(hookSource, /current > idx \? current - 1 : current/, '删除中间 Key 后应同步修正聚焦/展开下标');

// 高级设置标题旁应有移动端也可见的插件配置入口，并复用现有 Sheet。
assert.match(editorSource, /高级设置[\s\S]*插件配置[\s\S]*setShowPluginSheet\(true\)|setShowPluginSheet\(true\)[\s\S]*插件配置/, '高级设置标题旁应能打开插件配置');

// 高级 JSON 应补在 Pipeline 原有紧凑 PluginCard 内，并与“插件配置”复用同一 provider_config 子区域。
assert.match(interceptorSource, /<PluginConfigFields[\s\S]*pluginName=\{plugin\.plugin_name\}/, '插件配置 Sheet 应继续使用共享完整面板');
assert.match(pipelineSource, /className="bg-card border border-border rounded-md px-3 py-2"/, 'Pipeline 应保留原有紧凑插件卡片');
assert.match(pipelineSource, /<PluginParamsForm[\s\S]*<PluginProviderConfigFields/, '高级 JSON 应紧跟在原有插件参数表单下方');
assert.match(pipelineSource, /providerConfig = info\?\.metadata\?\.provider_config/, 'Pipeline 应读取插件 metadata.provider_config');
assert.match(editorSource, /onProviderPreferenceChange[\s\S]*onProviderPreferenceDelete/, '渠道编辑器应接入高级 JSON 的更新与清空');
assert.match(sharedFieldsSource, /export function PluginProviderConfigFields/, '两处应复用同一个高级 JSON 子区域');
assert.match(sharedFieldsSource, /格式化/, '共享高级 JSON 应保留格式化按钮');
assert.match(sharedFieldsSource, /填入示例/, '共享高级 JSON 应保留填入示例按钮');
assert.match(sharedFieldsSource, /清空/, '共享高级 JSON 应保留清空按钮');
assert.doesNotMatch(editorSource, /<PluginConfigFields|ParameterFilterEditorDialog|打开完整面板/, '高级设置下不应再额外塞独立完整大卡片');

console.log('channel key identity and shared plugin panel regression passed');
process.exit(0);
