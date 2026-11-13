"""Mask dispatcher: spec -> float32 HxW in 0..1.

Spec (recursive dict): {type, ...params, feather: px@2000, invert, opacity, refine: {guided_radius, guided_eps,
blur, contrast}}. Order of the common steps: raw mask -> feather (Gaussian for non-geometric masks; intrinsic for
drawn shapes) -> refine (guided filter against the image, Gaussian blur, contrast) -> invert -> opacity.
Coordinates are normalised 0..1 of the (post-geometry) image; pixel sizes are px at a 2000 px long edge.
"""
from __future__ import annotations

import cv2
import numpy as np

from ..filters import fast_guided
from . import ai, drawn, parametric
from .common import MASK_TYPES, get_domain, normalise_spec, px_scale, to_display

INTRINSIC_FEATHER = {"linear", "radial", "polygon", "brush"}


# --------------------------------------------------------------------------- combine
def combine_masks(masks: list[np.ndarray], mode: str = "union", soft: bool = False) -> np.ndarray:
    """union = max (or a+b-ab when soft), intersect = min (or a*b), subtract = a*(1-b)(1-c).. , exclude = XOR."""
    if not masks:
        raise ValueError("combine needs at least one mask")
    mode = {"add": "union", "or": "union", "and": "intersect", "intersection": "intersect", "difference": "subtract",
            "minus": "subtract", "xor": "exclude", "exclusive": "exclude"}.get(mode, mode)
    out = masks[0].astype(np.float32).copy()
    for m in masks[1:]:
        if mode == "union":
            out = out + m - out * m if soft else np.maximum(out, m)
        elif mode == "intersect":
            out = out * m if soft else np.minimum(out, m)
        elif mode == "subtract":
            out = out * (1.0 - m)
        elif mode == "exclude":
            out = out + m - 2.0 * out * m if soft else np.maximum(out, m) - np.minimum(out, m)
        else:
            raise ValueError(f"unknown combine mode '{mode}' (union|intersect|subtract|exclude)")
    return np.clip(out, 0.0, 1.0).astype(np.float32)


# --------------------------------------------------------------------------- refine
def _soften_curve(m: np.ndarray) -> np.ndarray:
    """Inverse of smoothstep: widens transitions while keeping 0 and 1 fixed."""
    return 0.5 - np.sin(np.arcsin(np.clip(1.0 - 2.0 * m, -1.0, 1.0)) / 3.0)


def refine_mask(m: np.ndarray, spec: dict | None, img: np.ndarray, ctx=None) -> np.ndarray:
    """Edge refinement. guided_radius (px@2000, 0 = off) + guided_eps snap mask edges to image edges (guided filter with
    the image as guide); blur = Gaussian sigma px@2000 applied after; contrast -1..1 steepens (>0) or widens (<0)
    the transition around 0.5 (0 and 1 stay put)."""
    if not spec:
        return m
    out = m.astype(np.float32)
    gr = float(spec.get("guided_radius", spec.get("feather", 0.0)) or 0.0)
    if gr > 0:
        r = max(1, int(round(px_scale(ctx, img, gr))))
        eps = float(spec.get("guided_eps", 1e-3))
        guide = to_display(img, get_domain(ctx)).astype(np.float32)
        out = np.clip(fast_guided(guide, out, r, eps), 0.0, 1.0)
    bl = float(spec.get("blur", 0.0) or 0.0)
    if bl > 0:
        out = cv2.GaussianBlur(out, (0, 0), max(px_scale(ctx, img, bl), 0.3), borderType=cv2.BORDER_REPLICATE)
    c = float(spec.get("contrast", 0.0) or 0.0)
    if c > 0:
        out = np.clip((out - 0.5) * (1.0 + 6.0 * c) + 0.5, 0.0, 1.0)
    elif c < 0:
        out = (1.0 + c) * out + (-c) * _soften_curve(out)
    if "threshold" in spec and spec["threshold"] is not None:
        t = float(spec["threshold"])
        soft = max(float(spec.get("threshold_softness", 0.05)), 1e-3)
        out = np.clip((out - t) / soft + 0.5, 0.0, 1.0)
    return np.clip(out, 0.0, 1.0).astype(np.float32)


