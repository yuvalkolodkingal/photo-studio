"""Scene-referred basics: raw decode params, highlight reconstruction, exposure, white balance / CAT,
gamut compression, and the tone mappers (the scene->display boundary)."""
from __future__ import annotations

import numpy as np
from scipy.interpolate import PchipInterpolator

from ... import color
from ...filters import gaussian
from ..registry import TONEMAP_STAGE, Ctx, op


# --------------------------------------------------------------------------- stage 1-4: decode params
@op("raw", 4, "linear", raw_only=True, category="raw", demosaic="dht", wb="camera", highlight="reconstruct",
    half_size=False, choices={"demosaic": ["linear", "vng", "ppg", "ahd", "dcb", "dht", "aahd"],
                              "highlight": ["clip", "reconstruct", "ignore", "none"]})
def raw_params(img, p, ctx):
    """Decode parameters (demosaic, raw WB, highlight mode); consumed by the decoder, no-op in the pipe."""
    return img


def reconstruct_highlights(cam: np.ndarray, clip: np.ndarray, method: str = "reconstruct") -> np.ndarray:
    """cam: camera RGB after WB scaling; clip[c]: per-channel saturation level.
    clip        -> min(x, white) (LibRaw mode 0 behaviour, neutral white)
    reconstruct -> channel-ratio propagation from unclipped neighbours (inpaint-opposed style)
    ignore      -> leave unclipped (magenta highlights possible)"""
    if method in ("ignore", "none"):
        return cam
    white = float(np.min(clip))
    if method == "clip":
        return np.minimum(cam, white).astype(np.float32)
    thr = clip.reshape(1, 1, 3) * 0.985
    clipped = cam >= thr
    anyc = clipped.any(axis=2)
    if not anyc.any():
        return cam
    out = cam.copy()
    guide = cam.mean(axis=2) + 1e-6
    valid = (~anyc).astype(np.float32)
    h, w = guide.shape
    sig = max(2.0, max(h, w) / 300.0)
    ratios = np.empty_like(cam)
    for c in range(3):
        r = cam[..., c] / guide
        num = gaussian(r * valid, sig)
        den = gaussian(valid, sig)
        # multi-scale fill for large blown areas
        s2 = sig
        while (den < 1e-3).any() and s2 < max(h, w) / 4:
            s2 *= 3
            num = np.where(den < 1e-3, gaussian(r * valid, s2), num)
            den = np.where(den < 1e-3, gaussian(valid, s2), den)
        ratios[..., c] = np.where(den > 1e-6, num / np.maximum(den, 1e-6), 1.0)
    ratios = np.where(ratios <= 0, 1.0, ratios)
    unc = (~clipped).astype(np.float32)
    est_guide = (cam * unc).sum(2) / np.maximum((ratios * unc).sum(2), 1e-6)
    allc = clipped.all(axis=2)
    est = ratios * est_guide[..., None]
    rec = np.where(clipped, np.maximum(cam, est), cam)
    # fully clipped: neutral at the highest clip level
    rec[allc] = np.max(clip)
    out = rec.astype(np.float32)
    return out


# --------------------------------------------------------------------------- stage 10
@op("exposure", 10, "linear", ev=0.0, black=0.0, compensate_bias=False)
def exposure(img, p, ctx: Ctx):
    """Exposure in EV (x 2^ev) on linear data, optional black offset and EXIF bias compensation."""
    ev = float(p["ev"])
    if p.get("compensate_bias"):
        ev -= float(ctx.meta.get("exposure_comp") or 0.0)
    out = (img - float(p["black"])) * (2.0 ** ev)
    return out.astype(np.float32)


def _gray_world(img: np.ndarray, p: float = 1.0) -> np.ndarray:
    v = np.clip(img.reshape(-1, 3), 0, None)
    lum = v.mean(1)
    keep = (lum > np.percentile(lum, 2)) & (lum < np.percentile(lum, 98))
    v = v[keep] if keep.sum() > 100 else v
    if p == np.inf:
        return np.percentile(v, 99.5, axis=0)
    return np.power(np.mean(np.power(v, p), axis=0), 1.0 / p)


@op("white_balance", 10, "linear", temperature=6504.0, tint=0.0, method="cat16", auto=None, picker=None,
    choices={"method": ["cat16", "bradford", "vonkries", "multipliers"],
             "auto": [None, "grayworld", "shades_of_gray", "white_patch"]})
