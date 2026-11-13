"""Drawn (geometric) masks: linear gradient, radial/ellipse, polygon/bezier path, brush strokes.

Coordinates are normalised 0..1 of the image (x/W, y/H). Sizes that are *pixels* (feather, brush radius) are
given at a 2000 px long edge and scaled with ctx.px(). Feather falls off *outside* the shape by default
(shape = 100% core, like darktable); `feather_mode: "center"|"in"` centres it on / moves it inside the edge.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from .common import pixel_grid, px_scale, smoothstep

_FEATHER_RANGE = {"out": (0.0, 1.0), "center": (-0.5, 0.5), "in": (-1.0, 0.0)}


def _pt(v, default=None):
    if v is None:
        return default
    if isinstance(v, dict):
        if "x" in v:
            return (float(v["x"]), float(v["y"]))
        if "p" in v:
            return _pt(v["p"])
    return (float(v[0]), float(v[1]))


def _first(spec: dict, *keys, default=None):
    for k in keys:
        if spec.get(k) is not None:
            return spec[k]
    return default


# --------------------------------------------------------------------------- linear gradient
def linear_mask(spec: dict, h: int, w: int, ctx=None, img=None) -> np.ndarray:
    """Two-point gradient: 1 at p0 falling to 0 at p1. `feather` (px@2000) softens the ramp further (Gaussian);
    when p0==p1 (or p1 missing) the gradient degenerates to a half-plane through p0 facing `angle` degrees
    (0 = +x) whose ramp is `feather` px wide. `curvature` (-1..1) bends the iso-lines parabolically;
    `curve: smooth|linear` picks the ramp shape."""
    ref = img if img is not None else np.zeros((h, w), np.float32)
    f_px = max(px_scale(ctx, ref, float(spec.get("feather", 0.0))), 0.0)
    pts = spec.get("points")
    p0 = _pt(_first(spec, "p0", "start", "from", "a", "point", "center"), None)
    p1 = _pt(_first(spec, "p1", "end", "to", "b"), None)
    if pts and len(pts) >= 2:
        p0, p1 = _pt(pts[0]), _pt(pts[1])
    if p0 is None:
        p0 = (0.5, 0.5)
    x0, y0 = p0[0] * w, p0[1] * h
    xs, ys = pixel_grid(h, w)
    if p1 is not None and math.hypot((p1[0] - p0[0]) * w, (p1[1] - p0[1]) * h) > 1.0:
        dx, dy = p1[0] * w - x0, p1[1] * h - y0
        L = math.hypot(dx, dy)
        nx, ny = dx / L, dy / L
        t = ((xs - x0) * nx + (ys - y0) * ny) / L
        curv = float(spec.get("curvature", 0.0))
        if curv:
            s = ((xs - x0) * -ny + (ys - y0) * nx) / (0.5 * math.hypot(w, h))
            t = t + curv * s * s
        sigma = f_px * 0.5
    else:
        ang = math.radians(float(spec.get("angle", 90.0)))
        nx, ny = math.cos(ang), math.sin(ang)
        L = max(f_px, 1.0)
        t = ((xs - x0) * nx + (ys - y0) * ny) / L + 0.5   # ramp centred on the line
        sigma = 0.0
    if spec.get("curve", "smooth") == "smooth":
        m = smoothstep(1.0, 0.0, t)
    else:
        m = np.clip(1.0 - t, 0.0, 1.0)
    m = m.astype(np.float32)
    if sigma > 0.3:
        m = cv2.GaussianBlur(m, (0, 0), sigma, borderType=cv2.BORDER_REPLICATE)
    return m


# --------------------------------------------------------------------------- radial / ellipse
def radial_mask(spec: dict, h: int, w: int, ctx=None, img=None) -> np.ndarray:
    """Ellipse: centre, radii=[rx, ry] (fractions of W and H; scalar `radius` = circle, fraction of the shorter
    side), rotation (deg), feather (px@2000, outward by default). inside = 1."""
    ref = img if img is not None else np.zeros((h, w), np.float32)
    c = _pt(_first(spec, "center", "centre", "c"), (0.5, 0.5))
    radii = _first(spec, "radii", "radius", "size", "r")
    if radii is None:
        radii = [0.25, 0.25]
    if np.isscalar(radii):
        a = b = float(radii) * min(w, h)
    else:
        a, b = float(radii[0]) * w, float(radii[1]) * h
    a, b = max(a, 0.5), max(b, 0.5)
    rot = math.radians(float(spec.get("rotation", spec.get("angle", 0.0))))
    f = max(px_scale(ctx, ref, float(spec.get("feather", 0.0))), 1.0)
    lo, hi = _FEATHER_RANGE.get(spec.get("feather_mode", "out"), (0.0, 1.0))
    xs, ys = pixel_grid(h, w)
    dx, dy = xs - c[0] * w, ys - c[1] * h
    cs, sn = math.cos(rot), math.sin(rot)
    u = dx * cs + dy * sn
    v = -dx * sn + dy * cs
    q = np.sqrt((u / a) ** 2 + (v / b) ** 2)
    grad = np.sqrt((u / (a * a)) ** 2 + (v / (b * b)) ** 2)
    sd = (q - 1.0) * np.maximum(q, 1e-6) / np.maximum(grad, 1e-9)   # first-order signed distance in px
    sd = np.where(q < 1e-6, -min(a, b), sd)
    return smoothstep(hi * f, lo * f, sd).astype(np.float32)


# --------------------------------------------------------------------------- polygon / bezier
def _cubic(p0, p1, p2, p3, n):
    t = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)[:, None]
    mt = 1 - t
    return (mt ** 3) * p0 + 3 * (mt ** 2) * t * p1 + 3 * mt * (t ** 2) * p2 + (t ** 3) * p3


def path_points(spec: dict, w: int, h: int) -> tuple[np.ndarray, bool]:
    """Sample a polygon / bezier path to a dense polyline in pixels. Nodes: [x,y] | {x,y} | {p, in, out}.
    With `smooth: true` a Catmull-Rom spline through the nodes (converted to cubic beziers) is used."""
    raw = spec.get("points") or spec.get("nodes") or []
    if len(raw) < 2:
        return np.zeros((0, 2), np.float32), False
    closed = bool(spec.get("closed", True))
    scale = np.array([w, h], np.float32)
    P = np.array([_pt(n) for n in raw], np.float32) * scale
    n = len(P)
    hin = [None] * n
    hout = [None] * n
    for i, node in enumerate(raw):
        if isinstance(node, dict):
            if node.get("out") is not None:
                hout[i] = np.array(_pt(node["out"]), np.float32) * scale
            if node.get("in") is not None:
                hin[i] = np.array(_pt(node["in"]), np.float32) * scale
    smooth = bool(spec.get("smooth", False))
    tension = float(spec.get("tension", 0.5))
    pts: list[np.ndarray] = []
    nseg = n if closed else n - 1
    for i in range(nseg):
        a, b = P[i], P[(i + 1) % n]
        if smooth:
            pa = P[(i - 1) % n] if (closed or i > 0) else a
            pb = P[(i + 2) % n] if (closed or i + 2 < n) else b
            # Catmull-Rom -> cubic bezier handles: c1 = a + (b - pa) * t/3, c2 = b - (pb - a) * t/3
            c1 = hout[i] if hout[i] is not None else a + (b - pa) * tension / 3.0
            c2 = hin[(i + 1) % n] if hin[(i + 1) % n] is not None else b - (pb - a) * tension / 3.0
        else:
            c1 = hout[i] if hout[i] is not None else a
            c2 = hin[(i + 1) % n] if hin[(i + 1) % n] is not None else b
        curved = smooth or hout[i] is not None or hin[(i + 1) % n] is not None
        if curved:
            length = float(np.linalg.norm(b - a))
            m = int(np.clip(length / 5.0, 6, 64))
            pts.append(_cubic(a, c1, c2, b, m))
        else:
            pts.append(a[None])
    if not closed:
        pts.append(P[-1][None])
    return np.concatenate(pts, 0).astype(np.float32), closed


def polygon_mask(spec: dict, h: int, w: int, ctx=None, img=None) -> np.ndarray:
    """Closed polygon / bezier path (see `path_points`). feather (px@2000) falls off outside the path;
    `closed: false` draws a stroke of `width` px instead."""
    ref = img if img is not None else np.zeros((h, w), np.float32)
    poly, closed = path_points(spec, w, h)
    if len(poly) < 2:
        return np.zeros((h, w), np.float32)
    f = max(px_scale(ctx, ref, float(spec.get("feather", 0.0))), 1.0)
    lo, hi = _FEATHER_RANGE.get(spec.get("feather_mode", "out"), (0.0, 1.0))
    pad = int(math.ceil(max(abs(lo), abs(hi)) * f)) + 3
    x0 = max(int(poly[:, 0].min()) - pad, 0)
    y0 = max(int(poly[:, 1].min()) - pad, 0)
    x1 = min(int(poly[:, 0].max()) + pad + 1, w)
    y1 = min(int(poly[:, 1].max()) + pad + 1, h)
    out = np.zeros((h, w), np.float32)
    if x1 <= x0 or y1 <= y0:
        return out
    sh = 4
    local = np.round((poly - np.array([x0, y0], np.float32)) * (1 << sh)).astype(np.int32)
    canvas = np.zeros((y1 - y0, x1 - x0), np.uint8)
    if closed:
        cv2.fillPoly(canvas, [local], 255, lineType=cv2.LINE_AA, shift=sh)
    else:
        wd = max(px_scale(ctx, ref, float(spec.get("width", 4.0))), 1.0)
        cv2.polylines(canvas, [local], False, 255, thickness=max(1, int(round(wd))), lineType=cv2.LINE_AA, shift=sh)
    inside = canvas >= 128
    d_out = cv2.distanceTransform((~inside).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    d_in = cv2.distanceTransform(inside.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    sd = np.where(inside, -(d_in - 0.5), d_out - 0.5).astype(np.float32)
    out[y0:y1, x0:x1] = smoothstep(hi * f, lo * f, sd)
    return out


# --------------------------------------------------------------------------- brush
def _stroke_points(stroke: dict, w: int, h: int, default_r: float, ctx, img) -> np.ndarray:
    """-> (n,3) array of x_px, y_px, radius_px for one stroke (points may carry their own radius)."""
    rows = []
    for p in stroke.get("points", []):
        if isinstance(p, dict):
            x, y = float(p["x"]), float(p["y"])
            r = p.get("radius", p.get("r", stroke.get("radius", default_r)))
        else:
            x, y = float(p[0]), float(p[1])
            r = p[2] if len(p) > 2 else stroke.get("radius", default_r)
        rows.append((x * w, y * h, px_scale(ctx, img, float(r))))
    return np.array(rows, np.float32).reshape(-1, 3)


def brush_stroke_alpha(stroke: dict, h: int, w: int, ctx=None, img=None, default_radius: float = 30.0) -> np.ndarray:
    """Soft alpha of ONE stroke (max-accumulated stamps; * flow). radius is px@2000, hardness 0..1."""
    ref = img if img is not None else np.zeros((h, w), np.float32)
    P = _stroke_points(stroke, w, h, default_radius, ctx, ref)
    out = np.zeros((h, w), np.float32)
    if len(P) == 0:
        return out
    hard = float(np.clip(stroke.get("hardness", 0.5), 0.0, 0.999))
    flow = float(np.clip(stroke.get("flow", stroke.get("opacity", 1.0)), 0.0, 1.0))
    # resample the polyline to stamps spaced <= 20% of the local radius
    stamps = [P[0]]
    for a, b in zip(P[:-1], P[1:]):
        seg = float(np.hypot(b[0] - a[0], b[1] - a[1]))
        r = max(0.5 * (a[2] + b[2]), 1.0)
        n = max(int(math.ceil(seg / max(r * 0.2, 0.75))), 1)
        for k in range(1, n + 1):
            stamps.append(a + (b - a) * (k / n))
    for x, y, r in stamps:
        r = max(float(r), 0.75)
        xa, xb = max(int(x - r - 1), 0), min(int(x + r + 2), w)
        ya, yb = max(int(y - r - 1), 0), min(int(y + r + 2), h)
        if xb <= xa or yb <= ya:
            continue
        gx = (np.arange(xa, xb, dtype=np.float32) + 0.5 - x)[None, :]
        gy = (np.arange(ya, yb, dtype=np.float32) + 0.5 - y)[:, None]
        d = np.sqrt(gx * gx + gy * gy)
        a_ = 1.0 - smoothstep(r * hard, r, d)
        np.maximum(out[ya:yb, xa:xb], a_, out=out[ya:yb, xa:xb])
    return out * flow


def brush_mask(spec: dict, h: int, w: int, ctx=None, img=None) -> np.ndarray:
    """Strokes composited in order: paint = over (m + s(1-m)), `erase: true` = m(1-s). Each stroke:
    {points: [[x,y(,r)],..], radius: px@2000, hardness, flow, erase}."""
    m = np.zeros((h, w), np.float32)
    dr = float(spec.get("radius", 30.0))
    for st in spec.get("strokes", []):
        if st.get("points") is None:
            continue
        s = brush_stroke_alpha({**{"hardness": spec.get("hardness", 0.5), "flow": spec.get("flow", 1.0)}, **st},
                               h, w, ctx, img, dr)
        if st.get("erase"):
            m = m * (1.0 - s)
        else:
            m = m + s * (1.0 - m)
    return m
