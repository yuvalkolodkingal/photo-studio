"""Retouch ops (stage 8 raw-referred spot work, plus face / sky / lens-blur helpers in display or linear slots).

Mask-driven ops build their own internal masks (ai_skin, ai_eyes, ai_teeth, ai_sky, depth, subject) through
`photostudio.masks.build_mask`; the engine's per-entry mask/blend still applies on top. Spot coordinates are
normalised 0..1, pixel sizes are given at a 2000 px long edge (ctx.px). Linear-domain edits run in a log-like
work space (y = log1p(x/0.05)) so additive low-frequency matching behaves multiplicatively in scene light.

  heal / clone     spots [{src, dst, radius, feather, mode, opacity}] feathered clone + low-frequency match
  inpaint          region mask spec / spots -> cv2.inpaint (Telea | NS) or LaMa (optional)
  blemish_auto     detect small blobs on skin and inpaint them
  skin_smooth      frequency separation restricted to the AI skin mask (texture preserved)
  dodge_burn       brush strokes -> EV map (linear exposure)
  red_eye          red/green eye-shine removal inside eye masks
  teeth_whiten     yellow-band desaturation + lift inside the teeth mask
  eye_enhance      iris contrast/saturation, sclera brighten/de-redden
  background_blur  layered gather lens blur driven by subject mask or depth map
  sky_enhance      graduated sky darkening, blue saturation, cloud clarity
  liquify          push / pinch / bloat / twirl displacement warps (remap)
"""
from __future__ import annotations

import dataclasses
import math

import cv2
import numpy as np

from ... import color
from ...filters import fast_guided, gaussian
from ...masks import build_mask
from ...masks.drawn import brush_stroke_alpha as stroke_alpha
from ...util import MissingDependency, lazy_import
from ..registry import Ctx, op

_K = 0.05   # log work-space knee (linear light)


# --------------------------------------------------------------------------- helpers
def _enc(x: np.ndarray, domain: str) -> np.ndarray:
    return np.log1p(np.maximum(x, 0.0) / _K).astype(np.float32) if domain == "linear" else x.astype(np.float32)


def _dec(y: np.ndarray, domain: str) -> np.ndarray:
    return (_K * np.expm1(y)).astype(np.float32) if domain == "linear" else y.astype(np.float32)


def _ctx(ctx: Ctx | None, domain: str) -> Ctx:
    if ctx is None:
        return Ctx(domain=domain)
    return dataclasses.replace(ctx, domain=domain)   # shares cache / log lists


def _smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0 if e1 != e0 else 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)



# --------------------------------------------------------------------------- heal / clone
def _auto_source(work: np.ndarray, dst: tuple[float, float], r: float) -> tuple[float, float]:
    """Pick a nearby source patch whose mean/std best match the ring around the destination (darktable 'auto')."""
    H, W = work.shape[:2]
    cx, cy = dst
    best, best_s = None, 1e9

    def stats(x, y, r_in, r_out):
        x0, x1 = int(max(x - r_out, 0)), int(min(x + r_out + 1, W))
        y0, y1 = int(max(y - r_out, 0)), int(min(y + r_out + 1, H))
        if x1 - x0 < 3 or y1 - y0 < 3:
            return None
        gy, gx = np.mgrid[y0:y1, x0:x1]
        d = np.hypot(gx - x, gy - y)
        sel = (d >= r_in) & (d <= r_out)
        if sel.sum() < 8:
            return None
        v = work[y0:y1, x0:x1][sel]
        return v.mean(0), v.std(0)

    ref = stats(cx, cy, 1.05 * r, 1.7 * r)
    if ref is None:
        return cx + 3 * r, cy
    for dist in (2.4, 3.2, 4.4):
        for k in range(8):
            a = k * math.pi / 4
            sx, sy = cx + dist * r * math.cos(a), cy + dist * r * math.sin(a)
            if sx - r < 0 or sy - r < 0 or sx + r >= W or sy + r >= H:
                continue
            st = stats(sx, sy, 0.0, r)
            if st is None:
                continue
            score = float(np.abs(st[0] - ref[0]).sum() + 0.5 * np.abs(st[1] - ref[1]).sum())
            if score < best_s:
                best, best_s = (sx, sy), score
    return best if best is not None else (min(cx + 3 * r, W - r - 1), cy)


