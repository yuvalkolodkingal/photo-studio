"""Tonal ops: curves, levels, shadows/highlights, tone equalizer, contrast, local contrast, dehaze, HDR tone.

Registered ops (stage / domain)
    tone_curve        18 display  monotone (PCHIP) curve; rgb | luma | film | oklab modes, per-channel curves
    scene_curve       12 linear   the same curve engine in log2-EV space around middle grey (hue-safe gain)
    levels            18 display  black / white / gamma + output levels, optional auto-levels from percentiles
    highlights_shadows 12 linear  edge-aware (guided filter on log-luminance) shadow lift / highlight recovery
    tone_equalizer    12 linear   darktable design: guided-filter mask in log2 EV, 9 bands -8..0 EV -> per-pixel gain
    contrast          13 linear   luminance power law around a fulcrum (0.1845), ratio preserving
    local_contrast    14 linear   local-Laplacian (Paris 2011 / Aubry 2014 fast variant) or multiscale guided
                                  detail boost with clarity / texture / structure scale weights
    dehaze            12 linear   dark channel prior + guided-filter transmission, negative strength adds haze
    hdr_tone          12 linear   Durand-Dorsey base/detail compression with shadow lift and detail boost

Public helpers (imported by other modules)
    pchip_curve(points, n=4096, lo=0, hi=1, extend='flat') -> LUT array (n,) over [lo, hi]
    apply_lut_1d(x, lut, lo=0, hi=1)                        -> np.interp wrapper
    curve_fn(points, ...)                                    -> callable x -> y (vectorised, PCHIP)
    luma_ratio_apply(img, f, luma_fn=None)                   -> apply scalar curve f to luminance, keep ratios
    local_laplacian(L, sigma_r, alpha, level_gain=None, K=None) -> filtered single-channel image
    guided_log_mask(rgb, radius, eps, norm='rms')            -> (mask_log2, L_log2)
    dark_channel(img, size)                                  -> HxW
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.interpolate import PchipInterpolator

from ... import color
from ...filters import (collapse_pyramid, fast_guided, gaussian_pyramid, laplacian_pyramid)
from ..registry import Ctx, op

GREY = color.MIDDLE_GREY
LOG2_GREY = float(np.log2(GREY))
EPS = 1e-6


# --------------------------------------------------------------------------- curve helpers
def _clean_points(points, lo: float = 0.0, hi: float = 1.0, add_ends: bool = True) -> np.ndarray:
    pts = np.asarray(points if points is not None else [[lo, lo], [hi, hi]], dtype=np.float64).reshape(-1, 2)
    pts = pts[np.argsort(pts[:, 0], kind="stable")]
    # collapse duplicates in x (keep the last value)
    keep = np.concatenate([np.diff(pts[:, 0]) > 1e-9, [True]])
    pts = pts[keep]
    if add_ends:
        if pts[0, 0] > lo + 1e-9:
            pts = np.vstack([[lo, pts[0, 1]], pts])
        if pts[-1, 0] < hi - 1e-9:
            pts = np.vstack([pts, [hi, pts[-1, 1]]])
    if len(pts) < 2:
        pts = np.array([[lo, lo], [hi, hi]], dtype=np.float64)
    return pts


def curve_fn(points, lo: float = 0.0, hi: float = 1.0, extend: str = "flat", add_ends: bool = True):
    """Monotone-cubic (PCHIP, Fritsch-Carlson-safe: no overshoot) curve through control points.
    extend='flat' holds the end values outside [first, last] x; 'linear' continues with slope 1 (identity)."""
    pts = _clean_points(points, lo, hi, add_ends)
    f = PchipInterpolator(pts[:, 0], pts[:, 1], extrapolate=False)
    x0, x1 = pts[0, 0], pts[-1, 0]
    y0, y1 = pts[0, 1], pts[-1, 1]

    def fn(x):
        x = np.asarray(x, dtype=np.float64)
        xc = np.clip(x, x0, x1)
        y = f(xc)
        if extend == "linear":
            y = np.where(x < x0, y0 + (x - x0), y)
            y = np.where(x > x1, y1 + (x - x1), y)
        return y
    return fn


def pchip_curve(points, n: int = 4096, lo: float = 0.0, hi: float = 1.0, extend: str = "flat") -> np.ndarray:
    """Sample a PCHIP curve through `points` [(x, y), ...] into an n-entry LUT over [lo, hi]."""
    xs = np.linspace(lo, hi, n)
    return curve_fn(points, lo, hi, extend)(xs).astype(np.float32)


def apply_lut_1d(x: np.ndarray, lut: np.ndarray, lo: float = 0.0, hi: float = 1.0) -> np.ndarray:
    xs = np.linspace(lo, hi, len(lut), dtype=np.float32)
    return np.interp(x, xs, lut).astype(np.float32)


def _is_identity(points, tol: float = 1e-6) -> bool:
    if points is None:
        return True
    p = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    return bool(np.all(np.abs(p[:, 0] - p[:, 1]) < tol))


def luma_ratio_apply(img: np.ndarray, f, luma_fn=None, fit_gamut: float | None = None) -> np.ndarray:
    """Apply scalar curve f to the luminance and rescale RGB by the ratio (hue/ratio preserving).
    fit_gamut=1.0: blend toward the grey of the new luminance so max channel <= 1 (display domain)."""
    Y = (luma_fn or color.luma)(img)
    Yn = np.asarray(f(np.maximum(Y, 0.0)), dtype=np.float32)
    ratio = Yn / np.maximum(Y, 1e-5)
    # in deep black the ratio is ill-defined: fall back to adding the delta
    out = np.where((Y > 1e-5)[..., None], img * ratio[..., None], img + (Yn - Y)[..., None])
    if fit_gamut is not None:
        m = out.max(axis=2)
        t = np.where(m > fit_gamut, np.clip((fit_gamut - Yn) / np.maximum(m - Yn, 1e-6), 0.0, 1.0), 1.0)
        out = Yn[..., None] + (out - Yn[..., None]) * t[..., None]
    return out.astype(np.float32)


def _mix(orig: np.ndarray, new: np.ndarray, s: float) -> np.ndarray:
    return new if s >= 1.0 else (orig + (new - orig) * np.float32(s)).astype(np.float32)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


# --------------------------------------------------------------------------- tone_curve (display)
_CURVE_CHOICES = {"mode": ["rgb", "luma", "film", "oklab"]}


@op("tone_curve", 18, "display", points=[[0.0, 0.0], [1.0, 1.0]], red=None, green=None, blue=None,
    mode="rgb", strength=1.0, choices=_CURVE_CHOICES, category="tone")
def tone_curve(img, p, ctx: Ctx):
    """Monotone cubic (PCHIP) tone curve on display values: rgb, luma (ratio-preserving), film-like or Oklab L."""
    pts, r, g, b = p["points"], p.get("red"), p.get("green"), p.get("blue")
    if _is_identity(pts) and not any(c is not None and not _is_identity(c) for c in (r, g, b)):
        return img
    f = curve_fn(pts)
    mode = p["mode"]
    x = np.clip(img, 0.0, 1.0)
    if mode == "rgb":
        lut = pchip_curve(pts)
        out = apply_lut_1d(x, lut)
        for c, pc in enumerate((r, g, b)):
            if pc is not None and not _is_identity(pc):
                out[..., c] = apply_lut_1d(out[..., c], pchip_curve(pc))
    elif mode == "luma":
        out = luma_ratio_apply(x, f, fit_gamut=1.0)
    elif mode == "film":
        # Adobe DNG "film-like": curve on max and min channel, middle channel keeps its relative position
        lut = pchip_curve(pts)
        mx, mn = x.max(2), x.min(2)
        mxn, mnn = apply_lut_1d(mx, lut), apply_lut_1d(mn, lut)
        den = np.maximum(mx - mn, 1e-6)
        out = mnn[..., None] + (mxn - mnn)[..., None] * (x - mn[..., None]) / den[..., None]
        out = np.where((mx - mn < 1e-6)[..., None], mxn[..., None], out)
    else:  # oklab: curve on perceptual lightness, chroma scaled by L'/L so colours keep their look
        lab = color.display_to_oklab(x)
        L = np.clip(lab[..., 0], 0.0, 1.0)
        Ln = apply_lut_1d(L, pchip_curve(pts))
        k = (Ln / np.maximum(L, 1e-4))[..., None]
        lab = np.concatenate([Ln[..., None], lab[..., 1:] * k], axis=2)
        out = np.clip(color.oklab_to_display(lab), 0.0, 1.0)
    return _mix(img, out.astype(np.float32), float(p["strength"]))


@op("scene_curve", 12, "linear", points=[[-8.0, -8.0], [0.0, 0.0], [4.0, 4.0]], red=None, green=None, blue=None,
    mode="luma", strength=1.0, choices={"mode": ["luma", "rgb"]}, category="tone")
def scene_curve(img, p, ctx: Ctx):
    """Tone curve on scene-linear data in log2-EV space (x, y = EV relative to middle grey); applied as a
    per-pixel gain so hue is preserved in 'luma' mode. Identity continues with slope 1 outside the points."""
    pts = p["points"]
    if _is_identity(pts) and not any(c is not None and not _is_identity(c) for c in (p.get("red"), p.get("green"), p.get("blue"))):
        return img

    def gain_of(v, points):
        ev = np.log2(np.maximum(v, EPS) / GREY)
        lo, hi = -14.0, 8.0
        lut = curve_fn(points, lo, hi, extend="linear", add_ends=False)(np.linspace(lo, hi, 2048)) - np.linspace(lo, hi, 2048)
        # gain in EV looked up on the grid (identity-extended outside)
        g = np.interp(ev, np.linspace(lo, hi, 2048), lut)
        return np.exp2(g).astype(np.float32)

    if p["mode"] == "luma":
        Y = color.luminance(img)
        out = img * gain_of(Y, pts)[..., None]
    else:
        out = np.empty_like(img)
        extra = (p.get("red"), p.get("green"), p.get("blue"))
        for c in range(3):
            ch = img[..., c]
            o = ch * gain_of(ch, pts)
            if extra[c] is not None and not _is_identity(extra[c]):
                o = o * gain_of(np.maximum(o, 0), extra[c])
            out[..., c] = o
    return _mix(img, out.astype(np.float32), float(p["strength"]))


# --------------------------------------------------------------------------- levels (display)
@op("levels", 18, "display", black=0.0, white=1.0, gamma=1.0, out_black=0.0, out_white=1.0, auto=False,
    auto_clip=0.001, per_channel=False, category="tone")
def levels(img, p, ctx: Ctx):
    """Levels: input black/white points, midtone gamma (>1 brightens), output black/white; optional auto-levels."""
    black, white = float(p["black"]), float(p["white"])
    if p.get("auto"):
        lum = color.luma(np.clip(img, 0, 1)).ravel()
        c = float(p["auto_clip"])
        black, white = (float(v) for v in np.percentile(lum, [100 * c, 100 * (1 - c)]))
    ob, ow, gamma = float(p["out_black"]), float(p["out_white"]), max(float(p["gamma"]), 1e-3)
    if (black == 0 and white == 1 and gamma == 1 and ob == 0 and ow == 1):
        return img
    if p.get("per_channel") and p.get("auto"):
        out = np.empty_like(img)
        for c in range(3):
            lo, hi = np.percentile(img[..., c], [100 * p["auto_clip"], 100 * (1 - p["auto_clip"])])
            out[..., c] = np.clip((img[..., c] - lo) / max(hi - lo, 1e-6), 0, 1)
    else:
        out = np.clip((img - black) / max(white - black, 1e-6), 0.0, 1.0)
    out = np.power(out, 1.0 / gamma)
    return (ob + (ow - ob) * out).astype(np.float32)


# --------------------------------------------------------------------------- guided log mask
def _norm_for(img: np.ndarray, kind: str) -> np.ndarray:
    x = np.maximum(img, 0.0)
    if kind == "luminance":
        return color.luminance(x)
    if kind == "max":
        return x.max(2)
    if kind == "geomean":
        return np.exp(np.log(np.maximum(x, EPS)).mean(2))
    if kind == "power":
        return (x ** 3).sum(2) / np.maximum((x ** 2).sum(2), 1e-10)
    return np.sqrt((x ** 2).mean(2))  # rms: Euclidean norm / sqrt(3), grey stays grey


def guided_log_mask(img: np.ndarray, radius: float, eps: float, norm: str = "rms", diffusion: int = 1):
    """Edge-aware luminance mask in log2 units: guided filter (guide = src = log2 norm) so that the blur
    strength is exposure independent (darktable 'eigf'-like). Returns (mask, log2_norm)."""
    L = np.log2(np.maximum(_norm_for(img, norm), 2.0 ** -12)).astype(np.float32)
    M = L
    for _ in range(max(1, int(diffusion))):
        M = fast_guided(M, M, max(1, int(radius)), float(eps))
    return M.astype(np.float32), L


def _gain_rgb(img: np.ndarray, ev: np.ndarray) -> np.ndarray:
    return (img * np.exp2(ev).astype(np.float32)[..., None]).astype(np.float32)


# --------------------------------------------------------------------------- highlights & shadows
@op("highlights_shadows", 12, "linear", shadows=0.0, highlights=0.0, whites=0.0, blacks=0.0, radius=40.0,
    edge_ev=0.6, tonal_width=2.0, shadow_pivot=-2.0, highlight_pivot=1.0, category="tone")
def highlights_shadows(img, p, ctx: Ctx):
    """Shadows lift / highlights recovery from an edge-aware (guided, log-luminance) base: no halos.
    Slider convention follows brightness: shadows +100 lifts darks, highlights -100 recovers brights."""
    sh, hl = float(p["shadows"]) / 100.0, float(p["highlights"]) / 100.0
    wh, bl = float(p["whites"]) / 100.0, float(p["blacks"]) / 100.0
    if sh == 0 and hl == 0 and wh == 0 and bl == 0:
        return img
    radius = ctx.px(p["radius"], img)
    M, L = guided_log_mask(img, radius, float(p["edge_ev"]) ** 2)
    t = M - LOG2_GREY                         # EV relative to grey
    w = max(float(p["tonal_width"]), 0.2)
    # compact-support masks (exactly 0 outside +-w EV of the pivot): shadows leave mids/highlights alone
    ms = 1.0 - color.smoothstep(float(p["shadow_pivot"]) - w, float(p["shadow_pivot"]) + w, t)
    mh = color.smoothstep(float(p["highlight_pivot"]) - w, float(p["highlight_pivot"]) + w, t)
    ev = 3.0 * sh * ms + 3.0 * hl * mh
    if wh or bl:   # whites / blacks act on the pixel itself (steeper, narrower)
        t_px = L - LOG2_GREY
        ev = ev + 1.5 * wh * _sigmoid((t_px - 2.0) / 0.7) + 1.5 * bl * _sigmoid(-(t_px + 4.0) / 0.7)
    return _gain_rgb(img, ev)


# --------------------------------------------------------------------------- tone equalizer
TE_NODES = np.arange(-8.0, 1.0, 1.0)  # -8 .. 0 EV (9 bands)
_TE_NAMES = ["m8", "m7", "m6", "m5", "m4", "m3", "m2", "m1", "m0"]


def tone_equalizer_curve(bands, smoothing: float = 0.25, step: float = 0.02):
    """9 band gains (EV) at -8..0 EV -> (grid, gain_ev) with PCHIP interpolation, flat outside, then an
    optional Gaussian smoothing of the curve (sigma = smoothing EV)."""
    if isinstance(bands, dict):
        b = [float(bands.get(str(int(k)), bands.get(n, 0.0))) for k, n in zip(TE_NODES, _TE_NAMES)]
    else:
        b = [float(v) for v in bands]
        b = (b + [0.0] * 9)[:9]
    grid = np.arange(-12.0, 3.0 + step, step)
    g = PchipInterpolator(TE_NODES, b, extrapolate=False)(np.clip(grid, -8.0, 0.0))
    s = float(smoothing)
    if s > 0:
        k = max(1, int(3 * s / step))
        kern = np.exp(-0.5 * (np.arange(-k, k + 1) * step / s) ** 2)
        kern /= kern.sum()
        pad = np.pad(g, k, mode="edge")
        g = np.convolve(pad, kern, mode="valid")
    return grid, g


@op("tone_equalizer", 12, "linear", bands=[0.0] * 9, smoothing=0.25, norm="rms", smoothing_diameter=5.0,
    edge_ev=0.5, diffusion=1, exposure_comp=0.0, contrast_comp=1.0, auto_center=False, preserve_grey=False,
    choices={"norm": ["rms", "luminance", "max", "geomean", "power"]}, category="tone")
def tone_equalizer(img, p, ctx: Ctx):
    """darktable-style tone equalizer: guided-filter log2-EV luminance mask -> 9-band (-8..0 EV) gain curve
    -> per-pixel linear gain on all channels (hue preserving). bands are EV gains at -8,-7,...,0 EV."""
    bands = p["bands"]
    vals = list(bands.values()) if isinstance(bands, dict) else list(bands)
    if all(abs(float(v)) < 1e-9 for v in vals):
        return img
    diam = float(p["smoothing_diameter"]) / 100.0 * max(img.shape[:2])
    M, L = guided_log_mask(img, diam / 2.0, float(p["edge_ev"]) ** 2, p["norm"], int(p["diffusion"]))
    mean = float(M.mean())
    if p.get("auto_center"):
        mean_t = float(np.median(M))
        shift = -4.0 - mean_t
    else:
        shift = 0.0
    Mc = (M - mean) * float(p["contrast_comp"]) + mean + float(p["exposure_comp"]) + shift
    ctx.cache["tone_equalizer_mask"] = Mc
    grid, g = tone_equalizer_curve(bands, float(p["smoothing"]))
    if p.get("preserve_grey"):   # keep middle grey (about -2.4 EV in 'rms' units) untouched
        g = g - np.interp(LOG2_GREY, grid, g)
    ev = np.interp(np.clip(Mc, -12.0, 3.0), grid, g).astype(np.float32)
    return _gain_rgb(img, ev)


# --------------------------------------------------------------------------- contrast
@op("contrast", 13, "linear", contrast=0.0, fulcrum=GREY, hue_preserve=True, category="tone")
def contrast(img, p, ctx: Ctx):
    """Contrast as a luminance power law around the fulcrum (default 18.45 % grey): Y' = f (Y/f)^(1+c)."""
    c = float(p["contrast"])
    if abs(c) < 1e-9:
        return img
    f = max(float(p["fulcrum"]), 1e-4)
    e = max(1.0 + c, 0.05)
    if p.get("hue_preserve", True):
        return luma_ratio_apply(img, lambda Y: f * np.power(Y / f, e), color.luminance)
    return (f * np.power(np.maximum(img, 0.0) / f, e)).astype(np.float32)


