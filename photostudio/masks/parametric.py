"""Parametric masks (darktable-style trapezoids), Kuyper luminosity masks and colour-range masks.

Trapezoid: channel value v with markers (o_lo, i_lo, i_hi, o_hi) -> clip((v-o_lo)/(i_lo-o_lo)) * clip((o_hi-v)/(o_hi-i_hi));
the final mask is the product over channels. Hue is circular (markers wrap). Channels:
  L, C, h   Oklch lightness 0..1, chroma ~0..0.4, hue degrees (of the image in its own domain)
  R, G, B   raw channel values of the image in its own domain
  g / Y     luminance (weighted RGB) in the image domain
  ev        scene luminance in EV relative to 0.18 (works for linear and display images)
A channel entry is either the 4-list or {range: [...], invert: bool, smooth: bool}.
"""
from __future__ import annotations

import numpy as np

from .. import color
from .common import get_domain, linear_luminance, smoothstep, to_display

MIDDLE = 0.18


def trapezoid(v: np.ndarray, markers, smooth: bool = False, circular: bool = False, period: float = 360.0) -> np.ndarray:
    o_lo, i_lo, i_hi, o_hi = [float(m) for m in markers]
    if circular:
        m = [o_lo, i_lo, i_hi, o_hi]
        for k in range(1, 4):
            while m[k] < m[k - 1]:
                m[k] += period
        c = 0.5 * (m[1] + m[2])
        rel = [x - c for x in m]
        v = (v - c + period / 2.0) % period - period / 2.0
        o_lo, i_lo, i_hi, o_hi = rel
    if i_lo > o_lo:
        left = np.clip((v - o_lo) / (i_lo - o_lo), 0.0, 1.0)
    else:
        left = (v >= i_lo).astype(np.float32)
    if o_hi > i_hi:
        right = np.clip((o_hi - v) / (o_hi - i_hi), 0.0, 1.0)
    else:
        right = (v <= i_hi).astype(np.float32)
    out = left * right
    if smooth:
        out = out * out * (3.0 - 2.0 * out)
    return out.astype(np.float32)


def _channel_arrays(img: np.ndarray, domain: str, need: set[str]) -> dict[str, np.ndarray]:
    ch: dict[str, np.ndarray] = {}
    if need & {"L", "C", "h"}:
        lch = color.to_oklch(np.maximum(img, 0) if domain == "linear" else np.clip(img, 0, 1), domain)
        ch["L"], ch["C"], ch["h"] = lch[..., 0], lch[..., 1], lch[..., 2]
    for i, n in enumerate("RGB"):
        if n in need:
            ch[n] = img[..., i]
    if need & {"g", "Y", "y"}:
        ch["g"] = ch["Y"] = ch["y"] = (color.luminance(img, "rec2020") if domain == "linear"
                                       else color.luma(np.clip(img, 0, 1)))
    if "ev" in need:
        ch["ev"] = np.log2(np.maximum(linear_luminance(img, domain), 1e-6) / MIDDLE)
    return ch


_ALIASES = {"l": "L", "c": "C", "chroma": "C", "hue": "h", "H": "h", "lightness": "L", "r": "R", "g_": "G", "b": "B",
            "luma": "Y", "lum": "Y", "luminance": "Y", "exposure": "ev", "EV": "ev"}


def parametric_mask(spec: dict, img: np.ndarray, ctx) -> np.ndarray:
    domain = get_domain(ctx)
    chans = dict(spec.get("channels") or {})
    for k in ("L", "C", "h", "R", "G", "B", "g", "Y", "ev"):
        if k in spec and k not in chans:
            chans[k] = spec[k]
    if not chans:
        return np.ones(img.shape[:2], np.float32)
    norm = {}
    for k, v in chans.items():
        kk = _ALIASES.get(k, k)
        norm[kk] = v
    arrays = _channel_arrays(img, domain, set(norm))
    out = np.ones(img.shape[:2], np.float32)
    gsmooth = bool(spec.get("smooth", False))
    for k, v in norm.items():
        if k not in arrays:
            raise ValueError(f"parametric mask: unknown channel '{k}'")
        if isinstance(v, dict):
            rng, inv, sm = v.get("range"), bool(v.get("invert", False)), bool(v.get("smooth", gsmooth))
        else:
            rng, inv, sm = v, False, gsmooth
        m = trapezoid(arrays[k], rng, smooth=sm, circular=(k == "h"))
        out *= (1.0 - m) if inv else m
    return out


# --------------------------------------------------------------------------- luminosity (Kuyper)
def luminosity_values(img: np.ndarray, domain: str, ev_offset: float = 0.0) -> np.ndarray:
    """Display-referred lightness 0..1 used by luminosity masks (sRGB-encoded luminance)."""
    if domain == "display":
        return color.luma(np.clip(img, 0, 1))
    y = np.maximum(color.luminance(img, "rec2020"), 0) * (2.0 ** ev_offset)
    return color.srgb_oetf(np.clip(y, 0, 1))


