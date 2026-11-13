import { h, clamp, hsl2rgb } from '../util.js';

/**
 * Colour grading wheel. Value = [hue (HSV degrees, 0 = red, clockwise from the top), chroma 0..100, luma].
 * The puck sets hue + chroma; luma is a separate slider owned by the caller.
 */
export function wheel({ onInput, onCommit }) {
  const cv = h('canvas.wheel', { width: 150, height: 150 });
  const g = cv.getContext('2d');
  let val = [0, 0, 0], drag = false, img = null, imgSize = 0;

  function build(sz) {
    const im = g.createImageData(sz, sz), R = sz / 2 - 2;
    for (let y = 0; y < sz; y++) for (let x = 0; x < sz; x++) {
      const dx = x - sz / 2, dy = y - sz / 2, r = Math.hypot(dx, dy);
      if (r > R) continue;
      const hue = (Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360;
      const [rr, gg, bb] = hsl2rgb(hue, 1, 0.5);
      const t = r / R, i = (y * sz + x) * 4;
      im.data[i] = 90 + (rr - 90) * t; im.data[i + 1] = 90 + (gg - 90) * t; im.data[i + 2] = 90 + (bb - 90) * t; im.data[i + 3] = 255;
    }
    return im;
  }
  function draw() {
    const sz = Math.max(100, Math.round(cv.getBoundingClientRect().width * (window.devicePixelRatio || 1)) || 150);
    if (cv.width !== sz) { cv.width = sz; cv.height = sz; }
    if (!img || imgSize !== sz) { img = build(sz); imgSize = sz; }
    g.putImageData(img, 0, 0);
    const R = sz / 2 - 2, a = val[0] * Math.PI / 180, r = clamp(val[1] / 100, 0, 1) * R;
    const px = sz / 2 + Math.sin(a) * r, py = sz / 2 - Math.cos(a) * r;
    g.strokeStyle = '#0008'; g.lineWidth = 1; g.beginPath(); g.arc(sz / 2, sz / 2, R, 0, 7); g.stroke();
    g.beginPath(); g.arc(px, py, 5, 0, 7); g.fillStyle = val[1] > 0 ? '#fff' : '#ccc'; g.fill(); g.strokeStyle = '#000'; g.lineWidth = 1.5; g.stroke();
  }
  function fromEvent(e) {
    const r = cv.getBoundingClientRect(), s = r.width, R = s / 2 - 1.5;
    const dx = e.clientX - r.left - s / 2, dy = e.clientY - r.top - s / 2;
    const hue = (Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360;
    const chroma = clamp(Math.hypot(dx, dy) / R, 0, 1) * 100;
    return [Math.round(hue), Math.round(chroma), val[2]];
  }
  cv.addEventListener('pointerdown', (e) => { cv.setPointerCapture(e.pointerId); drag = true; val = fromEvent(e); draw(); if (onInput) onInput(val.slice()); });
  cv.addEventListener('pointermove', (e) => { if (!drag) return; val = fromEvent(e); draw(); if (onInput) onInput(val.slice()); });
  const up = () => { if (drag) { drag = false; if (onCommit) onCommit(val.slice()); } };
  cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
  cv.addEventListener('dblclick', () => { val = [val[0], 0, val[2]]; draw(); if (onInput) onInput(val.slice()); if (onCommit) onCommit(val.slice()); });
  new ResizeObserver(draw).observe(cv);
  return { el: cv, set(v) { if (drag) return; val = [Number(v[0]) || 0, Number(v[1]) || 0, Number(v[2]) || 0]; draw(); }, draw };
}
