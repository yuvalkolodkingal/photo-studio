"""Shared building blocks for the effects / art ops (not an op module itself).

Conventions: images are float32 HxWx3 RGB. Display-referred helpers expect encoded sRGB 0..1;
`*_lin` helpers expect scene-linear. Pixel sizes are declared at a 2000 px long edge and scaled by
`ctx.px`. Randomness always comes from `rng_for(ctx, p)` so renders are deterministic per ctx.seed.
"""
from __future__ import annotations

import cv2
import numpy as np

from ... import color
from ...filters import gaussian
from ...util import hex_to_rgb

F32 = np.float32


# ----------------------------------------------------------------------------- basics
def finish(img: np.ndarray) -> np.ndarray:
    """Clip to 0..1, kill NaN/inf, contiguous float32 (end of every display op)."""
    out = np.nan_to_num(np.asarray(img, dtype=F32), nan=0.0, posinf=1.0, neginf=0.0)
    return np.ascontiguousarray(np.clip(out, 0.0, 1.0), dtype=F32)


def luma(img: np.ndarray) -> np.ndarray:
    return color.luma(img)


def px(ctx, v: float, img: np.ndarray, lo: float = 0.0) -> float:
    return max(lo, float(ctx.px(v, img)))


def odd(n: float, lo: int = 1) -> int:
    n = max(lo, int(round(n)))
    return n if n % 2 == 1 else n + 1


def rng_for(ctx, p: dict, salt: int = 0) -> np.random.Generator:
    seed = (int(getattr(ctx, "seed", 0) or 0) * 1000003 + int(p.get("seed", 0) or 0) * 7919 + salt * 104729) & 0xFFFFFFFF
    return np.random.default_rng(seed)


def rgb(c) -> np.ndarray:
    """Hex string / 0-255 / 0-1 triple -> float32 (3,) display colour."""
    return np.asarray(hex_to_rgb(c), dtype=F32)


def pal(colors) -> np.ndarray:
    return np.stack([rgb(c) for c in colors]).astype(F32)


def smoothstep(e0, e1, x):
    return color.smoothstep(e0, e1, x).astype(F32)


def mix(a, b, t):
    return a + (b - a) * t


def screen(a, b):
    return 1.0 - (1.0 - a) * (1.0 - b)


