// The op schema served by /api/schema: labels, ranges, defaults. Panels are built from it, never from hard-coded numbers.
import * as api from './api.js';

let SCH = null;
export async function loadSchema() { SCH = await api.schema(); return SCH; }
export const schema = () => SCH;
export const op = (name) => (SCH && SCH.ops[name]) || null;
export const param = (opName, pname) => { const o = op(opName); return o ? o.params.find((p) => p.name === pname) || null : null; };
export function defaultOf(opName, pname) {
  const p = param(opName, pname);
  if (p) return p.default;
  const o = op(opName);
  return o && o.defaults ? o.defaults[pname] : undefined;
}
/** display multiplier (e.g. contrast stored 0.25 shows as 25) */
export const scaleOf = (opName, pname) => { const p = param(opName, pname); return (p && p.scale) || 1; };
export function opLabel(name) { const o = op(name); return o ? o.label : name; }
export function ops() { return SCH ? SCH.ops : {}; }
export function categories() { return SCH ? SCH.categories : {}; }
export function isGeometryOp(name) { const o = op(name); return !!(o && o.changes_geometry); }
