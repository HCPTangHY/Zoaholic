import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const source = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useChannelEditor.tsx'), 'utf8');

// 修改原因：Channels.tsx 拆分后，子渠道编辑/保存逻辑位于 hooks/useChannelEditor.tsx。
// 修改方式：直接读取 hook 源码，断言子渠道编辑入口、载荷构建和保存分支仍然存在且行为一致。
// 目的：防止子渠道保存再次退化为整体 channels 替换或丢失父渠道/provider 更新语义。
assert.match(source, /const updateFormData = \(field: keyof ProviderFormData, value: any\) => \{[\s\S]*setFormData\(prev => prev \? \{ \.\.\.prev, \[field\]: value \} : prev\);/, 'updateFormData 应保留通用表单字段更新');

const start = source.indexOf('const openSubChannelEdit = async');
assert.notEqual(start, -1, '应保留 openSubChannelEdit');
const editSection = source.slice(start, start + 4000);
assert.match(editSection, /setEditingSubChannel\(\{ parentIdx, subIdx \}\)/, '子渠道编辑时应记录编辑上下文');
assert.match(editSection, /await openModal\(\{/, '子渠道编辑时应复用 openModal 构建表单');
assert.match(editSection, /await apiFetch\(buildProviderApiPath\(providerId\), \{ method: 'GET'/, '子渠道编辑前应先 GET 父渠道最新数据');

const saveStart = source.indexOf('if (editingSubChannel) {');
assert.notEqual(saveStart, -1, '保存逻辑应保留子渠道编辑分支');
const saveSection = source.slice(saveStart, saveStart + 4000);
assert.match(saveSection, /const subObj: any = \{ engine: formData\.engine, model: finalModels\.length > 0 \? finalModels : undefined, enabled: formData\.enabled \};/, '子渠道编辑保存时应构建子渠道对象');
assert.match(saveSection, /newProviders\[parentIdx\] = \{ \.\.\.parent, sub_channels: subs \};/, '子渠道编辑保存时应更新父渠道 sub_channels');
assert.match(saveSection, /await apiFetch\(buildProviderApiPath\(subChannelParentProviderId\), \{ method: 'PUT'/, '子渠道编辑保存时应调用 provider PUT');
assert.match(saveSection, /body: JSON\.stringify\(newProviders!\[editingSubChannel\.parentIdx\]\)/, '子渠道编辑保存时应提交更新后的父渠道对象');
assert.match(saveSection, /await apiFetch\(providerSavePath, \{ method: providerSaveMethod/, '普通渠道保存应走 providerSavePath/providerSaveMethod');
assert.match(saveSection, /setIsModalOpen\(false\)/, '保存成功后应关闭编辑弹窗');
assert.match(saveSection, /setEditingSubChannel\(null\)/, '保存成功后应清空子渠道编辑上下文');

const deleteStart = source.indexOf('const handleDeleteSubChannel = async');
assert.notEqual(deleteStart, -1, '应保留 handleDeleteSubChannel');
const deleteSection = source.slice(deleteStart, deleteStart + 1800);
assert.match(deleteSection, /\(parent\.sub_channels \|\| \[\]\)\.filter\(\(_: any, i: number\) => i !== subIdx\)/, '删除子渠道时应从父渠道 sub_channels 中移除');
assert.match(deleteSection, /sub_channels: subs\.length > 0 \? subs : undefined/, '删除后无剩余子渠道时应清空 sub_channels');
assert.match(deleteSection, /apiFetch\(buildProviderApiPath\(providerId\), \{\s*method: 'PUT'/, '删除子渠道时应调用 provider PUT');

console.log('sub channel provider put regression passed');
process.exit(0);
