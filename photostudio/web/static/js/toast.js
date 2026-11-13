import { $, h } from './util.js';

export function toast(msg, kind = 'info', ms = 4500) {
  const root = $('#toasts');
  if (!root) return;
  if (kind === 'error') ms = Math.max(ms, 8000);
  const el = h('div.toast', { class: kind === 'info' ? '' : kind }, h('span.x', { onclick: () => el.remove(), text: 'x' }), String(msg));
  root.append(el);
  while (root.children.length > 5) root.firstChild.remove();
  if (ms > 0) setTimeout(() => el.remove(), ms);
  return el;
}
export const toastError = (e) => toast(e && e.message ? e.message : String(e), 'error');

let busyCount = 0;
/** Wrap a promise with the top bar busy indicator. */
export async function busy(promise) {
  const el = $('#busy');
  busyCount++; if (el) el.classList.remove('hidden');
  try { return await promise; } finally { busyCount--; if (!busyCount && el) el.classList.add('hidden'); }
}
export function busyAdd(n) {
  const el = $('#busy'); busyCount = Math.max(0, busyCount + n);
  if (el) el.classList.toggle('hidden', busyCount === 0);
}