# --------------------------------------------------------------------------- local contrast
def _remap_detail(L: np.ndarray, g: float, sigma: float, alpha: float) -> np.ndarray:
    """Local-Laplacian point-wise remapping around g (edges with |d| > sigma are untouched: beta = 1)."""
    d = L - g
    ad = np.abs(d)
    inner = sigma * np.power(np.minimum(ad, sigma) / sigma, alpha)
    out = np.where(ad <= sigma, inner, ad)
    return g + np.sign(d) * out


def local_laplacian(L: np.ndarray, sigma_r: float = 1.0, alpha: float = 0.5, level_gain=None,
                    K: int | None = None, levels: int | None = None) -> np.ndarray:
    """Fast local Laplacian filter (Aubry et al. 2014): K remapped Laplacian pyramids interpolated by the
    input Gaussian pyramid. alpha < 1 boosts detail (|d| <= sigma_r), > 1 smooths; large edges untouched.
    level_gain: list of weights per pyramid level (finest first): out_l = in_l + w_l (llf_l - in_l)."""
    L = np.ascontiguousarray(L, dtype=np.float32)
    h, w = L.shape
    nlev = levels or int(np.clip(np.floor(np.log2(min(h, w) / 8.0)) + 1, 3, 10))
    gp = gaussian_pyramid(L, nlev)
    nlev = len(gp)
    lap_in = laplacian_pyramid(L, nlev)
    lo, hi = float(L.min()), float(L.max())
    if hi - lo < 1e-6:
        return L
    K = K or int(np.clip(np.ceil((hi - lo) / (0.6 * sigma_r)) + 1, 6, 24))
    samples = np.linspace(lo, hi, K)
    out_lap = [np.zeros_like(a) for a in lap_in]
    step = samples[1] - samples[0]
    for k in range(K):
        gk = float(samples[k])
        lp = laplacian_pyramid(_remap_detail(L, gk, sigma_r, alpha), nlev)
        for l in range(nlev - 1):
            wk = np.maximum(0.0, 1.0 - np.abs(gp[l] - gk) / step)
            out_lap[l] += wk * lp[l]
    out_lap[-1] = lap_in[-1]
    if level_gain is not None:
        for l in range(nlev - 1):
            wl = float(level_gain[l]) if l < len(level_gain) else 0.0
            out_lap[l] = lap_in[l] + wl * (out_lap[l] - lap_in[l])
    return collapse_pyramid(out_lap)


