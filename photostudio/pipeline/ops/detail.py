"""Detail ops: denoise, hot pixels, sharpening, deconvolution, defringe, lateral CA, output sharpening.

Registered ops (stage / domain)
    denoise            5 linear   luma/chroma separate (orthonormal opponent space), generalised-Anscombe VST
                                  (Poisson-Gaussian a,b from ISO or fitted), methods wavelet (a-trous + local
                                  BayesShrink) | bilateral | nlm (OpenCV, 16-bit) | bm3d (optional lazy import)
    profiled_denoise   5 linear   alias of denoise with the profiled (VST) wavelet defaults
    hot_pixels         5 linear   isolated-spike detection in VST space, replace by neighbour median
    capture_sharpen    6 linear   Richardson-Lucy, Gaussian PSF (auto radius from f-number), noise-gated
    ca_correct         7 linear   lateral CA: radial per-channel scaling, optional auto estimate
    defringe           7 linear   purple/green fringe desaturation at high-contrast edges (Oklch)
    sharpen           14 linear   unsharp mask on a hue-preserving perceptual luminance, threshold + halo clamp
    deconvolve        14 linear   Richardson-Lucy "lens deblur" (damped by the noise model)
    output_sharpen    21 display  post-resize USM tuned by medium (screen / web / print) and ppi

Public helpers
    estimate_sigma(x)                    robust noise sigma (Laplacian MAD, Immerkaer-style)
    noise_model_for_iso(iso, wb_gain)    generic APS-C Poisson-Gaussian (a, b); replace with a measured profile
    fit_noise_profile(img, bins)         fit sigma^2 = a*y + b from a single noisy image (binned MAD)
    gat_forward / gat_inverse(f, a, b)   generalised Anscombe transform and the closed-form unbiased inverse
    atrous_decompose(x, J), wavelet_shrink(x, sigma, J)   a-trous B3 wavelets and local BayesShrink
    richardson_lucy(Y, sigma_px, iters, ...)             RL deconvolution with Gaussian PSF
    unsharp_luma(img, sigma, amount, ...)                USM used by sharpen / output_sharpen
    estimate_lateral_ca(img)             (kr, kb) radial magnification of red/blue relative to green
"""
from __future__ import annotations

from functools import lru_cache

import cv2
import numpy as np

from ... import color
from ...filters import fast_guided, gaussian
from ...util import lazy_import
from ..registry import Ctx, op

EPS = 1e-8
_SQ2, _SQ3, _SQ6 = np.sqrt(2.0), np.sqrt(3.0), np.sqrt(6.0)
# orthonormal opponent transform (white noise stays white with unchanged variance)
_OPP = np.array([[1 / _SQ3, 1 / _SQ3, 1 / _SQ3],
                 [1 / _SQ2, 0.0, -1 / _SQ2],
                 [1 / _SQ6, -2 / _SQ6, 1 / _SQ6]], dtype=np.float32)


# =========================================================================== noise model / VST
def estimate_sigma(x: np.ndarray) -> float:
    """Noise sigma of a single-channel image: 1.4826 * MAD of the 3x3 Laplacian-difference response / 6
    (the response of white noise has std 6 sigma). Robust to edges."""
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    r = cv2.filter2D(np.ascontiguousarray(x, np.float32), -1, k, borderType=cv2.BORDER_REFLECT)
    r = r[2:-2, 2:-2]
    if r.size == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(r - np.median(r))) / 6.0)


def noise_model_for_iso(iso: float, wb_gain: float = 1.5) -> tuple[float, float]:
    """Generic APS-C 24 MP Poisson-Gaussian model sigma^2 = a*y + b on data normalised to 0..1 (14-bit raw),
    scaled for the typical white-balance gain. NOT measured for the R50: override with a/b params or
    `fit_noise_profile`. full well ~ 25 000 e- at ISO 100 and read noise ~ 3.2 e- falling with ISO."""
    iso = max(float(iso), 50.0)
    fwc = 25000.0 * 100.0 / iso
    read = max(1.6, 3.2 - 0.35 * np.log2(iso / 100.0))
    a = 1.0 / fwc
    b = (read / fwc) ** 2
    return float(a * wb_gain), float(b * wb_gain ** 2)


