"""Colour ops: colour balance / grading, saturation & vibrance, HSL mixer, colour zones, channel mixer,
monochrome, split tone, 3D LUTs (.cube / Hald CLUT), temperature shift, invert.

Registered ops (stage / domain)
    color_balance 13 linear   4-way tints (shadows/midtones/highlights/global with smooth log-luminance masks),
                              ASC CDL slope/offset/power, luminance contrast around a fulcrum, linear chroma,
                              Oklch hue shift / saturation / vibrance / brilliance
    saturation    13 linear   Oklch chroma scaling (gated so neutrals stay neutral)
    vibrance      13 linear   chroma gain fading with chroma, skin-hue protection
    hsl_mixer     13 linear   8 bands (red..magenta) hue/sat/lum in Oklch, raised-cosine partition of unity
    color_zones   13 linear   hue (or lightness/chroma) indexed curves for L / C / h
    channel_mixer 13 linear   3x3 matrix
    monochrome    13 linear   channel weights, named colour filters and film-ish spectral mixes
    color_grade   18 display  lift/gamma/gain/offset wheels (hue, chroma, luma) + contrast + saturation
    split_tone    18 display  shadow/highlight tint with balance and softness (Oklab)
    lut3d         18 display  .cube (1D/3D, DOMAIN_MIN/MAX) or Hald CLUT PNG, tetrahedral, strength blend
    temperature   18 display  creative warm/cool + tint shift (CAT in linear sRGB)
    invert        18 display  negative

Public helpers (other modules import these)
    Lut                                 dataclass(kind '1d'|'3d', table, domain_min, domain_max, title)
    load_cube(path) -> Lut              Adobe/Resolve/Iridas .cube incl. DOMAIN_MIN/MAX, 1D and 3D
    write_cube(path, lut, title=None)   writes a 3D (or 1D) .cube, red index fastest
    identity_lut(size=33) -> Lut
    load_hald(path_or_array) -> Lut     Hald CLUT image (level L: L^3 x L^3 px -> L^2 sized LUT)
    make_hald_identity(level=8) -> float32 image
    lut_from_function(fn, size=33)      bake any display->display image function into a 3D LUT
    apply_cube(img, lut, strength=1.0, interp='tetrahedral')  apply a Lut (display-referred img, 0..1)
    resolve_lut_path(name) -> Path      search data/luts, $PHOTOSTUDIO_LUTS, ~/.local/share/photostudio/luts
    hsv_hue_to_oklch / oklch_hue_to_hsv  hue wheel conversion (0=red, 120=green, 240=blue as in HSV)
    skin_weight(img, domain)            0..1 soft skin-tone likelihood (Oklch hue/chroma/lightness window)
    wheel_to_rgb(hue, chroma, luma)     colour-wheel triple -> linear RGB multiplier (luminance 1)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.interpolate import PchipInterpolator

from ... import color
from ...util import data_home, imread_display, package_dir
from ..registry import Ctx, op

GREY = color.MIDDLE_GREY
LOG2_GREY = float(np.log2(GREY))


def _sig(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def _zero(*vals) -> bool:
    return all(abs(float(v)) < 1e-9 for v in vals)


def _mix(orig, new, s):
    s = float(s)
    return new if s >= 1.0 else (orig + (new - orig) * np.float32(s)).astype(np.float32)


# =========================================================================== hue wheel helpers
def _build_hue_tables():
    hs = np.arange(0.0, 361.0, 3.0)
    h6 = hs / 60.0
    c = np.ones_like(hs)
    x = 1 - np.abs(h6 % 2 - 1)
    sector = np.floor(h6).astype(int) % 6
    r = np.choose(sector, [c, x, 0 * c, 0 * c, x, c])
    g = np.choose(sector, [x, c, c, x, 0 * c, 0 * c])
    b = np.choose(sector, [0 * c, 0 * c, x, c, c, x])
    rgb = np.stack([r, g, b], -1).astype(np.float32)
    lab = color.display_to_oklab(rgb)
    ok = np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360.0
    ok = np.unwrap(np.radians(ok)) * 180.0 / np.pi
    # tiny non-monotone wiggles near blue would break the inverse interpolation: force strictly increasing
    ok = np.maximum.accumulate(ok) + np.arange(len(ok)) * 1e-6
    return hs, ok


_HSV_H, _OK_H = _build_hue_tables()


def hsv_hue_to_oklch(h):
    """HSV-style hue (0 red, 60 yellow, 120 green, 180 cyan, 240 blue, 300 magenta) -> Oklch hue degrees."""
    h = np.asarray(h, dtype=np.float64) % 360.0
    return np.interp(h, _HSV_H, _OK_H) % 360.0


def oklch_hue_to_hsv(hk):
    hk = np.asarray(hk, dtype=np.float64)
    base = _OK_H[0]
    x = (hk - base) % 360.0 + base
    return np.interp(x, _OK_H, _HSV_H) % 360.0


def wheel_to_rgb(hue: float, chroma: float, luma: float = 0.0, space: str = "rec2020") -> np.ndarray:
    """Colour-wheel triple -> RGB multiplier with luminance 1. hue: HSV degrees, chroma: 0..100 (100 = Oklab C 0.12),
    luma is NOT included (callers apply 2**luma_stops)."""
    C = float(chroma) / 100.0 * 0.12
    if C <= 0:
        return np.ones(3, np.float32)
    h = np.radians(float(hsv_hue_to_oklch(hue)))
    lab = np.array([[[0.75, C * np.cos(h), C * np.sin(h)]]], np.float32)
    rgb = color.oklab_to_lin(lab, "rec2020" if space == "rec2020" else "srgb")[0, 0]
    rgb = np.maximum(rgb, 0.02)
    y = float(color.luminance(rgb[None, None], "rec2020" if space == "rec2020" else "srgb")[0, 0])
    return (rgb / y).astype(np.float32)


def _wheel(w):
    """Accept [hue, chroma, luma] or {'hue','chroma','luma'}; returns (hue, chroma, luma)."""
    if w is None:
        return 0.0, 0.0, 0.0
    if isinstance(w, dict):
        return float(w.get("hue", 0)), float(w.get("chroma", 0)), float(w.get("luma", 0))
    w = list(w) + [0.0] * (3 - len(w))
    return float(w[0]), float(w[1]), float(w[2])


# =========================================================================== Oklab helpers
def _lab(img, domain):
    return color.lin_to_oklab(img, "rec2020") if domain == "linear" else color.display_to_oklab(np.clip(img, 0, 1))


def _from_lab(lab, domain):
    out = color.oklab_to_lin(lab, "rec2020") if domain == "linear" else color.oklab_to_display(lab)
    if domain == "linear":
        if out.min() < -1e-5:
            out = color.gamut_compress_negatives(out)
    else:
        out = np.clip(out, 0.0, 1.0)
    return out.astype(np.float32)


def _chroma_hue(lab):
    a, b = lab[..., 1], lab[..., 2]
    return np.hypot(a, b), np.degrees(np.arctan2(b, a)) % 360.0


def _scale_ab(lab, k):
    k = np.asarray(k, np.float32)
    out = lab.copy()
    out[..., 1] *= k
    out[..., 2] *= k
    return out


def _gate(C, c0=0.02):
    return color.smoothstep(0.0, c0, C).astype(np.float32)


def skin_weight(img: np.ndarray, domain: str = "linear") -> np.ndarray:
    """Soft skin likelihood from Oklch: hue window around ~55 deg (+-35), moderate chroma (0.03..0.17), mid lightness."""
    lab = _lab(img, domain)
    C, h = _chroma_hue(lab)
    L = lab[..., 0] if domain != "linear" else lab[..., 0] / (1.0 + lab[..., 0])  # compress scene L toward 0..1
    dh = np.abs((h - 55.0 + 180.0) % 360.0 - 180.0)
    wh = 1.0 - color.smoothstep(20.0, 40.0, dh)
    wc = color.smoothstep(0.02, 0.05, C) * (1.0 - color.smoothstep(0.14, 0.2, C))
    wl = color.smoothstep(0.15, 0.35, L) * (1.0 - color.smoothstep(0.8, 0.95, L))
    return (wh * wc * wl).astype(np.float32)


# =========================================================================== luminance masks (EV around grey)
def zone_masks(Y: np.ndarray, falloff: float = 1.2, shadow_pivot: float = -1.0, highlight_pivot: float = 1.0):
    """(m_shadows, m_mids, m_highlights) from luminance Y; sigmoid-in-EV, mids = remainder (darktable design)."""
    t = np.log2(np.maximum(Y, 1e-5) / GREY)
    f = max(float(falloff), 0.1)
    ms = _sig(-(t - shadow_pivot) / f)
    mh = _sig((t - highlight_pivot) / f)
    mm = np.clip(1.0 - ms - mh, 0.0, 1.0)
    return ms.astype(np.float32), mm.astype(np.float32), mh.astype(np.float32)


def _power(x, p, pivot):
    return np.sign(x) * pivot * np.power(np.abs(x) / pivot, p)


# =========================================================================== color_balance (linear)
@op("color_balance", 13, "linear", shadows=[0, 0, 0], midtones=[0, 0, 0], highlights=[0, 0, 0],
    **{"global": [0, 0, 0]}, slope=[1.0, 1.0, 1.0], offset=[0.0, 0.0, 0.0], power=[1.0, 1.0, 1.0],
    power_pivot=1.0, contrast=0.0, contrast_fulcrum=GREY, hue_shift=0.0, chroma=0.0, saturation=0.0,
    vibrance=0.0, brilliance=0.0, brilliance_shadows=0.0, brilliance_highlights=0.0, protect_skin=0.5,
    mask_falloff=1.2, category="color")
def color_balance(img, p, ctx: Ctx):
    """Scene-referred colour balance (darktable colour balance rgb design): 4-way tints (shadows ~ lift, mids ~
    gamma, highlights ~ gain, global ~ offset/white balance) as [hue, chroma 0..100, luma stops*100], ASC CDL
    slope/offset/power, contrast about a grey fulcrum, linear chroma and Oklch saturation / vibrance / brilliance."""
    x = img
    wheels = {k: _wheel(p.get(k)) for k in ("shadows", "midtones", "highlights", "global")}
    if any(not _zero(w[1], w[2]) for w in wheels.values()):
        ms = mm = mh = None
        mult = {k: wheel_to_rgb(w[0], w[1]) * np.float32(2.0 ** (w[2] / 100.0)) for k, w in wheels.items()}
        Y = color.luminance(np.maximum(x, 0.0))
        ms, mm, mh = zone_masks(Y, p["mask_falloff"])
        gain = np.ones_like(x)
        gl = mult["global"]
        gain = gain * gl.reshape(1, 1, 3)
        for key, m in (("shadows", ms), ("midtones", mm), ("highlights", mh)):
            if not _zero(wheels[key][1], wheels[key][2]):
                gain = gain * np.power(mult[key].reshape(1, 1, 3), m[..., None])
        x = x * gain
    slope, off, pw = (np.asarray(p[k], np.float32) for k in ("slope", "offset", "power"))
    if not (np.allclose(slope, 1) and np.allclose(off, 0) and np.allclose(pw, 1)):
        x = x * slope + off
        if not np.allclose(pw, 1):
            x = _power(x, pw.reshape(1, 1, 3), max(float(p["power_pivot"]), 1e-3))
    c = float(p["contrast"])
    if abs(c) > 1e-9:
        f = max(float(p["contrast_fulcrum"]), 1e-4)
        Y = color.luminance(x)
        Yn = f * np.power(np.maximum(Y, 0) / f, max(1.0 + c, 0.05))
        x = np.where((Y > 1e-6)[..., None], x * (Yn / np.maximum(Y, 1e-6))[..., None], x)
    chroma = float(p["chroma"])
    if abs(chroma) > 1e-9:  # linear chroma: scale distance to the achromatic axis (constant luminance)
        Y = color.luminance(x)[..., None]
        x = Y + (x - Y) * np.float32(max(1.0 + chroma / 100.0, 0.0))
    hs, sat, vib = float(p["hue_shift"]), float(p["saturation"]), float(p["vibrance"])
    bri, bs, bh = float(p["brilliance"]), float(p["brilliance_shadows"]), float(p["brilliance_highlights"])
    if not _zero(hs, sat, vib, bri, bs, bh):
        lab = _lab(x, "linear")
        C, h = _chroma_hue(lab)
        if hs:
            ang = np.radians(hs)
            a, b = lab[..., 1].copy(), lab[..., 2].copy()
            lab[..., 1] = a * np.cos(ang) - b * np.sin(ang)
            lab[..., 2] = a * np.sin(ang) + b * np.cos(ang)
        k = np.ones(C.shape, np.float32)
        if sat:
            k = k * max(1.0 + sat / 100.0, 0.0)
        if vib:
            k = k * _vibrance_gain(C, h, vib / 100.0, float(p["protect_skin"]), lab)
        if not _zero(sat, vib):
            lab = _scale_ab(lab, k)
        if not _zero(bri, bs, bh):
            Y = color.luminance(np.maximum(x, 0.0))
            ms, _, mh = zone_masks(Y, p["mask_falloff"])
            kb = 1.0 + 0.35 * (bri + bs * ms + bh * mh) / 100.0
            lab = lab * np.maximum(kb, 0.05)[..., None].astype(np.float32)
        x = _from_lab(lab, "linear")
    return x.astype(np.float32)


def _vibrance_gain(C, h, v, protect_skin, lab):
    """C' = C (1 + v (1 - C/Cref)); skin hues are attenuated by protect_skin."""
    cref = 0.22
    fade = 1.0 - np.clip(C / cref, 0.0, 1.0)
    amt = v * fade
    if protect_skin > 0 and v > 0:
        dh = np.abs((h - 55.0 + 180.0) % 360.0 - 180.0)
        wh = 1.0 - color.smoothstep(15.0, 45.0, dh)
        amt = amt * (1.0 - protect_skin * wh)
    return np.maximum(1.0 + amt, 0.0).astype(np.float32)


