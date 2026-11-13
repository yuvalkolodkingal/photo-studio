import { h } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import { Syncs, bound, opsSection } from './common.js';
import { curveEditor } from '../ui/curve.js';

const CH = { rgb: ['points', 'RGB', '#e8e8e8'], red: ['red', 'R', '#ff6a6a'], green: ['green', 'G', '#6adf7a'], blue: ['blue', 'B', '#6a9aff'] };
const ID = [[0, 0], [1, 1]];

export function build() {
  const syncs = new Syncs();
  const { sec, refresh } = opsSection({ id: 'tonecurve', title: 'Tone Curve', ops: ['tone_curve'] });
  let ch = 'rgb';
  const tabs = h('div.seg.mini', {}, Object.entries(CH).map(([k, [, t]]) => h('button', { dataset: { ch: k }, text: t, class: k === ch ? 'active' : '', onclick: () => { ch = k; paintTabs(); load(); } })));
  const paintTabs = () => tabs.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.ch === ch));
  const write = (pts, live) => session.edit('Tone curve', (st) => {
    const key = CH[ch][0];
    const identity = pts.length === 2 && pts[0][1] === 0 && pts[1][1] === 1 && pts[0][0] === 0 && pts[1][0] === 1;
    S.setParams(st, 'tone_curve', { [key]: key === 'points' ? pts : (identity ? null : pts) });
  }, { live });
  const ed = curveEditor({ onInput: (p) => write(p, true), onCommit: (p) => write(p, false) });
  const load = () => { const key = CH[ch][0]; ed.set(S.getRaw(session.stack, 'tone_curve', key) || ID, CH[ch][2]); };
  const rst = h('button.small', { text: 'Reset curve', onclick: () => write(ID, false) });
  const mode = h('select', {}, ['rgb', 'luma', 'film', 'oklab'].map((m) => h('option', { value: m, text: m })));
  mode.addEventListener('change', () => session.edit('Tone curve mode', (st) => S.setParams(st, 'tone_curve', { mode: mode.value })));
  sec.body.append(h('div.row2', {}, tabs, h('span.spacer'), rst), ed.el,
    h('div.row2', {}, h('label', { text: 'Apply in' }), mode),
    bound(syncs, 'tone_curve', 'strength', { label: 'Strength', history: 'Tone curve' }),
    h('div.subtle', { text: 'Click to add a point, drag to move, double-click or right-click a point to remove it.' }));
  syncs.add((st) => { refresh(st); load(); mode.value = S.get(st, 'tone_curve', 'mode', 'rgb'); });
  return { el: sec.el, sync: () => syncs.run() };
}
