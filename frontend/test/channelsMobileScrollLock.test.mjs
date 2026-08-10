import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：移动端打开编辑/测试弹窗时页面滚动锁不能跟随内部列表滚动，也不能在测试弹窗关闭时被提前移除。
// 修改方式：滚动锁实现位于 hooks/useChannelEditor.tsx，锁定 Layout 的 <main> 滚动容器并保存滚动位置。
// 目的：固定移动端弹窗滚动锁行为，避免未来改动重新引入背景跳动。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const source = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useChannelEditor.tsx'), 'utf8');

assert.match(source, /const channelModalScrollYRef = useRef\(0\);/, '应保留弹窗滚动位置 ref');

const restoreStart = source.indexOf('const restoreChannelModalScrollLock = useCallback(');
assert.notEqual(restoreStart, -1, '应保留 restoreChannelModalScrollLock');
const restoreBlock = source.slice(restoreStart, restoreStart + 800);
assert.match(restoreBlock, /const savedTop = channelModalScrollYRef\.current;/, '解锁前应读取保存的滚动位置');
assert.match(restoreBlock, /document\.querySelector\('main'\)/, '页面实际滚动容器是 Layout 的 <main>');
assert.match(restoreBlock, /scroller\.scrollTop = savedTop;/, '解锁后应恢复原页面滚动位置');

const applyStart = source.indexOf('const applyChannelModalScrollLock = useCallback(');
assert.notEqual(applyStart, -1, '应保留 applyChannelModalScrollLock');
const applyBlock = source.slice(applyStart, applyStart + 600);
assert.match(applyBlock, /channelModalScrollYRef\.current = scroller \? scroller\.scrollTop : 0;/, '锁定前应保存当前页面滚动位置');

assert.match(source, /const isChannelScrollLockedDialogOpen = isModalOpen \|\| testDialogOpen \|\| keyTestDialogOpen \|\| analyticsOpen;/, '滚动锁应同时覆盖编辑、渠道测试、Key 测试和分析弹窗');
assert.match(source, /if \(!isChannelScrollLockedDialogOpen\) restoreChannelModalScrollLock\(\);/, '所有受保护弹窗关闭后才应恢复滚动锁');
assert.match(source, /return restoreChannelModalScrollLock;/, '组件卸载时应兜底恢复滚动锁');

console.log('channels mobile scroll lock regression passed');
process.exit(0);
