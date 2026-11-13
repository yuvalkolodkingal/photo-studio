import { h } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import { slider } from '../ui/slider.js';
import { wheel } from '../ui/wheel.js';
import { Syncs, bound, opsSection } from './common.js';

const WH = [['shadows', 'Shadows'], ['midtones', 'Midtones'], ['highlights', 'Highlights'], ['global', 'Global']];

export function build() {
  const syncs = new Syncs();
  const { sec, refresh } = opsSection({ id: 'grading', title: 'Colour Grading', ops: ['color_balance'] });
  const grid = h('div.wheels');
  for (const [key, title] of WH) {
    const val = () => S.get(session.stack, 'color_balance', key, [0, 0, 0]);
    const write = (v, live) => session.edit(`Colour grading ${title.toLowerCase()}`, (st) => S.setParams(st, 'color_balance', { [key]: v }), { live });
    const w = wheel({ onInput: (v) => { const c = val(); write([v[0], v[1], c[2] || 0], true); }, onCommit: (v) => { const c = val(); write([v[0], v[1], c[2] || 0], false); } });
    const lum = slider({
      label: 'Lum', min: -100, max: 100, step: 1, digits: 0, value: val()[2] || 0, def: 0,
      onInput: (l) => { const c = val(); write([c[0] || 0, c[1] || 0, l], true); }, onCommit: (l) => { const c = val(); write([c[0] || 0, c[1] || 0, l], false); },
    });
    lum.el.style.gridTemplateColumns = '30px 1fr 40px';
    grid.append(h('div.wheel-cell', {}, h('div.wt', { text: title }), w.el, lum.el));
    syncs.add((st) => { const v = S.get(st, 'color_balance', key, [0, 0, 0]); w.set(v); lum.set(v[2] || 0); });
  }
  sec.body.append(grid,
    bound(syncs, 'color_balance', 'mask_falloff', { label: 'Blending', history: 'Colour grading blending', title: 'How softly the shadow / mid / highlight zones overlap (op: mask_falloff)' }),
    h('div.sub', { text: 'Global colour' }),
    bound(syncs, 'color_balance', 'hue_shift', { label: 'Hue shift', cls: 'grad-hue', history: 'Colour grading' }),
    bound(syncs, 'color_balance', 'chroma', { label: 'Chroma', history: 'Colour grading' }),
    bound(syncs, 'color_balance', 'saturation', { label: 'Saturation', history: 'Colour grading' }),
    bound(syncs, 'color_balance', 'brilliance', { label: 'Brilliance', history: 'Colour grading' }),
    h('div.subtle', { text: 'Wheel: hue (angle) and chroma (distance). Double-click a wheel to clear it.' }));
  syncs.add((st) => refresh(st));
  return { el: sec.el, sync: () => syncs.run() };
}