def blur(img: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur; very wide kernels run on a box-downsampled copy (4x or more faster, visually identical for smooth fields)."""
    if sigma < 0.25:
        return img
    if sigma >= 12.0:
        f = int(sigma / 5.0)
        h, w = img.shape[:2]
        small = cv2.resize(img, (max(2, w // f), max(2, h // f)), interpolation=cv2.INTER_AREA)
        small = gaussian(small, sigma / f)
        return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR).astype(img.dtype, copy=False)
    return gaussian(img, sigma)


def to_u8(img: np.ndarray) -> np.ndarray:
    return (np.clip(img, 0, 1) * 255.0 + 0.5).astype(np.uint8)


def from_u8(img: np.ndarray) -> np.ndarray:
    return img.astype(F32) / 255.0


def lin(img: np.ndarray) -> np.ndarray:
    """Display -> linear sRGB-primaries (used for physically-motivated steps inside display ops)."""
    return color.srgb_eotf(img)


def enc(x: np.ndarray) -> np.ndarray:
    return color.srgb_oetf(np.clip(x, 0, None))


# ----------------------------------------------------------------------------- tone
def sgain(x, gain: float, mid: float = 0.5):
    """Normalised logistic S-curve ('S gain' of the notes): maps 0->0, 1->1, slope grows with gain."""
    g = float(gain)
    if g <= 0.05:
        return x
    x = np.asarray(x, dtype=F32)
    lo = 1.0 / (1.0 + np.exp(g * mid))
    hi = 1.0 / (1.0 + np.exp(-g * (1.0 - mid)))
    y = 1.0 / (1.0 + np.exp(-g * (x - mid)))
    return ((y - lo) / (hi - lo)).astype(F32)


def tone(x, gain: float = 0.0, black: float = 0.0, white: float = 1.0, mid: float = 0.5):
    """S-curve then remap output range to [black, white] (lifted blacks / capped whites)."""
    y = sgain(x, gain, mid) if gain else x
    return (black + (white - black) * y).astype(F32)


def curve_lut(points, n: int = 1024) -> np.ndarray:
    """Monotone PCHIP LUT through (x, y) control points on 0..1."""
    from scipy.interpolate import PchipInterpolator

    pts = sorted((float(a), float(b)) for a, b in points)
    xs = np.array([q[0] for q in pts])
    ys = np.array([q[1] for q in pts])
    if xs[0] > 0:
        xs, ys = np.r_[0.0, xs], np.r_[ys[0], ys]
    if xs[-1] < 1:
        xs, ys = np.r_[xs, 1.0], np.r_[ys, ys[-1]]
    keep = np.r_[True, np.diff(xs) > 1e-6]
    f = PchipInterpolator(xs[keep], ys[keep])
    return np.clip(f(np.linspace(0, 1, n)), 0, 1).astype(F32)


def apply_curve(x: np.ndarray, lut: np.ndarray) -> np.ndarray:
    n = len(lut) - 1
    return np.interp(np.clip(x, 0, 1), np.linspace(0, 1, n + 1, dtype=F32), lut).astype(F32)


def saturate(img: np.ndarray, s: float) -> np.ndarray:
    l = luma(img)[..., None]
    return l + (img - l) * float(s)


def bw_mix(img: np.ndarray, w) -> np.ndarray:
    w = np.asarray(w, dtype=F32)
    return (img[..., 0] * w[0] + img[..., 1] * w[1] + img[..., 2] * w[2]).astype(F32)


def gray3(g: np.ndarray) -> np.ndarray:
    return np.repeat(g[..., None], 3, axis=2)


def unsharp(img: np.ndarray, sigma: float, amount: float) -> np.ndarray:
    if amount == 0 or sigma < 0.2:
        return img
    return img + amount * (img - blur(img, sigma))


def local_contrast(img: np.ndarray, sigma: float, amount: float) -> np.ndarray:
    """Large-radius unsharp on luminance only (zone-system 'dodge & burn' feel)."""
    if amount == 0:
        return img
    l = luma(img)
    d = (l - blur(l, sigma)) * amount
    return img + d[..., None]


# ----------------------------------------------------------------------------- hue helpers
def hsv(img: np.ndarray) -> np.ndarray:
    return color.rgb_to_hsv(np.clip(img, 0, 1))


def ang_weight(h: np.ndarray, center: float, half_width: float, feather: float | None = None) -> np.ndarray:
    """Smooth angular window around `center` degrees (1 inside half_width, 0 beyond half_width+feather)."""
    feather = half_width * 0.6 if feather is None else feather
    d = np.abs(((h - center + 180.0) % 360.0) - 180.0)
    return 1.0 - smoothstep(half_width, half_width + feather + 1e-6, d)


def hue_masks(img: np.ndarray):
    h = hsv(img)
    return h[..., 0], h[..., 1], h[..., 2]


def hsl_adjust(img: np.ndarray, rules: list[dict]) -> np.ndarray:
    """Hue-targeted adjustments. rule = {h: centre deg, w: half-width deg, sat: x, lum: x, shift: deg}."""
    if not rules:
        return img
    hh = hsv(img)
    H, S, V = hh[..., 0].copy(), hh[..., 1].copy(), hh[..., 2].copy()
    S0 = S.copy()
    chroma_w = np.clip(S0 / 0.25, 0, 1)
    for r in rules:
        w = ang_weight(H, r["h"], r.get("w", 25.0)) * chroma_w
        if r.get("shift"):
            H = H + r["shift"] * w
        S = S * (1 + (r.get("sat", 1.0) - 1) * w)
        V = V * (1 + (r.get("lum", 1.0) - 1) * w)
    out = np.stack([H % 360.0, np.clip(S, 0, 1), np.clip(V, 0, None)], -1).astype(F32)
    out = color.hsv_to_rgb(out)
    # preserve over-range values of the source (hsv clip at 1)
    return out


def tint_toward(img, col, amount, weight=None):
    c = rgb(col) if isinstance(col, (str, list, tuple)) else col
    a = amount if weight is None else amount * weight[..., None]
    return img * (1 - a) + c * a


def split_tint(img, shadow=None, highlight=None, sh_amt=0.15, hi_amt=0.1, sh_end=0.4, hi_start=0.6, mid=None, mid_amt=0.0):
    l = luma(img)
    out = img
    if shadow is not None and sh_amt > 0:
        out = tint_toward(out, shadow, sh_amt, 1.0 - smoothstep(0.0, sh_end, l))
    if highlight is not None and hi_amt > 0:
        out = tint_toward(out, highlight, hi_amt, smoothstep(hi_start, 1.0, l))
    if mid is not None and mid_amt > 0:
        out = tint_toward(out, mid, mid_amt, 1.0 - np.abs(2 * l - 1))
    return out


def warm(img: np.ndarray, w: float) -> np.ndarray:
    """White-balance style warm (+) / cool (-) shift, w roughly in [-1, 1]."""
    if w == 0:
        return img
    gains = np.array([1 + 0.14 * w, 1 + 0.02 * w, 1 - 0.16 * w], dtype=F32)
    return img * gains


# ----------------------------------------------------------------------------- gradient maps / palettes
def make_lut(stops, n: int = 1024, space: str = "oklab") -> np.ndarray:
    """stops: list of colours (evenly spaced) or (position, colour). Piece-wise linear in Oklab (default)."""
    if isinstance(stops[0], (tuple, list)) and len(stops[0]) == 2 and isinstance(stops[0][0], (int, float)):
        pos = np.array([float(s[0]) for s in stops])
        cols = pal([s[1] for s in stops])
    else:
        cols = pal(stops)
        pos = np.linspace(0, 1, len(cols))
    order = np.argsort(pos)
    pos, cols = pos[order], cols[order]
    grid = np.linspace(0, 1, n)
    if space == "oklab":
        lab = color.display_to_oklab(cols.reshape(1, -1, 3))[0]
        out = np.stack([np.interp(grid, pos, lab[:, i]) for i in range(3)], -1).astype(F32)
        res = color.oklab_to_display(out.reshape(1, -1, 3))[0]
    else:
        res = np.stack([np.interp(grid, pos, cols[:, i]) for i in range(3)], -1)
    return np.clip(res, 0, 1).astype(F32)


def apply_lut(t: np.ndarray, lut: np.ndarray) -> np.ndarray:
    n = len(lut) - 1
    idx = np.clip(t * n + 0.5, 0, n).astype(np.int32)
    return lut[idx]


def oklab(img: np.ndarray) -> np.ndarray:
    return color.display_to_oklab(np.clip(img, 0, 1))


def from_oklab(lab: np.ndarray) -> np.ndarray:
    return color.oklab_to_display(lab)


def nearest_palette(img: np.ndarray, palette: np.ndarray, weights=(1.0, 1.0, 1.0)) -> np.ndarray:
    """Index of the nearest palette colour in Oklab (HxW int32). `weights` scale L, a, b."""
    lab = oklab(img).reshape(-1, 3) * np.asarray(weights, F32)
    pl = oklab(palette.reshape(1, -1, 3))[0] * np.asarray(weights, F32)
    d = (lab ** 2).sum(1, keepdims=True) - 2.0 * lab @ pl.T + (pl ** 2).sum(1)[None]
    return np.argmin(d, axis=1).reshape(img.shape[:2]).astype(np.int32)


def kmeans_cluster(feat: np.ndarray, k: int, seed: int = 0, sample: int = 20000, attempts: int = 2, iters: int = 25):
    """cv2.kmeans on a random pixel sample (deterministic), then assign every pixel. feat: HxWxC float32."""
    h, w, c = feat.shape
    flat = np.ascontiguousarray(feat.reshape(-1, c), dtype=F32)
    n = flat.shape[0]
    rng = np.random.default_rng(seed)
    pts = flat[rng.choice(n, size=min(sample, n), replace=False)] if n > sample else flat
    k = int(max(1, min(k, len(pts))))
    cv2.setRNGSeed(int(seed) & 0x7FFFFFFF)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, iters, 0.2)
    _, _, centers = cv2.kmeans(pts, k, None, crit, attempts, cv2.KMEANS_PP_CENTERS)
    d = (flat ** 2).sum(1, keepdims=True) - 2.0 * flat @ centers.T + (centers ** 2).sum(1)[None]
    labels = np.argmin(d, axis=1).reshape(h, w).astype(np.int32)
    return centers.astype(F32), labels


def label_means(img: np.ndarray, labels: np.ndarray, k: int) -> np.ndarray:
    cnt = np.bincount(labels.ravel(), minlength=k).astype(np.float64)
    out = np.zeros((k, img.shape[2]), np.float64)
    for ch in range(img.shape[2]):
        out[:, ch] = np.bincount(labels.ravel(), weights=img[..., ch].ravel(), minlength=k)
    return (out / np.maximum(cnt, 1)[:, None]).astype(F32), cnt


# ----------------------------------------------------------------------------- filters / morphology
def ellipse(r: float) -> np.ndarray:
    d = max(1, int(round(r)) * 2 + 1)
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d))


def dilate(mask: np.ndarray, r: float) -> np.ndarray:
    if r < 0.5:
        return mask
    return cv2.dilate(mask, ellipse(r))


def erode(mask: np.ndarray, r: float) -> np.ndarray:
    if r < 0.5:
        return mask
    return cv2.erode(mask, ellipse(r))


def edge_preserve(img: np.ndarray, ctx, sigma_s: float = 60.0, sigma_r: float = 0.4) -> np.ndarray:
    """cv2.edgePreservingFilter (recursive) with the spatial sigma scaled to the image size."""
    ss = float(np.clip(sigma_s * max(img.shape[:2]) / 2000.0, 3.0, 200.0))
    out = cv2.edgePreservingFilter(to_u8(img), flags=1, sigma_s=ss, sigma_r=float(sigma_r))
    return from_u8(out)


def bilateral(img: np.ndarray, ctx, d: int = 9, sigma_color: float = 0.2, sigma_space: float = 9.0, passes: int = 1):
    s = max(img.shape[:2]) / 2000.0
    dd = odd(d * max(s, 0.5) * 1.0, 3)
    out = np.ascontiguousarray(img, dtype=F32)
    for _ in range(passes):
        out = cv2.bilateralFilter(out, dd, float(sigma_color), float(max(1.0, sigma_space * s)))
    return out


def resize_to(img: np.ndarray, w: int, h: int, interp=cv2.INTER_AREA):
    return cv2.resize(img, (int(w), int(h)), interpolation=interp)


def work_scale(img: np.ndarray, max_edge: int):
    """Downscale a copy so its long edge is <= max_edge. Returns (small, scale)."""
    h, w = img.shape[:2]
    s = min(1.0, max_edge / max(h, w))
    if s >= 0.999:
        return img, 1.0
    return resize_to(img, max(2, round(w * s)), max(2, round(h * s))), s


def upscale_like(img: np.ndarray, ref_shape, interp=cv2.INTER_CUBIC):
    h, w = ref_shape[:2]
    if img.shape[0] == h and img.shape[1] == w:
        return img
    return cv2.resize(img, (w, h), interpolation=interp)


def bayer(n: int) -> np.ndarray:
    """Normalised (0..1, centred) ordered-dither matrix of size n x n (n power of two)."""
    m = np.array([[0, 2], [3, 1]], dtype=np.float32)
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / (n * n)


def ordered_dither_levels(g: np.ndarray, levels: int, n: int = 4) -> np.ndarray:
    """Quantise grey (0..1) into `levels` levels with a Bayer matrix; returns integer level map."""
    h, w = g.shape
    b = np.tile(bayer(n), (h // n + 1, w // n + 1))[:h, :w]
    v = g * (levels - 1)
    base = np.floor(v)
    frac = v - base
    return np.clip(base + (frac > b), 0, levels - 1).astype(np.int32)


# ----------------------------------------------------------------------------- noise / grain / paper
def noise_unit(shape, rng: np.random.Generator, sigma: float = 0.0, octaves: int = 1) -> np.ndarray:
    """Smooth zero-mean unit-std noise (octaves of blurred white noise)."""
    h, w = shape[:2]
    acc = np.zeros((h, w), F32)
    amp = 1.0
    for o in range(octaves):
        n = rng.standard_normal((h, w), dtype=F32)
        s = sigma * (2 ** o)
        if s > 0.3:
            n = blur(n, s)
        n /= n.std() + 1e-8
        acc += amp * n
        amp *= 0.5
    return (acc / (acc.std() + 1e-8)).astype(F32)


def grain_field(h: int, w: int, rng: np.random.Generator, size: float = 1.0, roughness: float = 0.4, clump: float = 0.0):
    """Unit-std grain: white noise blurred to `size` px, optionally with a coarse clumpy component
    (`roughness`) and tanh shaping (`clump`) for pushed-film clumping."""
    n = rng.standard_normal((h, w), dtype=F32)
    if size > 0.45:
        n = blur(n, size * 0.6)
    n /= n.std() + 1e-8
    if roughness > 0:
        c = rng.standard_normal((h, w), dtype=F32)
        c = blur(c, max(1.0, size) * 2.2)
        c /= c.std() + 1e-8
        n = n + roughness * 0.85 * c
        n /= n.std() + 1e-8
    if clump > 0:
        t = np.tanh(2.2 * n)
        t /= t.std() + 1e-8
        n = (1 - clump) * n + clump * t
        n /= n.std() + 1e-8
    return n.astype(F32)


def grain_weight(l: np.ndarray, midtone_bias: float = 0.6, shadow_bias: float = 0.0) -> np.ndarray:
    w = (1.0 - midtone_bias) + midtone_bias * (1.0 - np.abs(2.0 * l - 1.0))
    if shadow_bias > 0:
        w = w * (1.0 - shadow_bias) + shadow_bias * 2.0 * np.power(np.clip(1.0 - l, 0, 1), 1.5)
    return w.astype(F32)


def add_grain(img: np.ndarray, rng, amount: float, size: float = 1.0, chroma: float = 0.0, roughness: float = 0.4,
              midtone_bias: float = 0.6, shadow_bias: float = 0.0, clump: float = 0.0) -> np.ndarray:
    if amount <= 0:
        return img
    h, w = img.shape[:2]
    shared = grain_field(h, w, rng, size, roughness, clump)
    if chroma > 0:
        per = np.stack([grain_field(h, w, rng, size, roughness, clump) for _ in range(3)], -1)
        n = np.sqrt(1 - chroma) * shared[..., None] + np.sqrt(chroma) * per
    else:
        n = shared[..., None]
    wgt = grain_weight(luma(img), midtone_bias, shadow_bias)[..., None]
    return img + n * wgt * amount


def vignette_mask(h: int, w: int, midpoint: float = 0.5, roundness: float = 0.0, feather: float = 0.6,
                  cx: float = 0.5, cy: float = 0.5) -> np.ndarray:
    """0 in the centre -> 1 in the corners (smooth)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    u = (xx - cx * (w - 1)) / (w * 0.5)
    v = (yy - cy * (h - 1)) / (h * 0.5)
    r_ell = np.sqrt(u * u + v * v) / np.sqrt(2.0)
    s = 0.5 * np.hypot(w, h)
    r_circ = np.hypot(xx - cx * (w - 1), yy - cy * (h - 1)) / s
    r = r_ell * (1 - roundness) + r_circ * roundness
    t = np.clip((r - midpoint) / (max(1e-3, (1.0 - midpoint)) * max(feather, 0.02)), 0, 1)
    return (t * t * (3 - 2 * t)).astype(F32)


def apply_vignette(img: np.ndarray, stops: float, midpoint: float = 0.5, roundness: float = 0.0, feather: float = 0.6,
                   protect: float = 0.0, cx: float = 0.5, cy: float = 0.5) -> np.ndarray:
    if stops == 0:
        return img
    m = vignette_mask(*img.shape[:2], midpoint, roundness, feather, cx, cy)
    if protect > 0:
        m = m * (1 - protect * smoothstep(0.6, 1.0, luma(img)))
    return img * np.power(2.0, stops * m / 2.2)[..., None]


def paper_field(h: int, w: int, rng, scale: float = 1.0, fibres: float = 0.3) -> np.ndarray:
    """Unit-std paper height field: fine tooth + medium mottling + a few long soft fibres."""
    fine = noise_unit((h, w), rng, 0.7 * scale)
    med = noise_unit((h, w), rng, 2.5 * scale)
    coarse = noise_unit((h, w), rng, 12.0 * scale)
    hf = 0.55 * fine + 0.3 * med + 0.15 * coarse
    if fibres > 0:
        layer = np.zeros((h, w), np.uint8)
        n = int(fibres * 0.0006 * h * w / max(scale, 0.3) ** 1.0 / 4)
        for _ in range(n):
            x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
            L = rng.uniform(6, 30) * scale
            a = rng.uniform(0, np.pi)
            cv2.line(layer, (x, y), (int(x + L * np.cos(a)), int(y + L * np.sin(a))), int(rng.integers(120, 255)), 1, cv2.LINE_AA)
        fib = blur(layer.astype(F32) / 255.0, 0.6 * scale)
        hf = hf + fibres * 2.0 * (fib - fib.mean()) / (fib.std() + 1e-6) * 0.35
    return (hf / (hf.std() + 1e-8)).astype(F32)


def paper_multiplier(h: int, w: int, rng, strength: float = 0.08, scale: float = 1.0, fibres: float = 0.3,
                     relief: float = 0.5) -> np.ndarray:
    """Multiplicative paper texture (mean ~1)."""
    f = paper_field(h, w, rng, scale, fibres)
    lit = f.copy()
    if relief > 0:
        k = np.array([[-1, -1, 0], [-1, 0, 1], [0, 1, 1]], F32)
        lit = cv2.filter2D(blur(f, 0.8 * scale), -1, k)
        lit /= lit.std() + 1e-8
        lit = (1 - relief) * f + relief * lit
    return (1.0 + strength * lit).astype(F32)


# ----------------------------------------------------------------------------- optics (linear)
def lum_lin(x: np.ndarray) -> np.ndarray:
    return color.luminance(x)


def highlight_mask_lin(L: np.ndarray, threshold: float, knee: float = 0.3) -> np.ndarray:
    """Soft threshold on linear luminance; keeps growing above 1 so HDR highlights bleed more."""
    x = np.maximum(L - threshold, 0.0) / max(knee, 1e-3)
    return np.where(x < 1.0, x * x * (3 - 2 * x), x).astype(F32)


def halation_glow(L_or_img: np.ndarray, threshold: float, knee: float, radius: float, tint, spread: float = 3.0) -> np.ndarray:
    """Red-orange glow layer (to be ADDED to the linear image). Two-scale blur of the highlight mask."""
    L = lum_lin(L_or_img) if L_or_img.ndim == 3 else L_or_img
    m = highlight_mask_lin(L, threshold, knee) * np.maximum(L, 1e-3)
    g = 0.65 * blur(m, radius) + 0.35 * blur(m, radius * spread)
    return g[..., None] * np.asarray(tint, F32)


# ----------------------------------------------------------------------------- flow / LIC
def structure_flow(L: np.ndarray, sigma: float):
    """Smoothed structure-tensor tangent field (tx, ty) (unit vectors along edges) and coherence."""
    gx = cv2.Sobel(L, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(L, cv2.CV_32F, 0, 1, ksize=3)
    jxx, jyy, jxy = blur(gx * gx, sigma), blur(gy * gy, sigma), blur(gx * gy, sigma)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy)  # dominant gradient direction
    tx, ty = -np.sin(theta), np.cos(theta)         # tangent = perpendicular to gradient
    lam = np.sqrt((jxx - jyy) ** 2 + 4 * jxy ** 2)
    coh = lam / (jxx + jyy + 1e-8)
    return tx.astype(F32), ty.astype(F32), coh.astype(F32)


def lic(tex: np.ndarray, tx: np.ndarray, ty: np.ndarray, length: int = 16, step: float = 1.0) -> np.ndarray:
    """Line-integral convolution of `tex` (HxW or HxWxC) along the vector field (tx, ty) using cv2.remap."""
    h, w = tex.shape[:2]
    gx, gy = np.meshgrid(np.arange(w, dtype=F32), np.arange(h, dtype=F32))
    acc = tex.astype(F32).copy()
    wsum = 1.0
    for sgn in (1.0, -1.0):
        x, y = gx.copy(), gy.copy()
        for i in range(1, length + 1):
            vx = cv2.remap(tx, x, y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            vy = cv2.remap(ty, x, y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            if i == 1:
                ref = (vx.copy(), vy.copy())
            sign = np.sign(vx * ref[0] + vy * ref[1])
            sign[sign == 0] = 1
            x = x + sgn * step * vx * sign
            y = y + sgn * step * vy * sign
            wt = 0.5 * (1 + np.cos(np.pi * i / (length + 1)))
            acc += wt * cv2.remap(tex.astype(F32), x, y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            wsum += wt
    return (acc / wsum).astype(F32)


# ----------------------------------------------------------------------------- XDoG
def xdog_map(L: np.ndarray, sigma: float, k: float = 1.6, p: float = 20.0, eps: float = 0.75, phi: float = 1.0,
             flow: tuple | None = None, flow_len: int = 0) -> np.ndarray:
    """Winnemoeller (2012) extended difference of gaussians on L in 0..1.
    S = (1+p)G_s - p G_ks; T = 1 if S >= eps else 1 + tanh(phi (S-eps)). Returns T clipped to 0..1.
    `phi` is on the 0..1 intensity scale (paper phi on the 0..100 L scale x 100)."""
    g1 = blur(L, sigma)
    g2 = blur(L, k * sigma)
    S = (1.0 + p) * g1 - p * g2
    if flow is not None and flow_len > 0:
        S = lic(S, flow[0], flow[1], flow_len)
    T = np.where(S >= eps, 1.0, 1.0 + np.tanh(phi * (S - eps)))
    return np.clip(T, 0, 1).astype(F32)


def sat_boost_hsv(img: np.ndarray, s: float = 1.0, v: float = 1.0) -> np.ndarray:
    h = hsv(img)
    h[..., 1] = np.clip(h[..., 1] * s, 0, 1)
    h[..., 2] = np.clip(h[..., 2] * v, 0, 1)
    return color.hsv_to_rgb(h)


def finalize_docs(module_name: str) -> None:
    """Registry keeps only the first docstring line; give every op of `module_name` its full flattened docstring."""
    from ..registry import REGISTRY

    for spec in REGISTRY.values():
        if getattr(spec.fn, "__module__", "") == module_name and spec.fn.__doc__:
            spec.doc = " ".join(spec.fn.__doc__.split())
