import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// Execute the real component and its handlers without a DOM dependency. Only
// React scheduling and navigation are replaced; fetch responses are per-test.
const hooks = `
  let values = [], cursor = 0, first = true, effects = [], cleanups = [];
  export function useState(initial) {
    const index = cursor++;
    if (!(index in values)) values[index] = typeof initial === 'function' ? initial() : initial;
    return [values[index], value => { values[index] = typeof value === 'function' ? value(values[index]) : value; }];
  }
  export function useEffect(effect) { if (first) effects.push(effect); }
  export function render(Component) { cursor = 0; const tree = Component(); first = false; return tree; }
  export function mount() { for (const effect of effects.splice(0)) cleanups.push(effect()); }
  export function unmount() { for (const cleanup of cleanups.splice(0)) cleanup?.(); }
  export const Fragment = 'fragment';
  export function jsx(type, props) { return {type, props: props || {}}; }
  export const jsxs = jsx;
`;
const bundle = await build({
  stdin: { resolveDir: root, loader: 'tsx', contents: `
    import Setup from './src/pages/Setup';
    import {render, mount, unmount} from 'setup-hooks';
    export const view = () => render(Setup);
    export {mount, unmount};
  ` },
  bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic', logLevel: 'silent',
  plugins: [{
    name: 'setup-test-runtime',
    setup(builder) {
      builder.onResolve({filter: /^(react(?:\/jsx-runtime)?|setup-hooks)$/}, () => ({path: 'hooks', namespace: 'setup-test'}));
      builder.onResolve({filter: /^react-router-dom$/}, () => ({path: 'router', namespace: 'setup-test'}));
      builder.onLoad({filter: /.*/, namespace: 'setup-test'}, ({path: name}) => ({
        contents: name === 'hooks' ? hooks : 'export const useNavigate = () => globalThis.navigate;', loader: 'js',
      }));
    },
  }],
});

