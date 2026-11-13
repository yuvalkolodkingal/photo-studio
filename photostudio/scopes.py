"""Scopes: histogram data and renders (histogram, waveform, RGB parade, vectorscope with skin-tone line, false colour,
clipping overlay). All computed on a <= 1 MP display-referred copy with numpy bincount, so they are fast (< ~20 ms).

Inputs are float32 HxWx3 images. By default they are display-referred (0..1 sRGB-encoded, `domain="display"`); pass
`domain="linear"` for scene-linear Rec.2020 data and the scope encodes it first (sRGB OETF after Rec.2020 -> sRGB).
Every render_* function returns uint8 HxWx3 (RGB). `png_*` aliases exist for the contract names.
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from . import color

BG = np.array([18, 18, 22], np.uint8)
GRID = np.array([60, 60, 68], np.uint8)
CH_COLORS = np.array([[230, 70, 60], [70, 210, 90], [70, 120, 240]], np.float32)

# false-colour bands on display luminance (% IRE): (lo, hi, rgb) - configurable
FALSE_COLOR_BANDS = [
    (0.0, 2.0, (110, 0, 160)),      # crushed black
    (2.0, 10.0, (30, 60, 200)),     # deep shadows
    (10.0, 25.0, (20, 140, 200)),   # shadows
    (25.0, 36.0, (40, 170, 110)),   # dark midtones
    (36.0, 48.0, (110, 190, 60)),   # mid grey / darker skin
    (48.0, 52.0, (128, 128, 128)),
    (52.0, 62.0, (240, 150, 190)),  # lighter skin
    (62.0, 80.0, (225, 210, 120)),  # light midtones
    (80.0, 90.0, (240, 190, 60)),   # highlights
    (90.0, 97.0, (250, 230, 40)),   # near clipping
    (97.0, 100.01, (255, 30, 30)),  # clipped
]


# --------------------------------------------------------------------------- preparation
def to_display_small(img: np.ndarray, domain: str = "display", max_px: int = 1_000_000) -> np.ndarray:
    a = np.asarray(img, np.float32)
    if a.ndim == 2:
        a = np.repeat(a[..., None], 3, 2)
    h, w = a.shape[:2]
    if h * w > max_px:
        s = (max_px / (h * w)) ** 0.5
        a = cv2.resize(a, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    if domain == "linear":
        a = color.srgb_oetf(np.clip(color.apply_matrix(a, color.REC2020_TO_SRGB), 0, None).clip(0, 1))
    return np.clip(a, 0.0, 1.0)


def _luma(d: np.ndarray) -> np.ndarray:
    return color.luma(d)


def _q(x: np.ndarray, n: int) -> np.ndarray:
    return np.clip((x * (n - 1) + 0.5).astype(np.int32), 0, n - 1)


# --------------------------------------------------------------------------- data
def histogram(img: np.ndarray, domain: str = "display", bins: int = 256) -> dict:
    """Per-channel and luma histograms (counts), clipping percentages and basic statistics.
    Returns {bins, r, g, b, luma (int arrays), clip_high_pct / clip_low_pct (per channel + 'any' + 'all'), mean, median,
    p1, p99, ev: {...}}. JSON-friendly via photostudio.util.to_jsonable."""
    d = to_display_small(img, domain)
    lum = _luma(d)
    out: dict = {"bins": bins}
    for i, n in enumerate("rgb"):
        out[n] = np.bincount(_q(d[..., i].ravel(), bins), minlength=bins)
    out["luma"] = np.bincount(_q(lum.ravel(), bins), minlength=bins)
    hi_t, lo_t = 254.5 / 255.0, 0.5 / 255.0
    hi = d >= hi_t
    lo = d <= lo_t
    n = d.shape[0] * d.shape[1]
    out["clip_high_pct"] = {"r": 100.0 * float(hi[..., 0].mean()), "g": 100.0 * float(hi[..., 1].mean()),
                            "b": 100.0 * float(hi[..., 2].mean()), "any": 100.0 * float(hi.any(2).mean()),
                            "all": 100.0 * float(hi.all(2).mean())}
    out["clip_low_pct"] = {"r": 100.0 * float(lo[..., 0].mean()), "g": 100.0 * float(lo[..., 1].mean()),
                           "b": 100.0 * float(lo[..., 2].mean()), "any": 100.0 * float(lo.any(2).mean()),
                           "all": 100.0 * float(lo.all(2).mean())}
    out["mean"] = float(lum.mean())
    out["median"] = float(np.median(lum))
    out["p1"], out["p99"] = [float(v) for v in np.percentile(lum, (1, 99))]
    out["pixels"] = int(n)
    out["ev"] = ev_histogram(img, domain)
    return out


def ev_histogram(img: np.ndarray, domain: str = "display", lo: float = -10.0, hi: float = 2.0, bins: int = 48) -> dict:
    """Linear-light EV histogram of luminance relative to 0.18 (the tone equalizer's view)."""
    a = np.asarray(img, np.float32)
    if a.shape[0] * a.shape[1] > 300_000:
        sc = (300_000 / (a.shape[0] * a.shape[1])) ** 0.5
        a = cv2.resize(a, (max(1, int(a.shape[1] * sc)), max(1, int(a.shape[0] * sc))), interpolation=cv2.INTER_AREA)
    y = color.luminance(color.srgb_eotf(np.clip(a, 0, 1)), "srgb") if domain == "display" else color.luminance(a, "rec2020")
    ev = np.log2(np.maximum(y, 1e-6) / color.MIDDLE_GREY)
    counts, edges = np.histogram(np.clip(ev, lo, hi), bins=bins, range=(lo, hi))
    return {"counts": counts, "edges": edges, "median_ev": float(np.median(ev)), "p1_ev": float(np.percentile(ev, 1)),
            "p99_ev": float(np.percentile(ev, 99))}


# --------------------------------------------------------------------------- renders
def _canvas(h: int, w: int) -> np.ndarray:
    return np.broadcast_to(BG, (h, w, 3)).copy()


def render_histogram(img: np.ndarray, domain: str = "display", size: tuple[int, int] = (512, 256), log: bool = False,
                     channels: str = "rgbl") -> np.ndarray:
    """Overlaid R/G/B (additive) + luma (grey) histogram with quarter-grid lines. size = (width, height)."""
    w, h = size
    hist = histogram(img, domain, 256)
    canvas = np.zeros((h, w, 3), np.float32)
    for name, ci in ((("r", 0), ("g", 1), ("b", 2)) if "rgb" in channels or "r" in channels else ()):
        layers_ = hist[name].astype(np.float64)
        layers_ = np.log1p(layers_) if log else layers_
        top = np.percentile(layers_[1:-1], 99.5) if layers_[1:-1].max() > 0 else max(layers_.max(), 1)
        v = np.clip(layers_ / max(top, 1e-9), 0, 1)
        x = np.linspace(0, 255, w)
        prof = np.interp(x, np.arange(256), v)
        rows = (prof * (h - 2)).astype(int)
        mask = np.arange(h)[::-1, None] < rows[None, :]
        canvas += mask[..., None] * (CH_COLORS[ci] * 0.55)
    if "l" in channels:
        l_ = hist["luma"].astype(np.float64)
        l_ = np.log1p(l_) if log else l_
        top = np.percentile(l_[1:-1], 99.5) if l_[1:-1].max() > 0 else max(l_.max(), 1)
        prof = np.interp(np.linspace(0, 255, w), np.arange(256), np.clip(l_ / max(top, 1e-9), 0, 1))
        rows = (prof * (h - 2)).astype(int)
        edge = (np.arange(h)[::-1, None] == np.clip(rows[None, :], 0, h - 1))
        canvas += edge[..., None] * 160.0
    out = np.clip(canvas, 0, 255).astype(np.uint8)
    base = _canvas(h, w)
    out = np.where(out.sum(2, keepdims=True) > 0, out, base)
    for q in (1, 2, 3):
        out[:, int(w * q / 4)] = np.maximum(out[:, int(w * q / 4)], GRID)
    hc = hist["clip_high_pct"]["any"]
    lc = hist["clip_low_pct"]["any"]
    if hc > 0.01:
        out[:6, -40:] = (255, 60, 60)
    if lc > 0.01:
        out[:6, :40] = (60, 90, 255)
    return out


def render_waveform(img: np.ndarray, domain: str = "display", size: tuple[int, int] = (512, 256), mode: str = "luma") -> np.ndarray:
    """Waveform: x = image column, y = level (0 bottom .. 1 top), intensity = log-scaled pixel count.
    mode: luma | rgb (overlay) | parade (see render_parade)."""
    if mode == "parade":
        return render_parade(img, domain, size)
    w, h = size
    d = to_display_small(img, domain, 600_000)
    H, W = d.shape[:2]
    cols = np.minimum((np.arange(W) * w // W), w - 1)
    colidx = np.broadcast_to(cols[None, :], (H, W)).ravel()
    if mode == "rgb":
        acc = np.zeros((h, w, 3), np.float32)
        for i in range(3):
            lv = _q(d[..., i].ravel(), h)
            wf = np.bincount(lv * w + colidx, minlength=h * w).reshape(h, w)[::-1]
            acc[..., i] = _norm_counts(wf)
        img_out = np.clip(acc * 255, 0, 255).astype(np.uint8)
        return _grid_overlay(np.maximum(img_out, _canvas(h, w) * (img_out.max(2, keepdims=True) == 0)), h)
    lv = _q(_luma(d).ravel(), h)
    wf = np.bincount(lv * w + colidx, minlength=h * w).reshape(h, w)[::-1]
    g = _norm_counts(wf)
    out = np.stack([g * 0.35, g * 1.0, g * 0.45], -1) * 255
    out = np.maximum(out, _canvas(h, w) * (g[..., None] == 0)).clip(0, 255).astype(np.uint8)
    return _grid_overlay(out, h)


def render_parade(img: np.ndarray, domain: str = "display", size: tuple[int, int] = (768, 256)) -> np.ndarray:
    """RGB parade: three side-by-side waveforms (R, G, B)."""
    w, h = size
    d = to_display_small(img, domain, 600_000)
    H, W = d.shape[:2]
    pw = w // 3
    cols = np.minimum((np.arange(W) * pw // W), pw - 1)
    colidx = np.broadcast_to(cols[None, :], (H, W)).ravel()
    out = _canvas(h, pw * 3)
    for i in range(3):
        lv = _q(d[..., i].ravel(), h)
        wf = np.bincount(lv * pw + colidx, minlength=h * pw).reshape(h, pw)[::-1]
        g = _norm_counts(wf)
        tile = g[..., None] * (CH_COLORS[i] * 0.85 + 38.0)
        seg = out[:, i * pw:(i + 1) * pw].astype(np.float32)
        out[:, i * pw:(i + 1) * pw] = np.maximum(seg, tile).clip(0, 255).astype(np.uint8)
    return _grid_overlay(out, h)


def _norm_counts(c: np.ndarray) -> np.ndarray:
    c = c.astype(np.float32)
    mx = np.percentile(c[c > 0], 99.5) if (c > 0).any() else 1.0
    return np.clip(np.log1p(c) / max(np.log1p(mx), 1e-6), 0, 1)


def _grid_overlay(out: np.ndarray, h: int) -> np.ndarray:
    for q in range(1, 4):
        y = int(h * q / 4)
        out[y] = np.maximum(out[y], GRID)
    return out


def _uv(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Rec.601-style U,V chroma of display RGB (vectorscope plane)."""
    Y = 0.299 * d[..., 0] + 0.587 * d[..., 1] + 0.114 * d[..., 2]
    return 0.492 * (d[..., 2] - Y), 0.877 * (d[..., 0] - Y)


def render_vectorscope(img: np.ndarray, domain: str = "display", size: int = 384, skin_line: bool = True) -> np.ndarray:
    """U/V vectorscope: 2-D histogram (bincount) of chroma, log-scaled, with the graticule circle, primary/secondary
    targets (R, Mg, B, Cy, G, Yl) and the skin-tone line at 123 deg."""
    S = int(size)
    d = to_display_small(img, domain, 500_000)
    u, v = _uv(d)
    scale = (S / 2) * 0.88 / 0.62
    ix = np.clip((S / 2 + u * scale).astype(np.int32), 0, S - 1).ravel()
    iy = np.clip((S / 2 - v * scale).astype(np.int32), 0, S - 1).ravel()
    h2 = np.bincount(iy * S + ix, minlength=S * S).reshape(S, S)
    g = _norm_counts(h2)
    g = cv2.GaussianBlur(g, (0, 0), 0.8)
    g = np.clip(g * 1.4, 0, 1)
    out = (g[..., None] * np.array([120, 255, 150], np.float32)).astype(np.float32)
    canvas = _canvas(S, S).astype(np.float32)
    out = np.maximum(out, canvas * (g[..., None] < 0.02)).clip(0, 255).astype(np.uint8)
    c = (S // 2, S // 2)
    for r in (0.25, 0.5, 0.75, 1.0):
        cv2.circle(out, c, int(0.62 * scale * r * 0.9), tuple(int(x) for x in GRID), 1, cv2.LINE_AA)
    cv2.line(out, (0, c[1]), (S - 1, c[1]), tuple(int(x) for x in GRID), 1)
    cv2.line(out, (c[0], 0), (c[0], S - 1), tuple(int(x) for x in GRID), 1)
    for name, rgb in (("R", (1, 0, 0)), ("Mg", (1, 0, 1)), ("B", (0, 0, 1)), ("Cy", (0, 1, 1)), ("G", (0, 1, 0)), ("Yl", (1, 1, 0))):
        uu, vv = _uv(np.array(rgb, np.float32)[None, None, :])
        x, y = int(S / 2 + float(uu.ravel()[0]) * scale * 0.75), int(S / 2 - float(vv.ravel()[0]) * scale * 0.75)
        cv2.rectangle(out, (x - 5, y - 5), (x + 5, y + 5), tuple(int(255 * q) for q in rgb), 1)
    if skin_line:
        a = np.radians(123.0)
        p2 = (int(c[0] + math.cos(a) * S * 0.46), int(c[1] - math.sin(a) * S * 0.46))
        cv2.line(out, c, p2, (255, 190, 90), 1, cv2.LINE_AA)
    return out


def false_color(img: np.ndarray, domain: str = "display", mode: str = "ire", bands=None, legend: bool = True) -> np.ndarray:
    """False-colour exposure map. mode 'ire': display luminance bands (FALSE_COLOR_BANDS, % IRE); mode 'ev': 1-EV zones of
    linear luminance relative to middle grey (-6..+3 EV), blue = dark, red = bright."""
    a = np.asarray(img, np.float32)
    d = np.clip(a, 0, 1) if domain == "display" else to_display_small(a, "linear", max_px=a.shape[0] * a.shape[1] + 1)
    lum = _luma(d)
    out = np.zeros(d.shape, np.uint8)
    if mode == "ev":
        y = color.luminance(color.srgb_eotf(d), "srgb")
        ev = np.log2(np.maximum(y, 1e-6) / color.MIDDLE_GREY)
        zones = np.clip(np.floor(ev + 6.5).astype(int), 0, 9)
        pal = np.array([cv2.applyColorMap(np.array([[int(z * 255 / 9)]], np.uint8), cv2.COLORMAP_TURBO)[0, 0, ::-1] for z in range(10)])
        out = pal[zones]
    else:
        ire = lum * 100.0
        out[:] = (60, 60, 60)
        for lo, hi, rgb in (bands or FALSE_COLOR_BANDS):
            out[(ire >= lo) & (ire < hi)] = rgb
    if legend:
        h = out.shape[0]
        bh = max(6, h // 40)
        n = len(bands or FALSE_COLOR_BANDS) if mode != "ev" else 10
        w = out.shape[1]
        for i in range(n):
            col = (bands or FALSE_COLOR_BANDS)[i][2] if mode != "ev" else tuple(int(v) for v in pal[i])
            out[h - bh:h, i * w // n:(i + 1) * w // n] = col
    return out


def clipping_overlay(img: np.ndarray, domain: str = "display", hi: float = 254.5 / 255.0, lo: float = 0.5 / 255.0,
                     strength: float = 0.85) -> np.ndarray:
    """Greyscale image dimmed, highlights in red (all channels clipped = solid red, partial = orange), shadows in blue
    (all channels crushed = solid blue, partial = cyan). Full resolution of the input."""
    a = np.asarray(img, np.float32)
    d = np.clip(a, 0, 1) if domain == "display" else to_display_small(a, "linear", max_px=a.shape[0] * a.shape[1] + 1)
    base = np.repeat(_luma(d)[..., None], 3, 2) * 0.55
    out = base.copy()
    h_all, h_any = (d >= hi).all(2), (d >= hi).any(2)
    l_all, l_any = (d <= lo).all(2), (d <= lo).any(2)
    col = lambda m, c: np.where(m[..., None], base * (1 - strength) + np.array(c, np.float32) * strength, out)  # noqa: E731
    out = col(h_any, (1.0, 0.55, 0.1))
    out = col(h_all, (1.0, 0.0, 0.0))
    out = col(l_any & ~h_any, (0.1, 0.75, 0.95))
    out = col(l_all, (0.1, 0.2, 1.0))
    return (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)


# contract aliases
png_histogram = render_histogram
png_waveform = render_waveform
png_parade = render_parade
png_vectorscope = render_vectorscope
png_false_color = false_color
png_clipping = clipping_overlay


def scope_pack(img: np.ndarray, out_dir: str | Path, domain: str = "display", prefix: str = "") -> dict:
    """Write histogram / waveform / parade / vectorscope / false colour / clipping PNGs into out_dir; returns paths + stats."""
    from PIL import Image

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, fn in (("histogram", render_histogram), ("waveform", render_waveform), ("parade", render_parade),
                     ("vectorscope", render_vectorscope), ("false_color", false_color), ("clipping", clipping_overlay)):
        p = out_dir / f"{prefix}{name}.png"
        Image.fromarray(fn(img, domain=domain)).save(p)
        files[name] = str(p)
    hist = histogram(img, domain)
    return {"files": files, "clip_high_pct": hist["clip_high_pct"], "clip_low_pct": hist["clip_low_pct"],
            "median_luma": hist["median"], "p1": hist["p1"], "p99": hist["p99"], "median_ev": hist["ev"]["median_ev"]}
