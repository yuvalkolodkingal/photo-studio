import { h, clone, uid } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import * as api from '../api.js';
import { bus, pref } from '../store.js';
import { toast, toastError } from '../toast.js';
import { slider } from '../ui/slider.js';
import { section } from '../ui/section.js';
import { brushSettings } from '../tools/mask.js';

const TYPES = {
  linear: { label: 'Linear gradient', make: () => ({ type: 'linear', p0: [0.5, 0.3], p1: [0.5, 0.6], feather: 0 }) },
  radial: { label: 'Radial', make: () => ({ type: 'radial', center: [0.5, 0.5], radii: [0.25, 0.25], rotation: 0, feather: 60 }) },
  brush: { label: 'Brush', make: () => ({ type: 'brush', strokes: [] }) },
  color_range: { label: 'Colour range', make: () => ({ type: 'color_range', point: [0.5, 0.5], tolerance: 14, falloff: 14 }) },
  luminosity: { label: 'Luminance range', make: () => ({ type: 'luminosity', range: [0.6, 1.0], softness: 0.15 }) },
  ai_subject: { label: 'Subject (AI)', make: () => ({ type: 'ai_subject' }) },
  ai_sky: { label: 'Sky (AI)', make: () => ({ type: 'ai_sky' }) },
  ai_background: { label: 'Background (AI)', make: () => ({ type: 'ai_background' }) },
  ai_face: { label: 'Face (AI)', make: () => ({ type: 'ai_face' }) },
  ai_skin: { label: 'Skin (AI)', make: () => ({ type: 'ai_skin' }) },
  ai_eyes: { label: 'Eyes (AI)', make: () => ({ type: 'ai_eyes' }) },
};
const DRAW = new Set(['linear', 'radial', 'brush', 'color_range']);
const MODE_LABEL = { add: 'Add', subtract: 'Subtract', intersect: 'Intersect' };

const INTRINSIC = new Set(['linear', 'radial', 'brush', 'polygon']);
const GROUP_KEYS = ['ui', 'name', 'invert', 'opacity'];
/** canonical JSON of a spec without the UI-only / group-level keys, for comparing "does ui still describe this spec" */
function norm(spec, top = true) {
  const o = clone(spec);
  if (top) { for (const k of GROUP_KEYS) delete o[k]; if (!INTRINSIC.has(o.type)) delete o.feather; }
  const sort = (v) => (Array.isArray(v) ? v.map(sort) : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, sort(v[k])])) : v);
  return JSON.stringify(sort(o));
}
/** components from a stored spec: our own ui metadata while it still matches the spec, else the spec as one opaque component */
function parseComponents(spec) {
  const ui = spec && spec.ui && spec.ui.components;
  if (Array.isArray(ui) && ui.length && norm(S.compose(ui)) === norm(spec)) return clone(ui);
  const s = clone(spec || { type: 'all' });
  for (const k of GROUP_KEYS) delete s[k];
  return [{ mode: 'add', spec: s }];
}

