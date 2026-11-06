import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import ts from 'typescript';

// 直接运行页面的请求处理函数，用本地 Response 覆盖 HTTP 和 SSE 错误。
const source = readFileSync(new URL('../src/pages/Playground.tsx', import.meta.url), 'utf8');
const start = source.indexOf('  const sendMessage = async');
const end = source.indexOf('  const handleSend =', start);
assert.ok(start >= 0 && end > start);
const { outputText } = ts.transpileModule(source.slice(start, end), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
});
const createSendMessage = new Function('env', `
  const { token, selectedModel, systemPrompt, temperature, stream, getSelectedPlaygroundApi,
    serializeMessage, setMessages, setIsGenerating, fetch } = env;
  ${outputText}
  return sendMessage;
`);

async function send(response, stream = false, history = [{ role: 'user', content: 'hello' }]) {
  let messages = history;
  let generating;
  let request;
  const sendMessage = createSendMessage({
    token: 'test-token', selectedModel: 'test-model', systemPrompt: 'Be helpful', temperature: 0.7, stream,
    getSelectedPlaygroundApi: () => '',
    serializeMessage: ({ role, content }) => ({ role, content }),
    setMessages: next => { messages = typeof next === 'function' ? next(messages) : next; },
    setIsGenerating: value => { generating = value; },
    fetch: async (_url, options) => { request = JSON.parse(options.body); return response; },
  });
  await sendMessage(history);
  assert.equal(generating, false);
  return { messages, request };
}

const error = { message: 'Quota exceeded', type: 'rate_limit_error', code: 'insufficient_quota', param: 'model' };

test('HTTP 错误保留状态、嵌套详情及请求 ID', async () => {
  const payload = { error, request_id: 'req-test', details: { retry_after: 30 } };
  const { messages } = await send(Response.json(payload, { status: 429, statusText: 'Too Many Requests' }));
  assert.equal(messages.at(-1).content, `请求失败：HTTP 429 Too Many Requests\n\n${JSON.stringify(payload, null, 2)}`);
});

test('代理 HTML、文本和空响应仍显示 HTTP 状态', async () => {
  for (const body of ['<h1>Bad Gateway</h1>', 'upstream timed out', '']) {
    const { messages } = await send(new Response(body, { status: 502 }));
    assert.equal(messages.at(-1).content, `请求失败：HTTP 502${body ? '\n\n' + body : ''}`);
  }
});

test('SSE 两种错误信封保留全部字段及已生成内容', async () => {
  for (const payload of [{ error, status_code: 429 }, { choices: [{ error }] }]) {
    const delta = { choices: [{ delta: { content: 'partial answer', reasoning_content: 'thinking' } }] };
    const body = `data: ${JSON.stringify(delta)}\n\ndata: ${JSON.stringify(payload)}\n\n`;
    const { messages } = await send(new Response(body), true);
    assert.deepEqual(messages[1], { role: 'assistant', content: 'partial answer', reasoning_content: 'thinking', isTyping: false });
    assert.equal(messages[2].content, `请求失败：${JSON.stringify(payload, null, 2)}`);
  }
});

test('非流式 HTTP 200 中的错误信封也显示原始错误', async () => {
  for (const payload of [{ error }, { choices: [{ error }] }]) {
    const { messages } = await send(Response.json(payload));
    assert.equal(messages.at(-1).content, `请求失败：${JSON.stringify(payload, null, 2)}`);
  }
});

test('正常回复保持不变，重试不把本地错误提示发给模型', async () => {
  const user = { role: 'user', content: 'hello' };
  const { messages, request } = await send(Response.json({ choices: [{ message: { content: 'world' } }] }), false,
    [user, { role: 'system', content: '请求失败：old diagnostic' }]);
  assert.deepEqual(messages.at(-1), { role: 'assistant', content: 'world' });
  assert.deepEqual(request.messages, [{ role: 'system', content: 'Be helpful' }, user]);
});

test('错误正文以纯文本展示，不交给 Markdown/HTML 渲染', () => {
  assert.match(source, /markdownRendering && msg\.role !== 'system'/);
});