@op("saturation", 13, "linear", amount=0.0, gate=0.02, mode="oklch", choices={"mode": ["oklch", "linear"]},
    category="color")
def saturation(img, p, ctx: Ctx):
    """Saturation (-100..100). 'oklch': perceptual chroma scaling at constant Oklab lightness (neutrals gated);
    'linear': CDL-style scaling about the luminance axis."""
    a = float(p["amount"]) / 100.0
    if a == 0:
        return img
    k = max(1.0 + a, 0.0)
    if p["mode"] == "linear" or ctx.domain == "display":
        Y = (color.luminance(img, "srgb") if ctx.domain == "display" else color.luminance(img))[..., None]
        out = Y + (img - Y) * np.float32(k)
        return np.clip(out, 0, 1).astype(np.float32) if ctx.domain == "display" else out.astype(np.float32)
    lab = _lab(img, "linear")
    C, _ = _chroma_hue(lab)
    g = 1.0 + (k - 1.0) * _gate(C, float(p["gate"]))
    return _from_lab(_scale_ab(lab, g), "linear")


@op("vibrance", 13, "linear", amount=0.0, protect_skin=0.5, category="color")
def vibrance(img, p, ctx: Ctx):
    """Vibrance (-100..100): chroma boost that fades with existing chroma and spares skin hues."""
    v = float(p["amount"]) / 100.0
    if v == 0:
        return img
    lab = _lab(img, "linear")
    C, h = _chroma_hue(lab)
    return _from_lab(_scale_ab(lab, _vibrance_gain(C, h, v, float(p["protect_skin"]), lab)), "linear")


