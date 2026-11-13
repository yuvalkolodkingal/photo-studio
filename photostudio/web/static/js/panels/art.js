import { h } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import * as SC from '../schema.js';
import * as api from '../api.js';
import { bus } from '../store.js';
import { toastError, busy } from '../toast.js';
import { section } from '../ui/section.js';
import { opFields } from './common.js';

const ART_CATS = new Set(['art', 'film', 'cinema', 'photographer', 'process', 'look']);
let artList = null;

/** Art mode picker + the auto-generated parameter controls of every look / art op currently in the stack. */
export function build() {
  const sec = section({ id: 'art', title: 'Art & Looks', toggle: false, open: false, help: 'Raster art, film, cinema and photographer looks',
    onReset: () => session.edit('Remove art look', (st) => { for (const e of st.slice()) { const o = SC.op(e.op); if (o && ART_CATS.has(o.category)) S.resetOps(st, [e.op], session.defaultOps); } }, {}) });
  const sel = h('select', { style: { width: '100%' } }, h('option', { value: '', text: '(none)' }));
  const box = h('div');
  sec.body.append(h('div.row2', {}, h('label', { text: 'Art look' })), sel, box,
    h('div.subtle', { text: 'Pick a look here or from the Presets panel. The parameters of each look op are listed below.' }));
  sel.addEventListener('change', async () => {
    try {
      if (!sel.value) { if (session.art) { const r = await busy(api.presetRemove({ kind: 'art', name: session.art, stack: session.stack })); session.setStack(r.stack, 'Remove art look', { art: null }); } return; }
      const r = await busy(api.presetApply({ p: session.p, kind: 'art', name: sel.value, stack: session.stack }));
      session.setStack(r.stack, `Art: ${sel.value}`, { art: r.set_art ? r.art : undefined });
    } catch (e) { toastError(e); }
  });
  let sig = '', fields = [];
  async function ensureList() {
    if (artList) return;
    try { artList = (await api.presets('art')).presets; } catch (_) { artList = []; }
    sel.append(...artList.map((p) => h('option', { value: p.name, text: p.title || p.name })));
    sel.value = session.art || '';
  }
  function sync(st) {
    const ops = [];
    for (const e of st) { const o = SC.op(e.op); if (o && ART_CATS.has(o.category) && !e.mask && !e.id && !ops.includes(e.op)) ops.push(e.op); }
    sec.setModified(ops.length > 0);
    if (sec.isOpen()) ensureList().then(() => { sel.value = session.art || ''; });
    const s = ops.join(',');
    if (s !== sig) {
      sig = s; fields = []; box.replaceChildren();
      for (const op of ops) {
        const f = opFields(op);
        fields.push(f);
        box.append(h('div.sub', {}, SC.opLabel(op), ' ', h('button.small.ghost', { text: 'remove', onclick: () => session.edit(`Remove ${SC.opLabel(op)}`, (stk) => S.resetOps(stk, [op], session.defaultOps)) })), f.el);
      }
    }
    fields.forEach((f) => f.sync());
  }
  sec.el.querySelector('.sh').addEventListener('click', () => setTimeout(() => sec.isOpen() && ensureList().then(() => { sel.value = session.art || ''; }), 0));
  bus.on('dev:loaded', () => { sig = ''; });
  return { el: sec.el, sync };
}
