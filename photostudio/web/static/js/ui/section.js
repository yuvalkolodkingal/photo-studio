import { h } from '../util.js';
import { pref as prefs } from '../store.js';

/**
 * Collapsible panel section with an enable toggle, a modified dot and a reset button.
 * opts: {id, title, open, toggle: bool, onToggle(on), onReset(), onOpen()}
 */
export function section({ id, title, open = false, toggle = true, onToggle, onReset, onOpen, help }) {
  const isOpen = prefs.get('sec.' + id, open);
  const tgl = toggle ? h('span.tgl.on', { title: 'Enable / disable this section', onclick: (e) => { e.stopPropagation(); const on = !tgl.classList.contains('on'); setEnabled(on); if (onToggle) onToggle(on); } }) : null;
  const rst = onReset ? h('span.rst', { title: 'Reset this section', onclick: (e) => { e.stopPropagation(); onReset(); }, text: '↺' }) : null;
  const caret = h('span.caret', { text: isOpen ? '▾' : '▸' });
  const head = h('div.sh', { onclick: () => toggleOpen() }, caret, h('span.title', { text: title, title: help || '' }), h('span.mod', { title: 'Modified' }), rst, tgl);
  const body = h('div.sb');
  const el = h('div.sect', { class: isOpen ? '' : 'closed', dataset: { sec: id } }, head, body);
  function toggleOpen(force) {
    const o = force == null ? el.classList.contains('closed') : force;
    el.classList.toggle('closed', !o); caret.textContent = o ? '▾' : '▸';
    prefs.set('sec.' + id, o);
    if (o && onOpen) onOpen();
  }
  function setEnabled(on) { if (tgl) tgl.classList.toggle('on', !!on); el.classList.toggle('disabled', !on); }
  return {
    el, body, id, setEnabled, setModified: (m) => el.classList.toggle('modified', !!m), isOpen: () => !el.classList.contains('closed'), open: () => toggleOpen(true),
  };
}
