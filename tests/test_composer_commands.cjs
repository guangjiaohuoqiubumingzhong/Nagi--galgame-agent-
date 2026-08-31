// Lightweight DOM/event tests: no browser, server, API key, or model calls needed.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../nagi/web_ui/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../nagi/web_ui/app.js'), 'utf8').replace(/\bboot\(\);\s*$/, '');
const workflowSource = fs.readFileSync(path.join(__dirname, '../nagi/web_ui/translation-workflow.js'), 'utf8');

const modelFixture = {
  revision: 'r1', active_provider: 'deepseek', credential_storage: 'Windows 用户加密',
  providers: [{ id: 'deepseek', kind: 'deepseek', name: 'DeepSeek', model: 'existing-model', models: ['existing-model', 'another-model'], api_key_configured: true, protocol: 'anthropic', base_url: 'https://api.deepseek.com/anthropic', json_output: true }],
  presets: [{ kind: 'kimi', name: 'Kimi', models: ['kimi-fixture'], base_url: 'https://api.moonshot.cn/v1', docs_url: 'https://platform.kimi.com/docs/overview' }],
};

function harness() {
  const nodes = new Map();
  let document;
  class Element {
    constructor() {
      this.listeners = {};
      this.attributes = {};
      this.children = [];
      this.style = {};
      this.dataset = {};
      this.hidden = false;
      this.disabled = false;
      this.value = '';
      this.files = [];
      this.selectionStart = this.selectionEnd = 0;
      this.classList = { toggle() {}, add() {}, remove() {}, contains() { return false; } };
    }
    set id(value) { this._id = value; nodes.set(`#${value}`, this); }
    get id() { return this._id; }
    set textContent(value) { this._text = value; }
    get textContent() { return this._text || ''; }
    get innerHTML() { return this._html ?? this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
    set innerHTML(value) { this._html = value; }
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
    fire(type, properties = {}) {
      const event = { target: this, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.propagationStopped = true; }, ...properties };
      for (const listener of this.listeners[type] || []) listener(event);
      return event;
    }
    setAttribute(name, value) { this.attributes[name] = value; }
    removeAttribute(name) { delete this.attributes[name]; }
    replaceChildren() { this.children = []; }
    append(child) { child.parent = this; this.children.push(child); }
    focus() { document.activeElement = this; }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    getBoundingClientRect() { return { top: 500 }; }
    scrollIntoView() {}
    showModal() { this.open = true; }
    close() { this.open = false; }
    reportValidity() { return true; }
    closest(selector) {
      if (nodes.get(selector) === this) return this;
      return this.parent?.closest(selector) || null;
    }
  }
  for (const match of html.matchAll(/id="([^"]+)"/g)) new Element().id = match[1];
  const modeInput = new Element();
  modeInput.value = 'pilot';
  nodes.set('input[name="mode"]:checked', modeInput);
  nodes.set('#start-button small', new Element());
  for (const match of html.matchAll(/class="([^"]+)"/g)) {
    for (const name of match[1].split(' ')) if (!nodes.has(`.${name}`)) nodes.set(`.${name}`, new Element());
  }
  const get = (selector) => {
    assert.ok(nodes.has(selector), `Selector exists in HTML or rendered menu: ${selector}`);
    return nodes.get(selector);
  };
  document = new Element();
  document.body = new Element();
  document.querySelector = get;
  document.querySelectorAll = () => [];
  document.createElement = () => new Element();
  for (const id of ['prompt-input', 'composer-add', 'command-menu', 'permission-trigger']) get(`#${id}`).parent = get('.composer');
  get('#command-menu').hidden = get('#permission-menu').hidden = true;
  const context = vm.createContext({
    document, window: new Element(), localStorage: { getItem() { return null; }, setItem() {} },
    requestAnimationFrame: (callback) => callback(), setInterval, clearInterval,
    fetch: () => { throw Error('Unexpected network request'); },
  });
  vm.runInContext(workflowSource, context);
  vm.runInContext(source, context);
  const run = (code) => vm.runInContext(code, context);
  run('state.workspaces = [{ path: "D:/nagi", name: "nagi" }]; state.workspacePath = "D:/nagi"; updateWorkspaceHeader(); bindEvents();');
  const input = get('#prompt-input');
  const draft = (text, caret = text.length) => { input.value = text; input.setSelectionRange(caret, caret); };
  return { get, input, draft, run, document };
}

