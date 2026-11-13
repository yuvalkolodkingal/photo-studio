"""Blend modes: out = (1 - alpha) * base + alpha * mode(base, edited), alpha = clip(mask * opacity, 0, 1).

Display domain (bounded 0..1, a = base, b = edited): standard W3C / Photoshop separable formulas, plus the
non-separable channel modes done in Oklch (lightness / chroma / hue / colour).

Linear (scene-referred, unbounded) domain: only modes that are meaningful on unbounded data are evaluated;
multiply/divide use an explicit fulcrum (default 0.18, middle grey) so `multiply` darkens below the fulcrum and
brightens above it, add/subtract/difference/lighten/darken/average are unchanged, and modes that assume 0..1 data
(screen, overlay, soft/hard/vivid/linear/pin light, exclusion) fall back to `normal`. Channel modes use Oklch on
linear Rec.2020, which is unbounded-safe.
"""
from __future__ import annotations

import numpy as np

from .. import color

BLEND_MODES = {
    "normal": "replace (alpha-weighted 'over')",
    "multiply": "a*b (linear domain: a*b/fulcrum)",
    "screen": "1-(1-a)(1-b); display only",
    "overlay": "contrast, a decides; display only",
    "soft_light": "gentle contrast (W3C); display only",
    "hard_light": "contrast, b decides; display only",
    "vivid_light": "colour dodge/burn by b; display only",
    "linear_light": "a+2b-1; display only",
    "pin_light": "lighten/darken by 2b; display only",
    "exclusion": "a+b-2ab; display only",
    "darken": "min(a,b)",
    "lighten": "max(a,b)",
    "add": "a+b",
    "subtract": "max(a-b,0)",
    "difference": "|a-b|",
    "divide": "a/b (linear domain: a*fulcrum/b)",
    "average": "(a+b)/2",
    "geometric_mean": "sqrt(a*b)",
    "luminosity": "lightness of b, chroma+hue of a (Oklch); alias lightness",
    "chroma": "chroma of b, lightness+hue of a",
    "hue": "hue of b, lightness+chroma of a",
    "color": "hue+chroma of b, lightness of a",
}
_ALIASES = {"lightness": "luminosity", "softlight": "soft_light", "hardlight": "hard_light", "vividlight": "vivid_light",
            "linearlight": "linear_light", "pinlight": "pin_light", "colour": "color", "addition": "add",
            "linear_dodge": "add", "subtraction": "subtract", "chromaticity": "chroma", "saturation": "chroma",
            "mul": "multiply", "lum": "luminosity", "geomean": "geometric_mean"}
_BOUNDED_ONLY = {"screen", "overlay", "soft_light", "hard_light", "vivid_light", "linear_light", "pin_light", "exclusion"}
_NONSEP = {"luminosity", "chroma", "hue", "color"}


def canonical_mode(mode: str) -> str:
    m = str(mode or "normal").lower().strip().replace(" ", "_").replace("-", "_")
    m = _ALIASES.get(m, m)
    if m not in BLEND_MODES:
        raise ValueError(f"unknown blend mode '{mode}'. Known: {', '.join(BLEND_MODES)}")
    return m


def effective_mode(mode: str, domain: str) -> str:
    """The mode that will actually run (linear domain falls back to normal for bounded-only modes)."""
    m = canonical_mode(mode)
    return "normal" if (domain == "linear" and m in _BOUNDED_ONLY) else m


def _soft_light(a, b):
    d = np.where(a <= 0.25, ((16 * a - 12) * a + 4) * a, np.sqrt(a))
    return np.where(b <= 0.5, a - (1 - 2 * b) * a * (1 - a), a + (2 * b - 1) * (d - a))


def _overlay(a, b):
    return np.where(a < 0.5, 2 * a * b, 1 - 2 * (1 - a) * (1 - b))


