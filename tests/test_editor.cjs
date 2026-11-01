'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup() {
  const nodes = new Map();
  const requests = [];
  function makeNode() {
    return {
      value: '', textContent: '', disabled: false, hidden: false, checked: true, className: '',
      scrollTop: 0, selectionStart: 0, selectionEnd: 0, children: [], listeners: {}, dataset: {},
      addEventListener(name, handler) { this.listeners[name] = handler; },
      setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }, focus() {},
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; }
    };
  }
  function getNode(id) {
    if (!nodes.has(id)) nodes.set(id, makeNode());
    return nodes.get(id);
  }
  const context = vm.createContext({
    TextEncoder, URLSearchParams,
    document: {getElementById: getNode, createElement: () => makeNode(), querySelectorAll: () => []},
    window: {addEventListener() {}}, setInterval() {}, setTimeout: () => 0, clearTimeout() {}, confirm: () => true,
    getComputedStyle: () => ({lineHeight: '20px'}),
    fetch(url, options) {
      return new Promise((resolve, reject) => requests.push({
        url, options, reject,
        respond(doc) { resolve({ok: true, json: async () => doc}); }
      }));
    }
  });
  const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  // Keep the production editor functions intact; only skip page authentication.
  assert.equal(source.split('action(start);').length, 2);
  vm.runInContext(source.replace('action(start);', ''), context);
  function run(code) { return vm.runInContext(code, context); }
  run("state.doc = {path: 'A.md', version: 'a'}; state.dirty = true; $('editor').value = 'A draft'; updateEditor();");
  return {run, requests, getNode};
}

async function testOpenSuccess() {
  const {run, requests, getNode} = setup();
  const pending = run("openDocument('B.md')");
  assert.equal(run('state.loading'), true);
  for (const id of ['editor', 'save', 'new-document', 'move-document', 'delete-document', 'reload-document']) {
    assert.equal(getNode(id).disabled, true, id);
  }
  assert.equal(getNode('save-state').textContent, '正在读取文档…');
  await run('saveDocument()');
  await run('newDocument()');
  await run('moveDocument()');
  await run('deleteDocument()');
  await run('moveFolder()');
  await run("switchView('trash')");
  assert.equal(requests.length, 1);
  assert.equal(run('state.view'), 'library');
  requests[0].respond({path: 'B.md', content: 'B content', version: 'b'});
  await pending;
  assert.equal(run('state.doc.path'), 'B.md');
  assert.equal(getNode('editor').value, 'B content');
  assert.equal(run('state.dirty'), false);
  assert.equal(run('state.loading'), false);
  assert.equal(getNode('editor').disabled, false);
}

async function testOpenFailure() {
  const {run, requests, getNode} = setup();
  const pending = run("openDocument('B.md')");
  requests[0].reject(new Error('offline'));
  await assert.rejects(pending, /无法连接后台服务/);
  assert.equal(run('state.doc.path'), 'A.md');
  assert.equal(getNode('editor').value, 'A draft');
  assert.equal(run('state.dirty'), true);
  assert.equal(run('state.loading'), false);
  assert.equal(getNode('editor').disabled, false);
  assert.equal(getNode('save').disabled, false);
}

async function testStaleResponse() {
  const {run, requests, getNode} = setup();
  const first = run("openDocument('B.md')");
  const second = run("openDocument('C.md')");
  requests[0].respond({path: 'B.md', content: 'B content', version: 'b'});
  await first;
  assert.equal(run('state.doc.path'), 'A.md');
  assert.equal(getNode('editor').value, 'A draft');
  assert.equal(run('state.loading'), true);
  assert.equal(getNode('editor').disabled, true);
  requests[1].respond({path: 'C.md', content: 'C content', version: 'c'});
  await second;
  assert.equal(run('state.doc.path'), 'C.md');
  assert.equal(getNode('editor').value, 'C content');
  assert.equal(run('state.loading'), false);
  assert.equal(getNode('editor').disabled, false);
}

async function assertMutationLocked(fixture) {
  const {run, requests, getNode} = fixture;
  const count = requests.length;
  for (const id of ['editor', 'save', 'new-document', 'move-document', 'delete-document', 'reload-document']) {
    assert.equal(getNode(id).disabled, true, id);
  }
  await run("openDocument('B.md')");
  await run('saveDocument()');
  await run('newDocument()');
  await run('moveDocument()');
  await run('deleteDocument()');
  await run('moveFolder()');
  await run("switchView('trash')");
  assert.equal(requests.length, count);
  assert.equal(run('state.doc.path'), 'A.md');
  assert.equal(run('state.view'), 'library');
  assert.equal(getNode('editor').value, 'A draft');
}

