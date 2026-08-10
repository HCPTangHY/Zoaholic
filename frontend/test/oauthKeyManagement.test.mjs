import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const repoRoot = path.resolve(frontendRoot, '..');
const editorSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/ChannelEditor.tsx'), 'utf8');
const fullRowSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/FullKeyRow.tsx'), 'utf8');
const oauthHookSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useChannelOAuth.ts'), 'utf8');
const editorHookSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/hooks/useChannelEditor.tsx'), 'utf8');
const keyComponentsSource = readFileSync(path.resolve(frontendRoot, 'src/pages/channels/components/KeyComponents.tsx'), 'utf8');
const claudeCodeSource = readFileSync(path.resolve(repoRoot, 'core/channels/claude_code_channel.py'), 'utf8');

// 修改原因：Channels.tsx 拆分后，OAuth Key 管理相关代码分布在 ChannelEditor/FullKeyRow/useChannelOAuth/useChannelEditor。
// 修改方式：按新文件位置做源码回归断言。
// 目的：防止 OAuth 渠道的 Key 行退化成普通 Key 输入框，同时避免额度显示回退为旧 quota_5h/quota_7d 或丢失 defer 渲染。

assert.match(fullRowSource, /import \{ ClipboardPaste, LogIn,/, 'FullKeyRow 应导入 OAuth 导入和登录按钮图标');

// UiSlot 应支持 fallbackText，用于 OAuth 占位符插槽无插件时的默认文案。
assert.match(keyComponentsSource, /fallbackText/, 'UiSlot 应支持 fallbackText');

// OAuth Key 行应保留导入/登录按钮和 OAuth 占位符。
assert.match(fullRowSource, /onClick=\{\(\) => openImportModal\(idx\)\}/, 'OAuth Key 行应保留导入按钮');
assert.match(fullRowSource, /onClick=\{\(\) => void startOAuthLogin\(idx\)\}/, 'OAuth Key 行应保留登录按钮');
assert.match(fullRowSource, /placeholder=\{isOAuthEngine \? '邮箱或标识符' : 'sk-\.\.\.'\}/, 'OAuth Key 输入框应使用 OAuth 占位符');

// 删除 OAuth 账号必须先确认并调用账号删除接口，不能只删表单里的 Key。
assert.match(editorHookSource, /window\.confirm\(`确定要删除 OAuth 账号 \$\{keyValue\} 吗？/, '删除 OAuth Key 时应先确认');
assert.match(editorHookSource, /apiFetch\(`\/v1\/oauth\/accounts\/\$\{encodeURIComponent\(keyValue\)\}\?provider=\$\{encodeURIComponent\(providerName\)\}`, \{\s*method: 'DELETE'/, '删除 OAuth Key 时应调用 OAuth 账号删除接口');

// Claude Code 渠道应注册额度显示脚本，并通过 ui_slots.quota_display 暴露给前端。
assert.match(claudeCodeSource, /CC_QUOTA_DISPLAY = """/, 'Claude Code 渠道应注册额度显示脚本');
assert.match(claudeCodeSource, /"quota_display": CC_QUOTA_DISPLAY/, 'Claude Code 渠道应通过 ui_slots.quota_display 暴露额度显示脚本');

// OAuth 账号重命名应通过 blur 比较并调用 rename 接口。
assert.match(oauthHookSource, /const handleOAuthKeyFocus = \(idx: number, keyStr: string\) => \{/, '应保留 OAuth focus 快照逻辑');
assert.match(oauthHookSource, /const handleOAuthKeyBlur = async \(idx: number, newValue: string\) => \{/, '应保留 OAuth blur 重命名逻辑');
assert.match(oauthHookSource, /apiFetch\(`\/v1\/oauth\/accounts\/\$\{encodeURIComponent\(oldValue\)\}\/rename`/, 'OAuth 账号重命名应调用 rename 接口');

// 打开 OAuth 编辑面板后应自动补查缺失的额度信息。
assert.match(oauthHookSource, /account\?\.status === 'active'/, '自动额度补查应只处理 active 账号');
assert.match(oauthHookSource, /accountQuota\?\.quota_inner == null/, '自动额度补查应只处理缺少 quota_inner 的账号');
assert.match(oauthHookSource, /accountQuota\?\.quota_outer == null/, '自动额度补查应只处理缺少 quota_outer 的账号');
assert.match(oauthHookSource, /!account\._quota_loading/, '自动额度补查应跳过正在加载的账号');
assert.match(oauthHookSource, /!account\._quota_unavailable/, '自动额度补查应跳过已知不可用的账号');
assert.match(oauthHookSource, /apiFetch\('\/v1\/channels\/balance', \{\s*method: 'POST'/, '自动额度补查应调用统一余额接口');

// ChannelEditor 应保留 OAuth 同步与导出入口。
assert.match(editorSource, /同步 OAuth 账号/, 'ChannelEditor 应保留同步 OAuth 账号按钮');
assert.match(editorSource, /导出全部凭证/, 'ChannelEditor 应保留导出全部凭证按钮');

console.log('oauth key management regression passed');
process.exit(0);
