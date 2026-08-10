import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const virtualLibSource = readFileSync(path.resolve(frontendRoot, 'src/lib/virtualModels.ts'), 'utf8');
const virtualHookSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useVirtualModels.tsx'), 'utf8');

// 修改原因：Channels.tsx 拆分后，虚拟路由手风琴实现位于 pages/channels/hooks/useVirtualModels.tsx，
//   虚拟 provider 条目 helper 位于 lib/virtualModels.ts。
// 修改方式：按新文件位置做源码回归断言。
// 目的：防止虚拟路由手风琴、虚拟条目构建或虚拟路由测试入口被意外移除。

assert.match(virtualLibSource, /export function buildVirtualProviderEntries\(/, '应保留虚拟 provider 条目构建 helper');
assert.match(virtualLibSource, /export function isVirtualProviderEntry\(/, '应保留虚拟 provider 条目类型守卫');

assert.match(virtualHookSource, /const filteredVirtualProviderEntries/, '渠道 hook 应提供过滤后的虚拟 provider 条目');
assert.match(virtualHookSource, /const renderDesktopVirtualRoutesAccordionRows/, '桌面端虚拟路由应手风琴行渲染');
assert.match(virtualHookSource, /const renderMobileVirtualRoutesAccordion/, '移动端虚拟路由应手风琴渲染');

// 虚拟路由测试入口：应构建临时 provider 并打开统一测试弹窗。
assert.match(virtualHookSource, /const openVirtualRouteTestDialog = \(entries: VirtualProviderEntry\[\]\) => \{/, '应保留虚拟路由测试弹窗入口');
assert.match(virtualHookSource, /buildVirtualRouteTestProvider\(entries\)/, '虚拟路由测试应构建临时 provider');
assert.match(virtualLibSource, /export function buildVirtualRouteTestProvider\(/, '应保留虚拟路由测试 provider 构建 helper');
assert.match(virtualLibSource, /_virtual_route_test: true/, '虚拟路由测试 provider 应带临时标记');

console.log('virtual accordion layout regression passed');
process.exit(0);
