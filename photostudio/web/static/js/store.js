// Global state and a tiny event bus. Views and panels talk through this, never through each other.
const handlers = new Map();
export const bus = {
  on(evt, fn) { if (!handlers.has(evt)) handlers.set(evt, new Set()); handlers.get(evt).add(fn); return () => handlers.get(evt).delete(fn); },
  emit(evt, ...a) { for (const fn of Array.from(handlers.get(evt) || [])) { try { fn(...a); } catch (e) { console.error(`handler for ${evt}`, e); } } },
};

export const state = {
  info: null,
  view: 'library',
  // library
  items: [],            // current listing (array of catalog rows with .p)
  itemsByPath: new Map(),
  selection: new Set(), // selected paths
  cursor: null,         // keyboard cursor / "active" path
  filters: { folder: '', recursive: false, rating_min: 0, flag: '', label: '', tag: '', q: '', edited: false, album: '', sort: 'taken_at', order: 'asc', issue: '' },
  cell: Number(localStorage.getItem('ps.cell') || 180),
  libMode: 'grid',      // grid | compare | survey | cull
  cull: new Map(),      // path -> cull result
  // develop
  path: null,           // path open in Develop
  clipboard: null,      // copied settings (list of ops)
};

export const LABELS = ['red', 'yellow', 'green', 'blue', 'purple'];
export const LABEL_KEYS = { 6: 'red', 7: 'yellow', 8: 'green', 9: 'blue' };

const normItem = (it) => { if (it && it.label) it.label = String(it.label).toLowerCase(); return it; };
export function setItems(items) {
  items.forEach(normItem);
  state.items = items;
  state.itemsByPath = new Map(items.map((i) => [i.p, i]));
}
export function patchItem(p, patch) {
  const it = state.itemsByPath.get(p);
  if (it) { Object.assign(it, patch); normItem(it); }
  return it;
}
export const pref = {
  get(k, d) { try { const v = localStorage.getItem('ps.' + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem('ps.' + k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
};