async function testMutations() {
  for (const operation of ['deleteDocument', 'moveDocument', 'moveFolder']) {
    for (const fails of [false, true]) {
      const fixture = setup();
      const {run, requests, getNode} = fixture;
      run("loadTree=async()=>{}; loadDocuments=async()=>{}; state.directory='folder'; state.tree={path:'folder',type:'directory',version:'folder-v',files:1,children:[]};");
      // Hold the modal open to exercise its asynchronous boundary too.
      run('let closePath; askPath=()=>new Promise(resolve=>{closePath=resolve;});');
      const pending = run(operation + '()');
      if (operation !== 'deleteDocument') {
        await assertMutationLocked(fixture);
        assert.equal(requests.length, 0);
        run("closePath('moved.md')");
        await Promise.resolve();
      }
      await assertMutationLocked(fixture);
      assert.equal(requests.length, 1);
      if (fails) {
        requests[0].reject(new Error('offline'));
        await assert.rejects(pending, /无法连接后台服务/);
        assert.equal(run('state.doc.path'), 'A.md');
        assert.equal(run('state.dirty'), true);
        assert.equal(getNode('editor').value, 'A draft');
        assert.equal(getNode('editor').disabled, false);
        assert.equal(getNode('save').disabled, false);
      } else {
        requests[0].respond({ok:true});
        if (operation === 'moveDocument') {
          // The move keeps its write lock during the follow-up read.
          for (let turn = 0; turn < 10 && requests.length < 2; turn++) await Promise.resolve();
          assert.equal(requests.length, 2);
          assert.equal(run('state.saving'), true);
          assert.equal(run('state.loading'), true);
          assert.equal(getNode('editor').disabled, true);
          requests[1].respond({path:'moved.md',content:'saved original',version:'new-v'});
        }
        await pending;
        assert.equal(run('state.dirty'), false);
        if (operation === 'moveDocument') {
          assert.equal(run('state.doc.path'), 'moved.md');
          assert.equal(getNode('editor').value, 'saved original');
          assert.equal(getNode('editor').disabled, false);
        } else {
          assert.equal(run('state.doc'), null);
          assert.equal(getNode('editor').value, '');
        }
      }
      assert.equal(run('state.saving'), false);
      assert.equal(run('state.loading'), false);
      console.log('PASS ' + operation + (fails ? ' failure' : ' success'));
    }
  }
}

async function testMoveCancellation() {
  for (const operation of ['moveDocument', 'moveFolder']) {
    const {run, requests, getNode} = setup();
    run("state.directory='folder'; state.tree={path:'folder',type:'directory',version:'v',files:1}; askPath=async()=>null;");
    await run(operation + '()');
    assert.equal(requests.length, 0);
    assert.equal(run('state.saving'), false);
    assert.equal(run('state.dirty'), true);
    assert.equal(getNode('editor').value, 'A draft');
    assert.equal(getNode('editor').disabled, false);
  }
}

async function testListStaleResponse() {
  const {run, requests, getNode} = setup();
  getNode('search').value = 'alpha';
  const older = run('loadDocuments()');
  getNode('search').value = 'beta';
  const newer = run('loadDocuments()');
  requests[1].respond({total: 1, items: [{path: 'beta.md'}]});
  await newer;
  assert.equal(getNode('document-list').children[0].children[0].textContent, 'beta.md');
  requests[0].respond({total: 1, items: [{path: 'alpha.md'}]});
  await older;
  assert.equal(getNode('search').value, 'beta');
  assert.equal(getNode('document-list').children[0].children[0].textContent, 'beta.md');
  assert.match(getNode('page-info').textContent, /^1 篇/);
}

async function testSearchJump() {
  const {run, requests, getNode} = setup();
  const pending = run("openDocument('A.md', false, 3, '知识库')");
  assert.match(requests[0].url, /line=3/);
  requests[0].respond({path: 'A.md', content: 'line1\nline2\n知识库 在这里\nline4', version: 'v'});
  await pending;
  assert.equal(getNode('editor').selectionStart, 12);
  assert.equal(getNode('editor').selectionEnd, 15);
}

async function testFreshDraftRename() {
  const {run, requests, getNode} = setup();
  run("state.doc={path:'draft.md',fresh:true}; state.dirty=true; updateEditor();");
  assert.equal(getNode('move-document').disabled, false);
  run("askPath=async()=>'new.md'");
  await run('moveDocument()');
  assert.equal(run('state.doc.path'), 'new.md');
  assert.equal(requests.length, 0);
  assert.match(getNode('notice').textContent, /路径已更新/);
}

async function testUnindexedHint() {
  const {run, requests, getNode} = setup();
  run("state.tree={path:'',type:'directory',children:[{path:'附件',name:'附件',type:'directory',unsupported:3,files:3,version:'v',children:[]}]}; state.directory='附件';");
  const pending = run('loadDocuments()');
  requests[0].respond({total: 0, items: []});
  await pending;
  assert.match(getNode('document-list').children[0].textContent, /3 个文件/);
  assert.match(getNode('document-list').children[0].textContent, /未参与索引/);
}

async function testSetupView() {
  const {run, getNode} = setup();
  run("showSetup({config_path:'C:/config.json', config_error:'配置损坏'})");
  assert.equal(getNode('nav').hidden, true);
  assert.equal(getNode('setup-view').hidden, false);
  assert.equal(getNode('setup-config').textContent, '配置文件：C:/config.json');
  assert.equal(getNode('setup-error').textContent, '配置损坏');
  assert.equal(getNode('setup-error').hidden, false);
}