def fit_noise_profile(img: np.ndarray, bins: int = 32, min_count: int = 500) -> tuple[float, float]:
    """Fit sigma^2 = a*y + b from one image: bin pixels by a smoothed local mean, estimate sigma per bin from the
    MAD of x - blur(x), least squares in (mean, var). Returns (a, b) with a >= 0, b >= 0."""
    g = img if img.ndim == 2 else img.mean(axis=2)
    g = np.ascontiguousarray(g, np.float32)
    sm = cv2.GaussianBlur(g, (0, 0), 2.0)
    res = (g - sm) * (1.0 / 0.93)   # blur removes a small share of the noise
    hi = np.percentile(sm, 99.5)
    edges = np.linspace(max(float(sm.min()), 0.0), max(hi, 1e-4), bins + 1)
    idx = np.clip(np.digitize(sm.ravel(), edges) - 1, 0, bins - 1)
    r = res.ravel()
    xs, vs = [], []
    for b in range(bins):
        sel = idx == b
        if sel.sum() < min_count:
            continue
        rv = r[sel]
        s = 1.4826 * np.median(np.abs(rv - np.median(rv)))
        xs.append(float(sm.ravel()[sel].mean()))
        vs.append(float(s * s))
    if len(xs) < 3:
        s = estimate_sigma(g)
        return 0.0, s * s
    A = np.stack([xs, np.ones(len(xs))], 1)
    a, b = np.linalg.lstsq(A, np.asarray(vs), rcond=None)[0]
    return float(max(a, 0.0)), float(max(b, 0.0))


def gat_forward(y: np.ndarray, a: float, b: float) -> np.ndarray:
    """Generalised Anscombe: f = (2/a) sqrt(max(a y + 3a^2/8 + b, 0)); noise std ~ 1."""
    return ((2.0 / a) * np.sqrt(np.maximum(a * y + 0.375 * a * a + b, 0.0))).astype(np.float32)


def gat_inverse(f: np.ndarray, a: float, b: float) -> np.ndarray:
    """Closed-form asymptotically unbiased inverse (Makitalo & Foi 2011) applied on the equivalent Poisson variable
    y' = y/a + b/a^2:  I(D) = D^2/4 + sqrt(3/2)/(4D) - 11/(8D^2) + 5 sqrt(3/2)/(8D^3) - 1/8."""
    D = np.maximum(f.astype(np.float64), 1.0)
    s32 = np.sqrt(1.5)
    yp = D * D / 4.0 + s32 / (4.0 * D) - 11.0 / (8.0 * D ** 2) + 5.0 * s32 / (8.0 * D ** 3) - 0.125
    return (a * yp - b / a).astype(np.float32)


# =========================================================================== a-trous wavelets
_B3 = np.array([1, 4, 6, 4, 1], np.float32) / 16.0


def _kernel(j: int) -> np.ndarray:
    d = 2 ** j
    k = np.zeros(4 * d + 1, np.float32)
    k[::d] = _B3
    return k


def atrous_decompose(x: np.ndarray, J: int):
    """Undecimated B3-spline a-trous: returns (details[0..J-1] fine->coarse, residual). sum = x exactly."""
    c = np.ascontiguousarray(x, np.float32)
    details = []
    for j in range(J):
        k = _kernel(j)
        cn = cv2.sepFilter2D(c, -1, k, k, borderType=cv2.BORDER_REFLECT)
        details.append(c - cn)
        c = cn
    return details, c


@lru_cache(maxsize=1)
def _atrous_noise_factors(J: int = 8) -> tuple:
    """Std of each a-trous detail band for unit-variance white noise (computed numerically; deterministic)."""
    rng = np.random.default_rng(1234)
    n = rng.standard_normal((384, 384)).astype(np.float32)
    det, _ = atrous_decompose(n, J)
    return tuple(float(d[40:-40, 40:-40].std()) for d in det)


