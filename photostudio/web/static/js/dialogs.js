import { h, clear, sleep, download } from './util.js';
import * as api from './api.js';
import { state } from './store.js';
import { session } from './session.js';
import { toast, toastError } from './toast.js';

let openModal = null;
export function closeModal() { const root = document.getElementById('modal-root'); clear(root); openModal = null; }
export const modalOpen = () => !!openModal;

export function modal({ title, body, buttons = [], width }) {
  closeModal();
  const root = document.getElementById('modal-root');
  const box = h('div.modal', { style: width ? { width: `${width}px` } : {} },
    h('header', { text: title }), h('div.body', {}, body), h('footer', {}, buttons.map((b) => h('button', { class: b.primary ? 'primary' : '', text: b.text, disabled: b.disabled, onclick: () => b.onclick && b.onclick(box) }))));
  const scrim = h('div.scrim', { onmousedown: (e) => { if (e.target === scrim) closeModal(); } }, box);
  root.append(scrim); openModal = scrim;
  return box;
}

// ------------------------------------------------------------------ shortcuts
const SHORTCUTS = [
  ['Everywhere', [['?', 'This list'], ['G', 'Library'], ['D / Enter', 'Open in Develop'], ['0-5', 'Rating'], ['P / X / U', 'Pick / reject / clear flag'], ['6-9', 'Red / yellow / green / blue label'], ['Ctrl+E', 'Export'], ['Ctrl+Shift+C / V', 'Copy / paste settings']]],
  ['Library', [['Arrows', 'Move'], ['Shift / Ctrl+click', 'Range / toggle selection'], ['Ctrl+A', 'Select all'], ['G / C / N / B', 'Grid / compare / survey / cull'], ['Drag', 'Photos onto an album']]],
  ['Develop', [['Left / Right', 'Previous / next photo'], ['Ctrl+Z / Ctrl+Shift+Z', 'Undo / redo'], ['\\ (hold)', 'Show before'], ['Y', 'Before / after split'], ['Z or double-click', 'Fit / 100%'], ['Wheel', 'Zoom'], ['Drag (zoomed)', 'Pan'], ['C', 'Crop (Enter / Esc to leave)'], ['X (crop)', 'Flip orientation'], ['Q', 'Spot heal / clone (Delete removes)'], ['W', 'White balance picker'], ['O', 'Mask overlay'], ['J', 'Clipping overlay'], ['Tab', 'Hide / show panels'], ['Double-click slider label', 'Reset the slider'], ['Alt (brush)', 'Erase']]],
];
export function showShortcuts() {
  const body = h('div.kgrid');
  for (const [title, rows] of SHORTCUTS) {
    body.append(h('h4', { text: title }));
    for (const [k, d] of rows) body.append(h('div.row', {}, h('span', { text: d }), h('span.kbd', { text: k })));
  }
  modal({ title: 'Keyboard shortcuts', body, width: 820, buttons: [{ text: 'Close', primary: true, onclick: closeModal }] });
}

// ------------------------------------------------------------------ export
export async function showExport({ selected }) {
  let presets = [];
  try { presets = (await api.presets('export')).presets; } catch (e) { toastError(e); return; }
  const cur = state.view === 'develop' && session.p ? [session.p] : null;
  const sources = [];
  if (cur) sources.push(['current', `Current photo (${cur[0].split('/').pop()})`, cur]);
  if (selected.length) sources.push(['selected', `${selected.length} selected photo${selected.length > 1 ? 's' : ''}`, selected]);
  sources.push(['listed', `All ${state.items.length} listed photos`, state.items.map((i) => i.p)]);
  const src = h('select', {}, sources.map(([v, t]) => h('option', { value: v, text: t })));
  const preset = h('select', {}, presets.map((p) => h('option', { value: p.name, text: p.title || p.name, title: p.description })));
  preset.value = presets.find((p) => p.name === 'web') ? 'web' : presets[0] && presets[0].name;
  const fmt = h('select', {}, ['(preset default)', 'jpeg', 'png', 'tiff', 'webp'].map((v) => h('option', { value: v === '(preset default)' ? '' : v, text: v })));
  const q = h('input', { type: 'number', min: 30, max: 100, placeholder: 'preset default' });
  const edge = h('input', { type: 'number', min: 100, max: 20000, placeholder: 'preset default' });
  const dir = h('input', { type: 'text', value: 'exports' });
  for (const i of [q, edge, dir]) i.addEventListener('keydown', (e) => e.stopPropagation());
  const prog = h('div'); const out = h('div');
  const body = h('div', {}, h('div.form', {}, h('label', { text: 'Photos' }), src, h('label', { text: 'Preset' }), preset, h('label', { text: 'Format' }), fmt, h('label', { text: 'Quality' }), q, h('label', { text: 'Long edge (px)' }), edge, h('label', { text: 'Folder' }), dir),
    h('div.subtle', { text: 'Files are written to this folder inside your library. Edits are exported from the saved sidecars.' }), prog, out);
  let running = false;
  const box = modal({ title: 'Export', body, width: 520, buttons: [{ text: 'Close', onclick: closeModal }, { text: 'Export', primary: true, onclick: run }] });
  async function run(b) {
    if (running) return;
    const paths = sources.find((s) => s[0] === src.value)[2];
    running = true; clear(out);
    try {
      await session.flush();
      const ov = {};
      if (fmt.value) ov.format = fmt.value;
      if (q.value) ov.quality = Number(q.value);
      if (edge.value) ov.long_edge = Number(edge.value);
      const job0 = await api.exportRun({ paths, preset: preset.value, overrides: ov, out_dir: dir.value || 'exports' });
      let job = job0;
      while (job.state === 'queued' || job.state === 'running') {
        prog.replaceChildren(h('div', { text: `Exporting ${job.done} / ${job.total || paths.length}` }), h('progress', { value: job.done, max: job.total || paths.length, style: { width: '100%' } }));
        await sleep(500); job = await api.job(job0.id);
      }
      prog.replaceChildren();
      if (job.state !== 'done') throw new Error(job.error || `export ${job.state}`);
      const r = job.results;
      out.replaceChildren(h('div', { text: `${r.ok} exported${r.failed ? `, ${r.failed} failed` : ''} to ${r.out_dir}/`, style: { color: r.failed ? 'var(--warn)' : 'var(--good)', margin: '8px 0' } }),
        ...r.files.map((f) => h('div.row2', {}, f.ok ? h('a', { href: api.fileUrl(f.out), download: '', style: { color: 'var(--accent)' }, text: f.out }) : h('span', { style: { color: 'var(--bad)' }, text: `${f.src}: ${f.error}` }), f.ok && f.bytes ? h('span.dim', { text: `${Math.round(f.bytes / 1024)} KB` }) : null)));
      toast(`Exported ${r.ok} file(s)`, 'good');
    } catch (e) { toastError(e); prog.replaceChildren(); } finally { running = false; }
    void b; void box; void download;
  }
}