def white_balance(img, p, ctx: Ctx):
    """Colour calibration: chromatic adaptation (CAT16 default, Bradford) from an assumed scene illuminant
    (temperature/tint relative to the as-shot neutral, 6504 K = unchanged) or an auto/picked estimate."""
    src_xyz = None
    if p.get("picker"):
        x, y, w, h = p["picker"]
        H, W = img.shape[:2]
        patch = img[int(y * H):max(int(y * H) + 1, int((y + h) * H)), int(x * W):max(int(x * W) + 1, int((x + w) * W))]
        rgb = patch.reshape(-1, 3).mean(0)
        src_xyz = color.REC2020_TO_XYZ @ rgb
    elif p.get("auto"):
        pw = {"grayworld": 1.0, "shades_of_gray": 6.0, "white_patch": np.inf}[p["auto"]]
        rgb = _gray_world(img, pw)
        src_xyz = color.REC2020_TO_XYZ @ rgb
    if p.get("method") == "multipliers":
        if src_xyz is not None:
            rgb = color.XYZ_TO_REC2020 @ src_xyz
            g = rgb / rgb[1]
            return (img / g.astype(np.float32)).astype(np.float32)
        # map temp/tint to gains via CAT diagonal approximation
    if src_xyz is not None:
        src_xy = color.xyz_to_xy(src_xyz)
    else:
        T, tint = float(p["temperature"]), float(p["tint"])
        if abs(T - 6504.0) < 1 and abs(tint) < 1e-6:
            return img
        # scene assumed lit by T (relative to D65 neutral): adapt T -> D65.
        src_xy = color.cct_tint_to_xy(T, tint) - color.cct_to_xy(6504.0) + color.D65_XY
    method = p.get("method", "cat16")
    if method == "multipliers":
        method = "vonkries"
    M = color.cat_rec2020(src_xy, color.D65_XY, method)
    out = color.apply_matrix(img, M)
    return color.gamut_compress_negatives(out)


@op("gamut_compress", 10, "linear", threshold=0.8)
def gamut_compress(img, p, ctx):
    """Compress negative/imaginary colours toward the achromatic axis (ACES RGC style)."""
    return color.gamut_compress_negatives(img, float(p["threshold"]))


# --------------------------------------------------------------------------- stage 16: tone mappers
def _norm(img: np.ndarray, kind: str) -> np.ndarray:
    if kind == "max":
        return img.max(axis=2)
    if kind == "luminance":
        return color.luminance(img)
    if kind == "power":
        num = (img ** 3).sum(2)
        den = (img ** 2).sum(2)
        return num / np.maximum(den, 1e-10)
    if kind == "euclidean":
        return np.sqrt((img ** 2).sum(2)) / np.sqrt(3.0)
    raise ValueError(kind)


def sigmoid_curve(x: np.ndarray, contrast: float = 1.6, skew: float = 0.0, white: float = 1.0,
                  black: float = 0.0, grey: float = color.MIDDLE_GREY) -> np.ndarray:
    """Generalised log-logistic: y = white*(x^c/(x^c+k))^p with y(grey) = grey (display-linear).
    Reconstruction of darktable's sigmoid (validate against darktable output)."""
    c = max(0.1, float(contrast))
    pw = 5.0 ** (-float(skew))
    target = grey
    k = grey ** c * ((white / target) ** (1.0 / pw) - 1.0)
    xc = np.power(np.maximum(x, 0.0), c)
    y = white * np.power(xc / (xc + k), pw)
    return (black + (1 - black / max(white, 1e-6)) * y).astype(np.float32)


def filmic_curve(x: np.ndarray, white_ev: float = 4.0, black_ev: float = -8.0, contrast: float = 1.2,
                 latitude: float = 0.3, balance: float = 0.0, grey: float = color.MIDDLE_GREY) -> np.ndarray:
    """Log encoding around middle grey + monotone (PCHIP) S-curve with a linear latitude, then the
    display power that makes grey land on 18.45% (filmic rgb skeleton; no spline overshoot)."""
    rng = white_ev - black_ev
    xl = (np.log2(np.maximum(x, 1e-9) / grey) - black_ev) / rng
    xl = np.clip(xl, 0.0, 1.0)
    xg = -black_ev / rng
    gamma = 2.2
    yg = grey ** (1.0 / gamma)
    lat = np.clip(latitude, 0.01, 0.9) * min(xg, 1 - xg)
    lo = max(lat * (1 - balance), 1e-3)
    hi = max(lat * (1 + balance), 1e-3)
    slope = contrast * (1.0 / rng) * rng  # contrast in encoded units
    x_t, y_t = xg - lo, yg - slope * lo
    x_s, y_s = xg + hi, yg + slope * hi
    y_t = float(np.clip(y_t, 0.02, yg - 0.02))
    y_s = float(np.clip(y_s, yg + 0.02, 0.98))
    xs = np.array([0.0, x_t, xg, x_s, 1.0])
    ys = np.array([0.0, y_t, yg, y_s, 1.0])
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    keep = np.concatenate([[True], np.diff(xs) > 1e-4])
    f = PchipInterpolator(xs[keep], ys[keep])
    lut_x = np.linspace(0, 1, 4096)
    lut = np.clip(f(lut_x), 0, 1)
    y = np.interp(xl, lut_x, lut)
    return np.power(y, gamma).astype(np.float32)