test('translation page omits removed explanatory copy without removing directory controls', () => {
  assert.doesNotMatch(html, /按引擎 → 游戏 → 任务|locale-status|Locale Emulator 官方发布页|只保存本机路径|Case 六花（YU-RIS 479）/);
  assert.doesNotMatch(workflowSource, /locale-status|不做内容筛选或翻译准确度评分/);
  assert.match(workflowSource, /全部已提取文本分批提交所选 API。/);
  for (const id of ['game-path', 'storage-path', 'locale-path', 'browse-locale-button', 'save-locale-button', 'locale-error']) {
    assert.ok(html.includes(`id="${id}"`));
  }
});

for (const purpose of ['game', 'storage', 'locale']) {
  test(`${purpose} folder picker handles cancellation, errors, and a successful retry`, async () => {
    const h = harness();
    const input = h.get(`#${purpose}-path`);
    const error = h.get(`#${purpose === 'locale' ? 'locale' : 'extract'}-error`);
    const action = purpose === 'locale' ? 'translationUI.browseLocale()' : `translationUI.browse('${purpose}')`;
    input.value = 'D:/原有目录';
    h.run(`translationUI.render = () => {}; translationUI.workflow = {workflow_id:'keep'};
      globalThis.calls = []; api = async (path, options) => { calls.push({path, options}); return {path:null}; };`);
    await h.run(action);
    assert.equal(input.value, 'D:/原有目录');
    assert.equal(h.run('translationUI.workflow.workflow_id'), 'keep');
    assert.equal(h.run('translationUI.pending'), false);
    assert.equal(h.run('calls[0].path'), '/api/select-directory');
    assert.equal(h.run('calls[0].options.body.purpose'), purpose);

    h.run(`api = async () => { throw Error('无法打开目录选择窗口，请手动填写完整目录路径。'); };`);
    await h.run(action);
    assert.match(error.textContent, /手动填写/);
    assert.equal(error.hidden, false);
    assert.equal(input.value, 'D:/原有目录');
    assert.equal(h.run('translationUI.pending'), false);

    h.run(`api = async () => ({path:'D:/中文 游戏 & 工具目录'});`);
    await h.run(action);
    assert.equal(input.value, 'D:/中文 游戏 & 工具目录');
    assert.equal(error.hidden, true);
    assert.equal(h.run('translationUI.pending'), false);
    assert.equal(h.run('translationUI.workflow?.workflow_id ?? null'), purpose === 'locale' ? 'keep' : null);
  });
}

test('translation starts without a reference file or context field', async () => {
  const h = harness();
  h.get('#game-path').value = 'D:/game';
  h.get('#cost-confirm').checked = true;
  h.run(`globalThis.calls = []; window.confirm = () => true; translationUI.render = () => {}; setInterval = () => 0;
    translationUI.workflow = { workflow_id: 'prepared', stages: {extract: {status:'completed'}} };
    api = async (path, options) => { calls.push({path, options}); return {job_id: 'test'}; };`);
  await h.run('translationUI.run("translate")');
  assert.equal(h.run('Object.hasOwn(calls[0].options.body, "context")'), false);
  assert.equal(h.run('calls[0].options.body.confirmed'), true);
  assert.equal(h.run('calls[0].path'), '/api/translation-workflows/prepared/translate');
});

test('YU-RIS full translation is not blocked by the legacy pilot guard', async () => {
  const h = harness();
  h.get('input[name="mode"]:checked').value = 'full';
  h.get('#cost-confirm').checked = true;
  h.run(`globalThis.calls = []; globalThis.confirmation = ''; translationUI.render = () => {};
    window.confirm = message => { confirmation = message; return true; };
    translationUI.workflow = { workflow_id: 'yuris-full', source_kind: 'yuris-479',
      stages: { extract: { status: 'completed' }, translate: { status: 'pending' } } };
    api = async (path, options) => { calls.push({path, options}); return {}; };`);
  await h.run('translationUI.run("translate")');
  assert.equal(h.run('calls.length'), 1);
  assert.equal(h.run('calls[0].path'), '/api/translation-workflows/yuris-full/translate');
  assert.equal(h.run('calls[0].options.body.mode'), 'full');
  assert.equal(h.run('confirmation'), '请保持网络通畅，调用 API 会产生费用。确认开始？');
  assert.equal(h.run('calls[0].options.body.confirmed'), true);
  h.run('translationUI.workflow.source_kind = "yuris479-rikka-approved-opening-v1";');
  await h.run('translationUI.run("translate")');
  assert.equal(h.run('calls.length'), 1);
  assert.match(h.get('#translate-error').textContent, /旧开场试译/);
});

