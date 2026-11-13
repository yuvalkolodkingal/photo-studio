import { h, clone } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import { bus } from '../store.js';
import { slider } from '../ui/slider.js';
import { section } from '../ui/section.js';
import { healSettings } from '../tools/heal.js';

export function build({ tools }) {
  const sec = section({ id: 'retouch', title: 'Retouch', toggle: false, open: false, onReset: () => session.edit('Clear retouch', (st) => S.resetOps(st, ['heal'], session.defaultOps)) });
  const btn = h('button', { text: 'Spot tool (Q)', title: 'Click spots to heal or clone', onclick: () => tools.heal() });
  const mode = h('select', {}, [['heal', 'Heal'], ['clone', 'Clone']].map(([v, t]) => h('option', { value: v, text: t })));
  mode.addEventListener('change', () => { healSettings.mode = mode.value; });
  const sz = slider({ label: 'Size', min: 2, max: 150, step: 1, digits: 0, value: healSettings.radius, def: 24, onInput: (v) => { healSettings.radius = v; tools.viewer.drawOverlay(); }, onCommit: (v) => { healSettings.radius = v; } });
  const fe = slider({ label: 'Feather', min: 0, max: 1, step: 0.01, scale: 100, digits: 0, value: healSettings.feather, def: 0.35, onInput: (v) => { healSettings.feather = v; }, onCommit: (v) => { healSettings.feather = v; } });
  const op = slider({ label: 'Opacity', min: 0, max: 1, step: 0.01, scale: 100, digits: 0, value: healSettings.opacity, def: 1, onInput: (v) => { healSettings.opacity = v; }, onCommit: (v) => { healSettings.opacity = v; } });
  const list = h('div');
  sec.body.append(h('div.row2', {}, btn, mode), sz.el, fe.el, op.el, list,
    h('div.subtle', { text: 'Click a blemish to heal it; the source is chosen automatically. Drag from the spot to pick the source yourself. Select a spot and press Delete to remove it.' }));
  function sync(st) {
    const e = S.findEntry(st, 'heal'); const spots = (e && e.params && e.params.spots) || [];
    sec.setModified(spots.length > 0);
    btn.classList.toggle('on', tools.active === 'heal');
    list.replaceChildren(...spots.map((s, i) => h('div.layer', { class: tools.healTool.sel === i ? 'active' : '', onclick: () => { tools.healTool.sel = i; tools.viewer.drawOverlay(); sync(session.stack); } },
      h('span.nm', { text: `${s.mode || 'heal'} ${i + 1}` }),
      h('button.small.ghost', { text: 'x', onclick: (ev) => { ev.stopPropagation(); const next = clone(spots); next.splice(i, 1); session.edit('Remove retouch spot', (stk) => { if (!next.length) S.resetOps(stk, ['heal'], session.defaultOps); else S.setParams(stk, 'heal', { spots: next }); }); } }))));
  }
  bus.on('tool', () => sync(session.stack));
  return { el: sec.el, sync };
}
