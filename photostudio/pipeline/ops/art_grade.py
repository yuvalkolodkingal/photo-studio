"""Table-driven display-referred grading engine shared by the film-stock, photographer and cinema ops.

`grade(img, spec, rng, ctx, intensity)` applies, in a fixed order: exposure -> warmth -> colour matrix -> B&W mix ->
saturation / hue rules -> tone curve (S gain, lifted black, capped white, knee) -> per-channel curves -> highlight desat ->
tints (shadow / mid / highlight, scaled by `intensity`) -> B&W toning -> local contrast / sharpen -> halation / bloom ->
fog -> soften -> vignette -> grain (scaled by `intensity`). `intensity` multiplies tint weights and grain, as in the spec."""
from __future__ import annotations

import cv2
import numpy as np

from . import art_common as ac
from .art_common import F32, luma

# ----------------------------------------------------------------------------- helpers used by several look ops
def halate_display(img: np.ndarray, k: float, thr: float, radius: float, tint=(1.0, 0.30, 0.08), knee: float = 0.35) -> np.ndarray:
    """Halation on a display image: decode to linear, add the red-orange glow, re-encode (slot-A op is the physical version)."""
    if k <= 0:
        return img
    li = ac.lin(img)
    g = ac.halation_glow(li, thr, knee, max(1.0, radius), tint)
    return ac.enc(li + k * g)


def bloom_display(img: np.ndarray, thr: float, radius: float, k: float) -> np.ndarray:
    if k <= 0:
        return img
    m = ac.smoothstep(thr, 1.0, luma(img))[..., None]
    g = 0.6 * ac.blur(img * m, radius) + 0.4 * ac.blur(img * m, radius * 3)
    return ac.screen(img, np.clip(g * k * 2.0, 0, 1))


def sky_mask(img: np.ndarray, feather: float = 20.0, hue=(190.0, 250.0), min_sat: float = 0.15, min_lum: float = 0.35) -> np.ndarray:
    """Soft mask of blue sky from the *colour* source (before any B&W conversion)."""
    hh = ac.hsv(img)
    centre, half = (hue[0] + hue[1]) / 2.0, (hue[1] - hue[0]) / 2.0
    m = ac.ang_weight(hh[..., 0], centre, half, 12.0) * ac.smoothstep(min_sat * 0.6, min_sat * 1.6, hh[..., 1]) \
        * ac.smoothstep(min_lum - 0.1, min_lum + 0.1, luma(img))
    return ac.blur(m.astype(F32), max(1.0, feather))


def skin_mask(img: np.ndarray) -> np.ndarray:
    hh = ac.hsv(img)
    return (ac.ang_weight(hh[..., 0], 24.0, 16.0, 12.0) * ac.smoothstep(0.12, 0.25, hh[..., 1]) * (1 - ac.smoothstep(0.6, 0.8, hh[..., 1]))).astype(F32)


def clahe_luma(img: np.ndarray, clip: float = 2.0, grid: int = 8, amount: float = 1.0) -> np.ndarray:
    """CLAHE on lightness blended at `amount` (applied as a luminance ratio so colours stay put)."""
    if amount <= 0:
        return img
    l = luma(img)
    c = cv2.createCLAHE(clipLimit=float(clip), tileGridSize=(grid, grid))
    l2 = c.apply(ac.to_u8(l)).astype(F32) / 255.0
    l2 = l * (1 - amount) + l2 * amount
    return np.clip(img * ((l2 + 0.01) / (l + 0.01))[..., None], 0, 1)


def hdr_tonemap(img: np.ndarray, compress: float = 0.65, detail: float = 1.5, sigma: float = 25.0) -> np.ndarray:
    """Base/detail local tone-mapping in log-luminance (HDR-landscape look): compress the bilateral base, boost the detail."""
    li = ac.lin(img)
    L = np.maximum(luma(li), 1e-4)
    logL = np.log2(L)
    small, s = ac.work_scale(logL[..., None].repeat(3, -1), 900)
    base = cv2.bilateralFilter(np.ascontiguousarray(small[..., 0]), 9, 1.0, max(2.0, sigma * s * 0.5))
    base = base if s >= 0.999 else cv2.resize(base, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)
    pivot = np.median(base)
    out_log = pivot + (base - pivot) * compress + (logL - base) * detail
    ratio = np.power(2.0, out_log - logL)[..., None]
    return np.clip(ac.enc(li * ratio), 0, 1)


def angular_lerp(h: np.ndarray, target: float, amount: np.ndarray | float) -> np.ndarray:
    d = ((target - h + 180.0) % 360.0) - 180.0
    return (h + d * amount) % 360.0


