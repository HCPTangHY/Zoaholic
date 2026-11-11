import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const bundled = await build({
  stdin: { resolveDir: root, loader: 'tsx', contents: `
    import assert from 'node:assert/strict';
    import { ownSubChannelTimeouts, serializeSubChannelPreferences } from './src/pages/channels/hooks/useChannelEditor';
    const parent = {stream_header_timeout: {default:300, carbon:120}, model_timeout:900, keepalive_interval:20, weight:5};
    const own = {header_timeout:{carbon:0}, model_timeout:900, enabled_plugins:[]};
    const initial = ownSubChannelTimeouts({...parent,...own},own);
    assert.deepEqual(initial.header_timeout,{carbon:0});
    assert.equal(initial.model_timeout,900);
    assert.equal('stream_header_timeout' in initial,false);
    assert.equal('keepalive_interval' in initial,false);
    const saved=serializeSubChannelPreferences(initial,parent);
    assert.deepEqual(saved,{header_timeout:{carbon:0},model_timeout:900,enabled_plugins:[]});
    assert.equal('weight' in saved,false);
    assert.deepEqual(parent.stream_header_timeout,{default:300,carbon:120});
    const deleted=ownSubChannelTimeouts(initial,{});
    const inherited=serializeSubChannelPreferences(deleted,parent);
    assert.equal('header_timeout' in inherited,false);
    assert.equal('model_timeout' in inherited,false);
    const zero=serializeSubChannelPreferences({stream_header_timeout:0,keepalive_interval:0},parent);
    assert.deepEqual(zero,{stream_header_timeout:0,keepalive_interval:0});
    console.log('channel timeout ownership and roundtrip: passed');
  ` },
  bundle:true,write:false,platform:'node',format:'cjs',logLevel:'silent',
});
const prefix=`globalThis.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};`;
const output=execFileSync(process.execPath,['-'],{input:prefix+bundled.outputFiles[0].text,cwd:root,maxBuffer:10*1024*1024}).toString();
assert.match(output,/roundtrip: passed/);
console.log(output.trim());