def _heal_spot(work: np.ndarray, dst: tuple[float, float], src: tuple[float, float], r: float, feather: float,
               mode: str, method: str, opacity: float) -> None:
    H, W = work.shape[:2]
    cx, cy = dst
    shx, shy = int(round(src[0] - cx)), int(round(src[1] - cy))
    sig = max(1.5, 0.3 * r)
    heal = mode == "heal"
    pad = int(math.ceil(3 * sig)) if heal else 0
    half = int(math.ceil(r)) + pad
    icx, icy = int(round(cx)), int(round(cy))
    x0, x1 = max(icx - half, 0, -shx), min(icx + half + 1, W, W - shx)
    y0, y1 = max(icy - half, 0, -shy), min(icy + half + 1, H, H - shy)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return
    gy, gx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    d = np.hypot(gx - cx, gy - cy)
    core = max((1.0 - feather) * r, 0.0)
    M = 1.0 - _smoothstep(core, max(r, core + 1e-3), d)
    M = (M * opacity).astype(np.float32)
    dstp = work[y0:y1, x0:x1].copy()
    srcp = work[y0 + shy:y1 + shy, x0 + shx:x1 + shx].copy()
    if heal and method == "seamless":
        new = _seamless(srcp, dstp, M, (cx - x0, cy - y0))
    elif heal:
        w = (M < 0.02).astype(np.float32)
        s = sig
        for _ in range(5):
            den = gaussian(w, s)
            if (den[M > 0.02] > 0.04).all() or w.sum() < 1:
                break
            s *= 1.8
        num = np.stack([gaussian(dstp[..., c] * w, s) for c in range(dstp.shape[2])], -1)
        low_dst = num / np.maximum(den[..., None], 1e-3)
        low_src = np.stack([gaussian(srcp[..., c], s) for c in range(srcp.shape[2])], -1)
        new = srcp - low_src + low_dst
    else:
        new = srcp
    work[y0:y1, x0:x1] = dstp * (1.0 - M[..., None]) + new * M[..., None]


def _seamless(srcp: np.ndarray, dstp: np.ndarray, M: np.ndarray, centre: tuple[float, float]) -> np.ndarray:
    """cv2.seamlessClone (8-bit Poisson) on the work-space patch; result only trusted inside M."""
    lo = min(float(srcp.min()), float(dstp.min()))
    hi = max(float(srcp.max()), float(dstp.max()), lo + 1e-6)

    def to8(a):
        return np.clip((a - lo) / (hi - lo) * 255.0 + 0.5, 0, 255).astype(np.uint8)

    m8 = (M > 0.3).astype(np.uint8) * 255
    if m8.sum() == 0:
        return srcp
    ys, xs = np.where(m8 > 0)
    c = (int((xs.min() + xs.max()) / 2), int((ys.min() + ys.max()) / 2))
    # seamlessClone needs the mask bbox fully inside dst: crop mask to the ROI around c
    try:
        out8 = cv2.seamlessClone(to8(srcp), to8(dstp), m8, c, cv2.NORMAL_CLONE)
    except cv2.error:
        return srcp
    return out8.astype(np.float32) / 255.0 * (hi - lo) + lo


def _run_heal(img, p, ctx: Ctx, default_mode: str):
    spots = p.get("spots") or []
    if not spots:
        return img
    dom = ctx.domain if ctx.domain in ("linear", "display") else "linear"
    work = _enc(img, dom)
    H, W = img.shape[:2]
    for sp in spots:
        r = max(ctx.px(float(sp.get("radius", p.get("radius", 14.0))), img), 1.5)
        dst = (float(sp["dst"][0]) * W, float(sp["dst"][1]) * H)
        mode = sp.get("mode", p.get("mode", default_mode))
        src_n = sp.get("src")
        src = (float(src_n[0]) * W, float(src_n[1]) * H) if src_n is not None else _auto_source(work, dst, r)
        _heal_spot(work, dst, src, r, float(sp.get("feather", p.get("feather", 0.35))), mode,
                   sp.get("method", p.get("method", "blur_swap")), float(sp.get("opacity", 1.0)))
    return _dec(work, dom)


@op("heal", 8, "linear", category="retouch", spots=[], mode="heal", method="blur_swap", feather=0.35, radius=14.0,
    choices={"mode": ["heal", "clone"], "method": ["blur_swap", "seamless"]})
