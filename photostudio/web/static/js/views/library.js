import { h, clear, debounce, isTyping, sleep } from '../util.js';
import * as api from '../api.js';
import { bus, state, setItems, pref, LABELS } from '../store.js';
import * as M from '../meta.js';
import { toast, toastError, busy } from '../toast.js';
import * as info from './libinfo.js';
import { stars } from './libinfo.js';

let el = {}, navigate = null, openDevelop = null, folders = [], albums = [], tagsAll = [], sideInfo = null;
let cellEls = new Map(), lastAnchor = null, loadSeq = 0, building = false;

export function init(root, { navigate: nav, open }) {
  navigate = nav; openDevelop = open;
  el.side = h('div.lib-side');
  el.filter = h('div.filterbar');
  el.gridwrap = h('div.gridwrap', { tabindex: '0' });
  el.status = h('div.statusbar');
  el.center = h('div.lib-center', {}, el.filter, el.gridwrap, el.status);
  sideInfo = info.build({ open: (p) => openDevelop(p) });
  el.right = h('div.lib-right', {}, sideInfo.el);
  root.append(el.side, el.center, el.right);
  buildFilterBar(); buildStatus();
  el.gridwrap.addEventListener('click', (e) => { if (e.target === el.gridwrap || e.target.classList.contains('grid')) { state.selection.clear(); paintSel(); bus.emit('lib:selection'); } });
  bus.on('lib:items', (paths) => { for (const p of paths || []) refreshCell(p); });
  bus.on('lib:item', (p) => refreshCell(p, true));
  bus.on('lib:tagschanged', () => loadTags());
  bus.on('ext:changed', (p) => { refreshCell(p, true); });
  bus.on('lib:reload', () => reload());
  state.cell = pref.get('cell', 180);
  el.gridwrap.style.setProperty('--cell', `${state.cell}px`);
}

export async function activated() {
  if (!folders.length) await loadSide(); else { await loadSide(); }
  await reload();
  el.gridwrap.focus({ preventScroll: true });
}

// ------------------------------------------------------------------ data
async function loadSide() {
  try {
    const [f, a, t] = await Promise.all([api.folders(), api.albums().catch(() => ({ albums: [], available: false })), api.tags().catch(() => ({ tags: [] }))]);
    folders = f.folders; albums = a.albums; tagsAll = t.tags; state.albumsAvailable = a.available;
    bus.emit('lib:tags', tagsAll);
    renderSide();
  } catch (e) { toastError(e); }
}
async function loadTags() { try { tagsAll = (await api.tags()).tags; bus.emit('lib:tags', tagsAll); renderSide(); } catch (_) { /* ignore */ } }

function query() {
  const f = state.filters;
  return { folder: f.album ? '' : f.folder, recursive: f.recursive, album: f.album, rating_min: f.rating_min || '', flag: f.flag, label: f.label, tag: f.tag, q: f.q, edited: f.edited ? true : '', sort: f.sort, order: f.order, issue: f.issue, limit: 6000 };
}
export async function reload() {
  const my = ++loadSeq;
  try {
    const r = await busy(api.listImages(query()));
    if (my !== loadSeq) return;
    setItems(r.items); state.total = r.total; state.scanning = r.scanning;
    for (const p of Array.from(state.selection)) if (!state.itemsByPath.has(p)) state.selection.delete(p);
    if (state.cursor && !state.itemsByPath.has(state.cursor)) state.cursor = null;
    renderGrid(); renderStatus();
    bus.emit('lib:listed'); bus.emit('lib:selection');
    if (r.scanning) { await sleep(1500); if (state.view === 'library') reload(); }
  } catch (e) { toastError(e); }
}

