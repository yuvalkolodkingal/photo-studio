import { h, debounce } from '../util.js';
import { session } from '../session.js';
import { bus } from '../store.js';
import * as api from '../api.js';
import { section } from '../ui/section.js';

export function build() {
  const sec = section({ id: 'histogram', title: 'Histogram', open: true, toggle: false });
  const cv = h('canvas.hist', { width: 300, height: 110 });
  const img = h('img.hist.hidden', { alt: 'scope' });
  const read = h('div.readout');
  const scopeSel = h('select', {}, [['histogram', 'Histogram'], ['parade', 'RGB parade'], ['waveform', 'Waveform'], ['vectorscope', 'Vectorscope']].map(([v, t]) => h('option', { value: v, text: t })));
  const clipCb = h('input', { type: 'checkbox' });
  const falseCb = h('input', { type: 'checkbox' });
  sec.body.append(cv, img, read,
    h('div.histrow', {}, scopeSel,
      h('label', { title: 'Overlay clipped highlights (red) and shadows (blue) on the image (J)' }, clipCb, 'Clipping'),
      h('label', { title: 'False-colour exposure map' }, falseCb, 'False colour')));
  function setOverlay() { session.setOverlay(falseCb.checked ? 'false_color' : clipCb.checked ? 'clipping' : null); }
  clipCb.addEventListener('change', () => { if (clipCb.checked) falseCb.checked = false; setOverlay(); });
  falseCb.addEventListener('change', () => { if (falseCb.checked) clipCb.checked = false; setOverlay(); });
  scopeSel.addEventListener('change', () => refresh(true));
  bus.on('hist:toggleClip', () => { clipCb.checked = !clipCb.checked; if (clipCb.checked) falseCb.checked = false; setOverlay(); });

  let busy = false, again = false;
  async function refresh(force) {
    if (!session.p || !sec.isOpen()) return;
    if (busy) { again = true; return; }
    busy = true;
    try {
      if (scopeSel.value !== 'histogram') {
        img.classList.remove('hidden'); cv.classList.add('hidden');
        const w = Math.round(cv.getBoundingClientRect().width * (window.devicePixelRatio || 1)) || 300;
        img.src = api.scopeUrl(scopeSel.value, session.sid, w, Math.round(w * 0.45), Date.now());
        read.textContent = '';
        return;
      }
      img.classList.add('hidden'); cv.classList.remove('hidden');
      const d = await api.histogram(session.sid, 128);
      draw(d);
    } catch (e) { /* nothing rendered yet */ } finally { busy = false; if (again) { again = false; setTimeout(refresh, 30); } }
  }
  function draw(d) {
    const w = cv.width = Math.max(200, Math.round(cv.getBoundingClientRect().width * (window.devicePixelRatio || 1)));
    const hh = cv.height = Math.round(w * 0.36);
    const g = cv.getContext('2d');
    g.fillStyle = '#121212'; g.fillRect(0, 0, w, hh);
    const mx = Math.max(...d.luma, ...d.r, ...d.g, ...d.b, 1);
    const norm = (v) => Math.pow(v / mx, 0.6);
    const fill = (arr, col) => {
      g.beginPath(); g.moveTo(0, hh);
      arr.forEach((v, i) => g.lineTo((i / (arr.length - 1)) * w, hh - norm(v) * (hh - 3)));
      g.lineTo(w, hh); g.closePath(); g.fillStyle = col; g.fill();
    };
    g.globalCompositeOperation = 'lighter';
    fill(d.r, 'rgba(230,60,60,.55)'); fill(d.g, 'rgba(60,200,80,.55)'); fill(d.b, 'rgba(70,110,240,.6)');
    g.globalCompositeOperation = 'source-over';
    g.strokeStyle = '#ffffff55'; g.lineWidth = 1; g.beginPath();
    d.luma.forEach((v, i) => { const x = (i / (d.luma.length - 1)) * w, y = hh - norm(v) * (hh - 3); i ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke();
    g.strokeStyle = '#ffffff18'; for (let i = 1; i < 4; i++) { g.beginPath(); g.moveTo((i / 4) * w, 0); g.lineTo((i / 4) * w, hh); g.stroke(); }
    const anyOf = (v) => (v && typeof v === 'object' ? v.any : v);
    const lo = anyOf(d.clip_low_pct), hi = anyOf(d.clip_high_pct);
    const pct = (v) => (v == null ? '-' : `${Number(v).toFixed(v < 10 ? 1 : 0)}%`);
    read.replaceChildren(h('span', { style: { color: lo > 0.5 ? '#6ea8ff' : '' }, text: `shadows ${pct(lo)}` }), h('span', { text: `median ${(d.median * 100).toFixed(0)}` }), h('span', { style: { color: hi > 0.5 ? '#ff7070' : '' }, text: `highlights ${pct(hi)}` }));
  }
  const deb = debounce(() => refresh(), 120);
  bus.on('dev:rendered', (r) => { if (!r.draft) deb(); });
  sec.el.addEventListener('click', () => setTimeout(() => refresh(true), 0));
  return { el: sec.el, sync: () => {}, refresh };
}