def heal(img, p, ctx: Ctx):
    """Spot heal: feathered clone of a source patch with its low frequencies swapped for the destination's."""
    return _run_heal(img, p, ctx, "heal")


@op("clone", 8, "linear", category="retouch", spots=[], mode="clone", method="blur_swap", feather=0.35, radius=14.0,
    choices={"mode": ["heal", "clone"], "method": ["blur_swap", "seamless"]})
def clone(img, p, ctx: Ctx):
    """Clone stamp: feathered copy of a source patch (no tonal matching)."""
    return _run_heal(img, p, ctx, "clone")


# --------------------------------------------------------------------------- inpaint
def _inpaint_channels(work: np.ndarray, hole: np.ndarray, radius: int, flag: int) -> np.ndarray:
    lo, hi = float(work.min()), float(work.max())
    scale = max(hi - lo, 1e-6)
    u16 = np.clip((work - lo) / scale * 65535.0 + 0.5, 0, 65535).astype(np.uint16)
    m8 = (hole > 0).astype(np.uint8) * 255
    out = np.empty_like(work)
    for c in range(work.shape[2]):
        out[..., c] = cv2.inpaint(u16[..., c], m8, max(1, radius), flag).astype(np.float32) / 65535.0 * scale + lo
    return out


def _lama(work_disp: np.ndarray, hole: np.ndarray) -> np.ndarray:
    sl = lazy_import("simple_lama_inpainting", "LaMa inpainting")
    from PIL import Image

    lama = sl.SimpleLama()
    ys, xs = np.where(hole > 0)
    pad = 128
    y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad + 1, hole.shape[0])
    x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad + 1, hole.shape[1])
    crop = (np.clip(work_disp[y0:y1, x0:x1], 0, 1) * 255).astype(np.uint8)
    mk = (hole[y0:y1, x0:x1] > 0).astype(np.uint8) * 255
    res = np.asarray(lama(Image.fromarray(crop), Image.fromarray(mk)).convert("RGB"), np.float32) / 255.0
    res = res[:crop.shape[0], :crop.shape[1]]
    out = work_disp.copy()
    out[y0:y0 + res.shape[0], x0:x0 + res.shape[1]] = res
    return out


def inpaint_region(img: np.ndarray, region: np.ndarray, domain: str, ctx: Ctx | None = None, radius: float = 3.0,
                   method: str = "telea", grow: float = 2.0, feather: float = 2.0) -> np.ndarray:
    """Fill `region` (HxW 0..1, >0.5 = hole) from its surroundings. method telea | ns | lama (falls back to telea)."""
    px = (lambda v: ctx.px(v, img)) if ctx is not None else (lambda v: v * max(img.shape[:2]) / 2000.0)
    hole = (region > 0.5).astype(np.uint8)
    if not hole.any():
        return img
    g = int(round(px(grow)))
    if g > 0:
        hole = cv2.dilate(hole, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1)))
    work = _enc(img, domain)
    res = None
    if method == "lama":
        try:
            disp = work if domain == "display" else np.clip(work / max(float(work.max()), 1e-6), 0, 1)
            lam = _lama(disp, hole)
            res = lam if domain == "display" else lam * max(float(work.max()), 1e-6)
        except MissingDependency as exc:
            if ctx is not None:
                ctx.note(f"inpaint: LaMa unavailable ({exc}); using Telea")
    if res is None:
        flag = cv2.INPAINT_NS if method == "ns" else cv2.INPAINT_TELEA
        res = _inpaint_channels(work, hole, max(1, int(round(px(radius)))), flag)
    f = px(feather)
    soft = hole.astype(np.float32)
    if f > 0.3:
        soft = np.maximum(hole.astype(np.float32), gaussian(cv2.dilate(hole, np.ones((3, 3), np.uint8)).astype(np.float32), f * 0.6))
    out = work * (1.0 - soft[..., None]) + res * soft[..., None]
    return _dec(out, domain)


@op("inpaint", 8, "linear", category="retouch", region=None, spots=[], radius=3.0, method="telea", grow=2.0, feather=2.0,
    choices={"method": ["telea", "ns", "lama"]})