// ------------------------------------------------------------------ side panel
function renderSide() {
  const f = state.filters;
  const node = (label, count, active, onclick, depth = 0, extra) => h('div.node', { class: active ? 'active' : '', style: { paddingLeft: `${8 + depth * 12}px` }, onclick }, h('span.ellipsis', { text: label, title: label }), h('span.cnt', { text: count == null ? '' : String(count) }), extra);
  const fold = folders.map((d) => node(d.depth === 0 ? `All photographs (${d.name})` : d.name, d.count, !f.album && f.folder === d.path && !f.tag, () => { f.album = ''; f.folder = d.path; f.tag = ''; f.recursive = pref.get('recursive', true); reload(); renderSide(); }, d.depth));
  const albumNodes = albums.map((a) => {
    const n = node(`${a.kind === 'smart' ? '✨ ' : ''}${a.name}`, a.count, f.album === `id:${a.id}`, () => { f.album = `id:${a.id}`; f.tag = ''; reload(); renderSide(); }, 0,
      h('span', { style: { display: 'none' } }));
    n.draggable = false;
    n.addEventListener('dragover', (e) => { if (a.kind === 'static') { e.preventDefault(); n.classList.add('droptarget'); } });
    n.addEventListener('dragleave', () => n.classList.remove('droptarget'));
    n.addEventListener('drop', async (e) => {
      e.preventDefault(); n.classList.remove('droptarget');
      let paths = []; try { paths = JSON.parse(e.dataTransfer.getData('application/x-ps-paths') || '[]'); } catch (_) { /* ignore */ }
      if (!paths.length) return;
      try { const r = await api.albumImages(a.id, paths, 'add'); toast(`Added ${r.changed ?? paths.length} photo(s) to "${a.name}"`, 'good', 2500); loadSide(); } catch (er) { toastError(er); }
    });
    n.addEventListener('contextmenu', (e) => { e.preventDefault(); const act = prompt(`Album "${a.name}": type "rename" or "delete"`, ''); if (act === 'delete' && confirm(`Delete album "${a.name}"? Photos are not deleted.`)) api.albumDelete(a.id).then(loadSide).catch(toastError); else if (act === 'rename') { const nn = prompt('New name', a.name); if (nn) api.albumRename(a.id, nn).then(loadSide).catch(toastError); } });
    return n;
  });
  const tagNodes = tagsAll.map((t) => node(t.name, t.total, f.tag === t.path, () => { f.tag = f.tag === t.path ? '' : t.path; f.album = ''; reload(); renderSide(); }, t.path.split('|').length - 1));
  const rec = h('input', { type: 'checkbox', checked: pref.get('recursive', true) });
  rec.addEventListener('change', () => { pref.set('recursive', rec.checked); f.recursive = rec.checked; reload(); });
  clear(el.side).append(
    h('div.pane-h', {}, h('span', { text: 'Folders' }), h('label', { title: 'Include photos from subfolders' }, rec, ' sub')),
    h('div.tree', {}, fold),
    h('div.pane-h', {}, h('span', { text: 'Albums' }), h('span.acts', {}, state.albumsAvailable === false ? null : [
      h('button.small', { text: '+', title: 'New album', onclick: async () => { const n = prompt('Album name'); if (!n) return; try { await api.albumCreate(n); loadSide(); } catch (e) { toastError(e); } } }),
      h('button.small', { text: 'smart', title: 'New smart album from a rule such as rating>=4', onclick: async () => { const n = prompt('Smart album name'); if (!n) return; const r = prompt('Rule (e.g. rating>=4, flag=pick, label=red, iso>=3200, tag=kids)', 'rating>=4'); if (!r) return; try { await api.albumCreate(n, 'smart', r); loadSide(); } catch (e) { toastError(e); } } })])),
    h('div.tree', {}, state.albumsAvailable === false ? h('div.subtle', { style: { padding: '4px 8px' }, text: 'Albums need the catalog (not available with --no-catalog).' }) : (albumNodes.length ? albumNodes : h('div.subtle', { style: { padding: '4px 8px' }, text: 'Drag photos onto an album. Right-click renames or deletes.' }))),
    h('div.pane-h', {}, h('span', { text: 'Tags' })),
    h('div.tree.tagtree', {}, tagNodes.length ? tagNodes : h('div.subtle', { style: { padding: '4px 8px' }, text: 'No tags yet. Add them in the right panel.' })));
}

