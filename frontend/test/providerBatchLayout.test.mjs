import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const bundled = await build({
  stdin: { resolveDir: root, loader: 'tsx', contents: `
    import React from 'react';
    import { renderToStaticMarkup } from 'react-dom/server';
    import { ProviderList } from './src/pages/channels/components/ProviderList';
    const noop = () => {};
    const provider = {provider:'Example', engine:'openai', model:['gpt'], api:'test'};
    const props = {
      providers:[provider], token:'test', refreshProviders:async()=>{}, loading:false,
      totalListItemCount:1,visibleListItemCount:1,filterKeyword:'',filterEngine:'',filterGroup:'',filterStatus:'',
      setFilterKeyword:noop,setFilterEngine:noop,setFilterGroup:noop,setFilterStatus:noop,
      availableEngines:['openai'],availableGroups:['default'],hasActiveFilters:false,
      segments:[{type:'active',item:{p:provider,idx:0}}],expandedInactiveGroups:new Set(),toggleInactiveGroup:noop,
      runtimeKeyStatus:{},getMatchedModels:()=>[],getProviderAnalyticsName:()=> 'Example',
      setAnalyticsProvider:noop,setAnalyticsOpen:noop,openTestDialog:noop,handleToggleProvider:noop,
      handleCopyProvider:noop,openModal:noop,handleDeleteProvider:noop,handleUpdateWeight:noop,
      buildSubChannelProvider:()=>null,handleToggleSubChannel:noop,openSubChannelEdit:noop,handleDeleteSubChannel:noop,
      renderMobileVirtualRoutesAccordion:()=>null,renderDesktopVirtualRoutesAccordionRows:()=>null,
    };
    console.log(renderToStaticMarkup(<ProviderList {...props} />));
  ` },
  bundle: true, write: false, platform: 'node', format: 'cjs', logLevel: 'silent',
});
const prefix = `globalThis.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};`;
const html = execFileSync(process.execPath, ['-'], { input: prefix + bundled.outputFiles[0].text, cwd: root, maxBuffer: 10 * 1024 * 1024 }).toString();
assert.match(html, /id="provider-selection-toggle"[^>]*aria-pressed="false"/);
assert.doesNotMatch(html, /type="checkbox"/, 'browse mode must not render provider selection checkboxes');
assert.doesNotMatch(html, /全选当前筛选（含折叠渠道）/);
assert.doesNotMatch(html, /aria-label="(?:折叠|展开)渠道列表"/, 'do not add a second accordion around the entire list');
assert.match(html, /id="channel-provider-list">/);
assert.ok(html.indexOf('id="provider-selection-toggle"') < html.indexOf('id="channel-provider-list"'));
assert.match(html, /class="hidden"><span[^>]*aria-live="polite"/, 'selection toolbar starts hidden');
console.log('provider batch browse layout: passed');
