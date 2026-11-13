import { h, clear, debounce } from '../util.js';
import { session } from '../session.js';
import * as api from '../api.js';
import { bus, state } from '../store.js';
import { toast, toastError, busy } from '../toast.js';
import { section } from '../ui/section.js';

const TABS = [['mode', 'Modes'], ['art', 'Art'], ['picture_style', 'Styles'], ['user', 'Mine']];
const cache = {};
let pop = null;

function showPop(url, x, y) {
  hidePop();
  pop = h('div.pthumbpop', { style: { left: `${Math.min(x + 14, window.innerWidth - 280)}px`, top: `${Math.min(y - 20, window.innerHeight - 220)}px` } }, h('img', { src: url, alt: '' }));
  document.body.append(pop);
}
function hidePop() { if (pop) { pop.remove(); pop = null; } }

export function build() {
  const root = h('div');
  root.append(presets(), versions(), snapshots(), history(), settingsCopy());
  return { el: root, sync: () => {} };
}

// ---------------------------------------------------------------- presets
function presets() {
  const sec = section({ id: 'presets', title: 'Presets', toggle: false, open: true });
  let tab = 'mode', q = '';
  const tabs = h('div.ptabs', {}, TABS.map(([k, t]) => h('button', { text: t, dataset: { t: k }, onclick: () => { tab = k; paint(); load(); } })));
  const filter = h('input', { type: 'search', placeholder: 'Filter presets' });
  const list = h('div.plist');
  filter.addEventListener('input', () => { q = filter.value.toLowerCase(); render(); });
  filter.addEventListener('keydown', (e) => e.stopPropagation());
  const nameIn = h('input', { type: 'text', placeholder: 'New preset name' });
  nameIn.addEventListener('keydown', (e) => e.stopPropagation());
  const scope = h('select', { title: 'What to store' }, [['look', 'Look (no geometry)'], ['tone_color', 'Tone + colour'], ['all', 'Everything']].map(([v, t]) => h('option', { value: v, text: t })));
  const saveBtn = h('button', { text: 'Save', title: 'Save the current edit as a user preset', onclick: saveUser });
  sec.body.style.padding = '0';
  sec.body.append(tabs, h('div.pfilter', {}, filter), list, h('div.row2', { style: { padding: '4px 8px' } }, nameIn, scope, saveBtn));
  const paint = () => tabs.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.t === tab));
  paint();

  async function load(force) {
    if (!cache[tab] || force) {
      list.replaceChildren(h('div.empty', { text: 'Loading...' }));
      try { cache[tab] = (await api.presets(tab)).presets; } catch (e) { toastError(e); cache[tab] = []; }
    }
    render();
  }
  function render() {
    clear(list);
    const items = (cache[tab] || []).filter((p) => !q || `${p.name} ${p.title} ${(p.tags || []).join(' ')}`.toLowerCase().includes(q));
    const head = [];
    if (tab === 'mode') head.push(item({ name: 'auto', title: 'Auto-detect mode', description: 'Choose a mode from EXIF and the picture', synthetic: 'auto' }));
    if (tab === 'art') head.push(item({ name: '', title: 'No art look', description: 'Remove the art preset', synthetic: 'none' }));
    list.append(...head, ...items.map(item));
    if (!items.length && !head.length) list.append(h('div.empty', { text: tab === 'user' ? 'No saved presets yet.' : 'Nothing matches.' }));
  }
  function item(p) {
    const kind = tab === 'user' ? 'art' : tab;
    const active = (tab === 'mode' && session.mode && String(session.mode).split(':')[0] === p.name) || (tab === 'art' && session.art === p.name) || (tab === 'user' && session.art === p.name);
    const variant = p.variants && p.variants.length ? h('select', { onclick: (e) => e.stopPropagation(), title: 'Variant' }, h('option', { value: '', text: 'default' }), p.variants.map((v) => h('option', { value: v, text: v }))) : null;
    const row = h('div.pitem', { class: active ? 'active' : '', title: p.description ? p.description.slice(0, 280) : '' },
      h('div.pn', {}, h('div.t.ellipsis', { text: p.title || p.name }), h('div.d.ellipsis', { text: p.synthetic ? p.description : p.name })),
      variant,
      tab === 'user' && !p.synthetic ? h('button.small.ghost', { text: 'x', title: 'Delete preset', onclick: async (e) => { e.stopPropagation(); if (!confirm(`Delete preset "${p.name}"?`)) return; try { await api.presetDeleteUser('art', p.name); load(true); } catch (er) { toastError(er); } } }) : null);
    row.addEventListener('click', () => apply(p, kind, variant && variant.value));
    if (!p.synthetic && tab !== 'mode') {
      let t = null;
      row.addEventListener('mouseenter', (e) => { t = setTimeout(() => { if (session.p) showPop(api.presetThumbUrl(session.p, kind, p.name, 192, variant && variant.value || undefined), e.clientX, e.clientY); }, 350); });
      row.addEventListener('mouseleave', () => { clearTimeout(t); hidePop(); });
    }
    return row;
  }
  async function apply(p, kind, variant) {
    hidePop();
    if (!session.p) return;
    try {
      if (p.synthetic === 'none') {
        if (!session.art) return;
        const r = await busy(api.presetRemove({ kind: 'art', name: session.art, stack: session.stack }));
        session.setStack(r.stack, 'Remove art look', { art: null });
      } else {
        const body = { p: session.p, kind, name: p.synthetic === 'auto' ? 'auto' : p.name, stack: session.stack };
        if (variant) body.variant = variant;
        const r = await busy(api.presetApply(body));
        session.setStack(r.stack, `Preset: ${p.title || p.name}`, { mode: r.set_mode ? r.mode : undefined, art: r.set_art ? r.art : undefined });
        if (r.info) toast(typeof r.info === 'string' ? r.info : (r.info.reasons || []).join('; ') || `Applied ${p.title || p.name}`, 'info', 4000);
      }
      render();
    } catch (e) { toastError(e); }
  }
  async function saveUser() {
    const name = nameIn.value.trim();
    if (!name) { nameIn.focus(); return; }
    try {
      const r = await api.presetSaveUser({ name, stack: session.stack, kind: 'art', scope: scope.value });
      toast(`Saved preset "${r.name}" (${r.n_ops} op${r.n_ops === 1 ? '' : 's'})`, 'good'); nameIn.value = '';
      tab = 'user'; paint(); load(true);
    } catch (e) { toastError(e); }
  }
  bus.on('dev:stack', debounce(() => { if (sec.isOpen()) render(); }, 200));
  sec.el.querySelector('.sh').addEventListener('click', () => setTimeout(() => sec.isOpen() && load(), 0));
  load();
  return sec.el;
}