def inpaint(img, p, ctx: Ctx):
    """Remove objects: fill a mask spec (`region`) or round `spots` [{center, radius}] from the surroundings."""
    c = _ctx(ctx, "linear" if ctx.domain not in ("linear", "display") else ctx.domain)
    H, W = img.shape[:2]
    region = np.zeros((H, W), np.float32)
    if p.get("region"):
        region = np.maximum(region, build_mask(p["region"], img, c))
    for sp in p.get("spots") or []:
        spec = {"type": "radial", "center": sp["center"], "radii": [float(sp.get("radius", 0.01)), float(sp.get("radius", 0.01)) * W / H],
                "feather": 0.0}
        if "radius_px" in sp:
            spec["radii"] = [sp["radius_px"] / W, sp["radius_px"] / H]
        region = np.maximum(region, build_mask(spec, img, c))
    return inpaint_region(img, region, c.domain, c, p["radius"], p["method"], p["grow"], p["feather"])


# --------------------------------------------------------------------------- blemish auto-detect
def detect_blemishes(img: np.ndarray, ctx: Ctx, sensitivity: float = 0.5, max_size: float = 24.0, min_size: float = 3.0,
                     skin_only: bool = True) -> tuple[np.ndarray, list[dict]]:
    """Find small compact luminance/redness outliers (spots, pimples, dust). Returns (hole mask 0/1, spots list
    [{center, radius_px}]). Uses a difference-of-Gaussians blob score normalised by a robust (MAD) noise estimate."""
    dom = ctx.domain if ctx.domain in ("linear", "display") else "linear"
    c = _ctx(ctx, dom)
    work = _enc(img, dom)
    H, W = img.shape[:2]
    lum = work @ np.array([0.2627, 0.6780, 0.0593], np.float32)
    redn = work[..., 0] - 0.5 * (work[..., 1] + work[..., 2])
    rmax = max(c.px(max_size, img) * 0.5, 2.0)
    rmin = max(c.px(min_size, img) * 0.5, 0.8)
    s_small = max(rmin * 0.6, 0.8)
    s_large = max(rmax * 1.2, s_small * 2.5)
    score = np.zeros((H, W), np.float32)
    for ch, sign in ((lum, -1.0), (redn, 1.0)):
        dog = gaussian(ch, s_small) - gaussian(ch, s_large)
        mad = float(np.median(np.abs(dog - np.median(dog)))) * 1.4826 + 1e-6
        score = np.maximum(score, np.maximum(sign * dog, 0) / mad)
    thr = 7.0 - 4.5 * float(np.clip(sensitivity, 0, 1))
    cand = (score > thr).astype(np.uint8)
    if skin_only:
        skin = build_mask({"type": "ai_skin"}, img, c)
        cand = cand * (skin > 0.3).astype(np.uint8)
    n, lbl, stats, cent = cv2.connectedComponentsWithStats(cand, connectivity=8)
    hole = np.zeros((H, W), np.uint8)
    spots = []
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        if area < max(2, math.pi * rmin ** 2 * 0.3) or max(bw, bh) > 2 * rmax * 1.3:
            continue
        if area / max(bw * bh, 1) < 0.35 or max(bw, bh) / max(min(bw, bh), 1) > 2.5:
            continue
        hole[lbl == i] = 1
        spots.append({"center": [float(cent[i][0] / W), float(cent[i][1] / H)], "radius_px": float(0.5 * max(bw, bh))})
    return hole, spots


@op("blemish_auto", 8, "linear", category="retouch", sensitivity=0.5, max_size=24.0, min_size=3.0, skin_only=True,
    radius=3.0, method="telea", grow=2.0, feather=2.0, choices={"method": ["telea", "ns", "lama"]})
def blemish_auto(img, p, ctx: Ctx):
    """Auto-detect small skin blemishes / dust specks and inpaint them."""
    dom = ctx.domain if ctx.domain in ("linear", "display") else "linear"
    hole, spots = detect_blemishes(img, ctx, p["sensitivity"], p["max_size"], p["min_size"], p["skin_only"])
    ctx.note(f"blemish_auto: {len(spots)} spots")
    if not spots:
        return img
    return inpaint_region(img, hole.astype(np.float32), dom, _ctx(ctx, dom), p["radius"], p["method"], p["grow"], p["feather"])


# --------------------------------------------------------------------------- skin smoothing
@op("skin_smooth", 18, "display", category="retouch", amount=0.5, radius=6.0, texture=0.9, smooth_eps=0.03,
    use_ai_mask=True, even_tone=0.3)