// ------------------------------------------------------------------ filter bar
function buildFilterBar() {
  const f = state.filters;
  const starBox = h('span.stars');
  const paintStars = () => starBox.replaceChildren(...[1, 2, 3, 4, 5].map((i) => h('span', { class: i <= f.rating_min ? 'on' : '', text: '★', title: `${i}+ stars`, onclick: () => { f.rating_min = f.rating_min === i ? 0 : i; paintStars(); reload(); } })));
  paintStars();
  const chips = (key, opts) => h('span.grp', {}, opts.map(([v, t]) => h('span.chip', { class: f[key] === v ? 'on' : '', text: t, dataset: { v }, onclick: (e) => { f[key] = f[key] === v ? '' : v; $$chips(e.target.parentNode, f[key]); reload(); } })));
  const $$chips = (grp, val) => grp.querySelectorAll('.chip').forEach((c) => c.classList.toggle('on', c.dataset.v === val));
  const labels = h('span.grp', {}, LABELS.map((l) => h('span.dot', { class: l, title: `Label ${l}`, style: { cursor: 'pointer', outline: f.label === l ? '2px solid #fff' : '' }, onclick: (e) => { f.label = f.label === l ? '' : l; labels.querySelectorAll('.dot').forEach((d, i) => { d.style.outline = f.label === LABELS[i] ? '2px solid #fff' : ''; }); reload(); } })));
  const q = h('input', { type: 'search', placeholder: 'Search name, tag, note...', style: { width: '170px' } });
  q.addEventListener('input', debounce(() => { f.q = q.value.trim(); reload(); }, 250));
  q.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Escape') { q.value = ''; f.q = ''; reload(); q.blur(); } });
  const edited = h('label', {}, h('input', { type: 'checkbox' }), ' Edited');
  edited.querySelector('input').addEventListener('change', (e) => { f.edited = e.target.checked; reload(); });
  const sort = h('select', {}, [['taken_at', 'Capture time'], ['name', 'File name'], ['rating', 'Rating'], ['file_mtime', 'Modified']].map(([v, t]) => h('option', { value: v, text: t, selected: f.sort === v })));
  sort.addEventListener('change', () => { f.sort = sort.value; reload(); });
  const order = h('button.small', { text: f.order === 'asc' ? '↑' : '↓', title: 'Sort direction', onclick: () => { f.order = f.order === 'asc' ? 'desc' : 'asc'; order.textContent = f.order === 'asc' ? '↑' : '↓'; reload(); } });
  const modes = h('div.seg', {}, [['grid', 'Grid'], ['compare', 'Compare'], ['survey', 'Survey'], ['cull', 'Cull']].map(([m, t]) => h('button', { text: t, class: state.libMode === m ? 'active' : '', dataset: { m }, title: { grid: 'Grid (G)', compare: 'Compare two (C)', survey: 'Survey selected (N)', cull: 'Cull view with best-of-burst (B)' }[m], onclick: () => setMode(m) })));
  el.modes = modes;
  const cullBtn = h('button', { text: 'Run cull', title: 'Score sharpness / exposure / faces and group bursts for the listed photos', onclick: runCull });
  const refresh = h('button', { text: 'Rescan', title: 'Look for new files', onclick: async () => { try { await api.rescan(false); toast('Scanning...', 'info', 1500); setTimeout(() => { loadSide(); reload(); }, 800); } catch (e) { toastError(e); } } });
  el.filter.append(starBox, chips('flag', [['pick', 'Picked'], ['reject', 'Rejected'], ['none', 'Unflagged']]), labels, q, edited, sort, order, h('span.spacer', { style: { flex: 1 } }), modes, cullBtn, refresh);
}
export function setMode(m) {
  state.libMode = m;
  el.modes.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.m === m));
  renderGrid(); renderStatus();
}