def wavelet_shrink(x: np.ndarray, sigma: float, J: int = 5, strength: float = 1.0, preserve: float = 0.0,
                   scale_weights=None) -> np.ndarray:
    """Local BayesShrink soft thresholding of a-trous bands. sigma: noise std of x (white). strength scales the
    threshold, preserve (0..1) adds back that fraction of the removed detail; scale_weights per band multiplier."""
    if sigma <= 0 or strength <= 0:
        return x
    fac = _atrous_noise_factors(max(J, 8))
    det, res = atrous_decompose(x, J)
    out_det = []
    for j, w in enumerate(det):
        sn = sigma * fac[j] * strength
        r = int(min(24, max(2, 2 * 2 ** j)))
        var = cv2.boxFilter(w * w, -1, (2 * r + 1, 2 * r + 1), borderType=cv2.BORDER_REFLECT)
        sx = np.sqrt(np.maximum(var - sn * sn, 1e-12))
        T = np.minimum(sn * sn / sx, 3.0 * sn)
        if scale_weights is not None:
            T = T * float(scale_weights[j])
        ws = np.sign(w) * np.maximum(np.abs(w) - T, 0.0)
        if preserve > 0:
            ws = ws + preserve * (w - ws)
        out_det.append(ws)
    return (res + sum(out_det)).astype(np.float32)


# =========================================================================== denoise
def _iso_factor(meta: dict) -> float:
    iso = meta.get("ISO") or meta.get("iso")
    try:
        iso = float(iso)
    except (TypeError, ValueError):
        return 1.0
    return float(np.clip(1.0 + 0.25 * np.log2(max(iso, 50.0) / 800.0), 0.4, 1.6))


def _denoise_channel(z, method, sigma, k, J, preserve, ctx, img_for_px):
    if k <= 0 or sigma <= 0:
        return z
    if method == "bilateral":
        sp = max(1.0, ctx.px(2.5, img_for_px))
        d = int(min(15, 2 * np.ceil(1.5 * sp) + 1))
        out = z
        for _ in range(1 + int(k > 1.5)):
            out = cv2.bilateralFilter(np.ascontiguousarray(out), d, float(sigma * k * 2.0), float(sp))
        return out.astype(np.float32)
    if method == "nlm":
        lo, hi = float(np.percentile(z, 0.1)), float(np.percentile(z, 99.9))
        s = 60000.0 / max(hi - lo, 1e-6)
        u16 = np.clip((z - lo) * s, 0, 65535).astype(np.uint16)
        h = float(max(sigma * k * s * 0.9, 1.0))
        out = cv2.fastNlMeansDenoising(u16, [h], None, 7, 21, cv2.NORM_L1)
        return (out.astype(np.float32) / s + lo).astype(np.float32)
    if method == "bm3d":
        bm = lazy_import("bm3d", "BM3D denoising")
        lo, hi = float(z.min()), float(z.max())
        rng = max(hi - lo, 1e-6)       # BM3D expects data in ~[0, 1]; normalise and scale sigma accordingly
        out = bm.bm3d(np.ascontiguousarray((z - lo) / rng), sigma_psd=float(sigma * k / rng), profile="np")
        return (np.asarray(out, np.float32) * rng + lo).astype(np.float32)
    return wavelet_shrink(z, sigma, J, k, preserve)


@op("denoise", 5, "linear", luma=50.0, chroma=70.0, method="wavelet", detail=20.0, scales=5, iso_adapt=True,
    vst=True, a=None, b=None, profile="iso", wb_gain=1.5, sigma=None, chroma_smooth=1.0,
    choices={"method": ["wavelet", "bilateral", "nlm", "bm3d"], "profile": ["iso", "fit"]}, category="detail")
