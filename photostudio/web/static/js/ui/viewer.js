import { h, clamp, throttleRaf } from '../util.js';

const MIN_Z = 0.02, MAX_Z = 8;

/** Canvas image viewer: fit / fill / 1:1 / 2:1, wheel zoom, drag pan, before/after split, 1:1 tile, mask overlay, tool overlay. */
export class Viewer {
  constructor(host) {
    this.host = host;
    this.cv = h('canvas.view');
    this.ov = h('canvas.ovl');
    this.zoomInd = h('div.zoomind', { text: 'Fit' });
    this.handle = h('div.ba-handle.hidden');
    this.lblBefore = h('div.ba-lbl.hidden', { text: 'Before' });
    this.lblAfter = h('div.ba-lbl.hidden', { text: 'After' });
    host.append(this.cv, this.ov, this.handle, this.lblBefore, this.lblAfter, this.zoomInd);
    this.g = this.cv.getContext('2d');
    this.go = this.ov.getContext('2d');
    this.img = null;            // {bitmap, w, h}  w,h = full-resolution size of the frame being shown
    this.before = null;         // bitmap with the same framing
    this.splitF = null;         // 0..1 along the image width, null = no split
    this.peek = false;          // showing the before image full-frame
    this.tile = null;           // {bitmap, rect:[x0,y0,x1,y1]} normalised
    this.maskBmp = null;
    this.zoomMode = 'fit';
    this.z = 1;
    this.cn = [0.5, 0.5];       // view centre in normalised image coordinates
    this.tool = null;
    this.onView = null;
    this.W = 1; this.H = 1; this.dpr = window.devicePixelRatio || 1;
    this.draw = throttleRaf(() => this._draw());
    this.drawOverlay = throttleRaf(() => this._drawOverlay());
    new ResizeObserver(() => this.resize()).observe(host);
    this._bind();
    this.resize();
  }

  // ---- geometry ------------------------------------------------------------------
  resize() {
    const r = this.host.getBoundingClientRect();
    this.W = Math.max(1, Math.round(r.width)); this.H = Math.max(1, Math.round(r.height));
    this.dpr = window.devicePixelRatio || 1;
    for (const c of [this.cv, this.ov]) { c.width = Math.round(this.W * this.dpr); c.height = Math.round(this.H * this.dpr); }
    this._layout(); this.draw(); this.drawOverlay();
  }
  _fitZ() { return this.img ? Math.min(this.W / this.img.w, this.H / this.img.h) : 1; }
  _layout() {
    if (!this.img) return;
    const fz = this._fitZ();
    if (this.zoomMode === 'fit') { this.z = fz; this.cn = [0.5, 0.5]; }
    else if (this.zoomMode === 'fill') { this.z = Math.max(this.W / this.img.w, this.H / this.img.h); }
    else this.z = clamp(Number(this.zoomMode), MIN_Z, MAX_Z);
    this._clampPan();
    this.ox = this.W / 2 - this.cn[0] * this.img.w * this.z;
    this.oy = this.H / 2 - this.cn[1] * this.img.h * this.z;
    this.zoomInd.textContent = this.zoomMode === 'fit' ? `Fit (${Math.round(this.z * 100)}%)` : `${Math.round(this.z * 100)}%`;
    this._placeHandle();
  }
  _clampPan() {
    const iw = this.img.w * this.z, ih = this.img.h * this.z;
    const cx = iw <= this.W ? 0.5 : clamp(this.cn[0], (this.W / 2) / iw, 1 - (this.W / 2) / iw);
    const cy = ih <= this.H ? 0.5 : clamp(this.cn[1], (this.H / 2) / ih, 1 - (this.H / 2) / ih);
    this.cn = [cx, cy];
  }
  /** screen css px -> normalised image coords */
  toNorm(x, y) { return this.img ? [(x - this.ox) / (this.img.w * this.z), (y - this.oy) / (this.img.h * this.z)] : [0, 0]; }
  toScreen(nx, ny) { return this.img ? [this.ox + nx * this.img.w * this.z, this.oy + ny * this.img.h * this.z] : [0, 0]; }
  /** css px per normalised unit */
  scaleXY() { return this.img ? [this.img.w * this.z, this.img.h * this.z] : [1, 1]; }
  imageRectScreen() { const [x, y] = this.toScreen(0, 0); const [sx, sy] = this.scaleXY(); return { x, y, w: sx, h: sy }; }

