import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const source = readFileSync(path.resolve(frontendRoot, 'src/pages/Plugins.tsx'), 'utf8');

assert.match(source, /fd\.append\('admin_password',\s*adminPassword\)/, '上传插件请求应附带 admin_password 表单字段');
assert.match(source, /JSON\.stringify\(\{\s*admin_password:\s*adminPassword\s*\}\)/, '卸载插件请求应附带 admin_password JSON 字段');
assert.match(source, /type="password"/, '密码确认弹窗应使用 password 输入框');
assert.doesNotMatch(source, /confirm\(`确定要卸载并删除插件/, '卸载插件应改为密码确认弹窗而非浏览器原生 confirm');
assert.doesNotMatch(source, /toastSuccess\([^)]*(失败|网络错误)/, '错误分支不应误调用 toastSuccess');

console.log('plugins password confirmation checks passed');