def denoise(img, p, ctx: Ctx):
    """Luma/chroma denoise in an orthonormal opponent space after a variance-stabilising transform (profiled
    Poisson-Gaussian noise): luma/chroma 0..100 (50 = nominal 1 sigma), ISO-adaptive via ctx.meta['ISO'].
    Methods: wavelet (a-trous + BayesShrink), bilateral, nlm (OpenCV), bm3d (optional)."""
    kl, kc = float(p["luma"]) / 50.0, float(p["chroma"]) / 50.0
    if kl <= 0 and kc <= 0:
        return img
    if p.get("iso_adapt"):
        f = _iso_factor(ctx.meta)
        kl, kc = kl * f, kc * f
    linear = ctx.domain != "display" and bool(p.get("vst", True))
    x = np.ascontiguousarray(img, np.float32)
    if linear:
        if p.get("a") is not None and p.get("b") is not None:
            a, b = float(p["a"]), float(p["b"])
        elif p.get("profile") == "fit":
            a, b = fit_noise_profile(np.maximum(x, 0.0))
            a = max(a, 1e-6)
        else:
            iso = ctx.meta.get("ISO") or 400
            a, b = noise_model_for_iso(float(iso), float(p["wb_gain"]))
        z = gat_forward(x, a, b)
    else:
        z = x
    opp = z @ _OPP.T
    Y, U, V = (np.ascontiguousarray(opp[..., i]) for i in range(3))
    cap = 1.5 if linear else 10.0
    if p.get("sigma") is not None:
        sig_y = sig_c = float(p["sigma"])
    else:
        sig_y = min(estimate_sigma(Y), cap)
        sig_c = min(max(estimate_sigma(U), estimate_sigma(V), 0.7 * sig_y), cap)
    sigma = sig_y
    J = int(p["scales"])
    pres = float(p["detail"]) / 100.0
    method = p["method"]
    Yd = _denoise_channel(Y, method, sigma, kl, J, pres, ctx, x)
    cm = "bilateral" if method == "bilateral" else "wavelet"
    Ud = _denoise_channel(U, cm, sig_c, kc, J + 1, 0.0, ctx, x)
    Vd = _denoise_channel(V, cm, sig_c, kc, J + 1, 0.0, ctx, x)
    cs = float(p["chroma_smooth"])
    if kc > 0 and cs > 0 and sig_c > 0.1:   # luma-guided smoothing removes the low-frequency colour blotches
        r = max(2, int(ctx.px(3.0 + 6.0 * min(kc, 3.0), x)))
        # normalise the guide first: E[I^2]-E[I]^2 cancels catastrophically for large offsets / small eps
        mu, sd = float(Yd.mean()), float(Yd.std()) + 1e-6
        gn = ((Yd - mu) / sd).astype(np.float32)
        eps = max(((0.5 * sig_c * max(kc, 0.1)) / sd) ** 2, 1e-5)
        Ug = fast_guided(gn, Ud, r, eps)
        Vg = fast_guided(gn, Vd, r, eps)
        Ud, Vd = Ud + cs * (Ug - Ud), Vd + cs * (Vg - Vd)
    zd = np.stack([Yd, Ud, Vd], -1) @ _OPP        # orthonormal: inverse = transpose
    out = gat_inverse(zd, a, b) if linear else zd
    return out.astype(np.float32)


@op("profiled_denoise", 5, "linear", luma=50.0, chroma=70.0, detail=20.0, scales=5, iso_adapt=True, a=None, b=None,
    profile="iso", wb_gain=1.5, category="detail")
def profiled_denoise(img, p, ctx: Ctx):
    """Profiled denoise: variance-stabilised (Poisson-Gaussian a,b per ISO or fitted) wavelet denoise."""
    q = dict(p)
    q.update(method="wavelet", vst=True, sigma=None, chroma_smooth=1.0)
    return denoise(img, q, ctx)


