import { h, hsl2rgb } from '../util.js';
import * as S from '../stack.js';
import { Syncs, bound, opsSection } from './common.js';

const BANDS = [['red', 0], ['orange', 30], ['yellow', 60], ['green', 120], ['aqua', 180], ['blue', 225], ['purple', 275], ['magenta', 320]];

export function build() {
  const syncs = new Syncs();
  const { sec, refresh } = opsSection({ id: 'hsl', title: 'HSL / Colour', ops: ['hsl_mixer'] });
  let band = 'red';
  const bar = h('div.bands', {}, BANDS.map(([n, hue]) => {
    const [r, g, b] = hsl2rgb(hue, 0.75, 0.5);
    return h('button', { title: n, dataset: { b: n }, style: { background: `rgb(${r | 0},${g | 0},${b | 0})` }, class: n === band ? 'active' : '', onclick: () => { band = n; paint(); rebuild(); } });
  }));
  const paint = () => bar.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.b === band));
  const box = h('div');
  const bandSyncs = new Syncs();
  function rebuild() {
    bandSyncs.fns = [];
    box.replaceChildren(
      bound(bandSyncs, 'hsl_mixer', `${band}_hue`, { label: 'Hue', cls: 'grad-hue', history: `HSL ${band} hue`, step: 1, digits: 0 }),
      bound(bandSyncs, 'hsl_mixer', `${band}_sat`, { label: 'Saturation', cls: 'grad-sat', history: `HSL ${band} saturation`, step: 1, digits: 0 }),
      bound(bandSyncs, 'hsl_mixer', `${band}_lum`, { label: 'Luminance', cls: 'grad-lum', history: `HSL ${band} luminance`, step: 1, digits: 0 }));
    bandSyncs.run();
  }
  const dots = () => bar.querySelectorAll('button').forEach((b) => {
    const any = ['hue', 'sat', 'lum'].some((k) => Math.abs(S.get(sessionStack(), 'hsl_mixer', `${b.dataset.b}_${k}`, 0)) > 0);
    b.textContent = any ? '•' : '';
  });
  let stackRef = [];
  const sessionStack = () => stackRef;
  sec.body.append(bar, box, h('div.subtle', { text: 'Eight Oklch bands, each +/-100 hue / saturation / luminance. A dot marks bands you changed.' }));
  rebuild();
  syncs.add((st) => { stackRef = st; refresh(st); bandSyncs.run(); dots(); });
  return { el: sec.el, sync: () => syncs.run() };
}