test('partial translation submits its own mode with concise network and cost confirmation', async () => {
  const h = harness();
  h.get('input[name="mode"]:checked').value = 'partial';
  h.get('#cost-confirm').checked = true;
  h.run(`globalThis.calls = []; globalThis.confirmation = ''; translationUI.render = () => {};
    window.confirm = message => { confirmation = message; return true; };
    translationUI.workflow = { workflow_id: 'fifty', source_kind: 'yuris-479', mode: 'full',
      stages: {extract:{status:'completed'},translate:{status:'completed'}} };
    api = async (path, options) => { calls.push({path, options}); return {}; };`);
  await h.run('translationUI.run("translate")');
  assert.equal(h.run('calls.length'), 1);
  assert.equal(h.run('calls[0].options.body.mode'), 'partial');
  assert.equal(h.run('confirmation'), '请保持网络通畅，调用 API 会产生费用。确认开始？');
  assert.equal(h.run('calls[0].options.body.confirmed'), true);
  h.run('window.confirm = () => false;');
  await h.run('translationUI.run("translate")');
  assert.equal(h.run('calls.length'), 1);
});

test('completed YU-RIS can switch scope, retains selection, and locks radios only while running', () => {
  const h = harness();
  h.run(`globalThis.radios = ['partial','full'].map(value => Object.assign(document.createElement('input'), {value,checked:value==='full'}));
    globalThis.decorations = new Map(); globalThis.originalQuery = document.querySelector;
    document.querySelector = selector => {
      if (selector === 'input[name="mode"]:checked') return radios.find(node => node.checked);
      if (selector.endsWith(' span') || selector.endsWith(' strong')) {
        if (!decorations.has(selector)) decorations.set(selector, document.createElement('span'));
        return decorations.get(selector);
      }
      return originalQuery(selector);
    };
    document.querySelectorAll = selector => selector === 'input[name="mode"]' ? radios : [];
    translationUI.bind();
    globalThis.completed = {workflow_id:'existing',source_kind:'yuris-479',mode:'full',
      stages:{extract:{status:'completed',progress:1},translate:{status:'completed',progress:1},deploy:{status:'completed',progress:1}}};
    translationUI.render(completed);`);
  assert.equal(h.run('radios.every(node => !node.disabled)'), true);
  h.run(`radios[0].checked = true; radios[1].checked = false; radios[0].fire('change'); translationUI.render();`);
  assert.equal(h.run('radios[0].checked'), true);
  assert.equal(h.get('#translation-description').textContent, '仅翻译开场剧情前 50 条内容');
  assert.equal(h.get('#translate-status').textContent, '待开始');
  assert.match(h.run('translationUI.prerequisite("deploy")'), /范围已经改变/);
  assert.equal(h.run('translationUI.workflow.mode'), 'full'); // Existing results are not mutated by selection.
  h.run('translationUI.pending = true; translationUI.render();');
  assert.equal(h.run('radios.every(node => node.disabled)'), true);
  h.run(`translationUI.pending = false; translationUI.render({...completed,mode:'partial'});`);
  assert.equal(h.run('radios[0].checked && !radios[1].checked'), true);
  assert.equal(h.run('radios.every(node => !node.disabled)'), true);
  assert.match(html, /value="partial"/);
  assert.doesNotMatch(html, /先试译 32 条/);
  const css = fs.readFileSync(path.join(__dirname, '../nagi/web_ui/styles.css'), 'utf8');
  assert.match(css, /input\[type="radio"\]:checked\s*\{[^}]*background: var\(--green\)/);
});

