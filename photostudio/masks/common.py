"""Shared mask helpers: spec normalisation, display views, coordinate grids, smoothing, notes."""
from __future__ import annotations

import numpy as np

from .. import color

TYPE_ALIASES = {
    "gradient": "linear", "graduated": "linear", "linear_gradient": "linear", "grad": "linear",
    "ellipse": "radial", "circle": "radial", "radial_gradient": "radial", "vignette": "radial",
    "path": "polygon", "bezier": "polygon", "lasso": "polygon", "poly": "polygon", "shape": "polygon",
    "paint": "brush", "drawn": "brush",
    "lum": "luminosity", "luminance": "luminosity", "luma": "luminosity", "lights": "luminosity",
    "darks": "luminosity", "midtones": "luminosity", "tonal": "luminosity",
    "colour_range": "color_range", "color": "color_range", "colour": "color_range", "hue": "color_range",
    "subject": "ai_subject", "background": "ai_background", "sky": "ai_sky", "face": "ai_face",
    "eyes": "ai_eyes", "lips": "ai_lips", "teeth": "ai_teeth", "skin": "ai_skin", "face_skin": "ai_skin",
    "animal": "ai_animal", "person": "ai_subject", "foreground": "ai_subject",
    "all": "all", "none": "none", "full": "all", "empty": "none",
    "union": "combine", "intersect": "combine", "subtract": "combine", "exclude": "combine",
}

MASK_TYPES = ["linear", "radial", "polygon", "brush", "parametric", "luminosity", "color_range", "ai_subject",
              "ai_background", "ai_sky", "ai_face", "ai_eyes", "ai_lips", "ai_teeth", "ai_skin", "ai_animal",
              "depth", "combine", "all", "none"]


def normalise_spec(spec) -> dict | None:
    """Accept None, a bare type string ('subject'), or a spec dict; return a canonical dict (copy)."""
    if spec is None:
        return None
    if isinstance(spec, str):
        spec = {"type": spec}
    s = dict(spec)
    t = str(s.get("type", "")).lower().strip()
    if t in ("union", "intersect", "subtract", "exclude") and "mode" not in s:
        s["mode"] = t
    if t in ("lights", "darks", "midtones") and "mode" not in s:
        s["mode"] = t
    s["type"] = TYPE_ALIASES.get(t, t)
    return s


def note(ctx, msg: str) -> None:
    if ctx is not None and hasattr(ctx, "note"):
        ctx.note(msg)


def get_domain(ctx, default: str = "display") -> str:
    return getattr(ctx, "domain", None) or default


def px_scale(ctx, img: np.ndarray, value: float) -> float:
    """Pixel size defined at 2000 px long edge -> pixels of this image."""
    if ctx is not None and hasattr(ctx, "px"):
        return float(ctx.px(value, img))
    return float(value) * max(img.shape[:2]) / 2000.0


def smoothstep(e0, e1, x):
    """Smoothstep that also works when e0 > e1 (falling edge)."""
    t = np.clip((x - e0) / (e1 - e0 if e1 != e0 else 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def pixel_grid(h: int, w: int):
    """Pixel-centre coordinates in pixels (x right, y down), float32 broadcastable arrays."""
    ys = (np.arange(h, dtype=np.float32) + 0.5)[:, None]
    xs = (np.arange(w, dtype=np.float32) + 0.5)[None, :]
    return xs, ys


def to_display(img: np.ndarray, domain: str, gain: float | None = None) -> np.ndarray:
    """A display-referred (0..1) view of image data for guides, models and overlays.
    Linear scene data is exposure-normalised (when gain is None: mean luminance -> ~0.2), soft-clipped
    and sRGB-encoded."""
    if domain == "display":
        return np.clip(img, 0.0, 1.0).astype(np.float32)
    x = np.maximum(img, 0.0)
    if gain is None:
        y = float(np.mean(color.luminance(x[::4, ::4])))
        gain = float(np.clip(0.2 / max(y, 1e-4), 0.5, 16.0))
    x = x * gain
    # soft shoulder above 0.8 so highlights stay distinguishable
    s = 0.8
    over = np.maximum(x - s, 0)
    x = np.where(x > s, s + (1 - s) * (1 - np.exp(-over / (1 - s))), x)
    return color.srgb_oetf(x).astype(np.float32)


def luma_display(img: np.ndarray, domain: str) -> np.ndarray:
    return color.luma(to_display(img, domain))


def linear_luminance(img: np.ndarray, domain: str) -> np.ndarray:
    """Relative scene luminance Y (linear light) irrespective of the domain."""
    if domain == "display":
        return color.luminance(color.srgb_eotf(np.clip(img, 0, 1)), "srgb")
    return color.luminance(img, "rec2020")


def resize_mask(m: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
    import cv2

    if m.shape[:2] == tuple(hw):
        return m
    interp = cv2.INTER_AREA if m.shape[0] > hw[0] else cv2.INTER_LINEAR
    return cv2.resize(m.astype(np.float32), (hw[1], hw[0]), interpolation=interp)


def box_of(mask: np.ndarray, thr: float = 1e-3):
    """Bounding box (y0, y1, x0, x1) of mask > thr, or None."""
    ys, xs = np.where(mask > thr)
    if ys.size == 0:
        return None
    return int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
