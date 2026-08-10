import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(__dirname, '../src/components/InterceptorSheet.tsx'), 'utf8');

// InterceptorSheet 标签布局：全部 / 渠道入站 / 请求拦截 / 响应拦截 / 渠道出站 / Key 出站。
assert.match(source, /type InterceptorTab = 'all' \| 'channel_inbound' \| 'request' \| 'response' \| 'channel_outbound' \| 'key_outbound'/, '应保留 InterceptorTab 类型');
assert.match(source, /\{ value: 'all', label: '全部' \}/, '应保留全部标签');
assert.match(source, /\{ value: 'channel_inbound', label: '渠道入站' \}/, '应保留渠道入站标签');
assert.match(source, /\{ value: 'request', label: '请求拦截' \}/, '应保留请求拦截标签');
assert.match(source, /\{ value: 'response', label: '响应拦截' \}/, '应保留响应拦截标签');
assert.match(source, /\{ value: 'channel_outbound', label: '渠道出站' \}/, '应保留渠道出站标签');
assert.match(source, /\{ value: 'key_outbound', label: 'Key 出站' \}/, '应保留 Key 出站标签');

// 插件搜索：应按名称/描述过滤。
assert.match(source, /normalizedSearch/, 'InterceptorSheet 应保留插件搜索过滤');
assert.match(source, /plugin\.plugin_name\.toLowerCase\(\)\.includes\(normalizedSearch\)/, '搜索应匹配插件名称');
assert.match(source, /plugin\.description\.toLowerCase\(\)\.includes\(normalizedSearch\)/, '搜索应匹配插件描述');

// Tab 和搜索过滤之后，应把已启用和未启用插件拆成两组展示。
assert.match(source, /const selectedPlugins: PluginOption\[\] = \[\]/, '应保留已启用分组');
assert.match(source, /const unselectedPlugins: PluginOption\[\] = \[\]/, '应保留未启用分组');
assert.match(source, /if \(selected\.has\(plugin\.plugin_name\)\) selectedPlugins\.push\(plugin\);/, '已启用插件应进入已启用分组');

// 插件行交互：行点击切换选中，有渠道配置的插件应有标识。
assert.match(source, /const handlePluginRowClick = \(pluginName: string\)/, '应保留行点击切换');
assert.match(source, /title="有渠道配置"/, '插件行应标识存在 provider_config 的插件');

console.log('interceptor sheet layout regression passed');
process.exit(0);