# =========================================================================== HSL mixer
BANDS = ["red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta"]
_BAND_HSV = np.array([0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 270.0, 300.0])


def band_centers_oklch() -> np.ndarray:
    return hsv_hue_to_oklch(_BAND_HSV)


def band_weights(h_ok: np.ndarray, softness: float = 1.0) -> np.ndarray:
    """Raised-cosine partition of unity over the 8 band centres (Oklch hue). Returns (8, *h.shape); sums to 1.
    softness < 1 makes plateaus around the centres (narrower cross-fades)."""
    cen = band_centers_oklch()
    order = np.argsort(cen)
    cs = cen[order]
    ext = np.concatenate([cs, [cs[0] + 360.0]])
    h = np.asarray(h_ok, np.float64) % 360.0
    h = np.where(h < cs[0], h + 360.0, h)
    seg = np.clip(np.searchsorted(ext, h, side="right") - 1, 0, len(cs) - 1)
    lo, hi = ext[seg], ext[seg + 1]
    t = (h - lo) / (hi - lo)
    s = max(float(softness), 0.05)
    t = np.clip((t - 0.5) / s + 0.5, 0.0, 1.0)
    wa = np.cos(0.5 * np.pi * t) ** 2
    wb = np.sin(0.5 * np.pi * t) ** 2
    W = np.zeros((len(cs),) + h.shape, np.float32)
    ia = order[seg % len(cs)]
    ib = order[(seg + 1) % len(cs)]
    for k in range(len(cs)):
        W[k] = np.where(ia == k, wa, 0.0) + np.where(ib == k, wb, 0.0)
    return W