  setZoom(mode, anchor) {
    if (!this.img) { this.zoomMode = mode; return; }
    if (anchor && mode !== 'fit' && mode !== 'fill') {
      const n = this.toNorm(anchor[0], anchor[1]);
      this.zoomMode = mode; this.z = clamp(Number(mode), MIN_Z, MAX_Z);
      // keep the point under the cursor fixed
      this.cn = [n[0] - (anchor[0] - this.W / 2) / (this.img.w * this.z), n[1] - (anchor[1] - this.H / 2) / (this.img.h * this.z)];
    } else this.zoomMode = mode;
    this._layout(); this.draw(); this.drawOverlay();
    if (this.onView) this.onView(this);
  }
  zoomBy(f, anchor) { const z = clamp(this.z * f, MIN_Z, MAX_Z); this.setZoom(z, anchor || [this.W / 2, this.H / 2]); }
  toggleFit() { this.setZoom(this.zoomMode === 'fit' ? 1 : 'fit', [this.W / 2, this.H / 2]); }

  // ---- content -------------------------------------------------------------------
  setImage(bitmap, w, h2, opts = {}) {
    const first = !this.img;
    const sameFrame = this.img && Math.abs(this.img.w - w) < 1 && Math.abs(this.img.h - h2) < 1;
    this.img = { bitmap, w, h: h2 };
    if (!sameFrame || opts.reset) { this.tile = null; if (first || opts.reset) { this.cn = [0.5, 0.5]; } }
    this._layout(); this.draw(); this.drawOverlay();
  }
  setBefore(bitmap) { this.before = bitmap; this.draw(); }
  setTile(bitmap, rect) { this.tile = bitmap ? { bitmap, rect } : null; this.draw(); }
  setMask(bitmap) { this.maskBmp = bitmap; this.draw(); }
  setPeek(on) { this.peek = !!on; this.draw(); }
  setSplit(f) {
    this.splitF = f;
    const on = f != null;
    this.handle.classList.toggle('hidden', !on); this.lblBefore.classList.toggle('hidden', !on); this.lblAfter.classList.toggle('hidden', !on);
    this._placeHandle(); this.draw();
  }
  _placeHandle() {
    if (this.splitF == null || !this.img) return;
    const x = this.ox + this.splitF * this.img.w * this.z;
    this.handle.style.left = `${x}px`;
    this.lblBefore.style.left = `${Math.max(8, x - 60)}px`; this.lblAfter.style.left = `${Math.min(this.W - 60, x + 10)}px`;
  }
  clearTile() { this.tile = null; }
  visibleRectNorm() {
    if (!this.img) return [0, 0, 1, 1];
    const a = this.toNorm(0, 0), b = this.toNorm(this.W, this.H);
    return [clamp(a[0], 0, 1), clamp(a[1], 0, 1), clamp(b[0], 0, 1), clamp(b[1], 0, 1)];
  }

