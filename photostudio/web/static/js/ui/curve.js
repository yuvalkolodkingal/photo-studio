import { h, clamp } from '../util.js';

/** Monotone cubic (Fritsch-Carlson) so the drawn curve never overshoots between points. */
export function monotone(points, n = 128) {
  const p = points.slice().sort((a, b) => a[0] - b[0]);
  if (p.length < 2) return p.length ? [p[0], p[0]] : [[0, 0], [1, 1]];
  const xs = p.map((q) => q[0]), ys = p.map((q) => q[1]);
  const m = p.length, d = [], t = new Array(m);
  for (let i = 0; i < m - 1; i++) d.push((ys[i + 1] - ys[i]) / Math.max(xs[i + 1] - xs[i], 1e-6));
  t[0] = d[0]; t[m - 1] = d[m - 2];
  for (let i = 1; i < m - 1; i++) t[i] = d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2;
  for (let i = 0; i < m - 1; i++) {
    if (d[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / d[i], b = t[i + 1] / d[i], s = a * a + b * b;
    if (s > 9) { const k = 3 / Math.sqrt(s); t[i] = k * a * d[i]; t[i + 1] = k * b * d[i]; }
  }
  const out = [];
  for (let k = 0; k <= n; k++) {
    const x = k / n;
    let i = 0; while (i < m - 2 && x > xs[i + 1]) i++;
    const dx = Math.max(xs[i + 1] - xs[i], 1e-6), u = clamp((x - xs[i]) / dx, 0, 1);
    const u2 = u * u, u3 = u2 * u;
    const y = (2 * u3 - 3 * u2 + 1) * ys[i] + (u3 - 2 * u2 + u) * dx * t[i] + (-2 * u3 + 3 * u2) * ys[i + 1] + (u3 - u2) * dx * t[i + 1];
    out.push([x < xs[0] ? xs[0] : x, clamp(y, 0, 1)]);
  }
  return out;
}

/** Draggable tone curve. onInput(points) while dragging, onCommit(points) on release. Points are [[x,y]...] in 0..1 with y up. */
export function curveEditor({ onInput, onCommit, color = '#ddd' }) {
  const cv = h('canvas.curve', { width: 256, height: 256 });
  const g = cv.getContext('2d');
  let pts = [[0, 0], [1, 1]], col = color, hover = -1, drag = -1, bg = null;
  const pad = 6;

  const X = (x, w) => pad + x * (w - 2 * pad), Y = (y, w) => w - pad - y * (w - 2 * pad);
  function size() {
    const r = cv.getBoundingClientRect(); const s = Math.max(120, Math.round(r.width * (window.devicePixelRatio || 1)));
    if (cv.width !== s) { cv.width = s; cv.height = s; }
    return s;
  }
  function draw() {
    const w = size(), d = w / (cv.getBoundingClientRect().width || w);
    g.clearRect(0, 0, w, w);
    g.fillStyle = '#121212'; g.fillRect(0, 0, w, w);
    if (bg) { g.fillStyle = '#ffffff14'; const bw = (w - 2 * pad) / bg.length; const mx = Math.max(...bg, 1e-6); bg.forEach((v, i) => { const hh = (v / mx) * (w - 2 * pad); g.fillRect(pad + i * bw, w - pad - hh, Math.ceil(bw), hh); }); }
    g.strokeStyle = '#2c2c2c'; g.lineWidth = 1;
    for (let i = 0; i <= 4; i++) { const q = pad + (i / 4) * (w - 2 * pad); g.beginPath(); g.moveTo(q, pad); g.lineTo(q, w - pad); g.moveTo(pad, q); g.lineTo(w - pad, q); g.stroke(); }
    g.strokeStyle = '#3a3a3a'; g.beginPath(); g.moveTo(X(0, w), Y(0, w)); g.lineTo(X(1, w), Y(1, w)); g.stroke();
    g.strokeStyle = col; g.lineWidth = 2 * d; g.beginPath();
    monotone(pts).forEach(([x, y], i) => (i ? g.lineTo(X(x, w), Y(y, w)) : g.moveTo(X(x, w), Y(y, w))));
    g.stroke();
    pts.forEach(([x, y], i) => {
      g.beginPath(); g.arc(X(x, w), Y(y, w), (i === hover || i === drag ? 5 : 4) * d, 0, 7);
      g.fillStyle = i === drag ? '#fff' : '#222'; g.fill(); g.strokeStyle = col; g.lineWidth = 1.5 * d; g.stroke();
    });
  }
  function toVal(e) {
    const r = cv.getBoundingClientRect(); const w = r.width;
    return [clamp(((e.clientX - r.left) - pad) / (w - 2 * pad), 0, 1), clamp(1 - ((e.clientY - r.top) - pad) / (w - 2 * pad), 0, 1)];
  }
  function nearest(e) {
    const r = cv.getBoundingClientRect(); const w = r.width; let best = -1, bd = 12;
    pts.forEach(([x, y], i) => { const dx = X(x, w) - (e.clientX - r.left), dy = Y(y, w) - (e.clientY - r.top); const dd = Math.hypot(dx, dy); if (dd < bd) { bd = dd; best = i; } });
    return best;
  }
  const out = () => pts.map(([x, y]) => [Math.round(x * 10000) / 10000, Math.round(y * 10000) / 10000]);
  cv.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    cv.setPointerCapture(e.pointerId);
    let i = nearest(e);
    if (i < 0) {
      const [x, y] = toVal(e);
      pts.push([x, y]); pts.sort((a, b) => a[0] - b[0]); i = pts.findIndex((q) => q[0] === x && q[1] === y);
    }
    drag = i; draw(); if (onInput) onInput(out());
  });
  cv.addEventListener('pointermove', (e) => {
    if (drag < 0) { const n = nearest(e); if (n !== hover) { hover = n; draw(); } return; }
    let [x, y] = toVal(e);
    const first = drag === 0, last = drag === pts.length - 1;
    if (first) x = pts[0][0]; else if (last) x = pts[drag][0];
    else x = clamp(x, pts[drag - 1][0] + 0.005, pts[drag + 1][0] - 0.005);
    pts[drag] = [x, y]; draw(); if (onInput) onInput(out());
  });
  const up = () => { if (drag >= 0) { drag = -1; draw(); if (onCommit) onCommit(out()); } };
  cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
  cv.addEventListener('dblclick', (e) => {
    const i = nearest(e);
    if (i > 0 && i < pts.length - 1) { pts.splice(i, 1); draw(); if (onInput) onInput(out()); if (onCommit) onCommit(out()); }
  });
  cv.addEventListener('contextmenu', (e) => { e.preventDefault(); const i = nearest(e); if (i > 0 && i < pts.length - 1) { pts.splice(i, 1); draw(); if (onCommit) onCommit(out()); } });
  new ResizeObserver(draw).observe(cv);
  return {
    el: cv,
    set(points, c) { if (drag >= 0) return; pts = (points && points.length >= 2 ? points : [[0, 0], [1, 1]]).map((q) => [q[0], q[1]]); if (c) col = c; draw(); },
    setBackground(arr) { bg = arr; draw(); },
    draw,
  };
}
