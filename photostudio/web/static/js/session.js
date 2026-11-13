// The Develop session: owns the stack being edited, previews it (draft while dragging, full on release), autosaves it to the
// same .ps.json sidecar the CLI uses, and follows external edits (Claude / CLI) to that sidecar.
import * as api from './api.js';
import { bus, state, patchItem } from './store.js';
import { toast, toastError, busyAdd } from './toast.js';
import { clone, debounce, uid, eq } from './util.js';
import * as S from './stack.js';

const FULL_EDGE = 1600, DRAFT_EDGE = 800;

class Session {
  constructor() {
    this.p = null; this.version = null; this.stack = []; this.payload = null;
    this.mode = null; this.art = null; this.metaDirty = false;
    this.defaultOps = ['exposure', 'white_balance', 'tone_map'];
    this.sid = 'web-' + uid(8); this.seq = 0; this.ctl = null; this.lastDraft = 0; this.drawTimer = null;
    this.viewer = null;
    this.nogeo = false;           // false | true | ['crop']
    this.overlay = null;          // null | 'clipping' | 'false_color'
    this.baMode = 'off';          // off | split | before
    this.peek = false;
    this.beforeKey = ''; this.beforeBmp = null;
    this.saving = false; this.again = false; this.dirty = false; this.label = 'Edit'; this.saveErr = false;
    this.frame = null;            // {w,h} of the frame on screen
    this.warnings = [];
    this.lastRenderMs = 0;
    this.saveSoon = debounce(() => this.save(), 700);
    this.tileTimer = null;
    this.maskPreview = null;      // {mask spec} -> mask overlay request
    this.maskTimer = null;
  }
  attach(viewer) { this.viewer = viewer; viewer.onView = () => this._scheduleTile(); }

  get loaded() { return !!this.p; }

  // ---- loading -------------------------------------------------------------------
  async open(p, version) {
    if (this.p && this.p !== p) await this.flush();
    this.ctl && this.ctl.abort();
    const pl = await api.loadImage(p, version);
    this.p = pl.p; state.path = pl.p;
    this.nogeo = false; this.maskPreview = null;
    this._adopt(pl);
    this.beforeKey = ''; this.beforeBmp = null;
    if (this.viewer) { this.viewer.setBefore(null); this.viewer.setTile(null); this.viewer.setMask(null); }
    bus.emit('dev:loaded', pl);
    this.render({ draft: false, reset: true });
    return pl;
  }
  _adopt(pl, cause = 'load') {
    this.payload = pl; this.version = pl.version;
    this.stack = clone(pl.stack) || [];
    this.mode = pl.mode == null ? null : pl.mode; this.art = pl.art == null ? null : pl.art; this.metaDirty = false;
    if (pl.default_ops) this.defaultOps = pl.default_ops;
    this.dirty = false; this.saveSoon.cancel();
    this._setSaveState('saved');
    bus.emit('dev:stack', cause);
    bus.emit('dev:history', pl.history);
  }
  async reload(cause = 'reload') {
    if (!this.p) return;
    const pl = await api.loadImage(this.p, this.version);
    this._adopt(pl, cause); bus.emit('dev:loaded', pl); this.render({ draft: false });
  }

