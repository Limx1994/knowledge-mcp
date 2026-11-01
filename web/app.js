'use strict';
const $ = id => document.getElementById(id);
const state = {csrf: '', view: 'library', directory: '', tree: null, offset: 0, listSerial: 0,
  limit: 20, trashLimit: 20, trashOffset: 0, doc: null, dirty: false, saving: false, loading: false, openSerial: 0,
  authenticated: false, configured: false};
const expanded = new Set(['']);
let noticeTimer = 0;
function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function notice(message, error = false) {
  clearTimeout(noticeTimer);
  $('notice').textContent = message;
  $('notice').className = error ? 'error' : '';
  $('notice').hidden = !message;
  if (message) noticeTimer = setTimeout(() => { $('notice').hidden = true; $('notice').textContent = ''; }, error ? 8000 : 4000);
}
async function api(path, body) {
  const options = {credentials: 'same-origin', headers: {}};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.headers['X-CSRF-Token'] = state.csrf;
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('无法连接后台服务，请检查托盘图标是否仍在运行。'); }
  let result;
  try { result = await response.json(); }
  catch { throw new Error('后台返回了无法识别的响应。'); }
  if (!response.ok || result.ok === false) throw new Error(result.error || '操作失败，请查看索引明细或服务日志。');
  return result;
}
function action(task) { return Promise.resolve().then(task).catch(error => notice(error.message, true)); }
function canLeave() { return !state.dirty || confirm('当前文档有未保存的修改，确定放弃这些修改吗？'); }
function updateEditor() {
  const doc = state.doc;
  $('editor-title').textContent = doc ? doc.path : '选择一篇文档';
  $('save-state').textContent = state.loading ? '正在读取文档…' : state.saving ? '正在处理…' : state.dirty ? '有未保存的修改' : doc ? '已与本地文件同步' : '在左侧打开资料，或新建一篇文档。';
  $('save').disabled = !doc || !state.dirty || state.saving || state.loading;
  $('editor').disabled = !doc || state.saving || state.loading;
  $('new-document').disabled = state.saving || state.loading;
  $('move-document').disabled = !doc || state.saving || state.loading;
  for (const id of ['delete-document', 'reload-document']) $(id).disabled = !doc || doc.fresh || state.saving || state.loading;
  const chunked = doc?.start_byte !== undefined;
  const bytes = doc ? new TextEncoder().encode($('editor').value).length : 0;
  $('document-size').textContent = doc
    ? chunked ? `本段 ${bytes.toLocaleString()} 字节 · 全文 ${doc.total_bytes.toLocaleString()} 字节 · UTF-8` : `${bytes.toLocaleString()} 字节 · UTF-8`
    : 'Markdown / 纯文本 · UTF-8';
  $('chunk-tools').hidden = !chunked;
  $('chunk-range').textContent = chunked ? `${doc.start_byte.toLocaleString()}–${doc.end_byte.toLocaleString()} / ${doc.total_bytes.toLocaleString()} 字节 · 第 ${doc.start_line} 行起` : '';
  $('chunk-previous').disabled = !chunked || doc.start_byte <= (doc.bom ? 3 : 0) || state.saving || state.loading;
  $('chunk-next').disabled = !chunked || !doc.has_more || state.saving || state.loading;
  $('chunk-jump').disabled = !chunked || state.saving || state.loading;
  $('chunk-offset').disabled = !chunked || state.saving || state.loading;
}
function clearEditor() {
  state.openSerial++;
  state.doc = null; state.dirty = false; state.loading = false; $('editor').value = ''; updateEditor();
}
function findFolder(path, node = state.tree) {
  if (!node) return null;
  if (node.path === path && node.type === 'directory') return node;
  for (const child of node.children || []) { const found = findFolder(path, child); if (found) return found; }
  return null;
}
function renderTree() {
  const container = $('tree'); container.replaceChildren();
  function render(node, parent) {
    const button = element('button', node.path ? node.name : '全部资料', node.path === state.directory ? 'selected' : '');
    button.title = node.path || 'knowledge/';
    button.onclick = event => {
      event.preventDefault();
      if (node.path) {
        if (expanded.has(node.path)) expanded.delete(node.path);
        else expanded.add(node.path);
      }
      action(async () => {
        state.directory = node.path; state.offset = 0; $('search').value = '';
        renderTree(); await loadDocuments();
      });
    };
    const folders = node.children.filter(n => n.type === 'directory');
    if (!node.path) parent.append(button);
    if (node.path) {
      const details = element('details'); const summary = element('summary'); summary.append(button); details.append(summary);
      details.open = expanded.has(node.path);
      details.ontoggle = () => details.open ? expanded.add(node.path) : expanded.delete(node.path);
      parent.append(details); parent = details;
    }
    for (const child of folders) render(child, parent);
  }
  if (state.tree) render(state.tree, container);
  $('move-folder').disabled = !state.directory;
  $('delete-folder').disabled = !state.directory;
}
async function loadTree() {
  const result = await api('/api/tree'); state.tree = result.tree;
  if (!findFolder(state.directory)) state.directory = '';
  renderTree();
}
async function loadDocuments() {
  const serial = ++state.listSerial;
  const snapshot = {directory: state.directory, offset: state.offset,
    query: $('search').value, recursive: $('recursive').checked};
  $('directory-title').textContent = snapshot.directory.split('/').pop() || '全部资料';
  $('document-list').replaceChildren(element('div', '正在读取文档…', 'empty'));
  const params = new URLSearchParams({directory: snapshot.directory, recursive: String(snapshot.recursive), offset: String(snapshot.offset), limit: String(state.limit), query: snapshot.query});
  const result = await api('/api/documents?' + params);
  if (serial !== state.listSerial || snapshot.directory !== state.directory || snapshot.offset !== state.offset
      || snapshot.query !== $('search').value || snapshot.recursive !== $('recursive').checked) return;
  const list = $('document-list'); list.replaceChildren();
  for (const item of result.items) {
    const button = element('button', undefined, 'document-row' + (state.doc?.path === item.path ? ' active' : ''));
    button.append(element('strong', item.path.split('/').pop()), element('small', item.path));
    if (item.snippets?.length) button.append(element('p', `第 ${item.snippets[0].line} 行 · ${item.snippets[0].text}`));
    button.onclick = () => action(() => openDocument(item.path, false, item.snippets?.[0]?.line, searchWord()));
    list.append(button);
  }
  const folder = findFolder(state.directory);
  const unindexed = folder?.unsupported || 0;
  const searching = Boolean(snapshot.query.trim());
  if (!result.items.length) {
    const text = searching ? '没有找到匹配的文档。试试更短的关键词。'
      : unindexed ? `此目录及子目录有 ${unindexed} 个文件，但不是 .md 或 .txt，未参与索引。`
        : '这个目录还没有文档。\n点击“新建文档”开始整理。';
    list.append(element('div', text, 'empty'));
  } else if (unindexed && !searching) {
    list.append(element('div', `另有 ${unindexed} 个文件未参与索引（含子目录，仅支持 .md / .txt）。`, 'unindexed'));
  }
  const limit = state.limit, page = Math.floor(snapshot.offset / limit) + 1, pages = Math.max(1, Math.ceil(result.total / limit));
  $('page-info').textContent = `${result.total} 篇 · 第 ${page} / ${pages} 页`;
  $('page-info').dataset.total = result.total;
  $('page-jump').value = page;
  $('previous').disabled = snapshot.offset === 0; $('next').disabled = snapshot.offset + limit >= result.total;
}
async function openDocument(path, force = false, line, word) {
  if (state.saving || (!force && !canLeave())) return;
  return readDocument(path, undefined, line, word);
}
function searchWord() {
  const value = $('search').value.trim();
  const match = value.match(/"([^"]+)"|(\S+)/);
  return (match?.[1] || match?.[2] || '').toLowerCase();
}
function focusLine(line, word) {
  const editor = $('editor');
  const doc = state.doc;
  if (!doc || !line) {
    editor.setSelectionRange(0, 0);
    editor.scrollTop = 0;
    return;
  }
  if (doc.start_byte !== undefined) {
    const last = doc.start_line + (editor.value.match(/\n/g) || []).length;
    if (line < doc.start_line || line > last) {
      notice(`第 ${line} 行不在当前片段（${doc.start_line}–${last} 行），可用“跳转到字节”定位。`);
      editor.setSelectionRange(0, 0);
      editor.scrollTop = 0;
      return;
    }
    line = line - doc.start_line + 1;
  }
  const text = editor.value;
  let pos = 0;
  for (let current = 1; current < line; current++) {
    const next = text.indexOf('\n', pos);
    if (next === -1) { pos = text.length; break; }
    pos = next + 1;
  }
  const end = text.indexOf('\n', pos);
  const lineText = text.slice(pos, end === -1 ? text.length : end);
  let start = pos;
  let length = 0;
  if (word) {
    const at = lineText.toLowerCase().indexOf(word);
    if (at >= 0) { start = pos + at; length = word.length; }
  }
  editor.focus();
  editor.setSelectionRange(start, start + length);
  const lineHeight = parseFloat(getComputedStyle(editor).lineHeight) || 20;
  editor.scrollTop = Math.max(0, editor.scrollTop - lineHeight * 3);
}
async function readDocument(path, offset, line, word) {
  const serial = ++state.openSerial;
  let loaded = false;
  state.loading = true; updateEditor();
  try {
    const params = {path, auto: 'true'};
    if (offset !== undefined) params.offset = String(offset);
    if (line !== undefined) params.line = String(line);
    const result = await api('/api/documents?' + new URLSearchParams(params));
    if (serial !== state.openSerial) return;
    state.doc = result; state.dirty = false; $('editor').value = result.content;
    notice('');
    loaded = true;
  } finally {
    if (serial === state.openSerial) {
      state.loading = false; updateEditor();
      if (loaded) focusLine(line, word);
    }
  }
}
async function changeChunk(offset) {
  if (!state.doc || state.saving || state.loading || !canLeave()) return;
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > state.doc.total_bytes) throw new Error('请输入文件范围内的整数字节位置。');
  await readDocument(state.doc.path, offset);
}
async function askPath(title, description, value) {
  const dialog = $('path-dialog');
  $('dialog-title').textContent = title; $('dialog-description').textContent = description;
  $('path-input').value = value;
  return new Promise(resolve => {
    let result = null;
    $('path-form').onsubmit = event => { event.preventDefault(); result = $('path-input').value.trim(); if (result) dialog.close(); };
    $('dialog-cancel').onclick = () => dialog.close();
    dialog.onclose = () => resolve(result);
    dialog.showModal(); $('path-input').focus(); $('path-input').select();
  });
}
async function saveDocument() {
  if (!state.doc || !state.dirty || state.saving || state.loading) return;
  state.saving = true; updateEditor();
  try {
    const chunked = state.doc.start_byte !== undefined;
    const body = {action: state.doc.fresh ? 'create' : chunked ? 'patch' : 'update', path: state.doc.path, content: $('editor').value, expected_version: state.doc.version || ''};
    if (chunked) { body.start_byte = state.doc.start_byte; body.end_byte = state.doc.end_byte; }
    const result = await api('/api/documents', body);
    state.doc = {...state.doc, version: result.version, fresh: false}; state.dirty = false;
    if (chunked) {
      // The write has committed. Update its range before the follow-up read so
      // a failed read cannot leave stale offsets available to the next save.
      state.doc.end_byte = result.end_byte; state.doc.next_offset = result.end_byte;
      state.doc.total_bytes = result.total_bytes; state.doc.has_more = result.end_byte < result.total_bytes;
      await readDocument(state.doc.path, state.doc.start_byte);
    }
    notice('文档已保存，索引已同步。'); await loadTree(); await loadDocuments();
  } finally { state.saving = false; updateEditor(); }
}
async function newDocument() {
  if (state.saving || state.loading || !canLeave()) return;
  const path = await askPath('新建文档', '输入文档名称或多级路径，缺失的父目录会自动创建。', (state.directory ? state.directory + '/' : '') + '新文档.md');
  if (!path) return;
  if (!/\.(md|txt)$/i.test(path)) throw new Error('文档路径必须以 .md 或 .txt 结尾。');
  state.openSerial++; state.doc = {path, fresh: true}; state.dirty = true; $('editor').value = ''; updateEditor(); $('editor').focus();
}
async function moveDocument() {
  if (!state.doc || state.saving || state.loading) return;
  if (state.doc.fresh) {
    const path = state.doc.path;
    const target = await askPath('重命名草稿', '草稿尚未保存，修改路径后保存即可创建。', path);
    if (!target || target === path) return;
    if (!/\.(md|txt)$/i.test(target)) throw new Error('文档路径必须以 .md 或 .txt 结尾。');
    state.doc.path = target;
    updateEditor();
    notice('草稿路径已更新，保存后创建。');
    return;
  }
  if (!canLeave()) return;
  state.saving = true; updateEditor();
  try {
    const path = state.doc.path;
    const target = await askPath('重命名 / 移动文档', '输入目标完整路径；已有文档不会被覆盖。', path);
    if (!target || target === path) return;
    await api('/api/documents', {action: 'move', path, target, expected_version: state.doc.version});
    await readDocument(target); await loadTree(); await loadDocuments(); notice('文档已移动，索引已同步。');
  } finally { state.saving = false; updateEditor(); }
}
async function deleteDocument() {
  if (!state.doc || state.saving || state.loading || !canLeave()) return;
  if (!confirm(`将“${state.doc.path}”移入回收站？之后可以恢复。`)) return;
  state.saving = true; updateEditor();
  try {
    await api('/api/documents', {action: 'delete', path: state.doc.path, expected_version: state.doc.version});
    clearEditor(); await loadTree(); await loadDocuments(); notice('文档已移入回收站。');
  } finally { state.saving = false; updateEditor(); }
}
async function newFolder() {
  const path = await askPath('新建文件夹', '创建一个空文件夹，支持多级目录。', state.directory ? state.directory + '/' : '');
  if (!path) return;
  await api('/api/tree', {action: 'create', path}); expanded.add(path); await loadTree(); notice('文件夹已创建。');
}
async function moveFolder() {
  if (state.saving || state.loading || !canLeave()) return;
  const folder = findFolder(state.directory); if (!folder || !folder.path) return;
  state.saving = true; updateEditor();
  try {
    const target = await askPath('重命名 / 移动文件夹', `将移动整个文件夹及其 ${folder.files} 个文件。输入目标完整路径。`, folder.path);
    if (!target || target === folder.path) return;
    if (!confirm(`确认将“${folder.path}”及其中全部 ${folder.files} 个文件移动到“${target}”？`)) return;
    await api('/api/tree', {action: 'move', path: folder.path, target, expected_version: folder.version});
    clearEditor(); state.directory = target; expanded.add(target); await loadTree(); await loadDocuments(); notice('文件夹已移动，索引已同步。');
  } finally { state.saving = false; updateEditor(); }
}
async function deleteFolder() {
  const folder = findFolder(state.directory); if (!folder || !folder.path) return;
  if (folder.children.length) throw new Error('只能删除空文件夹，请先移动或处理其中内容。');
  if (!confirm(`删除空文件夹“${folder.path}”？空目录删除不进入回收站。`)) return;
  await api('/api/tree', {action: 'delete', path: folder.path, expected_version: folder.version});
  state.directory = ''; await loadTree(); await loadDocuments(); notice('空文件夹已删除。');
}
async function loadTrash() {
  const result = await api('/api/trash?' + new URLSearchParams({offset: String(state.trashOffset), limit: String(state.trashLimit)}));
  const list = $('trash-list'); list.replaceChildren();
  for (const record of result.items) {
    const row = element('div', undefined, 'record'); const info = element('div');
    info.append(element('strong', record.path), element('small', `删除于 ${new Date(record.deleted_at).toLocaleString('zh-CN')}`));
    const button = element('button', '恢复原位置');
    button.onclick = () => action(async () => { button.disabled = true; try { await api('/api/trash', {action: 'restore', record_id: record.record_id}); notice('文档已恢复。'); await loadTrash(); } finally { button.disabled = false; } });
    row.append(info, button); list.append(row);
  }
  if (!result.items.length) list.append(element('div', '回收站为空。', 'empty'));
  $('trash-info').textContent = `共 ${result.total} 条记录`;
  $('trash-info').dataset.total = result.total;
  $('trash-jump').value = Math.floor(state.trashOffset / state.trashLimit) + 1;
  $('trash-previous').disabled = state.trashOffset === 0; $('trash-next').disabled = state.trashOffset + state.trashLimit >= result.total;
}
function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MiB`;
  return `${(bytes / 1073741824).toFixed(2)} GiB`;
}
function renderReport(target, report) {
  if (!report) { target.textContent = '暂无记录'; return; }
  if (report.error) { target.textContent = report.error; return; }
  const labels = {added: '新增', updated: '更新', deleted: '删除', unchanged: '未变', skipped: '跳过', failed: '失败'};
  const lines = [Object.entries(labels).map(([key, label]) => `${label} ${report[key] ?? 0}`).join(' · ')];
  const issues = report.issues || [];
  if (issues.length) {
    lines.push(`明细 ${report.issues_total} 条${report.issues_truncated ? '（仅显示前 200 条）' : ''}`);
    for (const issue of issues.slice(0, 20)) {
      lines.push(`${issue.kind === 'skipped' ? '跳过' : '失败'}  ${issue.path}  ${issue.reason}`);
    }
  } else if (report.failed) {
    lines.push(`失败 ${report.failed} 条，执行“完整重建”可查看明细。`);
  }
  if (report.completed_at) lines.push(`完成于 ${new Date(report.completed_at).toLocaleString('zh-CN')}`);
  target.textContent = lines.join('\n');
}
async function updateStatus() {
  const status = await api('/api/status'); $('connection').textContent = `后台运行中 · ${status.connections} 个 MCP 连接`;
  const list = $('service-status'); list.replaceChildren();
  for (const [key, value] of [['知识库位置', status.root], ['后台状态', status.stopping ? '正在退出' : '运行中'], ['文档数量', status.documents], ['数据库大小', formatBytes(status.database_bytes)], ['MCP 连接数', status.connections], ['崩溃记录', status.crashes ? `${status.crashes} 份` : '无'], ['最近同步', status.last_sync?.completed_at ? new Date(status.last_sync.completed_at).toLocaleString('zh-CN') : '暂无记录']]) list.append(element('dt', key), element('dd', String(value)));
  renderReport($('sync-report'), status.last_sync);
}
async function loadCrashes() {
  const result = await api('/api/crashes');
  $('crash-dir').textContent = result.dir;
  const list = $('crash-list'); list.replaceChildren();
  $('clear-crashes').disabled = result.items.length === 0;
  if (!result.items.length) { list.append(element('p', '没有崩溃记录。', 'empty')); return; }
  for (const item of result.items) {
    const details = element('details', undefined, 'crash-record');
    const summary = element('summary');
    const when = item.created ? item.created.replace('T', ' ').slice(0, 19) : '未知时间';
    summary.append(element('strong', `${when} · ${item.role_label}`), element('small', item.error));
    details.append(summary, element('pre', item.text));
    if (item.truncated) details.append(element('p', '预览已截断，完整内容见记录文件。', 'hint'));
    list.append(details);
  }
}
async function loadIndex() {
  const job = await api('/api/index'); const running = job.state === 'running';
  $('refresh-index').disabled = running; $('rebuild-index').disabled = running;
  $('index-state').textContent = {idle: '尚未执行手动索引任务', running: '正在执行，请稍候…', completed: '任务完成', failed: '任务失败，请查看明细'}[job.state] || job.state;
  renderReport($('index-report'), job.report);
}
async function refreshIndex(full) {
  if (full && !confirm('完整重建会重新读取全部资料，确认开始？')) return;
  await api('/api/index', {full}); await loadIndex(); notice('索引任务已开始，可在本页查看执行结果。');
}
async function switchView(view) {
  if (state.saving || state.loading || (view !== state.view && !canLeave())) return;
  if (view !== state.view && state.dirty) clearEditor();
  state.view = view;
  for (const name of ['library', 'trash', 'status']) $(name + '-view').hidden = name !== view;
  for (const button of document.querySelectorAll('[data-view]')) button.classList.toggle('active', button.dataset.view === view);
  notice('');
  if (view === 'library') { await loadTree(); await loadDocuments(); }
  if (view === 'trash') await loadTrash();
  if (view === 'status') { await updateStatus(); await loadIndex(); await loadCrashes(); }
}
$('editor').addEventListener('input', () => { state.dirty = true; updateEditor(); });
$('save').onclick = () => action(saveDocument);
$('chunk-previous').onclick = () => action(() => changeChunk(Math.max(state.doc.bom ? 3 : 0, state.doc.start_byte - 262144)));
$('chunk-next').onclick = () => action(() => changeChunk(state.doc.next_offset));
$('chunk-jump').onclick = () => action(() => changeChunk(Number($('chunk-offset').value)));
$('new-document').onclick = () => action(newDocument);
$('move-document').onclick = () => action(moveDocument);
$('delete-document').onclick = () => action(deleteDocument);
$('reload-document').onclick = () => action(async () => {
  if (!state.doc || state.saving || state.loading || !canLeave()) return;
  const offset = state.doc.start_byte !== undefined ? state.doc.start_byte : undefined;
  await readDocument(state.doc.path, offset);
});
$('new-folder').onclick = () => action(newFolder);
$('move-folder').onclick = () => action(moveFolder);
$('delete-folder').onclick = () => action(deleteFolder);
$('refresh').onclick = () => action(async () => { await loadTree(); await loadDocuments(); notice('目录与列表已刷新，编辑中的正文未被覆盖。'); });
$('refresh-crashes').onclick = () => action(async () => { await loadCrashes(); await updateStatus(); notice('崩溃记录已刷新。'); });
$('clear-crashes').onclick = () => action(async () => {
  if (!confirm('清空全部崩溃记录？该操作不可恢复。')) return;
  const result = await api('/api/crashes', {action: 'clear'});
  await loadCrashes(); await updateStatus(); notice(`已清空 ${result.removed} 份崩溃记录。`);
});
$('search-form').onsubmit = event => { event.preventDefault(); state.offset = 0; action(loadDocuments); };
$('recursive').onchange = () => { state.offset = 0; action(loadDocuments); };
$('previous').onclick = () => { state.offset = Math.max(0, state.offset - state.limit); action(loadDocuments); };
$('next').onclick = () => { state.offset += state.limit; action(loadDocuments); };
$('page-size').onchange = () => { state.limit = Number($('page-size').value); state.offset = 0; action(loadDocuments); };
$('page-jump-btn').onclick = () => { const total = Number($('page-info').dataset.total || 0); const pages = Math.max(1, Math.ceil(total / state.limit)); const p = Math.min(pages, Math.max(1, Number($('page-jump').value) || 1)); state.offset = (p - 1) * state.limit; action(loadDocuments); };
$('page-jump').onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); $('page-jump-btn').click(); } };
$('refresh-trash').onclick = () => action(loadTrash);
$('trash-previous').onclick = () => { state.trashOffset = Math.max(0, state.trashOffset - state.trashLimit); action(loadTrash); };
$('trash-next').onclick = () => { state.trashOffset += state.trashLimit; action(loadTrash); };
$('trash-page-size').onchange = () => { state.trashLimit = Number($('trash-page-size').value); state.trashOffset = 0; action(loadTrash); };
$('trash-jump-btn').onclick = () => { const total = Number($('trash-info').dataset.total || 0); const pages = Math.max(1, Math.ceil(total / state.trashLimit)); const p = Math.min(pages, Math.max(1, Number($('trash-jump').value) || 1)); state.trashOffset = (p - 1) * state.trashLimit; action(loadTrash); };
$('trash-jump').onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); $('trash-jump-btn').click(); } };
$('refresh-index').onclick = () => action(() => refreshIndex(false));
$('rebuild-index').onclick = () => action(() => refreshIndex(true));
for (const button of document.querySelectorAll('[data-view]')) button.onclick = () => action(() => switchView(button.dataset.view));
window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
window.addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); action(saveDocument); } });
function showSetup(status) {
  state.configured = false;
  $('nav').hidden = true;
  for (const name of ['library', 'trash', 'status']) $(name + '-view').hidden = true;
  $('setup-view').hidden = false;
  $('connection').textContent = '等待配置';
  $('setup-config').textContent = `配置文件：${status.config_path}`;
  if (status.config_error) {
    $('setup-error').textContent = status.config_error;
    $('setup-error').hidden = false;
  }
  $('setup-root').focus();
}
async function start() {
  const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket');
  history.replaceState(null, '', location.pathname);
  const session = await api('/api/session', ticket ? {ticket} : undefined); state.csrf = session.csrf; state.authenticated = true;
  const status = await api('/api/status');
  if (!status.configured) { showSetup(status); return; }
  state.configured = true;
  $('setup-view').hidden = true;
  await loadTree(); await loadDocuments(); await updateStatus();
}
action(start);
$('setup-form').onsubmit = event => {
  event.preventDefault();
  const root = $('setup-root').value.trim();
  const create = $('setup-create').checked;
  action(async () => {
    try {
      await api('/api/setup', {root, create});
      location.reload();
    } catch (error) {
      $('setup-error').textContent = error.message;
      $('setup-error').hidden = false;
    }
  });
};
setInterval(() => {
  if (document.hidden || !state.authenticated || !state.configured) return;
  action(async () => { await updateStatus(); if (state.view === 'status') await loadIndex(); });
}, 5000);
