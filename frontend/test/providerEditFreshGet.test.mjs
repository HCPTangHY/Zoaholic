import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const pageSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/ChannelsPage.tsx'), 'utf8');
const hookSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useChannelEditor.tsx'), 'utf8');

// Channels.tsx 拆分后，编辑入口 hook 组合位于 ChannelsPage.tsx，openModal 实现位于 hooks/useChannelEditor.tsx。
assert.match(pageSource, /useChannelEditor\(core\)/, '页面应通过 useChannelEditor 组合编辑逻辑');

function sliceBetween(source, startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

const openModal = sliceBetween(hookSource, 'const openModal = async', 'const updateFormData');
assert.match(openModal, /await apiFetch\(buildProviderApiPath\(providerId\), \{\s*method: 'GET'/, '编辑渠道时应先 GET 后端最新详情');
assert.match(openModal, /if \(data\?\.provider\) freshProvider = data\.provider;/, 'GET 成功时应使用后端返回的最新 provider');
assert.match(openModal, /获取渠道最新数据失败，已使用页面缓存继续编辑/, 'GET 失败时应提示并回退页面缓存');
assert.match(openModal, /setOriginalIndex\(index\)/, '编辑入口应保留 originalIndex');
assert.match(openModal, /setIsModalOpen\(true\)/, 'openModal 最终应打开编辑弹窗');

console.log('provider edit fresh get regression passed');
process.exit(0);
