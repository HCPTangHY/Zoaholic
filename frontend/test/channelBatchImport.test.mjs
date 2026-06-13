import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const editorSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/ChannelEditor.tsx'), 'utf8');
const batchImportSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/lib/batchImport.ts'), 'utf8');
const packageJson = JSON.parse(readFileSync(path.resolve(frontendRoot, 'package.json'), 'utf8'));
const headerStart = editorSource.indexOf('API Keys 标题栏按钮过多');
const headerEnd = editorSource.indexOf('<div data-key-scroll');
const headerSource = editorSource.slice(headerStart, headerEnd);

// 修改原因：API Keys 标题栏按钮已调整为高频按钮可见、低频按钮下拉，后续重构不能再退回横向拥挤布局。
// 修改方式：直接读取源码，断言标题栏保留添加密钥、余额和多 key 测试，同时存在 MoreHorizontal 菜单。
// 目的：固定本次按钮布局调整，避免复制、清空和布局切换再次挤回标题栏。
assert.match(headerSource, /添加密钥/, '标题栏应保留“添加密钥”高频按钮');
assert.match(headerSource, /余额/, '标题栏应保留“余额”高频按钮');
assert.match(headerSource, /多key测试/, '标题栏应保留“多key测试”高频按钮');
assert.match(headerSource, /MoreHorizontal/, '标题栏应使用 MoreHorizontal 下拉菜单承载低频操作');
assert.match(headerSource, /复制全部/, '更多菜单应包含“复制全部”');
assert.match(headerSource, /清空/, '更多菜单应包含“清空”');
assert.match(headerSource, /完整行模式|机房模式/, '更多菜单应包含布局切换');
assert.match(editorSource, /document\.addEventListener\('click', closeOnOutsideClick\)/, '更多菜单应支持点击外部关闭');

// 修改原因：列表末尾操作区现在分为空状态和已有 Key 两种布局，不能再出现旧 placeholder 或重复的底部添加按钮。
// 修改方式：断言空状态渲染批量入口和 BYOK，有 Key 时只渲染批量导入或批量粘贴入口。
// 目的：保证列表末尾操作区与标题栏分工明确。
assert.match(editorSource, /renderEmptyKeyState/, '0 key 时应渲染空状态操作区');
assert.match(editorSource, /暂无密钥/, '空状态应显示“暂无密钥”');
assert.match(editorSource, /\* BYOK/, '空状态应保留 BYOK 快捷入口');
assert.match(editorSource, /renderKeyListBatchRow/, '有 key 时应渲染列表底部批量入口');
assert.match(editorSource, /renderRackActionCard/, '机房模式应在 RackGrid 末尾渲染特殊操作卡片');
assert.match(editorSource, /border-2 border-dashed border-border/, '机房模式特殊卡片应使用虚线边框');

// 修改原因：OAuth 批量导入依赖前端解析 JSON 和 zip 文件，缺少 JSZip 会导致 zip 上传入口运行时失败。
// 修改方式：断言依赖已写入 package.json，编辑器导入 JSZip，并调用后端批量导入端点。
// 目的：确保 .zip 导入链路从依赖到接口调用都存在。
assert.equal(typeof packageJson.dependencies?.jszip, 'string', 'package.json dependencies 应包含 jszip');
assert.match(editorSource, /import JSZip from 'jszip'/, 'ChannelEditor 应导入 JSZip 处理 zip 文件');
assert.match(editorSource, /accept="\.json,\.zip"/, '文件上传应接受 .json 和 .zip');
assert.match(editorSource, /\/v1\/oauth\/batch_import/, 'OAuth 批量导入应调用后端 batch_import 接口');

// 修改原因：前端只做预检和预览，具体保存仍由后端 normalize；预览格式识别必须覆盖需求中的三类输入。
// 修改方式：断言批量导入工具包含 sub2api、CPA 单文件、CPA 批量数组和普通 Key 分隔符解析逻辑。
// 目的：防止后续修改只支持其中一种格式，导致面板预览与后端支持范围不一致。
assert.match(batchImportSource, /detectOAuthBatchFormat/, '应有 OAuth 批量格式检测函数');
assert.match(batchImportSource, /检测到 sub2api 格式/, '应识别 sub2api accounts 数组');
assert.match(batchImportSource, /检测到 CPA 单文件/, '应识别 CPA 单文件');
assert.match(batchImportSource, /检测到 CPA 批量文件/, '应识别 CPA 批量数组');
assert.match(batchImportSource, /parsePlainKeyBatch/, '应有普通 Key 批量粘贴解析函数');
assert.match(batchImportSource, /delimiterMode === 'custom'/, '普通 Key 粘贴应支持自定义分隔符');
