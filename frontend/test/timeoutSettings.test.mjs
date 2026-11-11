import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(new URL('../src/lib/timeoutSettings.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 } });
const helpers = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const { readTimeoutValue, timeoutDraft, serializeTimeoutDraft, validateTimeoutDraft, writeTimeoutValue, resolveTimeoutPreview, CODE_TIMEOUT_DEFAULTS } = helpers;

const draft = (seconds, rows = []) => ({ mode: 'value', seconds, rows });
const row = (name, seconds, mode = 'value') => ({ name, seconds, mode });

test('numeric and ordered model configs roundtrip and keep explicit zero', () => {
  assert.equal(serializeTimeoutDraft(timeoutDraft(0, 'stream_header_timeout'), 'stream_header_timeout'), 0);
  assert.equal(serializeTimeoutDraft(timeoutDraft(12.5, 'stream_header_timeout'), 'stream_header_timeout'), 12.5);
  const value = { default: 60, 'gemini-4': 180, 'gemini-4-carbon': 0 };
  assert.deepEqual(serializeTimeoutDraft(timeoutDraft(value, 'stream_header_timeout'), 'stream_header_timeout'), value);
  assert.deepEqual(timeoutDraft(null, 'stream_header_timeout'), {mode:'inherit',seconds:'',rows:[]});
  assert.equal(serializeTimeoutDraft({mode:'inherit',seconds:'',rows:[]}, 'stream_header_timeout'), undefined);
});

test('canonical beats alias; editing only selected field removes alias without dropping others', () => {
  const original = {header_timeout: 300, stream_header_timeout: 0, model_timeout: {default:900, other:1200}, custom: {active:false}};
  assert.equal(readTimeoutValue(original, 'stream_header_timeout'), 0);
  assert.equal(readTimeoutValue({header_timeout: 300, stream_header_timeout:null}, 'stream_header_timeout'), 300);
  const next = writeTimeoutValue(original, 'stream_header_timeout', {default:300,carbon:0});
  assert.equal('header_timeout' in next, false);
  assert.deepEqual(next.model_timeout, original.model_timeout);
  assert.deepEqual(next.custom, original.custom);
  assert.equal(original.header_timeout,300);
  const removed = writeTimeoutValue(next,'stream_header_timeout',undefined);
  assert.equal('stream_header_timeout' in removed,false);
  const heartbeat = writeTimeoutValue(original,'keepalive_interval',20);
  assert.equal(heartbeat.header_timeout,300);
});

test('incomplete and invalid drafts cannot become persisted values', () => {
  for (const value of ['', ' ', 'NaN', 'Infinity', '-2']) {
    assert.equal(validateTimeoutDraft(draft(value),'stream_header_timeout').valid,false,value);
    assert.throws(()=>serializeTimeoutDraft(draft(value),'stream_header_timeout'));
  }
  assert.equal(validateTimeoutDraft(draft('0'),'model_timeout').valid,false);
  assert.equal(validateTimeoutDraft(draft('1.5'),'keepalive_interval').valid,false);
  assert.equal(validateTimeoutDraft(draft('0'),'keepalive_interval').valid,true);
  assert.equal(validateTimeoutDraft(draft('0.25'),'stream_header_timeout').valid,true);
  assert.equal(validateTimeoutDraft(timeoutDraft('60','stream_header_timeout'),'stream_header_timeout').valid,false);
});

test('model names must be unique ignoring case and cannot use default row', () => {
  for (const rows of [[row('','1')], [row('default','1')], [row(' DEFAULT ','1')], [row('Model','1'),row('model','2')]]) {
    assert.equal(validateTimeoutDraft(draft('60',rows),'stream_header_timeout').valid,false);
  }
  const value = serializeTimeoutDraft(draft('60',[row('carbon','', 'off'),row('other','45')]),'stream_header_timeout');
  assert.deepEqual(value,{default:60,carbon:0,other:45});
});

test('channel default overrides global model while channel model-only falls back globally', () => {
  const globals={stream_header_timeout:{default:60,carbon:0}};
  assert.deepEqual(resolveTimeoutPreview('stream_header_timeout',{header_timeout:300},globals,CODE_TIMEOUT_DEFAULTS,'channel','carbon'),{value:300,rule:'default',source:'channel'});
  assert.deepEqual(resolveTimeoutPreview('stream_header_timeout',{stream_header_timeout:{other:120}},globals,CODE_TIMEOUT_DEFAULTS,'channel','carbon'),{value:0,rule:'carbon',source:'global'});
  assert.deepEqual(resolveTimeoutPreview('stream_header_timeout',{},globals,CODE_TIMEOUT_DEFAULTS,'channel','alias','carbon'),{value:60,rule:'default',source:'global'});
  assert.deepEqual(resolveTimeoutPreview('stream_header_timeout',{stream_header_timeout:{gemini:90,'gemini-carbon':120}},globals,CODE_TIMEOUT_DEFAULTS,'channel','gemini-carbon'),{value:120,rule:'gemini-carbon',source:'channel'});
  assert.deepEqual(resolveTimeoutPreview('stream_header_timeout',{stream_header_timeout:{gemini:90,'gemini-carbon':120}},globals,CODE_TIMEOUT_DEFAULTS,'channel','gemini-carbon-preview'),{value:90,rule:'gemini',source:'channel'});
});

test('empty channel tables try global request name before the mapped upstream', () => {
  const globals = {stream_header_timeout:{default:60,alias:0,upstream:300}};
  for (const own of [{}, {stream_header_timeout:{}}]) {
    assert.equal(resolveTimeoutPreview('stream_header_timeout',own,globals,CODE_TIMEOUT_DEFAULTS,'channel','alias','upstream').value,0);
    assert.equal(resolveTimeoutPreview('stream_header_timeout',own,globals,CODE_TIMEOUT_DEFAULTS,'channel','other','upstream').value,60);
  }
  assert.equal(resolveTimeoutPreview('stream_header_timeout',{stream_header_timeout:{unrelated:40}},globals,CODE_TIMEOUT_DEFAULTS,'channel','other','upstream').value,300);
  assert.equal(resolveTimeoutPreview('stream_header_timeout',globals,{},CODE_TIMEOUT_DEFAULTS,'global','alias','upstream').value,0);
});

test('subchannel honors shallow parent merge, canonical alias priority, then global fallback', () => {
  const globals = {stream_header_timeout:{default:60,carbon:0}};
  const parent = {stream_header_timeout:300};
  assert.deepEqual(resolveTimeoutPreview('stream_header_timeout',{header_timeout:90},globals,CODE_TIMEOUT_DEFAULTS,'channel','carbon','carbon',parent),{value:300,rule:'default',source:'parent'});
  assert.deepEqual(resolveTimeoutPreview('stream_header_timeout',{stream_header_timeout:{other:45}},globals,CODE_TIMEOUT_DEFAULTS,'channel','carbon','carbon',parent),{value:0,rule:'carbon',source:'global'});
  assert.deepEqual(resolveTimeoutPreview('stream_header_timeout',{},globals,CODE_TIMEOUT_DEFAULTS,'channel','carbon','carbon',parent),{value:300,rule:'default',source:'parent'});
  assert.deepEqual(resolveTimeoutPreview('stream_header_timeout',{stream_header_timeout:0},globals,CODE_TIMEOUT_DEFAULTS,'channel','carbon','carbon',parent),{value:0,rule:'default',source:'channel'});
});

test('initial render is read-only, has three controls and explains missing runtime metadata', async () => {
  const bundle = await build({stdin:{resolveDir:root,loader:'tsx',contents:`
    import React from 'react';
    import {renderToStaticMarkup} from 'react-dom/server';
    import {TimeoutSettings} from './src/components/TimeoutSettings';
    const original={header_timeout:300,model_timeout:{default:900},keepalive_interval:20};
    const html=renderToStaticMarkup(<TimeoutSettings preferences={original} scope="channel" onChange={()=>{throw new Error('render wrote preferences')}} />);
    console.log(html);
  `},bundle:true,write:false,platform:'node',format:'cjs',logLevel:'silent'});
  const html=execFileSync(process.execPath,['-'],{input:bundle.outputFiles[0].text,cwd:root,maxBuffer:2*1024*1024}).toString();
  assert.match(html,/首包等待上限/);assert.match(html,/网络读取超时/);assert.match(html,/SSE 心跳间隔/);
  assert.match(html,/服务未提供运行时默认值/);
  assert.equal((html.match(/<select/g)||[]).length,3);
  assert.doesNotMatch(html,/role="dialog"/);
});