def _nonseparable(a, b, mode, domain):
    if domain == "linear":
        la = color.to_oklch(np.maximum(a, 0), "linear")
        lb = color.to_oklch(np.maximum(b, 0), "linear")
    else:
        la = color.to_oklch(np.clip(a, 0, 1), "display")
        lb = color.to_oklch(np.clip(b, 0, 1), "display")
    out = la.copy()
    if mode == "luminosity":
        out[..., 0] = lb[..., 0]
    elif mode == "chroma":
        out[..., 1] = lb[..., 1]
    elif mode == "hue":
        out[..., 2] = lb[..., 2]
    else:   # color
        out[..., 1] = lb[..., 1]
        out[..., 2] = lb[..., 2]
    res = color.from_oklch(out, domain)
    return np.maximum(res, 0) if domain == "linear" else np.clip(res, 0, 1)


def blend_pixels(a: np.ndarray, b: np.ndarray, mode: str, domain: str = "linear", fulcrum: float = 0.18) -> np.ndarray:
    """mode(a=base, b=edited) with no mask/opacity."""
    mode = effective_mode(mode, domain)
    if mode == "normal":
        return b
    if mode in _NONSEP:
        return _nonseparable(a, b, mode, domain)
    lin = domain == "linear"
    if not lin:
        a = np.clip(a, 0.0, 1.0)
        b = np.clip(b, 0.0, 1.0)
    if mode == "multiply":
        return a * b / fulcrum if lin else a * b
    if mode == "divide":
        return a * fulcrum / np.maximum(b, 1e-6) if lin else np.clip(a / np.maximum(b, 1e-6), 0, 1)
    if mode == "add":
        return a + b if lin else np.clip(a + b, 0, 1)
    if mode == "subtract":
        return np.maximum(a - b, 0.0)
    if mode == "difference":
        return np.abs(a - b)
    if mode == "darken":
        return np.minimum(a, b)
    if mode == "lighten":
        return np.maximum(a, b)
    if mode == "average":
        return 0.5 * (a + b)
    if mode == "geometric_mean":
        return np.sqrt(np.maximum(a, 0) * np.maximum(b, 0))
    # bounded-only (display) modes below
    if mode == "screen":
        return 1 - (1 - a) * (1 - b)
    if mode == "overlay":
        return _overlay(a, b)
    if mode == "hard_light":
        return _overlay(b, a)
    if mode == "soft_light":
        return _soft_light(a, b)
    if mode == "exclusion":
        return a + b - 2 * a * b
    if mode == "linear_light":
        return np.clip(a + 2 * b - 1, 0, 1)
    if mode == "vivid_light":
        burn = 1 - (1 - a) / np.maximum(2 * b, 1e-6)
        dodge = a / np.maximum(2 * (1 - b), 1e-6)
        return np.clip(np.where(b < 0.5, burn, dodge), 0, 1)
    if mode == "pin_light":
        return np.where(b < 0.5, np.minimum(a, 2 * b), np.maximum(a, 2 * b - 1))
    raise ValueError(mode)


def blend(base: np.ndarray, edited: np.ndarray, mask: np.ndarray | None = None, mode: str = "normal",
          opacity: float | np.ndarray = 1.0, domain: str = "linear", fulcrum: float = 0.18) -> np.ndarray:
    """Composite `edited` over `base` through `mask` (HxW 0..1, None = everywhere).
    opacity 0 or mask 0 returns `base` exactly; mode normal with alpha 1 returns `edited` exactly."""
    base = np.asarray(base, np.float32)
    edited = np.asarray(edited, np.float32)
    squeeze = base.ndim == 2
    if squeeze:
        base, edited = base[..., None], edited[..., None]
    if mask is None:
        alpha = np.ones(base.shape[:2], np.float32)
    else:
        alpha = np.asarray(mask, np.float32)
        if alpha.ndim == 3:
            alpha = alpha[..., 0]
    alpha = np.clip(alpha * np.asarray(opacity, np.float32), 0.0, 1.0)[..., None]
    if not np.any(alpha > 0):
        out = base.copy()
    else:
        mode_c = canonical_mode(mode)
        if mode_c == "normal" or edited.shape[-1] != 3:
            blended = edited
        else:
            blended = blend_pixels(base, edited, mode_c, domain, fulcrum)
        out = (base * (1.0 - alpha) + blended * alpha).astype(np.float32)
    return out[..., 0] if squeeze else out
