import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const componentSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/VirtualModels.tsx'), 'utf8');
const hookSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useVirtualModels.tsx'), 'utf8');

// 修改原因：移动端无法稳定使用 HTML5 拖拽排序，虚拟模型链条编辑器需要提供按钮式上移/下移。
// 修改方式：虚拟模型抽屉组件位于 components/VirtualModels.tsx，链条操作 hook 位于 hooks/useVirtualModels.tsx。
// 目的：固定移动端虚拟链条排序能力，防止后续重构只保留拖拽。

assert.match(componentSource, /max-h-\[50vh\] overflow-y-auto/, '移动端渠道面板应限制高度并可滚动');
assert.match(componentSource, /hidden xl:block/, '桌面端应保留独立渠道面板');
assert.match(componentSource, /<ChevronUp className="w-4 h-4" \/>/, '虚拟链条节点应提供上移按钮');
assert.match(componentSource, /<ChevronDown className="w-4 h-4" \/>/, '虚拟链条节点应提供下移按钮');
assert.match(componentSource, /disabled=\{idx === 0\}/, '首个节点的上移按钮应禁用');
assert.match(componentSource, /disabled=\{idx === virtualEditorChain\.length - 1\}/, '末尾节点的下移按钮应禁用');
assert.match(componentSource, /virtual-editor-\$\{idx\}/, '虚拟链条节点应保留稳定 key');
assert.match(componentSource, /'__virtual_editor__'/, '虚拟链条拖拽应保留虚拟编辑器标识');
assert.match(componentSource, /virtualAddNodeTypes/, '虚拟链条编辑器应保留新增节点类型选择');
assert.match(componentSource, /appendVirtualEditorNodeByType/, '虚拟链条编辑器应保留按类型追加节点按钮');

// 上移/下移应通过交换实现，越界时保持原链条。
assert.match(hookSource, /const swapVirtualEditorNode = \(idx: number, direction: -1 \| 1\) => \{/, '应保留虚拟链条节点交换函数');
assert.match(hookSource, /\[next\[idx\], next\[targetIdx\]\] = \[next\[targetIdx\], next\[idx\]\];/, '上移/下移应通过交换相邻节点实现');
assert.match(hookSource, /if \(idx < 0 \|\| idx >= prev\.length \|\| targetIdx < 0 \|\| targetIdx >= prev\.length\) return prev;/, '交换越界时应保持原链条');

console.log('virtual editor mobile reorder regression passed');
process.exit(0);
