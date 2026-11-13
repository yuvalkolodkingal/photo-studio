import { $, $$, h, isTyping } from '../util.js';
import { session } from '../session.js';
import * as api from '../api.js';
import { bus, state } from '../store.js';
import { toast, toastError } from '../toast.js';
import { metaKey } from '../meta.js';
import { Viewer } from '../ui/viewer.js';
import { createTools } from '../tools/index.js';
import * as left from '../panels/left.js';
import * as histogram from '../panels/histogram.js';
import * as basic from '../panels/basic.js';
import * as tonecurve from '../panels/tonecurve.js';
import * as hsl from '../panels/hsl.js';
import * as grading from '../panels/grading.js';
import * as bw from '../panels/bw.js';
import * as detail from '../panels/detail.js';
import * as lens from '../panels/lens.js';
import * as geometry from '../panels/geometry.js';
import * as effects from '../panels/effects.js';
import * as masking from '../panels/masking.js';
import * as retouch from '../panels/retouch.js';
import * as art from '../panels/art.js';
import * as allops from '../panels/allops.js';
import * as filmstrip from '../panels/filmstrip.js';

let built = false, viewer = null, tools = null, strip = null, panels = [], hintEl = null, bannerEl = null, warnEl = null, loadEl = null;
let onNavigate = null;

export function init(root, { navigate }) {
  onNavigate = navigate;
  if (built) return;
  built = true;
  const stage = h('div.stage');
  viewer = new Viewer(stage);
  session.attach(viewer);
  tools = createTools(viewer);
  hintEl = h('div.tool-hint.hidden'); bannerEl = h('div.banner.hidden'); warnEl = h('div.warn.hidden'); loadEl = h('div.loading.hidden', { text: 'Rendering...' });
  stage.append(hintEl, bannerEl, warnEl, loadEl);
  strip = filmstrip.build({ open: (p) => openImage(p) });
  const ctx = { tools, viewer };
  const leftPanel = left.build(ctx);
  panels = [histogram, basic, tonecurve, hsl, grading, bw, detail, lens, geometry, effects, masking, retouch, art, allops].map((m) => m.build(ctx));
  const right = h('div.dev-right', {}, panels.map((p) => p.el));
  root.append(h('div.dev-left', {}, leftPanel.el), h('div.dev-center', {}, stage, strip.el), right);

  bus.on('dev:stack', () => panels.forEach((p) => p.sync(session.stack)));
  bus.on('dev:loaded', () => { panels.forEach((p) => p.sync(session.stack)); updateTop(); });
  bus.on('dev:history', updateTop);
  bus.on('dev:rendered', (r) => {
    loadEl.classList.add('hidden');
    const w = r.warnings && r.warnings.length ? r.warnings.slice(0, 3).join('; ') : '';
    warnEl.textContent = w ? `Warning: ${w}` : ''; warnEl.classList.toggle('hidden', !w);
  });
  bus.on('tool:hint', (t) => { hintEl.textContent = t || ''; hintEl.classList.toggle('hidden', !t); });
  bus.on('dev:banner', (t) => { bannerEl.textContent = t; bannerEl.classList.remove('hidden'); setTimeout(() => bannerEl.classList.add('hidden'), 4500); });
  bus.on('lib:listed', () => { if (state.view === 'develop') strip.mark(); });

  // top bar wiring
  $$('#zoomseg button').forEach((b) => b.addEventListener('click', () => { const z = b.dataset.zoom; viewer.setZoom(z === 'fit' || z === 'fill' ? z : Number(z), [viewer.W / 2, viewer.H / 2]); markZoom(); }));
  $$('#baseg button').forEach((b) => b.addEventListener('click', () => setBA(b.dataset.ba)));
  $('#btnUndo').addEventListener('click', () => session.undo());
  $('#btnRedo').addEventListener('click', () => session.redo());
  viewer.onView = (v) => { session._scheduleTile(); markZoom(); void v; };
  window.addEventListener('keyup', (e) => { if (e.key === '\\') session.setPeek(false); });
}

function markZoom() {
  const m = viewer.zoomMode;
  $$('#zoomseg button').forEach((b) => { const z = b.dataset.zoom; b.classList.toggle('active', z === String(m) || (z === '1' && m === 1) || (z === '2' && m === 2)); });
}
function setBA(mode) {
  session.setBA(mode);
  $$('#baseg button').forEach((b) => b.classList.toggle('active', b.dataset.ba === mode));
}
function updateTop() {
  const hist = session.payload && session.payload.history;
  $('#btnUndo').disabled = !(hist && hist.can_undo); $('#btnRedo').disabled = !(hist && hist.can_redo);
  const nm = $('#topname'); if (nm && state.view === 'develop') nm.textContent = session.p ? `${session.p}${session.version && session.version !== 'default' ? `  [${session.version}]` : ''}` : '';
}

export async function openImage(p) {
  if (!built) return;
  if (state.view !== 'develop') onNavigate('develop');
  loadEl.classList.remove('hidden');
  try { await session.open(p); state.cursor = p; strip.mark(); updateTop(); setBA($('#baseg .active') ? $('#baseg .active').dataset.ba : 'off'); }
  catch (e) { toastError(e); loadEl.classList.add('hidden'); }
}
export function activated() { if (viewer) { viewer.resize(); strip.render(); updateTop(); markZoom(); } }
export function currentTools() { return tools; }

function step(d) {
  const i = state.items.findIndex((it) => it.p === session.p);
  const n = state.items[i + d];
  if (n) openImage(n.p);
}

/** Develop keyboard handling. Returns true when the key was used. */
export function onKey(e) {
  if (isTyping(e)) return false;
  if (tools && tools.active && tools.key(e)) { e.preventDefault(); return true; }
  const k = e.key, mod = e.ctrlKey || e.metaKey;
  if (mod && (k === 'z' || k === 'Z')) { e.shiftKey ? session.redo() : session.undo(); return true; }
  if (mod && (k === 'y' || k === 'Y')) { session.redo(); return true; }
  if (mod && e.shiftKey && (k === 'c' || k === 'C')) { left.copyNow(); return true; }
  if (mod && e.shiftKey && (k === 'v' || k === 'V')) { left.pasteNow(); return true; }
  if (mod) return false;
  if (k === 'ArrowRight') { step(1); return true; }
  if (k === 'ArrowLeft') { step(-1); return true; }
  if (k === '\\') { session.setPeek(true); return true; }
  if (k === 'y' || k === 'Y') { const on = $('#baseg .active') && $('#baseg .active').dataset.ba === 'split'; setBA(on ? 'off' : 'split'); return true; }
  if (k === 'z' || k === 'Z') { viewer.toggleFit(); markZoom(); return true; }
  if (k === 'c' || k === 'C') { tools.crop(); return true; }
  if (k === 'q' || k === 'Q') { tools.heal(); return true; }
  if (k === 'w' || k === 'W') { tools.wbPicker(); return true; }
  if (k === 'o' || k === 'O') { bus.emit('mask:toggleOverlay'); return true; }
  if (k === 'j' || k === 'J') { bus.emit('hist:toggleClip'); return true; }
  if (k === 'Tab') { document.querySelector('.dev-left').classList.toggle('hidden'); document.querySelector('.dev-right').classList.toggle('hidden'); setTimeout(() => viewer.resize(), 30); return true; }
  if (k === 'Escape') { if (tools.active) { tools.none(); return true; } return false; }
  if (session.p && metaKey(e, [session.p])) return true;
  return false;
}