def luminosity_mask(spec: dict, img: np.ndarray, ctx) -> np.ndarray:
    """Tony Kuyper luminosity masks. mode: lights|darks|midtones (also 'lights3'), level 1..5:
    Lights_n = L^n, Darks_n = (1-L)^n, Midtones_n = (1-|2L-1|)^n (tent, narrower with n).
    Alternative: `range: [lo, hi]` + `softness` (display lightness) for a plain band."""
    domain = get_domain(ctx)
    L = luminosity_values(img, domain, float(spec.get("ev_offset", 0.0)))
    if spec.get("range") is not None:
        lo, hi = [float(v) for v in spec["range"]]
        s = float(spec.get("softness", 0.1))
        return trapezoid(L, (lo - s, lo, hi, hi + s), smooth=True)
    mode = str(spec.get("mode", "lights")).lower()
    level = int(spec.get("level", 1))
    if mode[-1:].isdigit():
        level = int(mode[-1])
        mode = mode[:-1]
    level = int(np.clip(level, 1, 8))
    mode = {"light": "lights", "highlights": "lights", "dark": "darks", "shadows": "darks", "mid": "midtones",
            "midtone": "midtones", "mids": "midtones"}.get(mode, mode)
    if mode == "lights":
        base = L
    elif mode == "darks":
        base = 1.0 - L
    elif mode == "midtones":
        base = 1.0 - np.abs(2.0 * L - 1.0)
    else:
        raise ValueError(f"luminosity mask: unknown mode '{mode}' (lights|darks|midtones)")
    return np.power(np.clip(base, 0.0, 1.0), level).astype(np.float32)


# --------------------------------------------------------------------------- colour range
def _parse_color(c) -> np.ndarray:
    from ..util import hex_to_rgb

    return np.asarray(hex_to_rgb(c), np.float32)


def sample_patch(img: np.ndarray, domain: str, x: float, y: float, radius_px: float = 3.0) -> np.ndarray:
    """Mean display colour (0..1) around a normalised point."""
    h, w = img.shape[:2]
    cx, cy = int(x * w), int(y * h)
    r = max(1, int(radius_px))
    patch = img[max(cy - r, 0):cy + r + 1, max(cx - r, 0):cx + r + 1].reshape(-1, 3)
    mean = patch.mean(0)
    if domain == "linear":
        return to_display(mean[None, None, :], "linear", gain=1.0)[0, 0]
    return np.clip(mean, 0, 1)


def color_range_mask(spec: dict, img: np.ndarray, ctx) -> np.ndarray:
    """Colour-range selection: colour(s) given as hex / [r,g,b] display values (`color` / `colors`) or picked
    from the image (`point: [x,y]` or `points`). Distance is Euclidean in Oklab x100 (`metric: oklab`) or CIEDE2000
    (`metric: de2000`); m = 1 inside `tolerance`, smoothstep to 0 over `falloff`. `lightness_weight` (default 1) scales
    the L difference (0 = hue/chroma only, like Lightroom's hue+sat weighting)."""
    domain = get_domain(ctx)
    cols: list[np.ndarray] = []
    for c in ([spec["color"]] if "color" in spec else []) + list(spec.get("colors", []) or []):
        cols.append(_parse_color(c))
    pts = list(spec.get("points", []) or [])
    if "point" in spec:
        pts.append(spec["point"])
    for p in pts:
        cols.append(sample_patch(img, domain, float(p[0]), float(p[1]), float(spec.get("sample_radius", 3))))
    if not cols:
        raise ValueError("color_range mask needs `color`, `colors` or `point`")
    tol = float(spec.get("tolerance", 10.0))
    fall = max(float(spec.get("falloff", 10.0)), 1e-3)
    wl = float(spec.get("lightness_weight", 1.0))
    disp = to_display(img, domain, gain=1.0) if domain == "linear" else np.clip(img, 0, 1)
    metric = spec.get("metric", "oklab")
    best = np.full(img.shape[:2], np.inf, np.float32)
    if metric == "de2000":
        import cv2

        h, w = disp.shape[:2]
        s = min(1.0, 1024.0 / max(h, w))
        small = cv2.resize(disp, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else disp
        lab = color.rgb_to_lab(small)
        for c in cols:
            ref = color.rgb_to_lab(c.reshape(1, 1, 3))[0, 0]
            d = color.delta_e2000(lab, np.broadcast_to(ref, lab.shape)).astype(np.float32)
            d = cv2.resize(d, (w, h), interpolation=cv2.INTER_LINEAR) if s < 1 else d
            best = np.minimum(best, d)
    else:
        lab = color.display_to_oklab(disp)
        for c in cols:
            ref = color.display_to_oklab(c.reshape(1, 1, 3))[0, 0]
            dl = (lab[..., 0] - ref[0]) * wl
            da, db = lab[..., 1] - ref[1], lab[..., 2] - ref[2]
            best = np.minimum(best, 100.0 * np.sqrt(dl * dl + da * da + db * db))
    return (1.0 - smoothstep(tol, tol + fall, best)).astype(np.float32)


def depth_range_mask(spec: dict, depth: np.ndarray) -> np.ndarray:
    """Select a depth band: depth is 0 (near) .. 1 (far). `near`/`far` give the band (default whole range),
    `falloff` the soft edge in depth units."""
    rng = spec.get("range")
    near = float(spec.get("near", rng[0] if rng else 0.0))
    far = float(spec.get("far", rng[1] if rng else 1.0))
    fall = max(float(spec.get("falloff", 0.08)), 1e-4)
    lo = smoothstep(near - fall, near, depth) if near > 0 else np.ones_like(depth)
    hi = smoothstep(far + fall, far, depth) if far < 1 else np.ones_like(depth)
    note_ = lo * hi
    return note_.astype(np.float32)