_HSL_DEFAULTS = {f"{b}_{q}": 0.0 for b in BANDS for q in ("hue", "sat", "lum")}


@op("hsl_mixer", 13, "linear", gate=0.02, softness=1.0, category="color", **_HSL_DEFAULTS)
def hsl_mixer(img, p, ctx: Ctx):
    """8-band HSL mixer in Oklch (red, orange, yellow, green, aqua, blue, purple, magenta): <band>_hue (+-100 =
    +-30 deg), <band>_sat (+-100 = x0..x2 chroma), <band>_lum (+-100 = +-35 % Oklab L). Neutrals are gated."""
    hue = np.array([p[f"{b}_hue"] for b in BANDS], np.float32) * 0.3
    sat = np.array([p[f"{b}_sat"] for b in BANDS], np.float32) / 100.0
    lum = np.array([p[f"{b}_lum"] for b in BANDS], np.float32) * 0.0035
    if not (hue.any() or sat.any() or lum.any()):
        return img
    dom = ctx.domain if ctx.domain in ("linear", "display") else "linear"
    lab = _lab(img, dom)
    C, h = _chroma_hue(lab)
    g = _gate(C, float(p["gate"]))
    grid = np.arange(0.0, 360.0 + 0.5, 0.5)
    W = band_weights(grid, p["softness"])                      # (8, G): per-hue LUTs, cheap
    dh = np.tensordot(hue, W, 1)
    ds = np.tensordot(sat, W, 1)
    dl = np.tensordot(lum, W, 1)
    idx = h / 0.5
    dH = np.interp(idx, np.arange(len(grid)), dh) * g
    dS = np.interp(idx, np.arange(len(grid)), ds) * g
    dL = np.interp(idx, np.arange(len(grid)), dl) * g
    ang = np.radians(dH)
    a, b = lab[..., 1], lab[..., 2]
    ca, sa = np.cos(ang), np.sin(ang)
    k = np.maximum(1.0 + dS, 0.0)
    lab2 = np.stack([lab[..., 0] * (1.0 + dL), (a * ca - b * sa) * k, (a * sa + b * ca) * k], -1).astype(np.float32)
    return _from_lab(lab2, dom)


# =========================================================================== colour zones
def _periodic_curve(points, n=720):
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    pts = pts[np.argsort(pts[:, 0])]
    xs = np.concatenate([pts[:, 0] - 360.0, pts[:, 0], pts[:, 0] + 360.0])
    ys = np.concatenate([pts[:, 1]] * 3)
    f = PchipInterpolator(xs, ys)
    g = np.linspace(0, 360, n + 1)
    return g, f(g)


def _open_curve(points, lo, hi, n=512):
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    pts = pts[np.argsort(pts[:, 0])]
    f = PchipInterpolator(pts[:, 0], pts[:, 1], extrapolate=False)
    g = np.linspace(lo, hi, n)
    return g, f(np.clip(g, pts[0, 0], pts[-1, 0]))


@op("color_zones", 13, "linear", selector="hue", lightness=None, chroma=None, hue_shift=None, mix=1.0,
    gate=0.02, choices={"selector": ["hue", "lightness", "chroma"]}, category="color")
