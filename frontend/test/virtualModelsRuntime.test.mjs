import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { fileURLToPath } from 'node:url';

// 修改原因：渠道面板的排序会直接影响移动端展开后的可用性，单靠源码字符串检查无法验证子渠道是否跟随主渠道。
// 修改方式：测试运行时临时编译 virtualModels.ts，再调用导出的纯函数检查主渠道权重排序和子渠道跟随规则。
// 目的：在不引入测试框架的情况下，覆盖虚拟模型渠道面板的真实排序行为。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const tempDir = mkdtempSync(path.join(os.tmpdir(), 'zoaholic-virtual-models-'));
writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');

execFileSync(process.execPath, [
  path.join(frontendRoot, 'node_modules/typescript/bin/tsc'),
  '--target', 'ES2020',
  '--module', 'ES2020',
  '--moduleResolution', 'Bundler',
  '--rootDir', path.join(frontendRoot, 'src'),
  '--outDir', tempDir,
  '--noEmit', 'false',
  '--skipLibCheck', 'true',
  path.join(frontendRoot, 'src/lib/virtualModels.ts'),
], { cwd: frontendRoot, stdio: 'pipe' });

const helpers = await import(pathToFileURL(path.join(tempDir, 'lib/virtualModels.js')).href);
const { buildProviderListItems, buildVirtualProviderEntries, buildVirtualProviderPanelItems, buildVirtualRouteTestProvider } = helpers;

assert.equal(typeof buildVirtualProviderPanelItems, 'function', '应该导出渠道面板排序 helper');
assert.equal(typeof buildProviderListItems, 'function', '应该导出真实渠道列表 helper');
assert.equal(typeof buildVirtualProviderEntries, 'function', '应该导出虚拟模型 entries helper');
assert.equal(typeof buildVirtualRouteTestProvider, 'function', '应该导出虚拟路由测试 provider helper');

const providers = [
  {
    provider: 'middle-parent',
    enabled: true,
    preferences: { weight: 50 },
    sub_channels: [
      { engine: 'low-child', enabled: true, preferences: { weight: 5 }, model: ['a'] },
      { engine: 'high-child', enabled: true, preferences: { weight: 90 }, model: ['b'] },
    ],
  },
  { provider: 'top-parent', enabled: true, preferences: { weight: 100 }, sub_channels: [] },
  { provider: 'disabled-parent', enabled: false, preferences: { weight: 200 }, sub_channels: [] },
  { provider: 'bottom-parent', enabled: true, preferences: { weight: 10 }, sub_channels: [] },
];

const panelItems = buildVirtualProviderPanelItems(providers);

assert.deepEqual(
  panelItems.map(item => item.provider),
  ['top-parent', 'middle-parent', 'middle-parent:high-child', 'middle-parent:low-child', 'bottom-parent'],
  '渠道面板应该先按主渠道 weight 降序，再把子渠道按 weight 降序跟在所属主渠道后面',
);
assert.equal(panelItems.some(item => item.provider === 'disabled-parent'), false, '禁用渠道不应该出现在可拖拽渠道面板中');

// 修改原因：虚拟模型现在由手风琴单独渲染，主渠道列表不能再包含 _isVirtual 伪行。
// 修改方式：运行时断言 buildProviderListItems 只返回真实渠道，同时 buildVirtualRouteTestProvider 单独生成测试快照。
// 目的：防止后续修改重新把虚拟模型混入 segments，导致不活跃折叠和真实渠道操作误处理虚拟模型。
const virtualModels = {
  'deepseek-chat': { enabled: true, chain: [{ type: 'model', value: 'deepseek-chat' }] },
  'disabled-route': { enabled: false, chain: [] },
};
const listItems = buildProviderListItems(providers, virtualModels);
assert.deepEqual(
  listItems.map(item => item.p.provider),
  ['disabled-parent', 'top-parent', 'middle-parent', 'bottom-parent'],
  '主列表 helper 应该只返回真实渠道并按权重降序排列',
);
assert.equal(listItems.some(item => item.p._isVirtual), false, '主列表 helper 不应该返回虚拟伪渠道');

const virtualEntries = buildVirtualProviderEntries(virtualModels);
const testProvider = buildVirtualRouteTestProvider(virtualEntries);
assert.equal(testProvider._virtual_route_test, true, '虚拟测试 provider 应该携带后端识别标记');
assert.deepEqual(testProvider.model, ['deepseek-chat', 'disabled-route'], '虚拟测试 provider 应该把虚拟模型名放入 model 列表');
assert.deepEqual(Object.keys(testProvider.preferences.virtual_models), ['deepseek-chat', 'disabled-route'], '虚拟测试 provider 应该携带虚拟模型配置快照');

console.log('virtual model provider panel ordering passed');
// 修改原因：当前部署环境的 Node 18 在部分 ESM 脚本自然结束后会触发 Aborted。
// 修改方式：断言全部通过后显式以 0 退出，断言失败时仍会在这里之前抛出错误。
// 目的：让测试退出码只反映排序断言是否通过。
process.exit(0);