def _scale_gain(size2000: float, detail: float, texture: float, clarity: float, structure: float) -> float:
    """Weight of a detail band whose feature size is `size2000` px (at a 2000 px long edge): log-size Gaussian
    bumps, texture ~ 4 px, clarity ~ 32 px, structure ~ 180 px, plus the all-scale `detail` term."""
    s = float(np.log2(max(size2000, 0.25)))
    bump = lambda c, wd: float(np.exp(-0.5 * ((s - c) / wd) ** 2))
    return detail + texture * bump(2.0, 1.0) + clarity * bump(5.0, 1.3) + structure * bump(7.5, 1.3)


def _level_weights(nlev: int, long_edge: int, texture: float, clarity: float, structure: float,
                   detail: float) -> list[float]:
    """Pyramid level l has feature size ~1.5 * 2^l px of the current image -> weights per level (finest first)."""
    scale = 2000.0 / max(long_edge, 1)
    return [_scale_gain((2.0 ** l) * 1.5 * scale, detail, texture, clarity, structure) for l in range(nlev - 1)]


@op("local_contrast", 14, "linear", amount=0.0, clarity=0.0, texture=0.0, structure=0.0, method="laplacian",
    sigma_ev=1.0, protect_extremes=0.0, max_gain_ev=2.5, choices={"method": ["laplacian", "guided"]},
    category="tone")
