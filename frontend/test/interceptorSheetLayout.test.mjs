import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(__dirname, '../src/components/InterceptorSheet.tsx'), 'utf8');

assert.match(source, /type InterceptorTab = 'all'[\s\S]*'request'[\s\S]*'response'/, '应该包含全部、请求拦截、响应拦截等 Tab 状态');
assert.match(source, /label: '全部'/, 'Tab 应包含全部');
assert.match(source, /label: '请求拦截'/, 'Tab 应包含请求拦截');
assert.match(source, /label: '响应拦截'/, 'Tab 应包含响应拦截');
assert.match(source, /placeholder="搜索插件\.\.\."/, '应该提供插件搜索框');
assert.match(source, /plugin\.plugin_name\.toLowerCase\(\)\.includes\(normalizedSearch\)/, '搜索应该匹配 plugin_name');
assert.match(source, /plugin\.description\.toLowerCase\(\)\.includes\(normalizedSearch\)/, '搜索应该匹配 description');
assert.match(source, /selectedPlugins[\s\S]*unselectedPlugins/, '列表应该拆成已启用和未启用两组');
assert.match(source, /未启用/, '已启用和未启用之间应该有未启用分割标签');
assert.match(source, /handlePluginRowClick[\s\S]*next\.set\(pluginName, ''\)[\s\S]*next\.add\(pluginName\)/, '点击未选中插件行应该先选中再展开');
assert.match(source, /有渠道配置/, '折叠行应该提示插件存在渠道配置');
assert.match(source, />请求<\//, '仅请求插件应该显示请求类型标签');
assert.match(source, />响应<\//, '仅响应插件应该显示响应类型标签');
assert.match(source, /\{options && <span className="text-xs bg-blue-500\/10/, '折叠行应该继续显示插件参数 pill');

console.log('interceptor sheet layout regression passed');
process.exit(0);