  // ---- drawing -------------------------------------------------------------------
  _draw() {
    const g = this.g, d = this.dpr;
    g.setTransform(d, 0, 0, d, 0, 0);
    g.clearRect(0, 0, this.W, this.H);
    if (!this.img) return;
    const { x, y, w, h: hh } = this.imageRectScreen();
    g.imageSmoothingEnabled = true; g.imageSmoothingQuality = 'high';
    const showBefore = this.peek && this.before;
    g.save();
    g.beginPath(); g.rect(Math.max(0, x), Math.max(0, y), Math.min(w, this.W), Math.min(hh, this.H)); g.clip();
    if (showBefore) g.drawImage(this.before, x, y, w, hh);
    else {
      g.drawImage(this.img.bitmap, x, y, w, hh);
      if (this.tile) {
        const r = this.tile.rect;
        g.drawImage(this.tile.bitmap, x + r[0] * w, y + r[1] * hh, (r[2] - r[0]) * w, (r[3] - r[1]) * hh);
      }
      if (this.splitF != null && this.before) {
        const sx = x + this.splitF * w;
        g.save(); g.beginPath(); g.rect(x, y, sx - x, hh); g.clip(); g.drawImage(this.before, x, y, w, hh); g.restore();
      }
      if (this.maskBmp) { g.globalAlpha = 0.9; g.drawImage(this.maskBmp, x, y, w, hh); g.globalAlpha = 1; }
    }
    g.restore();
  }
  _drawOverlay() {
    const g = this.go, d = this.dpr;
    g.setTransform(d, 0, 0, d, 0, 0);
    g.clearRect(0, 0, this.W, this.H);
    if (this.tool && this.tool.paint && this.img) this.tool.paint(g, this);
  }
  setTool(tool) {
    if (this.tool && this.tool.deactivate) this.tool.deactivate(this);
    this.tool = tool;
    this.cv.style.cursor = tool && tool.cursor ? tool.cursor : '';
    if (tool && tool.activate) tool.activate(this);
    this.drawOverlay();
  }

  // ---- input ---------------------------------------------------------------------
  _pt(e) {
    const r = this.cv.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const [nx, ny] = this.toNorm(x, y);
    return { x, y, nx, ny, e };
  }
  _bind() {
    const cv = this.cv;
    let pan = null;
    cv.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 && e.button !== 1) return;
      const pt = this._pt(e);
      cv.setPointerCapture(e.pointerId);
      if (e.button === 0 && this.tool && this.tool.pointerdown && this.tool.pointerdown(pt, this) === true) { this._captured = true; return; }
      const iw = this.img ? this.img.w * this.z : 0, ih = this.img ? this.img.h * this.z : 0;
      if (this.img && (iw > this.W + 1 || ih > this.H + 1)) { pan = { x: e.clientX, y: e.clientY, cn: [...this.cn] }; cv.classList.add('grabbing'); }
    });
    cv.addEventListener('pointermove', (e) => {
      const pt = this._pt(e);
      if (pan) {
        const dx = (e.clientX - pan.x) / (this.img.w * this.z), dy = (e.clientY - pan.y) / (this.img.h * this.z);
        this.cn = [pan.cn[0] - dx, pan.cn[1] - dy]; this._layout(); this.draw(); this.drawOverlay();
        return;
      }
      if (this.tool && this.tool.pointermove) this.tool.pointermove(pt, this, this._captured);
    });
    const up = (e) => {
      const pt = this._pt(e);
      if (pan) { pan = null; cv.classList.remove('grabbing'); if (this.onView) this.onView(this); return; }
      if (this._captured) { this._captured = false; if (this.tool && this.tool.pointerup) this.tool.pointerup(pt, this); }
    };
    cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
    cv.addEventListener('dblclick', (e) => { if (!this.tool || !this.tool.noDblZoom) this.toggleFit(); e.preventDefault(); });
    cv.addEventListener('wheel', (e) => {
      if (!this.img) return;
      e.preventDefault();
      const r = cv.getBoundingClientRect();
      this.zoomBy(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), [e.clientX - r.left, e.clientY - r.top]);
    }, { passive: false });
    // before/after split handle
    let dragging = false;
    this.handle.addEventListener('pointerdown', (e) => { dragging = true; this.handle.setPointerCapture(e.pointerId); e.preventDefault(); });
    this.handle.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const r = this.host.getBoundingClientRect();
      this.splitF = clamp((e.clientX - r.left - this.ox) / (this.img.w * this.z), 0.02, 0.98);
      this._placeHandle(); this.draw();
    });
    this.handle.addEventListener('pointerup', () => { dragging = false; });
  }
}