# --------------------------------------------------------------------------- dispatch
def _raw_mask(t: str, spec: dict, img: np.ndarray, ctx) -> np.ndarray:
    h, w = img.shape[:2]
    if t == "all":
        return np.ones((h, w), np.float32)
    if t == "none":
        return np.zeros((h, w), np.float32)
    if t == "linear":
        return drawn.linear_mask(spec, h, w, ctx, img)
    if t == "radial":
        return drawn.radial_mask(spec, h, w, ctx, img)
    if t == "polygon":
        return drawn.polygon_mask(spec, h, w, ctx, img)
    if t == "brush":
        return drawn.brush_mask(spec, h, w, ctx, img)
    if t == "parametric":
        return parametric.parametric_mask(spec, img, ctx)
    if t == "luminosity":
        return parametric.luminosity_mask(spec, img, ctx)
    if t == "color_range":
        return parametric.color_range_mask(spec, img, ctx)
    if t == "ai_subject":
        return ai.subject_mask(img, ctx, spec)
    if t == "ai_background":
        return 1.0 - ai.subject_mask(img, ctx, spec)
    if t == "ai_sky":
        return ai.sky_mask(img, ctx, spec)
    if t in ("ai_face", "ai_eyes", "ai_lips", "ai_teeth", "ai_skin"):
        return ai.face_part_mask(img, ctx, t, spec)
    if t == "ai_animal":
        return ai.animal_mask(img, ctx, spec)
    if t == "depth":
        return parametric.depth_range_mask(spec, ai.depth_map(img, ctx, spec))
    if t == "combine":
        kids = spec.get("masks") or []
        built = [build_mask(k, img, ctx) for k in kids]
        return combine_masks(built, spec.get("mode", "union"), bool(spec.get("soft", False)))
    raise ValueError(f"unknown mask type '{t}'. Known: {', '.join(MASK_TYPES)}")


def build_mask(spec, img: np.ndarray, ctx=None) -> np.ndarray:
    """Build a float32 HxW mask in 0..1 for `spec` over `img` (float32 HxWx3 in ctx.domain: 'linear' scene data or
    'display' encoded data). `spec` may be None (all ones), a bare type name, or a spec dict (see module doc)."""
    h, w = img.shape[:2]
    s = normalise_spec(spec)
    if s is None:
        return np.ones((h, w), np.float32)
    t = s["type"]
    m = _raw_mask(t, s, img, ctx).astype(np.float32)
    f = float(s.get("feather", 0.0) or 0.0)
    if f > 0 and t not in INTRINSIC_FEATHER:
        sigma = px_scale(ctx, img, f) * 0.5
        if sigma > 0.3:
            m = cv2.GaussianBlur(m, (0, 0), sigma, borderType=cv2.BORDER_REPLICATE)
    if s.get("refine"):
        m = refine_mask(m, s["refine"], img, ctx)
    if s.get("invert"):
        m = 1.0 - m
    op = s.get("opacity", 1.0)
    if op is not None and float(op) != 1.0:
        m = m * float(op)
    return np.clip(m, 0.0, 1.0).astype(np.float32)


def mask_preview(spec, img: np.ndarray, ctx=None, color_rgb=(1.0, 0.1, 0.1), strength: float = 0.6) -> np.ndarray:
    """uint8 HxWx3: the display view of `img` with the mask as a red overlay (solid red = 100 %), plus a thin
    contour line at 0.5 so thin selections stay visible."""
    m = build_mask(spec, img, ctx)
    disp = to_display(img, get_domain(ctx))
    grey = disp.mean(axis=2, keepdims=True)
    base = 0.75 * disp + 0.25 * grey
    col = np.array(color_rgb, np.float32)
    out = base * (1.0 - strength * m[..., None]) + col * (strength * m[..., None])
    edge = cv2.morphologyEx((m > 0.5).astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
    out[edge] = out[edge] * 0.4 + np.array([1.0, 1.0, 0.2], np.float32) * 0.6
    return (np.clip(out, 0, 1) * 255.0 + 0.5).astype(np.uint8)
