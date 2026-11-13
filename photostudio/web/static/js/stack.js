// Pure helpers over a stack (array of {op, params, enabled, mask, id, ...}). Mutating helpers expect an already cloned stack.
import { defaultOf, op as opSchema } from './schema.js';
import { clone, eq } from './util.js';

export const isPlain = (e) => !e.mask && !e.id;
export const findEntry = (stack, op, id = null) => stack.find((e) => e.op === op && (id ? e.id === id : isPlain(e)));
export const findAll = (stack, op) => stack.filter((e) => e.op === op && isPlain(e));

/** Value of a param with the schema default when the stack does not set it. */
export function get(stack, op, name, fallback) {
  const e = findEntry(stack, op);
  if (e && e.params && e.params[name] !== undefined && e.params[name] !== null) return e.params[name];
  const d = defaultOf(op, name);
  return d === undefined || d === null ? fallback : d;
}
export function getRaw(stack, op, name) {
  const e = findEntry(stack, op);
  return e && e.params ? e.params[name] : undefined;
}

export function ensure(stack, op, id = null) {
  let e = findEntry(stack, op, id);
  if (!e) {
    e = { op, params: {}, enabled: true, mask: null, blend: 'normal', opacity: 1.0, v: (opSchema(op) || {}).version || 1 };
    if (id) e.id = id;
    // geometry ops must run before the crop (stack order breaks ties inside the geometry stage)
    const ci = ['rotate', 'auto_level', 'straighten', 'perspective'].includes(op) ? stack.findIndex((x) => x.op === 'crop') : -1;
    if (ci >= 0) stack.splice(ci, 0, e); else stack.push(e);
  }
  if (!e.params) e.params = {};
  return e;
}
export function setParams(stack, op, patch, id = null) {
  const e = ensure(stack, op, id);
  Object.assign(e.params, patch);
  e.enabled = true;
  return e;
}
export function removeParam(stack, op, name) {
  const e = findEntry(stack, op);
  if (e && e.params) delete e.params[name];
}
export function setEnabled(stack, op, on) {
  for (const e of stack) if (e.op === op && isPlain(e)) e.enabled = !!on;
}
export function isEnabled(stack, op) {
  const es = findAll(stack, op);
  return es.length ? es.some((e) => e.enabled !== false) : true;
}
const numEq = (a, b) => (typeof a === 'number' && typeof b === 'number' ? Math.abs(a - b) < 1e-9 : eq(a, b));
/** true if the op's entry sets any param to something other than the default */
export function isModified(stack, op) {
  for (const e of findAll(stack, op)) {
    for (const [k, v] of Object.entries(e.params || {})) {
      const d = defaultOf(op, k);
      if (v === null && (d === null || d === undefined)) continue;
      if (!numEq(v, d)) return true;
    }
  }
  return false;
}
/** reset = defaults. Ops the engine includes by default keep an (empty) entry; others are removed. */
export function resetOps(stack, ops, keep = []) {
  const set = new Set(ops);
  const out = [];
  for (const e of stack) {
    if (set.has(e.op) && isPlain(e)) {
      if (keep.includes(e.op)) out.push({ ...e, params: {}, enabled: true });
      continue;
    }
    out.push(e);
  }
  stack.length = 0; stack.push(...out);
  return stack;
}
export function resetParams(stack, op, names) {
  const e = findEntry(stack, op);
  if (e) for (const n of names) delete e.params[n];
}