def skin_smooth(img, p, ctx: Ctx):
    """Frequency separation limited to the AI skin mask: the low layer is edge-aware smoothed (guided filter), the
    high layer (pores, texture) is kept at `texture` strength, so skin looks smooth but not plastic."""
    amount = float(np.clip(p["amount"], 0, 1))
    if amount <= 0:
        return img
    c = _ctx(ctx, "display")
    skin = build_mask({"type": "ai_skin", "feather": 3.0}, img, c) if p["use_ai_mask"] else np.ones(img.shape[:2], np.float32)
    if skin.max() <= 0:
        return img
    sig = max(c.px(p["radius"], img), 1.0)
    wgt = np.clip(skin * 1.5, 0.0, 1.0)                  # normalised convolution: background never leaks into the skin layers
    den_raw = gaussian(wgt, sig)[..., None]
    low = np.stack([gaussian(img[..., k] * wgt, sig) for k in range(3)], -1) / np.maximum(den_raw, 1e-3)
    low = np.where(den_raw > 0.01, low, img)          # extrapolated into the surroundings so edges never mix in
    high = img - low
    eps = float(p["smooth_eps"]) ** 2
    low_s = fast_guided(low, low, int(max(2, round(sig * 2.0))), eps)
    even = float(p["even_tone"])
    if even > 0:   # also pull the colour of the low layer toward its large-scale mean (blotchy redness)
        den3 = np.maximum(gaussian(wgt, sig * 3.0), 1e-3)[..., None]
        big = np.stack([gaussian(low_s[..., k] * wgt, sig * 3.0) for k in range(3)], -1) / den3
        low_s = low_s * (1 - even * 0.5) + np.where(wgt[..., None] > 0.02, big, low_s) * (even * 0.5)
    a = (amount * skin)[..., None]
    low_o = low * (1 - a) + low_s * a
    high_o = high * (1.0 - a * (1.0 - float(p["texture"])))
    return np.clip(low_o + high_o, 0, 1).astype(np.float32)


# --------------------------------------------------------------------------- dodge & burn
@op("dodge_burn", 12, "linear", category="retouch", strokes=[], regions=[], radius=60.0, hardness=0.2, flow=1.0, ev=0.3)
def dodge_burn(img, p, ctx: Ctx):
    """Dodge/burn as masked linear exposure: every stroke {points, radius, hardness, flow, ev} adds ev stops where
    it paints (positive = dodge, negative = burn); `regions` [{mask: spec, ev}] add EV through any mask spec."""
    H, W = img.shape[:2]
    ev_map = np.zeros((H, W), np.float32)
    for st in p.get("strokes") or []:
        s = {**{"radius": p["radius"], "hardness": p["hardness"], "flow": p["flow"]}, **st}
        a = stroke_alpha(s, H, W, ctx, img, float(p["radius"]))
        ev_map += float(st.get("ev", p["ev"])) * a
    for rg in p.get("regions") or []:
        ev_map += float(rg.get("ev", p["ev"])) * build_mask(rg["mask"], img, _ctx(ctx, "linear"))
    if not np.any(ev_map):
        return img
    return (img * np.exp2(ev_map)[..., None]).astype(np.float32)


# --------------------------------------------------------------------------- face helpers
def _blend_mask(img, new, mask):
    return (img * (1.0 - mask[..., None]) + new * mask[..., None]).astype(np.float32)


@op("red_eye", 8, "linear", category="retouch", threshold=1.6, strength=1.0, channel="auto", spots=[], auto=True,
    choices={"channel": ["auto", "red", "green"]})
