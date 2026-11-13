// Thin fetch layer over the local server. All paths are library-relative ("trip/IMG_0001.CR3").
import { toast } from './toast.js';

export class ApiError extends Error {
  constructor(msg, status, body) { super(msg); this.status = status; this.body = body; }
}

async function req(method, url, body, opts = {}) {
  const init = { method, headers: {}, signal: opts.signal };
  if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(url, init); } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError('Cannot reach the local server. Is `photo serve` still running?', 0, null);
  }
  if (r.status === 204) return null;
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.blob();
  if (!r.ok) {
    const msg = (data && data.error) || `HTTP ${r.status}`;
    throw new ApiError(msg, r.status, data);
  }
  return data;
}
export const get = (url, opts) => req('GET', url, undefined, opts);
export const post = (url, body, opts) => req('POST', url, body ?? {}, opts);
export const put = (url, body, opts) => req('PUT', url, body, opts);
export const del = (url, opts) => req('DELETE', url, undefined, opts);

export const qs = (o) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o || {})) if (v !== undefined && v !== null && v !== '' && v !== false) p.set(k, v === true ? 'true' : v);
  const s = p.toString(); return s ? `?${s}` : '';
};

export const info = () => get('/api/info');
export const schema = () => get('/api/schema');
export const folders = () => get('/api/library/folders');
export const listImages = (f) => get('/api/library/images' + qs(f));
export const rescan = (wait = false) => post('/api/library/scan', { wait });
export const stats = () => get('/api/library/stats');
export const item = (p) => get('/api/image/item' + qs({ p }));
export const setMeta = (paths, fields) => post('/api/image/meta', { paths, ...fields });
export const albums = () => get('/api/albums');
export const albumCreate = (name, kind = 'static', rule = null) => post('/api/albums', { name, kind, rule });
export const albumImages = (id, paths, action = 'add') => post(`/api/albums/${id}/images`, { paths, action });
export const albumRename = (id, name) => post(`/api/albums/${id}/rename`, { name });
export const albumDelete = (id) => del(`/api/albums/${id}`);
export const tags = () => get('/api/tags');
export const cullRun = (paths) => post('/api/cull/run', { paths });
export const job = (id) => get(`/api/jobs/${id}`);
export const jobCancel = (id) => post(`/api/jobs/${id}/cancel`);

export const loadImage = (p, version) => get('/api/image' + qs({ p, version }));
export const saveStack = (body) => put('/api/image/stack', body);
export const undo = (p, version) => post('/api/image/undo', { p, version });
export const redo = (p, version) => post('/api/image/redo', { p, version });
export const historyGoto = (p, id, version) => post('/api/image/history/goto', { p, id, version });
export const versions = (p, action, name, new_name) => post('/api/image/versions', { p, action, name, new_name });
export const snapshots = (p, action, name, version) => post('/api/image/snapshots', { p, action, name, version });
export const resetImage = (p, version) => post('/api/image/reset', { p, version });
export const copySettings = (p, ops) => post('/api/image/copy', { p, ops });
export const pasteSettings = (paths, mode, ops) => post('/api/image/paste', { paths, mode, ops });
export const syncSettings = (src, paths, ops, mode) => post('/api/image/sync', { src, paths, ops, mode });
export const watch = (paths, replace = false) => post('/api/watch', { paths, replace });
export const changes = (since, wait, opts) => get(`/api/changes?since=${since}&wait=${wait}`, opts);

export const presets = (kind) => get('/api/presets' + qs({ kind }));
export const presetApply = (body) => post('/api/presets/apply', body);
export const presetRemove = (body) => post('/api/presets/remove', body);
export const presetSaveUser = (body) => post('/api/presets/user', body);
export const presetDeleteUser = (kind, name) => del(`/api/presets/user/${kind}/${name}`);
export const modeDetect = (p) => post('/api/mode/detect', { p });
export const maskAi = (p, type, stack) => post('/api/mask/ai', { p, type, stack });
export const wbPick = (p, x, y) => post('/api/wb/pick', { p, x, y });
export const exportRun = (body) => post('/api/export', body);

export const thumbUrl = (p, size = 256, v = '') => '/api/thumb' + qs({ p, size, v });
export const presetThumbUrl = (p, kind, name, size = 160, variant) => '/api/preset_thumb' + qs({ p, kind, name, size, variant });
export const fileUrl = (p) => '/api/file' + qs({ p });
export const scopeUrl = (kind, session, w = 512, h = 256, nonce = '') => `/api/scopes/${kind}.png` + qs({ session, w, h, n: nonce });
export const histogram = (session, bins = 128) => get('/api/scopes/histogram' + qs({ session, bins }));

function parseWarn(v) { try { const a = JSON.parse(v || '[]'); return Array.isArray(a) ? a.map(String) : []; } catch (_) { return []; } }

/** Render a preview: resolves {blob, headers} or null if the server dropped it (superseded) */
export async function render(body, signal) {
  let r;
  try {
    r = await fetch('/api/render', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });
  } catch (e) {
    if (e.name === 'AbortError') return null;
    throw new ApiError('Cannot reach the local server.', 0, null);
  }
  if (r.status === 204) return null;
  if (!r.ok) {
    let msg = `render failed (HTTP ${r.status})`;
    try { msg = (await r.json()).error || msg; } catch (_) { /* ignore */ }
    throw new ApiError(msg, r.status, null);
  }
  const blob = await r.blob();
  const hd = (k) => r.headers.get(k);
  const size = (hd('x-image-size') || '').split(',').map(Number);
  const full = (hd('x-full-size') || '').split(',').map(Number);
  return { blob, etag: hd('etag'), size, full, ms: Number(hd('x-render-ms') || 0), warnings: parseWarn(hd('x-warnings')), seq: Number(hd('x-seq') || 0) };
}
export async function renderRegion(body, signal) {
  let r;
  try { r = await fetch('/api/render/region', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal }); }
  catch (e) { if (e.name === 'AbortError') return null; throw new ApiError('Cannot reach the local server.', 0, null); }
  if (!r.ok) { let m = `HTTP ${r.status}`; try { m = (await r.json()).error || m; } catch (_) { /* ignore */ } throw new ApiError(m, r.status, null); }
  return { blob: await r.blob(), region: (r.headers.get('x-region') || '').split(',').map(Number) };
}
export async function maskPreview(body, signal) {
  let r;
  try { r = await fetch('/api/mask/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal }); }
  catch (e) { if (e.name === 'AbortError') return null; throw new ApiError('Cannot reach the local server.', 0, null); }
  if (!r.ok) { let m = `HTTP ${r.status}`; try { m = (await r.json()).error || m; } catch (_) { /* ignore */ } throw new ApiError(m, r.status, null); }
  return r.blob();
}

/** Long-poll the change log. Calls onEvents(events) for each batch; never throws. */
export function startChangeLoop(onEvents) {
  let since = 0, alive = true, ctl = null;
  (async function loop() {
    try { const r0 = await changes(0, 0); if (r0) since = r0.seq; } catch (_) { /* server not ready yet */ }
    while (alive) {
      try {
        ctl = new AbortController();
        const r = await changes(since, 20, { signal: ctl.signal });
        if (r) { since = r.seq; if (r.events && r.events.length) onEvents(r.events); }
      } catch (e) {
        if (e.name === 'AbortError') break;
        await new Promise((res) => setTimeout(res, 3000));
      }
    }
  })();
  return () => { alive = false; if (ctl) ctl.abort(); };
}
export { toast };