export function build({ tools }) {
  let work = null;             // {mid, name, enabled, opacity, adj, components, invert, gfeather}
  let sel = { ci: 0 };
  let showOverlay = pref.get('mask.overlay', true);
  let selfEdit = false, sig = '';
  const aiNotes = new Map();

  const sec = section({
    id: 'masking', title: 'Masking', toggle: false, open: false,
    onReset: () => { if (S.maskGroups(session.stack).length && confirm('Remove all local adjustments from this version?')) session.edit('Remove all masks', (st) => { for (const g of S.maskGroups(st)) S.removeMaskGroup(st, g.mid); }); },
    onOpen: () => { update(true); },
  });
  const addBar = h('div'); const list = h('div'); const editor = h('div');
  sec.body.append(h('div.sub', { text: 'New local adjustment' }), addBar, h('div.sub', { text: 'Adjustments' }), list, editor);

  // ---- new mask / add component buttons
  const modeSel = h('select', {}, Object.entries(MODE_LABEL).map(([k, t]) => h('option', { value: k, text: t })));
  function typeButtons(onPick, big) {
    const box = h('div.aspects');
    for (const [k, t] of Object.entries(TYPES)) box.append(h('button', { text: t.label.replace(' (AI)', ''), title: t.label, class: k.startsWith('ai_') ? 'ai' : '', onclick: () => onPick(k) }));
    return box;
  }
  addBar.append(typeButtons((k) => newGroup(k)));
  function cur() { return work; }

  const hasGroupFeather = (spec) => !INTRINSIC.has(spec.type);
  function toGroup(g) {
    const comps = g.components;
    const spec = S.compose(comps) || { type: 'all' };
    if (g.gfeather && hasGroupFeather(spec)) spec.feather = g.gfeather;
    spec.ui = { components: clone(comps) };
    if (g.invert) spec.invert = true;
    return { mid: g.mid, name: g.name, spec, enabled: g.enabled, adj: g.adj, opacity: g.opacity };
  }
  function write(label, live) {
    if (!work) return;
    selfEdit = true;
    try { session.edit(label, (st) => S.writeMaskGroup(st, toGroup(work)), { live }); } finally { selfEdit = false; }
    refreshOverlay();
  }
  function refreshOverlay() {
    if (!work || !showOverlay || !sec.isOpen()) { session.setMaskPreview(null); return; }
    const spec = toGroup(work).spec;
    session.setMaskPreview(spec);
  }

  // ---- group lifecycle
  function newGroup(type) {
    const mid = uid(5);
    const n = S.maskGroups(session.stack).length + 1;
    work = { mid, name: `${TYPES[type].label.replace(' (AI)', '')} ${n}`, enabled: true, opacity: 1, adj: {}, components: [{ mode: 'add', spec: TYPES[type].make() }], invert: false, gfeather: 0 };
    sel = { ci: 0 };
    write('New mask: ' + TYPES[type].label, false);
    sec.open(); bindTool(); onAi(0);
    update(true);
  }
  function addComponent(type) {
    if (!work) return;
    work.components.push({ mode: modeSel.value, spec: TYPES[type].make() });
    sel.ci = work.components.length - 1;
    write(`Add ${TYPES[type].label} to mask`, false); bindTool(); onAi(sel.ci); update(true);
  }
  function selectGroup(mid) {
    const g = S.maskGroups(session.stack).find((x) => x.mid === mid);
    if (!g) return;
    work = fromStack(g); sel = { ci: 0 }; update(true); bindTool(); refreshOverlay();
  }
  function fromStack(g) {
    const spec = clone(g.spec);
    return { mid: g.mid, name: g.name || spec.name || 'Mask', enabled: g.enabled, opacity: g.opacity, adj: clone(g.adj), components: parseComponents(spec), invert: !!spec.invert, gfeather: hasGroupFeather(spec) ? (spec.feather || 0) : 0 };
  }
  function removeGroup(mid) {
    session.edit('Delete mask', (st) => S.removeMaskGroup(st, mid));
    if (work && work.mid === mid) { work = null; tools.none(); session.setMaskPreview(null); }
  }
  async function onAi(ci) {
    const c = work && work.components[ci]; if (!c || !c.spec.type.startsWith('ai_')) return;
    try {
      const r = await api.maskAi(session.p, c.spec.type, session.stack);
      aiNotes.set(`${work.mid}:${ci}`, r);
      update(true); refreshOverlay();
      if (r.notes && r.notes.length) toast(r.notes.join(' '), 'info', 6000);
    } catch (e) { toastError(e); }
  }
  function bindTool() {
    const c = work && work.components[sel.ci];
    if (!c || !DRAW.has(c.spec.type) || !sec.isOpen()) { if (tools.active === 'mask') tools.none(); return; }
    tools.mask();
    tools.maskTool.bind(c, (final) => write(final ? 'Edit mask' : 'Edit mask', !final));
  }

  // ---- rendering the editor
  const syncs = [];
  function groupSig() { return JSON.stringify([S.maskGroups(session.stack).map((g) => [g.mid, g.name, g.enabled]), work && [work.mid, work.components.map((c) => [c.mode, c.spec.type]), sel.ci]]); }
  function update(force) {
    const gs = S.maskGroups(session.stack);
    if (work && !gs.find((g) => g.mid === work.mid) && !selfEdit) work = null;
    const s = groupSig();
    if (!force && s === sig) { syncs.forEach((f) => f()); return; }
    sig = s; renderList(gs); renderEditor();
  }
  function renderList(gs) {
    list.replaceChildren(...(gs.length ? gs.map((g) => h('div.layer', { class: work && work.mid === g.mid ? 'active' : '', onclick: () => selectGroup(g.mid) },
      h('span.tgl', { title: 'Show / hide this adjustment', text: g.enabled ? '◉' : '○', onclick: (e) => { e.stopPropagation(); session.edit('Toggle mask', (st) => { for (const en of st) if ((en.id || '').startsWith(`mask:${g.mid}:`)) en.enabled = !g.enabled; }); } }),
      h('span.nm', { text: g.name || 'Mask', ondblclick: (e) => { e.stopPropagation(); const n = prompt('Name', g.name); if (n != null) { if (!work || work.mid !== g.mid) { work = fromStack(g); } work.name = n; write('Rename mask', false); } } }),
      h('button.small.ghost', { title: 'Delete', text: 'x', onclick: (e) => { e.stopPropagation(); removeGroup(g.mid); } })))
      : [h('div.subtle', { text: 'No local adjustments yet. Pick a mask type above.' })]));
  }
  function field(label, node) { return h('div.row2', {}, h('label', { text: label }), node); }
  function compControls(c, ci) {
    const s = c.spec, box = h('div');
    const edit = (live) => write('Edit mask', live);
    const sl = (label, key, min, max, step, def, digits, target = s, scale = 1) => {
      const w = slider({ label, min, max, step, digits, value: target[key] ?? def, def, scale, onInput: (v) => { target[key] = v; edit(true); bus.emit('mask:param'); }, onCommit: (v) => { target[key] = v; edit(false); } });
      syncs.push(() => w.set(target[key] ?? def)); return w.el;
    };
    if (s.type === 'linear') box.append(sl('Feather', 'feather', 0, 400, 1, 0, 0));
    else if (s.type === 'radial') box.append(sl('Feather', 'feather', 0, 400, 1, 60, 0), sl('Rotation', 'rotation', -180, 180, 1, 0, 0));
    else if (s.type === 'brush') {
      const bs = (label, key, min, max, step, digits) => { const w = slider({ label, min, max, step, digits, value: brushSettings[key], def: brushSettings[key], onInput: (v) => { brushSettings[key] = v; }, onCommit: (v) => { brushSettings[key] = v; } }); return w.el; };
      box.append(bs('Size', 'radius', 2, 300, 1, 0), bs('Hardness', 'hardness', 0, 1, 0.01, 2), bs('Flow', 'flow', 0.05, 1, 0.01, 2),
        h('div.row2', {}, h('span.subtle', { text: `${(s.strokes || []).length} stroke(s). Alt = erase.` }), h('span.spacer'),
          h('button.small', { text: 'Undo stroke', onclick: () => { s.strokes.pop(); write('Edit mask', false); } }),
          h('button.small', { text: 'Clear', onclick: () => { s.strokes = []; write('Clear brush', false); } })));
    } else if (s.type === 'color_range') {
      box.append(sl('Tolerance', 'tolerance', 1, 60, 1, 14, 0), sl('Falloff', 'falloff', 1, 60, 1, 14, 0),
        h('div.subtle', { text: 'Click the picture to sample the colour (the marker shows the sample).' }));
    } else if (s.type === 'luminosity') {
      const lo = slider({ label: 'From', min: 0, max: 1, step: 0.01, value: s.range[0], def: 0, scale: 100, digits: 0, onInput: (v) => { s.range = [v, Math.max(s.range[1], v)]; edit(true); }, onCommit: (v) => { s.range = [v, Math.max(s.range[1], v)]; edit(false); } });
      const hi = slider({ label: 'To', min: 0, max: 1, step: 0.01, value: s.range[1], def: 1, scale: 100, digits: 0, onInput: (v) => { s.range = [Math.min(s.range[0], v), v]; edit(true); }, onCommit: (v) => { s.range = [Math.min(s.range[0], v), v]; edit(false); } });
      syncs.push(() => { lo.set(s.range[0]); hi.set(s.range[1]); });
      box.append(lo.el, hi.el, sl('Softness', 'softness', 0, 0.5, 0.01, 0.15, 0, s, 100));
    } else if (s.type.startsWith('ai_')) {
      const n = aiNotes.get(`${work.mid}:${ci}`);
      box.append(h('div.subtle', { text: n ? `Coverage ${(n.coverage * 100).toFixed(0)}%. ${(n.notes || []).join(' ')}` : 'Detecting...' }),
        h('div.row2', {}, h('button.small', { text: 'Re-detect', onclick: () => onAi(ci) })));
    }
    return box;
  }
  function renderEditor() {
    syncs.length = 0;
    if (!work) { editor.replaceChildren(); return; }
    const g = work;
    const comps = h('div');
    g.components.forEach((c, ci) => {
      comps.append(h('div.comp', { class: ci === sel.ci ? 'active' : '', onclick: () => { sel.ci = ci; update(true); bindTool(); refreshOverlay(); } },
        ci === 0 ? h('span.dim', { text: 'Base' }) : h('select', { onclick: (e) => e.stopPropagation(), onchange: (e) => { c.mode = e.target.value; write('Mask mode', false); } }, Object.entries(MODE_LABEL).map(([k, t]) => h('option', { value: k, text: t, selected: c.mode === k }))),
        h('span', { text: TYPES[c.spec.type] ? TYPES[c.spec.type].label : c.spec.type }), h('span.spacer'),
        g.components.length > 1 ? h('button.small.ghost', { text: 'x', title: 'Remove component', onclick: (e) => { e.stopPropagation(); g.components.splice(ci, 1); sel.ci = 0; write('Remove mask component', false); bindTool(); update(true); } }) : null));
    });
    const adds = h('div', {}, h('div.row2', {}, h('label', { text: 'Add to mask' }), modeSel), typeButtons((k) => addComponent(k)));
    const c = g.components[sel.ci] || g.components[0];
    const overlayCb = h('input', { type: 'checkbox', checked: showOverlay });
    overlayCb.addEventListener('change', () => { showOverlay = overlayCb.checked; pref.set('mask.overlay', showOverlay); refreshOverlay(); });
    const invCb = h('input', { type: 'checkbox', checked: g.invert });
    invCb.addEventListener('change', () => { g.invert = invCb.checked; write('Invert mask', false); });
    const adj = h('div');
    for (const key of S.MASK_ADJ_ORDER) {
      const [o, pname, label, min, max, step, scale] = S.MASK_ADJ[key];
      const def = key === 'sharpen' || key === 'noise' ? 0 : 0;
      const w = slider({ label, min, max, step, scale: scale || 1, value: g.adj[key] ?? def, def, digits: scale ? 0 : (step < 1 ? 2 : 0), onInput: (v) => { g.adj[key] = v; write('Local ' + label, true); }, onCommit: (v) => { g.adj[key] = v; write('Local ' + label, false); } });
      syncs.push(() => w.set(g.adj[key] ?? def)); adj.append(w.el);
    }
    const opac = slider({ label: 'Amount', min: 0, max: 1, step: 0.01, scale: 100, digits: 0, value: g.opacity, def: 1, onInput: (v) => { g.opacity = v; write('Mask amount', true); }, onCommit: (v) => { g.opacity = v; write('Mask amount', false); } });
    const gfOk = hasGroupFeather(S.compose(g.components) || { type: 'all' });
    const gf = slider({ label: 'Edge softness', min: 0, max: 200, step: 1, digits: 0, value: g.gfeather || 0, def: 0, onInput: (v) => { g.gfeather = v; write('Mask feather', true); }, onCommit: (v) => { g.gfeather = v; write('Mask feather', false); } });
    editor.replaceChildren(...[
      h('div.sub', { text: `Mask: ${g.name}` }), comps, adds,
      h('div.sub', { text: 'Selected component' }), c ? compControls(c, g.components.indexOf(c)) : null,
      h('div.sub', { text: 'Mask options' }),
      h('div.row2', {}, h('label', {}, invCb, ' Invert'), h('label', { title: 'Show the mask as a red overlay (O)' }, overlayCb, ' Show overlay (O)')),
      gfOk ? gf.el : null, opac.el,
      h('div.sub', { text: 'Adjustments inside the mask' }), adj,
      h('div.subtle', { text: 'Coordinates are relative to the cropped picture. Each adjustment is stored as its own stack entry with this mask, so the CLI and Claude can read and edit it.' })].filter(Boolean));
  }

  bus.on('dev:stack', (cause) => {
    if (selfEdit) { return; }
    if (cause === 'external' || cause === 'undo' || cause === 'redo' || cause === 'history' || cause === 'load' || cause === 'server' || cause === 'conflict' || cause === 'reload') {
      if (work) { const g = S.maskGroups(session.stack).find((x) => x.mid === work.mid); work = g ? fromStack(g) : null; }
      update(true); refreshOverlay();
    } else update(false);
  });
  bus.on('dev:loaded', () => { work = null; sel = { ci: 0 }; update(true); refreshOverlay(); });
  bus.on('mask:toggleOverlay', () => { showOverlay = !showOverlay; pref.set('mask.overlay', showOverlay); update(true); refreshOverlay(); });
  bus.on('tool', (n) => { if (n !== 'mask' && sec.isOpen() && work && DRAW.has((work.components[sel.ci] || {}).spec && work.components[sel.ci].spec.type) === false) { /* keep */ } });
  sec.el.querySelector('.sh').addEventListener('click', () => setTimeout(() => { if (sec.isOpen()) { bindTool(); refreshOverlay(); } else { if (tools.active === 'mask') tools.none(); session.setMaskPreview(null); } }, 0));
  return { el: sec.el, sync: () => update(false), cur, newGroup };
}
export { TYPES };