def color_zones(img, p, ctx: Ctx):
    """Selective colour by curves (darktable colour zones): the x axis is hue (HSV degrees, periodic), lightness
    or chroma; curves give lightness delta (-1..1 x 35 %), chroma delta (-1..1 -> x0..x2) and hue delta (degrees),
    as lists of [x, value]."""
    curves = {k: p.get(k) for k in ("lightness", "chroma", "hue_shift")}
    if all(v is None for v in curves.values()):
        return img
    dom = ctx.domain if ctx.domain in ("linear", "display") else "linear"
    lab = _lab(img, dom)
    C, h = _chroma_hue(lab)
    sel = p["selector"]
    if sel == "hue":
        x = oklch_hue_to_hsv(h)
    elif sel == "lightness":
        x = lab[..., 0]
    else:
        x = C
    out = {}
    for k, pts in curves.items():
        if pts is None:
            out[k] = 0.0
            continue
        if sel == "hue":
            g, v = _periodic_curve(pts)
            out[k] = np.interp(x, g, v)
        else:
            lo, hi = (0.0, 1.0) if sel == "lightness" else (0.0, 0.4)
            g, v = _open_curve(pts, lo, hi)
            out[k] = np.interp(np.clip(x, lo, hi), g, v)
    g = _gate(C, float(p["gate"]))
    w = float(p["mix"]) * g
    wl = float(p["mix"]) * (g if sel == "hue" else 1.0)     # neutrals have no hue: only a hue selector is gated
    ang = np.radians(out["hue_shift"] * w)
    a, b = lab[..., 1], lab[..., 2]
    ca, sa = np.cos(ang), np.sin(ang)
    k = np.maximum(1.0 + out["chroma"] * w, 0.0)
    L = lab[..., 0] * (1.0 + 0.35 * out["lightness"] * wl)
    lab2 = np.stack([L, (a * ca - b * sa) * k, (a * sa + b * ca) * k], -1).astype(np.float32)
    return _from_lab(lab2, dom)


# =========================================================================== channel mixer / monochrome
@op("channel_mixer", 13, "linear", matrix=[[1, 0, 0], [0, 1, 0], [0, 0, 1]], normalize=False, strength=1.0,
    category="color")
def channel_mixer(img, p, ctx: Ctx):
    """3x3 channel mixer: out_c = sum_j matrix[c][j] * in_j. normalize=True divides each row by its sum so
    neutral greys are unchanged."""
    m = np.asarray(p["matrix"], np.float32).reshape(3, 3).copy()
    if p.get("normalize"):
        s = m.sum(axis=1, keepdims=True)
        m = m / np.where(np.abs(s) < 1e-6, 1.0, s)
    if np.allclose(m, np.eye(3)):
        return img
    return _mix(img, color.apply_matrix(img, m), p["strength"])


FILTERS = {
    "neutral": (0.2627, 0.6780, 0.0593),
    "red": (0.90, 0.10, 0.0),
    "orange": (0.60, 0.40, 0.0),
    "yellow": (0.40, 0.60, 0.0),
    "green": (0.10, 0.90, 0.0),
    "blue": (0.0, 0.20, 0.80),
    # spectral-ish film responses (weights on linear Rec.2020, approximate)
    "panchromatic": (0.36, 0.54, 0.10),
    "orthochromatic": (0.0, 0.55, 0.45),
    "infrared": (-0.25, 1.15, 0.10),
}


@op("monochrome", 13, "linear", filter="neutral", weights=None, filter_strength=1.0, amount=1.0, tint=None,
    choices={"filter": list(FILTERS)}, category="color")
def monochrome(img, p, ctx: Ctx):
    """Black & white by channel weights on linear data: `weights` [r,g,b] or a named filter (neutral, red, orange,
    yellow, green, blue, panchromatic, orthochromatic, infrared). Weights are normalised to sum 1 (grey stays grey);
    filter_strength blends the filter with the neutral luminance weights."""
    neutral = np.asarray(FILTERS["neutral"], np.float32)
    if p.get("weights") is not None:
        w = np.asarray(p["weights"], np.float32)
    else:
        w = neutral + (np.asarray(FILTERS[p["filter"]], np.float32) - neutral) * float(p["filter_strength"])
    s = float(w.sum())
    w = w / (s if abs(s) > 1e-6 else 1.0)
    g = np.maximum(img @ w.astype(np.float32), 0.0).astype(np.float32)
    out = np.repeat(g[..., None], 3, axis=2)
    if p.get("tint"):
        t = np.asarray(p["tint"], np.float32).reshape(3)
        out = out * (t / max(float(color.luminance(t[None, None])[0, 0]), 1e-6))
    return _mix(img, out, p["amount"])


# =========================================================================== LUTs
@dataclass
class Lut:
    kind: str                       # '1d' | '3d'
    table: np.ndarray               # 1d: (N,3); 3d: (N,N,N,3) indexed [r,g,b]
    domain_min: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))
    domain_max: np.ndarray = field(default_factory=lambda: np.ones(3, np.float32))
    title: str = ""

    @property
    def size(self) -> int:
        return int(self.table.shape[0])


def identity_lut(size: int = 33) -> Lut:
    g = np.linspace(0, 1, size, dtype=np.float32)
    r, gg, b = np.meshgrid(g, g, g, indexing="ij")
    return Lut("3d", np.stack([r, gg, b], -1).astype(np.float32), title="identity")


