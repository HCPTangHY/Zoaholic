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

// Key 行身份必须独立于数组下标，防止删除中间项后复用被删项的 DeferredInput/交互状态。
assert.match(typesSource, /_clientId: string;/, 'ApiKeyObj 应包含仅前端稳定身份');
assert.match(hookSource, /createApiKeyClientId\(\)/, '读取和新增 Key 时应生成稳定身份');
assert.match(editorSource, /key=\{keyObj\._clientId\}/, '完整行和机房卡片应使用稳定身份作为 React key');
assert.match(editorSource, /full-\$\{keyObj\._clientId\}/, '机房模式展开行应使用稳定身份');
assert.doesNotMatch(editorSource, /<FullKeyRow\s+key=\{idx\}/, '完整 Key 行不应继续使用数组下标作为 React key');
assert.doesNotMatch(editorSource, /<RackCard\s+key=\{idx\}/, '机房 Key 卡片不应继续使用数组下标作为 React key');
assert.match(hookSource, /current > idx \? current - 1 : current/, '删除中间 Key 后应同步修正聚焦/展开下标');

// 编辑面板应保留插件配置入口（Pipeline 完整配置按钮 + 侧边凸出插件按钮），并复用现有 Sheet。
assert.match(editorSource, /onOpenPluginSheet=\{\(\) => setShowPluginSheet\(true\)\}/, 'Pipeline 应保留完整插件配置入口');
assert.match(editorSource, /onClick=\{\(\) => setShowPluginSheet\(true\)\}/, '编辑面板应保留插件配置按钮');

// 渠道级插件配置（provider_config）应在 InterceptorSheet 中按 metadata.provider_config 渲染 JSON 编辑区。
assert.match(interceptorSource, /metadata\?\.provider_config\?\.key/, 'InterceptorSheet 应读取插件 metadata.provider_config');
assert.match(interceptorSource, /渠道配置（JSON）/, 'InterceptorSheet 应渲染渠道配置 JSON 编辑区');
assert.match(interceptorSource, /providerConfigText/, 'InterceptorSheet 应维护渠道配置文本状态');
assert.match(interceptorSource, /preferences_patch\[meta\.key\] = JSON\.parse\(t\)/, '渠道配置保存时应解析 JSON 写入 preferences_patch');

// Pipeline 应保留紧凑插件卡片和可视化参数表单。
assert.match(pipelineSource, /className="bg-card border border-border rounded-md px-3 py-2"/, 'Pipeline 应保留原有紧凑插件卡片');
assert.match(pipelineSource, /<PluginParamsForm/, 'Pipeline 应使用可视化参数表单');
assert.match(pipelineSource, /params_schema/, 'Pipeline 应读取插件 metadata.params_schema');

// ChannelEditor 应把 Pipeline 的插件变更接回 formData.preferences.enabled_plugins。
assert.match(editorSource, /onPluginsChange=\{\(plugins\) => \{[\s\S]*enabled_plugins: plugins/, '渠道编辑器应接入插件列表变更');

console.log('channel key identity and plugin panel regression passed');
process.exit(0);