function buildStatus() {
  const size = h('input', { type: 'range', min: 90, max: 360, step: 10, value: state.cell, title: 'Thumbnail size' });
  size.addEventListener('input', () => { state.cell = Number(size.value); pref.set('cell', state.cell); el.gridwrap.style.setProperty('--cell', `${state.cell}px`); });
  el.sizeBox = h('span', {}, 'Size ', size);
  el.count = h('span');
  el.status.append(el.count, h('span.spacer', { style: { flex: 1 } }), el.sizeBox);
}
function renderStatus() {
  const n = state.items.length, sel = state.selection.size;
  el.count.textContent = `${n} photo${n === 1 ? '' : 's'}${state.total > n ? ` of ${state.total}` : ''}${sel ? `, ${sel} selected` : ''}${state.scanning ? '  (scanning...)' : ''}`;
}

// ------------------------------------------------------------------ grid
function renderGrid() {
  cellEls = new Map();
  el.gridwrap.style.padding = '';
  const mode = state.libMode;
  el.status.classList.toggle('hidden', false);
  if (mode === 'compare' || mode === 'survey') return renderCompare(mode);
  if (!state.items.length) { clear(el.gridwrap).append(h('div.empty', { text: state.scanning ? 'Scanning the library...' : 'No photos match. Clear a filter or pick another folder.' })); return; }
  const grid = h('div.grid');
  if (mode === 'cull' && state.cull.size) {
    const groups = new Map();
    for (const it of state.items) { const c = state.cull.get(it.p); const g = c && c.group != null ? c.group : `solo:${it.p}`; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(it); }
    for (const [g, its] of groups) {
      if (its.length > 1) {
        its.sort((a, b) => ((state.cull.get(a.p) || {}).group_rank || 9) - ((state.cull.get(b.p) || {}).group_rank || 9));
        grid.append(h('div.burst', {}, h('div.bh', { text: `Burst ${its.length}x` }), h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } }, its.map(cell))));
      } else grid.append(cell(its[0]));
    }
  } else grid.append(...state.items.map(cell));
  clear(el.gridwrap).append(grid);
  if (mode === 'cull' && !state.cull.size) grid.before(h('div.notice', { text: 'Run cull to see suggested picks / rejects and best-of-burst. Nothing is changed until you apply the suggestions.' }));
  if (mode === 'cull' && state.cull.size) grid.before(cullBar());
}
function cullBar() {
  const picks = Array.from(state.cull.values()).filter((c) => c.flag === 'pick').length, rej = Array.from(state.cull.values()).filter((c) => c.flag === 'reject').length;
  return h('div.row2', { style: { marginBottom: '8px' } }, h('span', { text: `Suggested: ${picks} pick, ${rej} reject.` }),
    h('button', { text: 'Apply suggestions', title: 'Flag the listed photos as suggested', onclick: async () => {
      const toPick = [], toRej = [];
      for (const [p, c] of state.cull) { if (!state.itemsByPath.has(p)) continue; if (c.flag === 'pick') toPick.push(p); else if (c.flag === 'reject') toRej.push(p); }
      await M.flag(toPick, 'pick'); await M.flag(toRej, 'reject'); toast('Flags written to the sidecars', 'good');
    } }),
    h('button', { text: 'Keep best of each burst', title: 'Pick the best of every burst and reject the rest', onclick: async () => {
      const best = [], rest = [];
      for (const [p, c] of state.cull) { if (!state.itemsByPath.has(p) || (c.group_size || 1) < 2) continue; (c.best_of_group ? best : rest).push(p); }
      if (!rest.length) { toast('No bursts found', 'info'); return; }
      if (!confirm(`Pick ${best.length} and reject ${rest.length} photos?`)) return;
      await M.flag(best, 'pick'); await M.flag(rest, 'reject');
    } }),
    h('button', { text: 'Clear results', onclick: () => { state.cull.clear(); bus.emit('lib:cull'); renderGrid(); } }));
}