# ----------------------------------------------------------------------------- engine
def grade(img: np.ndarray, s: dict, rng, ctx, intensity: float = 1.0, grain_scale: float = 1.0) -> np.ndarray:
    x = np.asarray(img, dtype=F32)
    px = lambda v: max(0.0, float(ctx.px(v, x)))
    if s.get("ev"):
        x = ac.enc(ac.lin(x) * 2.0 ** float(s["ev"]))
    x = np.clip(x, 0, 1)
    if s.get("warm"):
        x = np.clip(ac.warm(x, float(s["warm"])), 0, 1)
    if s.get("matrix") is not None:
        x = np.clip(color_matrix(x, s["matrix"]), 0, 1)
    mono = s.get("bw") is not None
    if mono:
        if s.get("bw_linear"):
            g = ac.enc(np.maximum(ac.bw_mix(ac.lin(x), s["bw"]), 0))
        else:
            g = np.maximum(ac.bw_mix(x, s["bw"]), 0)
        x = ac.gray3(np.clip(g, 0, 1))
    else:
        if s.get("sat") is not None:
            x = ac.saturate(x, float(s["sat"]))
        if s.get("hsl"):
            x = np.clip(ac.hsl_adjust(np.clip(x, 0, 1), s["hsl"]), 0, 1)
    # tone curve
    if s.get("gain") or s.get("black") or s.get("white", 1.0) != 1.0:
        gain, bl, wh = float(s.get("gain", 0)), float(s.get("black", 0)), float(s.get("white", 1.0))
        if s.get("curve_mode", "rgb") == "luma" and not mono:
            l = luma(x)
            l2 = ac.tone(np.clip(l, 0, 1), gain, bl, wh, float(s.get("mid", 0.5)))
            x = x * ((l2 + 0.01) / (l + 0.01))[..., None]
        else:
            x = ac.tone(np.clip(x, 0, 1), gain, bl, wh, float(s.get("mid", 0.5)))
    if s.get("knee"):
        k0, k1 = s["knee"]   # soft knee: above k0 compress toward k1 ceiling
        x = np.where(x > k0, k0 + (k1 - k0) * (1 - np.exp(-(x - k0) / max(k1 - k0, 1e-3))), x)
    if s.get("curves"):
        c = s["curves"]
        for i, ch in enumerate("rgb"):
            if ch in c:
                x[..., i] = ac.apply_curve(x[..., i], ac.curve_lut(c[ch]))
        if "l" in c:
            x = np.stack([ac.apply_curve(x[..., i], ac.curve_lut(c["l"])) for i in range(3)], -1)
    x = np.clip(x, 0, 1)
    if s.get("highlight_desat"):
        st, am = s["highlight_desat"]
        l = luma(x)[..., None]
        wgt = ac.smoothstep(st, 1.0, l) * am
        x = x * (1 - wgt) + l * wgt
    # tints (scaled by intensity)
    I = float(intensity)
    if s.get("shadow_tint"):
        c, a, e = s["shadow_tint"]
        x = ac.tint_toward(x, c, a * I, 1.0 - ac.smoothstep(0.0, e, luma(x)))
    if s.get("mid_tint"):
        c, a = s["mid_tint"]
        x = ac.tint_toward(x, c, a * I, 1.0 - np.abs(2 * luma(x) - 1))
    if s.get("high_tint"):
        c, a, st = s["high_tint"]
        x = ac.tint_toward(x, c, a * I, ac.smoothstep(st, 1.0, luma(x)))
    if s.get("toning"):      # B&W toning: gradient shadow -> highlight colour
        sh, hi, a = s["toning"]
        lut = ac.make_lut([sh, hi])
        x = x * (1 - a * I) + ac.apply_lut(np.clip(luma(x), 0, 1), lut) * (a * I)
    if s.get("local"):
        sg, am = s["local"]
        x = ac.local_contrast(x, px(sg), float(am))
    if s.get("sharpen"):
        sg, am = s["sharpen"]
        x = ac.unsharp(x, max(0.5, px(sg)), float(am))
    x = np.clip(x, 0, 1)
    if s.get("halation"):
        k, thr, rad = s["halation"]
        x = halate_display(x, float(k), float(thr), px(rad))
    if s.get("bloom"):
        thr, rad, k = s["bloom"]
        x = bloom_display(x, float(thr), px(rad), float(k))
    if s.get("fog"):
        a, c = s["fog"]
        f = float(a) * (1 - luma(x))[..., None]
        x = x * (1 - f) + ac.rgb(c) * f
    if s.get("soften"):
        x = x * (1 - 0.5) + ac.blur(x, px(s["soften"])) * 0.5 if s["soften"] > 0 else x
    if s.get("vignette"):
        st, mid, fe = s["vignette"]
        x = ac.apply_vignette(x, float(st) * min(I, 1.5) if I else 0.0, float(mid), 0.3, float(fe))
    if s.get("grain"):
        g = dict(s["grain"])
        amt = float(g.get("amount", 0.03)) * float(grain_scale) * I
        if amt > 0:
            x = ac.add_grain(x, rng, amt, max(0.3, px(g.get("size", 1.0))), float(g.get("chroma", 0.0)), float(g.get("roughness", 0.4)),
                             float(g.get("midtone_bias", 0.6)), float(g.get("shadow_bias", 0.0)), float(g.get("clump", 0.0)))
    return np.clip(np.nan_to_num(x), 0, 1).astype(F32)


def color_matrix(x: np.ndarray, m) -> np.ndarray:
    m = np.asarray(m, F32)
    return (x.reshape(-1, 3) @ m.T).reshape(x.shape)
