import { h } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import * as SC from '../schema.js';
import { section } from '../ui/section.js';
import { opFields } from './common.js';

/** Every registered op, generated from /api/schema: anything the CLI can do is reachable here. */
export function build() {
  const sec = section({ id: 'allops', title: 'All Ops', toggle: false, open: false, help: 'Every engine op, auto-generated from the schema' });
  const q = h('input', { type: 'search', placeholder: 'Search ops (name, category, text)', style: { width: '100%' } });
  q.addEventListener('keydown', (e) => e.stopPropagation());
  const list = h('div.allops');
  sec.body.append(q, list);
  const rows = new Map();   // op -> {row, tgl, body, fields}
  let built = false;

  function build_() {
    built = true;
    const cats = SC.categories(), ops = Object.values(SC.ops());
    const byCat = {};
    for (const o of ops) (byCat[o.category] = byCat[o.category] || []).push(o);
    for (const [cat, title] of Object.entries(cats)) {
      const items = (byCat[cat] || []).sort((a, b) => a.stage - b.stage || a.label.localeCompare(b.label));
      if (!items.length) continue;
      const wrap = h('div.catwrap', { dataset: { cat } }, h('div.sub', { text: `${title} (${items.length})` }));
      for (const o of items) wrap.append(opRow(o));
      list.append(wrap);
    }
    sync(session.stack);
  }
  function opRow(o) {
    const tgl = h('input', { type: 'checkbox', title: 'Enable this op in the stack' });
    tgl.addEventListener('click', (e) => e.stopPropagation());
    tgl.addEventListener('change', () => session.edit(`${tgl.checked ? 'Enable' : 'Disable'} ${o.label}`, (st) => {
      const es = S.findAll(st, o.name || o.op);
      if (es.length) es.forEach((e) => { e.enabled = tgl.checked; }); else if (tgl.checked) S.ensure(st, o.op);
    }));
    const rm = h('button.small.ghost', { text: 'remove', title: 'Remove from the stack', onclick: (e) => { e.stopPropagation(); session.edit(`Remove ${o.label}`, (st) => S.resetOps(st, [o.op], session.defaultOps)); } });
    const body = h('div.ob.hidden');
    const head = h('div.oh', { title: o.doc || '', onclick: () => {
      const open = body.classList.contains('hidden');
      if (open && !rec.fields) { rec.fields = opFields(o.op); body.append(rec.fields.el); }
      body.classList.toggle('hidden', !open);
      if (open) rec.fields.sync();
    } }, tgl, h('span', { text: o.label }), h('span.spacer'), h('span.dim', { text: `st ${o.stage}` }), rm);
    const row = h('div.opitem', { dataset: { name: `${o.op} ${o.label} ${o.category} ${o.doc || ''}`.toLowerCase() } }, head, body);
    const rec = { row, tgl, body, fields: null, op: o.op };
    rows.set(o.op, rec);
    return row;
  }
  function sync(st) {
    if (!built) return;
    for (const [op, r] of rows) {
      const es = S.findAll(st, op);
      r.tgl.checked = es.some((e) => e.enabled !== false);
      r.row.classList.toggle('on', es.length > 0);
      if (r.fields && !r.body.classList.contains('hidden')) r.fields.sync();
    }
  }
  q.addEventListener('input', () => {
    const t = q.value.trim().toLowerCase();
    for (const r of rows.values()) r.row.classList.toggle('hidden', !!t && !r.row.dataset.name.includes(t));
    list.querySelectorAll('.catwrap').forEach((w) => w.classList.toggle('hidden', !w.querySelector('.opitem:not(.hidden)')));
  });
  sec.el.querySelector('.sh').addEventListener('click', () => setTimeout(() => { if (sec.isOpen() && !built) build_(); }, 0));
  if (sec.isOpen()) setTimeout(build_, 0);
  return { el: sec.el, sync };
}