# =========================================================================== hot pixels
@op("hot_pixels", 5, "linear", threshold=6.0, dead=False, amount=1.0, category="detail")
def hot_pixels(img, p, ctx: Ctx):
    """Remove isolated hot (and optionally dead) pixels: a pixel is replaced by the 3x3 median when it is the
    strict local extreme and deviates by > threshold sigma (noise-stabilised). RGB fallback of the raw-stage filter."""
    thr = float(p["threshold"])
    if thr <= 0:
        return img
    iso = ctx.meta.get("ISO") or 400
    a, b = noise_model_for_iso(float(iso))
    out = np.array(img, np.float32, copy=True)
    for c in range(3):
        ch = np.ascontiguousarray(img[..., c])
        z = gat_forward(np.maximum(ch, 0.0), a, b)
        sig = max(estimate_sigma(z), 0.15)
        med = cv2.medianBlur(z, 3)
        # strict extreme among 8 neighbours: compare against max/min of neighbours excluding centre
        k8 = np.ones((3, 3), np.uint8)
        k8[1, 1] = 0
        nmax = cv2.dilate(z, k8)
        nmin = cv2.erode(z, k8)
        hot = (z > nmax) & ((z - med) > thr * sig)
        if p.get("dead"):
            hot |= (z < nmin) & ((med - z) > thr * sig)
        if hot.any():
            nb = cv2.medianBlur(ch, 3)
            out[..., c] = np.where(hot, ch + (nb - ch) * float(p["amount"]), ch)
    return out


# =========================================================================== sharpening
def _hue_ratio_apply(img, Y, Yn):
    """Scale RGB by Yn/Y (ratio preserving); additive fallback in deep black."""
    ratio = Yn / np.maximum(Y, 1e-6)
    out = np.where((Y > 1e-6)[..., None], img * ratio[..., None], img + (Yn - Y)[..., None])
    return out.astype(np.float32)


def _enc(Y):   # perceptual encode / decode of luminance
    return color.srgb_oetf(np.maximum(Y, 0.0))


def unsharp_luma(img: np.ndarray, sigma: float, amount: float, threshold: float = 0.0, masking: float = 0.0,
                 halo: float = 0.0, domain: str = "linear", luma_weights=None) -> np.ndarray:
    """Unsharp mask on a hue-preserving perceptual luminance. threshold (encoded units) soft-gates small detail,
    masking (0..1) limits the effect to midtones, halo (0..1) clamps overshoot to the local min/max."""
    if amount == 0 or sigma <= 0:
        return img
    if domain == "linear":
        Y = color.luminance(img)
        Ye = _enc(Y)
    else:
        Y = color.luma(img)
        Ye = Y
    blur = gaussian(Ye, sigma)
    d = Ye - blur
    w = np.ones_like(d)
    if threshold > 0:
        w = color.smoothstep(threshold * 0.5, threshold * 1.5, np.abs(d))
    if masking > 0:
        w = w * (1.0 - masking * np.clip(2.0 * np.abs(Ye - 0.5), 0.0, 1.0) ** 2)
    Yn = Ye + amount * d * w
    if halo > 0:
        k = int(max(3, 2 * round(1.5 * sigma) + 1))
        ker = np.ones((k, k), np.uint8)
        lo, hi = cv2.erode(Ye, ker), cv2.dilate(Ye, ker)
        Yc = np.clip(Yn, lo, hi)
        Yn = Yc * halo + Yn * (1.0 - halo)
    if domain == "linear":
        return _hue_ratio_apply(img, Y, color.srgb_eotf(np.maximum(Yn, 0.0)))
    return np.clip(_hue_ratio_apply(img, Y, np.maximum(Yn, 0.0)), 0.0, 1.0)


@op("sharpen", 14, "linear", amount=0.5, radius=1.0, threshold=0.0, masking=0.3, halo=0.5, category="detail")
def sharpen(img, p, ctx: Ctx):
    """Unsharp mask on perceptual luminance (hue preserving). radius in px at 2000 px long edge; amount 0..3;
    threshold in encoded units (0.01 ~ low noise); masking limits to midtones; halo clamps overshoot."""
    sigma = ctx.px(p["radius"], img)
    return unsharp_luma(img, sigma, float(p["amount"]), float(p["threshold"]), float(p["masking"]), float(p["halo"]))