def load_cube(path) -> Lut:
    """Parse an Adobe/Resolve .cube file (TITLE, LUT_1D_SIZE, LUT_3D_SIZE, DOMAIN_MIN/MAX; red index fastest)."""
    title, n1, n3 = "", None, None
    dmin, dmax = np.zeros(3, np.float32), np.ones(3, np.float32)
    data = []
    with open(path, "r", errors="replace") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            c = line[0]
            if c.isalpha():
                tok = line.split(None, 1)
                key = tok[0].upper()
                rest = tok[1] if len(tok) > 1 else ""
                if key == "TITLE":
                    title = rest.strip().strip('"')
                elif key == "LUT_1D_SIZE":
                    n1 = int(rest)
                elif key == "LUT_3D_SIZE":
                    n3 = int(rest)
                elif key == "DOMAIN_MIN":
                    dmin = np.array(rest.split()[:3], np.float32)
                elif key == "DOMAIN_MAX":
                    dmax = np.array(rest.split()[:3], np.float32)
                elif key in ("LUT_1D_INPUT_RANGE", "LUT_3D_INPUT_RANGE"):
                    lo, hi = (float(v) for v in rest.split()[:2])
                    dmin, dmax = np.full(3, lo, np.float32), np.full(3, hi, np.float32)
                continue
            data.append(line)
    if (n1 is None) == (n3 is None):
        raise ValueError(f"{path}: need exactly one of LUT_1D_SIZE / LUT_3D_SIZE")
    arr = np.array(" ".join(data).split(), dtype=np.float32)
    if n3:
        if arr.size != n3 ** 3 * 3:
            raise ValueError(f"{path}: expected {n3 ** 3} rows, got {arr.size / 3:g}")
        tab = np.ascontiguousarray(arr.reshape(n3, n3, n3, 3).transpose(2, 1, 0, 3))
        return Lut("3d", tab, dmin, dmax, title)
    if arr.size != n1 * 3:
        raise ValueError(f"{path}: expected {n1} rows, got {arr.size / 3:g}")
    return Lut("1d", arr.reshape(n1, 3), dmin, dmax, title)


def write_cube(path, lut: Lut, title: str | None = None) -> Path:
    path = Path(path)
    t = title if title is not None else (lut.title or path.stem)
    lines = [f'TITLE "{t}"']
    if lut.kind == "3d":
        lines.append(f"LUT_3D_SIZE {lut.size}")
        body = lut.table.transpose(2, 1, 0, 3).reshape(-1, 3)
    else:
        lines.append(f"LUT_1D_SIZE {lut.size}")
        body = lut.table
    if not (np.allclose(lut.domain_min, 0) and np.allclose(lut.domain_max, 1)):
        lines.append("DOMAIN_MIN " + " ".join(f"{v:.6f}" for v in lut.domain_min))
        lines.append("DOMAIN_MAX " + " ".join(f"{v:.6f}" for v in lut.domain_max))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
        np.savetxt(f, body, fmt="%.6f")
    return path