def red_eye(img, p, ctx: Ctx):
    """Replace red (or green/yellow animal) eye-shine inside the eye region with a dark neutral, keeping specular
    highlights (pixels where all channels are high are left alone)."""
    c = _ctx(ctx, "linear")
    H, W = img.shape[:2]
    region = np.zeros((H, W), np.float32)
    if p["auto"]:
        region = np.maximum(region, build_mask({"type": "ai_eyes", "feather": 1.0}, img, c))
    for sp in p.get("spots") or []:
        r = float(sp.get("radius", 0.01))
        region = np.maximum(region, build_mask({"type": "radial", "center": sp["center"], "radii": [r, r * W / H],
                                                "feather": 2.0}, img, c))
    if region.max() <= 0:
        return img
    R, G, B = img[..., 0], img[..., 1], img[..., 2]
    k = float(p["threshold"])
    ch = p["channel"]
    if ch == "auto":
        ch = "green" if (np.mean(G[region > 0.5]) > np.mean(R[region > 0.5]) * 1.1 if (region > 0.5).any() else False) else "red"
    if ch == "red":
        dom = R / np.maximum(np.maximum(G, B), 1e-4)
        repl_val = np.minimum(R, 0.5 * (G + B))
        out = img.copy()
        out[..., 0] = repl_val
    else:
        dom = G / np.maximum(np.maximum(R, B), 1e-4)
        out = img.copy()
        out[..., 1] = np.minimum(G, 0.5 * (R + B))
    m = _smoothstep(k, k * 1.5, dom) * region * float(p["strength"])   # specular highlights are neutral: dom ~ 1, so untouched
    return _blend_mask(img, out, np.clip(m, 0, 1))


@op("teeth_whiten", 18, "display", category="retouch", amount=0.5, lift=0.04, hue_range=[55.0, 80.0, 110.0, 135.0])
def teeth_whiten(img, p, ctx: Ctx):
    """Inside the teeth mask: reduce chroma of the yellow band (Oklch hue ~60-130 deg) and lift lightness."""
    c = _ctx(ctx, "display")
    m = build_mask({"type": "ai_teeth", "feather": 1.5}, img, c)
    if m.max() <= 0:
        return img
    from ...masks.parametric import trapezoid

    lch = color.to_oklch(img, "display")
    yellow = trapezoid(lch[..., 2], p["hue_range"], smooth=True, circular=True)
    amt = float(np.clip(p["amount"], 0, 1))
    out = lch.copy()
    out[..., 1] = lch[..., 1] * (1.0 - 0.85 * amt * (0.35 + 0.65 * yellow))
    out[..., 0] = np.clip(lch[..., 0] + float(p["lift"]) * amt * (1.0 - lch[..., 0]) * 4.0, 0, 1)
    return _blend_mask(img, np.clip(color.from_oklch(out, "display"), 0, 1), m)


@op("eye_enhance", 18, "display", category="retouch", amount=0.5, contrast=0.35, saturation=0.25, sclera=0.25,
    sharpen=0.5)
def eye_enhance(img, p, ctx: Ctx):
    """Eye pop: local contrast + sharpening and saturation on the iris/eye region, brighten and de-redden the sclera."""
    c = _ctx(ctx, "display")
    m = build_mask({"type": "ai_eyes", "feather": 1.5}, img, c)
    if m.max() <= 0:
        return img
    amt = float(np.clip(p["amount"], 0, 1))
    sig = max(c.px(3.0, img), 0.8)
    blur = np.stack([gaussian(img[..., k], sig) for k in range(3)], -1)
    detail = img - blur
    out = img + detail * (float(p["contrast"]) + float(p["sharpen"])) * amt
    lch = color.to_oklch(np.clip(out, 0, 1), "display")
    sclera = _smoothstep(0.7, 0.9, lch[..., 0]) * (1.0 - _smoothstep(0.03, 0.09, lch[..., 1]))
    iris = (1.0 - sclera) * _smoothstep(0.05, 0.25, lch[..., 0]) * (1.0 - _smoothstep(0.7, 0.9, lch[..., 0]))
    lch[..., 1] = lch[..., 1] * (1.0 + float(p["saturation"]) * amt * iris - 0.6 * amt * sclera)
    lch[..., 0] = np.clip(lch[..., 0] + float(p["sclera"]) * amt * 0.06 * sclera, 0, 1)
    out = np.clip(color.from_oklch(lch, "display"), 0, 1)
    return _blend_mask(img, out, m)


# --------------------------------------------------------------------------- lens blur
def _disc_kernel(r: float) -> np.ndarray:
    R = max(int(math.ceil(r)), 1)
    y, x = np.mgrid[-R:R + 1, -R:R + 1].astype(np.float32)
    d = np.hypot(x, y)
    k = np.clip(r + 0.5 - d, 0.0, 1.0)   # anti-aliased disc
    return k / k.sum()