test('YU-RIS does not make a request before cost confirmation or complete translation', async () => {
  const h = harness();
  h.run(`translationUI.workflow = {source_kind: 'yuris-479', mode: 'full',
    stages: {extract: {status: 'completed'}, translate: {status: 'pending'}}};
    api = async () => { throw Error('Must not make a network request'); };`);
  await h.run('translationUI.run("translate")');
  assert.match(h.get('#translate-error').textContent, /费用/);
  await h.run('translationUI.run("deploy")');
  assert.match(h.get('#deploy-error').textContent, /第二步/);
});

test('Locale Emulator selection saves a local directory without invalidating the translation', async () => {
  const h = harness();
  h.get('#locale-path').value = 'D:/tools/LE';
  h.run(`globalThis.calls = []; translationUI.render = () => {};
    translationUI.workflow = { workflow_id: 'keep-existing', stages: {} };
    api = async (path, options) => { calls.push({path, options}); return {valid:true,directory:'D:/tools/LE',version:'2.5.0.1',message:'checked'}; };`);
  await h.run('translationUI.saveLocale()');
  assert.equal(h.run('calls[0].path'), '/api/settings/locale-emulator');
  assert.equal(h.run('calls[0].options.body.directory'), 'D:/tools/LE');
  assert.equal(h.run('translationUI.workflow.workflow_id'), 'keep-existing');
  assert.equal(h.run('translationUI.locale.valid'), true);
  assert.equal(h.get('#locale-path').value, 'D:/tools/LE');
});

test('YU-RIS deployment requires saved Locale Emulator but existing launchers stay usable', () => {
  const h = harness();
  h.get('input[name="mode"]:checked').value = 'full';
  h.run(`translationUI.workflow = {source_kind:'yuris-479',mode:'full',
    stages:{extract:{status:'completed'},translate:{status:'completed'},deploy:{status:'pending'}}};`);
  assert.match(h.run('translationUI.prerequisite("deploy")'), /Locale Emulator/);
  h.run(`translationUI.locale = {valid:true,directory:'D:/LE'};`);
  h.get('#locale-path').value = 'D:/LE';
  assert.equal(h.run('translationUI.prerequisite("deploy")'), '');
  h.get('#locale-path').value = 'D:/new-LE';
  assert.match(h.run('translationUI.prerequisite("deploy")'), /检查并保存/);
  h.run('translationUI.workflow.stages.deploy.status = "completed"; translationUI.locale = null;');
  assert.equal(h.run('translationUI.prerequisite("deploy")'), '');
});

test('reference upload control is absent from the page and script', () => {
  const html = fs.readFileSync(path.join(__dirname, '../nagi/web_ui/index.html'), 'utf8');
  assert.equal(html.includes('translation-context-file'), false);
  assert.equal(source.includes('translation-context-file'), false);
});

test('translation and deployment show prerequisite errors without any request', async () => {
  const h = harness();
  h.run('api = async () => { throw Error("Must not request API before extraction"); };');
  await h.run('translationUI.run("translate")');
  assert.match(h.get('#translate-error').textContent, /第一步/);
  assert.equal(h.get('#translate-error').hidden, false);
  await h.run('translationUI.run("deploy")');
  assert.match(h.get('#deploy-error').textContent, /第一步/);
  h.run('translationUI.workflow = {stages:{extract:{status:"completed"},translate:{status:"pending"}}};');
  await h.run('translationUI.run("deploy")');
  assert.match(h.get('#deploy-error').textContent, /第二步/);
});

test('translation requires explicit costs and redeployment matches selected scope', async () => {
  const h = harness();
  h.run('translationUI.workflow = {mode:"pilot", stages:{extract:{status:"completed"},translate:{status:"completed"}}};');
  await h.run('translationUI.run("translate")');
  assert.match(h.get('#translate-error').textContent, /费用/);
  h.get('input[name="mode"]:checked').value = 'full';
  await h.run('translationUI.run("deploy")');
  assert.match(h.get('#deploy-error').textContent, /范围已经改变/);
});

test('changing a source directory invalidates earlier step completion', () => {
  const h = harness();
  h.run('translationUI.render = () => {}; translationUI.workflow = {workflow_id:"old", stages:{extract:{status:"completed"}}};');
  h.get('#game-path').value = 'D:/different-game';
  h.get('#game-path').fire('input');
  assert.equal(h.run('translationUI.workflow'), null);
  assert.match(h.run('translationUI.prerequisite("translate")'), /第一步/);
});

