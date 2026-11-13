// Rating / flag / label / tag actions shared by Library and Develop. They write the same sidecar the CLI reads.
import * as api from './api.js';
import { state, patchItem, bus } from './store.js';
import { toastError } from './toast.js';

async function apply(paths, fields) {
  if (!paths.length) return;
  const before = paths.map((p) => [p, state.itemsByPath.get(p) && { ...state.itemsByPath.get(p) }]);
  for (const p of paths) patchItem(p, fields.add_tags || fields.remove_tags ? {} : { ...fields });
  bus.emit('lib:items', paths);
  try {
    const r = await api.setMeta(paths, fields);
    for (const u of r.updated) patchItem(u.p, { rating: u.rating, flag: u.flag, label: u.label, tags: u.tags });
    bus.emit('lib:items', paths);
    return r;
  } catch (e) {
    for (const [p, old] of before) if (old) patchItem(p, old);
    bus.emit('lib:items', paths); toastError(e);
  }
}
export const rate = (paths, n) => apply(paths, { rating: n });
export const flag = (paths, f) => apply(paths, { flag: f });
export const label = (paths, l) => apply(paths, { label: l });
export const addTags = (paths, tags) => apply(paths, { add_tags: tags });
export const removeTags = (paths, tags) => apply(paths, { remove_tags: tags });

/** Common key handling for ratings, flags and labels. Returns true if handled. */
export function metaKey(e, paths) {
  if (e.ctrlKey || e.metaKey || e.altKey || !paths.length) return false;
  const k = e.key;
  const cur = state.itemsByPath.get(paths[0]);
  if (/^[0-5]$/.test(k)) { rate(paths, Number(k)); return true; }
  if (k === 'p' || k === 'P') { flag(paths, cur && cur.flag === 'pick' ? null : 'pick'); return true; }
  if (k === 'x' || k === 'X') { flag(paths, cur && cur.flag === 'reject' ? null : 'reject'); return true; }
  if (k === 'u' || k === 'U') { flag(paths, null); return true; }
  const lab = { 6: 'red', 7: 'yellow', 8: 'green', 9: 'blue' }[k];
  if (lab) { label(paths, cur && cur.label === lab ? null : lab); return true; }
  if (k === '-') { rate(paths, Math.max(0, ((cur && cur.rating) || 0) - 1)); return true; }
  if (k === '=') { rate(paths, Math.min(5, ((cur && cur.rating) || 0) + 1)); return true; }
  return false;
}
