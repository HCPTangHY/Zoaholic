import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const pipelineSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/PipelineView.tsx'), 'utf8');
const editorSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/ChannelEditor.tsx'), 'utf8');
const adminSource = readFileSync(path.resolve(frontendRoot, 'src/pages/Admin.tsx'), 'utf8');

// 修改原因：Pipeline 面板需要在常用场景中直接完成插件增删和参数编辑，避免每次打开 InterceptorSheet。
// 修改方式：用源码断言固定 onPluginsChange、可操作 PluginCard、阶段化添加菜单和 ChannelEditor 接线。
// 目的：防止后续重构把 inline 插件操作退回纯展示状态。
assert.match(pipelineSource, /onPluginsChange:\s*\(plugins: EnabledPluginValue\[\]\) => void/, 'PipelineViewProps 应暴露 onPluginsChange');
assert.match(pipelineSource, /function PluginCard[\s\S]*onRemove[\s\S]*onOptsChange/, 'PluginCard 应支持删除和参数更新');
assert.match(pipelineSource, /<X className="w-3 h-3" \/>/, 'PluginCard 应有小号删除按钮');
assert.match(pipelineSource, /PluginParamsForm/, '参数区域应使用可视化参数表单');
assert.match(pipelineSource, /metadata\?\.params_schema/, 'PipelineView 应读取插件 metadata.params_schema');
assert.match(pipelineSource, /function PluginAddDropdown/, '应有快速添加插件下拉菜单');
assert.match(pipelineSource, /request_interceptors\?\.length/, '出站添加菜单应按 request_interceptors 过滤');
assert.match(pipelineSource, /response_interceptors\?\.length/, '响应添加菜单应按 response_interceptors 过滤');
assert.match(pipelineSource, /完整配置 →/, '快速添加菜单底部应保留完整配置入口');
assert.match(pipelineSource, /data-plugin-add-menu/, '快速添加菜单应有点击外部关闭的识别容器');
assert.match(editorSource, /onPluginsChange=\{\(plugins\) => \{[\s\S]*enabled_plugins: plugins/, 'ChannelEditor 应把 onPluginsChange 接到 formData.preferences.enabled_plugins');

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
