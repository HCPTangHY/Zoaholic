import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const pipelineSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/PipelineView.tsx'), 'utf8');
const editorSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/ChannelEditor.tsx'), 'utf8');

// 修改原因：Pipeline 面板需要在常用场景中直接完成插件增删和参数编辑，避免每次打开 InterceptorSheet。
// 修改方式：用源码断言固定 onPluginsChange、可操作 PluginCard、阶段化添加菜单和 ChannelEditor 接线。
// 目的：防止后续重构把 inline 插件操作退回纯展示状态。
assert.match(pipelineSource, /onPluginsChange:\s*\(plugins: string\[\]\) => void/, 'PipelineViewProps 应暴露 onPluginsChange');
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
