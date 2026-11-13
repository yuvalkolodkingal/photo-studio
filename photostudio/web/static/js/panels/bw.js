import { h, clamp } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import * as SC from '../schema.js';
import { slider } from '../ui/slider.js';
import { Syncs, bound, opsSection } from './common.js';

const NEUTRAL = [0.2126, 0.7152, 0.0722];

export function build() {
  const syncs = new Syncs();
  const { sec, refresh } = opsSection({ id: 'bw', title: 'Black & White', ops: ['monochrome'], absentMeansOff: true });
  const sel = h('select', {}, (SC.param('monochrome', 'filter') ? SC.param('monochrome', 'filter').choices : ['neutral']).map((c) => h('option', { value: c, text: c })));
  sel.addEventListener('change', () => session.edit('B&W filter', (st) => S.setParams(st, 'monochrome', { filter: sel.value, weights: null })));
  sec.body.append(h('div.row2', {}, h('label', { text: 'Filter' }), sel),
    bound(syncs, 'monochrome', 'filter_strength', { label: 'Filter strength', history: 'B&W filter strength' }),
    bound(syncs, 'monochrome', 'amount', { label: 'Amount', history: 'B&W amount' }),
    h('div.sub', { text: 'Channel mix (overrides the filter)' }));
  ['Red', 'Green', 'Blue'].forEach((name, i) => {
    const cur = (st) => { const w = S.getRaw(st, 'monochrome', 'weights'); return w ? w[i] : NEUTRAL[i]; };
    const write = (v, live) => session.edit('B&W channel mix', (st) => {
      const w = (S.getRaw(st, 'monochrome', 'weights') || NEUTRAL).slice(); w[i] = v; S.setParams(st, 'monochrome', { weights: w });
    }, { live });
    const sl = slider({ label: name, min: -1, max: 2, step: 0.01, value: cur(session.stack), def: NEUTRAL[i], scale: 100, digits: 0, onInput: (v) => write(v, true), onCommit: (v) => write(v, false) });
    sec.body.append(sl.el); syncs.add((st) => sl.set(cur(st)));
  });
  sec.body.append(h('div.row2', {}, h('button.small', { text: 'Reset mix', onclick: () => session.edit('B&W channel mix', (st) => S.setParams(st, 'monochrome', { weights: null, filter: 'neutral' })) })),
    h('div.subtle', { text: 'A 3-channel mixer plus named filters. It has no Lightroom-style 8-colour B&W sliders; use HSL luminance before converting for that.' }));
  syncs.add((st) => { refresh(st); sel.value = S.get(st, 'monochrome', 'filter', 'neutral'); });
  return { el: sec.el, sync: () => syncs.run() };
}