function cell(it) {
  const c = state.cull.get(it.p);
  const img = h('img', { loading: 'lazy', alt: '', draggable: 'false' });
  img.addEventListener('load', () => img.classList.add('loaded'));
  img.src = api.thumbUrl(it.p, 360, it.ops_hash || '');
  const box = h('div.cell', { dataset: { p: it.p }, draggable: 'true', title: `${it.name}${c && c.reasons && c.reasons.length ? '\n' + c.reasons.join('\n') : ''}` });
  fillCell(box, it, img, c);
  box.addEventListener('click', (e) => select(it.p, e));
  box.addEventListener('dblclick', () => openDevelop(it.p));
  box.addEventListener('dragstart', (e) => {
    if (!state.selection.has(it.p)) { state.selection = new Set([it.p]); paintSel(); }
    e.dataTransfer.setData('application/x-ps-paths', JSON.stringify(Array.from(state.selection)));
    e.dataTransfer.effectAllowed = 'copy';
  });
  cellEls.set(it.p, box);
  return box;
}
function fillCell(box, it, img, c) {
  const keep = img || box.querySelector('img');
  box.replaceChildren(...[keep,
    h('div.tl', { text: it.name }),
    h('div.tr', {}, it.has_edits ? h('span', { title: 'Has edits', text: '✎', style: { color: 'var(--accent)' } }) : null, it.flag === 'pick' ? h('span.flag-pick', { text: '⚑' }) : it.flag === 'reject' ? h('span.flag-reject', { text: '✕' }) : null),
    h('div.badges', {}, it.rating ? h('span.star', { text: '★'.repeat(it.rating) }) : null, it.label ? h('span.dot', { class: it.label }) : null),
    c && c.flag && c.flag !== 'maybe' ? h('div.cullmark', { style: { background: c.flag === 'pick' ? 'var(--pick)' : 'var(--reject)' } }) : null,
    c && c.best_of_group && (c.group_size || 1) > 1 ? h('div.best', { text: 'BEST' }) : null].filter(Boolean));
  box.classList.toggle('rejected', it.flag === 'reject');
  box.classList.toggle('sel', state.selection.has(it.p));
  box.classList.toggle('cur', state.cursor === it.p);
}
function refreshCell(p, thumb) {
  const box = cellEls.get(p), it = state.itemsByPath.get(p);
  if (!box || !it) return;
  const img = box.querySelector('img');
  if (thumb && img) { img.classList.remove('loaded'); img.src = api.thumbUrl(p, 360, it.ops_hash || String(Date.now())); }
  fillCell(box, it, img, state.cull.get(p));
}
function paintSel() {
  for (const [p, c] of cellEls) { c.classList.toggle('sel', state.selection.has(p)); c.classList.toggle('cur', state.cursor === p); }
  renderStatus();
}
function select(p, e) {
  const order = state.items.map((i) => i.p);
  if (e && e.shiftKey && lastAnchor) {
    const a = order.indexOf(lastAnchor), b = order.indexOf(p);
    const [lo, hi] = a < b ? [a, b] : [b, a];
    if (!e.ctrlKey && !e.metaKey) state.selection.clear();
    for (let i = lo; i <= hi; i++) state.selection.add(order[i]);
  } else if (e && (e.ctrlKey || e.metaKey)) {
    if (state.selection.has(p)) state.selection.delete(p); else state.selection.add(p);
    lastAnchor = p;
  } else { state.selection = new Set([p]); lastAnchor = p; }
  state.cursor = p;
  paintSel(); bus.emit('lib:selection');
}
function columns() {
  const first = el.gridwrap.querySelector('.grid > .cell'); if (!first) return 1;
  const gw = el.gridwrap.querySelector('.grid').getBoundingClientRect().width, cw = first.getBoundingClientRect().width + 8;
  return Math.max(1, Math.floor((gw + 8) / cw));
}
function move(dx, dy, e) {
  const order = state.items.map((i) => i.p);
  if (!order.length) return;
  let i = order.indexOf(state.cursor); if (i < 0) i = 0; else i = Math.max(0, Math.min(order.length - 1, i + dx + dy * columns()));
  select(order[i], e && e.shiftKey ? { shiftKey: true } : null);
  const c = cellEls.get(order[i]); if (c) c.scrollIntoView({ block: 'nearest' });
}

