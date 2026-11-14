"""Finishing for linear astro stacks (the Sequator "composition" switches, done the Siril / PixInsight way).

    remove_gradient      light pollution / sky glow: star-clipped block medians, smoothed, subtracted per channel
    calibrate_colour     background neutralisation + "average star is white" colour calibration from star photometry
    star_mask            soft mask of point sources (protects stars during noise reduction, drives enhancement)
    stretch              colour-preserving asinh stretch of luminance, auto black point and auto brightness,
                         HDR mode that keeps bright cores and fades saturated cores to white instead of a false hue
    enhance_stars        lifts faint stars through the star mask
    denoise              star-protected non-local means on luminance and strong chroma smoothing
    validate_sources     split-half validation: sources significant in the full stack *and* in both independent halves
    finish               everything above in order, returns a display-referred sRGB float image
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .. import color


def _mad(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    return float(1.4826 * np.median(np.abs(x - np.median(x)))) if x.size else 0.0


def _lum(img: np.ndarray) -> np.ndarray:
    return img.mean(2).astype(np.float32)


# --------------------------------------------------------------------------- background
def remove_gradient(img: np.ndarray, grid: int = 24, smooth: float = 2.0, sky: np.ndarray | None = None,
                    keep_pedestal: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Model the sky background on a grid of star-clipped block medians (DBE-like, automatic) and subtract it.
    `grid` = blocks along the long edge. Pixels outside `sky` (0 = ground) do not influence the model.
    Returns (corrected, background)."""
    H, W, _ = img.shape
    bs = max(8, max(H, W) // grid)
    gh, gw = max(2, H // bs), max(2, W // bs)
    L = _lum(img)
    sig = _mad(L[::4, ::4])
    med = float(np.median(L[::4, ::4]))
    starish = cv2.dilate((L > med + 3 * sig).astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    ok = ~starish if sky is None else (~starish & (sky > 0.5))
    bg = np.zeros((gh, gw, 3), np.float32)
    have = np.zeros((gh, gw), bool)
    for gy in range(gh):
        for gx in range(gw):
            ys, xs = slice(gy * H // gh, (gy + 1) * H // gh), slice(gx * W // gw, (gx + 1) * W // gw)
            m = ok[ys, xs]
            if m.mean() > 0.25:
                bg[gy, gx] = np.median(img[ys, xs][m], 0)
                have[gy, gx] = True
    if not have.any():
        return img, np.zeros_like(img)
    if not have.all():   # fill holes (ground, big stars) from the nearest valid cells
        from scipy import ndimage as nd

        idx = nd.distance_transform_edt(~have, return_distances=False, return_indices=True)
        bg = bg[idx[0], idx[1]]
    # reject cells that are much brighter than their neighbours (nebula / galaxy cores, missed stars)
    from scipy import ndimage as nd

    for c in range(3):
        m = nd.median_filter(bg[..., c], 3, mode="nearest")
        bg[..., c] = np.where(bg[..., c] > m + 2 * _mad(bg[..., c] - m), m, bg[..., c])
        bg[..., c] = cv2.GaussianBlur(bg[..., c], (0, 0), smooth) if smooth > 0 else bg[..., c]
    full = cv2.resize(bg, (W, H), interpolation=cv2.INTER_CUBIC)
    ped = keep_pedestal * full.reshape(-1, 3).mean(0)
    return (img - full + ped).astype(np.float32), full.astype(np.float32)


# --------------------------------------------------------------------------- colour
def _star_peaks(L: np.ndarray, sig: float, k: float = 8.0, sep: int = 9):
    from scipy import ndimage as nd

    sm = cv2.GaussianBlur(L, (0, 0), 2)
    peaks = (sm == nd.maximum_filter(sm, sep)) & (sm > k * sig)
    return peaks, sm


def calibrate_colour(img: np.ndarray, sky: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """Neutralise the background, then scale channels so the median star is white (a catalogue-free photometric
    colour calibration). Saturated stars are skipped. Returns (image, info)."""
    out = img.copy()
    L = _lum(out)
    region = (slice(None, None, 4), slice(None, None, 4))
    bgm = np.median(out[region].reshape(-1, 3), 0)
    out -= bgm - bgm.mean()
    L = _lum(out)
    sig = _mad(L[region])
    peaks, sm = _star_peaks(L, sig)
    if sky is not None:
        peaks &= sky > 0.5
    info = {"stars": 0, "white_balance": [1.0, 1.0, 1.0]}
    if peaks.sum() < 5:
        return out, info
    peaks &= sm < np.percentile(sm[peaks], 97)
    ys, xs = np.nonzero(peaks)
    H, W, _ = out.shape
    flux = []
    for y, x in zip(ys, xs):
        a = out[max(y - 3, 0):y + 4, max(x - 3, 0):x + 4].reshape(-1, 3).sum(0)
        r = out[max(y - 8, 0):y + 9, max(x - 8, 0):x + 9]
        bgl = np.median(r.reshape(-1, 3), 0) * 49
        flux.append(a - bgl)
    flux = np.array(flux)
    flux = flux[(flux > 0).all(1)]
    if len(flux) < 5:
        return out, info
    wb = np.median(flux.mean(1, keepdims=True) / flux, 0)
    wb = wb / wb.mean()
    out = out * wb
    return out.astype(np.float32), {"stars": int(len(flux)), "white_balance": [round(float(v), 4) for v in wb]}


# --------------------------------------------------------------------------- masks
def star_mask(img: np.ndarray, k: float = 2.5, grow: int = 5) -> np.ndarray:
    L = _lum(img)
    bg = cv2.GaussianBlur(cv2.morphologyEx(L, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))),
                          (0, 0), 6)
    r = L - bg
    sig = _mad(r[::4, ::4])
    m = np.clip((cv2.GaussianBlur(r, (0, 0), 1.5) - k * sig) / (4 * sig + 1e-12), 0, 1)
    return cv2.GaussianBlur(cv2.dilate(m, np.ones((grow, grow), np.uint8)), (0, 0), 2).astype(np.float32)


# --------------------------------------------------------------------------- stretch
def stretch(img: np.ndarray, strength: float = 0.0, black: float = 0.0, hdr: bool = False, target_bg: float = 0.10,
            saturation: float = 1.25, mask: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """Colour-preserving asinh stretch. strength 0 = auto (background lands at `target_bg`); otherwise the asinh
    softening factor (100 = gentle .. 3000 = aggressive). black = black point in background sigmas above the sky
    median (0 keeps a faint natural noise floor, 1.5+ gives an inky black)."""
    L = _lum(img)
    sig = _mad(L[::4, ::4])
    med = float(np.median(L[::4, ::4]))
    b = med + (black - 2.0) * sig          # black=0: clip only 2 sigma below the sky, so the sky keeps its noise floor
    Lc = np.clip(L - b, 0, None)
    top = float(np.percentile(Lc[::2, ::2], 99.995 if not hdr else 99.9995)) or 1.0
    x = Lc / top
    if strength <= 0:   # auto brightness: solve asinh(k*m)/asinh(k) = target_bg for the median sky level m
        m = max(med - b, 0.5 * sig) / top   # the sky median lands on target_bg
        lo, hi = 1.0, 1e6
        for _ in range(60):
            k = (lo * hi) ** 0.5
            v = np.arcsinh(k * m) / np.arcsinh(k)
            lo, hi = (k, hi) if v < target_bg else (lo, k)
        strength = (lo * hi) ** 0.5
        if hdr:
            strength *= 0.6
    Ls = np.arcsinh(strength * x) / np.arcsinh(strength)
    ratio = np.where(L[..., None] > b, img / np.maximum(L[..., None], 1e-9), 1.0)
    ratio = np.clip(ratio, 0, 3)
    if mask is not None:   # background chroma is noise: keep colour where the stars are
        ratio = 1 + (ratio - 1) * (0.15 + 0.85 * mask[..., None])
    wcore = np.clip((Ls[..., None] - (0.80 if hdr else 0.88)) / 0.12, 0, 1)
    ratio = ratio * (1 - wcore) + wcore
    ratio = 1 + (ratio - 1) * saturation
    out = Ls[..., None] * ratio
    if hdr:   # soft shoulder instead of a hard clip
        out = out / (1 + np.maximum(out - 0.75, 0))
    return np.clip(out, 0, 1).astype(np.float32), {"strength": round(float(strength), 1), "black": b, "top": top}


def enhance_stars(disp: np.ndarray, mask: np.ndarray, amount: float = 0.35) -> np.ndarray:
    """Lift stars (display-referred): screen-blend a brightened copy through the star mask."""
    if amount <= 0:
        return disp
    lift = 1 - (1 - disp) ** (1 + 2 * amount)
    return np.clip(disp + (lift - disp) * mask[..., None], 0, 1).astype(np.float32)


def denoise(disp: np.ndarray, strength: float = 0.5, mask: np.ndarray | None = None) -> np.ndarray:
    """Star-protected noise reduction in display space: NLM on 16-bit luminance, Gaussian on chroma."""
    if strength <= 0:
        return disp
    ycc = cv2.cvtColor(disp.astype(np.float32), cv2.COLOR_RGB2YCrCb)
    # 8-bit NLM (the only depth every OpenCV build exposes); the sub-8-bit residual is added back, so the float
    # result keeps its precision and the quantisation acts as a dither
    Yf = np.clip(ycc[..., 0], 0, 1)
    Y8 = np.round(Yf * 255).astype(np.uint8)
    sig = max(_mad(Yf[::4, ::4]) * 255, 0.5)
    h = float(sig * (1.0 + 2.5 * strength))
    D8 = cv2.fastNlMeansDenoising(Y8, None, h, 7, 21)
    Yd = Yf + (D8.astype(np.float32) - Y8.astype(np.float32)) / 255
    out = ycc.copy()
    out[..., 0] = Yd
    cs = 1.0 + 4.0 * strength
    out[..., 1] = cv2.GaussianBlur(ycc[..., 1], (0, 0), cs)
    out[..., 2] = cv2.GaussianBlur(ycc[..., 2], (0, 0), cs)
    if mask is not None:   # inside stars keep most of the original detail
        keep = (0.85 * mask)[..., None]
        out = out * (1 - keep) + ycc * keep
    return np.clip(cv2.cvtColor(out, cv2.COLOR_YCrCb2RGB), 0, 1).astype(np.float32)


# --------------------------------------------------------------------------- validation
def significance(img: np.ndarray, psf_sigma: float = 1.5) -> np.ndarray:
    """Matched-filter significance map (in sigmas) of point sources, with a locally estimated noise level."""
    L = _lum(img)
    bg = cv2.GaussianBlur(cv2.morphologyEx(L, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))),
                          (0, 0), 8)
    f = cv2.GaussianBlur(L - bg, (0, 0), psf_sigma)
    a = np.abs(f)
    b = 128
    H, W = f.shape
    small = cv2.resize(a, (max(1, W // b), max(1, H // b)), interpolation=cv2.INTER_AREA)
    from scipy import ndimage as nd

    loc = cv2.resize(nd.median_filter(small, 3), (W, H), interpolation=cv2.INTER_LINEAR) / 0.6745   # median|f| -> sigma
    return (f / np.maximum(loc, 1e-12)).astype(np.float32)


def validate_sources(full: np.ndarray, half_a: np.ndarray, half_b: np.ndarray, k_full: float = 6.0,
                     k_half: float = 3.0, max_sources: int = 20000) -> dict:
    """Point sources above k_full in the full stack that are also above k_half at the same spot in BOTH independent
    half stacks. Noise peaks almost never survive the second test, so this separates real faint stars from
    speckle without a catalogue. The false-alarm rate is measured by applying the half test to random positions."""
    from scipy import ndimage as nd

    Sf, Sa, Sb = significance(full), significance(half_a), significance(half_b)
    peaks = (Sf == nd.maximum_filter(Sf, 7)) & (Sf > k_full)
    ys, xs = np.nonzero(peaks)
    order = np.argsort(-Sf[ys, xs])[:max_sources]
    ys, xs = ys[order], xs[order]
    ma = nd.maximum_filter(Sa, 3)
    mb = nd.maximum_filter(Sb, 3)
    ok = (ma[ys, xs] > k_half) & (mb[ys, xs] > k_half)
    rng = np.random.default_rng(0)
    ry = rng.integers(0, Sf.shape[0], 4000)
    rx = rng.integers(0, Sf.shape[1], 4000)
    fa = float(((ma[ry, rx] > k_half) & (mb[ry, rx] > k_half)).mean())
    return {"candidates": int(len(ys)), "confirmed": int(ok.sum()),
            "half_test_false_alarm_rate": round(fa, 4),
            "sources": [[int(x), int(y), round(float(Sf[y, x]), 1)] for y, x in zip(ys[ok], xs[ok])]}


# --------------------------------------------------------------------------- one call
@dataclass
class FinishOptions:
    light_pollution: bool = True
    colour: bool = True
    stretch: float = 0.0          # 0 = auto brightness
    black: float = 0.0
    target_bg: float = 0.10
    hdr: bool = False
    enhance_stars: float = 0.3
    denoise: float = 0.5
    saturation: float = 1.25
    to_srgb: bool = True          # input is linear Rec.2020 (photo-studio decode); False for camera-RGB FITS


def finish(img: np.ndarray, opts: FinishOptions | None = None, sky: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """Linear stack -> display sRGB float32 (0..1) and an info dict."""
    o = opts or FinishOptions()
    info: dict = {}
    x = np.maximum(img, 0).astype(np.float32)
    if o.to_srgb:
        x = np.maximum(color.apply_matrix(x, color.REC2020_TO_SRGB), 0).astype(np.float32)
    if o.light_pollution:
        x, _bg = remove_gradient(x, sky=sky)
    else:
        x = x - np.median(x[::4, ::4].reshape(-1, 3), 0)
    if o.colour:
        x, info["colour"] = calibrate_colour(x, sky)
    m = star_mask(x)
    disp, info["stretch"] = stretch(x, o.stretch, o.black, o.hdr, o.target_bg, o.saturation, m)
    m = star_mask(disp, k=3.0)
    disp = enhance_stars(disp, m, o.enhance_stars)
    disp = denoise(disp, o.denoise, m)
    return disp, info
