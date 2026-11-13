import { h } from '../util.js';
import * as S from '../stack.js';
import { session } from '../session.js';
import { slider } from '../ui/slider.js';
import { Syncs, bound, opsSection } from './common.js';

export function build() {
  const syncs = new Syncs();
  const { sec, refresh } = opsSection({ id: 'effects', title: 'Effects', ops: ['vignette', 'grain', 'halation', 'split_tone'] });
  const hueSlider = (key, label) => {
    const w = (v) => (st) => S.setParams(st, 'split_tone', { [key]: v });
    const sl = slider({ label, min: 0, max: 360, step: 1, digits: 0, value: S.get(session.stack, 'split_tone', key, 0), def: key.startsWith('shadow') ? 220 : 40, cls: 'grad-hue', onInput: (v) => session.edit('Split toning', w(v), { live: true }), onCommit: (v) => session.edit('Split toning', w(v)) });
    syncs.add((st) => sl.set(S.get(st, 'split_tone', key, 0)));
    return sl.el;
  };
  sec.body.append(
    h('div.sub', { text: 'Vignette' }),
    bound(syncs, 'vignette', 'amount', { label: 'Amount', history: 'Vignette', def: 0, absent: 0 }),
    bound(syncs, 'vignette', 'midpoint', { seed: { amount: 0 }, label: 'Midpoint', history: 'Vignette' }),
    bound(syncs, 'vignette', 'roundness', { seed: { amount: 0 }, label: 'Roundness', history: 'Vignette' }),
    bound(syncs, 'vignette', 'feather', { seed: { amount: 0 }, label: 'Feather', history: 'Vignette' }),
    bound(syncs, 'vignette', 'highlights', { seed: { amount: 0 }, label: 'Highlights', history: 'Vignette' }),
    h('div.sub', { text: 'Grain' }),
    bound(syncs, 'grain', 'amount', { label: 'Amount', history: 'Grain', def: 0, absent: 0 }),
    bound(syncs, 'grain', 'size', { seed: { amount: 0 }, label: 'Size', history: 'Grain' }),
    bound(syncs, 'grain', 'roughness', { seed: { amount: 0 }, label: 'Roughness', history: 'Grain' }),
    h('div.sub', { text: 'Halation' }),
    bound(syncs, 'halation', 'strength', { label: 'Strength', history: 'Halation', def: 0, absent: 0 }),
    bound(syncs, 'halation', 'radius', { seed: { strength: 0 }, label: 'Radius', history: 'Halation' }),
    bound(syncs, 'halation', 'threshold', { seed: { strength: 0 }, label: 'Threshold', history: 'Halation' }),
    h('div.sub', { text: 'Split toning' }),
    hueSlider('highlight_hue', 'Highlight hue'),
    bound(syncs, 'split_tone', 'highlight_sat', { label: 'Highlight sat', history: 'Split toning' }),
    hueSlider('shadow_hue', 'Shadow hue'),
    bound(syncs, 'split_tone', 'shadow_sat', { label: 'Shadow sat', history: 'Split toning' }),
    bound(syncs, 'split_tone', 'balance', { label: 'Balance', history: 'Split toning' }),
    h('div.subtle', { text: 'Vignette, grain, halation and split toning are separate ops; the toggle enables all four.' }));
  syncs.add((st) => refresh(st));
  return { el: sec.el, sync: () => syncs.run() };
}