test('extract request uses chosen output and does not ask for model costs', async () => {
  const h = harness();
  h.get('#game-path').value = 'D:/game';
  h.get('#storage-path').value = 'D:/chosen-results';
  h.run(`globalThis.calls = []; translationUI.render = () => {};
    window.confirm = () => { throw Error('Extraction should not require paid model consent'); };
    api = async (path, options) => { calls.push({path, options}); return {}; };`);
  await h.run('translationUI.run("extract")');
  assert.equal(h.run('calls[0].path'), '/api/translation-workflows');
  assert.equal(h.run('calls[0].options.body.storage_dir'), 'D:/chosen-results');
  assert.equal(h.run('Object.hasOwn(calls[0].options.body, "confirmed")'), false);
});

test('completed pilot reuse is explicit and never asks for paid translation', async () => {
  const h = harness();
  h.get('#game-path').value = 'D:/verified-game';
  h.get('#storage-path').value = 'D:/results';
  h.run(`globalThis.calls = []; translationUI.render = () => {};
    window.confirm = () => { throw Error('No model call is expected'); };
    api = async (path, options) => { calls.push({path, options}); return {}; };`);
  await h.run('translationUI.run("extract", true)');
  assert.equal(h.run('calls[0].path'), '/api/translation-workflows');
  assert.equal(h.run('calls[0].options.body.reuse_trial'), true);
  h.run('translationUI.workflow = {source_kind:"verified",stages:{extract:{status:"completed"}}};');
  await h.run('translationUI.run("translate")');
  assert.match(h.get('#translate-error').textContent, /旧开场试译快照/);
  assert.equal(h.run('calls.length'), 1);
});

test('settings and model buttons open real model settings without touching the draft', async () => {
  const h = harness();
  h.draft('保留草稿');
  h.run(`globalThis.calls = []; api = async (path, options) => { calls.push({path, options}); return ${JSON.stringify(modelFixture)}; };`);
  h.get('#settings-button').fire('click');
  await new Promise(setImmediate);
  assert.equal(h.get('#settings-dialog').open, true);
  assert.equal(h.get('#provider-list').children.length, 1);
  assert.equal(h.get('#model-name').textContent, 'existing-model');
  assert.equal(h.run('calls[0].path'), '/api/settings/models');
  assert.equal(h.input.value, '保留草稿');
  h.get('#settings-close').fire('click');
  assert.equal(h.get('#settings-dialog').open, false);
  h.get('#model-name').fire('click');
  await new Promise(setImmediate);
  assert.equal(h.get('#settings-dialog').open, true);
});

test('provider picker, edit, save and activation update actual API and model labels', async () => {
  const h = harness();
  h.run(`syncModelSettings(${JSON.stringify(modelFixture)}); showProviderPicker();`);
  assert.equal(h.get('#provider-presets').children.length, 1);
  h.get('#provider-presets').children[0].fire('click');
  assert.equal(h.get('#provider-base').value, 'https://api.moonshot.cn/v1');
  assert.equal(h.get('#provider-model').value, 'kimi-fixture');
  h.get('#provider-key').value = 'fake-key-for-test';
  h.run(`globalThis.calls = []; api = async (path, options) => { calls.push({path, options}); return { ...modelSettings.document, revision: 'r2', active_provider: 'kimi', providers: [{ ...options.body.provider, id: 'kimi', models: ['kimi-fixture'], api_key_configured: true }] }; };`);
  await h.run('saveProvider(true)');
  assert.equal(h.run('calls[0].path'), '/api/settings/models/save');
  assert.equal(h.run('calls[0].options.body.activate'), true);
  assert.equal(h.run('calls[0].options.body.expected_revision'), 'r1');
  assert.equal(h.get('#model-name').textContent, 'kimi-fixture');
  assert.equal(h.get('#translation-model').textContent, 'kimi-fixture');
  assert.equal(h.get('#provider-key').value, '');
  assert.equal(h.get('#provider-overview').hidden, false);
});

