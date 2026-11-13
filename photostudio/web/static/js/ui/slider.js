import { h, clamp, fmtNum } from '../util.js';

/**
 * Lightroom-style slider: label (double-click resets), range input, numeric field.
 * Values handed to callbacks are in PARAM units; `scale` is only a display multiplier.
 *   onInput(value)  while dragging (live preview)       onCommit(value)  on release / numeric entry / reset
 * Returns {el, set(v), get(), setRange(min,max), setModified(bool)}.
 */
export function slider({ label, min, max, step = 0.01, value, def = 0, digits, unit = '', scale = 1, cls = '', title = '', onInput, onCommit, wide = false }) {
  const dmin = min * scale, dmax = max * scale, dstep = step * scale;
  if (digits == null) digits = Math.max(0, Math.min(4, Math.ceil(-Math.log10(dstep || 1)) + (dstep < 1 ? 0 : 0)));
  const toDisp = (v) => (v == null ? def * scale : v * scale);
  const fromDisp = (d) => d / scale;
  let cur = value == null ? def : value;

  const lbl = h('span.lbl', { title: title || `${label}  (double-click to reset)`, text: label });
  const range = h('input', { type: 'range', min: dmin, max: dmax, step: dstep, value: toDisp(cur) });
  const num = h('input', { type: 'number', min: dmin, max: dmax, step: dstep, value: fmtNum(toDisp(cur), digits) });
  const el = h('div.sl', { class: `${cls} ${wide ? 'wide' : ''}` }, lbl, range, num);
  if (unit) num.title = unit;

  const modified = () => Math.abs(cur - def) > 1e-9;
  const paint = () => { el.classList.toggle('modified', modified()); };
  const roundTo = (d) => { const s = Math.pow(10, digits + 1); return Math.round(d * s) / s; };
  function setCur(v) { cur = clamp(v, min, max); range.value = toDisp(cur); num.value = fmtNum(toDisp(cur), digits); paint(); }

  range.addEventListener('input', () => { cur = clamp(fromDisp(roundTo(Number(range.value))), min, max); num.value = fmtNum(toDisp(cur), digits); paint(); if (onInput) onInput(cur); });
  range.addEventListener('change', () => { if (onCommit) onCommit(cur); });
  num.addEventListener('change', () => {
    const d = Number(num.value);
    if (Number.isNaN(d)) { num.value = fmtNum(toDisp(cur), digits); return; }
    setCur(fromDisp(d)); if (onInput) onInput(cur); if (onCommit) onCommit(cur);
  });
  num.addEventListener('keydown', (e) => { if (e.key === 'Enter') num.blur(); e.stopPropagation(); });
  const reset = () => { setCur(def); if (onInput) onInput(cur); if (onCommit) onCommit(cur); };
  lbl.addEventListener('dblclick', reset);
  range.addEventListener('dblclick', (e) => { if (e.altKey) reset(); });
  paint();
  return {
    el, set: (v) => { if (document.activeElement !== num) setCur(v == null ? def : v); }, get: () => cur,
    setRange(a, b) { range.min = a * scale; range.max = b * scale; num.min = a * scale; num.max = b * scale; },
    reset,
  };
}