// ---------------------------------------------------------------- versions (virtual copies)
function versions() {
  const sec = section({ id: 'versions', title: 'Versions', toggle: false, open: false });
  const list = h('div');
  const call = (action, name, extra) => session.adoptCall(() => api.versions(session.p, action, name, extra));
  sec.body.append(list, h('div.row2', {}, h('button.small', { text: 'New copy', title: 'Duplicate the current edit as a new version (virtual copy)', onclick: () => { const n = prompt('Name for the new version', 'v' + ((session.payload && session.payload.versions.length) || 1)); if (n) call('create', n); } })));
  function paint(pl) {
    const vs = (pl && pl.versions) || [];
    list.replaceChildren(...vs.map((v) => h('div.snap-item', {},
      h('span.nm', { class: v.active || v.name === session.version ? 'dim' : '', text: `${v.name === session.version ? '● ' : ''}${v.name}  (${v.n_ops} ops)`, style: { color: v.name === session.version ? 'var(--bright)' : '' }, onclick: () => { if (v.name !== session.version) call('switch', v.name); } }),
      h('button.small.ghost', { text: 'rename', onclick: () => { const n = prompt('Rename version', v.name); if (n && n !== v.name) call('rename', v.name, n); } }),
      vs.length > 1 ? h('button.small.ghost', { text: 'x', onclick: () => { if (confirm(`Delete version "${v.name}"?`)) call('delete', v.name); } }) : null)));
  }
  bus.on('dev:loaded', paint);
  return sec.el;
}