test('existing provider edits do not reveal stored key and Escape clears draft credentials', () => {
  const h = harness();
  h.run(`syncModelSettings(${JSON.stringify(modelFixture)}); editProvider(modelSettings.document.providers[0]);`);
  assert.equal(h.get('#provider-key').value, '');
  assert.match(h.get('#provider-key').placeholder, /留空/);
  h.get('#provider-key').value = 'fake-key-for-test';
  h.get('#settings-dialog').showModal();
  assert.equal(h.get('#settings-dialog').fire('cancel').defaultPrevented, true);
  assert.equal(h.get('#settings-dialog').open, false);
  assert.equal(h.get('#provider-key').value, '');
});

test('API errors stay in settings and testing does not switch the active model', async () => {
  const h = harness();
  h.run(`syncModelSettings(${JSON.stringify(modelFixture)}); editProvider(modelSettings.document.providers[0]); api = async () => { throw Error('密钥无效'); };`);
  await h.run('saveProvider(true)');
  assert.equal(h.get('#provider-editor').hidden, false);
  assert.equal(h.get('#settings-feedback').textContent, '密钥无效');
  assert.equal(h.run('modelSettings.busy'), false);
  h.run(`api = async () => ({ ok: true, message: '连接成功' });`);
  await h.run(`modelSettingsAction('test', { provider: providerFormValues() })`);
  assert.equal(h.get('#model-name').textContent, 'existing-model');
  assert.equal(h.get('#settings-feedback').textContent, '连接成功');
});

test('deleting active provider asks confirmation and clears selection without losing conversations', async () => {
  const h = harness();
  h.run(`syncModelSettings(${JSON.stringify(modelFixture)}); editProvider(modelSettings.document.providers[0]); window.confirm = () => false;`);
  h.get('#provider-delete').fire('click');
  assert.equal(h.run('modelSettings.document.active_provider'), 'deepseek');
  h.run(`window.confirm = () => true; api = async (path) => { if (!path.endsWith('/remove')) throw Error('wrong route'); return { ...modelSettings.document, revision: 'r2', active_provider: null, providers: [] }; };`);
  h.get('#provider-delete').fire('click');
  await new Promise(setImmediate);
  assert.equal(h.get('#model-name').textContent, '未选择模型');
  assert.equal(h.run('state.workspaces.length'), 1);
});

test('late settings reads do not reopen closed dialog or overwrite newer state', async () => {
  const h = harness();
  h.run(`globalThis.resolveSettings = null; api = () => new Promise(resolve => { resolveSettings = resolve; }); openModelSettings();`);
  h.get('#settings-close').fire('click');
  h.run(`resolveSettings(${JSON.stringify(modelFixture)});`);
  await new Promise(setImmediate);
  assert.equal(h.get('#settings-dialog').open, false);
  assert.equal(h.run('modelSettings.document'), null);
});

test('plus toggles the command list without changing draft/selection or losing editor focus', () => {
  const h = harness();
  h.draft('保留未发送的草稿', 3);
  assert.equal(h.get('#composer-add').fire('mousedown').defaultPrevented, true);
  h.get('#composer-add').fire('click');
  assert.equal(h.get('#command-menu').hidden, false);
  assert.equal(h.get('#command-menu').children.length, 7);
  assert.equal(h.document.activeElement, h.input);
  assert.equal(h.input.value, '保留未发送的草稿');
  assert.equal(h.input.selectionStart, 3);
  assert.equal(h.get('#composer-add').attributes['aria-expanded'], 'true');
  h.get('#composer-add').fire('click');
  assert.equal(h.get('#command-menu').hidden, true);
  assert.equal(h.get('#composer-add').attributes['aria-expanded'], 'false');
  assert.equal(h.input.attributes['aria-activedescendant'], undefined);
});

test('slash filters command names including ordered subsequences and Escape dismisses', () => {
  const h = harness();
  h.draft('/PRMS');
  h.input.fire('input');
  assert.equal(h.get('#command-menu').children.length, 1);
  assert.equal(h.input.attributes['aria-activedescendant'], 'command-option-permissions');
  assert.equal(h.input.fire('keydown', { key: 'Escape' }).defaultPrevented, true);
  assert.equal(h.get('#command-menu').hidden, true);
  assert.equal(h.input.value, '/PRMS');
});