// ------------------------------------------------------------------ masks (local adjustments)
export const MASK_ADJ = {
  // UI key -> [op, param, label, min, max, step, scale]
  ev: ['exposure', 'ev', 'Exposure', -5, 5, 0.01],
  temp: ['temperature', 'temperature', 'Temp', -100, 100, 1],
  tint: ['temperature', 'tint', 'Tint', -100, 100, 1],
  contrast: ['contrast', 'contrast', 'Contrast', -0.8, 0.8, 0.01, 100],
  highlights: ['highlights_shadows', 'highlights', 'Highlights', -100, 100, 1],
  shadows: ['highlights_shadows', 'shadows', 'Shadows', -100, 100, 1],
  whites: ['highlights_shadows', 'whites', 'Whites', -100, 100, 1],
  blacks: ['highlights_shadows', 'blacks', 'Blacks', -100, 100, 1],
  texture: ['local_contrast', 'texture', 'Texture', -1, 1.5, 0.01, 100],
  clarity: ['local_contrast', 'clarity', 'Clarity', -1, 1.5, 0.01, 100],
  dehaze: ['dehaze', 'strength', 'Dehaze', -1, 1, 0.01, 100],
  vibrance: ['vibrance', 'amount', 'Vibrance', -100, 100, 1],
  saturation: ['saturation', 'amount', 'Saturation', -100, 100, 1],
  sharpen: ['sharpen', 'amount', 'Sharpness', 0, 3, 0.01],
  noise: ['denoise', 'luma', 'Noise reduction', 0, 100, 1],
};
export const MASK_ADJ_ORDER = ['ev', 'temp', 'tint', 'contrast', 'highlights', 'shadows', 'whites', 'blacks', 'texture', 'clarity', 'dehaze', 'vibrance', 'saturation', 'sharpen', 'noise'];

const idFor = (mid, o) => `mask:${mid}:${o}`;
/** Read the local-adjustment groups out of a stack. A group = all entries whose id is mask:<mid>:<op>. */
export function maskGroups(stack) {
  const groups = new Map();
  for (const e of stack) {
    const m = /^mask:([^:]+):(.+)$/.exec(e.id || '');
    if (!m || !e.mask) continue;
    let g = groups.get(m[1]);
    if (!g) { g = { mid: m[1], name: (e.mask && e.mask.name) || '', spec: clone(e.mask), enabled: e.enabled !== false, adj: {}, opacity: e.opacity == null ? 1 : e.opacity, entries: {} }; groups.set(m[1], g); }
    g.entries[m[2]] = e;
  }
  for (const g of groups.values()) {
    for (const [key, [o, pname]] of Object.entries(MASK_ADJ)) {
      const e = g.entries[o];
      if (e && e.params && e.params[pname] !== undefined) g.adj[key] = e.params[pname];
    }
    g.name = g.name || (g.spec && g.spec.name) || '';
  }
  return Array.from(groups.values());
}
/** Write a group back: remove its entries, then add one entry per op that has a non-neutral adjustment (+ a neutral exposure anchor). */
export function writeMaskGroup(stack, g) {
  const prefix = `mask:${g.mid}:`;
  const keep = stack.filter((e) => !(e.id || '').startsWith(prefix));
  const byOp = new Map();
  for (const [key, val] of Object.entries(g.adj || {})) {
    const def = MASK_ADJ[key]; if (!def) continue;
    const [o, pname] = def;
    const neutral = defaultOf(o, pname);
    if (val === undefined || val === null || (typeof val === 'number' && typeof neutral === 'number' && Math.abs(val - neutral) < 1e-9)) continue;
    if (!byOp.has(o)) byOp.set(o, {});
    byOp.get(o)[pname] = val;
  }
  if (!byOp.size) byOp.set('exposure', { ev: 0 });   // anchor so the mask survives a save/reload
  const spec = clone(g.spec); spec.name = g.name || spec.name;
  const added = [];
  for (const [o, params] of byOp) {
    added.push({ op: o, params, enabled: g.enabled !== false, mask: clone(spec), blend: 'normal', opacity: g.opacity == null ? 1 : g.opacity, v: (opSchema(o) || {}).version || 1, id: idFor(g.mid, o), label: g.name || 'Local adjustment' });
  }
  stack.length = 0; stack.push(...keep, ...added);
  return stack;
}
export function removeMaskGroup(stack, mid) {
  const prefix = `mask:${mid}:`;
  const keep = stack.filter((e) => !(e.id || '').startsWith(prefix));
  stack.length = 0; stack.push(...keep);
}
/** UI mask components [{mode:'add'|'subtract'|'intersect', spec}] <-> one nested combine spec (flat spec when a single component) */
export function compose(components) {
  let acc = null;
  for (const c of components) {
    if (!acc) { acc = clone(c.spec); continue; }
    const mode = c.mode === 'subtract' ? 'subtract' : c.mode === 'intersect' ? 'intersect' : 'union';
    acc = { type: 'combine', mode, masks: [acc, clone(c.spec)] };
  }
  return acc;
}
