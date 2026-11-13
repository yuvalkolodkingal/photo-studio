"""Rendering service: previews (latest-wins per session, step cache via the engine), 1:1 regions, thumbnails,
scopes, mask overlays and AI mask warm-up. JPEG encoding uses OpenCV."""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .. import sidecar
from ..pipeline import engine
from ..util import JPEG_EXTS
from . import edits as E
from . import paths as P

log = logging.getLogger("photostudio.web")

GEO_OPS = {"crop", "rotate", "straighten", "auto_level", "perspective"}
MAX_EDGE = 8000


class Superseded(Exception):
    """A newer render request of the same session arrived; this one was skipped."""


class RenderError(ValueError):
    pass


@dataclass
class Session:
    sid: str
    latest: int = 0
    last_u8: np.ndarray | None = None
    last_key: str = ""
    touched: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class Rendered:
    data: bytes
    media_type: str
    etag: str
    headers: dict


def encode_jpeg(rgb_u8: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", rgb_u8[..., ::-1], [cv2.IMWRITE_JPEG_QUALITY, int(quality), cv2.IMWRITE_JPEG_OPTIMIZE, 1])
    if not ok:
        raise RenderError("JPEG encoding failed")
    return buf.tobytes()


def encode_png(rgb_u8: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", rgb_u8[..., ::-1], [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise RenderError("PNG encoding failed")
    return buf.tobytes()


def to_u8(img: np.ndarray) -> np.ndarray:
    return (np.clip(img, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


# ----------------------------------------------------------------------------- process-wide AI mask cache
def install_ai_mask_cache(max_mb: float = 256.0) -> bool:
    """AI masks are cached per render context, i.e. recomputed on every slider move. The web server process turns on the
    process-level LRU of :mod:`photostudio.masks` (keyed by file, mtime, mask type and size); the AI models read
    ``ctx.source`` (the unedited decode), so a result does not depend on upstream edits."""
    try:
        from .. import masks
    except Exception as exc:  # noqa: BLE001
        log.debug("AI masks unavailable: %s", exc)
        return False
    masks.enable_cache(max_mb)
    return True


class Renderer:
    def __init__(self, root: Path, edits: E.EditService, workers: int = 2):
        self.root = root
        self.edits = edits
        self.pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ps-render")
        self.sessions: "collections.OrderedDict[str, Session]" = collections.OrderedDict()
        self._sess_lock = threading.Lock()
        self._full: "collections.OrderedDict[str, np.ndarray]" = collections.OrderedDict()
        self._full_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._full_guard = threading.Lock()
        self._decode_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._default_cache: dict[tuple, list[dict]] = {}
        self.thumb_dir = root / ".photostudio" / "web_thumbs"
        install_ai_mask_cache()

    # ------------------------------------------------------------------ sessions
    def session(self, sid: str | None) -> Session:
        sid = sid or "default"
        with self._sess_lock:
            s = self.sessions.get(sid)
            if s is None:
                s = self.sessions[sid] = Session(sid)
            s.touched = time.time()
            self.sessions.move_to_end(sid)
            while len(self.sessions) > 8:
                self.sessions.popitem(last=False)
            return s

    def register_seq(self, sid: str | None, seq: int | None) -> tuple[Session, int]:
        s = self.session(sid)
        with s.lock:
            if seq is None:
                s.latest += 1
                seq = s.latest
            else:
                s.latest = max(s.latest, int(seq))
        return s, int(seq)

    # ------------------------------------------------------------------ stack preparation
    def default_stack(self, path: Path) -> list[dict]:
        st = path.stat()
        key = (str(path), st.st_mtime_ns)
        if key not in self._default_cache:
            if len(self._default_cache) > 500:
                self._default_cache.clear()
            self._default_cache[key] = engine.resolve_stack(None, None, None, engine.source_kind(path), self.edits.metadata(path))
        return copy.deepcopy(self._default_cache[key])

    def prepare(self, path: Path, stack: list[dict] | None, *, before: bool = False, nogeo: bool = False,
                version: str | None = None) -> list[dict]:
        if before:
            st = self.default_stack(path)
        elif stack is None:
            st = engine.stack_for(path, version)
        else:
            st = E.clean_stack(stack)
        if nogeo:
            off = GEO_OPS if nogeo is True else (set(nogeo) & GEO_OPS)
            st = [({**e, "enabled": False} if e["op"] in off else e) for e in st]
        return st

    def _prewarm(self, path: Path, stack: list[dict], edge: int | None) -> None:
        """Decode once even if two workers ask for the same file at the same time."""
        raw = engine.raw_params_from_stack(stack) if engine.source_kind(path) == "raw" else None
        with self._decode_locks[str(path)]:
            engine.load_source(path, edge, raw_params=raw)

    def _render_u8(self, path: Path, stack: list[dict], edge: int | None, use_cache: bool = True):
        if edge is not None:
            self._prewarm(path, stack, edge)
        try:
            img, ctx = engine.render(path, stack, long_edge=edge, return_ctx=True, use_cache=use_cache if edge else False)
        except engine.EngineError as exc:
            raise RenderError(str(exc)) from exc
        warnings = [m for m in ctx.log if str(m).startswith("warning")]
        scale = 1.0
        try:
            src_long = max(ctx.source.shape[:2]) if ctx.source is not None else max(img.shape[:2])
            scale = float(ctx.full_long_edge or src_long) / float(src_long)
        except Exception:  # noqa: BLE001
            pass
        return to_u8(img), {"scale": scale, "warnings": warnings, "notes": [str(m) for m in ctx.log][:20]}

    # ------------------------------------------------------------------ preview
    def render_preview(self, rel: str, stack: list[dict] | None, *, long_edge: int = 1600, quality: int = 85,
                       before: bool = False, nogeo: bool = False, overlay: str | None = None, sid: str | None = None,
                       seq: int | None = None, version: str | None = None, if_none_match: str | None = None,
                       draft: bool = False) -> Rendered | None:
        """Blocking render (call from a worker thread). Returns None when superseded. Raises RenderError / FileNotFoundError."""
        path = P.resolve_image(self.root, rel)
        edge = int(min(max(long_edge, 32), MAX_EDGE))
        sess = self.session(sid)
        t0 = time.time()
        st = self.prepare(path, stack, before=before, nogeo=nogeo, version=version)
        stat = path.stat()
        key = hashlib.sha1(json.dumps([str(path), stat.st_mtime_ns, E.stack_hash(st), edge, quality, overlay, sorted(nogeo) if isinstance(nogeo, (set, frozenset, list, tuple)) else nogeo, before],
                                      default=str).encode()).hexdigest()
        etag = f'W/"{key[:20]}"'
        if if_none_match and if_none_match == etag:
            return Rendered(b"", "image/jpeg", etag, {"status": 304})
        if seq is not None and seq < sess.latest:
            raise Superseded()
        u8, info = self._render_u8(path, st, edge)
        if seq is not None and seq < sess.latest:
            raise Superseded()
        if not before:
            with sess.lock:
                sess.last_u8, sess.last_key = u8, key
        shown = u8
        if overlay:
            shown = self._overlay(u8, overlay)
        data = encode_jpeg(shown, quality)
        h, w = u8.shape[:2]
        full = (int(round(w * info["scale"])), int(round(h * info["scale"])))
        headers = {"X-Image-Size": f"{w},{h}", "X-Full-Size": f"{full[0]},{full[1]}", "X-Render-Ms": str(int((time.time() - t0) * 1000)),
                   "X-Warnings": json.dumps(info["warnings"])[:900], "X-Draft": "1" if draft else "0"}
        return Rendered(data, "image/jpeg", etag, headers)

    @staticmethod
    def _overlay(u8: np.ndarray, kind: str) -> np.ndarray:
        from .. import scopes

        f = u8.astype(np.float32) / 255.0
        if kind == "clipping":
            return scopes.clipping_overlay(f)
        if kind in ("false_color", "false-color"):
            return scopes.false_color(f)
        raise RenderError(f"unknown overlay '{kind}'")

    # ------------------------------------------------------------------ 1:1 region
    def render_region(self, rel: str, stack: list[dict] | None, rect: list[float], *, max_px: int = 2600, quality: int = 88,
                      nogeo: bool = False, version: str | None = None) -> Rendered:
        path = P.resolve_image(self.root, rel)
        st = self.prepare(path, stack, nogeo=nogeo, version=version)
        stat = path.stat()
        key = hashlib.sha1(json.dumps([str(path), stat.st_mtime_ns, E.stack_hash(st), "full", sorted(nogeo) if isinstance(nogeo, (set, frozenset, list, tuple)) else nogeo], default=str).encode()).hexdigest()
        with self._full_guard:
            lock = self._full_locks[key]
            hit = self._full.get(key)
            if hit is not None:
                self._full.move_to_end(key)
        if hit is None:
            with lock:
                with self._full_guard:
                    hit = self._full.get(key)
                if hit is None:
                    hit, _ = self._render_u8(path, st, None, use_cache=False)
                    with self._full_guard:
                        self._full[key] = hit
                        while len(self._full) > 2:
                            old, _a = self._full.popitem(last=False)
                            self._full_locks.pop(old, None)
        H, W = hit.shape[:2]
        try:
            x0, y0, x1, y1 = (float(v) for v in rect)
        except (TypeError, ValueError) as exc:
            raise RenderError("rect must be [x0, y0, x1, y1] (normalised)") from exc
        x0, x1 = sorted((min(max(x0, 0.0), 1.0), min(max(x1, 0.0), 1.0)))
        y0, y1 = sorted((min(max(y0, 0.0), 1.0), min(max(y1, 0.0), 1.0)))
        px0, px1 = int(np.floor(x0 * W)), max(int(np.ceil(x1 * W)), int(np.floor(x0 * W)) + 1)
        py0, py1 = int(np.floor(y0 * H)), max(int(np.ceil(y1 * H)), int(np.floor(y0 * H)) + 1)
        crop = hit[py0:py1, px0:px1]
        if max(crop.shape[:2]) > max_px:
            s = max_px / max(crop.shape[:2])
            crop = cv2.resize(crop, (max(1, int(crop.shape[1] * s)), max(1, int(crop.shape[0] * s))), interpolation=cv2.INTER_AREA)
        headers = {"X-Region": f"{px0 / W:.6f},{py0 / H:.6f},{px1 / W:.6f},{py1 / H:.6f}", "X-Full-Size": f"{W},{H}"}
        return Rendered(encode_jpeg(crop, quality), "image/jpeg", "", headers)

    # ------------------------------------------------------------------ thumbnails
    def thumb(self, rel: str, size: int = 320, quality: int = 82) -> Rendered:
        path = P.resolve_image(self.root, rel)
        size = int(min(max(size, 32), 2400))
        doc = sidecar.load(path)
        stack = sidecar.get_stack(doc)
        ops_h = sidecar.ops_hash(stack) if stack else "orig"
        st = path.stat()
        comp_m = 0
        try:
            from ..decode import companion_jpeg

            cj = companion_jpeg(path) if path.suffix.lower() not in JPEG_EXTS else None
            comp_m = cj.stat().st_mtime_ns if cj else 0
        except Exception:  # noqa: BLE001
            pass
        key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{comp_m}|{ops_h}|{size}".encode()).hexdigest()
        out = self.thumb_dir / key[:2] / f"{key}.jpg"
        if out.exists():
            return Rendered(out.read_bytes(), "image/jpeg", f'"{key[:20]}"', {})
        if stack:
            u8, _ = self._render_u8(path, stack, size, use_cache=False)
        else:
            from ..decode import preview_display

            u8 = to_u8(preview_display(path, long_edge=size))
        data = encode_jpeg(u8, quality)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, out)
        return Rendered(data, "image/jpeg", f'"{key[:20]}"', {})

    def preset_thumb(self, rel: str, stack: list[dict], tag: str, size: int = 256) -> Rendered:
        path = P.resolve_image(self.root, rel)
        st = path.stat()
        key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{E.stack_hash(stack)}|{tag}|{size}".encode()).hexdigest()
        out = self.thumb_dir / "preset" / key[:2] / f"{key}.jpg"
        if out.exists():
            return Rendered(out.read_bytes(), "image/jpeg", f'"{key[:20]}"', {})
        u8, _ = self._render_u8(path, stack, int(size), use_cache=False)
        data = encode_jpeg(u8, 80)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return Rendered(data, "image/jpeg", f'"{key[:20]}"', {})

    # ------------------------------------------------------------------ scopes
    def _last(self, sid: str | None) -> np.ndarray:
        s = self.session(sid)
        if s.last_u8 is None:
            raise RenderError("nothing rendered yet in this session")
        return s.last_u8.astype(np.float32) / 255.0

    def histogram(self, sid: str | None, bins: int = 128) -> dict:
        from .. import scopes

        img = self._last(sid)
        h = scopes.histogram(img, "display", 256)
        out = {k: np.asarray(h[k]).reshape(bins, -1).sum(1).tolist() if bins < 256 else np.asarray(h[k]).tolist()
               for k in ("r", "g", "b", "luma")}
        out.update({"bins": bins, "clip_high_pct": h["clip_high_pct"], "clip_low_pct": h["clip_low_pct"], "mean": h["mean"],
                    "median": h["median"], "p1": h["p1"], "p99": h["p99"]})
        return out

    def scope_png(self, sid: str | None, kind: str, w: int = 512, h: int = 256) -> bytes:
        from .. import scopes

        img = self._last(sid)
        if kind == "waveform":
            a = scopes.render_waveform(img, "display", (w, h))
        elif kind == "parade":
            a = scopes.render_parade(img, "display", (w, h))
        elif kind == "vectorscope":
            a = scopes.render_vectorscope(img, "display", int(min(w, h) if h else w))
        elif kind == "histogram":
            a = scopes.render_histogram(img, "display", (w, h))
        elif kind in ("false_color", "false-color"):
            a = scopes.false_color(img, "display")
        elif kind == "clipping":
            a = scopes.clipping_overlay(img)
        else:
            raise RenderError(f"unknown scope '{kind}'")
        return encode_png(np.asarray(a, np.uint8))

    # ------------------------------------------------------------------ masks
    def _mask_ctx(self, path: Path, st: list[dict], edge: int):
        dec = engine.load_source(path, edge, raw_params=engine.raw_params_from_stack(st) if engine.source_kind(path) == "raw" else None)
        ctx = engine.make_ctx(dec, kind=engine.source_kind(path))
        ctx.domain = "display"
        return ctx

    def mask_preview(self, rel: str, stack: list[dict] | None, spec: dict, long_edge: int = 1024, quality: int = 80,
                     nogeo: bool = False, color: tuple = (1.0, 0.1, 0.1), strength: float = 0.6) -> Rendered:
        from .. import masks

        if not isinstance(spec, dict) or "type" not in spec:
            raise RenderError("mask spec needs a 'type'")
        path = P.resolve_image(self.root, rel)
        edge = int(min(max(long_edge, 64), 3000))
        st = self.prepare(path, stack, nogeo=nogeo)
        u8, _ = self._render_u8(path, st, edge)
        ctx = self._mask_ctx(path, st, edge)
        try:
            ov = masks.mask_preview(spec, u8.astype(np.float32) / 255.0, ctx, color_rgb=color, strength=strength)
        except (ValueError, KeyError, TypeError) as exc:
            raise RenderError(f"mask: {exc}") from exc
        return Rendered(encode_jpeg(ov, quality), "image/jpeg", "", {"X-Mask-Notes": json.dumps(ctx.log[:6])[:600]})

    def mask_ai(self, rel: str, spec: dict, stack: list[dict] | None = None, long_edge: int = 1024) -> dict:
        from .. import masks

        path = P.resolve_image(self.root, rel)
        edge = int(min(max(long_edge, 64), 3000))
        st = self.prepare(path, stack)
        u8, _ = self._render_u8(path, st, edge)
        ctx = self._mask_ctx(path, st, edge)
        try:
            m = masks.build_mask(spec, u8.astype(np.float32) / 255.0, ctx)
        except (ValueError, KeyError, TypeError) as exc:
            raise RenderError(f"mask: {exc}") from exc
        ys, xs = np.where(m > 0.5)
        h, w = m.shape
        bbox = [float(xs.min() / w), float(ys.min() / h), float((xs.max() + 1) / w), float((ys.max() + 1) / h)] if xs.size else None
        return {"type": spec.get("type"), "coverage": float((m > 0.5).mean()), "mean": float(m.mean()), "bbox": bbox,
                "notes": [str(x) for x in ctx.log][:8], "size": [w, h]}

    # ------------------------------------------------------------------ white balance picker
    def wb_pick(self, rel: str, x: float, y: float, radius: float = 0.01) -> dict:
        from scipy.optimize import minimize

        from .. import color

        path = P.resolve_image(self.root, rel)
        dec = engine.load_source(path, 1024)
        img = dec.image
        H, W = img.shape[:2]
        r = max(1, int(radius * max(H, W)))
        cx, cy = int(min(max(x, 0), 1) * (W - 1)), int(min(max(y, 0), 1) * (H - 1))
        patch = img[max(cy - r, 0):cy + r + 1, max(cx - r, 0):cx + r + 1].reshape(-1, 3)
        rgb = np.maximum(patch.mean(0), 1e-6)
        xyz = color.REC2020_TO_XYZ @ rgb
        src_xy = color.xyz_to_xy(xyz)
        target = src_xy + color.cct_to_xy(6504.0) - color.D65_XY
        t0 = float(np.clip(color.xy_to_cct(target), 2000, 12000))

        def cost(v):
            return float(np.sum((color.cct_tint_to_xy(float(np.clip(v[0], 1500, 20000)), float(v[1])) - target) ** 2)) * 1e6

        res = minimize(cost, [t0, 0.0], method="Nelder-Mead", options={"xatol": 1.0, "fatol": 1e-9, "maxiter": 200})
        temp, tint = float(np.clip(res.x[0], 2000, 12000)), float(np.clip(res.x[1], -150, 150))
        return {"temperature": round(temp), "tint": round(tint, 1), "rgb": [float(v) for v in rgb], "xy": [float(v) for v in src_xy]}

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
