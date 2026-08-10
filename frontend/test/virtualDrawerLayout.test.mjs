import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const source = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/VirtualModels.tsx'), 'utf8');

// 修改原因：虚拟模型编辑从顶部内联画布迁移到右侧抽屉，列表卡片只负责折叠展示。
// 修改方式：直接读取 VirtualModels.tsx 源码，断言抽屉结构、桌面折叠面板和移动端渠道面板仍然存在。
// 目的：防止虚拟模型编辑布局回退到占满主页面顶部。
assert.match(source, /虚拟模型编辑从顶部内联画布迁移到抽屉/, '虚拟模型编辑应保留抽屉布局说明');
assert.match(source, /<Dialog\.Root open=\{isVirtualModalOpen\}/, '虚拟模型编辑应使用 Dialog 抽屉');
assert.match(source, /fixed right-0 top-0 h-full w-full xl:w-\[1040px\]/, '虚拟模型编辑抽屉应从右侧滑入');
assert.match(source, /isVirtualProviderPanelCollapsed/, '桌面端应保留渠道面板折叠状态');
assert.match(source, /isVirtualMobileProviderPanelOpen/, '移动端应保留渠道面板展开状态');
assert.match(source, /渠道面板/, '虚拟模型编辑应保留渠道面板文案');
assert.match(source, /title="展开渠道面板"/, '桌面端应保留展开渠道面板按钮');
assert.match(source, /title="收起渠道面板"/, '桌面端应保留收起渠道面板按钮');

console.log('virtual drawer layout regression passed');
process.exit(0);