def richardson_lucy(Y: np.ndarray, sigma_px: float, iters: int = 10, sigma_noise=None, damping: float = 0.0,
                    gate: np.ndarray | None = None, max_corr: float = 2.0) -> np.ndarray:
    """RL deconvolution of a linear single-channel image with a Gaussian PSF (the Gaussian is its own adjoint).
    damping > 0: damped RL (White 1994) - residuals below damping * sigma_noise(y) barely update the estimate.
    sigma_noise: callable y -> noise std (linear units) or None. gate: HxW 0..1 weight on the update."""
    Y = np.ascontiguousarray(np.maximum(Y, 0.0), np.float32)
    u = Y.copy()
    floor = float(Y.mean()) * 1e-3 + 1e-7
    for _ in range(int(iters)):
        est = gaussian(u, sigma_px)
        resid = Y - est
        ratio = Y / (est + floor)
        if damping > 0 and sigma_noise is not None:
            t = np.abs(resid) / (damping * sigma_noise(est) + 1e-9)
            wdamp = 1.0 - np.exp(-t * t)
            ratio = 1.0 + (ratio - 1.0) * wdamp
        corr = gaussian(ratio, sigma_px)
        corr = np.clip(corr, 1.0 / max_corr, max_corr)
        if gate is not None:
            corr = 1.0 + (corr - 1.0) * gate
        u = u * corr
    return u


def _contrast_gate(Y: np.ndarray, thr_sigma: float, floor_thr: float = 0.0) -> np.ndarray:
    """Weight 0..1: local std of encoded luminance above k * noise sigma (RT 'contrast threshold')."""
    Ye = _enc(Y)
    sig = estimate_sigma(Ye)
    mean = cv2.boxFilter(Ye, -1, (5, 5), borderType=cv2.BORDER_REFLECT)
    sq = cv2.boxFilter(Ye * Ye, -1, (5, 5), borderType=cv2.BORDER_REFLECT)
    std = np.sqrt(np.maximum(sq - mean * mean, 0.0))
    t = max(thr_sigma * sig, floor_thr, 1e-4)
    return color.smoothstep(t, 2.0 * t, std).astype(np.float32)


def _noise_fn(ctx: Ctx):
    iso = ctx.meta.get("ISO") or 400
    a, b = noise_model_for_iso(float(iso))
    return lambda y: np.sqrt(np.maximum(a * y + b, 1e-12))


def _rl_luma(img, sigma_px, iters, amount, damping, thr_sigma, ctx):
    Y = color.luminance(np.maximum(img, 0.0))
    gate = _contrast_gate(Y, thr_sigma) if thr_sigma > 0 else None
    u = richardson_lucy(Y, sigma_px, iters, _noise_fn(ctx), damping, gate)
    Yn = Y + float(amount) * (u - Y)
    return _hue_ratio_apply(img, Y, Yn)


@op("capture_sharpen", 6, "linear", amount=0.8, radius=None, iterations=8, threshold=2.5, damping=0.5,
    category="detail")
def capture_sharpen(img, p, ctx: Ctx):
    """Capture sharpening: Richardson-Lucy with a small Gaussian PSF in linear light, gated by a noise-aware
    local-contrast mask. radius = native sigma in px (None = auto from lens/diffraction: R50 3.7 um pitch)."""
    if float(p["amount"]) <= 0:
        return img
    if p.get("radius") is None:
        N = float(ctx.meta.get("aperture") or ctx.meta.get("FNumber") or 5.6)
        sig_native = float(np.clip(np.hypot(0.6, 0.062 * N), 0.5, 1.6))
    else:
        sig_native = float(p["radius"])
    scale = max(img.shape[:2]) / float(ctx.full_long_edge or max(img.shape[:2]))
    sig = sig_native * scale
    amount = float(p["amount"])
    if sig < 0.35:      # preview below native resolution: fade out, nothing meaningful to deconvolve
        amount *= (sig / 0.35) ** 2
        sig = 0.35
        ctx.note("capture_sharpen: preview scale below native resolution, effect faded")
    return _rl_luma(img, sig, int(p["iterations"]), amount, float(p["damping"]), float(p["threshold"]), ctx)


@op("deconvolve", 14, "linear", radius=1.0, iterations=12, amount=1.0, damping=1.0, threshold=2.0, category="detail")
def deconvolve(img, p, ctx: Ctx):
    """Richardson-Lucy deconvolution (lens deblur) with Gaussian PSF sigma = radius (px at 2000 px long edge),
    damped by the sensor noise model so noise is not amplified."""
    if float(p["amount"]) <= 0 or float(p["radius"]) <= 0:
        return img
    return _rl_luma(img, max(ctx.px(p["radius"], img), 0.3), int(p["iterations"]), float(p["amount"]),
                    float(p["damping"]), float(p["threshold"]), ctx)


