"""The pipeline engine: decode -> ordered op stack -> tone map -> output transform -> display array.

Responsibilities (see docs/architecture.md):
  * ``load_source``      LRU-cached decode (raw via decode.decode, JPEG via inverse sRGB), preview-size aware.
  * ``default_stack`` / ``resolve_stack``   defaults < mode < art < user merge; raw-only ops dropped for JPEG.
  * ``render`` / ``render_file`` / ``apply_stack_to_array``   fixed stage order, linear->display domain switch at
    the tone mapper (stage 16) and the gamut-map + OETF output transform (stage 17), masks + blend, geometry,
    per-step result cache for interactive previews, ``upto_stage`` for debugging.
  * ``bake_lut``         render an identity lattice through the colour-only ops of a stack (video / .cube).

Execution rules
  * Entries are sorted by the op's registry *stage* (stable by stack position); the stack only says which ops
    are enabled and with which params. Unknown ops add a warning to ``ctx.log`` and are skipped.
  * Ops never see the cached source or a cached intermediate: when the preview cache is active every op gets a
    private copy; a masked/blended op always gets a copy so the blend base stays pristine.
  * Masks are specified in normalised *post-geometry* coordinates. For masked entries that run before the
    geometry stage (11, e.g. dodge/burn exposure) the mask is built on the geometry-transformed image and pulled
    back to the pre-geometry frame via a coordinate-image trick (``_pull_back_mask``).
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import logging
import math
import os
import re
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from .. import color, decode as _decode
from ..presets import PresetError, load_preset, merge_stack, normalise_entry, split_variant
from ..util import JPEG_EXTS, RAW_EXTS, MissingDependency, params_hash, parse_value
from . import registry
from .registry import TONEMAP_STAGE, Ctx, OpSpec

log = logging.getLogger("photostudio")

GEOMETRY_STAGE = 11
OUTPUT_STAGE = 17
Decoded = _decode.Decoded


class EngineError(RuntimeError):
    pass


# =============================================================================== source cache
_LOCK = threading.RLock()
_SRC_CACHE: "OrderedDict[tuple, Decoded]" = OrderedDict()
_SRC_MAX_BYTES = int(float(os.environ.get("PHOTOSTUDIO_SRC_CACHE_MB", "1500")) * 2**20)
_SIZE_CACHE: dict[tuple, tuple[int, int] | None] = {}


def _nbytes(d: Decoded) -> int:
    return int(d.image.nbytes)


def clear_caches() -> None:
    """Drop the source and per-step caches (tests, memory pressure)."""
    with _LOCK:
        _SRC_CACHE.clear()
        _STAGE_CACHE.clear()
        _STAGE_BYTES[0] = 0
        _SIZE_CACHE.clear()


def cache_info() -> dict:
    with _LOCK:
        return {"sources": len(_SRC_CACHE), "source_mb": round(sum(_nbytes(d) for d in _SRC_CACHE.values()) / 2**20, 1),
                "steps": len(_STAGE_CACHE), "step_mb": round(_STAGE_BYTES[0] / 2**20, 1)}


def _raw_full_size(p: Path, st) -> tuple[int, int] | None:
    """(w, h) of the full-resolution raw frame (cached), None if it cannot be read cheaply."""
    key = (str(p), st.st_mtime_ns)
    if key in _SIZE_CACHE:
        return _SIZE_CACHE[key]
    size = None
    try:
        with _decode.open_raw(p) as raw:
            s = raw.sizes
            w = int(getattr(s, "iwidth", 0) or getattr(s, "width", 0))
            h = int(getattr(s, "iheight", 0) or getattr(s, "height", 0))
            if w and h:
                size = (w, h)
    except Exception as exc:  # noqa: BLE001
        log.debug("raw size probe failed for %s: %s", p, exc)
    _SIZE_CACHE[key] = size
    return size


def raw_params_from_stack(stack: list[dict] | None) -> dict:
    """Decoder parameters from the ``raw`` stack entry (defaults from the registry)."""
    registry._ensure_loaded()
    spec = registry.REGISTRY.get("raw")
    base = dict(spec.defaults) if spec else {"demosaic": "dht", "wb": "camera", "highlight": "reconstruct",
                                              "half_size": False}
    for e in stack or []:
        if e.get("op") == "raw" and e.get("enabled", True):
            base.update(e.get("params") or {})
    return base


def _resize_linear(img: np.ndarray, long_edge: int) -> np.ndarray:
    import cv2

    h, w = img.shape[:2]
    s = long_edge / max(h, w)
    if s >= 0.999:
        return img
    out = cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(out, dtype=np.float32)


def load_source(path: str | Path, long_edge: int | None = None, *, raw_params: dict | None = None) -> Decoded:
    """Decode ``path`` to linear Rec.2020 (cached). ``long_edge`` requests a preview: raw files use LibRaw's
    half-size path when the preview is at most about half the frame, then an area resize. The returned image is
    read-only (it is shared by the cache): copy before modifying. ``raw_info['full_long_edge']`` holds the
    long edge of the full-resolution frame."""
    p = Path(path)
    st = p.stat()
    ext = p.suffix.lower()
    is_raw = ext in RAW_EXTS
    kw: dict[str, Any] = {}
    full_hint = None
    if is_raw:
        rp = {**raw_params_from_stack(None), **(raw_params or {})}
        size = _raw_full_size(p, st)
        full_hint = max(size) if size else None
        half = bool(rp.get("half_size"))
        if long_edge and full_hint and long_edge <= 0.52 * full_hint:
            half = True
        wb = rp.get("wb", "camera")
        kw = dict(demosaic=rp.get("demosaic", "dht"), wb=list(wb) if isinstance(wb, (list, tuple)) else wb,
                  highlight=rp.get("highlight", "reconstruct"), half_size=half)
        if kw["highlight"] in (None, "none"):
            kw["highlight"] = "none"
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size, int(long_edge or 0),
           json.dumps(kw, sort_keys=True, default=str))
    with _LOCK:
        hit = _SRC_CACHE.get(key)
        if hit is not None:
            _SRC_CACHE.move_to_end(key)
            return hit
    dec = _decode.decode(p, **kw) if is_raw else _decode.decode(p)
    full_long = max(dec.image.shape[:2])
    if is_raw and kw.get("half_size"):
        full_long = full_hint or full_long * 2
    if long_edge and max(dec.image.shape[:2]) > long_edge:
        dec.image = _resize_linear(dec.image, int(long_edge))
    dec.image = np.ascontiguousarray(dec.image, dtype=np.float32)
    dec.image.flags.writeable = False
    dec.raw_info = dict(dec.raw_info or {})
    dec.raw_info["full_long_edge"] = int(full_long)
    with _LOCK:
        _SRC_CACHE[key] = dec
        total = sum(_nbytes(d) for d in _SRC_CACHE.values())
        while total > _SRC_MAX_BYTES and len(_SRC_CACHE) > 1:
            _, old = _SRC_CACHE.popitem(last=False)
            total -= _nbytes(old)
    return dec


def source_kind(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    return "raw" if ext in RAW_EXTS else ("jpeg" if ext in JPEG_EXTS else "image")


# =============================================================================== stacks
def default_stack(kind: str = "raw") -> list[dict]:
    """The pipeline-default stack. raw: decode params, lens correction, ISO-adaptive denoise, capture
    sharpening, exposure, white balance, sigmoid tone map (stage 17 output transform is implicit).
    JPEG/image: no raw-only stages and the gentle shoulder-only tone map, so an unedited JPEG round-trips
    nearly unchanged."""
    if kind == "raw":
        entries = [
            {"op": "raw", "params": {"demosaic": "dht", "wb": "camera", "highlight": "reconstruct"}},
            {"op": "lens_correct", "params": {}},
            {"op": "denoise", "params": {}},
            {"op": "capture_sharpen", "params": {}},
            {"op": "exposure", "params": {"ev": 0.0}},
            {"op": "white_balance", "params": {}},
            {"op": "tone_map", "params": {"curve": "sigmoid"}},
        ]
    else:
        entries = [
            {"op": "exposure", "params": {"ev": 0.0}},
            {"op": "white_balance", "params": {}},
            {"op": "tone_map", "params": {"curve": "none"}},
        ]
    return [normalise_entry(e) for e in entries]


def _iso_adapt(stack: list[dict], meta: dict | None) -> None:
    """ISO-adaptive denoise: the denoise op profiles its noise model from ``ctx.meta['iso']`` when its
    ``iso_adapt`` param is on (default), so the engine only has to make sure the flag is set explicitly in the
    resolved stack (visible and editable in the sidecar)."""
    registry._ensure_loaded()
    spec = registry.REGISTRY.get("denoise")
    if spec is None or "iso_adapt" not in spec.defaults:
        return
    for e in stack:
        if e["op"] == "denoise":
            e["params"].setdefault("iso_adapt", True)


def _is_stack(x) -> bool:
    return isinstance(x, list)


def _layer_from(spec_in, kind_name: str, meta: dict | None, image=None) -> tuple[list[dict], dict]:
    """Normalise a mode/art argument (name | 'name:variant' | preset dict | stack list) to (stack, info)."""
    if spec_in is None:
        return [], {}
    if _is_stack(spec_in):
        return [normalise_entry(e) for e in spec_in], {"name": None}
    if isinstance(spec_in, dict):
        stack = [normalise_entry(e) for e in spec_in.get("stack", [])]
        return stack, {"name": spec_in.get("name")}
    name = str(spec_in)
    info: dict[str, Any] = {}
    if kind_name == "mode" and name.split(":")[0].split("/")[0] == "auto":
        from .. import modes

        det = modes.detect_mode(meta or {}, image)
        info = {"auto": True, **det}
        if det.get("mode") is None or det.get("confidence", 0) < modes.AUTO_MIN_CONFIDENCE:
            info["fallback"] = "events"      # weak evidence: the conservative generic mode
            name = "events"
        else:
            name = det["mode"] + (":" + det["variant"] if det.get("variant") else "")
    try:
        try:
            pre = load_preset(kind_name, name)
        except PresetError:
            if kind_name != "art":
                raise
            pre = load_preset("picture_style", name.split("style:", 1)[-1])  # Canon Picture Style approximations
    except PresetError:
        base, var = split_variant(name)
        if var:  # unknown variant of a known preset -> fall back to the base preset
            pre = load_preset(kind_name, base)
            info["variant_missing"] = var
        else:
            raise
    info.setdefault("name", name)
    info["title"] = pre.get("title")
    return [normalise_entry(e) for e in pre["stack"]], info


def resolve_stack_ex(mode=None, art=None, user=None, kind: str = "raw", meta: dict | None = None,
                     image: np.ndarray | None = None, base: list[dict] | None = None) -> tuple[list[dict], dict]:
    """Like :func:`resolve_stack` but also returns ``info`` (resolved mode/art names, auto-detection result)."""
    stack = merge_stack([] , base) if base is not None else default_stack(kind)
    info: dict[str, Any] = {"mode": None, "art": None}
    m_stack, m_info = _layer_from(mode, "mode", meta, image)
    a_stack, a_info = _layer_from(art, "art", meta, image)
    if m_info:
        info["mode"] = m_info.get("name")
        if m_info.get("auto"):
            info["auto"] = {k: m_info[k] for k in ("mode", "confidence", "reasons", "variant") if k in m_info}
    info["art"] = a_info.get("name") if a_info else None
    if info.get("mode") and isinstance(mode, str) and ":" in mode and m_info.get("name") is None:
        info["mode"] = mode
    stack = merge_stack(stack, m_stack)
    stack = merge_stack(stack, a_stack)
    if user:
        if isinstance(user, dict):
            user = [normalise_entry(e) for e in user.get("stack", [])]
        stack = merge_stack(stack, user)
    # raw-only ops never apply to JPEG/image sources
    if kind != "raw":
        reg = registry.all_ops()
        stack = [e for e in stack if not (e["op"] in reg and reg[e["op"]].raw_only)]
    _iso_adapt(stack, meta)
    return stack, info


def resolve_stack(mode=None, art=None, user=None, kind: str = "raw", meta: dict | None = None) -> list[dict]:
    """Merge pipeline defaults < mode < art < user edits (CONTRACT merge rules) into one resolved stack.

    ``mode``/``art``: preset name (``'night:astro'`` for variants, ``'auto'`` for mode detection from ``meta``),
    a preset dict, a stack list or None. ``user``: stack list of entries. Raw-only ops are dropped for JPEG."""
    return resolve_stack_ex(mode, art, user, kind, meta)[0]


# ----------------------------------------------------------------------------- CLI --set helpers
_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _parse_val(text: str) -> Any:
    """CLI value: numbers (also '+0.3'), JSON, true/false, else a bare string."""
    if _NUM.match(text):
        f = float(text)
        return int(f) if re.match(r"^[+-]?\d+$", text) else f
    return parse_value(text)


def parse_assignment(text: str) -> tuple[str, str | None, list[str], Any]:
    """'exposure.ev=0.3' -> ('exposure', None, ['ev'], 0.3); 'exposure@sky.ev=1' -> id 'sky';
    'hsl_mixer.hue.red=5' -> params path ['hue','red']; 'vignette.enabled=false'."""
    if "=" not in text:
        raise EngineError(f"--set expects op.param=value, got '{text}'")
    lhs, rhs = text.split("=", 1)
    lhs = lhs.strip()
    if "." not in lhs:
        raise EngineError(f"--set expects op.param=value, got '{text}'")
    opid, rest = lhs.split(".", 1)
    op_name, _, eid = opid.partition("@")
    return op_name, (eid or None), rest.split("."), _parse_val(rhs.strip())


def apply_set(stack: list[dict], assignments: list[str], mask: dict | None = None) -> list[dict]:
    """Apply ``op.param=value`` edits to a stack (new list). Missing ops are appended. With ``mask`` the edited
    entry is a fresh masked instance (appended, not merged) so masked local edits stack."""
    out = [copy.deepcopy(e) for e in stack]
    grouped: dict[tuple, dict] = {}
    for a in assignments:
        op_name, eid, path, value = parse_assignment(a)
        key = (op_name, eid)
        if key not in grouped:
            if mask is not None:
                entry = normalise_entry({"op": op_name, "mask": mask, **({"id": eid} if eid else {})})
                out.append(entry)
            else:
                entry = next((e for e in out if e["op"] == op_name and (eid is None or e.get("id") == eid)
                              and not (e.get("mask") and eid is None)), None)
                if entry is None:
                    entry = normalise_entry({"op": op_name, **({"id": eid} if eid else {})})
                    out.append(entry)
            grouped[key] = entry
        entry = grouped[key]
        if len(path) == 1 and path[0] in ("enabled", "opacity", "blend"):
            entry[path[0]] = value
            continue
        d = entry["params"]
        for k in path[:-1]:
            d = d.setdefault(k, {})
        d[path[-1]] = value
    return out


# =============================================================================== step planning
@dataclass
class _Step:
    stage: int
    order: float
    name: str
    spec: OpSpec | None
    entry: dict
    params: dict
    pseudo: str | None = None   # 'tonemap' | 'output'

    def key_material(self) -> dict:
        e = self.entry
        return {"n": self.name, "p": self.params, "m": e.get("mask"), "b": e.get("blend"), "o": e.get("opacity"),
                "ps": self.pseudo}


def _output_transform(img: np.ndarray, ctx: Ctx) -> np.ndarray:
    """Stage 17: linear Rec.2020 display-referred -> output primaries, Oklch gamut map, OETF."""
    from .ops.basic import output_transform

    if ctx.output_space == "adobe":
        lin = color.apply_matrix(img, ADOBE_FROM_REC2020)
        lin = color.gamut_map_to_unit(lin)
        return np.power(np.clip(lin, 0, 1), 1.0 / 2.19921875).astype(np.float32)
    return output_transform(img, ctx)


ADOBE_TO_XYZ = np.array([[0.5767309, 0.1855540, 0.1881852],
                         [0.2973769, 0.6273491, 0.0752741],
                         [0.0270343, 0.0706872, 0.9911085]])
ADOBE_FROM_REC2020 = np.linalg.inv(ADOBE_TO_XYZ) @ color.REC2020_TO_XYZ


def _plan(stack: list[dict], ctx: Ctx, upto_stage: int | None) -> list[_Step]:
    registry._ensure_loaded()
    steps: list[_Step] = []
    has_tonemap_entry = False
    for idx, e in enumerate(stack):
        name = e.get("op")
        if name == "tone_map":
            has_tonemap_entry = True
        if not e.get("enabled", True):
            continue
        spec = registry.REGISTRY.get(name)
        if spec is None:
            ctx.note(f"warning: unknown op '{name}' skipped")
            continue
        if spec.raw_only and ctx.kind != "raw":
            continue
        if name == "raw":
            continue  # consumed by the decoder (see raw_params_from_stack)
        try:
            params = spec.resolve_params(e.get("params"), e.get("v"))
        except ValueError as exc:       # a bad stored value must not make the image unrenderable: skip it, say so
            ctx.note(f"warning: op '{name}' skipped, invalid params: {exc}")
            continue
        steps.append(_Step(spec.stage, float(idx), name, spec, e, params))
    if not has_tonemap_entry and registry.REGISTRY.get("tone_map"):
        spec = registry.REGISTRY["tone_map"]
        steps.append(_Step(TONEMAP_STAGE, -1.0, "tone_map", spec, {"op": "tone_map"}, spec.resolve_params({}),
                           pseudo="tonemap"))
    steps.append(_Step(OUTPUT_STAGE, -1.0, "output_transform", None, {"op": "output_transform"}, {}, pseudo="output"))
    steps.sort(key=lambda s: (s.stage, s.order))
    if upto_stage is not None:
        steps = [s for s in steps if s.stage <= upto_stage]
    return steps


# =============================================================================== masks
def _masks_module():
    try:
        return importlib.import_module("photostudio.masks")
    except Exception as exc:  # noqa: BLE001 - missing or broken: fall back to the small built-in implementation
        log.debug("photostudio.masks unavailable (%s); using fallback masks", exc)
        return None


def _fb_ramp(img: np.ndarray, spec: dict) -> np.ndarray:
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = (xx + 0.5) / w, (yy + 0.5) / h
    t = spec.get("type")
    if t == "linear":
        x0, y0, x1, y1 = (float(spec.get(k, d)) for k, d in (("x0", 0.5), ("y0", 0.0), ("x1", 0.5), ("y1", 1.0)))
        d = np.array([x1 - x0, y1 - y0], np.float32)
        n2 = float(d @ d) + 1e-9
        s = ((u - x0) * d[0] + (v - y0) * d[1]) / n2
        return np.clip(1.0 - s, 0, 1).astype(np.float32)
    if t == "radial":
        cx, cy = float(spec.get("cx", 0.5)), float(spec.get("cy", 0.5))
        rx, ry = float(spec.get("rx", 0.25)), float(spec.get("ry", spec.get("rx", 0.25)))
        r = np.hypot((u - cx) / max(rx, 1e-6), (v - cy) / max(ry, 1e-6))
        f = float(spec.get("feather_frac", 0.25))
        return np.clip((1.0 - r) / max(f, 1e-3) + 1.0, 0, 1).astype(np.float32) if f > 0 else (r <= 1).astype(np.float32)
    if t == "luminosity":
        lum = color.luminance(img) if img.max() > 1.0 else color.luma(img)
        lo, hi = float(spec.get("lo", 0.0)), float(spec.get("hi", 1.0))
        f = max(float(spec.get("softness", 0.1)), 1e-3)
        return (color.smoothstep(lo - f, lo + f, lum) * (1 - color.smoothstep(hi - f, hi + f, lum))).astype(np.float32)
    raise EngineError(f"mask type '{t}' needs photostudio.masks (not available)")


def _fb_build_mask(spec: dict, img: np.ndarray, ctx: Ctx) -> np.ndarray:
    t = spec.get("type")
    if t == "combine":
        masks = [_fb_build_mask(s, img, ctx) for s in spec.get("masks", [])]
        if not masks:
            return np.ones(img.shape[:2], np.float32)
        mode = spec.get("mode", "union")
        acc = masks[0]
        for m in masks[1:]:
            acc = {"union": np.maximum, "intersect": np.minimum}.get(mode, None)(acc, m) if mode in ("union", "intersect") \
                else (np.clip(acc - m, 0, 1) if mode == "subtract" else np.abs(acc - m))
        m = acc
    else:
        m = _fb_ramp(img, spec)
    if spec.get("invert"):
        m = 1.0 - m
    return np.clip(m * float(spec.get("opacity", 1.0)), 0, 1).astype(np.float32)


def _fb_blend(base, edited, mask, mode="normal", opacity=1.0, domain="linear"):
    a = (np.clip(mask, 0, 1) * float(opacity))[..., None].astype(np.float32)
    return (base * (1 - a) + edited * a).astype(np.float32)


def _build_mask(spec: dict, img: np.ndarray, ctx: Ctx) -> np.ndarray:
    mod = _masks_module()
    m = mod.build_mask(spec, img, ctx) if mod is not None and hasattr(mod, "build_mask") else _fb_build_mask(spec, img, ctx)
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 3:
        m = m[..., 0]
    if m.shape != img.shape[:2]:
        import cv2

        m = cv2.resize(m, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)
    return m


def _blend(base, edited, mask, mode, opacity, domain):
    mod = _masks_module()
    if mod is not None and hasattr(mod, "blend"):
        return np.asarray(mod.blend(base, edited, mask, mode=mode, opacity=opacity, domain=domain), np.float32)
    return _fb_blend(base, edited, mask, mode, opacity, domain)


def _geo_map(shape: tuple[int, int], geo_steps: list[_Step], ctx: Ctx) -> tuple[np.ndarray, tuple[int, int]]:
    """Run an (u, v, 1) coordinate image through the geometry steps: result[y, x] = source coords (normalised)
    of every post-geometry pixel, channel 2 = validity."""
    h, w = shape
    key = ("geo_map", h, w, params_hash([s.key_material() for s in geo_steps]))
    hit = ctx.cache.get(key)
    if hit is not None:
        return hit
    u = (np.arange(w, dtype=np.float32) + 0.5) / w
    v = (np.arange(h, dtype=np.float32) + 0.5) / h
    coord = np.stack([np.broadcast_to(u[None, :], (h, w)), np.broadcast_to(v[:, None], (h, w)),
                      np.ones((h, w), np.float32)], -1).copy()
    sub = replace(ctx, log=[])
    for s in geo_steps:
        coord = s.spec.fn(coord, s.params, sub)
    res = (np.asarray(coord, np.float32), shape)
    ctx.cache[key] = res
    return res


def _pull_back_mask(mask_post: np.ndarray, coord: np.ndarray, pre_shape: tuple[int, int]) -> np.ndarray:
    """Map a mask defined on the post-geometry image back onto the pre-geometry pixel grid by splatting each
    post pixel to its source location (normalised convolution fills holes)."""
    import cv2

    H, W = pre_shape
    if coord.shape[:2] == (H, W):
        u = (np.arange(W, dtype=np.float32) + 0.5) / W
        v = (np.arange(H, dtype=np.float32) + 0.5) / H
        if np.abs(coord[..., 0] - u[None, :]).max() < 1e-3 and np.abs(coord[..., 1] - v[:, None]).max() < 1e-3 \
                and coord[..., 2].min() > 0.99:
            return mask_post  # identity geometry
    valid = coord[..., 2] > 0.5
    sx = np.rint(coord[..., 0] * W - 0.5).astype(np.int64)
    sy = np.rint(coord[..., 1] * H - 0.5).astype(np.int64)
    ok = valid & (sx >= 0) & (sx < W) & (sy >= 0) & (sy < H)
    idx = (sy * W + sx)[ok]
    acc = np.bincount(idx, weights=mask_post[ok].astype(np.float64), minlength=H * W).reshape(H, W).astype(np.float32)
    cnt = np.bincount(idx, minlength=H * W).reshape(H, W).astype(np.float32)
    ratio = (H * W) / max(1.0, float(ok.sum()))
    sig = float(np.clip(math.sqrt(ratio), 0.8, 8.0))
    accb = cv2.GaussianBlur(acc, (0, 0), sig)
    cntb = cv2.GaussianBlur(cnt, (0, 0), sig)
    out = np.where(cntb > 1e-3, accb / np.maximum(cntb, 1e-6), 0.0)
    return np.clip(out, 0, 1).astype(np.float32)


# =============================================================================== execution
def _needs_blend(entry: dict) -> bool:
    return bool(entry.get("mask")) or (entry.get("blend") or "normal") != "normal" or \
        abs(float(entry.get("opacity", 1.0)) - 1.0) > 1e-6


def _exec_step(img: np.ndarray, st: _Step, ctx: Ctx, geo_steps: list[_Step], protect: bool) -> np.ndarray:
    """Run one step on ``img`` in the current ctx.domain; returns the new image."""
    if st.pseudo == "output":
        if ctx.domain == "linear":
            out = _output_transform(img, ctx)
            ctx.domain = "display"
            return out
        return img
    spec = st.spec
    if spec.domain == "linear" and ctx.domain != "linear":
        ctx.note(f"warning: op '{st.name}' needs linear data but the pipeline is already display-referred; skipped")
        return img
    if spec.domain == "display" and ctx.domain != "display":
        ctx.note(f"warning: op '{st.name}' needs display data but runs before the output transform; skipped")
        return img
    if st.pseudo:
        return _exec_entry(img, st, ctx, geo_steps, protect)
    try:
        return _exec_entry(img, st, ctx, geo_steps, protect)
    except MissingDependency:
        raise
    except Exception as exc:  # noqa: BLE001 - one broken entry (bad params, bad mask spec) must not kill the render
        ctx.note(f"warning: op '{st.name}' skipped, it failed with {type(exc).__name__}: {exc}")
        log.debug("op %s failed", st.name, exc_info=True)
        return img


def _exec_entry(img: np.ndarray, st: _Step, ctx: Ctx, geo_steps: list[_Step], protect: bool) -> np.ndarray:
    """The body of :func:`_exec_step` for one stack entry: op, then mask/opacity/blend."""
    spec = st.spec
    entry = st.entry
    blend = _needs_blend(entry) and not st.pseudo
    base = img
    work = img.copy() if (protect or blend) else img
    out = spec.fn(work, st.params, ctx)
    if out is None:
        raise EngineError(f"op '{st.name}' returned None")
    if getattr(out, "ndim", 0) != 3 or out.shape[0] < 2 or out.shape[1] < 2:
        ctx.note(f"warning: op '{st.name}' produced a degenerate {getattr(out, 'shape', None)} image; skipped")
        return base
    if st.pseudo == "tonemap":
        ctx.domain = "linear"  # tone mapper output is display-referred but still linear light
    if blend:
        if out.shape[:2] != base.shape[:2]:
            ctx.note(f"warning: op '{st.name}' changed the image size; mask/blend ignored")
        else:
            mask_spec = entry.get("mask")
            if mask_spec:
                if st.stage < GEOMETRY_STAGE and geo_steps:
                    post, _ = _run_geo(base, geo_steps, ctx)
                    m_post = _build_mask(mask_spec, post, ctx)
                    coord, _ = _geo_map(base.shape[:2], geo_steps, ctx)
                    mask = _pull_back_mask(m_post, coord, base.shape[:2])
                else:
                    mask = _build_mask(mask_spec, base, ctx)
            else:
                mask = np.ones(base.shape[:2], np.float32)
            out = _blend(base, out, mask, entry.get("blend") or "normal", float(entry.get("opacity", 1.0)),
                         ctx.domain)
    if out.dtype != np.float32:
        out = out.astype(np.float32)
    return out


def _run_geo(img: np.ndarray, geo_steps: list[_Step], ctx: Ctx) -> tuple[np.ndarray, Any]:
    sub = replace(ctx, log=[])
    out = img
    for s in geo_steps:
        out = s.spec.fn(out.copy(), s.params, sub)
    return out, None


# ----------------------------------------------------------------------------- per-step result cache
_STAGE_CACHE: "OrderedDict[str, tuple]" = OrderedDict()
_STAGE_BYTES = [0]
_STAGE_MAX_BYTES = int(float(os.environ.get("PHOTOSTUDIO_STEP_CACHE_MB", "768")) * 2**20)


def _cache_put(key: str, arr: np.ndarray, domain: str, notes: list[str]) -> None:
    with _LOCK:
        old = _STAGE_CACHE.pop(key, None)
        if old is not None:
            _STAGE_BYTES[0] -= old[0].nbytes
        _STAGE_CACHE[key] = (arr, domain, list(notes))
        _STAGE_BYTES[0] += arr.nbytes
        while _STAGE_BYTES[0] > _STAGE_MAX_BYTES and len(_STAGE_CACHE) > 1:
            _, (a, _d, _n) = _STAGE_CACHE.popitem(last=False)
            _STAGE_BYTES[0] -= a.nbytes


def _cache_get(key: str):
    with _LOCK:
        hit = _STAGE_CACHE.get(key)
        if hit is not None:
            _STAGE_CACHE.move_to_end(key)
        return hit


def _chain_keys(root: str, steps: list[_Step]) -> list[str]:
    keys, k = [], root
    for s in steps:
        k = hashlib.sha1((k + "|" + params_hash(s.key_material())).encode()).hexdigest()
        keys.append(k)
    return keys


def _run_steps(img: np.ndarray, steps: list[_Step], ctx: Ctx, root_key: str | None, stats: dict) -> np.ndarray:
    """Execute steps in order, resuming from the longest cached prefix when ``root_key`` is given."""
    geo_steps = [s for s in steps if s.spec is not None and s.stage == GEOMETRY_STAGE]
    keys = _chain_keys(root_key, steps) if root_key else []
    start = 0
    if root_key:
        for j in range(len(steps) - 1, -1, -1):
            hit = _cache_get(keys[j])
            if hit is not None:
                img, ctx.domain, notes = hit[0], hit[1], hit[2]
                ctx.log[:] = list(notes)
                start = j + 1
                break
    stats["resumed_from"] = start
    stats["executed"] = 0
    for i in range(start, len(steps)):
        img = _exec_step(img, steps[i], ctx, geo_steps, protect=bool(root_key))
        stats["executed"] += 1
        if root_key:
            _cache_put(keys[i], img, ctx.domain, ctx.log)
    return img


# =============================================================================== public render API
def _seed_for(path: str) -> int:
    return zlib.crc32(Path(path).name.encode()) & 0x7FFFFFFF if path else 0


def make_ctx(dec: Decoded | None, output_space: str = "srgb", kind: str | None = None, seed: int | None = None,
             meta: dict | None = None) -> Ctx:
    ctx = Ctx(domain="linear", kind=kind or (dec.kind if dec else "image"),
              meta=dict(meta if meta is not None else (dec.meta if dec else {})),
              path=dec.path if dec else "", output_space=output_space)
    ctx.seed = seed if seed is not None else _seed_for(ctx.path)
    if dec is not None:
        ctx.source = dec.image
        ctx.full_long_edge = (dec.raw_info or {}).get("full_long_edge") or max(dec.image.shape[:2])
    return ctx


def _view_transform(img: np.ndarray, ctx: Ctx, tone_done: bool) -> np.ndarray:
    """For ``upto_stage < 17``: make the intermediate viewable (default tone map if none ran, then output)."""
    ctx.cache["linear_at_stop"] = img
    if not tone_done and registry.REGISTRY.get("tone_map"):
        spec = registry.REGISTRY["tone_map"]
        img = spec.fn(img.copy(), spec.resolve_params({}), ctx)
    out = _output_transform(img, ctx)
    ctx.domain = "display"
    return out


def render(path_or_decoded, stack: list[dict] | None, long_edge: int | None = None, output_space: str = "srgb",
           upto_stage: int | None = None, return_ctx: bool = False, *, use_cache: bool | None = None,
           seed: int | None = None):
    """Render a stack to a float32 display image in [0, 1] (``output_space`` encoding).

    ``path_or_decoded``: file path or a :class:`Decoded`. ``long_edge``: preview size (None = full resolution).
    ``upto_stage``: stop after that stage; if it is below 17 the (linear) intermediate is shown through the
    default tone map + output transform and the raw linear data is kept in ``ctx.cache['linear_at_stop']``.
    ``use_cache``: per-step result cache (default: on for previews). ``return_ctx``: also return the Ctx
    (``ctx.log`` holds warnings such as skipped unknown ops)."""
    stack = stack if stack is not None else []
    if isinstance(path_or_decoded, Decoded):
        dec = path_or_decoded
        root_id = None
        if long_edge and max(dec.image.shape[:2]) > long_edge:
            dec = replace(dec, image=_resize_linear(dec.image, long_edge))
    else:
        path = Path(path_or_decoded)
        dec = load_source(path, long_edge, raw_params=raw_params_from_stack(stack) if source_kind(path) == "raw" else None)
        st = path.stat()
        root_id = f"{path.resolve()}|{st.st_mtime_ns}|{long_edge}|{json.dumps(raw_params_from_stack(stack), sort_keys=True, default=str)}"
    if use_cache is None:
        use_cache = long_edge is not None
    ctx = make_ctx(dec, output_space, seed=seed)
    steps = _plan(stack, ctx, upto_stage)
    root = None
    if use_cache and root_id:
        root = hashlib.sha1(f"{root_id}|{ctx.kind}|{output_space}|{ctx.seed}".encode()).hexdigest()
    stats: dict = {}
    img = dec.image if root else dec.image.copy()
    out = _run_steps(img, steps, ctx, root, stats)
    ctx.cache["render_stats"] = stats
    if ctx.domain == "linear":
        out = _view_transform(out, ctx, tone_done=any(s.stage == TONEMAP_STAGE for s in steps))
    out = np.array(out, dtype=np.float32, order="C", copy=bool(root))   # never hand out a cached array
    return (out, ctx) if return_ctx else out


def apply_stack_to_array(img_linear: np.ndarray, stack: list[dict], ctx: Ctx | None = None,
                         upto_stage: int | None = None) -> np.ndarray:
    """Run a stack on an in-memory linear Rec.2020 array. ``ctx`` supplies kind/meta/output_space/seed
    (default: kind='image'). Returns display-referred float32 in ``ctx.output_space``. ``img_linear`` is not
    modified."""
    if ctx is None:
        ctx = Ctx(kind="image")
    ctx.domain = "linear"
    if ctx.source is None:
        ctx.source = img_linear
    steps = _plan(stack, ctx, upto_stage)
    out = _run_steps(np.array(img_linear, dtype=np.float32, copy=True), steps, ctx, None, {})
    if ctx.domain == "linear":
        out = _view_transform(out, ctx, tone_done=any(s.stage == TONEMAP_STAGE for s in steps))
    return np.ascontiguousarray(out, dtype=np.float32)


def stack_for(path: str | Path, version: str | None = None) -> list[dict]:
    """The stack to render for ``path``: the sidecar version's stack, or (if empty) defaults resolved with the
    version's recorded mode/art and the file's metadata."""
    from .. import sidecar

    doc = sidecar.load(path)
    stack = sidecar.get_stack(doc, version)
    if stack:
        return stack
    v = doc["versions"][version or doc["active"]]
    return resolve_stack(v.get("mode"), v.get("art"), None, source_kind(path), _meta_for(path))


def render_file(path: str | Path, version: str | None = None, long_edge: int | None = None,
                output_space: str = "srgb", return_ctx: bool = False):
    """Render an image with the stack stored in its sidecar (defaults if there is none)."""
    return render(path, stack_for(path, version), long_edge=long_edge, output_space=output_space,
                  return_ctx=return_ctx)


def _meta_for(path: str | Path) -> dict:
    try:
        from ..metadata import read_metadata

        return read_metadata(path)
    except Exception:  # noqa: BLE001
        return {}


# =============================================================================== LUT baking
SPATIAL_CATEGORIES = {"detail", "geometry", "retouch", "lens", "raster", "art", "raw", "denoise", "sharpen",
                      "blur", "spatial", "optics", "effects"}
SPATIAL_STAGES = {1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 19, 20}
COLOUR_ONLY_NAMES = {"exposure", "white_balance", "gamut_compress", "tone_map", "tone_curve", "color_balance",
                     "vibrance", "saturation", "hsl_mixer", "monochrome", "split_tone", "lut3d", "channel_mixer",
                     "gradient_map", "duotone", "curves", "levels", "color_grade", "color_zones", "brightness_contrast"}
SPATIAL_NAMES = {"vignette", "grain", "halation", "bloom", "orton", "blur", "sharpen", "capture_sharpen",
                 "denoise", "local_contrast", "dehaze", "tone_equalizer", "crop", "rotate", "perspective",
                 "lens_correct", "skin_smooth", "eye_enhance", "background_blur", "defringe", "clarity",
                 "texture", "light_leak", "glitch", "halftone", "posterize_spatial", "lens_blur", "glow", "soft_focus",
                 "noise", "chromatic_shift", "deconvolve", "output_sharpen", "hot_pixels", "ca_correct",
                 "profiled_denoise", "scene_curve_spatial", "highlights_shadows", "hdr_tone"}
COLOUR_MASK_TYPES = {"luminosity", "color_range", "parametric"}


def is_spatial(spec: OpSpec, entry: dict | None = None) -> bool:
    """True if the op is not a pure per-pixel colour function (cannot be baked into a 3D LUT). Honours an
    explicit ``spatial`` attribute on the OpSpec, then falls back to name/category/stage tables. A mask that
    depends on pixel position (anything but luminosity / colour-range / parametric) also makes it spatial."""
    attr = getattr(spec, "spatial", None)
    if attr is not None:
        sp = bool(attr)
    elif spec.name in SPATIAL_NAMES:
        sp = True
    elif spec.name in COLOUR_ONLY_NAMES:
        sp = False
    else:
        sp = spec.category in SPATIAL_CATEGORIES or spec.stage in SPATIAL_STAGES or spec.changes_geometry
    if not sp and entry and entry.get("mask"):
        m = entry["mask"]
        types = {m.get("type")} if m.get("type") != "combine" else {s.get("type") for s in m.get("masks", [])}
        if not types <= COLOUR_MASK_TYPES or m.get("refine"):
            sp = True
    return sp


def bake_lut(stack: list[dict], size: int = 33, kind: str = "jpeg", meta: dict | None = None,
             skipped: list[str] | None = None, shoulder: bool = False) -> np.ndarray:
    """Render an identity lattice (display sRGB in) through the colour-only ops of ``stack`` and return a
    ``(size, size, size, 3)`` float32 array indexed ``[b][g][r]`` (red fastest when flattened, i.e. the .cube
    order) with display-sRGB output. Spatial ops (detail, geometry, blur/grain/vignette effects, raster art,
    spatially varying masks) are skipped; their names are appended to ``skipped`` if a list is given.

    ``shoulder=False`` (default) keeps the default JPEG tone step an identity, so white stays exactly 1.0 and an empty
    stack bakes an identity LUT (what video needs). ``shoulder=True`` bakes the soft highlight shoulder that
    :func:`render` applies to JPEG sources (white lands near 0.984 encoded)."""
    registry._ensure_loaded()
    n = int(size)
    g = np.linspace(0.0, 1.0, n, dtype=np.float32)
    bb, gg, rr = np.meshgrid(g, g, g, indexing="ij")
    lattice = np.stack([rr, gg, bb], -1).reshape(n * n, n, 3)      # row = b*n+g, col = r
    lin = color.apply_matrix(color.srgb_eotf(lattice), color.SRGB_TO_REC2020)
    ctx = Ctx(kind=kind, meta=dict(meta or {}), output_space="srgb", shoulder=bool(shoulder))
    keep = []
    for e in stack:
        spec = registry.REGISTRY.get(e.get("op"))
        if spec is None:
            ctx.note(f"warning: unknown op '{e.get('op')}' skipped")
            continue
        if spec.raw_only and kind != "raw":
            continue
        if e.get("enabled", True) and is_spatial(spec, e):
            ctx.note(f"skipped spatial op '{e['op']}' for LUT bake")
            if skipped is not None:
                skipped.append(e["op"])
            continue
        keep.append(e)
    out = apply_stack_to_array(lin, keep, ctx)
    return np.clip(out, 0, 1).reshape(n, n, n, 3).astype(np.float32)


def write_cube(path: str | Path, lut: np.ndarray, title: str = "photostudio") -> Path:
    """Write an Adobe/IRIDAS ``.cube`` 3D LUT (red index fastest) from a ``[b][g][r]`` array."""
    n = lut.shape[0]
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {n}", "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    flat = lut.reshape(-1, 3)
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in flat]
    p = Path(path)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def apply_lut3d(img: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Trilinear lookup of display-sRGB ``img`` in a ``[b][g][r]`` LUT (verification/helper)."""
    n = lut.shape[0]
    x = np.clip(img, 0, 1) * (n - 1)
    i0 = np.minimum(np.floor(x).astype(np.int32), n - 2)
    f = x - i0
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    out = 0
    for db in (0, 1):
        for dg in (0, 1):
            for dr in (0, 1):
                w = (fr if dr else 1 - fr) * (fg if dg else 1 - fg) * (fb if db else 1 - fb)
                out = out + w * lut[b0 + db, g0 + dg, r0 + dr]
    return out.astype(np.float32)