def local_contrast(img, p, ctx: Ctx):
    """Halo-free local contrast on log2 luminance: local-Laplacian (default) or multiscale guided filter.
    amount = all scales; clarity = mid/coarse scales; texture = fine scales; structure = very coarse."""
    d, cl, tx, st = (float(p[k]) for k in ("amount", "clarity", "texture", "structure"))
    if d == 0 and cl == 0 and tx == 0 and st == 0:
        return img
    Y = color.luminance(np.maximum(img, 0.0)) if img.min() < 0 else color.luminance(img)
    L = np.log2(np.maximum(Y, 2.0 ** -12)).astype(np.float32)
    long_edge = max(img.shape[:2])
    if p["method"] == "guided":
        # multiscale guided-filter detail boost: bands between successive guided bases
        radii = [2.0, 6.0, 20.0, 60.0, 160.0]                      # px at 2000 px long edge
        bases = [L] + [fast_guided(L, L, max(1, int(ctx.px(r, img))), 0.04) for r in radii]
        sizes = [1.5, 4.0, 12.0, 40.0, 110.0]                      # band feature sizes (px@2000)
        out = bases[-1].copy()
        for i in range(len(bases) - 1):
            out += (1.0 + 0.8 * _scale_gain(sizes[i], d, tx, cl, st)) * (bases[i] - bases[i + 1])
        Lp = out
    else:
        gp = gaussian_pyramid(L, int(np.clip(np.floor(np.log2(min(L.shape) / 8.0)) + 1, 3, 10)))
        ws = _level_weights(len(gp), long_edge, tx, cl, st, d)
        Lp = local_laplacian(L, float(p["sigma_ev"]), 0.5, ws, levels=len(gp))
    dEV = Lp - L
    lim = float(p["max_gain_ev"])
    dEV = np.clip(dEV, -lim, lim)
    prot = float(p["protect_extremes"])
    if prot > 0:  # fade the effect in deep shadows / near white (RawPedia-style midtone window)
        t = L - LOG2_GREY
        win = np.exp(-0.5 * (t / 3.5) ** 2)
        dEV = dEV * (1 - prot + prot * win)
    return _gain_rgb(img, dEV)