@op("output_sharpen", 21, "display", medium="screen", amount=None, radius=None, ppi=None, threshold=0.0,
    choices={"medium": ["screen", "web", "print"]}, category="detail")
def output_sharpen(img, p, ctx: Ctx):
    """Post-resize sharpening on the final pixels: screen/web 0.5 px, print scales with ppi (0.8 px at 300).
    amount/radius override the medium defaults. Radius is in output pixels (no preview scaling)."""
    med = p["medium"]
    ppi = float(p["ppi"] or 300.0)
    base_r = {"screen": 0.5, "web": 0.6, "print": 0.8 * ppi / 300.0}[med]
    base_a = {"screen": 0.5, "web": 0.6, "print": 1.0}[med]
    r = float(p["radius"]) if p.get("radius") is not None else base_r
    a = float(p["amount"]) if p.get("amount") is not None else base_a
    return unsharp_luma(np.clip(img, 0, 1), r, a, float(p["threshold"]), 0.2, 0.5, domain="display")


# =========================================================================== defringe
_HUE_RANGES = {"purple": (270.0, 345.0), "green": (95.0, 165.0)}   # Oklch degrees


def _hue_window(h, lo, hi, soft=15.0):
    mid, half = 0.5 * (lo + hi), 0.5 * (hi - lo)
    d = np.abs((h - mid + 180.0) % 360.0 - 180.0)
    return 1.0 - color.smoothstep(half, half + soft, d)


@op("defringe", 7, "linear", amount=60.0, radius=3.0, threshold=0.04, hues=["purple", "green"], mode="average",
    chroma_min=0.03, choices={"mode": ["average", "neutral"]}, category="detail")
def defringe(img, p, ctx: Ctx):
    """Purple/green fringe removal: chroma of fringe-hued pixels within `radius` px (at 2000) of strong luminance
    edges is pulled toward the local non-fringe average (or neutral)."""
    amount = float(p["amount"]) / 100.0
    if amount <= 0:
        return img
    Y = color.luminance(np.maximum(img, 0.0))
    Ye = _enc(Y)
    gx = cv2.Sobel(Ye, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(Ye, cv2.CV_32F, 0, 1, ksize=3)
    edge = np.hypot(gx, gy) * 0.25
    r = max(1.0, ctx.px(p["radius"], img))
    k = int(2 * round(r) + 1)
    edge = cv2.dilate(edge, np.ones((k, k), np.uint8))
    edge = gaussian(edge, max(r * 0.5, 0.5))
    t = float(p["threshold"])
    strong = color.smoothstep(t, 2.5 * t, edge)
    lab = color.lin_to_oklab(img if img.min() >= 0 else np.maximum(img, 0.0), "rec2020")
    C = np.hypot(lab[..., 1], lab[..., 2])
    h = np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) % 360.0
    hw = np.zeros_like(C)
    for name in p["hues"]:
        lo, hi = _HUE_RANGES[name]
        hw = np.maximum(hw, _hue_window(h, lo, hi))
    cm = float(p["chroma_min"])
    fw = strong * hw * color.smoothstep(cm * 0.5, cm * 1.5, C) * amount
    if p["mode"] == "neutral":
        target = np.zeros_like(C)
    else:
        keep = 1.0 - np.clip(strong * hw, 0.0, 1.0)
        sig = max(r * 3.0, 2.0)
        num, den = gaussian(C * keep, sig), gaussian(keep, sig)
        target = np.where(den > 1e-3, num / np.maximum(den, 1e-3), 0.0)
    Cn = C + (np.minimum(target, C) - C) * np.clip(fw, 0.0, 1.0)
    k2 = (Cn / np.maximum(C, 1e-6)).astype(np.float32)
    lab2 = lab.copy()
    lab2[..., 1] *= k2
    lab2[..., 2] *= k2
    out = color.oklab_to_lin(lab2, "rec2020")
    if out.min() < -1e-5:
        out = color.gamut_compress_negatives(out)
    wmask = np.clip(fw, 0, 1)[..., None]
    return (img + (out - img) * wmask).astype(np.float32)