const settle = () => new Promise(resolve => setImmediate(resolve));
const response = (body, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const fresh = {needs_setup: true, has_config: false, has_admin_user: false, database_enabled: true};

function elements(tree, predicate) {
  const found = [];
  function walk(node) {
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (!node || typeof node !== 'object') return;
    if (predicate(node)) found.push(node);
    walk(node.props?.children);
  }
  walk(tree);
  return found;
}
function form(tree) { return elements(tree, node => node.type === 'form')[0]; }
function button(tree) { return elements(tree, node => node.type === 'button' && node.props.type === 'submit')[0]; }
function keyInput(tree) { return elements(tree, node => node.type === 'input' && node.props.autoComplete === 'off')[0]; }
function fillPasswords(tree) {
  for (const input of elements(tree, node => node.type === 'input' && node.props.autoComplete === 'new-password')) {
    input.props.onChange({target: {value: 'test-password-only'}});
  }
}
async function submit(tree) { await form(tree).props.onSubmit({preventDefault() {}}); }

function harness(fetcher) {
  const requests = [], navigations = [], writes = [];
  const storage = {setItem(...args) { writes.push(args); }, getItem() { return null; }, removeItem() {}};
  const module = {exports: {}};
  runInNewContext(bundle.outputFiles[0].text, {
    module, exports: module.exports, require: createRequire(import.meta.url),
    fetch: async (...args) => { requests.push(args); return fetcher(...args); },
    navigate: value => navigations.push(value),
    localStorage: storage, sessionStorage: storage, window: {localStorage: storage, sessionStorage: storage},
  });
  return {...module.exports, requests, navigations, writes};
}
async function load(status = fresh, initResponse = response({admin_api_key: 'response-must-not-be-stored'})) {
  const app = harness(url => url === '/setup/status' ? response(status) : initResponse);
  assert.equal(form(app.view()), undefined, 'Checking state must not expose a submit handler');
  app.mount();
  await settle();
  return app;
}

test('status request is uncached and unavailable status keeps submission closed', async () => {
  for (const fail of [() => { throw new Error('network unavailable'); }, () => response({}, 503)]) {
    const app = harness(fail);
    app.view(); app.mount(); await settle();
    const tree = app.view();
    assert.equal(button(tree).props.disabled, true);
    assert.equal(elements(tree, node => node.type === 'fieldset')[0].props.disabled, true);
    assert.equal(app.requests[0][0], '/setup/status');
    assert.equal(app.requests[0][1].cache, 'no-store');
    await submit(tree);
    assert.equal(app.requests.length, 1, 'Even direct handler invocation must not POST');
    assert.deepEqual(app.navigations, []);
  }
});

test('initialized status redirects to login without opening bootstrap form', async () => {
  const app = await load({...fresh, needs_setup: false, has_config: true, has_admin_user: true});
  assert.deepEqual(app.navigations, ['/login']);
  assert.equal(button(app.view()).props.disabled, true);
  await submit(app.view());
  assert.equal(app.requests.length, 1);
});

test('unmounted status checks cannot navigate or open the form', async () => {
  let resolveStatus;
  const app = harness(() => new Promise(resolve => { resolveStatus = resolve; }));
  app.view(); app.mount(); app.unmount();
  resolveStatus(response({...fresh, needs_setup: false}));
  await settle();
  assert.deepEqual(app.navigations, []);
  assert.equal(form(app.view()), undefined);
});

test('existing configuration requires a recovery key', async () => {
  for (const state of [{has_config: true}, {has_config: true, has_admin_user: true}]) {
    const app = await load({...fresh, ...state});
    const tree = app.view();
    assert.ok(keyInput(tree));
    assert.equal(keyInput(tree).props.type, 'password');
    fillPasswords(tree);
    await submit(app.view());
    assert.equal(app.requests.length, 1, 'Missing existing key must not reach setup/init');
    assert.deepEqual(app.navigations, []);
  }
});

test('recovery key is sent only as Authorization and cleared after success', async () => {
  const app = await load({...fresh, has_config: true});
  const tree = app.view();
  fillPasswords(tree);
  keyInput(tree).props.onChange({target: {value: '  test-recovery-secret  '}});
  await submit(app.view());
  const [url, init] = app.requests[1];
  assert.equal(url, '/setup/init');
  assert.equal(init.method, 'POST');
  assert.equal(init.headers.Authorization, 'Bearer test-recovery-secret');
  assert.deepEqual(JSON.parse(init.body), {username: 'admin', password: 'test-password-only', confirm_password: 'test-password-only'});
  assert.doesNotMatch(init.body, /test-recovery-secret|admin_api_key|existingAdminKey/);
  assert.deepEqual(app.writes, [], 'Neither request key nor returned admin key may enter browser storage');
  assert.deepEqual(app.navigations, ['/login']);
  assert.equal(keyInput(app.view()).props.value, '');
});

test('an existing account with missing config uses the original account password', async () => {
  const app = await load({...fresh, has_admin_user: true});
  assert.equal(keyInput(app.view()), undefined);
  fillPasswords(app.view());
  await submit(app.view());
  const [, init] = app.requests[1];
  assert.equal(init.headers.Authorization, undefined);
  assert.equal(JSON.parse(init.body).password, 'test-password-only');
  assert.deepEqual(app.writes, []);
});

test('fresh setup submits without a recovery Authorization header', async () => {
  const app = await load();
  assert.equal(keyInput(app.view()), undefined);
  fillPasswords(app.view());
  await submit(app.view());
  const [, init] = app.requests[1];
  assert.equal(init.headers.Authorization, undefined);
  assert.equal(JSON.parse(init.body).admin_api_key, undefined);
  assert.deepEqual(app.writes, []);
  assert.deepEqual(app.navigations, ['/login']);
});

test('disabled database blocks form and direct submit-handler invocation', async () => {
  const app = await load({...fresh, database_enabled: false});
  fillPasswords(app.view());
  assert.equal(button(app.view()).props.disabled, true);
  assert.equal(elements(app.view(), node => node.type === 'fieldset')[0].props.disabled, true);
  await submit(app.view());
  assert.equal(app.requests.length, 1);
  assert.deepEqual(app.navigations, []);
});

test('pending setup submission cannot be duplicated before rerender', async () => {
  let resolveInit;
  const app = harness(url => url === '/setup/status' ? response(fresh) : new Promise(resolve => { resolveInit = resolve; }));
  app.view(); app.mount(); await settle();
  fillPasswords(app.view());
  const tree = app.view();
  const first = submit(tree);
  await submit(tree);
  assert.equal(app.requests.length, 2, 'One status check and one POST, even with a stale handler');
  resolveInit(response({admin_api_key: 'test-response-key'}));
  await first;
  assert.deepEqual(app.navigations, ['/login']);
});

test('unmounted setup submission cannot navigate on success', async () => {
  let resolveInit;
  const app = harness(url => url === '/setup/status' ? response(fresh) : new Promise(resolve => { resolveInit = resolve; }));
  app.view(); app.mount(); await settle();
  fillPasswords(app.view());
  const pending = submit(app.view());
  app.unmount();
  resolveInit(response({admin_api_key: 'test-response-key'}));
  await pending;
  assert.deepEqual(app.navigations, []);
  assert.deepEqual(app.writes, []);
});

test('rejected recovery stays on setup and never stores credentials', async () => {
  const app = await load({...fresh, has_config: true}, response({detail: 'Permission denied'}, 403));
  fillPasswords(app.view());
  keyInput(app.view()).props.onChange({target: {value: 'test-rejected-key'}});
  await submit(app.view());
  assert.deepEqual(app.navigations, []);
  assert.deepEqual(app.writes, []);
  assert.ok(elements(app.view(), node => node.props?.role === 'alert').some(node => node.props.children === 'Permission denied'));
});
