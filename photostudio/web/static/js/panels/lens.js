import { h } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import { Syncs, bound, opsSection } from './common.js';

export function build() {
  const syncs = new Syncs();
  const { sec, refresh } = opsSection({ id: 'lens', title: 'Lens Corrections', ops: ['lens_correct', 'ca_correct'], toggleCreates: true });
  const cb = (op, name, label, def) => {
    const c = h('input', { type: 'checkbox' });
    c.addEventListener('change', () => session.edit(label, (st) => S.setParams(st, op, { [name]: c.checked })));
    syncs.add((st) => { c.checked = !!S.get(st, op, name, def); });
    return h('label', { style: { display: 'flex', gap: '4px', alignItems: 'center' } }, c, label);
  };
  sec.body.append(
    h('div.row2', {}, cb('lens_correct', 'distortion', 'Distortion', true), cb('lens_correct', 'vignetting', 'Vignetting', true), cb('lens_correct', 'tca', 'Chromatic aberration', true)),
    bound(syncs, 'lens_correct', 'scale', { label: 'Scale', history: 'Lens scale' }),
    bound(syncs, 'lens_correct', 'k1', { label: 'Distortion k1', history: 'Lens distortion' }),
    bound(syncs, 'lens_correct', 'k2', { label: 'Distortion k2', history: 'Lens distortion' }),
    bound(syncs, 'lens_correct', 'vignette', { label: 'Vignette fix', history: 'Lens vignette' }),
    h('div.sub', { text: 'Fringe removal' }),
    bound(syncs, 'ca_correct', 'red', { label: 'Red/Cyan', history: 'CA correction' }),
    bound(syncs, 'ca_correct', 'blue', { label: 'Blue/Yellow', history: 'CA correction' }),
    h('div.subtle', { text: 'Profile based: the camera and lens come from EXIF. k1 / k2 / vignette are manual overrides.' }));
  syncs.add((st) => refresh(st));
  return { el: sec.el, sync: () => syncs.run() };
}
