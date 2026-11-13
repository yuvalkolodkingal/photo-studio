// Helpers shared by the right-hand panels: sliders bound to stack params, schema-driven generic fields.
import { h, clone, eq, fmtNum, titleCase } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import * as SC from '../schema.js';
import { slider } from '../ui/slider.js';
import { section } from '../ui/section.js';

/** A panel collects `syncers` (functions that pull values from session.stack into controls). */
export class Syncs {
  constructor() { this.fns = []; }
  add(fn) { this.fns.push(fn); return fn; }
  run() { for (const f of this.fns) { try { f(session.stack); } catch (e) { console.error(e); } } }
}

/**
 * Slider bound to <op>.<param>. Range, default, scale and digits come from the op schema; `o` overrides them.
 * o: {label, min, max, step, def, scale, digits, cls, patch(v) -> extra params to set, label for history, fallback}
 */
export function bound(syncs, op, pname, o = {}) {
  const p = SC.param(op, pname) || {};
  const min = o.min ?? p.min ?? -1, max = o.max ?? p.max ?? 1, step = o.step ?? p.step ?? (max - min) / 200;
  const def = o.def ?? (p.default == null ? (o.fallback ?? 0) : p.default);
  const scale = o.scale ?? p.scale ?? 1;
  const label = o.label || p.label || titleCase(pname);
  const hist = o.history || label;
  // seed: params written when this control creates the op's entry (e.g. {amount: 0} so a new vignette starts neutral)
  const apply = (v) => (st) => { if (o.seed && !S.findEntry(st, op)) S.setParams(st, op, o.seed); S.setParams(st, op, { [pname]: v, ...(o.patch ? o.patch(v) : {}) }); };
  const read = (st) => (o.absent !== undefined && !S.findEntry(st, op) ? o.absent : S.get(st, op, pname, def));
  const sl = slider({
    label, min, max, step, value: read(session.stack), def, scale, digits: o.digits ?? p.digits, unit: o.unit ?? p.unit, cls: o.cls || '', title: o.title,
    onInput: (v) => session.edit(hist, apply(v), { live: true }),
    onCommit: (v) => session.edit(hist, apply(v)),
  });
  syncs.add((st) => sl.set(read(st)));
  return sl.el;
}

/** Section whose toggle/reset/modified dot cover a list of ops. `enabledFn(stack)` overrides "enabled" detection. */
export function opsSection({ id, title, ops, open = false, toggleCreates = false, absentMeansOff = false, extraReset, help }) {
  const sec = section({
    id, title, open, help,
    onToggle: (on) => session.edit(`${on ? 'Enable' : 'Disable'} ${title}`, (st) => {
      for (const o of ops) {
        const es = S.findAll(st, o);
        if (es.length) es.forEach((e) => { e.enabled = on; });
        else if (on && (toggleCreates || absentMeansOff)) S.ensure(st, o);
      }
    }),
    onReset: () => session.edit(`Reset ${title}`, (st) => { S.resetOps(st, ops, session.defaultOps); if (extraReset) extraReset(st); }),
  });
  const refresh = (st) => {
    sec.setModified(ops.some((o) => S.isModified(st, o)) || (absentMeansOff && ops.some((o) => S.findAll(st, o).length)));
    const es = ops.flatMap((o) => S.findAll(st, o));
    sec.setEnabled(es.length ? es.some((e) => e.enabled !== false) : !absentMeansOff);
  };
  return { sec, refresh };
}

