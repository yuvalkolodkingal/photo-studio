import { clamp, clone } from '../util.js';
import { session } from '../session.js';
import { bus } from '../store.js';

export const brushSettings = { radius: 40, hardness: 0.5, flow: 1.0 };

/**
 * Mask drawing tool. bind(comp, onChange) selects the component being edited:
 *   comp = {spec}  (spec is mutated)    onChange(final: boolean)  called while dragging and on release.
 * Kinds: linear (drag p0 -> p1), radial (drag from the centre outward, handles to adjust), brush (paint; Alt erases), pick (colour range sample).
 */
export const maskTool = {
  name: 'mask', cursor: 'crosshair', noDblZoom: true, comp: null, onChange: null, drag: null, hover: null, viewer: null,
  mode: null,
  hint: '',
  bind(comp, onChange) {
    this.comp = comp; this.onChange = onChange;
    const t = comp && comp.spec && comp.spec.type;
    this.mode = t === 'linear' ? 'linear' : t === 'radial' ? 'radial' : t === 'brush' ? 'brush' : t === 'color_range' ? 'pick' : null;
    this.hint = { linear: 'Drag to set the gradient: full effect at the start, none at the end.', radial: 'Drag from the centre outward. Drag the handles to adjust.', brush: 'Paint the mask. Hold Alt to erase.', pick: 'Click a colour in the picture.' }[this.mode] || '';
    if (this.viewer) { this.viewer.cv.style.cursor = this.mode ? 'crosshair' : ''; this.viewer.drawOverlay(); }
    bus.emit('tool:hint', this.hint);
  },
  activate(v) { this.viewer = v; bus.emit('tool', 'mask'); bus.emit('tool:hint', this.hint); },
  deactivate() { bus.emit('tool', null); bus.emit('tool:hint', ''); },
  spec() { return this.comp && this.comp.spec; },
  rScreen(r, v) { const long = Math.max(v.img.w, v.img.h); return (r / 2000) * long * v.z; },
  handlesRadial(v) {
    const s = this.spec(); if (!s || s.type !== 'radial') return [];
    const [cx, cy] = s.center, [rx, ry] = s.radii, a = (s.rotation || 0) * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a);
    const W = v.img.w, H = v.img.h;
    // handle positions in normalised coords: ellipse axes rotated in pixel space
    const pt = (u, w2) => [cx + (u * ca - w2 * sa) / W, cy + (u * sa + w2 * ca) / H];
    return [['c', ...v.toScreen(cx, cy)], ['rx', ...v.toScreen(...pt(rx * W, 0))], ['rx2', ...v.toScreen(...pt(-rx * W, 0))], ['ry', ...v.toScreen(...pt(0, ry * H))], ['ry2', ...v.toScreen(...pt(0, -ry * H))]];
  },
  pointerdown(pt, v) {
    const s = this.spec(); if (!s || !this.mode) return false;
    const n = [clamp(pt.nx, 0, 1), clamp(pt.ny, 0, 1)];
    if (this.mode === 'pick') { s.point = n; this.changed(true); return true; }
    if (this.mode === 'linear') {
      const near = (p) => { const [sx, sy] = v.toScreen(...p); return Math.hypot(sx - pt.x, sy - pt.y) < 12; };
      if (s.p0 && near(s.p0)) this.drag = { kind: 'p0' };
      else if (s.p1 && near(s.p1)) this.drag = { kind: 'p1' };
      else { s.p0 = n; s.p1 = [...n]; this.drag = { kind: 'p1' }; }
      return true;
    }
    if (this.mode === 'radial') {
      const hit = this.handlesRadial(v).find(([, hx, hy]) => Math.hypot(hx - pt.x, hy - pt.y) < 11);
      if (hit) this.drag = { kind: hit[0] };
      else { s.center = n; s.radii = [0.01, 0.01]; s.rotation = 0; this.drag = { kind: 'new', c: n }; }
      return true;
    }
    if (this.mode === 'brush') {
      const stroke = { points: [[n[0], n[1]]], radius: brushSettings.radius, hardness: brushSettings.hardness, flow: brushSettings.flow };
      if (pt.e.altKey) stroke.erase = true;
      s.strokes = s.strokes || []; s.strokes.push(stroke);
      this.drag = { kind: 'paint', stroke, last: [pt.x, pt.y] };
      this.changed(false); return true;
    }
    return false;
  },
  pointermove(pt, v, captured) {
    this.hover = [pt.x, pt.y];
    const s = this.spec(), d = this.drag;
    if (!captured || !d || !s) { if (this.mode === 'brush') v.drawOverlay(); return; }
    const n = [clamp(pt.nx, 0, 1), clamp(pt.ny, 0, 1)], W = v.img.w, H = v.img.h;
    if (d.kind === 'p0') s.p0 = n; else if (d.kind === 'p1') s.p1 = n;
    else if (d.kind === 'new' || d.kind === 'rx' || d.kind === 'rx2' || d.kind === 'ry' || d.kind === 'ry2') {
      const c = s.center, a = (s.rotation || 0) * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a);
      const dx = (n[0] - c[0]) * W, dy = (n[1] - c[1]) * H;
      const u = dx * ca + dy * sa, w2 = -dx * sa + dy * ca;   // along / across the ellipse axes (pixels)
      if (d.kind === 'new') { const r = Math.hypot(dx, dy); s.radii = [Math.max(r / W, 0.005), Math.max(r / H, 0.005)]; }
      else if (d.kind === 'rx' || d.kind === 'rx2') s.radii = [Math.max(Math.abs(u) / W, 0.005), s.radii[1]];
      else s.radii = [s.radii[0], Math.max(Math.abs(w2) / H, 0.005)];
    } else if (d.kind === 'c') s.center = n;
    else if (d.kind === 'paint') {
      if (Math.hypot(pt.x - d.last[0], pt.y - d.last[1]) > 3) { d.stroke.points.push([Math.round(n[0] * 10000) / 10000, Math.round(n[1] * 10000) / 10000]); d.last = [pt.x, pt.y]; }
    }
    this.changed(false); v.drawOverlay();
  },
  pointerup() { const d = this.drag; this.drag = null; if (d) this.changed(true); },
  changed(final) { if (this.onChange) this.onChange(final); },
  key(e) { if (e.key === 'Escape') { bus.emit('tool:cancel'); return true; } return false; },
  paint(g, v) {
    const s = this.spec(); if (!s) return;
    g.save(); g.lineWidth = 1.5; g.strokeStyle = '#ffd34d'; g.fillStyle = '#ffd34d';
    if (s.type === 'linear' && s.p0 && s.p1) {
      const [x0, y0] = v.toScreen(...s.p0), [x1, y1] = v.toScreen(...s.p1);
      const dx = x1 - x0, dy = y1 - y0, L = Math.hypot(dx, dy) || 1, nx = -dy / L, ny = dx / L, ext = Math.max(v.W, v.H);
      g.beginPath(); g.moveTo(x0 - nx * ext, y0 - ny * ext); g.lineTo(x0 + nx * ext, y0 + ny * ext); g.stroke();
      g.setLineDash([6, 5]); g.beginPath(); g.moveTo(x1 - nx * ext, y1 - ny * ext); g.lineTo(x1 + nx * ext, y1 + ny * ext); g.stroke(); g.setLineDash([]);
      g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1); g.stroke();
      for (const [px, py] of [[x0, y0], [x1, y1]]) { g.beginPath(); g.arc(px, py, 5, 0, 7); g.fill(); }
    } else if (s.type === 'radial' && s.center && s.radii) {
      const [cx, cy] = v.toScreen(...s.center), W = v.img.w * v.z, H = v.img.h * v.z;
      g.beginPath(); g.ellipse(cx, cy, Math.max(s.radii[0] * W, 1), Math.max(s.radii[1] * H, 1), (s.rotation || 0) * Math.PI / 180, 0, 7); g.stroke();
      for (const [, hx, hy] of this.handlesRadial(v)) { g.beginPath(); g.rect(hx - 4, hy - 4, 8, 8); g.fill(); }
    } else if (s.type === 'color_range' && s.point) {
      const [px, py] = v.toScreen(...s.point); g.beginPath(); g.arc(px, py, 7, 0, 7); g.stroke(); g.beginPath(); g.arc(px, py, 2, 0, 7); g.fill();
    } else if (s.type === 'brush' && this.hover && this.mode === 'brush') {
      g.strokeStyle = '#fff'; g.beginPath(); g.arc(this.hover[0], this.hover[1], Math.max(this.rScreen(brushSettings.radius, v), 2), 0, 7); g.stroke();
      g.strokeStyle = '#ffffff66'; g.beginPath(); g.arc(this.hover[0], this.hover[1], Math.max(this.rScreen(brushSettings.radius * brushSettings.hardness, v), 1), 0, 7); g.stroke();
    }
    g.restore();
  },
};
export { clone };
