// Small DOM + math helpers shared by every module.
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** h('div.cls#id', {attrs, onclick}, ...children) -> HTMLElement. Strings become text nodes. */
export function h(tag, attrs, ...kids) {
  const m = /^([a-z0-9]+)((?:[.#][\w-]+)*)$/i.exec(tag) || [null, tag, ''];
  const el = document.createElement(m[1]);
  for (const part of (m[2].match(/[.#][\w-]+/g) || [])) {
    if (part[0] === '.') el.classList.add(part.slice(1)); else el.id = part.slice(1);
  }
  if (attrs && (typeof attrs !== 'object' || attrs instanceof Node || Array.isArray(attrs))) { kids.unshift(attrs); attrs = null; }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className += (el.className ? ' ' : '') + v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected') el[k] = !!v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'text') el.textContent = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function mount(el, ...kids) { clear(el); add(el, kids); return el; }

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const lerp = (a, b, t) => a + (b - a) * t;
export const clone = (o) => (o === undefined ? undefined : JSON.parse(JSON.stringify(o)));
export const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export const uid = (n = 6) => Math.random().toString(36).slice(2, 2 + n);

export function debounce(fn, ms) {
  let t = null;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => { t = null; fn(...a); }, ms); };
  d.cancel = () => { clearTimeout(t); t = null; };
  d.flush = (...a) => { if (t != null) { clearTimeout(t); t = null; fn(...a); } };
  d.pending = () => t != null;
  return d;
}
export function throttleRaf(fn) {
  let queued = false, last;
  return (...a) => { last = a; if (queued) return; queued = true; requestAnimationFrame(() => { queued = false; fn(...last); }); };
}

export function fmtNum(v, digits = 2) {
  if (v == null || Number.isNaN(v)) return '';
  const s = Number(v).toFixed(digits);
  return digits > 0 ? s.replace(/\.?0+$/, '') || '0' : s;
}
export function fmtBytes(n) {
  if (n == null) return '';
  const u = ['B', 'KB', 'MB', 'GB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
export function fmtShutter(s) {
  if (!s) return '';
  return s >= 1 ? `${s}s` : `1/${Math.round(1 / s)}s`;
}
export function titleCase(s) { return String(s).replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()); }
export function isTyping(e) {
  const t = e.target;
  return t && (t.tagName === 'INPUT' && !['range', 'checkbox', 'button'].includes(t.type) || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
}
export function hex(r, g, b) { return '#' + [r, g, b].map((v) => Math.round(clamp(v, 0, 255)).toString(16).padStart(2, '0')).join(''); }
/** hsv/hsl helpers for the colour wheels and swatches (h in degrees, s/l/v 0..1) */
export function hsl2rgb(h, s, l) {
  h = ((h % 360) + 360) % 360;
  const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2;
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}
export function download(url, name) {
  const a = h('a', { href: url, download: name || '' });
  document.body.append(a); a.click(); a.remove();
}
export function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
