import { $, $$, h, isTyping } from './util.js';
import * as api from './api.js';
import { bus, state, patchItem } from './store.js';
import { loadSchema } from './schema.js';
import { session } from './session.js';
import { toast, toastError } from './toast.js';
import * as library from './views/library.js';
import * as develop from './views/develop.js';
import { showShortcuts, showExport, closeModal, modalOpen } from './dialogs.js';

async function boot() {
  try {
    const [info] = await Promise.all([api.info(), loadSchema()]);
    state.info = info;
    document.title = `Photo Studio - ${info.root_name}`;
    $('#topname').textContent = info.root;
    if (info.op_load_errors && Object.keys(info.op_load_errors).length) toast(`${Object.keys(info.op_load_errors).length} op module(s) failed to load: some controls are missing`, 'warn', 9000);
  } catch (e) { toastError(e); return; }
  library.init($('#view-library'), { navigate: setView, open: (p) => develop.openImage(p) });
  develop.init($('#view-develop'), { navigate: setView });
  $$('#modes button').forEach((b) => b.addEventListener('click', () => { if (b.dataset.view === 'develop') { const p = session.p || state.cursor || (state.items[0] && state.items[0].p); if (p) develop.openImage(p); else toast('Pick a photo first', 'info'); } else setView('library'); }));
  $('#btnExport').addEventListener('click', openExport);
  $('#btnHelp').addEventListener('click', showShortcuts);
  window.addEventListener('keydown', onKey);
  window.addEventListener('beforeunload', () => session.saveBeacon());
  bus.on('dev:loaded', () => { if (state.view === 'develop' && session.p) history.replaceState(null, '', `#develop:${encodeURIComponent(session.p)}`); });
  const m = /^#develop:(.+)$/.exec(location.hash);
  await setView('library');
  if (m) { try { await develop.openImage(decodeURIComponent(m[1])); } catch (_) { /* fall back to the library */ } }
  window.addEventListener('hashchange', () => {
    const d = /^#develop:(.+)$/.exec(location.hash);
    if (d) develop.openImage(decodeURIComponent(d[1])); else if (location.hash === '#library') setView('library');
  });
  api.startChangeLoop(onChanges);
}

export async function setView(v) {
  state.view = v;
  $$('#modes button').forEach((b) => b.classList.toggle('active', b.dataset.view === v));
  $('#view-library').classList.toggle('hidden', v !== 'library');
  $('#view-develop').classList.toggle('hidden', v !== 'develop');
  $('#topdevelop').classList.toggle('hidden', v !== 'develop');
  if (v === 'library') {
    if (session.dirty) await session.flush();
    $('#topname').textContent = state.info ? state.info.root : '';
    $('#saveind').textContent = '';
    await library.activated();
    // keep the cursor on the photo we just edited
    if (session.p && state.itemsByPath.has(session.p)) { state.cursor = session.p; bus.emit('lib:selection'); }
  } else develop.activated();
  history.replaceState(null, '', v === 'develop' && session.p ? `#develop:${encodeURIComponent(session.p)}` : `#${v}`);
}

function openExport() { showExport({ selected: state.view === 'library' ? library.selectedPaths() : [] }); }

function onKey(e) {
  if (modalOpen()) { if (e.key === 'Escape') { closeModal(); e.preventDefault(); } return; }
  if (isTyping(e)) return;
  if (e.key === '?' || (e.key === '/' && e.shiftKey)) { showShortcuts(); e.preventDefault(); return; }
  if ((e.ctrlKey || e.metaKey) && (e.key === 'e' || e.key === 'E')) { openExport(); e.preventDefault(); return; }
  if ((e.ctrlKey || e.metaKey) && ['s', 'S'].includes(e.key)) { e.preventDefault(); session.flush(); return; }
  const handled = state.view === 'develop' ? develop.onKey(e) : library.onKey(e);
  if (handled) { e.preventDefault(); return; }
  if (e.key === 'g' || e.key === 'G') { setView('library'); e.preventDefault(); }
  else if ((e.key === 'd' || e.key === 'D') && state.view === 'library') e.preventDefault();
}

// ---- edits made behind our back (Claude / the CLI) -----------------------------------------------------
const pending = new Set();
let flushTimer = null;
function onChanges(events) {
  for (const ev of events) {
    if (ev.type !== 'sidecar' || !ev.external) continue;
    if (state.view === 'develop' && ev.p === session.p) { session.onExternal(); continue; }
    pending.add(ev.p);
  }
  if (pending.size && !flushTimer) flushTimer = setTimeout(async () => {
    flushTimer = null;
    const paths = Array.from(pending); pending.clear();
    for (const p of paths) {
      try { const it = await api.item(p); patchItem(p, it); bus.emit('lib:item', p); bus.emit('ext:changed', p); } catch (_) { /* not in the library view */ }
    }
    toast(`${paths.length} photo${paths.length > 1 ? 's were' : ' was'} edited by Claude / the CLI: refreshed`, 'info', 4000);
    if (state.view === 'library') bus.emit('lib:selection');
  }, 400);
}

boot().catch((e) => { console.error(e); toastError(e); });
void h;