def disc_blur(img: np.ndarray, r: float) -> np.ndarray:
    """Uniform disc (bokeh) blur of radius r px; large radii are computed on a downscaled copy."""
    if r < 0.6:
        return img
    s = min(1.0, 10.0 / r)
    h, w = img.shape[:2]
    if s < 1.0:
        small = cv2.resize(img, (max(2, int(w * s)), max(2, int(h * s))), interpolation=cv2.INTER_AREA)
        out = cv2.filter2D(small, -1, _disc_kernel(r * s), borderType=cv2.BORDER_REFLECT)
        return cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.filter2D(img, -1, _disc_kernel(r), borderType=cv2.BORDER_REFLECT)


def layered_lens_blur(img: np.ndarray, coc: np.ndarray, levels: int = 6, highlight_boost: float = 0.0) -> np.ndarray:
    """Gather lens blur with a per-pixel circle-of-confusion radius map `coc` (px). Layer k blurs with radius r_k using only
    pixels that are at least ~half as blurry (so sharp subjects never bleed into the background), normalised
    by the contributing weight; layers are mixed by triangular weights of coc."""
    rmax = float(coc.max())
    if rmax < 0.6:
        return img
    radii = np.linspace(0.0, rmax, levels + 1)
    if highlight_boost > 0:   # bokeh balls: highlight-weighted averaging (weight g) inside each disc
        g = (1.0 + highlight_boost * 3.0 * _smoothstep(0.7, 1.5, img.mean(2))).astype(np.float32)
    else:
        g = np.ones(img.shape[:2], np.float32)
    out = np.zeros_like(img)
    wsum = np.zeros(img.shape[:2], np.float32)
    for k, rk in enumerate(radii):
        lo = radii[k - 1] if k > 0 else rk - 1.0
        hi = radii[k + 1] if k < levels else rk + 1.0
        w_k = np.where(coc <= rk, np.clip((coc - lo) / max(rk - lo, 1e-6), 0, 1), np.clip((hi - coc) / max(hi - rk, 1e-6), 0, 1))
        if k == levels:
            w_k = np.clip((coc - lo) / max(rk - lo, 1e-6), 0, 1)
        if k == 0:
            w_k = np.clip(1.0 - coc / max(radii[1], 1e-6), 0, 1)
        if not np.any(w_k > 1e-4):
            continue
        if k == 0:
            layer = img
        else:
            valid = _smoothstep(0.35 * rk, 0.65 * rk, coc).astype(np.float32) * g
            num = disc_blur(np.concatenate([img * valid[..., None], valid[..., None]], 2), rk)
            den = num[..., 3:4]
            layer = np.where(den > 2e-3, num[..., :3] / np.maximum(den, 2e-3), img)
        out += layer * w_k[..., None]
        wsum += w_k
    out /= np.maximum(wsum, 1e-4)[..., None]
    return out.astype(np.float32)


@op("background_blur", 15, "linear", category="retouch", radius=18.0, source="subject", focus=0.2, focus_range=0.12,
    falloff=0.25, foreground_blur=True, levels=6, highlight_boost=0.3, mask=None, edge_grow=1.0,
    choices={"source": ["subject", "depth", "mask"]})
def background_blur(img, p, ctx: Ctx):
    """Lens blur (disc bokeh) of the background: radius map from the subject mask (source=subject), a depth map
    (source=depth: pixels away from `focus` +- `focus_range` blur up to `radius`) or an explicit mask spec (source=mask)."""
    c = _ctx(ctx, "linear" if ctx.domain not in ("linear", "display") else ctx.domain)
    rmax = max(c.px(p["radius"], img), 0.0)
    if rmax < 0.6:
        return img
    if p["source"] == "depth":
        from ...masks.ai import depth_map

        d = depth_map(img, c)
        dist = d - float(p["focus"])
        if not p["foreground_blur"]:
            dist = np.maximum(dist, 0)
        t = _smoothstep(float(p["focus_range"]), float(p["focus_range"]) + max(float(p["falloff"]), 1e-3), np.abs(dist))
        coc = rmax * t
    else:
        spec = p["mask"] if (p["source"] == "mask" and p.get("mask")) else {"type": "ai_subject"}
        subj = build_mask(spec, img, c)
        g = int(round(c.px(p["edge_grow"], img)))
        if g > 0:   # grow the protected subject slightly so its edge pixels stay sharp
            subj = cv2.dilate(subj, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1)))
        coc = rmax * (1.0 - subj)
    dom = c.domain
    work = img if dom == "linear" else color.srgb_eotf(np.clip(img, 0, 1))
    res = layered_lens_blur(work.astype(np.float32), coc.astype(np.float32), int(p["levels"]), float(p["highlight_boost"]))
    return (res if dom == "linear" else color.srgb_oetf(np.clip(res, 0, None))).astype(np.float32)