def reinhard_curve(x, white: float = 8.0):
    return (x * (1 + x / (white * white)) / (1 + x)).astype(np.float32)


def aces_fit_curve(x):
    """Narkowicz rational fit of ACES RRT+ODT (exposure 0.6 pre-scale)."""
    x = np.maximum(x, 0) * 0.6
    return np.clip((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0, 1).astype(np.float32)


def soft_shoulder(x, start: float = 0.9):
    """Identity below `start`, smooth roll-off to 1.0 above (for already display-referred JPEG input)."""
    s = start
    over = np.maximum(x - s, 0)
    rolled = s + (1 - s) * (1 - np.exp(-over / (1 - s)))
    return np.where(x > s, rolled, x).astype(np.float32)


@op("tone_map", TONEMAP_STAGE, "linear", curve="auto", contrast=1.6, skew=0.0, hue_preserve=1.0,
    norm="max", white=1.0, black=0.0, white_ev=4.0, black_ev=-8.0, latitude=0.3, balance=0.0,
    highlight_desat=0.6,
    choices={"curve": ["auto", "sigmoid", "filmic", "reinhard", "aces", "none"],
             "norm": ["max", "luminance", "power", "euclidean"]})
def tone_map(img, p, ctx: Ctx):
    """Scene->display tone mapper: sigmoid (default for raw), filmic, reinhard, aces; 'none' (default
    for JPEG: soft shoulder only, never a second S-curve). Chroma-preserving norm with hue_preserve blend."""
    curve = p["curve"]
    if curve == "auto":
        curve = "sigmoid" if ctx.kind == "raw" else "none"
    if curve == "none":
        if not getattr(ctx, "shoulder", True):       # LUT baking: keep white at exactly 1.0
            return np.maximum(img, 0).astype(np.float32)
        return soft_shoulder(np.maximum(img, 0))

    def f(x):
        if curve == "sigmoid":
            return sigmoid_curve(x, p["contrast"], p["skew"], p["white"], p["black"])
        if curve == "filmic":
            return filmic_curve(x, p["white_ev"], p["black_ev"], p["contrast"], p["latitude"], p["balance"])
        if curve == "reinhard":
            return reinhard_curve(x, max(1.0, 2 ** p["white_ev"] * color.MIDDLE_GREY))
        return aces_fit_curve(x)

    x = np.maximum(img, 0.0)
    h = float(np.clip(p["hue_preserve"], 0, 1))
    out_ratio = None
    if h > 0:
        N = np.maximum(_norm(x, p["norm"]), 1e-9)
        Nm = f(N)
        out_ratio = x * (Nm / N)[..., None]
        # desaturate toward white where the ratio result exceeds the display ceiling
        mx = out_ratio.max(axis=2)
        t = color.smoothstep(0.85, 1.25, mx) * float(p["highlight_desat"])
        out_ratio = out_ratio * (1 - t[..., None]) + Nm[..., None] * t[..., None]
        over = out_ratio.max(axis=2, keepdims=True)
        out_ratio = np.where(over > 1.0, out_ratio / np.maximum(over, 1e-9) * np.minimum(over, 1.0), out_ratio)
    if h < 1:
        out_pc = f(x)
        if out_ratio is None:
            return out_pc
        return (out_pc * (1 - h) + out_ratio * h).astype(np.float32)
    return out_ratio.astype(np.float32)


def output_transform(img: np.ndarray, ctx: Ctx) -> np.ndarray:
    """Stage 17: linear Rec.2020 display-referred -> output primaries, Oklch chroma gamut map, OETF."""
    lin = color.rec2020_to_output(img, ctx.output_space)
    lin = color.gamut_map_to_unit(lin)
    return color.srgb_oetf(lin)  # sRGB and Display-P3 share the sRGB transfer curve