// ---------------------------------------------------------------- snapshots
function snapshots() {
  const sec = section({ id: 'snapshots', title: 'Snapshots', toggle: false, open: false });
  const list = h('div');
  sec.body.append(list, h('div.row2', {}, h('button.small', { text: 'New snapshot', onclick: async () => {
    const n = prompt('Snapshot name', 'snapshot ' + new Date().toLocaleTimeString()); if (!n) return;
    const pl = await session.adoptCall(() => api.snapshots(session.p, 'create', n, session.version)); if (pl) toast('Snapshot saved', 'good', 2000);
  } })));
  function paint(pl) {
    const ss = (pl && pl.snapshots) || [];
    list.replaceChildren(...(ss.length ? ss.map((s) => h('div.snap-item', {},
      h('span.nm.ellipsis', { title: 'Click to restore', text: s.name, onclick: () => session.adoptCall(() => api.snapshots(session.p, 'restore', s.name, session.version)) }),
      h('span.dim', { text: (s.created || '').slice(11, 16) }),
      h('button.small.ghost', { text: 'x', onclick: () => session.adoptCall(() => api.snapshots(session.p, 'delete', s.name, session.version)) }))) : [h('div.subtle', { text: 'No snapshots. Resetting an image keeps an automatic one.' })]));
  }
  bus.on('dev:loaded', paint);
  return sec.el;
}

// ---------------------------------------------------------------- history
function history() {
  const sec = section({ id: 'history', title: 'History', toggle: false, open: true });
  const list = h('div', { style: { maxHeight: '240px', overflowY: 'auto' } });
  sec.body.style.padding = '0';
  sec.body.append(list);
  function paint(hist) {
    if (!hist) return;
    list.replaceChildren(...hist.entries.map((e, i) => h('div.hist-item', { class: `${i === hist.index ? 'cur' : ''} ${i > hist.index ? 'future' : ''}`, onclick: () => { if (i !== hist.index) session.gotoHistory(e.id); } },
      h('span.ellipsis', { text: e.label }), h('span.n', { text: `${e.n_ops}` }))).reverse());
  }
  bus.on('dev:history', paint);
  return sec.el;
}

// ---------------------------------------------------------------- copy / paste settings
function settingsCopy() {
  const sec = section({ id: 'copy', title: 'Copy settings', toggle: false, open: false });
  sec.body.append(
    h('div.row2', {}, h('button', { text: 'Copy', title: 'Copy this image\'s edit (Ctrl+Shift+C)', onclick: () => copyNow() }),
      h('button', { text: 'Paste', title: 'Paste onto this image (Ctrl+Shift+V)', onclick: () => pasteNow() })),
    h('div.row2', {}, h('button', { text: 'Sync to selected', title: 'Apply this edit to the images selected in the library', onclick: async () => {
      const targets = Array.from(state.selection).filter((p) => p !== session.p);
      if (!targets.length) { toast('Select images in the Library first', 'warn'); return; }
      try { const r = await api.syncSettings(session.p, targets, null, 'replace'); toast(`Synced to ${r.synced.length} image(s)`, 'good'); } catch (e) { toastError(e); }
    } })),
    h('div.subtle', { text: 'Crop and retouch spots are copied too; paste mode replaces the target edit.' }));
  return sec.el;
}
export async function copyNow() {
  if (!session.p) return;
  await session.flush();
  try { const r = await api.copySettings(session.p); state.clipboard = r; toast(`Copied ${(r.ops || []).length} op(s)`, 'good', 2000); } catch (e) { toastError(e); }
}
export async function pasteNow(paths) {
  if (!state.clipboard) { toast('Nothing copied yet', 'warn'); return; }
  const targets = paths && paths.length ? paths : [session.p];
  try {
    await session.flush();
    const r = await api.pasteSettings(targets, 'replace');
    toast(`Pasted to ${r.pasted.length} image(s)`, 'good', 2500);
    if (targets.includes(session.p)) await session.reload('server');
  } catch (e) { toastError(e); }
}
