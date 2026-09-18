import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const pageSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/ChannelsPage.tsx'), 'utf8');
const editorSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/ChannelEditor.tsx'), 'utf8');
const editorHookSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useChannelEditor.tsx'), 'utf8');

function sliceBetween(source, startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

// 修改原因：Channels.tsx 拆分后，OAuth 导入/手动回调弹窗位于 ChannelsPage.tsx。
// 修改方式：弹窗通过 createPortal 渲染到 body，并在遮罩容器添加 tabIndex={-1}。
// 目的：避免 OAuth 弹窗被当作 body 第一个可聚焦元素自动聚焦，同时继续阻止主编辑器触屏误触关闭。
const importModal = sliceBetween(pageSource, '{importModalIdx !== null && createPortal(', '{oauthManualState !== null && createPortal(');
assert.match(importModal, /tabIndex=\{-1\}/, 'OAuth 导入弹窗遮罩应禁止自动聚焦');
assert.match(importModal, /fixed inset-0 z-\[100\]/, 'OAuth 导入弹窗应覆盖主编辑器');

const manualModalStart = pageSource.indexOf('{oauthManualState !== null && createPortal(');
assert.notEqual(manualModalStart, -1, '应保留 OAuth 手动回调弹窗');
const manualModal = pageSource.slice(manualModalStart, manualModalStart + 3000);
assert.match(manualModal, /tabIndex=\{-1\}/, 'OAuth 手动回调弹窗遮罩应禁止自动聚焦');
assert.match(manualModal, /fixed inset-0 z-\[100\]/, 'OAuth 手动回调弹窗应覆盖主编辑器');

// 移动端打开渠道编辑弹窗时，应锁定 Layout 的 <main> 滚动容器阻止背景回弹。
// 滚动锁实现位于 hooks/useChannelEditor.tsx。
assert.match(editorHookSource, /const applyChannelModalScrollLock = useCallback\(/, '渠道编辑弹窗打开时应应用滚动锁');
assert.match(editorHookSource, /channelModalScrollYRef\.current = scroller \? scroller\.scrollTop : 0;/, '滚动锁应保存当前滚动位置');

// 渠道编辑器应保留弹窗结构（遮罩 + 居中内容）。
assert.match(editorSource, /fixed inset-0/, '渠道编辑器应保留遮罩层');
assert.match(editorSource, /编辑渠道|新增渠道|编辑子渠道/, '渠道编辑器应保留弹窗标题');

console.log('oauth portal focus guard regression passed');
process.exit(0);