def make_hald_identity(level: int = 8) -> np.ndarray:
    n = level * level
    s = level ** 3
    idx = np.arange(s * s)
    r, g, b = idx % n, (idx // n) % n, idx // (n * n)
    img = np.stack([r, g, b], -1).astype(np.float32) / (n - 1)
    return img.reshape(s, s, 3)


def load_hald(src) -> Lut:
    """Hald CLUT (path or HxWx3 float array 0..1). Level L image is L^3 x L^3 px -> LUT size L^2."""
    img = imread_display(src) if isinstance(src, (str, Path)) else np.asarray(src, np.float32)
    s = img.shape[0]
    if img.shape[0] != img.shape[1]:
        raise ValueError("Hald CLUT must be square")
    level = int(round(s ** (1.0 / 3.0)))
    n = level * level
    if level ** 3 != s:
        raise ValueError(f"not a Hald CLUT: side {s} is not a perfect cube")
    tab = img.reshape(n, n, n, 3).transpose(2, 1, 0, 3)
    return Lut("3d", np.ascontiguousarray(tab, np.float32), title=f"hald{level}")


def lut_from_function(fn, size: int = 33) -> Lut:
    """Bake an image function (display HxWx3 -> display HxWx3) into a 3D LUT by running it on an identity grid."""
    ident = identity_lut(size).table                       # [r,g,b]
    grid = np.ascontiguousarray(ident.transpose(2, 1, 0, 3)).reshape(size * size, size, 3)  # r fastest on x
    out = np.asarray(fn(grid), np.float32).reshape(size, size, size, 3)  # [b,g,r]
    return Lut("3d", np.ascontiguousarray(out.transpose(2, 1, 0, 3)), title="baked")


def _tetra(flat: np.ndarray, N: int, p: np.ndarray) -> np.ndarray:
    i = np.minimum(np.floor(p).astype(np.int64), N - 2)
    i = np.maximum(i, 0)
    f = (p - i).astype(np.float32)
    fr, fg, fb = f[:, 0], f[:, 1], f[:, 2]
    base = (i[:, 0] * N + i[:, 1]) * N + i[:, 2]
    sr, sg, sb = N * N, N, 1
    c0 = flat[base]
    c7 = flat[base + sr + sg + sb]
    out = np.empty_like(c0)
    cases = [
        ((fr >= fg) & (fg >= fb), sr, sg, 0, 1, 2),
        ((fr >= fb) & (fb > fg), sr, sb, 0, 2, 1),
        ((fb > fr) & (fr >= fg), sb, sr, 2, 0, 1),
        ((fg > fr) & (fr >= fb), sg, sr, 1, 0, 2),
        ((fg >= fb) & (fb > fr), sg, sb, 1, 2, 0),
        ((fb > fg) & (fg > fr), sb, sg, 2, 1, 0),
    ]
    for m, s1, s2, a, b, c in cases:
        if not m.any():
            continue
        bi = base[m]
        c1 = flat[bi + s1]
        c2 = flat[bi + s1 + s2]
        fa, fb_, fc = f[m, a][:, None], f[m, b][:, None], f[m, c][:, None]
        out[m] = c0[m] + fa * (c1 - c0[m]) + fb_ * (c2 - c1) + fc * (c7[m] - c2)
    return out


def _trilinear(flat: np.ndarray, N: int, p: np.ndarray) -> np.ndarray:
    i = np.maximum(np.minimum(np.floor(p).astype(np.int64), N - 2), 0)
    f = (p - i).astype(np.float32)
    base = (i[:, 0] * N + i[:, 1]) * N + i[:, 2]
    out = 0.0
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = (f[:, 0] if dr else 1 - f[:, 0]) * (f[:, 1] if dg else 1 - f[:, 1]) * (f[:, 2] if db else 1 - f[:, 2])
                out = out + w[:, None] * flat[base + dr * N * N + dg * N + db]
    return out


def apply_cube(img: np.ndarray, lut: Lut, strength: float = 1.0, interp: str = "tetrahedral",
               chunk: int = 1 << 17) -> np.ndarray:
    """Apply a Lut to a display-referred float image (HxWx3). Inputs are clipped to the LUT domain.
    3D: tetrahedral (default) or trilinear interpolation; 1D: per-channel linear interpolation."""
    shape = img.shape
    x = img.reshape(-1, 3).astype(np.float32)
    dmin, dmax = lut.domain_min.astype(np.float32), lut.domain_max.astype(np.float32)
    t = np.clip((x - dmin) / np.maximum(dmax - dmin, 1e-9), 0.0, 1.0)
    if lut.kind == "1d":
        grid = np.linspace(0, 1, lut.size, dtype=np.float32)
        out = np.stack([np.interp(t[:, c], grid, lut.table[:, c]) for c in range(3)], -1)
    else:
        N = lut.size
        flat = np.ascontiguousarray(lut.table.reshape(-1, 3), dtype=np.float32)
        fn = _tetra if interp == "tetrahedral" else _trilinear
        out = np.empty_like(t)
        for s in range(0, t.shape[0], chunk):
            out[s:s + chunk] = fn(flat, N, t[s:s + chunk] * (N - 1))
    out = out.reshape(shape).astype(np.float32)
    return out if strength >= 1.0 else (img + (out - img) * np.float32(strength)).astype(np.float32)


_LUT_CACHE: dict = {}


def lut_search_dirs() -> list[Path]:
    dirs = [package_dir() / "data" / "luts", data_home() / "luts"]
    env = os.environ.get("PHOTOSTUDIO_LUTS")
    if env:
        dirs.extend(Path(d) for d in env.split(os.pathsep) if d)
    return dirs


def resolve_lut_path(name) -> Path:
    pth = Path(str(name)).expanduser()
    if pth.is_file():
        return pth
    for d in reversed(lut_search_dirs()):
        for cand in (d / str(name), d / f"{name}.cube", d / f"{name}.png"):
            if cand.is_file():
                return cand
    raise FileNotFoundError(f"LUT '{name}' not found (searched {[str(d) for d in lut_search_dirs()]})")


def load_lut(name) -> Lut:
    """Load (with cache) a .cube or Hald image by path or name."""
    p = resolve_lut_path(name)
    key = (str(p), p.stat().st_mtime_ns)
    if key not in _LUT_CACHE:
        _LUT_CACHE[key] = load_cube(p) if p.suffix.lower() == ".cube" else load_hald(p)
    return _LUT_CACHE[key]


@op("lut3d", 18, "display", path=None, strength=1.0, interp="tetrahedral",
    choices={"interp": ["tetrahedral", "trilinear"]}, category="color")
def lut3d(img, p, ctx: Ctx):
    """Apply a .cube (1D/3D) or Hald CLUT PNG/TIFF to display-referred data with tetrahedral interpolation."""
    if not p.get("path") or float(p["strength"]) <= 0:
        return img
    lut = load_lut(p["path"])
    out = apply_cube(np.clip(img, 0.0, 1.0), lut, float(p["strength"]), p["interp"])
    return np.clip(out, 0.0, 1.0)


# =========================================================================== display-referred looks
@op("color_grade", 18, "display", lift=[0, 0, 0], gamma=[0, 0, 0], gain=[0, 0, 0], offset=[0, 0, 0], contrast=0.0,
    pivot=0.435, saturation=0.0, strength=1.0, category="color")
def color_grade(img, p, ctx: Ctx):
    """Lift/gamma/gain/offset colour wheels ([hue, chroma 0..100, luma -100..100]) on display data, DaVinci-style:
    out = (in*gain + lift*(1-in) + offset)^(1/gamma), then S-contrast about `pivot` and Oklch saturation."""
    lw, gw, hw, ow = (_wheel(p[k]) for k in ("lift", "gamma", "gain", "offset"))
    c, sat = float(p["contrast"]), float(p["saturation"])
    if all(_zero(w[1], w[2]) for w in (lw, gw, hw, ow)) and _zero(c, sat):
        return img
    x = np.clip(img, 0.0, 1.0)
    gm = wheel_to_rgb(hw[0], hw[1]) * 2.0 ** (hw[2] / 100.0)
    lm = (wheel_to_rgb(lw[0], lw[1]) - 1.0) * 0.5 + lw[2] / 100.0 * 0.2
    om = (wheel_to_rgb(ow[0], ow[1]) - 1.0) * 0.25 + ow[2] / 100.0 * 0.1
    gg = wheel_to_rgb(gw[0], gw[1]) * 2.0 ** (gw[2] / 100.0)
    y = np.clip(x * gm + lm * (1.0 - x) + om, 0.0, 1.0)
    y = np.power(y, 1.0 / np.maximum(gg, 1e-3))
    if abs(c) > 1e-9:
        pv = float(np.clip(p["pivot"], 0.05, 0.95))
        e = max(1.0 + c, 0.05)
        lo = pv * np.power(np.minimum(y, pv) / pv, e)
        hi = 1.0 - (1.0 - pv) * np.power(np.minimum(1.0 - np.maximum(y, pv), 1.0 - pv) / (1.0 - pv), e)
        y = np.where(y < pv, lo, hi)
    if sat:
        lab = _lab(y, "display")
        C, _ = _chroma_hue(lab)
        y = _from_lab(_scale_ab(lab, 1.0 + (max(1.0 + sat / 100.0, 0.0) - 1.0) * _gate(C)), "display")
    return _mix(img, np.clip(y, 0, 1).astype(np.float32), p["strength"])


@op("split_tone", 18, "display", shadow_hue=220.0, shadow_sat=0.0, highlight_hue=40.0, highlight_sat=0.0,
    balance=0.0, softness=0.35, protect_ends=0.5, strength=1.0, category="color")
def split_tone(img, p, ctx: Ctx):
    """Split toning in Oklab: tint shadows and highlights (hue = HSV degrees, sat 0..100) with a luminance
    split controlled by balance (-100..100) and softness; pure black / white are protected."""
    ss, hs = float(p["shadow_sat"]), float(p["highlight_sat"])
    if ss == 0 and hs == 0:
        return img
    x = np.clip(img, 0.0, 1.0)
    lab = color.display_to_oklab(x)
    L = lab[..., 0]
    pivot = 0.5 - 0.3 * float(p["balance"]) / 100.0
    w = max(float(p["softness"]), 0.02)
    m_sh = 1.0 - color.smoothstep(pivot - w, pivot + w, L)
    m_hl = 1.0 - m_sh
    prot = float(p["protect_ends"])
    ends = (1.0 - prot) + prot * np.sqrt(np.clip(4.0 * L * (1.0 - L), 0.0, 1.0))
    a, b = lab[..., 1].copy(), lab[..., 2].copy()
    for sat, hue, m in ((ss, p["shadow_hue"], m_sh), (hs, p["highlight_hue"], m_hl)):
        if sat:
            ang = np.radians(float(hsv_hue_to_oklch(hue)))
            amt = sat / 100.0 * 0.08 * m * ends
            a += amt * np.cos(ang)
            b += amt * np.sin(ang)
    out = color.oklab_to_display(np.stack([L, a, b], -1).astype(np.float32))
    return _mix(img, np.clip(out, 0, 1).astype(np.float32), p["strength"])


@op("temperature", 18, "display", temperature=0.0, tint=0.0, category="color")
def temperature(img, p, ctx: Ctx):
    """Creative warm/cool (+-100, +warm) and tint (+-100, +magenta) shift: chromatic adaptation applied to
    display values through linear sRGB (about +-60 mired at the extremes)."""
    t, tint = float(p["temperature"]), float(p["tint"])
    if t == 0 and tint == 0:
        return img
    mired = 1e6 / 6504.0 - 0.6 * t                  # +warm: assume a bluer illuminant and adapt it away
    T = 1e6 / max(mired, 40.0)
    src_xy = color.cct_tint_to_xy(T, -tint) - color.cct_to_xy(6504.0) + color.D65_XY   # +tint = magenta cast
    M = color.XYZ_TO_SRGB @ color.cat_matrix(src_xy, color.D65_XY, "bradford") @ color.SRGB_TO_XYZ
    lin = color.srgb_eotf(np.clip(img, 0, 1))
    out = color.srgb_oetf(np.clip(color.apply_matrix(lin, M), 0.0, 1.0))
    return out.astype(np.float32)


@op("invert", 18, "display", amount=1.0, category="color")
def invert(img, p, ctx: Ctx):
    """Colour negative: out = 1 - in (amount blends)."""
    return _mix(img, (1.0 - np.clip(img, 0.0, 1.0)).astype(np.float32), p["amount"])
