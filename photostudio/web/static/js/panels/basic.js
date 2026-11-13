import { h } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import * as api from '../api.js';
import { toast, toastError } from '../toast.js';
import { Syncs, bound, opsSection } from './common.js';

const OPS = ['white_balance', 'exposure', 'contrast', 'highlights_shadows', 'local_contrast', 'dehaze', 'vibrance', 'saturation'];

export function build({ tools }) {
  const syncs = new Syncs();
  const { sec, refresh } = opsSection({ id: 'basic', title: 'Basic', ops: OPS, open: true });
  const b = sec.body;
  // White balance: the sliders edit white_balance.temperature / tint (6504 K = no change; higher = warmer image).
  const eye = h('button.eyedrop', { title: 'White balance picker: click a neutral grey area (W)', text: 'Pick WB', onclick: () => tools.wbPicker() });
  const auto = h('button', { title: 'Automatic white balance (grey-world)', text: 'Auto', onclick: () => session.edit('Auto white balance', (st) => S.setParams(st, 'white_balance', { auto: 'grayworld', picker: null })) });
  const asShot = h('button', { title: 'Back to as shot', text: 'As shot', onclick: () => session.edit('Reset white balance', (st) => S.resetOps(st, ['white_balance'], session.defaultOps)) });
  const wbNote = h('div.subtle');
  b.append(h('div.row2', {}, h('span.sub', { style: { margin: 0 }, text: 'White balance' }), h('span.spacer'), eye, auto, asShot), wbNote);
  const clearAuto = () => ({ auto: null, picker: null });
  b.append(bound(syncs, 'white_balance', 'temperature', { label: 'Temp', cls: 'grad-temp', patch: clearAuto, history: 'White balance', title: 'Assumed scene temperature in Kelvin: 6504 = unchanged, higher makes the picture warmer (double-click to reset)' }));
  b.append(bound(syncs, 'white_balance', 'tint', { label: 'Tint', cls: 'grad-tint', patch: clearAuto, history: 'White balance' }));
  b.append(h('div.sub', { text: 'Tone' }));
  b.append(bound(syncs, 'exposure', 'ev', { label: 'Exposure', history: 'Exposure' }));
  b.append(bound(syncs, 'contrast', 'contrast', { label: 'Contrast', history: 'Contrast' }));
  b.append(bound(syncs, 'highlights_shadows', 'highlights', { label: 'Highlights', history: 'Highlights' }));
  b.append(bound(syncs, 'highlights_shadows', 'shadows', { label: 'Shadows', history: 'Shadows' }));
  b.append(bound(syncs, 'highlights_shadows', 'whites', { label: 'Whites', history: 'Whites' }));
  b.append(bound(syncs, 'highlights_shadows', 'blacks', { label: 'Blacks', history: 'Blacks' }));
  b.append(h('div.sub', { text: 'Presence' }));
  b.append(bound(syncs, 'local_contrast', 'texture', { label: 'Texture', history: 'Texture' }));
  b.append(bound(syncs, 'local_contrast', 'clarity', { label: 'Clarity', history: 'Clarity' }));
  b.append(bound(syncs, 'dehaze', 'strength', { label: 'Dehaze', history: 'Dehaze' }));
  b.append(bound(syncs, 'vibrance', 'amount', { label: 'Vibrance', history: 'Vibrance' }));
  b.append(bound(syncs, 'saturation', 'amount', { label: 'Saturation', history: 'Saturation' }));
  syncs.add((st) => {
    refresh(st);
    const e = S.findEntry(st, 'white_balance');
    wbNote.textContent = e && e.params && e.params.auto ? `Auto white balance: ${e.params.auto}` : e && e.params && e.params.picker ? 'White balance from picker' : '';
  });
  return { el: sec.el, sync: () => syncs.run() };
}
