import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const pipelineSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/PipelineView.tsx'), 'utf8');
const editorSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/ChannelEditor.tsx'), 'utf8');
const adminSource = readFileSync(path.resolve(frontendRoot, 'src/pages/Admin.tsx'), 'utf8');

// PipelineView 应提供 inline 插件管理：紧凑卡片、移除按钮、参数表单和添加下拉。
assert.match(pipelineSource, /function PluginCard\(\{ name, opts, hasOpts, description, paramsSchema, paramsHint, onRemove, onOptsChange \}/, 'PipelineView 应保留紧凑插件卡片');
assert.match(pipelineSource, /<X className="w-3 h-3" \/>/, 'PipelineView 插件卡片应提供 inline 移除按钮');
assert.match(pipelineSource, /<PluginParamsForm/, 'PipelineView 插件卡片应使用可视化参数表单');
assert.match(pipelineSource, /params_schema/, 'PipelineView 应读取插件 metadata.params_schema');
assert.match(pipelineSource, /function PluginAddDropdown\(\{ stage, allPlugins, enabledPluginNames, openMenu, setOpenMenu, onAdd, onOpenPluginSheet \}/, 'PipelineView 应提供按阶段的插件添加下拉');
assert.match(pipelineSource, /data-plugin-add-menu/, '插件添加菜单应支持点击外部关闭');
assert.match(pipelineSource, /完整配置 →/, '插件添加菜单应保留完整配置入口');
assert.match(pipelineSource, /request: 'request_interceptors'/, 'PipelineView 应保留请求阶段拦截器字段');
assert.match(pipelineSource, /response: 'response_interceptors'/, 'PipelineView 应保留响应阶段拦截器字段');

// ChannelEditor 应把 Pipeline 的插件变更接回 formData.preferences.enabled_plugins。
assert.match(editorSource, /onPluginsChange=\{\(plugins\) => \{[\s\S]*enabled_plugins: plugins/, '渠道编辑器应接入插件列表变更');

// Admin Key Pipeline 应读取 /v1/plugins/interceptors，并接入 InterceptorSheet。
assert.match(adminSource, /apiFetch\('\/v1\/plugins\/interceptors', \{ headers \}\)/, 'Admin 应通过 /v1/plugins/interceptors 获取插件能力');
assert.match(adminSource, /onOpenPluginSheet=\{\(\) => setShowPluginSheet\(true\)\}/, 'Admin PipelineView 应接入插件配置入口');
assert.match(adminSource, /<InterceptorSheet/, 'Admin 应渲染 InterceptorSheet');
assert.match(adminSource, /enabledPlugins=\{formEnabledPlugins\}/, 'Admin InterceptorSheet 应接收当前 enabledPlugins');
assert.match(adminSource, /providerPreferences=\{currentKeyPluginPreferences\}/, 'Admin InterceptorSheet 应接收当前 Key preferences');
assert.match(adminSource, /params_schema/, 'Admin 应读取插件 metadata.params_schema');

console.log('pipeline inline plugins regression passed');
process.exit(0);