# --------------------------------------------------------------------------- sky
@op("sky_enhance", 18, "display", category="retouch", darken=0.18, saturation=0.3, clouds=0.35, gradient=0.6, cloud_radius=18.0)
def sky_enhance(img, p, ctx: Ctx):
    """Inside the AI sky mask: graduated darkening (stronger toward the top), blue saturation, and cloud clarity
    (local contrast on lightness)."""
    c = _ctx(ctx, "display")
    m = build_mask({"type": "ai_sky", "feather": 2.0}, img, c)
    if m.max() <= 0:
        return img
    H, W = img.shape[:2]
    lch = color.to_oklch(img, "display")
    ys = (np.arange(H, dtype=np.float32) + 0.5) / H
    top_w = 1.0 - float(p["gradient"]) * _smoothstep(0.0, 0.7, ys)[:, None]
    bluew = _smoothstep(200.0, 235.0, lch[..., 2]) * (1.0 - _smoothstep(270.0, 300.0, lch[..., 2]))
    L = lch[..., 0]
    sig = max(c.px(p["cloud_radius"], img), 1.0)
    Lb = fast_guided(L, gaussian(L, sig * 0.5), int(max(2, sig)), 4e-3)
    detail = L - Lb
    L2 = L - float(p["darken"]) * top_w * (0.3 + 0.7 * bluew) * L + float(p["clouds"]) * detail * (1.0 - bluew * 0.5)
    C2 = lch[..., 1] * (1.0 + float(p["saturation"]) * top_w * bluew)
    out = np.clip(color.from_oklch(np.stack([np.clip(L2, 0, 1), C2, lch[..., 2]], -1), "display"), 0, 1)
    return _blend_mask(img, out, m)


# --------------------------------------------------------------------------- liquify
@op("liquify", 8, "linear", category="retouch", warps=[], radius=120.0, strength=0.5)
def liquify(img, p, ctx: Ctx):
    """Mesh-free liquify: warps [{type: push|pinch|bloat|twirl, center, radius (px@2000), strength, dx, dy}] accumulate an
    inverse-mapping displacement field (smoothstep falloff) sampled with cv2.remap (cubic). push moves content by
    (dx, dy) (normalised); pinch contracts (strength>0) / bloat expands towards `center`; twirl rotates by strength*pi."""
    warps = p.get("warps") or []
    if not warps:
        return img
    H, W = img.shape[:2]
    dx_f = np.zeros((H, W), np.float32)
    dy_f = np.zeros((H, W), np.float32)
    for wp in warps:
        cx, cy = float(wp["center"][0]) * W, float(wp["center"][1]) * H
        r = max(ctx.px(float(wp.get("radius", p["radius"])), img), 2.0)
        s = float(wp.get("strength", p["strength"]))
        kind = wp.get("type", "push")
        x0, x1 = max(int(cx - r), 0), min(int(cx + r) + 2, W)
        y0, y1 = max(int(cy - r), 0), min(int(cy + r) + 2, H)
        if x1 <= x0 or y1 <= y0:
            continue
        gy, gx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        ux, uy = gx - cx, gy - cy
        d = np.hypot(ux, uy)
        fall = _smoothstep(r, 0.0, d)   # 1 at centre -> 0 at radius
        if kind == "push":
            ddx = -float(wp.get("dx", 0.0)) * W * fall * s
            ddy = -float(wp.get("dy", 0.0)) * H * fall * s
        elif kind in ("pinch", "bloat"):
            sg = s if kind == "pinch" else -s
            ddx, ddy = ux * sg * fall, uy * sg * fall
        elif kind == "twirl":
            ang = s * math.pi * fall
            cs, sn = np.cos(ang), np.sin(ang)
            ddx, ddy = ux * cs - uy * sn - ux, ux * sn + uy * cs - uy
        else:
            raise ValueError(f"liquify: unknown warp type '{kind}'")
        dx_f[y0:y1, x0:x1] += ddx
        dy_f[y0:y1, x0:x1] += ddy
    gy, gx = np.mgrid[0:H, 0:W].astype(np.float32)
    return cv2.remap(img.astype(np.float32), gx + dx_f, gy + dy_f, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