  // ---- editing -------------------------------------------------------------------
  /** edit(label, fn(stack)) mutates a clone of the stack. live=true during a drag: preview only, no save. */
  edit(label, fn, { live = false } = {}) {
    const next = clone(this.stack);
    fn(next);
    this.setStack(next, label, { live });
  }
  setStack(next, label, { live = false, mode, art } = {}) {
    this.stack = next; this.label = label || this.label;
    if (mode !== undefined) { this.mode = mode; this.metaDirty = true; }
    if (art !== undefined) { this.art = art; this.metaDirty = true; }
    this.dirty = true;
    bus.emit('dev:stack', live ? 'live' : 'commit');
    this.render({ draft: live });
    if (!live) { this._setSaveState('dirty'); this.saveSoon(); }
  }
  _setSaveState(s) {
    this.saveState = s;
    const el = document.getElementById('saveind');
    if (!el) return;
    const map = { saved: ['Saved', 'saved'], dirty: ['Unsaved', 'saving'], saving: ['Saving...', 'saving'], err: ['Save failed', 'err'] };
    const [t, c] = map[s] || ['', ''];
    el.textContent = this.p ? t : ''; el.className = 'saveind ' + c;
  }
  async flush() {
    this.saveSoon.cancel();
    while (this.saving) await new Promise((r) => setTimeout(r, 30));
    if (this.dirty) await this.save();
  }
  async save({ force = false } = {}) {
    if (!this.p || !this.dirty) return;
    if (this.saving) { this.again = true; return; }
    this.saving = true; this._setSaveState('saving');
    const body = { p: this.p, stack: this.stack, version: this.version, label: this.label, force };
    if (this.metaDirty) { body.mode = this.mode; body.art = this.art; }
    const sentStack = this.stack;
    try {
      const res = await api.saveStack(body);
      if (this.stack === sentStack) { this.dirty = false; this.metaDirty = false; this._setSaveState('saved'); }
      else { this.dirty = true; this.again = true; }
      if (this.payload) { this.payload.history = res.history; this.payload.mtime_ns = res.mtime_ns; this.payload.ops_hash = res.ops_hash; }
      bus.emit('dev:history', res.history);
      patchItem(this.p, { has_edits: true, ops_hash: res.ops_hash });
      bus.emit('lib:item', this.p);
    } catch (e) {
      this.saving = false;
      if (e.status === 409 && e.body && e.body.image) {
        this._adopt(e.body.image, 'conflict'); this.render({ draft: false });
        toast('This image was changed on disk (Claude or the CLI) while you were editing. Loaded the newer version; your last change was not saved.', 'warn', 9000);
        return;
      }
      this.saveErr = true; this._setSaveState('err'); toastError(e);
      return;
    }
    this.saving = false;
    if (this.again) { this.again = false; this.saveSoon(); }
  }
  /** best effort save while the page is closing (fetch keepalive survives unload) */
  saveBeacon() {
    if (!this.p || !this.dirty) return;
    const body = { p: this.p, stack: this.stack, version: this.version, label: this.label };
    if (this.metaDirty) { body.mode = this.mode; body.art = this.art; }
    try { fetch('/api/image/stack', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: true }); } catch (_) { /* closing */ }
  }
  async undo() { await this._hist(() => api.undo(this.p, this.version), 'undo'); }
  async redo() { await this._hist(() => api.redo(this.p, this.version), 'redo'); }
  async gotoHistory(id) { await this._hist(() => api.historyGoto(this.p, id, this.version), 'history'); }
  async _hist(fn, cause) {
    if (!this.p) return;
    await this.flush();
    try { const pl = await fn(); if (pl.changed === false) { bus.emit('dev:history', pl.history); return; } this._adopt(pl, cause); this.render({ draft: false }); patchItem(this.p, { has_edits: true }); bus.emit('lib:item', this.p); } catch (e) { toastError(e); }
  }
  /** version / snapshot / reset / paste: server returns the new payload for this image */
  async adoptCall(fn) {
    await this.flush();
    try { const pl = await fn(); this._adopt(pl, 'server'); bus.emit('dev:loaded', pl); this.render({ draft: false }); return pl; } catch (e) { toastError(e); return null; }
  }

  // ---- external changes (Claude / CLI wrote the sidecar) ------------------------------
  async onExternal() {
    if (!this.p) return;
    const hadLocal = this.dirty;
    this.saveSoon.cancel(); this.dirty = false;
    try {
      const pl = await api.loadImage(this.p, this.version);
      if (this.payload && pl.mtime_ns === this.payload.mtime_ns) return;
      this._adopt(pl, 'external'); bus.emit('dev:loaded', pl); this.render({ draft: false });
      toast(hadLocal ? 'Edited by Claude just now: reloaded (unsaved changes were replaced).' : 'Edited by Claude just now: reloaded.', 'info', 6000);
      bus.emit('dev:banner', 'Edited by Claude just now: reloaded');
    } catch (e) { toastError(e); }
  }

  // ---- rendering -----------------------------------------------------------------
  setNogeo(v) { if (!eq(v, this.nogeo)) { this.nogeo = v; this.render({ draft: false }); } }
  setOverlay(v) { if (this.overlay !== v) { this.overlay = v; this.render({ draft: false }); } }
  setBA(mode) {
    this.baMode = mode;
    if (this.viewer) { this.viewer.setSplit(mode === 'split' ? 0.5 : null); this.viewer.setPeek(mode === 'before' || this.peek); }
    if (mode !== 'off') this._ensureBefore();
  }
  setPeek(on) { this.peek = on; if (on) this._ensureBefore(); if (this.viewer) this.viewer.setPeek(on || this.baMode === 'before'); }

  render({ draft = false, reset = false } = {}) {
    if (!this.p || !this.viewer) return;
    clearTimeout(this.tileTimer);
    this.viewer.clearTile();
    if (draft) {
      const dt = performance.now() - this.lastDraft;
      if (dt < 60) { clearTimeout(this.drawTimer); this.drawTimer = setTimeout(() => this._send(true, false), 60 - dt); return; }
    }
    clearTimeout(this.drawTimer);
    this._send(draft, reset);
  }
  async _send(draft, reset) {
    this.lastDraft = performance.now();
    const seq = ++this.seq;
    if (this.ctl) this.ctl.abort();
    const ctl = this.ctl = new AbortController();
    busyAdd(1);
    try {
      const res = await api.render({
        p: this.p, stack: this.stack, version: this.version, long_edge: draft ? DRAFT_EDGE : FULL_EDGE, quality: draft ? 70 : 88, draft,
        session: this.sid, seq, nogeo: this.nogeo || undefined, overlay: this.overlay || undefined,
      }, ctl.signal);
      if (!res || seq !== this.seq) return;
      const bmp = await createImageBitmap(res.blob);
      if (seq !== this.seq) return;
      this.frame = { w: res.full[0], h: res.full[1] };
      this.viewer.setImage(bmp, res.full[0], res.full[1], { reset });
      this.warnings = res.warnings; this.lastRenderMs = res.ms;
      bus.emit('dev:rendered', { draft, ms: res.ms, warnings: res.warnings, size: res.size, full: res.full });
      if (this.baMode !== 'off' || this.peek) this._ensureBefore();
      if (!draft) { this._scheduleTile(); this._refreshMask(); }
    } catch (e) {
      if (e.name !== 'AbortError' && seq === this.seq) { bus.emit('dev:error', e); toastError(e); }
    } finally { busyAdd(-1); }
  }
  async _ensureBefore() {
    const key = `${this.p}|${JSON.stringify(this.nogeo)}`;
    if (this.beforeKey === key && this.beforeBmp) { this.viewer.setBefore(this.beforeBmp); return; }
    this.beforeKey = key;
    try {
      const res = await api.render({ p: this.p, before: true, long_edge: FULL_EDGE, quality: 85, session: this.sid + '-before', nogeo: this.nogeo || undefined, version: this.version });
      if (!res || this.beforeKey !== key) return;
      this.beforeBmp = await createImageBitmap(res.blob);
      this.viewer.setBefore(this.beforeBmp);
    } catch (e) { toastError(e); }
  }
  _scheduleTile() {
    clearTimeout(this.tileTimer);
    this.tileTimer = setTimeout(() => this._tile(), 260);
  }
  async _tile() {
    const v = this.viewer;
    if (!v || !v.img) return;
    // only when we are magnified past the preview's resolution
    const previewW = v.img.bitmap.width;
    if (v.z * v.img.w <= previewW * 1.02) { v.setTile(null); return; }
    let [x0, y0, x1, y1] = v.visibleRectNorm();
    const maxPx = 2600;
    const wpx = (x1 - x0) * v.img.w, hpx = (y1 - y0) * v.img.h;
    if (wpx > maxPx) { const c = (x0 + x1) / 2, half = maxPx / v.img.w / 2; x0 = c - half; x1 = c + half; }
    if (hpx > maxPx) { const c = (y0 + y1) / 2, half = maxPx / v.img.h / 2; y0 = c - half; y1 = c + half; }
    const seq = this.seq;
    try {
      const res = await api.renderRegion({ p: this.p, stack: this.stack, version: this.version, rect: [x0, y0, x1, y1], max_px: maxPx, nogeo: this.nogeo || undefined });
      if (!res || seq !== this.seq) return;
      const bmp = await createImageBitmap(res.blob);
      const r = res.region.length === 4 && res.region.every(Number.isFinite) ? res.region : [x0, y0, x1, y1];
      v.setTile(bmp, r);
    } catch (e) { /* the preview stays; no toast for a tile */ }
  }

  // ---- mask overlay (red tint of the selected mask) ------------------------------------
  setMaskPreview(spec) { this.maskPreview = spec; this._refreshMask(); }
  _refreshMask() {
    clearTimeout(this.maskTimer);
    if (!this.viewer) return;
    if (!this.maskPreview) { this.viewer.setMask(null); return; }
    this.maskTimer = setTimeout(async () => {
      const spec = this.maskPreview; if (!spec) return;
      try {
        const blob = await api.maskPreview({ p: this.p, stack: this.stack, mask: spec, long_edge: 900, nogeo: this.nogeo || undefined });
        if (!blob || spec !== this.maskPreview) return;
        const bmp = await createImageBitmap(blob);
        this.viewer.setMask(bmp);
      } catch (e) { /* mask preview is best effort */ }
    }, 120);
  }
}

export const session = new Session();
