import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：机房模式曾把圆环轨道、tier 药丸和空 OAuth 操作层写成深色主题样式，浅色主题下可读性不足。
// 修改方式：用源码静态断言锁定 RackRingCircle、getRackTierClass 和 RackCard 空 OAuth 操作层的 light/dark 双主题类。
// 目的：防止后续维护再次引入只适合深色主题的机房卡片视觉样式。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const channelsSource = readFileSync(path.resolve(frontendRoot, 'src/pages/Channels.tsx'), 'utf8');

function sliceBetween(source, startMarker, endMarker, fromIndex = 0) {
  const start = source.indexOf(startMarker, fromIndex);
  assert.notEqual(start, -1, `找不到起始片段：${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `找不到结束片段：${endMarker}`);
  return source.slice(start, end);
}

const rackRingCircle = sliceBetween(channelsSource, 'function RackRingCircle', 'function RackSingleRing');
assert.match(rackRingCircle, /className="stroke-slate-300 dark:stroke-\[#1a1a2e\]"/, '圆环轨道应使用浅色可见的 slate-300，并在深色主题保留原暗色轨道');
assert.doesNotMatch(rackRingCircle, /stroke="#1a1a2e"/, '圆环轨道不能继续使用硬编码 stroke 属性');
assert.doesNotMatch(rackRingCircle, /stroke-slate-800 dark:stroke-\[#1a1a2e\]/, '圆环轨道浅色主题不能使用过深的 slate-800');

const rackTierClass = sliceBetween(channelsSource, 'function getRackTierClass', 'function getRackBalanceTextClass');
[
  'border-amber-600/40 bg-amber-500/20 text-amber-800 dark:text-amber-200',
  'border-emerald-600/35 bg-emerald-500/15 text-emerald-800 dark:text-emerald-200',
  'border-pink-600/40 bg-pink-500/20 text-pink-800 dark:text-pink-200',
  'border-purple-600/40 bg-purple-500/20 text-purple-800 dark:text-purple-200',
  'border-sky-600/35 bg-sky-500/15 text-sky-800 dark:text-sky-200',
  'border-slate-500/30 bg-slate-500/15 text-slate-700 dark:text-slate-200',
].forEach(expectedClass => {
  assert.ok(rackTierClass.includes(expectedClass), `tier 药丸应包含浅色与深色主题类：${expectedClass}`);
});
// 修改原因：dark:text-*-200 是预期深色主题类，不能被旧写法检查误判。
// 修改方式：逐个读取 return 字符串，确认每个返回值都同时带浅色 text 类和 dark:text 类。
// 目的：测试只拦截“只返回 text-*-200”的旧实现，不拦截正确的深色主题变体。
const rackTierReturns = [...rackTierClass.matchAll(/return '([^']+)'/g)].map(match => match[1]);
assert.equal(rackTierReturns.length, 6, 'getRackTierClass 应继续返回 6 组 tier 样式');
assert.ok(rackTierReturns.every(className => /\stext-(amber|emerald|pink|purple|sky|slate)-(800|700)\s/.test(`${className} `)), '每个 tier 药丸都应包含浅色主题文字类');
assert.ok(rackTierReturns.every(className => /\sdark:text-(amber|emerald|pink|purple|sky|slate)-200(?:\s|$)/.test(` ${className}`)), '每个 tier 药丸都应包含深色主题文字类');

const rackCard = sliceBetween(channelsSource, 'function RackCard', 'export default function Channels');
assert.match(rackCard, /bg-card\/90 dark:bg-muted\/50/, '机房卡片背景应在浅色主题使用 card 表面，并在深色主题保留 muted 表面');
assert.match(rackCard, /bg-white\/90 dark:bg-\[#0f0f12\]\/85/, '空 OAuth 圆形操作层应在浅色主题使用白色遮罩，深色主题保留暗色遮罩');
assert.match(rackCard, /border-slate-300 bg-white\/95 px-1\.5 py-0\.5 text-\[9px\] text-slate-700 hover:bg-slate-100 dark:border-\[#1e1e22\] dark:bg-slate-800\/90 dark:text-slate-100 dark:hover:bg-slate-700/, '空 OAuth 导入按钮应同时提供浅色与深色主题颜色');
assert.match(rackCard, /text-\[9px\] text-blue-700 hover:bg-blue-500\/20 dark:text-blue-200 dark:hover:bg-blue-500\/25/, '空 OAuth 登录按钮应同时提供浅色与深色主题颜色');
// 修改原因：OAuth 双环中心文字属于 RackOAuthRings，不属于 RackCard 函数体。
// 修改方式：在完整 Channels.tsx 源码中检查该主题类，而不是只检查 RackCard 截取片段。
// 目的：避免测试范围错误导致已经生效的中心文字类被误判为缺失。
assert.match(channelsSource, /text-sky-700 dark:text-sky-100/, 'OAuth 双环中心文字应保留浅色和深色主题文字类');

console.log('rack mode theme regression passed');
// 修改原因：当前部署环境的 Node 18 在部分 ESM 脚本自然结束后可能触发 Aborted。
// 修改方式：断言全部通过后显式以 0 退出，断言失败时仍会在这里之前抛出错误。
// 目的：让测试退出码只反映本文件断言是否通过。
process.exit(0);
