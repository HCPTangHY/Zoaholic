import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：插件参数从自由文本升级为 metadata.params_schema 驱动的可视化表单，需要防止后续重构遗漏任一接入点。
// 修改方式：用轻量源码断言检查共享类型、解析序列化函数、五类控件、visible_when 和两个使用位置。
//   原 Python 脚本改写为 .mjs，与其它前端测试统一由 node --test 收集。
// 目的：在没有前端测试框架的项目里，为本次 UI 结构提供可重复的最低成本回归检查。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const formSource = readFileSync(path.resolve(frontendRoot, 'src/components/PluginParamsForm.tsx'), 'utf8');
const pipelineSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/PipelineView.tsx'), 'utf8');
const sheetSource = readFileSync(path.resolve(frontendRoot, 'src/components/InterceptorSheet.tsx'), 'utf8');

assert.ok(formSource.includes('export interface ParamSchema'), '应导出 ParamSchema 类型');
assert.ok(formSource.includes('parsePluginOptions'), '应导出 parsePluginOptions');
assert.ok(formSource.includes('serializePluginOptions'), '应导出 serializePluginOptions');
assert.ok(formSource.includes('PluginParamsForm'), '应导出 PluginParamsForm 组件');
assert.ok(formSource.includes('visible_when'), '应支持 visible_when 条件显示');
assert.ok(formSource.includes('type="number"'), '应渲染 number 输入框');
assert.ok(formSource.includes('<select'), '应渲染 select 控件');
assert.ok(formSource.includes('Switch.Root'), '应渲染 toggle 开关');
assert.ok(formSource.includes('multiple'), '应渲染 multi-select 控件');
assert.ok(formSource.includes('key=value'), '应保留 key=value 模式说明');
assert.ok(formSource.includes('positional'), '应保留 positional 模式说明');

assert.ok(pipelineSource.includes('PluginParamsForm'), 'PipelineView 应使用可视化参数表单');
assert.ok(pipelineSource.includes('metadata?.params_schema'), 'PipelineView 应读取 metadata.params_schema');
assert.ok(sheetSource.includes('PluginParamsForm'), 'InterceptorSheet 应使用可视化参数表单');
assert.ok(sheetSource.includes('metadata?.params_schema'), 'InterceptorSheet 应读取 metadata.params_schema');

console.log('plugin params form source checks passed');
process.exit(0);