test('arrow navigation and Enter execute permission action locally without changing policy', async () => {
  const h = harness();
  h.get('#approval-policy').value = 'ask';
  h.get('#composer-add').fire('click');
  for (let index = 0; index < 4; index++) h.input.fire('keydown', { key: 'ArrowDown' });
  assert.equal(h.input.attributes['aria-activedescendant'], 'command-option-permissions');
  h.input.fire('keydown', { key: 'Enter' });
  await Promise.resolve();
  assert.equal(h.get('#command-menu').hidden, true);
  assert.equal(h.get('#permission-menu').hidden, false);
  assert.equal(h.get('#approval-policy').value, 'ask');
  assert.equal(h.input.disabled, false);
});

test('click selects a command, removes only slash token, and preserves following draft', async () => {
  const h = harness();
  h.draft('/permissions 后续草稿', 5);
  h.input.fire('input');
  const option = h.get('#command-option-permissions');
  assert.equal(option.fire('mousedown').defaultPrevented, true);
  assert.equal(option.fire('click').propagationStopped, true);
  await Promise.resolve();
  assert.equal(h.input.value, ' 后续草稿');
  assert.equal(h.get('#permission-menu').hidden, false);
});

test('workspace and QLIE commands open real UI destinations without starting a translation', async () => {
  const h = harness();
  h.draft('/workspace');
  await h.run('sendMessage()');
  assert.equal(h.get('.workspace-search-box').hidden, false);
  assert.equal(h.document.activeElement, h.get('#workspace-search-input'));
  h.draft('/qlie');
  await h.run('sendMessage()');
  assert.equal(h.get('#agent-view').hidden, true);
  assert.equal(h.get('#translation-view').hidden, false);
  assert.equal(h.run('translationUI.workflow'), null);
});

test('new uses existing session creation, remains local, and prevents duplicate execution', async () => {
  const h = harness();
  h.run('globalThis.created = []; globalThis.release = null; createSessionForWorkspace = async (path) => { created.push(path); await new Promise(resolve => { release = resolve; }); };');
  h.draft('/new');
  const pending = h.run('sendMessage()');
  assert.equal(h.input.disabled, true);
  assert.equal(h.get('#composer-add').disabled, true);
  await h.run('sendMessage()');
  assert.equal(h.run('created.length'), 1);
  assert.equal(h.run('created[0]'), 'D:/nagi');
  h.run('release()');
  await pending;
  assert.equal(h.input.disabled, false);
});

test('unknown commands, empty results, and unsupported arguments never become model messages', async () => {
  const h = harness();
  h.draft('/missing');
  h.input.fire('input');
  assert.equal(h.get('#command-menu').children[0].textContent, '没有匹配的命令');
  assert.equal(h.input.fire('keydown', { key: 'Enter' }).defaultPrevented, true);
  await h.run('sendMessage()');
  assert.equal(h.input.value, '/missing');
  assert.match(h.get('#run-metrics').textContent, /未知命令/);
  h.draft('/new unsupported');
  await h.run('sendMessage()');
  assert.equal(h.input.value, '/new unsupported');
});

test('click inside composer keeps menu open; outside and opening permissions close it', () => {
  const h = harness();
  h.get('#composer-add').fire('click');
  h.document.fire('pointerdown', { target: h.input });
  assert.equal(h.get('#command-menu').hidden, false);
  h.document.fire('pointerdown', { target: h.get('#session-title') });
  assert.equal(h.get('#command-menu').hidden, true);
  h.get('#composer-add').fire('click');
  h.get('#permission-trigger').fire('click');
  assert.equal(h.get('#command-menu').hidden, true);
});

test('Chinese IME, Shift+Enter and Tab do not accidentally execute or send', () => {
  const h = harness();
  h.draft('/');
  h.input.fire('input');
  for (const event of [{ key: 'Enter', isComposing: true }, { key: 'Enter', keyCode: 229 }, { key: 'Enter', shiftKey: true }, { key: 'Tab' }]) {
    assert.equal(h.input.fire('keydown', event).defaultPrevented, undefined);
  }
  h.input.fire('compositionstart');
  assert.equal(h.get('#command-menu').hidden, true);
  h.input.fire('keydown', { key: 'Enter' });
  assert.equal(h.input.value, '/');
});

