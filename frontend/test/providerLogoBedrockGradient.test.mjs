import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, symlinkSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

// 修改原因：AWS Bedrock 图标需要恢复带 linearGradient 的 SVG，但固定 defs id 在多个渠道卡片同时出现时会发生整页级冲突。
// 修改方式：把 ProviderLogos.tsx 临时编译为可导入模块，并一次渲染两个 Bedrock 图标检查 gradient id 与 fill 引用。
// 目的：防止后续维护再次使用固定 id，导致第二个或后续 Bedrock 图标只显示背景不显示图案。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const tempDir = mkdtempSync(path.join(os.tmpdir(), 'zoaholic-provider-logo-'));
writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');
// 修改原因：临时编译目录不在 frontend 下，Node ESM 默认无法从那里解析 react 和 react-dom。
// 修改方式：把 frontend/node_modules 软链接到临时目录，只改变测试沙箱内的模块解析路径。
// 目的：让测试渲染真实 React 组件，同时不改动项目依赖。
symlinkSync(path.join(frontendRoot, 'node_modules'), path.join(tempDir, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir');

execFileSync(process.execPath, [
  path.join(frontendRoot, 'node_modules/typescript/bin/tsc'),
  '--target', 'ES2020',
  '--module', 'ES2020',
  '--moduleResolution', 'Bundler',
  '--jsx', 'react-jsx',
  '--rootDir', path.join(frontendRoot, 'src'),
  '--outDir', tempDir,
  '--noEmit', 'false',
  '--skipLibCheck', 'true',
  path.join(frontendRoot, 'src/components/ProviderLogos.tsx'),
], { cwd: frontendRoot, stdio: 'pipe' });

const { ProviderLogo } = await import(pathToFileURL(path.join(tempDir, 'components/ProviderLogos.js')).href);
const markup = renderToStaticMarkup(React.createElement(
  'div',
  null,
  React.createElement(ProviderLogo, { name: 'aws bedrock', engine: 'aws' }),
  React.createElement(ProviderLogo, { name: 'aws bedrock backup', engine: 'aws' }),
));

const gradientIds = [...markup.matchAll(/<linearGradient[^>]*id="([^"]+)"/g)].map(match => match[1]);
const fillRefs = [...markup.matchAll(/fill="url\(#([^)]+)\)"/g)].map(match => match[1]);

assert.equal(gradientIds.length, 2, '两个 Bedrock 图标都应该渲染独立 linearGradient 定义');
assert.equal(new Set(gradientIds).size, 2, '多个 Bedrock 图标不能复用同一个 gradient id');
assert.deepEqual(fillRefs, gradientIds, '每个 path 的 fill 都应该引用同一个 SVG 实例内的 gradient id');
assert.ok(gradientIds.every(id => id.startsWith('bedrock-grad-')), 'gradient id 应该保留 Bedrock 语义前缀便于排查');
assert.ok(gradientIds.every(id => /^[A-Za-z][A-Za-z0-9_-]*$/.test(id)), 'gradient id 应该清理成 SVG 友好的字符');

console.log('provider logo Bedrock gradient id regression passed');
// 修改原因：当前部署环境的 Node 18 在部分 ESM 脚本自然结束后可能触发 Aborted。
// 修改方式：断言全部通过后显式以 0 退出，断言失败时仍会在这里之前抛出错误。
// 目的：让测试退出码只反映 Bedrock 渐变 id 回归断言是否通过。
process.exit(0);
