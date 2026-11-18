import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const rendererSource = readFileSync(path.resolve(frontendRoot, 'src/components/MarkdownRenderer.tsx'), 'utf8');

assert.match(rendererSource, /import ReactMarkdown, \{ defaultUrlTransform \} from 'react-markdown';/, 'MarkdownRenderer 应使用 react-markdown 渲染 markdown');
assert.match(rendererSource, /import remarkGfm from 'remark-gfm';/, 'MarkdownRenderer 应启用 remark-gfm');
assert.match(rendererSource, /import remarkMath from 'remark-math';/, 'MarkdownRenderer 应启用 remark-math');
assert.match(rendererSource, /import rehypeKatex from 'rehype-katex';/, 'MarkdownRenderer 应启用 rehype-katex');
assert.doesNotMatch(rendererSource, /rehype-raw|rehypeRaw|allowDangerousHtml|dangerouslySetInnerHTML/, 'MarkdownRenderer 不得挂载原始 HTML 解析能力');

assert.match(rendererSource, /export function MarkdownRenderer\(\{ content, className = '', tone = 'default' \}: MarkdownRendererProps\)/, 'MarkdownRenderer 的公开 props 接口应保持不变');
assert.match(rendererSource, /remarkPlugins=\{\[remarkGfm, remarkMath\]\}/, 'ReactMarkdown 应挂载 GFM 和 math remark 插件');
assert.match(rendererSource, /rehypePlugins=\{\[rehypeKatex\]\}/, 'ReactMarkdown 应仅挂载 KaTeX rehype 插件');
assert.match(rendererSource, /components=\{components\}/, 'MarkdownRenderer 应通过 components prop 迁移现有样式');
assert.match(rendererSource, /function CodeBlock\(\{ code, language, tone \}/, '代码块复制按钮组件应保留');
assert.match(rendererSource, /navigator\.clipboard\.writeText\(code\)/, '代码块仍应支持复制到剪贴板');
assert.match(rendererSource, /data-footnote-ref/, '脚注引用应保留主题样式入口');
assert.match(rendererSource, /data-footnotes/, '脚注区域应保留主题样式入口');
assert.match(rendererSource, /task-list-item/, '任务列表项应保留样式入口');

assert.doesNotMatch(rendererSource, /function parseBlocks\(/, '不应保留手写块级 parser');
assert.doesNotMatch(rendererSource, /function renderInline\(/, '不应保留手写行内 parser');
assert.doesNotMatch(rendererSource, /katex\.renderToString/, 'KaTeX 渲染应交给 rehype-katex');
assert.doesNotMatch(rendererSource, /extractFootnotes/, '脚注应交给 remark-gfm');

const bundle = await build({
  stdin: {
    resolveDir: frontendRoot,
    loader: 'tsx',
    contents: `
      import React from 'react';
      import { renderToStaticMarkup } from 'react-dom/server';
      import { MarkdownRenderer } from './src/components/MarkdownRenderer';

      const sample = [
        '# 示例标题',
        '',
        '公式 $E = mc^2$ 与表格：',
        '',
        '| A | B |',
        '| - | - |',
        '| 1 | 2 |',
        '',
        '- [x] 已完成任务',
        '',
        '[伪协议链接](javascript:void(0))',
        '',
        '<iframe srcdoc="<script>void 0</script>"></iframe>',
        '<script>void 0</script>',
        '<img src="x" onerror="void 0" />',
        '<meta http-equiv="refresh" content="0;url=https://example.org" />',
        '<form action="https://example.org"><input name="demo" /></form>',
        '<style>body { opacity: 1; }</style>',
      ].join('\\n');

      console.log(renderToStaticMarkup(<MarkdownRenderer content={sample} />));
    `,
  },
  bundle: true,
  write: false,
  platform: 'node',
  format: 'cjs',
  loader: { '.css': 'empty', '.woff': 'empty', '.woff2': 'empty', '.ttf': 'empty' },
  logLevel: 'silent',
});

const rendered = execFileSync(process.execPath, ['-'], {
  input: bundle.outputFiles[0].text,
  cwd: frontendRoot,
  maxBuffer: 4 * 1024 * 1024,
}).toString();

assert.match(rendered, /<h1[^>]*>示例标题<\/h1>/, '正常 Markdown 标题应渲染');
assert.match(rendered, /katex/, 'KaTeX 数学公式应正常渲染');
assert.match(rendered, /<table/, 'GFM 表格应正常渲染');
assert.match(rendered, /type="checkbox"/, 'GFM 任务列表复选框应正常渲染');
assert.match(rendered, /&lt;iframe srcdoc=/, '内嵌 iframe 标签应转义为纯文本');
assert.match(rendered, /&lt;img src=&quot;x&quot; onerror=&quot;void 0&quot; \/&gt;/, '内嵌 img 标签应转义为纯文本');
assert.doesNotMatch(rendered, /<iframe\b|<script\b|<meta\b|<form\b|<style\b|<img\b|href="javascript:/i, '不得生成未转义的内嵌标签或脚本协议链接');

console.log('markdown renderer react-markdown regression passed');
process.exit(0);