// ------------------------------------------------------------------ schema driven generic fields
/** Build a control for one param definition. get() -> current value (may be undefined), set(v, live) -> writes it. */
export function paramField(opName, pd, get, set) {
  const label = pd.label || titleCase(pd.name);
  const cur = () => { const v = get(); return v === undefined ? pd.default : v; };
  const row = (control, extra) => h('div.pfield', {}, h('span.lbl', { title: pd.name, text: label }), control, extra);
  const t = pd.type;
  if ((t === 'number' || t === 'int') && pd.min != null && pd.max != null) {
    const sl = slider({
      label, min: pd.min, max: pd.max, step: pd.step || (t === 'int' ? 1 : (pd.max - pd.min) / 200), value: cur() == null ? (pd.min + pd.max) / 2 : cur(), def: pd.default == null ? (pd.min + pd.max) / 2 : pd.default,
      scale: pd.scale || 1, digits: pd.digits, unit: pd.unit, onInput: (v) => set(t === 'int' ? Math.round(v) : v, true), onCommit: (v) => set(t === 'int' ? Math.round(v) : v, false),
    });
    sl.el.title = pd.name; sl.sync = () => sl.set(cur());
    return { el: sl.el, sync: () => sl.set(cur() == null ? pd.default : cur()) };
  }
  if (t === 'number' || t === 'int') {
    const inp = h('input', { type: 'number', step: t === 'int' ? 1 : 'any', value: cur() ?? '', placeholder: pd.default == null ? 'auto' : '' });
    inp.addEventListener('change', () => set(inp.value === '' ? null : Number(inp.value), false));
    inp.addEventListener('keydown', (e) => e.stopPropagation());
    return { el: row(inp), sync: () => { if (document.activeElement !== inp) inp.value = cur() ?? ''; } };
  }
  if (t === 'bool') {
    const cb = h('input', { type: 'checkbox', checked: !!cur() });
    cb.addEventListener('change', () => set(cb.checked, false));
    return { el: row(cb), sync: () => { cb.checked = !!cur(); } };
  }
  if (t === 'choice') {
    const sel = h('select', {}, (pd.choices || []).map((c) => h('option', { value: c === null ? '' : String(c), text: c === null ? '(none)' : String(c) })));
    sel.value = cur() === null || cur() === undefined ? '' : String(cur());
    sel.addEventListener('change', () => set(sel.value === '' ? null : sel.value, false));
    return { el: row(sel), sync: () => { sel.value = cur() == null ? '' : String(cur()); } };
  }
  if (t === 'vec') {
    const n = Array.isArray(pd.default) ? pd.default.length : 3;
    const ins = Array.from({ length: n }, (_, i) => h('input', { type: 'number', step: 'any', value: (cur() || [])[i] ?? 0 }));
    const commit = () => set(ins.map((i) => Number(i.value) || 0), false);
    ins.forEach((i) => { i.addEventListener('change', commit); i.addEventListener('keydown', (e) => e.stopPropagation()); });
    return { el: row(h('div.vecs', {}, ins)), sync: () => ins.forEach((i, k) => { if (document.activeElement !== i) i.value = (cur() || [])[k] ?? 0; }) };
  }
  if (t === 'color') {
    const inp = h('input', { type: 'text', value: typeof cur() === 'string' ? cur() : JSON.stringify(cur() ?? ''), placeholder: '#rrggbb or [r,g,b]' });
    inp.addEventListener('change', () => { let v = inp.value.trim(); if (v.startsWith('[')) { try { v = JSON.parse(v); } catch (_) { return; } } set(v === '' ? null : v, false); });
    inp.addEventListener('keydown', (e) => e.stopPropagation());
    return { el: row(inp), sync: () => { if (document.activeElement !== inp) inp.value = typeof cur() === 'string' ? cur() : JSON.stringify(cur() ?? ''); } };
  }
  if (t === 'string') {
    const inp = h('input', { type: 'text', value: cur() ?? '' });
    inp.addEventListener('change', () => set(inp.value === '' ? null : inp.value, false));
    inp.addEventListener('keydown', (e) => e.stopPropagation());
    return { el: row(inp), sync: () => { if (document.activeElement !== inp) inp.value = cur() ?? ''; } };
  }
  // json / colors / anything else: validated JSON text
  const ta = h('textarea', { spellcheck: 'false' });
  ta.value = JSON.stringify(cur() ?? null);
  ta.addEventListener('keydown', (e) => e.stopPropagation());
  ta.addEventListener('change', () => { try { set(JSON.parse(ta.value), false); ta.style.outline = ''; } catch (_) { ta.style.outline = '1px solid #e0605a'; } });
  return { el: row(ta), sync: () => { if (document.activeElement !== ta) ta.value = JSON.stringify(cur() ?? null); } };
}

/** All params of an op as generic fields bound to an entry (found by op + optional id). skip: param names to hide. */
export function opFields(opName, { id = null, skip = [], only = null } = {}) {
  const o = SC.op(opName);
  const box = h('div');
  const fields = [];
  for (const pd of (o ? o.params : [])) {
    if (skip.includes(pd.name) || (only && !only.includes(pd.name))) continue;
    const f = paramField(opName, pd,
      () => { const e = S.findEntry(session.stack, opName, id); return e && e.params ? e.params[pd.name] : undefined; },
      (v, live) => session.edit(SC.opLabel(opName) + ': ' + (pd.label || pd.name), (st) => S.setParams(st, opName, { [pd.name]: v }, id), { live }));
    fields.push(f); box.append(f.el);
  }
  return { el: box, sync: () => fields.forEach((f) => f.sync()) };
}
export { fmtNum, clone, eq };
