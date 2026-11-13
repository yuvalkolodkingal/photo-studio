import { clamp, clone } from '../util.js';
import { session } from '../session.js';
import * as S from '../stack.js';
import { bus } from '../store.js';

export const healSettings = { radius: 24, feather: 0.35, mode: 'heal', opacity: 1 };
const spotsOf = (st) => { const e = S.findEntry(st, 'heal'); return (e && e.params && e.params.spots) || []; };

/** Spot heal / clone. Spots live in the `heal` op (pre-geometry, normalised): {src?, dst, radius, feather, mode, opacity}. */
export const healTool = {
  name: 'heal', cursor: 'crosshair', noDblZoom: true, sel: -1, drag: null, hover: null, viewer: null,
  hint: 'Click to heal a spot (source is picked automatically). Drag from the spot to choose the source. Delete removes the selected spot.',
  activate(v) { this.viewer = v; session.setNogeo(true); this.sel = -1; bus.emit('tool', 'heal'); bus.emit('tool:hint', this.hint); },
  deactivate() { session.setNogeo(false); bus.emit('tool', null); bus.emit('tool:hint', ''); },
  px(v) { return v.img.w; },  // radius is defined at a 2000 px long edge: scale to screen
  rScreen(r, v) { const long = Math.max(v.img.w, v.img.h); return (r / 2000) * long * v.z; },
  save(spots, live, label = 'Retouch') {
    session.edit(label, (st) => { if (!spots.length) S.resetOps(st, ['heal'], session.defaultOps); else S.setParams(st, 'heal', { spots }); }, { live });
  },
  hit(pt, v) {
    const spots = spotsOf(session.stack);
    for (let i = spots.length - 1; i >= 0; i--) {
      const s = spots[i], [dx, dy] = v.toScreen(...s.dst), r = Math.max(this.rScreen(s.radius || healSettings.radius, v), 8);
      if (s.src) { const [sx, sy] = v.toScreen(...s.src); if (Math.hypot(sx - pt.x, sy - pt.y) <= r) return { i, part: 'src' }; }
      if (Math.hypot(dx - pt.x, dy - pt.y) <= r) return { i, part: 'dst' };
    }
    return null;
  },
  pointerdown(pt, v) {
    const spots = clone(spotsOf(session.stack));
    const h = this.hit(pt, v);
    if (h) { this.sel = h.i; this.drag = { kind: 'move', i: h.i, part: h.part, spots, moved: false }; bus.emit('tool', 'heal'); v.drawOverlay(); return true; }
    if (pt.nx < 0 || pt.nx > 1 || pt.ny < 0 || pt.ny > 1) return false;
    spots.push({ dst: [pt.nx, pt.ny], radius: healSettings.radius, feather: healSettings.feather, mode: healSettings.mode, opacity: healSettings.opacity });
    this.sel = spots.length - 1;
    this.drag = { kind: 'new', i: this.sel, spots, start: [pt.x, pt.y], moved: false };
    v.drawOverlay(); return true;
  },
  pointermove(pt, v, captured) {
    if (!captured || !this.drag) { this.hover = [pt.x, pt.y]; v.drawOverlay(); return; }
    const d = this.drag, s = d.spots[d.i];
    if (d.kind === 'new') {
      if (Math.hypot(pt.x - d.start[0], pt.y - d.start[1]) > 6) { s.src = [clamp(pt.nx, 0, 1), clamp(pt.ny, 0, 1)]; d.moved = true; }
    } else {
      const p = [clamp(pt.nx, 0, 1), clamp(pt.ny, 0, 1)];
      if (d.part === 'src') s.src = p; else s.dst = p;
      d.moved = true;
    }
    this.hover = [pt.x, pt.y];
    const st = clone(session.stack); S.setParams(st, 'heal', { spots: clone(d.spots) });
    session.setStack(st, 'Retouch', { live: true });
    v.drawOverlay();
  },
  pointerup() {
    const d = this.drag; this.drag = null;
    if (!d) return;
    if (d.kind === 'new' || d.moved) this.save(d.spots, false, d.kind === 'new' ? 'Heal spot' : 'Move retouch spot');
    bus.emit('tool', 'heal');
  },
  removeSelected() { const spots = clone(spotsOf(session.stack)); if (this.sel >= 0 && this.sel < spots.length) { spots.splice(this.sel, 1); this.sel = -1; this.save(spots, false, 'Remove retouch spot'); } },
  key(e) {
    if (e.key === 'Delete' || e.key === 'Backspace') { this.removeSelected(); return true; }
    if (e.key === 'Escape' || e.key === 'Enter') { bus.emit('tool:cancel'); return true; }
    return false;
  },
  paint(g, v) {
    const spots = spotsOf(session.stack);
    g.save(); g.lineWidth = 1.5;
    spots.forEach((s, i) => {
      const [dx, dy] = v.toScreen(...s.dst), r = Math.max(this.rScreen(s.radius || healSettings.radius, v), 4);
      g.strokeStyle = i === this.sel ? '#ffd34d' : '#ffffffcc'; g.beginPath(); g.arc(dx, dy, r, 0, 7); g.stroke();
      if (s.src) {
        const [sx, sy] = v.toScreen(...s.src);
        g.setLineDash([5, 4]); g.beginPath(); g.moveTo(dx, dy); g.lineTo(sx, sy); g.stroke(); g.setLineDash([]);
        g.beginPath(); g.arc(sx, sy, r, 0, 7); g.strokeStyle = i === this.sel ? '#6ad0ff' : '#ffffff88'; g.stroke();
      }
    });
    if (this.hover && !this.drag) { g.strokeStyle = '#ffffff77'; g.beginPath(); g.arc(this.hover[0], this.hover[1], Math.max(this.rScreen(healSettings.radius, v), 3), 0, 7); g.stroke(); }
    g.restore();
  },
};