# =========================================================================== lateral CA
def _radial_remap(ch: np.ndarray, s: float, cx: float, cy: float, interp=cv2.INTER_CUBIC) -> np.ndarray:
    """out(p) = ch(c + (p - c) * s) : samples the channel at a radially scaled position."""
    h, w = ch.shape
    xs = (np.arange(w, dtype=np.float32) - cx) * np.float32(s) + cx
    ys = (np.arange(h, dtype=np.float32) - cy) * np.float32(s) + cy
    mapx = np.broadcast_to(xs[None, :], (h, w)).copy()
    mapy = np.broadcast_to(ys[:, None], (h, w)).copy()
    return cv2.remap(np.ascontiguousarray(ch), mapx, mapy, interp, borderMode=cv2.BORDER_REFLECT)


def _gradmag(ch):
    gx = cv2.Sobel(ch, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(ch, cv2.CV_32F, 0, 1, ksize=3)
    return np.hypot(gx, gy)


def estimate_lateral_ca(img: np.ndarray, center=(0.5, 0.5), max_scale: float = 0.004, work_px: int = 900):
    """Estimate relative radial magnification (kr, kb) of red / blue versus green by maximising the correlation of
    gradient magnitudes in the outer image region. Positive k: the channel is magnified (fringes outward)."""
    h, w = img.shape[:2]
    s = min(1.0, work_px / max(h, w))
    small = cv2.resize(np.ascontiguousarray(img), (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)
    enc = color.srgb_oetf(np.maximum(small, 0.0))
    H, W = enc.shape[:2]
    cx, cy = center[0] * (W - 1), center[1] * (H - 1)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    rr = np.hypot(xx - cx, yy - cy) / np.hypot(max(cx, W - 1 - cx), max(cy, H - 1 - cy))
    gG = _gradmag(enc[..., 1])
    sel = (rr > 0.3) & (gG > np.percentile(gG, 80))
    if sel.sum() < 200:
        return 0.0, 0.0

    def score(c, k):
        g = _gradmag(_radial_remap(enc[..., c], 1.0 + k, cx, cy))
        a, b = gG[sel], g[sel]
        a = a - a.mean()
        b = b - b.mean()
        return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-12))

    res = []
    for c in (0, 2):
        ks = np.linspace(-max_scale, max_scale, 17)
        best = ks[int(np.argmax([score(c, k) for k in ks]))]
        ks2 = np.linspace(best - max_scale / 8, best + max_scale / 8, 9)
        sc2 = [score(c, k) for k in ks2]
        k = float(ks2[int(np.argmax(sc2))])
        res.append(k)
    return res[0], res[1]


@op("ca_correct", 7, "linear", red=0.0, blue=0.0, auto=False, strength=1.0, center=[0.5, 0.5], category="detail")
def ca_correct(img, p, ctx: Ctx):
    """Lateral chromatic aberration: red / blue are radially rescaled to match green. red / blue = relative
    magnification of that channel (e.g. 0.001 = 0.1 %); auto estimates them from the image and adds the manual values."""
    kr, kb = float(p["red"]), float(p["blue"])
    cx, cy = p["center"]
    if p.get("auto"):
        ar, ab = estimate_lateral_ca(img, (cx, cy))
        kr += ar * float(p["strength"])
        kb += ab * float(p["strength"])
        ctx.note(f"ca_correct auto: red {ar:+.5f} blue {ab:+.5f}")
    if kr == 0 and kb == 0:
        return img
    h, w = img.shape[:2]
    px, py = cx * (w - 1), cy * (h - 1)
    out = np.array(img, np.float32, copy=True)
    for c, k in ((0, kr), (2, kb)):
        if k != 0:
            out[..., c] = _radial_remap(img[..., c], 1.0 + k, px, py)
    return out
