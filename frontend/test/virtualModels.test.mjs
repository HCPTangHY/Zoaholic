import assert from 'node:assert/strict';
import { buildSync } from 'esbuild';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// 修改原因：本测试断言 src/lib/virtualModels.ts 的运行时行为，但项目没有 TS 测试 runner，原 .ts 测试文件从未能执行。
// 修改方式：用前端自带的 esbuild 把源码编译到临时目录，再动态 import；测试本体保持 .mjs，无需新增依赖。
// 目的：让虚拟模型排序、测试快照和链条摘要的行为级回归检查真实可运行。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = mkdtempSync(path.join(tmpdir(), 'virtualmodels-test-'));
buildSync({
  entryPoints: [path.resolve(__dirname, '../src/lib/virtualModels.ts')],
  outfile: path.join(outDir, 'virtualModels.mjs'),
  bundle: true,
  format: 'esm',
  platform: 'node',
});
const {
  buildProviderListItems,
  buildVirtualProviderEntries,
  buildVirtualProviderPanelItems,
  buildVirtualRouteTestProvider,
  summarizeVirtualChain,
} = await import(pathToFileURL(path.join(outDir, 'virtualModels.mjs')).href);

const virtualModels = {
  'deepseek-chat': {
    enabled: true,
    chain: [
      { type: 'model', value: 'deepseek-chat' },
      { type: 'channel', value: '打野硅基', model: 'deepseek-v4' },
    ],
  },
  'disabled-route': {
    enabled: false,
    chain: [],
  },
};

const providers = [
  { provider: 'low-weight', enabled: true, preferences: { weight: 5 }, engine: 'openai' },
  { provider: 'high-weight', enabled: true, preferences: { weight: 100 }, engine: 'openai' },
];

const virtualEntries = buildVirtualProviderEntries(virtualModels);
assert.equal(virtualEntries.length, 2);
assert.equal(virtualEntries[0]._isVirtual, true);
assert.equal(virtualEntries[0].preferences.weight, Infinity);
assert.equal(virtualEntries.find(item => item.provider === 'disabled-route')?.enabled, false);

const listItems = buildProviderListItems(providers, virtualModels);
assert.deepEqual(
  listItems.map(item => item.p.provider),
  ['high-weight', 'low-weight'],
);
assert.deepEqual(
  listItems.map(item => item.idx),
  [1, 0],
);

// 修改原因：虚拟模型不再作为主列表条目出现，但测试弹窗仍需要一次接收一个可列出模型名的 provider 快照。
// 修改方式：断言虚拟测试 provider 只包含虚拟模型名，并携带后端识别用的 _virtual_route_test 标记。
// 目的：保证标题行批量测试和子行单模型测试都能复用 ChannelTestDialog。
const virtualTestProvider = buildVirtualRouteTestProvider(virtualEntries);
assert.ok(virtualTestProvider);
assert.equal(virtualTestProvider._virtual_route_test, true);
assert.deepEqual(virtualTestProvider.model, ['deepseek-chat', 'disabled-route']);
assert.deepEqual(Object.keys(virtualTestProvider.preferences.virtual_models), ['deepseek-chat', 'disabled-route']);

assert.equal(
  summarizeVirtualChain(virtualModels['deepseek-chat'].chain, 'deepseek-chat'),
  'deepseek-chat → 打野硅基 → deepseek-v4',
);
assert.equal(summarizeVirtualChain([], 'empty'), '未配置链条');

// 修改原因：移动端渠道面板重新显示完整渠道列表后，排序必须比底部下拉更直观。
// 修改方式：主渠道按 weight 降序排列，子渠道按自身 weight 降序紧跟所属主渠道。
// 目的：避免子渠道被全局排序拆散，保证用户看到的渠道层级和优先级一致。
const providerPanelItems = buildVirtualProviderPanelItems([
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
]);
assert.deepEqual(
  providerPanelItems.map(item => item.provider),
  ['top-parent', 'middle-parent', 'middle-parent:high-child', 'middle-parent:low-child', 'bottom-parent'],
);

console.log('virtual model list helpers passed');
process.exit(0);
