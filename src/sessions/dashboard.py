"""Dashboard HTML for Sessions."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<title>Sessions</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, Segoe UI, sans-serif;
         margin: 0 auto; padding: 0; background: #0f172a; color: #e2e8f0;
         font-size: 14px; max-width: 90%; }
  .sticky-header { position:sticky; top:0; z-index:100; background:#0f172a;
                   padding:16px 16px 0; }
  h1 { margin: 0 0 12px; font-size: 19px; display:flex; align-items:center;
       justify-content:space-between; }
  .toolbar { display:flex; gap:6px; align-items:center; }
  button { background:#1e293b; color:#e2e8f0; border:1px solid #334155;
           border-radius:5px; padding:4px 10px; cursor:pointer;
           font-size:13px; }
  button:hover { background:#334155; }
  button.primary { background:#3b82f6; border-color:#3b82f6; color:white; }
  button.danger  { background:#7f1d1d; border-color:#7f1d1d; color:white; }
  button.warm    { background:#92400e; border-color:#92400e; color:white; }
  .bulk-bar { background:#1e293b; border:1px solid #334155;
              border-radius:8px; padding:6px 10px; margin-bottom:8px;
              display:flex; align-items:center; gap:8px; }
  .bulk-bar .bulk-left { display:flex; align-items:center; gap:8px; }
  .bulk-bar .bulk-left input[type=checkbox] { width:15px; height:15px;
              accent-color:#3b82f6; cursor:pointer; }
  .bulk-bar .bulk-count { font-size:13px; color:#94a3b8; }
  .bulk-bar .spacer { flex:1; }
  /* search */
  .search-wrap { display:flex; align-items:center; gap:6px; margin-bottom:8px; }
  #search-box { flex:1; background:#1e293b; color:#e2e8f0;
                border:1px solid #334155; border-radius:6px;
                padding:6px 10px; font-size:14px; outline:none; }
  #search-box:focus { border-color:#3b82f6; }
  #search-box::placeholder { color:#475569; }
  #search-clear { display:none; background:none; border:none; color:#64748b;
                  font-size:16px; cursor:pointer; padding:0 4px; line-height:1; }
  #search-clear:hover { color:#e2e8f0; }
  /* session list */
  .list { display:grid; grid-template-columns:1fr 1fr; gap:0 16px;
         padding:8px 16px 16px; align-items:start; }
  .col { display:flex; flex-direction:column; gap:6px; }
  .col-header { font-size:11px; color:#64748b; text-transform:uppercase;
                letter-spacing:0.05em; padding:2px 4px 4px; font-weight:600; }
  .col-empty { color:#334155; font-size:12px; font-style:italic; padding:4px 10px; }
  .row { background:#1e293b; border:1px solid #334155; border-radius:6px;
         border-left:3px solid #334155; transition:border-color 0.2s; }
  .row.hot  { border-left-color:#22c55e; }
  .row.cold { border-left-color:#f59e0b; }
  .row.selected { outline:2px solid #3b82f6; outline-offset:-2px; }
  .row-cb { flex:0 0 auto; display:flex; align-items:flex-start;
            padding:5px 2px 0 8px; }
  .row-cb input[type=checkbox] { width:15px; height:15px;
            accent-color:#3b82f6; cursor:pointer; }
  .spacer { flex:1; }
  /* tabs — first tab is the row header */
  .tabs { display:flex; flex-direction:column; }
  .tab { display:flex; align-items:center; gap:6px; padding:5px 10px;
         border-radius:4px; font-size:13px; color:#cbd5e1;
         cursor:pointer; user-select:none; min-width:0; }
  .tab:hover, .tab.focused { background:#334155; }
  .tabs .tab:not(:last-child) { border-bottom:1px solid #0f172a; }
  /* row-level hover action buttons (Hibernate/Restore + Delete) */
  .row-actions { display:inline-flex; gap:2px; flex:0 0 auto; opacity:0;
                 transition:opacity 0.15s; }
  .row:hover .row-actions { opacity:1; }
  .row-actions .row-action { opacity:1; }
  .row-actions .action-sep { opacity:0.6; }
  .row-action { display:flex; align-items:center; justify-content:center;
                width:26px; height:26px; border-radius:4px;
                cursor:pointer; color:#64748b; opacity:0;
                transition:opacity 0.15s, color 0.15s, background 0.15s; }
  .tab:hover .row-action { opacity:1; }
  .row-action:hover { background:#475569; color:#e2e8f0; }
  .row-action.danger:hover { color:#f87171; background:#3b1818; }
  .action-sep { width:1px; height:16px; background:#475569; margin:0 2px; flex:0 0 auto;
                opacity:0; transition:opacity 0.15s; }
  .tab:hover .action-sep { opacity:0.6; }
  /* tab-num removed — checkboxes replace numbering */
  .tab-body { flex:1; display:flex; align-items:center; gap:6px;
              overflow:hidden; min-width:0; }
  .tab-title { flex:0 0 auto; overflow:hidden;
               text-overflow:ellipsis; white-space:nowrap; max-width:200px; }
  .tab-url { color:#64748b; overflow:hidden; text-overflow:ellipsis;
             white-space:nowrap; flex:1 1 auto; font-size:12px; min-width:0; max-width:300px; }
  .tab-close { flex:0 0 auto; opacity:0; cursor:pointer; font-size:15px;
               padding:0 2px; }
  .tab:hover .tab-close { opacity:0.4; }
  .tab-close:hover { opacity:1 !important; color:#f87171; }
  /* cut/paste mode */
  .tab.cut-active { background:#7c3aed33; outline:1px solid #7c3aed; }
  .paste-target { cursor:pointer; animation:pastePulse 1.5s infinite; }
  .paste-target:hover { outline:2px solid #7c3aed; outline-offset:-2px; }
  @keyframes pastePulse { 0%,100%{border-left-color:#7c3aed} 50%{border-left-color:#a78bfa} }
  .paste-btn { display:inline-flex; align-items:center; justify-content:center;
               width:26px; height:26px; border-radius:4px;
               cursor:pointer; color:#a78bfa; transition:color 0.15s, background 0.15s;
               flex:0 0 auto; }
  .paste-btn:hover { background:#7c3aed33; color:#e2e8f0; }
  .tab-cut { flex:0 0 auto; opacity:0; cursor:pointer; font-size:13px;
             padding:0 2px; color:#64748b; }
  .tab:hover .tab-cut { opacity:0.5; }
  .tab-cut:hover { opacity:1 !important; color:#a78bfa; }
  /* empty session row */
  .row-empty { display:flex; align-items:center; gap:8px; padding:6px 10px;
               cursor:default; user-select:none; color:#475569; font-size:13px;
               min-width:0; }
  .row-empty .row-actions { margin-left:auto; }
  /* search results */
  .search-row { background:#1e293b; border:1px solid #334155; border-radius:6px;
                display:flex; align-items:center; gap:8px; padding:6px 10px;
                cursor:pointer; }
  .search-row:hover, .search-row.focused { background:#334155; }
  .search-hot { border-left:3px solid #22c55e; }
  .search-cold { border-left:3px solid #f59e0b; }
  .search-session { font-size:12px; color:#64748b; flex:0 0 auto;
                    min-width:24px; text-align:right; }
  /* context menu */
  #ctx-menu { position:fixed; background:#1e293b; border:1px solid #475569;
              border-radius:6px; padding:4px 0; z-index:9000;
              box-shadow:0 8px 24px rgba(0,0,0,0.5); min-width:140px;
              display:none; }
  #ctx-menu.open { display:block; }
  .ctx-item { padding:6px 14px; cursor:pointer; font-size:13px;
              white-space:nowrap; color:#e2e8f0; }
  .ctx-item:hover { background:#334155; }
  .ctx-sep { border-top:1px solid #334155; margin:4px 0; }
  .ctx-item.danger { color:#f87171; }
  /* misc */
  #toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
           background:#334155; color:#f1f5f9; padding:10px 24px;
           border-radius:8px; font-size:15px; font-weight:500;
           opacity:0; transition:opacity 0.3s; pointer-events:none;
           z-index:9999; white-space:nowrap;
           box-shadow:0 4px 12px rgba(0,0,0,0.4); }
  #toast.show { opacity:1; }
  #disconnected { display:none; background:#7f1d1d; color:#fecaca;
                  padding:8px 16px; border-radius:8px; margin-bottom:8px;
                  font-size:14px; font-weight:500; text-align:center;
                  animation:pulse 2s infinite; }
  #disconnected.visible { display:block; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.6} }
</style>
</head>
<body>
<div class=sticky-header>
<h1>Sessions <span class=toolbar>
  <button class=primary onclick=createSession()>+ New</button>
  <button onclick=cleanDefault()>Clean</button>
  <button class=warm onclick=restartBackend()>Restart</button>
  <button id=trim-log-btn style=display:none onclick=trimLog()>Trim Log</button>
  <button class=danger onclick=quitDaemon()>Quit</button>
</span></h1>
<div class=bulk-bar>
  <div class=bulk-left>
    <input type=checkbox id=selAll title="Select / deselect all"
           onchange="toggleSelectAll(this.checked)">
    <span class=bulk-count id=bulkCount></span>
  </div>
  <span class=spacer></span>
  <button class=primary onclick="bulkAct('restore')">Restore</button>
  <button class=warm onclick="bulkAct('hibernate')">Hibernate</button>
  <button onclick="bulkAct('clean')">Clean</button>
  <button class=danger onclick="bulkAct('delete')">Delete</button>
</div>
<div class=search-wrap>
  <input id=search-box type=text placeholder="Search tabs…" autocomplete=off>
  <button id=search-clear title="Clear search" onclick=clearSearch()>&times;</button>
</div>
<div id=disconnected>Disconnected — waiting for backend...</div>
</div>
<div id=list class=list></div>
<div id=toast></div>
<!-- right-click context menu -->
<div id=ctx-menu>
  <div class=ctx-item id=ctx-restore  onclick=ctxAct('restore')>Restore</div>
  <div class=ctx-item id=ctx-hibernate onclick=ctxAct('hibernate')>Hibernate</div>
  <div class=ctx-sep></div>
  <div class=ctx-item onclick=ctxAct('clone')>Clone</div>
  <div class=ctx-item onclick=ctxAct('clean')>Clean</div>
  <div class=ctx-sep></div>
  <div class="ctx-item danger" onclick=ctxAct('delete')>Delete</div>
</div>

<script>
// ── state ────────────────────────────────────────────────────────────────────
const selected = new Set();
let _allIds  = [];
let _lastData = null;
let _lastJson = '';
let _disconnected = false;
// browseFocusIdx: for arrow-key navigation in the normal (non-search) list
let _browseFocusIdx = -1;
let _browseItems = [];   // flat [{c, t}] built from last renderList
// cut/paste state
let _cutTab = null;  // {cid, url, targetId, isHot}

// ── icons ────────────────────────────────────────────────────────────────────
const _svgPause = '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M5 2h2v12H5zM9 2h2v12H9z"/></svg>';
const _svgPlay  = '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M4 2l10 6-10 6z"/></svg>';
const _svgTrash = '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M6 1a.5.5 0 00-.5.5V2H3v1h10V2h-2.5v-.5A.5.5 0 0010 1H6zM4.5 5l.5 8.5a1 1 0 001 .5h4a1 1 0 001-.5L11.5 5h-7z"/></svg>';
const _svgCut   = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/></svg>';
const _svgPaste = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/></svg>';

// ── helpers ──────────────────────────────────────────────────────────────────
function toast(msg, ms=2500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._tid);
  toast._tid = setTimeout(() => t.classList.remove('show'), ms);
}
async function api(path, method='GET', body=null) {
  const opts = {method, headers:{'content-type':'application/json'}};
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function trimUrl(u){try{const p=new URL(u);return p.hostname+p.pathname.replace(/\/$/,'');}catch(e){return u;}}

// ── disconnected banner ───────────────────────────────────────────────────────
function setDisconnected(v) {
  _disconnected = v;
  document.getElementById('disconnected').classList.toggle('visible', v);
}

// ── bulk-bar ──────────────────────────────────────────────────────────────────
function updateBulkBar() {
  const cnt = document.getElementById('bulkCount');
  const cb  = document.getElementById('selAll');
  if (selected.size > 0) {
    cnt.textContent = selected.size + ' selected';
    cb.checked = selected.size === _allIds.length;
    cb.indeterminate = selected.size > 0 && selected.size < _allIds.length;
  } else {
    cnt.textContent = '';
    cb.checked = false;
    cb.indeterminate = false;
  }
}
function toggleSelectAll(on) {
  if (on) _allIds.forEach(id => selected.add(id)); else selected.clear();
  document.querySelectorAll('.row').forEach(r =>
    r.classList.toggle('selected', selected.has(r.dataset.id)));
  updateBulkBar();
}
function toggleSelect(id, el) {
  if (selected.has(id)) selected.delete(id); else selected.add(id);
  const row = el.closest('.row');
  if (row) row.classList.toggle('selected', selected.has(id));
  updateBulkBar();
}
async function bulkAct(action) {
  if (!selected.size) return;
  const label = {restore:'Restoring',hibernate:'Hibernating',clean:'Cleaning',delete:'Deleting'}[action]||action;
  const doneLabel = {restore:'Restored',hibernate:'Hibernated',clean:'Cleaned',delete:'Deleted'}[action]||action;
  const ids = [...selected]; let count = 0;
  toast(`${label} ${ids.length} session(s)…`);
  const bulkActions = {hibernate:1, clean:1, delete:1};
  if (bulkActions[action]) {
    try {
      const res = await api(`/api/bulk-${action}`, 'POST', {ids});
      count = (res.results||[]).filter(r => !r.error).length;
    } catch(e) {}
  } else {
    for (const id of ids) {
      try {
        await api(`/api/containers/${id}/${action}`, 'POST');
        count++;
      } catch(e) {}
    }
  }
  selected.clear(); updateBulkBar();
  toast(`${doneLabel} ${count} session(s)`);
  _lastJson = ''; refresh();
}

// ── context menu ──────────────────────────────────────────────────────────────
let _ctxId = null;
function showCtxMenu(e, cid, isHot) {
  e.preventDefault();
  e.stopPropagation();
  _ctxId = cid;
  const m = document.getElementById('ctx-menu');
  document.getElementById('ctx-restore').style.display   = isHot ? 'none' : '';
  document.getElementById('ctx-hibernate').style.display = isHot ? '' : 'none';
  // position near cursor, keep on screen
  const pad = 4;
  let x = e.clientX, y = e.clientY;
  m.style.display = 'block';
  const mw = m.offsetWidth, mh = m.offsetHeight;
  m.style.display = '';
  if (x + mw + pad > window.innerWidth)  x = window.innerWidth  - mw - pad;
  if (y + mh + pad > window.innerHeight) y = window.innerHeight - mh - pad;
  m.style.left = x + 'px';
  m.style.top  = y + 'px';
  m.classList.add('open');
  refocusSearch();
}
function closeCtxMenu() {
  document.getElementById('ctx-menu').classList.remove('open');
}
async function ctxAct(action) {
  closeCtxMenu();
  if (!_ctxId) return;
  const id = _ctxId; _ctxId = null;
  const startLabel = {restore:'Restoring…',hibernate:'Hibernating…',clone:'Cloning…',clean:'Cleaning…',delete:'Deleting…'}[action];
  const doneLabel  = {restore:'Restored',hibernate:'Hibernated',clone:'Cloned',clean:'Cleaned',delete:'Deleted'}[action];
  if (action === 'clone') {
    const n = prompt('Clone name?'); if (!n) return;
    toast(startLabel);
    await api(`/api/containers/${id}/clone`, 'POST', {name: n});
    toast(doneLabel);
  } else if (action === 'delete') {
    toast(startLabel);
    await api(`/api/containers/${id}`, 'DELETE');
    selected.delete(id); updateBulkBar(); toast(doneLabel);
  } else if (action === 'clean') {
    toast(startLabel);
    await api(`/api/containers/${id}/clean`, 'POST'); toast(doneLabel);
  } else {
    toast(startLabel);
    await api(`/api/containers/${id}/${action}`, 'POST');
    toast(doneLabel);
  }
  _lastJson = ''; refresh();
  refocusSearch();
}
document.addEventListener('click', e => {
  if (!e.target.closest('#ctx-menu')) closeCtxMenu();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeCtxMenu(); if (_cutTab) { cancelCut(); return; } clearSearch(); }
});

// ── render session list ───────────────────────────────────────────────────────
function rowActionsHtml(c) {
  const isHot = c.hot;
  return `<span class=row-actions>`
    + `<span class="row-action" title="${isHot?'Hibernate':'Restore'}" `
    + `onclick="event.stopPropagation();rowAction('${isHot?'hibernate':'restore'}','${esc(c.id)}')">`
    + `${isHot?_svgPause:_svgPlay}</span>`
    + `<span class=action-sep></span>`
    + `<span class="row-action danger" title="Delete" `
    + `onclick="event.stopPropagation();rowAction('delete','${esc(c.id)}')">`
    + `${_svgTrash}</span></span>`;
}
function makeTabRow(c, t, isFirst) {
  const isHot = c.hot;
  const isBlank = !t.url || t.url === 'about:blank';
  const displayTitle = isBlank ? 'New Tab' : esc(t.title || trimUrl(t.url));
  const displayUrl   = isBlank ? '' : esc(trimUrl(t.url));
  const clickAction = `if(_cutTab&&_cutTab.cid!=='${esc(c.id)}'){pasteTab('${esc(c.id)}');return;}`
    + (isHot
      ? `activate('${esc(t.targetId)}');refocusSearch()`
      : `restoreAndOpen('${esc(c.id)}','${esc(t.url)}');refocusSearch()`);
  const closeAction = isHot
    ? `closeTab('${esc(t.targetId)}')`
    : `deleteSavedTab('${esc(c.id)}','${esc(t.url)}')`;
  const cutAction = `cutTab('${esc(c.id)}','${esc(t.url)}','${esc(t.targetId||'')}',${isHot})`;
  const isCut = _cutTab && _cutTab.cid === c.id && _cutTab.url === t.url;
  const hibernateBtn = isFirst
    ? `<span class="row-action" title="${isHot?'Hibernate':'Restore'}" `
      + `onclick="event.stopPropagation();rowAction('${isHot?'hibernate':'restore'}','${esc(c.id)}')">`
      + `${isHot?_svgPause:_svgPlay}</span>`
    : '';
  const deleteBtn = isFirst
    ? `<span class="row-action danger" title="Delete" `
      + `onclick="event.stopPropagation();rowAction('delete','${esc(c.id)}')">`
      + `${_svgTrash}</span>`
    : '';
  return `<div class="tab${isCut?' cut-active':''}"
               onclick="event.stopPropagation();${clickAction}">
    <div class=tab-body>
      <span class=tab-title${isBlank?' style="color:#94a3b8"':''}>${displayTitle}</span>
      <span class=tab-url>${displayUrl}</span>
    </div>
    ${hibernateBtn}
    <span class=tab-cut title="Cut — move to another session" onclick="event.stopPropagation();${cutAction}">${_svgCut}</span>
    <span class=action-sep></span>
    ${deleteBtn}
    <span class=tab-close onclick="event.stopPropagation();${closeAction}">&times;</span>
  </div>`;
}

function _buildRow(c) {
  const el = document.createElement('div');
  el.className = 'row ' + (c.hot ? 'hot' : 'cold') + (selected.has(c.id) ? ' selected' : '');
  el.dataset.id = c.id;
  el.dataset.hot = c.hot ? '1' : '';
  el.addEventListener('contextmenu', e => showCtxMenu(e, c.id, c.hot));
  const isPasteTarget = _cutTab && _cutTab.cid !== c.id;
  if (isPasteTarget) el.classList.add('paste-target');
  const tabs = c.hot ? (c.live_tabs||[]) : (c.saved_tabs||[]);
  let tabsHtml = '';
  const pasteHtml = isPasteTarget
    ? `<span class=paste-btn title="Paste tab here" onclick="event.stopPropagation();pasteTab('${esc(c.id)}')">${_svgPaste}</span>`
    : '';
  if (tabs.length) {
    tabs.forEach((t, ti) => { tabsHtml += makeTabRow(c, t, ti===0); });
  } else {
    tabsHtml = `<div class=row-empty><span style="color:#475569">${c.hot?'no tabs':'hibernated'}</span>${pasteHtml}${rowActionsHtml(c)}</div>`;
  }
  const cbChecked = selected.has(c.id) ? ' checked' : '';
  el.innerHTML = `<div style="display:flex"><div class=row-cb><input type=checkbox${cbChecked} onchange="toggleSelect('${esc(c.id)}',this)"></div><div class=tabs style="flex:1">${tabsHtml}</div></div>`;
  if (isPasteTarget) el.addEventListener('click', e => {
    if (!e.target.closest('.tab') && !e.target.closest('input')) pasteTab(c.id);
  });
  return el;
}

function renderList(data) {
  _lastData = data;
  const q = document.getElementById('search-box').value.trim();
  if (q) { renderSearch(q, data); return; }
  const g = document.getElementById('list');
  g.innerHTML = '';
  _allIds = data.containers.map(c => c.id);
  _browseItems = [];
  const hot = data.containers.filter(c => c.hot);
  const cold = data.containers.filter(c => !c.hot);
  [...hot, ...cold].forEach(c => {
    const tabs = c.hot ? (c.live_tabs||[]) : (c.saved_tabs||[]);
    tabs.forEach(t => _browseItems.push({c, t}));
  });
  const hotCol = document.createElement('div');
  hotCol.className = 'col col-hot';
  hotCol.innerHTML = '<div class="col-header">Active</div>';
  if (!hot.length) hotCol.insertAdjacentHTML('beforeend', '<div class="col-empty">No active sessions</div>');
  hot.forEach(c => hotCol.appendChild(_buildRow(c)));
  const coldCol = document.createElement('div');
  coldCol.className = 'col col-cold';
  coldCol.innerHTML = '<div class="col-header">Hibernated</div>';
  if (!cold.length) coldCol.insertAdjacentHTML('beforeend', '<div class="col-empty">No hibernated sessions</div>');
  cold.forEach(c => coldCol.appendChild(_buildRow(c)));
  g.appendChild(hotCol);
  g.appendChild(coldCol);
  _browseFocusIdx = -1;
  _highlightBrowse();
  updateBulkBar();
}

// ── browse focus (arrow keys when search is blank) ───────────────────────────
function _highlightBrowse() {
  const tabs = document.querySelectorAll('#list .tab');
  tabs.forEach((el, i) => el.classList.toggle('focused', i === _browseFocusIdx));
}
function _moveBrowseFocus(delta) {
  if (!_browseItems.length) return;
  _browseFocusIdx = (_browseFocusIdx + delta + _browseItems.length) % _browseItems.length;
  _highlightBrowse();
  const tabs = document.querySelectorAll('#list .tab');
  if (tabs[_browseFocusIdx]) tabs[_browseFocusIdx].scrollIntoView({block:'nearest'});
}
function _activateBrowseItem(i) {
  if (i < 0 || i >= _browseItems.length) return;
  const {c, t} = _browseItems[i];
  if (c.hot) activate(t.targetId);
  else restoreAndOpen(c.id, t.url);
  refocusSearch();
}

// ── search ────────────────────────────────────────────────────────────────────
let _searchMatches = [];   // [{c, t}] — flat list of current matches
let _searchFocusIdx = -1;

function _buildSearchRow(c, t, gi, focused) {
  const el = document.createElement('div');
  el.className = 'search-row' + (focused ? ' focused' : '') + (c.hot ? ' search-hot' : ' search-cold');
  el.dataset.matchIdx = gi;
  const label = esc(t.title || trimUrl(t.url));
  const urlLabel = esc(trimUrl(t.url));
  el.innerHTML = `<div class=tab-body onclick="_activateSearchMatch(${gi})" style="flex:1;overflow:hidden"><span class=tab-title>${label}</span><span class=tab-url>${urlLabel}</span></div>`;
  return el;
}

function renderSearch(q, data) {
  const g = document.getElementById('list');
  g.innerHTML = '';
  _searchMatches = [];
  _browseItems = [];
  const lq = q.toLowerCase();
  const hotM = [], coldM = [];
  (data.containers||[]).forEach((c, idx) => {
    const tabs = c.hot ? (c.live_tabs||[]) : (c.saved_tabs||[]);
    tabs.forEach(t => {
      if (!(t.title||'').toLowerCase().includes(lq) &&
          !(t.url||'').toLowerCase().includes(lq)) return;
      (c.hot ? hotM : coldM).push({c, t, idx});
    });
  });
  _searchMatches = [...hotM, ...coldM];
  if (!_searchMatches.length) {
    g.innerHTML = '<div style="color:#475569;padding:8px 10px;font-size:13px;grid-column:1/-1">No matching tabs</div>';
    _searchFocusIdx = -1;
    return;
  }
  _searchFocusIdx = _searchMatches.length === 1 ? 0 : -1;
  const hotCol = document.createElement('div');
  hotCol.className = 'col col-hot';
  hotCol.innerHTML = '<div class="col-header">Active</div>';
  const coldCol = document.createElement('div');
  coldCol.className = 'col col-cold';
  coldCol.innerHTML = '<div class="col-header">Hibernated</div>';
  hotM.forEach(({c, t}, i) => hotCol.appendChild(_buildSearchRow(c, t, i, i === _searchFocusIdx)));
  coldM.forEach(({c, t}, i) => {
    const gi = hotM.length + i;
    coldCol.appendChild(_buildSearchRow(c, t, gi, gi === _searchFocusIdx));
  });
  if (!hotM.length) hotCol.insertAdjacentHTML('beforeend', '<div class="col-empty">No matches</div>');
  if (!coldM.length) coldCol.insertAdjacentHTML('beforeend', '<div class="col-empty">No matches</div>');
  g.appendChild(hotCol);
  g.appendChild(coldCol);
}

function _activateSearchMatch(i) {
  if (i < 0 || i >= _searchMatches.length) return;
  const {c, t} = _searchMatches[i];
  if (c.hot) activate(t.targetId);
  else restoreAndOpen(c.id, t.url);
  clearSearch();
}

function _moveFocus(delta) {
  if (!_searchMatches.length) return;
  _searchFocusIdx = (_searchFocusIdx + delta + _searchMatches.length) % _searchMatches.length;
  const rows = document.querySelectorAll('.search-row');
  rows.forEach((el, i) => el.classList.toggle('focused', i === _searchFocusIdx));
  if (rows[_searchFocusIdx]) rows[_searchFocusIdx].scrollIntoView({block:'nearest'});
}

function clearSearch() {
  const sb = document.getElementById('search-box');
  sb.value = '';
  document.getElementById('search-clear').style.display = 'none';
  _searchMatches = []; _searchFocusIdx = -1;
  _lastJson = '';
  if (_lastData) renderList(_lastData);
  refocusSearch();
}

function refocusSearch() {
  setTimeout(() => document.getElementById('search-box').focus(), 0);
}

function onSearchInput(val) {
  document.getElementById('search-clear').style.display = val ? 'inline' : 'none';
  _browseFocusIdx = -1;
  _lastJson = '';
  if (_lastData) renderList(_lastData);
}

// search keyboard handling
document.getElementById('search-box').addEventListener('keydown', e => {
  const hasQuery = document.getElementById('search-box').value.trim();
  if (e.key === 'Enter') {
    if (hasQuery) {
      if (_searchMatches.length === 1) { _activateSearchMatch(0); return; }
      if (_searchFocusIdx >= 0)        { _activateSearchMatch(_searchFocusIdx); return; }
    } else {
      if (_browseFocusIdx >= 0)        { _activateBrowseItem(_browseFocusIdx); return; }
    }
  }
  if (e.key === 'ArrowDown') { e.preventDefault(); hasQuery ? _moveFocus(1) : _moveBrowseFocus(1); }
  if (e.key === 'ArrowUp')   { e.preventDefault(); hasQuery ? _moveFocus(-1) : _moveBrowseFocus(-1); }
  if (e.key === 'Escape')    { clearSearch(); }
});
document.getElementById('search-box').addEventListener('input', e => onSearchInput(e.target.value));

// keep search focused unless user clicks a button/input elsewhere
document.addEventListener('click', e => {
  const sb = document.getElementById('search-box');
  if (!e.target.closest('button') && !e.target.closest('input') &&
      !e.target.closest('#ctx-menu') && !e.target.closest('.tab-body') &&
      !e.target.closest('.row-action')) {
    sb.focus();
  }
});

// ── cut / paste (move tab between sessions) ───────────────────────────────
function cutTab(cid, url, targetId, isHot) {
  if (_cutTab && _cutTab.cid === cid && _cutTab.url === url) {
    cancelCut(); return;
  }
  _cutTab = {cid, url, targetId: targetId||'', isHot};
  toast('Tab cut — click paste on another session');
  _lastJson = ''; if (_lastData) renderList(_lastData);
  refocusSearch();
}
function cancelCut() {
  _cutTab = null;
  _lastJson = ''; if (_lastData) renderList(_lastData);
  refocusSearch();
}
async function pasteTab(destCid) {
  if (!_cutTab) return;
  const {cid: src, url, targetId} = _cutTab;
  _cutTab = null;
  toast('Moving tab…');
  try {
    const res = await api('/api/move-tab', 'POST',
      {src, dest: destCid, url, targetId});
    if (res.error) { toast('Move failed: ' + res.error); }
    else { toast('Tab moved'); }
  } catch(e) { toast('Move failed'); }
  _lastJson = ''; refresh();
  refocusSearch();
}

// ── row-level actions (hover buttons) ─────────────────────────────────────
async function rowAction(action, cid) {
  if (action === 'delete' && !confirm('Delete this session?')) return;
  refocusSearch();
  const labels = {restore:'Restoring…',hibernate:'Hibernating…',delete:'Deleting…'};
  const done   = {restore:'Restored',hibernate:'Hibernated',delete:'Deleted'};
  toast(labels[action] || action);
  try {
    if (action === 'delete') await api(`/api/containers/${cid}`, 'DELETE');
    else await api(`/api/containers/${cid}/${action}`, 'POST');
    selected.delete(cid); updateBulkBar();
    toast(done[action] || 'Done');
  } catch(e) { toast('Action failed'); }
  _lastJson = ''; refresh();
  refocusSearch();
}

// ── API actions ───────────────────────────────────────────────────────────────
async function trimLog() {
  toast('Trimming log…');
  try {
    const r = await api('/api/trim-log', 'POST');
    if (r.trimmed) toast(`Log trimmed — kept last 500 lines`);
    else toast('Trim skipped: ' + (r.reason || 'unknown'));
  } catch(e) { toast('Trim failed'); }
}
async function createSession() {
  toast('Creating session…');
  await api('/api/containers', 'POST', {name: 'session'});
  toast('Session created');
  _lastJson = ''; refresh();
}
async function activate(targetId) {
  try { await api('/api/activate', 'POST', {targetId}); } catch(e) {}
}
async function restoreAndOpen(cid, url) {
  toast('Restoring…');
  try {
    const data = await api(`/api/containers/${cid}/open`, 'POST', {url});
    toast('Restored');
    if (data && data.targetId) { try { await activate(data.targetId); } catch(e) {} }
  }
  catch(e) { toast('Restore failed'); }
  _lastJson = ''; refresh();
}
async function closeTab(targetId) {
  toast('Closing tab…');
  try { await api('/api/close-tab', 'POST', {targetId}); toast('Tab closed'); } catch(e) {}
  _lastJson = ''; refresh();
}
async function deleteSavedTab(cid, url) {
  try { await api(`/api/containers/${cid}/tab`, 'DELETE', {url}); toast('Tab removed'); } catch(e) {}
  _lastJson = ''; refresh();
}
async function cleanDefault() {
  toast('Cleaning…');
  await api('/api/clean-default', 'POST');
  toast('Default session cleaned');
}
async function restartBackend() {
  toast('Restarting backend…');
  try { await api('/api/restart', 'POST'); } catch(e) {}
  // Wait for new process to come back up (up to 30s).
  // The existing polling loop handles reconnect display; we just wait here.
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      await api('/api/containers');
      toast('Backend restarted');
      _lastJson = ''; refresh();
      return;
    } catch(e) {}
  }
  toast('Restart may have failed — try refreshing manually');
}
async function quitDaemon() {
  if (!confirm('Hibernate all sessions and shut down?')) return;
  toast('Shutting down…');
  try { await api('/api/shutdown', 'POST'); } catch(e) {}
  try { window.close(); } catch(e) {}
  document.body.innerHTML = '<h1>Sessions stopped.</h1>';
}
refresh();
refocusSearch();
// Debounced refresh: coalesces rapid action-triggered calls into one request.
// The timer-based poller also goes through this so there is at most one
// in-flight /api/containers request at a time.
let _lastPoll = 0;
let _refreshPending = false;
let _refreshInFlight = false;
const _POLL_CONNECTED = 3000;
const _POLL_DISCONNECTED = 1000;
const _DEBOUNCE_MS = 200;
async function refresh() {
  if (_refreshPending) return;   // already scheduled
  if (_refreshInFlight) { _refreshPending = true; return; }
  _refreshPending = false;
  _refreshInFlight = true;
  _lastPoll = Date.now();
  try {
    const data = await api('/api/containers');
    if (_disconnected) { setDisconnected(false); toast('Reconnected'); }
    const btn = document.getElementById('trim-log-btn');
    if (btn) btn.style.display = data.debug_mode ? '' : 'none';
    const j = JSON.stringify(data);
    if (j !== _lastJson) { _lastJson = j; renderList(data); }
  } catch(e) {
    if (!_disconnected) { setDisconnected(true); }
  } finally {
    _refreshInFlight = false;
    if (_refreshPending) { _refreshPending = false; setTimeout(refresh, _DEBOUNCE_MS); }
  }
}
setInterval(() => {
  const now = Date.now();
  const interval = _disconnected ? _POLL_DISCONNECTED : _POLL_CONNECTED;
  if (now - _lastPoll >= interval) refresh();
}, 500);
</script>
</body>
</html>
"""
