import { h } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import { Syncs, bound, opsSection } from './common.js';

export function build() {
  const syncs = new Syncs();
  const { sec, refresh } = opsSection({ id: 'detail', title: 'Detail', ops: ['sharpen', 'denoise', 'capture_sharpen'] });
  sec.body.append(
    h('div.sub', { text: 'Sharpening' }),
    bound(syncs, 'sharpen', 'amount', { label: 'Amount', history: 'Sharpen' }),
    bound(syncs, 'sharpen', 'radius', { label: 'Radius', history: 'Sharpen radius' }),
    bound(syncs, 'sharpen', 'masking', { label: 'Masking', history: 'Sharpen masking' }),
    bound(syncs, 'sharpen', 'halo', { label: 'Halo limit', history: 'Sharpen' }),
    h('div.sub', { text: 'Noise reduction' }),
    bound(syncs, 'denoise', 'luma', { label: 'Luminance', history: 'Denoise luminance' }),
    bound(syncs, 'denoise', 'chroma', { label: 'Colour', history: 'Denoise colour' }),
    bound(syncs, 'denoise', 'detail', { label: 'Detail', history: 'Denoise detail' }),
    h('div.row2', {}, h('label', { text: 'Method' }), (() => {
      const s = h('select', {}, ['wavelet', 'bilateral', 'nlm', 'bm3d'].map((m) => h('option', { value: m, text: m })));
      s.addEventListener('change', () => session.edit('Denoise method', (st) => S.setParams(st, 'denoise', { method: s.value })));
      syncs.add((st) => { s.value = S.get(st, 'denoise', 'method', 'wavelet'); });
      return s;
    })()),
    h('div.sub', { text: 'Capture sharpening (raw)' }),
    bound(syncs, 'capture_sharpen', 'amount', { label: 'Amount', history: 'Capture sharpen' }),
    h('div.subtle', { text: 'Denoise and capture sharpening act on raw files (ISO-adaptive by default). Sharpen runs after the tone curve on every file.' }));
  syncs.add((st) => refresh(st));
  return { el: sec.el, sync: () => syncs.run() };
}
