import { clamp } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import { bus } from '../store.js';

export const ASPECTS = [['free', 'Free', null], ['original', 'Original', 'orig'], ['1:1', '1:1', 1], ['5:4', '5:4', 5 / 4], ['4:3', '4:3', 4 / 3], ['3:2', '3:2', 3 / 2], ['16:9', '16:9', 16 / 9], ['2:1', '2:1', 2]];
const HS = 7;

/** Crop overlay tool: works on the frame rendered without the crop (nogeo=['crop']); writes crop.rect = [x, y, w, h] normalised. */
export const cropTool = {
  name: 'crop', cursor: 'crosshair', noDblZoom: true,
  rect: [0, 0, 1, 1], aspect: 'free', flip: false, drag: null, viewer: null,
  hint: 'Drag the edges to crop. Enter applies, Esc cancels, X flips orientation.',
  activate(v) {
    this.viewer = v;
    session.setNogeo(['crop']);
    const e = S.findEntry(session.stack, 'crop');
    const r = e && e.params && e.params.rect;
    this.rect = r ? [...r] : [0, 0, 1, 1];
    this.aspect = (e && e.params && e.params.ui_aspect) || (r ? 'free' : 'free');
    bus.emit('tool', 'crop');
    bus.emit('tool:hint', this.hint);
  },
  deactivate() { session.setNogeo(false); bus.emit('tool', null); bus.emit('tool:hint', ''); this.drag = null; },
  ratio() {
    const v = this.viewer; if (!v || !v.img) return null;
    const a = ASPECTS.find((x) => x[0] === this.aspect); if (!a || a[2] == null) return null;
    let r = a[2] === 'orig' ? v.img.w / v.img.h : a[2];
    if (this.flip && a[2] !== 'orig') r = 1 / r;
    return r;
  },
  setAspect(key) { this.aspect = key; const r = this.ratio(); if (r) this.fitRatio(r); this.commit(); this.viewer.drawOverlay(); bus.emit('tool', 'crop'); },
  toggleFlip() { this.flip = !this.flip; const r = this.ratio(); if (r) this.fitRatio(r); this.commit(); this.viewer.drawOverlay(); },
  fitRatio(r) {
    const v = this.viewer, W = v.img.w, H = v.img.h;
    let [x, y, w, h] = this.rect; const cx = x + w / 2, cy = y + h / 2;
    // largest box of this ratio with the same area-ish, centred, inside the frame
    let nh = h, nw = r * nh * H / W;
    if (nw > w) { nw = w; nh = nw * W / (r * H); }
    if (nh > 1) { nh = 1; nw = r * nh * H / W; }
    if (nw > 1) { nw = 1; nh = nw * W / (r * H); }
    x = clamp(cx - nw / 2, 0, 1 - nw); y = clamp(cy - nh / 2, 0, 1 - nh);
    this.rect = [x, y, nw, nh];
  },
  reset() { this.rect = [0, 0, 1, 1]; session.edit('Reset crop', (st) => S.resetOps(st, ['crop'], session.defaultOps)); this.viewer && this.viewer.drawOverlay(); },
  commit() {
    const [x, y, w, h] = this.rect;
    const full = w > 0.999 && h > 0.999;
    session.edit('Crop', (st) => { if (full) S.resetOps(st, ['crop'], session.defaultOps); else S.setParams(st, 'crop', { rect: [x, y, w, h].map((n) => Math.round(n * 10000) / 10000), aspect: null }); });
  },
  handles() {
    const v = this.viewer, [x, y, w, h] = this.rect;
    const P = (nx, ny) => v.toScreen(nx, ny);
    return [['nw', ...P(x, y)], ['n', ...P(x + w / 2, y)], ['ne', ...P(x + w, y)], ['e', ...P(x + w, y + h / 2)], ['se', ...P(x + w, y + h)], ['s', ...P(x + w / 2, y + h)], ['sw', ...P(x, y + h)], ['w', ...P(x, y + h / 2)]];
  },
  pointerdown(pt, v) {
    const hit = this.handles().find(([, hx, hy]) => Math.hypot(hx - pt.x, hy - pt.y) <= HS + 3);
    const [x, y, w, h] = this.rect;
    if (hit) this.drag = { kind: hit[0], start: [...this.rect] };
    else if (pt.nx >= x && pt.nx <= x + w && pt.ny >= y && pt.ny <= y + h) this.drag = { kind: 'move', start: [...this.rect], p0: [pt.nx, pt.ny] };
    else this.drag = { kind: 'new', p0: [clamp(pt.nx, 0, 1), clamp(pt.ny, 0, 1)], start: [...this.rect] };
    return true;
  },
  pointermove(pt, v, captured) {
    if (!captured || !this.drag) {
      const hit = this.handles().find(([, hx, hy]) => Math.hypot(hx - pt.x, hy - pt.y) <= HS + 3);
      v.cv.style.cursor = hit ? { nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize', n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize' }[hit[0]] : 'crosshair';
      return;
    }
    const d = this.drag, W = v.img.w, H = v.img.h, r = this.ratio();
    const nx = clamp(pt.nx, 0, 1), ny = clamp(pt.ny, 0, 1);
    let [x, y, w, h] = d.start;
    if (d.kind === 'move') {
      const dx = pt.nx - d.p0[0], dy = pt.ny - d.p0[1];
      x = clamp(x + dx, 0, 1 - w); y = clamp(y + dy, 0, 1 - h);
    } else {
      let x0, y0, x1, y1;   // dragged box corners
      if (d.kind === 'new') { x0 = d.p0[0]; y0 = d.p0[1]; x1 = nx; y1 = ny; }
      else {
        x0 = x; y0 = y; x1 = x + w; y1 = y + h;
        if (d.kind.includes('w')) x0 = nx; if (d.kind.includes('e')) x1 = nx;
        if (d.kind.includes('n')) y0 = ny; if (d.kind.includes('s')) y1 = ny;
      }
      let lx = Math.min(x0, x1), rx = Math.max(x0, x1), ty = Math.min(y0, y1), by = Math.max(y0, y1);
      if (r) {
        const corner = d.kind === 'new' || d.kind.length === 2;
        let nw = rx - lx, nh = by - ty;
        const hFromW = nw * W / (r * H), wFromH = r * nh * H / W;
        if (d.kind === 'n' || d.kind === 's') nw = wFromH; else if (d.kind === 'e' || d.kind === 'w') nh = hFromW;
        else if (hFromW > nh) nh = hFromW; else nw = wFromH;
        // anchor: the opposite side/corner stays put
        const k = d.kind === 'new' ? (x1 >= x0 ? (y1 >= y0 ? 'se' : 'ne') : (y1 >= y0 ? 'sw' : 'nw')) : d.kind;
        const ax = k.includes('w') ? d.start[0] + d.start[2] : k.includes('e') ? d.start[0] : (d.start[0] + d.start[2] / 2);
        const ay = k.includes('n') ? d.start[1] + d.start[3] : k.includes('s') ? d.start[1] : (d.start[1] + d.start[3] / 2);
        const axx = d.kind === 'new' ? d.p0[0] : ax, ayy = d.kind === 'new' ? d.p0[1] : ay;
        // clamp the size so the box stays inside the frame
        const maxW = k.includes('w') ? axx : k.includes('e') ? 1 - axx : 2 * Math.min(axx, 1 - axx);
        const maxH = k.includes('n') ? ayy : k.includes('s') ? 1 - ayy : 2 * Math.min(ayy, 1 - ayy);
        const sc = Math.min(1, maxW / Math.max(nw, 1e-6), maxH / Math.max(nh, 1e-6));
        nw *= sc; nh *= sc;
        lx = k.includes('w') ? axx - nw : k.includes('e') ? axx : axx - nw / 2;
        ty = k.includes('n') ? ayy - nh : k.includes('s') ? ayy : ayy - nh / 2;
        rx = lx + nw; by = ty + nh;
        void corner;
      }
      x = lx; y = ty; w = Math.max(rx - lx, 0.02); h = Math.max(by - ty, 0.02);
    }
    this.rect = [x, y, w, h];
    v.drawOverlay();
  },
  pointerup() { this.drag = null; this.commit(); },
  paint(g, v) {
    const ir = v.imageRectScreen(), [x, y, w, h] = this.rect;
    const sx = ir.x + x * ir.w, sy = ir.y + y * ir.h, sw = w * ir.w, sh = h * ir.h;
    g.save();
    g.fillStyle = 'rgba(0,0,0,.58)';
    g.fillRect(ir.x, ir.y, ir.w, sy - ir.y);
    g.fillRect(ir.x, sy + sh, ir.w, ir.y + ir.h - sy - sh);
    g.fillRect(ir.x, sy, sx - ir.x, sh);
    g.fillRect(sx + sw, sy, ir.x + ir.w - sx - sw, sh);
    g.strokeStyle = '#fff'; g.lineWidth = 1.2; g.strokeRect(sx + 0.5, sy + 0.5, sw, sh);
    g.strokeStyle = 'rgba(255,255,255,.35)'; g.lineWidth = 1; g.beginPath();
    for (let i = 1; i < 3; i++) { g.moveTo(sx + (sw * i) / 3, sy); g.lineTo(sx + (sw * i) / 3, sy + sh); g.moveTo(sx, sy + (sh * i) / 3); g.lineTo(sx + sw, sy + (sh * i) / 3); }
    g.stroke();
    g.fillStyle = '#fff'; g.strokeStyle = '#000';
    for (const [, hx, hy] of this.handles()) { g.beginPath(); g.rect(hx - HS / 2, hy - HS / 2, HS, HS); g.fill(); g.stroke(); }
    const W = v.img.w, H = v.img.h;
    g.font = '11px system-ui'; g.fillStyle = '#fff'; g.shadowColor = '#000'; g.shadowBlur = 3;
    g.fillText(`${Math.round(w * W)} x ${Math.round(h * H)}`, sx + 6, sy + sh - 6);
    g.restore();
  },
  key(e) {
    if (e.key === 'Enter') { bus.emit('tool:done'); return true; }
    if (e.key === 'Escape') { bus.emit('tool:cancel'); return true; }
    if (e.key === 'x' || e.key === 'X') { this.toggleFlip(); return true; }
    return false;
  },
};

