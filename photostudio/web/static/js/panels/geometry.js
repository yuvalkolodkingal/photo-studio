import { h } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import { bus } from '../store.js';
import { ASPECTS } from '../tools/crop.js';
import { Syncs, bound, opsSection } from './common.js';

const GEO = ['crop', 'rotate', 'auto_level', 'perspective'];

export function build({ tools }) {
  const syncs = new Syncs();
  const { sec, refresh } = opsSection({ id: 'geometry', title: 'Geometry', ops: GEO, help: 'Crop, straighten, auto-level and perspective' });
  const cropBtn = h('button', { text: 'Crop (C)', title: 'Crop overlay', onclick: () => tools.crop() });
  const levelBtn = h('button', { text: 'Level', title: 'Drag a line along the horizon to straighten', onclick: () => tools.level() });
  const resetCrop = h('button', { text: 'Reset crop', onclick: () => session.edit('Reset crop', (st) => S.resetOps(st, ['crop'], session.defaultOps)) });
  const aspects = h('div.aspects', {}, ASPECTS.map(([k, t]) => h('button', { dataset: { a: k }, text: t, onclick: () => { tools.cropOn(); tools.cropTool.setAspect(k); paint(); } })));
  const flip = h('button.small', { text: 'Flip', title: 'Portrait / landscape (X)', onclick: () => { tools.cropOn(); tools.cropTool.toggleFlip(); } });
  const paint = () => { const cur = tools.cropTool.aspect; aspects.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.a === cur)); };
  const auto = h('select', {}, [['none', 'Off'], ['vertical', 'Vertical'], ['horizontal', 'Horizontal'], ['both', 'Full']].map(([v, t]) => h('option', { value: v, text: t })));
  auto.addEventListener('change', () => session.edit('Auto perspective', (st) => S.setParams(st, 'perspective', { auto: auto.value })));
  const autoLevel = h('button', { text: 'Auto level', title: 'Detect the horizon / verticals and straighten (auto_level op)', onclick: () => session.edit('Auto level', (st) => S.setParams(st, 'auto_level', { mode: 'auto' })) });
  const clearLevel = h('button.small', { text: 'Clear', onclick: () => session.edit('Clear auto level', (st) => S.resetOps(st, ['auto_level'], session.defaultOps)) });
  sec.body.append(
    h('div.row2', {}, cropBtn, levelBtn, h('span.spacer'), resetCrop),
    h('div.row2', {}, h('label', { text: 'Aspect' }), flip), aspects,
    bound(syncs, 'rotate', 'angle', { label: 'Straighten', history: 'Straighten', step: 0.05, digits: 2, def: 0 }),
    h('div.row2', {}, autoLevel, clearLevel),
    h('div.sub', { text: 'Perspective' }),
    bound(syncs, 'perspective', 'vertical', { label: 'Vertical', history: 'Perspective' }),
    bound(syncs, 'perspective', 'horizontal', { label: 'Horizontal', history: 'Perspective' }),
    bound(syncs, 'perspective', 'rotate', { label: 'Rotate', history: 'Perspective' }),
    h('div.row2', {}, h('label', { text: 'Auto' }), auto),
    h('div.subtle', { text: 'Crop applies after straighten and perspective. Auto perspective detects converging lines.' }));
  syncs.add((st) => {
    refresh(st); paint(); auto.value = S.get(st, 'perspective', 'auto', 'none');
    cropBtn.classList.toggle('on', tools.active === 'crop'); levelBtn.classList.toggle('on', tools.active === 'level');
  });
  bus.on('tool', () => syncs.run());
  return { el: sec.el, sync: () => syncs.run() };
}
