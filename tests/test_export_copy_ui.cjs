const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const storageKey = 'email-rebind:export-copy-format';
const full = 'old@example.com----new@example.com----Pass----word!----2FA-SECRET----AT-token\n';
const withoutOld = 'new@example.com----Pass----word!----2FA-SECRET----AT-token\n';

function setup(saved, storageBlocked = false) {
  const selectors = [{value: 'full'}, {value: 'full'}];
  const elements = new Map(['#copyExport', '#copySuccessResultsBottom', '#successAccountsBody'].map(id => [id, {}]));
  const storage = new Map(saved === undefined ? [] : [[storageKey, saved]]);
  const copied = [], requests = [], messages = [];
  const context = vm.createContext({
    $: selector => elements.get(selector),
    $$: selector => selector === '[data-export-copy-format]' ? selectors : [],
    localStorage: {
      getItem(key) { if (storageBlocked) throw new Error('blocked'); return storage.get(key) ?? null; },
      setItem(key, value) { if (storageBlocked) throw new Error('blocked'); storage.set(key, value); },
    },
    navigator: {clipboard: {async writeText(value) { copied.push(value); }}},
    async fetch(url, options) {
      requests.push({url, options});
      return {ok: true, async text() { return url.endsWith('/access-token') ? '  AT-token\n' : full; }};
    },
    toast: message => messages.push(message),
  });
  const declaration = source.split(/\r?\n/).find(line => line.startsWith('let exportCopyFormat ='));
  vm.runInContext(declaration + '\n' + source.slice(source.indexOf('async function fetchText('), source.indexOf('function waitForGCashFrame(')), context);
  for (const selector of elements.keys()) {
    const handler = source.split(/\r?\n/).find(line => line.startsWith(`$('${selector}').onclick=`));
    assert.ok(handler, `missing handler for ${selector}`);
    vm.runInContext(handler, context);
  }
  context.initExportCopyFormat();
  return {context, selectors, elements, storage, copied, requests, messages};
}

test('default format preserves the original export byte for byte', async () => {
  const ui = setup();
  assert.deepEqual(ui.selectors.map(input => input.value), ['full', 'full']);
  await ui.elements.get('#copySuccessResultsBottom').onclick();
  assert.deepEqual(ui.copied, [full]);
  assert.equal(ui.elements.get('#copySuccessResultsBottom').disabled, false);
});

test('both selectors synchronize and both bulk-copy buttons omit only the old email', async () => {
  const ui = setup();
  ui.selectors[1].value = 'without_old_email';
  ui.selectors[1].onchange();
  assert.deepEqual(ui.selectors.map(input => input.value), ['without_old_email', 'without_old_email']);
  assert.equal(ui.storage.get(storageKey), 'without_old_email');
  for (const selector of ['#copyExport', '#copySuccessResultsBottom']) {
    await ui.elements.get(selector).onclick();
    assert.equal(ui.elements.get(selector).disabled, false);
  }
  assert.deepEqual(ui.copied, [withoutOld, withoutOld]);
  ui.selectors[0].value = 'full';
  ui.selectors[0].onchange();
  await ui.elements.get('#copySuccessResultsBottom').onclick();
  assert.equal(ui.copied.at(-1), full);
  assert.equal(ui.selectors[1].value, 'full');
});

test('single-account result follows the format while AT-only copying is unchanged', async () => {
  const ui = setup('without_old_email');
  const button = {dataset: {copyExport: '7'}};
  await ui.elements.get('#successAccountsBody').onclick({target: {closest: selector => selector === '[data-copy-export]' ? button : null}});
  assert.equal(ui.copied[0], withoutOld);
  assert.equal(ui.requests[0].url, '/api/accounts/7/export');
  assert.equal(ui.requests[0].options.cache, 'no-store');
  assert.equal(button.disabled, false);
  await ui.context.copyAccessToken(7);
  assert.equal(ui.copied[1], 'AT-token');
});

test('mixed account formats, CRLF, blank lines and embedded separators are preserved', () => {
  const {context} = setup('without_old_email');
  const url = 'old-api@example.com----new-api@example.com----https://mail.example/key----suffix----AT-api';
  const input = full.replace('\n', '\r\n') + '\r\n' + url + '\r\n';
  const expected = withoutOld.replace('\n', '\r\n') + '\r\n' + url.slice(url.indexOf('----') + 4) + '\r\n';
  assert.equal(context.formatExportText(input), expected);
  assert.equal(context.formatExportText(''), '');
  assert.equal(context.formatExportText('unchanged'), 'unchanged');
});

test('saved preferences restore, invalid preferences use default, and blocked storage is harmless', () => {
  assert.equal(setup('without_old_email').context.formatExportText(full), withoutOld);
  assert.equal(setup('invalid').context.formatExportText(full), full);
  const ui = setup(undefined, true);
  assert.equal(ui.context.formatExportText(full), full);
  ui.selectors[0].value = 'without_old_email';
  ui.selectors[0].onchange();
  assert.equal(ui.context.formatExportText(full), withoutOld);
});

test('in-flight copy uses the format selected when clicked', async () => {
  const ui = setup('without_old_email');
  let finishFetch;
  ui.context.fetch = () => new Promise(resolve => { finishFetch = resolve; });
  const pending = ui.context.copyExport('/api/export');
  ui.context.setExportCopyFormat('full');
  finishFetch({ok: true, async text() { return full; }});
  await pending;
  assert.deepEqual(ui.copied, [withoutOld]);
});

test('empty exports and HTTP errors never overwrite the clipboard', async () => {
  const ui = setup('without_old_email');
  ui.context.fetch = async () => ({ok: true, async text() { return '\n'; }});
  await ui.elements.get('#copySuccessResultsBottom').onclick();
  assert.deepEqual(ui.copied, []);
  assert.equal(ui.messages.at(-1), '暂无完成结果');
  assert.equal(ui.elements.get('#copySuccessResultsBottom').disabled, false);
  ui.context.fetch = async () => ({ok: false, status: 404, async json() { return {error: '没有结果'}; }});
  await assert.rejects(ui.context.copyExport('/api/export'), /没有结果/);
  assert.deepEqual(ui.copied, []);
});