/** Drag a line along the horizon (or any line that should be level / vertical): sets rotate.angle. */
export const levelTool = {
  name: 'level', cursor: 'crosshair', noDblZoom: true, line: null, viewer: null,
  hint: 'Drag a line along something that should be level.',
  activate(v) { this.viewer = v; session.setNogeo(['crop', 'rotate']); this.line = null; bus.emit('tool', 'level'); bus.emit('tool:hint', this.hint); },
  deactivate() { session.setNogeo(false); bus.emit('tool', null); bus.emit('tool:hint', ''); },
  pointerdown(pt) { this.line = [pt.nx, pt.ny, pt.nx, pt.ny]; return true; },
  pointermove(pt, v, captured) { if (captured && this.line) { this.line[2] = pt.nx; this.line[3] = pt.ny; v.drawOverlay(); } },
  pointerup(pt, v) {
    const l = this.line; this.line = null;
    if (!l) return;
    const W = v.img.w, H = v.img.h;
    let dx = (l[2] - l[0]) * W, dy = (l[3] - l[1]) * H;
    if (Math.hypot(dx, dy) < 12) { v.drawOverlay(); return; }
    if (dx < 0) { dx = -dx; dy = -dy; }
    // near-vertical lines level to 90 degrees
    let ang = Math.atan2(dy, dx) * 180 / Math.PI;
    if (Math.abs(ang) > 45) ang = ang > 0 ? ang - 90 : ang + 90;
    // the frame is shown without the current rotation, so the measured angle is the new rotation
    session.edit('Straighten', (st) => S.setParams(st, 'rotate', { angle: Math.round(clamp(ang, -45, 45) * 100) / 100 }));
    bus.emit('tool:done');
  },
  paint(g, v) {
    if (!this.line) return;
    const [x0, y0] = v.toScreen(this.line[0], this.line[1]), [x1, y1] = v.toScreen(this.line[2], this.line[3]);
    g.strokeStyle = '#ffd34d'; g.lineWidth = 2; g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1); g.stroke();
  },
  key(e) { if (e.key === 'Escape') { bus.emit('tool:cancel'); return true; } return false; },
};