# --------------------------------------------------------------------------- dehaze
def dark_channel(img: np.ndarray, size: int) -> np.ndarray:
    size = max(1, int(size)) | 1
    m = img.min(axis=2)
    return cv2.erode(m, np.ones((size, size), np.uint8))


def estimate_airlight(img: np.ndarray, dark: np.ndarray, frac: float = 0.001) -> np.ndarray:
    n = dark.size
    k = max(1, int(n * frac))
    idx = np.argpartition(dark.ravel(), -k)[-k:]
    cand = img.reshape(-1, 3)[idx]
    A = cand[np.argmax(cand.sum(axis=1))]
    return np.maximum(A, 1e-3).astype(np.float32)


@op("dehaze", 12, "linear", strength=0.0, distance=1.0, patch=15.0, t0=0.2, refine_radius=60.0, airlight=None,
    category="tone")
def dehaze(img, p, ctx: Ctx):
    """Haze removal by dark channel prior (He et al.) with guided-filter transmission; negative strength adds haze.
    t0 is the minimum transmission (larger on linear data to avoid noise blow-up); distance limits the reach."""
    s = float(np.clip(p["strength"], -1.0, 1.0))
    if abs(s) < 1e-6:
        return img
    x = np.maximum(img, 0.0).astype(np.float32)
    size = max(3, int(round(ctx.px(p["patch"], img))) | 1)
    A = np.asarray(p["airlight"], np.float32) if p.get("airlight") is not None else None
    dark = dark_channel(x, size)
    if A is None:
        A = estimate_airlight(x, dark)
    omega = 0.95
    t = 1.0 - omega * dark_channel(x / A, size)
    guide = np.sqrt(np.maximum(color.luminance(x) / max(float(A.mean()), 1e-3), 0.0)).astype(np.float32)
    t = fast_guided(guide, t.astype(np.float32), max(2, int(ctx.px(p["refine_radius"], img))), 1e-3)
    t = np.clip(t, 0.0, 1.0)
    if s > 0:
        t0 = float(p["t0"])
        dist = float(np.clip(p["distance"], 0.0, 1.0))
        floor = 1.0 - dist * (1.0 - t0)
        t_eff = 1.0 - s * (1.0 - np.maximum(t, floor))      # strength blends between untouched and full removal
        out = (img - A) / np.maximum(t_eff, t0)[..., None] + A
    else:
        a = (-s) * 0.6 * (0.5 + 0.5 * (1.0 - t))
        out = img * (1.0 - a)[..., None] + A * a[..., None]
    return out.astype(np.float32)


