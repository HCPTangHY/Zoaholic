import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：编辑渠道曾直接使用页面内存里的 provider 快照，另一个会话改动后再保存会覆盖后端新值。
// 修改方式：通过源码回归测试锁定 openModal 的单渠道 GET、失败回退和复制渠道不 GET 的分支结构。
// 目的：防止后续维护时把编辑填表逻辑退回为只读 providers 数组中的旧对象。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const source = readFileSync(path.resolve(frontendRoot, 'src/pages/Channels.tsx'), 'utf8');

function sliceBetween(startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

const openModal = sliceBetween('const openModal', 'const updateFormData');
assert.match(openModal, /const openModal = async \(provider: any = null, index: number \| null = null\) => \{/, 'openModal 应该改为 async，以便编辑时先等待后端最新 provider');
assert.match(openModal, /if \(provider && index !== null\) \{/, '只有真实主渠道编辑应该在 openModal 内触发 GET，复制渠道传入 index=null 时不应 GET');
assert.match(openModal, /let freshProvider = provider;/, '编辑 GET 失败时应该保留传入 provider 作为回退数据源');
assert.match(openModal, /apiFetch\(buildProviderApiPath\(providerId\), \{\s*method: 'GET'/s, '编辑主渠道应该通过 buildProviderApiPath(providerId) 请求单渠道详情');
assert.match(openModal, /const data = await res\.json\(\);\s*if \(data\?\.provider\) freshProvider = data\.provider;/s, 'GET 成功后应该使用响应中的 provider 填表');
assert.match(openModal, /toastWarning\('获取渠道最新数据失败，已使用页面缓存继续编辑'\)/, 'GET 失败时应该提示正在使用页面缓存');
assert.match(openModal, /const activeProvider = freshProvider;/, '填表逻辑应该明确改用 freshProvider 的别名，避免继续读旧 provider');
assert.match(openModal, /Array\.isArray\(activeProvider\.api\)/, 'API Key 解析应该读取 freshProvider');
assert.match(openModal, /setFormData\(\{\s*provider: activeProvider\.provider \|\| activeProvider\.name \|\| ''/s, '表单 provider 字段应该读取 freshProvider');
assert.doesNotMatch(openModal, /setFormData\(\{\s*provider: provider\.provider/s, '表单不能继续直接读取旧 provider 参数');

const openSubChannelEdit = sliceBetween('const openSubChannelEdit', 'const buildSubChannelProvider');
assert.match(openSubChannelEdit, /const openSubChannelEdit = async \(parentIdx: number, subIdx: number\) => \{/, '子渠道编辑入口应该允许先等待主渠道最新数据');
assert.match(openSubChannelEdit, /let freshParent = parent;/, '子渠道 GET 失败时应该保留页面中的主渠道作为回退');
assert.match(openSubChannelEdit, /apiFetch\(buildProviderApiPath\(providerId\), \{\s*method: 'GET'/s, '子渠道编辑应该先按主渠道 provider id 请求最新主渠道');
assert.match(openSubChannelEdit, /setProviders\(prev => prev\.map\(\(item, idx\) => idx === parentIdx \? freshParent : item\)\)/, '子渠道编辑拿到最新主渠道后应该同步 providers 状态，保存时才不会合并旧 parent');
assert.match(openSubChannelEdit, /const sub = \(freshParent\.sub_channels \|\| \[\]\)\[subIdx\];/, '子渠道表单应该从最新主渠道中取对应子渠道');

console.log('provider edit fresh GET regression passed');
process.exit(0);
