// Library right panel: rating / flag / colour label, hierarchical tag editor, EXIF, notes, culling info.
import { h, fmtBytes, fmtShutter, debounce } from '../util.js';
import * as api from '../api.js';
import { bus, state, LABELS } from '../store.js';
import * as M from '../meta.js';
import { toastError } from '../toast.js';
import { section } from '../ui/section.js';

export function stars(value, onPick, small) {
  const box = h('span.stars', { class: small ? 'small' : '' });
  for (let i = 1; i <= 5; i++) box.append(h('span', { class: i <= value ? 'on' : '', text: '★', title: `${i} star${i > 1 ? 's' : ''}`, onclick: (e) => { e.stopPropagation(); onPick(value === i ? 0 : i); } }));
  return box;
}

export function build({ open }) {
  const root = h('div');
  const head = h('div');
  const metaSec = section({ id: 'lib-meta', title: 'Rating & labels', toggle: false, open: true });
  const tagSec = section({ id: 'lib-tags', title: 'Tags', toggle: false, open: true });
  const exifSec = section({ id: 'lib-exif', title: 'Metadata', toggle: false, open: true });
  const cullSec = section({ id: 'lib-cull', title: 'Culling', toggle: false, open: false });
  const notesSec = section({ id: 'lib-notes', title: 'Notes', toggle: false, open: false });
  root.append(head, metaSec.el, tagSec.el, exifSec.el, cullSec.el, notesSec.el);
  let tagNames = [];
  const tagList = h('datalist', { id: 'taglist' });
  document.body.append(tagList);
  bus.on('lib:tags', (tags) => { tagNames = tags.map((t) => t.path); tagList.replaceChildren(...tagNames.map((t) => h('option', { value: t }))); });

  let token = 0;
  async function paint() {
    const paths = Array.from(state.selection);
    const p = state.cursor && (paths.length ? (paths.includes(state.cursor) ? state.cursor : paths[0]) : state.cursor);
    const my = ++token;
    if (!p) { head.replaceChildren(h('div.empty', { text: 'Select a photo' })); for (const s of [metaSec, tagSec, exifSec, cullSec, notesSec]) s.body.replaceChildren(); return; }
    const base = state.itemsByPath.get(p) || {};
    const targets = paths.length > 1 ? paths : [p];
    head.replaceChildren(h('div', { style: { padding: '8px 10px' } }, h('div.ellipsis', { style: { color: 'var(--bright)' }, text: base.name || p, title: p }),
      h('div.dim', { text: targets.length > 1 ? `${targets.length} photos selected` : p }),
      h('div.row2', {}, h('button.primary', { text: 'Open in Develop (D)', onclick: () => open(p) }))));
    metaSec.body.replaceChildren(
      h('div.row2', {}, h('label', { text: 'Rating' }), stars(base.rating || 0, (n) => M.rate(targets, n))),
      h('div.row2', {}, h('label', { text: 'Flag' }),
        h('span.chip', { class: base.flag === 'pick' ? 'on' : '', text: 'Pick (P)', onclick: () => M.flag(targets, base.flag === 'pick' ? null : 'pick') }),
        h('span.chip', { class: base.flag === 'reject' ? 'on' : '', text: 'Reject (X)', onclick: () => M.flag(targets, base.flag === 'reject' ? null : 'reject') }),
        h('span.chip', { text: 'Clear (U)', onclick: () => M.flag(targets, null) })),
      h('div.row2', {}, h('label', { text: 'Label' }),
        ...LABELS.map((l, i) => h('span.dot', { class: `${l} ${base.label === l ? '' : ''}`, title: `${l} (${i + 6 <= 9 ? i + 6 : ''})`, style: { cursor: 'pointer', outline: base.label === l ? '2px solid #fff' : '' }, onclick: () => M.label(targets, base.label === l ? null : l) })),
        h('span.dot.none', { title: 'No label', style: { cursor: 'pointer' }, onclick: () => M.label(targets, null) })));
    // tags
    const tags = base.tags || [];
    const inp = h('input', { type: 'text', list: 'taglist', placeholder: 'Add tag (use | for hierarchy, e.g. places|israel)', style: { width: '100%' } });
    inp.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter' && inp.value.trim()) { M.addTags(targets, inp.value.split(',').map((t) => t.trim()).filter(Boolean)).then(() => { inp.value = ''; bus.emit('lib:tagschanged'); }); } });
    tagSec.body.replaceChildren(h('div.taglist', {}, tags.map((t) => h('span.tag', {}, t, h('span.x', { text: 'x', onclick: () => M.removeTags(targets, [t]).then(() => bus.emit('lib:tagschanged')) })))), inp);
    // full item (EXIF, albums, analysis)
    let it = base;
    try { it = await api.item(p); } catch (e) { /* keep base */ }
    if (my !== token) return;
    const kv = (k, v) => (v == null || v === '' ? null : [h('div.k', { text: k }), h('div.v', { text: String(v) })]);
    exifSec.body.replaceChildren(h('div.kvgrid', {}, [
      kv('Date', it.taken_at && it.taken_at.replace('T', ' ')), kv('Camera', it.camera), kv('Lens', it.lens), kv('ISO', it.iso), kv('Shutter', fmtShutter(it.shutter)),
      kv('Aperture', it.aperture && `f/${it.aperture}`), kv('Focal', it.focal && `${it.focal} mm${it.focal35 ? ` (${it.focal35} mm eq.)` : ''}`),
      kv('Size', it.width && `${it.width} x ${it.height}`), kv('File', fmtBytes(it.file_size)), kv('Type', it.kind), kv('Mode', it.mode),
      kv('Albums', (it.albums || []).map((a) => a.name || a).join(', ')),
    ].flat().filter(Boolean)));
    const a = it.analysis || {};
    const cr = state.cull.get(p);
    cullSec.body.replaceChildren(
      ...(cr ? [h('div', { text: `Suggested: ${cr.flag} (score ${cr.score == null ? '-' : Number(cr.score).toFixed(2)})`, style: { color: cr.flag === 'pick' ? 'var(--pick)' : cr.flag === 'reject' ? 'var(--reject)' : 'var(--dim)' } }), h('div.subtle', { text: (cr.reasons || []).join('; ') })] : [h('div.subtle', { text: 'Run Cull from the toolbar to score sharpness, exposure, faces and bursts.' })]),
      ...(Object.keys(a).length ? [h('div.kvgrid', {}, Object.entries(a).filter(([, v]) => typeof v !== 'object').slice(0, 14).flatMap(([k, v]) => [h('div.k', { text: k }), h('div.v', { text: typeof v === 'number' ? Number(v).toFixed(3) : String(v) })]))] : []));
    const ta = h('textarea', { rows: 4, style: { width: '100%' }, value: it.notes || '', placeholder: 'Notes (also stored in the sidecar)' });
    ta.addEventListener('keydown', (e) => e.stopPropagation());
    ta.addEventListener('change', () => M.setNotes ? M.setNotes(targets, ta.value) : api.setMeta(targets, { notes: ta.value }).catch(toastError));
    notesSec.body.replaceChildren(ta);
  }
  const deb = debounce(paint, 60);
  bus.on('lib:selection', deb); bus.on('lib:items', deb); bus.on('lib:listed', deb); bus.on('lib:cull', deb);
  paint();
  return { el: root, paint };
}