async function testCrashList() {
  const {run, requests, getNode} = setup();
  const pending = run('loadCrashes()');
  requests[0].respond({ok: true, dir: 'D:/kb/.data/crashes', items: [
    {created: '2026-11-01T21:00:00+08:00', role: 'background', role_label: '常驻后台', truncated: true,
      error: 'ValueError: boom', text: '记录时间: 2026-11-01 21:00:00 +0800\n异常: ValueError: boom'}
  ]});
  await pending;
  assert.equal(getNode('crash-dir').textContent, 'D:/kb/.data/crashes');
  assert.equal(getNode('clear-crashes').disabled, false);
  const record = getNode('crash-list').children[0];
  assert.match(record.children[0].children[0].textContent, /2026-11-01 21:00:00 · 常驻后台/);
  assert.equal(record.children[0].children[1].textContent, 'ValueError: boom');
  assert.match(record.children[1].textContent, /ValueError: boom/);
  assert.match(record.children[2].textContent, /预览已截断/);

  const empty = setup();
  const pendingEmpty = empty.run('loadCrashes()');
  empty.requests[0].respond({ok: true, dir: 'D:/kb/.data/crashes', items: []});
  await pendingEmpty;
  assert.equal(empty.getNode('clear-crashes').disabled, true);
  assert.match(empty.getNode('crash-list').children[0].textContent, /没有崩溃记录/);
}

async function main() {
  for (const test of [testOpenSuccess, testOpenFailure, testStaleResponse, testListStaleResponse, testMutations, testMoveCancellation, testChunkNavigation, testChunkSave, testReloadChunkOffset, testSearchJump, testFreshDraftRename, testUnindexedHint, testSetupView, testCrashList]) {
    await test();
    console.log('PASS ' + test.name);
  }
}
async function testChunkNavigation() {
  const {run, requests, getNode} = setup();
  run("state.doc={path:'large.md',version:'v',start_byte:0,end_byte:262144,next_offset:262144,total_bytes:20000000,start_line:1,has_more:true}; state.dirty=false; updateEditor();");
  assert.equal(getNode('chunk-tools').hidden, false);
  assert.match(getNode('document-size').textContent, /本段 7 字节 · 全文 20,000,000 字节/);
  run('confirm=()=>false; state.dirty=true;');
  await run('changeChunk(262144)');
  assert.equal(requests.length, 0);
  run('confirm=()=>true;');
  const pending = run('changeChunk(262144)');
  assert.equal(getNode('chunk-next').disabled, true);
  assert.match(requests[0].url, /offset=262144/);
  requests[0].reject(new Error('offline'));
  await assert.rejects(pending, /无法连接/);
  assert.equal(getNode('editor').value, 'A draft');
  assert.equal(run('state.dirty'), true);
  assert.equal(run('state.doc.start_byte'), 0);
}

async function testChunkSave() {  for (const failed of [true, false]) {
    const {run, requests, getNode} = setup();
    run("state.doc={path:'large.md',version:'v',start_byte:10,end_byte:20,next_offset:20,total_bytes:20000000,start_line:1,has_more:true}; loadTree=async()=>{}; loadDocuments=async()=>{}; updateEditor();");
    const pending = run('saveDocument()');
    const body = JSON.parse(requests[0].options.body);
    assert.equal(body.action, 'patch');
    assert.equal(body.start_byte, 10);
    assert.equal(body.end_byte, 20);
    if (failed) {
      requests[0].reject(new Error('conflict'));
      await assert.rejects(pending);
      assert.equal(run('state.dirty'), true);
      assert.equal(run('state.doc.version'), 'v');
    } else {
      requests[0].respond({version:'next',start_byte:10,end_byte:17,total_bytes:19999997});
      for (let turn = 0; turn < 10 && requests.length < 2; turn++) await Promise.resolve();
      assert.equal(requests.length, 2);
      assert.equal(run('state.saving'), true);
      assert.equal(run('state.doc.end_byte'), 17);
      requests[1].reject(new Error('read failed'));
      await assert.rejects(pending);
      assert.equal(run('state.doc.version'), 'next');
      assert.equal(run('state.dirty'), false);
    }
    assert.equal(getNode('editor').value, 'A draft');
    assert.equal(run('state.saving'), false);
  }
}
async function testReloadChunkOffset() {
  const {run, requests, getNode} = setup();
  run("state.doc={path:'large.md',version:'v',start_byte:5242880,end_byte:5505024,next_offset:5505024,total_bytes:20000000,start_line:1,has_more:true}; state.dirty=false; updateEditor();");
  const pending = run("$('reload-document').onclick()");
  for (let turn = 0; turn < 5 && requests.length < 1; turn++) await Promise.resolve();
  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /offset=5242880/);
  requests[0].respond({path: 'large.md', content: 'chunk', start_byte: 5242880, end_byte: 5505024,
    total_bytes: 20000000, start_line: 1, has_more: true, version: 'v'});
  await pending;
  assert.equal(run('state.doc.start_byte'), 5242880);
  assert.equal(getNode('editor').value, 'chunk');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