// ------------------------------------------------------------------ compare / survey
function renderCompare(mode) {
  let paths = Array.from(state.selection);
  if (mode === 'compare') { if (paths.length < 2) { const i = state.items.findIndex((x) => x.p === state.cursor); const a = state.items[Math.max(0, i)], b = state.items[Math.max(0, i) + 1]; paths = [a, b].filter(Boolean).map((x) => x.p); } paths = paths.slice(0, 2); }
  else paths = paths.length ? paths.slice(0, 12) : state.items.slice(0, 6).map((x) => x.p);
  if (!paths.length) { clear(el.gridwrap).append(h('div.empty', { text: 'Select photos in the grid first.' })); return; }
  const cols = mode === 'compare' ? paths.length : Math.ceil(Math.sqrt(paths.length));
  const box = h('div.compare', { class: mode === 'survey' ? 'survey' : '', style: { gridTemplateColumns: `repeat(${cols}, 1fr)`, height: '100%' } });
  let active = paths[0];
  const panes = paths.map((p) => {
    const it = state.itemsByPath.get(p) || { name: p };
    const pane = h('div.pane', { class: p === active ? 'sel' : '', onclick: () => { active = p; state.cursor = p; panes.forEach((x) => x.el.classList.toggle('sel', x.p === p)); bus.emit('lib:selection'); }, ondblclick: () => openDevelop(p) },
      h('img', { src: api.thumbUrl(p, 1400, it.ops_hash || ''), alt: '' }),
      h('div.cap', {}, `${it.name}  `, stars(it.rating || 0, (n) => M.rate([p], n), true), it.flag === 'pick' ? ' ⚑' : it.flag === 'reject' ? ' ✕' : ''));
    return { p, el: pane };
  });
  box.append(...panes.map((x) => x.el));
  clear(el.gridwrap).append(box);
  el.gridwrap.style.padding = '0';
  box.dataset.active = active;
}

// ------------------------------------------------------------------ cull job
async function runCull() {
  const paths = state.items.map((i) => i.p);
  if (!paths.length) return;
  try {
    const j = await api.cullRun(paths.slice(0, 5000));
    toast(`Analysing ${paths.length} photos...`, 'info', 3000);
    let job = j;
    while (job.state === 'queued' || job.state === 'running') {
      await sleep(700); job = await api.job(j.id);
      el.count.textContent = `Culling ${job.done}/${job.total}...`;
    }
    if (job.state !== 'done') throw new Error(job.error || `cull ${job.state}`);
    state.cull = new Map(job.results.map((r) => [r.p, r]));
    bus.emit('lib:cull'); setMode('cull'); renderStatus();
    toast(`Cull finished: ${job.results.filter((r) => r.flag === 'pick').length} suggested picks`, 'good');
  } catch (e) { toastError(e); renderStatus(); }
}

// ------------------------------------------------------------------ keyboard
export function onKey(e) {
  if (isTyping(e)) return false;
  const k = e.key;
  if (k === 'ArrowRight') { move(1, 0, e); return true; }
  if (k === 'ArrowLeft') { move(-1, 0, e); return true; }
  if (k === 'ArrowDown') { move(0, 1, e); return true; }
  if (k === 'ArrowUp') { move(0, -1, e); return true; }
  if ((e.ctrlKey || e.metaKey) && (k === 'a' || k === 'A')) { state.selection = new Set(state.items.map((i) => i.p)); paintSel(); bus.emit('lib:selection'); return true; }
  if (k === 'Enter' || k === 'd' || k === 'D') { const p = state.cursor || (state.items[0] && state.items[0].p); if (p) openDevelop(p); return true; }
  if (k === 'Escape') { if (state.selection.size) { state.selection.clear(); paintSel(); bus.emit('lib:selection'); return true; } return false; }
  if (k === 'g' || k === 'G') { setMode('grid'); return true; }
  if (k === 'c' || k === 'C') { setMode('compare'); return true; }
  if (k === 'n' || k === 'N') { setMode('survey'); return true; }
  if (k === 'b' || k === 'B') { setMode('cull'); return true; }
  const targets = state.selection.size ? Array.from(state.selection) : (state.cursor ? [state.cursor] : []);
  if (M.metaKey(e, targets)) return true;
  return false;
}
export function selectedPaths() { return state.selection.size ? Array.from(state.selection) : (state.cursor ? [state.cursor] : []); }
export { building };