# --------------------------------------------------------------------------- HDR-style tone
@op("hdr_tone", 12, "linear", shadows=0.0, highlights=0.0, compress=0.0, detail=0.0, radius=60.0, edge_ev=0.7,
    pivot_ev=0.0, category="tone")
def hdr_tone(img, p, ctx: Ctx):
    """Durand-Dorsey style local tone mapping in log2 luminance: compress the edge-aware base around grey,
    lift shadows (EV, up to ~3), pull highlights, and re-inject (optionally boosted) detail."""
    sh, hl, cp, dt = (float(p[k]) for k in ("shadows", "highlights", "compress", "detail"))
    if sh == 0 and hl == 0 and cp == 0 and dt == 0:
        return img
    M, L = guided_log_mask(img, ctx.px(p["radius"], img), float(p["edge_ev"]) ** 2)
    detail = L - M
    piv = LOG2_GREY + float(p["pivot_ev"])
    base = (M - piv) * (1.0 - np.clip(cp, 0.0, 0.95)) + piv
    t = M - LOG2_GREY
    base = base + sh * _sigmoid(-(t + 1.0) / 1.5) - hl * _sigmoid((t - 1.0) / 1.5)
    L2 = base + detail * (1.0 + dt)
    ev = np.clip(L2 - L, -5.0, 5.0)
    return _gain_rgb(img, ev)