test('menu is disabled without workspace or during active job; changing view dismisses', () => {
  const h = harness();
  h.get('#composer-add').fire('click');
  h.run('state.agentJobId = "running"; updateWorkspaceHeader(); toggleCommandMenu();');
  assert.equal(h.get('#command-menu').hidden, true);
  assert.equal(h.get('#composer-add').disabled, true);
  h.run('state.agentJobId = null; state.workspacePath = null; updateWorkspaceHeader();');
  assert.equal(h.get('#composer-add').disabled, true);
  h.run('state.workspacePath = "D:/nagi"; updateWorkspaceHeader(); toggleCommandMenu(); switchView("translation");');
  assert.equal(h.get('#command-menu').hidden, true);
});

test('command failure restores draft and unlocks composer', async () => {
  const h = harness();
  h.run('createSessionForWorkspace = async () => { throw Error("创建失败"); };');
  h.draft('/new');
  await h.run('sendMessage()');
  assert.equal(h.input.value, '/new');
  assert.equal(h.input.disabled, false);
  assert.match(h.get('#run-metrics').textContent, /创建失败/);
});

test('ordinary messages still use the agent endpoint and close/lock menu during submission', async () => {
  const h = harness();
  h.run('globalThis.request = null; globalThis.release = null; api = async (path, options) => { request = { path, ...options }; return new Promise(resolve => { release = resolve; }); }; renderAgentJob = () => {}; setInterval = () => null;');
  h.get('#approval-policy').value = 'ask';
  h.draft('请分析项目');
  h.get('#composer-add').fire('click');
  const pending = h.run('sendMessage()');
  assert.equal(h.get('#command-menu').hidden, true);
  assert.equal(h.get('#composer-add').disabled, true);
  assert.equal(h.run('request.path'), '/api/agent/jobs');
  assert.equal(h.run('request.body.message'), '请分析项目');
  assert.equal(h.run('request.body.approval_policy'), 'ask');
  h.run('release({})');
  await pending;
  assert.equal(h.input.disabled, false);
});

test('failed message submission restores draft and unlocks button', async () => {
  const h = harness();
  h.run('api = async () => { throw Error("服务未连接"); };');
  h.draft('保留这条消息');
  await h.run('sendMessage()');
  assert.equal(h.input.value, '保留这条消息');
  assert.equal(h.get('#composer-add').disabled, false);
  assert.match(h.get('#run-metrics').textContent, /服务未连接/);
});

test('compact, goal and plan use session command API rather than ordinary agent message API', async () => {
  const h = harness();
  h.run('state.sessionId = "saved"; window.confirm = () => true; globalThis.requests = []; api = async (path, options) => { requests.push({ path, ...options }); return { session: { controls: { plan_mode: options.body.command === "plan" && options.body.arguments !== "off" } }, message: "已更新" }; };');
  h.draft('/compact');
  await h.run('sendMessage()');
  assert.equal(h.run('requests[0].body.command'), 'compact');
  h.draft('/goal 完成测试与验收');
  await h.run('sendMessage()');
  assert.equal(h.run('requests[1].body.arguments'), '完成测试与验收');
  h.draft('/plan 检查目录结构');
  await h.run('sendMessage()');
  assert.equal(h.run('requests[2].path'), '/api/agent/sessions/saved/command');
  assert.equal(h.get('#plan-mode-badge').hidden, false);
  assert.equal(h.get('#plan-mode-badge').disabled, false);
  h.draft('/plan off');
  await h.run('sendMessage()');
  assert.equal(h.get('#plan-mode-badge').hidden, true);
  h.draft('/goal');
  await h.run('sendMessage()');
  assert.equal(h.get('#goal-dialog').open, true);
});

test('compact confirmation can cancel without making any request', async () => {
  const h = harness();
  h.run('window.confirm = () => false;');
  h.draft('/compact');
  await h.run('sendMessage()');
  assert.equal(h.input.disabled, false);
});

test('session switches restore plan/goal state rather than leaking it to another session', async () => {
  const h = harness();
  h.run('state.sessionControls = { plan_mode: true }; api = async () => ({ title: "另一个会话", controls: { plan_mode: false }, history: [] }); renderHistory = () => {}; renderWorkspaces = () => {};');
  await h.run('selectSession("D:/nagi", "other")');
  assert.equal(h.get('#plan-mode-badge').hidden, true);
  assert.equal(h.get('#goal-badge').hidden, true);
});
