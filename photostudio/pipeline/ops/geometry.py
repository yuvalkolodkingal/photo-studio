"""Geometry ops: lens correction, crop, rotate, auto-level, flip, perspective, resize.

Registered ops (stage / domain)
    lens_correct   7 any      lensfunpy (bundled data/lensfun/mil-canon.xml): vignetting, distortion, TCA in one
                              remap; manual k1/k2/vignette fallback; graceful no-op + ctx.note when unavailable
    crop          11 any      normalised rect and/or aspect preset (1:1, 4:5, 3:2, 16:9, 9:16, golden, ...)
    rotate        11 any      arbitrary angle (CCW positive) with largest-rectangle auto-crop (or expand/keep)
    auto_level    11 any      Hough-line horizon / verticals detection then rotate (alias: straighten)
    flip          11 any      mirror / quarter turns
    perspective   11 any      keystone sliders, 4-point quad, or automatic vertical/horizontal correction from
                              vanishing points; largest-rectangle auto-crop
    resize        21 display  long edge / short edge / megapixels / scale, Lanczos or area

Public helpers
    parse_aspect(spec, portrait=False, force=None) -> float | None   '3:2', 'square', 1.5, 'golden' ...
    crop_rect_px(shape, rect, aspect, anchor) -> (x0, y0, x1, y1)
    max_rect_in_polygon(poly, aspect, bounds) -> (x0, y0, x1, y1)   largest axis-aligned rect of given aspect
    detect_tilt(img, mode='horizon', max_angle=10) -> angle (deg, CCW-positive correction) or None
    estimate_vertical_rotation(img, f_px) -> 3x3 rotation or None   (auto-verticals)
    rotation_homography(shape, f_px, pitch, yaw, roll) -> 3x3
    warp_image(img, H, size, ...)                          perspective warp with float-safe interpolation
    lens_modifier(meta, shape, ...)                        lensfun Modifier or (None, reason)
"""
from __future__ import annotations

import difflib
import re

import cv2
import numpy as np

from ... import color
from ...util import lazy_import, package_dir
from ..registry import Ctx, op

ASPECTS = {
    "1:1": 1.0, "square": 1.0, "5:4": 5 / 4, "4:5": 4 / 5, "4:3": 4 / 3, "3:4": 3 / 4, "3:2": 3 / 2, "2:3": 2 / 3,
    "7:5": 7 / 5, "5:7": 5 / 7, "16:9": 16 / 9, "9:16": 9 / 16, "21:9": 21 / 9, "2:1": 2.0, "1:2": 0.5,
    "golden": 1.618034, "a4": 2 ** 0.5, "a-series": 2 ** 0.5, "65:24": 65 / 24, "xpan": 65 / 24,
}


def parse_aspect(spec, portrait: bool = False, force: str | None = None):
    """Aspect (width/height) from a preset name, 'W:H', a number, or [w, h]. None / 'free' / 'original' -> None.
    As written ('9:16' is portrait, '3:2' landscape). portrait=True flips a landscape-written aspect to portrait
    (so '3:2' follows a portrait image); portrait-written aspects are kept. A trailing 'L' / 'P' ('4:5L') or
    force='landscape' / 'portrait' pins the orientation."""
    if spec is None:
        return None
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        return float(spec[0]) / float(spec[1])
    if isinstance(spec, (int, float)):
        return float(spec)
    s = str(spec).strip().lower()
    if s in ("", "free", "none", "original"):
        return None
    if len(s) > 1 and s[-1] in "lp" and s[:-1] in ASPECTS or re.fullmatch(r"[\d.]+\s*[:x/]\s*[\d.]+[lp]", s):
        force = {"l": "landscape", "p": "portrait"}[s[-1]]
        s = s[:-1]
    if s in ASPECTS:
        a = ASPECTS[s]
    else:
        m = re.fullmatch(r"([\d.]+)\s*[:x/]\s*([\d.]+)", s)
        if not m:
            raise ValueError(f"unknown aspect '{spec}'")
        a = float(m.group(1)) / float(m.group(2))
    if a == 1.0:
        return 1.0
    if force == "landscape":
        return a if a > 1 else 1.0 / a
    if force == "portrait":
        return 1.0 / a if a > 1 else a
    return 1.0 / a if (portrait and a > 1) else a


def _clip_nonneg(out: np.ndarray, ref: np.ndarray) -> np.ndarray:
    return np.maximum(out, 0.0) if ref.min() >= 0 else out


# --------------------------------------------------------------------------- polygon / rectangle search
def _inside_convex(pts: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """pts (...,2) inside convex polygon poly (N,2) (either orientation); tolerance 1e-6."""
    n = len(poly)
    area = 0.5 * np.sum(poly[:, 0] * np.roll(poly[:, 1], -1) - np.roll(poly[:, 0], -1) * poly[:, 1])
    sgn = 1.0 if area >= 0 else -1.0
    ok = np.ones(pts.shape[:-1], bool)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        cross = (b[0] - a[0]) * (pts[..., 1] - a[1]) - (b[1] - a[1]) * (pts[..., 0] - a[0])
        ok &= sgn * cross >= -1e-6
    return ok


def max_rect_in_polygon(poly, aspect: float, bounds=None, grid: int = 11, iters: int = 34):
    """Largest axis-aligned rectangle with width/height = aspect inside the convex polygon (and inside bounds
    (x0, y0, x1, y1) if given). Searches centres on a grid (two refinement passes). Returns (x0, y0, x1, y1)."""
    poly = np.asarray(poly, np.float64).reshape(-1, 2)
    bx0, by0, bx1, by1 = bounds if bounds is not None else (-1e18, -1e18, 1e18, 1e18)
    minx, maxx = max(poly[:, 0].min(), bx0), min(poly[:, 0].max(), bx1)
    miny, maxy = max(poly[:, 1].min(), by0), min(poly[:, 1].max(), by1)
    c0 = poly.mean(axis=0)
    span = np.array([maxx - minx, maxy - miny])
    best = (0.0, c0)
    cen = c0.copy()
    rng = span * 0.5
    for _ in range(3):
        gx = np.linspace(cen[0] - rng[0], cen[0] + rng[0], grid)
        gy = np.linspace(cen[1] - rng[1], cen[1] + rng[1], grid)
        C = np.stack(np.meshgrid(gx, gy), -1).reshape(-1, 2)
        C = np.vstack([C, c0[None]])
        C = C[_inside_convex(C, poly)]
        if len(C) == 0:
            break
        lo = np.zeros(len(C))
        hi = np.full(len(C), max(span[0], span[1] * aspect) + 1.0)
        for _i in range(iters):
            mid = 0.5 * (lo + hi)
            hw, hh = mid, mid / aspect
            ok = np.ones(len(C), bool)
            for sx in (-1, 1):
                for sy in (-1, 1):
                    p = np.stack([C[:, 0] + sx * hw, C[:, 1] + sy * hh], -1)
                    ok &= _inside_convex(p, poly)
                    ok &= (p[:, 0] >= bx0 - 1e-6) & (p[:, 0] <= bx1 + 1e-6) & (p[:, 1] >= by0 - 1e-6) & (p[:, 1] <= by1 + 1e-6)
            lo = np.where(ok, mid, lo)
            hi = np.where(ok, hi, mid)
        i = int(np.argmax(lo))
        if lo[i] > best[0]:
            best = (float(lo[i]), C[i])
        cen = best[1]
        rng = rng * 0.25
    hw = best[0]
    cx, cy = best[1]
    return (cx - hw, cy - hw / aspect, cx + hw, cy + hw / aspect)


MIN_CROP_SIDE = 16


def _crop_to_rect(img: np.ndarray, rect, min_side: int = MIN_CROP_SIDE) -> np.ndarray | None:
    """Crop to the pixel rectangle ``rect`` (x0, y0, x1, y1). Returns ``None`` when the rectangle is degenerate (a side
    shorter than ``min_side`` px, e.g. an extreme auto-correction left no usable area): callers then skip the crop."""
    h, w = img.shape[:2]
    x0 = int(np.ceil(max(rect[0], 0.0) - 1e-6))
    y0 = int(np.ceil(max(rect[1], 0.0) - 1e-6))
    x1 = int(np.floor(min(rect[2], w) + 1e-6))
    y1 = int(np.floor(min(rect[3], h) + 1e-6))
    if x1 - x0 < min_side or y1 - y0 < min_side:
        return None
    return np.ascontiguousarray(img[y0:y1, x0:x1])


# --------------------------------------------------------------------------- crop
def crop_rect_px(shape, rect=None, aspect=None, anchor=(0.5, 0.5)):
    """Pixel crop box (x0, y0, x1, y1) from a normalised rect [x, y, w, h] and/or an aspect (w/h)."""
    h, w = shape[:2]
    if rect is not None:
        x, y, rw, rh = (float(v) for v in rect)
        x0, y0, x1, y1 = x * w, y * h, (x + rw) * w, (y + rh) * h
    else:
        x0, y0, x1, y1 = 0.0, 0.0, float(w), float(h)
    if aspect:
        cw, ch = x1 - x0, y1 - y0
        if cw / ch > aspect:       # too wide: shrink width
            nw, nh = ch * aspect, ch
        else:
            nw, nh = cw, cw / aspect
        if rect is None:
            cx = anchor[0] * w
            cy = anchor[1] * h
        else:
            cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
        x0, x1 = cx - nw / 2, cx + nw / 2
        y0, y1 = cy - nh / 2, cy + nh / 2
        if x0 < 0:
            x0, x1 = 0.0, nw
        if x1 > w:
            x0, x1 = w - nw, float(w)
        if y0 < 0:
            y0, y1 = 0.0, nh
        if y1 > h:
            y0, y1 = h - nh, float(h)
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(float(w), x1), min(float(h), y1)
    return x0, y0, x1, y1


@op("crop", 11, "any", rect=None, aspect=None, anchor=[0.5, 0.5], orientation="auto", changes_geometry=True,
    choices={"orientation": ["auto", "landscape", "portrait"]}, category="geometry")
def crop(img, p, ctx: Ctx):
    """Crop to a normalised rect [x, y, w, h] and/or aspect preset (1:1, 4:5, 3:2, 16:9, 9:16, golden ...). With an
    aspect only, the largest such rectangle is taken around `anchor`. orientation=auto follows the image."""
    h, w = img.shape[:2]
    orient = p["orientation"]
    asp = parse_aspect(p.get("aspect"), portrait=(h > w), force=None if orient == "auto" else orient)
    if p.get("rect") is None and asp is None:
        return img
    x0, y0, x1, y1 = crop_rect_px(img.shape, p.get("rect"), asp, tuple(p["anchor"]))
    ix0, iy0 = int(round(x0)), int(round(y0))
    ix1, iy1 = int(round(x1)), int(round(y1))
    if asp:  # rounding can leave +-1 px aspect error; keep the box inside and as exact as possible
        wpx = ix1 - ix0
        hpx = max(1, int(round(wpx / asp)))
        if iy0 + hpx > h:
            hpx = h - iy0
            wpx = int(round(hpx * asp))
        ix1, iy1 = ix0 + wpx, iy0 + hpx
    ix1, iy1 = max(ix1, ix0 + 1), max(iy1, iy0 + 1)
    return np.ascontiguousarray(img[iy0:iy1, ix0:ix1])


# --------------------------------------------------------------------------- warp helpers
def warp_image(img, H, size, interp=cv2.INTER_CUBIC, border=cv2.BORDER_CONSTANT):
    """Perspective warp (H maps source -> output pixel coords) on float32 data."""
    out = cv2.warpPerspective(np.ascontiguousarray(img, np.float32), np.asarray(H, np.float64), size,
                              flags=interp, borderMode=border, borderValue=0)
    return _clip_nonneg(out, img).astype(np.float32)


def _quad(w, h, shrink_px: float = 1.0):
    q = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float64)
    c = q.mean(0)
    s = max(0.0, 1.0 - 2.0 * shrink_px / max(min(w, h), 1))
    return c + (q - c) * s


def _apply_h(H, pts):
    pts = np.asarray(pts, np.float64)
    ph = np.concatenate([pts, np.ones((len(pts), 1))], 1) @ np.asarray(H).T
    return ph[:, :2] / ph[:, 2:3]


# --------------------------------------------------------------------------- rotate
@op("rotate", 11, "any", angle=0.0, mode="crop", aspect="original", changes_geometry=True,
    choices={"mode": ["crop", "expand", "keep"]}, category="geometry")
def rotate(img, p, ctx: Ctx):
    """Rotate by `angle` degrees (counter-clockwise positive). mode=crop: largest rectangle of the original (or
    given) aspect inside the rotated image; expand: grow canvas (black corners); keep: same canvas."""
    return rotate_image(img, float(p["angle"]), p["mode"], p.get("aspect"))


def rotate_image(img, angle, mode="crop", aspect="original"):
    if abs(angle) < 1e-9:
        return img
    h, w = img.shape[:2]
    center = ((w - 1) / 2.0, (h - 1) / 2.0)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    quad = _quad(w, h, 0.0)
    if mode == "expand":
        rq = cv2.transform(quad.reshape(-1, 1, 2), M).reshape(-1, 2)
        minx, miny = rq.min(0)
        maxx, maxy = rq.max(0)
        nw, nh = int(np.ceil(maxx - minx)), int(np.ceil(maxy - miny))
        M = M.copy()
        M[0, 2] -= minx
        M[1, 2] -= miny
        size = (nw, nh)
    else:
        size = (w, h)
    out = cv2.warpAffine(np.ascontiguousarray(img, np.float32), M, size, flags=cv2.INTER_CUBIC,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    out = _clip_nonneg(out, img).astype(np.float32)
    if mode != "crop":
        return out
    q = cv2.transform(_quad(w, h, 3.0).reshape(-1, 1, 2), M).reshape(-1, 2)
    asp = parse_aspect(aspect, portrait=h > w) if aspect not in (None, "original") else None
    asp = asp if asp else w / h
    rect = max_rect_in_polygon(q, asp, (0, 0, w, h))
    cropped = _crop_to_rect(out, rect)
    return out if cropped is None else cropped


# --------------------------------------------------------------------------- line detection
def _line_segments(img: np.ndarray, work_px: int = 1200):
    """Detect straight segments on a gamma-encoded grey copy. Returns (segments (N,4) in work coords, scale)."""
    h, w = img.shape[:2]
    s = min(1.0, work_px / max(h, w))
    g = color.luminance(np.maximum(img, 0.0)) if img.min() < 0 or img.max() > 1.0 else color.luma(img)
    g8 = (np.clip(color.srgb_oetf(g) if img.max() > 1.0 or img.min() < 0 else g, 0, 1) * 255 + 0.5).astype(np.uint8)
    if s < 1.0:
        g8 = cv2.resize(g8, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)
    g8 = cv2.GaussianBlur(g8, (0, 0), 1.0)
    edges = cv2.Canny(g8, 40, 120)
    L = max(g8.shape)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 720.0, threshold=max(30, int(0.04 * L)), minLineLength=max(20, int(0.08 * L)),
                            maxLineGap=max(4, int(0.01 * L)))
    if lines is None:
        return np.zeros((0, 4), np.float64), s
    return lines.reshape(-1, 4).astype(np.float64), s


def _peak_angle(angles, weights, max_angle, bin_deg=0.1, sigma=0.35):
    if len(angles) == 0:
        return None, 0.0
    edges = np.arange(-max_angle, max_angle + bin_deg, bin_deg)
    hist, _ = np.histogram(angles, bins=edges, weights=weights)
    k = np.exp(-0.5 * (np.arange(-int(4 * sigma / bin_deg), int(4 * sigma / bin_deg) + 1) * bin_deg / sigma) ** 2)
    sm = np.convolve(hist, k / k.sum(), mode="same")
    i = int(np.argmax(sm))
    peak = 0.5 * (edges[i] + edges[i + 1])
    sel = np.abs(angles - peak) < 0.6
    if sel.any() and weights[sel].sum() > 0:
        peak = float(np.average(angles[sel], weights=weights[sel]))
    return float(peak), float(weights[np.abs(angles - peak) < 1.0].sum())


def detect_tilt(img: np.ndarray, mode: str = "horizon", max_angle: float = 10.0):
    """Rotation (degrees, CCW-positive, as used by `rotate`) that levels the dominant near-horizontal ('horizon') or
    near-vertical ('verticals') lines; mode 'auto' picks the better supported. None when nothing convincing."""
    segs, _ = _line_segments(img)
    if len(segs) == 0:
        return None
    dx, dy = segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1]
    length = np.hypot(dx, dy)
    phi = np.degrees(np.arctan2(dy, dx))
    phi = (phi + 90.0) % 180.0 - 90.0               # (-90, 90]
    res = {}
    hor = phi
    ver = np.where(phi >= 0, phi - 90.0, phi + 90.0)
    for name, ang in (("horizon", hor), ("verticals", ver)):
        sel = np.abs(ang) <= max_angle
        res[name] = _peak_angle(ang[sel], length[sel], max_angle)
    if mode == "auto":
        mode = "verticals" if (res["verticals"][1] > res["horizon"][1] and res["verticals"][0] is not None) else "horizon"
    a, wt = res[mode]
    return None if a is None or wt < 0.25 * max(img.shape[:2]) * 0.2 else a


@op("auto_level", 11, "any", mode="horizon", max_angle=10.0, amount=1.0, angle=None, crop_mode="crop",
    changes_geometry=True, choices={"mode": ["horizon", "verticals", "auto"], "crop_mode": ["crop", "expand", "keep"]},
    category="geometry")
def auto_level(img, p, ctx: Ctx):
    """Auto-straighten: Hough-line detection of the horizon (landscape) or verticals (architecture), then rotate
    with auto-crop. angle overrides detection; amount scales the correction (0..1)."""
    if p.get("angle") is not None:
        a = float(p["angle"])
    else:
        a = detect_tilt(img, p["mode"], float(p["max_angle"]))
        if a is None:
            ctx.note("auto_level: no dominant line found, image left unchanged")
            return img
        a *= float(p["amount"])
        ctx.note(f"auto_level: rotated {a:+.2f} deg")
    return rotate_image(img, a, p["crop_mode"])


@op("straighten", 11, "any", mode="horizon", max_angle=10.0, amount=1.0, angle=None, crop_mode="crop",
    changes_geometry=True, choices={"mode": ["horizon", "verticals", "auto"], "crop_mode": ["crop", "expand", "keep"]},
    category="geometry")
def straighten(img, p, ctx: Ctx):
    """Alias of auto_level (manual `angle` or Hough horizon detection) with auto-crop."""
    return auto_level(img, p, ctx)


# --------------------------------------------------------------------------- flip
@op("flip", 11, "any", horizontal=False, vertical=False, turns=0, changes_geometry=True, category="geometry")
def flip(img, p, ctx: Ctx):
    """Mirror horizontally / vertically and rotate by quarter turns (counter-clockwise)."""
    out = img
    if p["horizontal"]:
        out = out[:, ::-1]
    if p["vertical"]:
        out = out[::-1]
    t = int(p["turns"]) % 4
    if t:
        out = np.rot90(out, t)
    return np.ascontiguousarray(out)


# --------------------------------------------------------------------------- perspective
def rotation_homography(shape, f_px: float, pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0):
    """H = K R K^-1 for a virtual camera rotation (degrees). pitch: about x (positive tilts the top toward the
    viewer, i.e. widens the top edge), yaw: about y (positive widens the right edge), roll: about z (CCW positive)."""
    h, w = shape[:2]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    K = np.array([[f_px, 0, cx], [0, f_px, cy], [0, 0, 1.0]])
    a, b, c = np.radians([pitch, yaw, roll])
    Rx = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    Ry = np.array([[np.cos(b), 0, np.sin(b)], [0, 1, 0], [-np.sin(b), 0, np.cos(b)]])
    Rz = np.array([[np.cos(c), np.sin(c), 0], [-np.sin(c), np.cos(c), 0], [0, 0, 1]])
    R = Rz @ Ry @ Rx
    return K @ R @ np.linalg.inv(K)


def _homography_from_R(shape, f_px, R):
    h, w = shape[:2]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    K = np.array([[f_px, 0, cx], [0, f_px, cy], [0, 0, 1.0]])
    return K @ R @ np.linalg.inv(K)


def _rot_between(d, e):
    """Minimal rotation matrix taking unit vector d to e (Rodrigues)."""
    d = d / np.linalg.norm(d)
    e = e / np.linalg.norm(e)
    v = np.cross(d, e)
    s = np.linalg.norm(v)
    c = float(d @ e)
    if s < 1e-12:
        return np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / (s * s))


def _scale_rotation(R, k):
    """R^k via axis-angle."""
    ang = np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1))
    if ang < 1e-9:
        return np.eye(3)
    axis = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / (2 * np.sin(ang))
    th = ang * k
    ax = axis / np.linalg.norm(axis)
    K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K


def _vanishing_direction(segs_n, tol_start=6.0, iters=6):
    """segs_n: (N,4) segments in normalised camera coords (x/f, y/f). Weighted least-squares vanishing point
    (smallest eigenvector of sum w l l^T) with iterative outlier down-weighting. Returns homogeneous direction."""
    p1 = np.concatenate([segs_n[:, 0:2], np.ones((len(segs_n), 1))], 1)
    p2 = np.concatenate([segs_n[:, 2:4], np.ones((len(segs_n), 1))], 1)
    L = np.cross(p1, p2)
    nrm = np.linalg.norm(L[:, :2], axis=1)
    L = L / np.maximum(nrm, 1e-12)[:, None]
    length = np.linalg.norm(segs_n[:, 2:4] - segs_n[:, 0:2], axis=1)
    w = length.copy()
    v = None
    for _ in range(iters):
        A = (L * w[:, None]).T @ L
        ev, evec = np.linalg.eigh(A)
        v = evec[:, 0]
        # residual: distance of segment midpoint-line to the vanishing direction (angle-ish)
        resid = np.abs(L @ v) / np.maximum(np.linalg.norm(v), 1e-12)
        med = np.median(resid) + 1e-9
        w = length * np.exp(-0.5 * (resid / (2.5 * med + 0.003)) ** 2)
    return v / np.linalg.norm(v)


def estimate_vertical_rotation(img: np.ndarray, f_px: float, which: str = "vertical", max_tilt: float = 30.0,
                               strength: float = 1.0):
    """Virtual camera rotation R that sends the dominant (near-)vertical (or horizontal) vanishing point to infinity.
    Returns (R, n_segments) or (None, 0)."""
    segs, s = _line_segments(img)
    if len(segs) < 3:
        return None, 0
    h, w = img.shape[:2]
    segs = segs / s
    dx, dy = segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1]
    phi = np.degrees(np.arctan2(dy, dx))
    phi = (phi + 90.0) % 180.0 - 90.0
    if which == "vertical":
        dev = np.where(phi >= 0, phi - 90.0, phi + 90.0)
        target = np.array([0.0, -1.0, 0.0])
    else:
        dev = phi
        target = np.array([1.0, 0.0, 0.0])
    sel = np.abs(dev) <= max_tilt
    if sel.sum() < 3:
        return None, 0
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    sg = segs[sel].copy()
    sg[:, [0, 2]] = (sg[:, [0, 2]] - cx) / f_px
    sg[:, [1, 3]] = (sg[:, [1, 3]] - cy) / f_px
    d = _vanishing_direction(sg)
    # orient d toward the target sense
    if d @ target < 0:
        d = -d
    R = _rot_between(d, target)
    return _scale_rotation(R, float(strength)), int(sel.sum())


def _focal_px(p, ctx, shape):
    if p.get("focal"):
        f35 = float(p["focal"])
    else:
        fl = ctx.meta.get("FocalLength")
        f35 = float(ctx.meta.get("FocalLengthIn35mmFormat") or (float(fl) * 1.613 if fl else 35.0))
    return f35 / 36.0 * max(shape[:2])


@op("perspective", 11, "any", vertical=0.0, horizontal=0.0, rotate=0.0, auto="none", strength=1.0, focal=None,
    corners=None, mode="crop", max_tilt=30.0, aspect="original", changes_geometry=True,
    choices={"auto": ["none", "vertical", "horizontal", "both"], "mode": ["crop", "keep"]}, category="geometry")
def perspective(img, p, ctx: Ctx):
    """Keystone / perspective correction. Manual: vertical / horizontal sliders (-100..100, +vertical widens the
    top edge, +horizontal widens the right edge, 100 = 30 deg virtual camera tilt) and roll `rotate`; `corners`
    [[x,y]*4 TL,TR,BR,BL normalised] maps a quadrilateral to the full frame; auto=vertical|horizontal|both fits
    vanishing points of detected lines (strength 0..1 = how much of the full correction to apply)."""
    h, w = img.shape[:2]
    f_px = _focal_px(p, ctx, img.shape)
    H = np.eye(3)
    if p.get("corners") is not None:
        src = np.asarray(p["corners"], np.float32).reshape(4, 2) * np.array([w, h], np.float32)
        dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
        H = cv2.getPerspectiveTransform(src, dst).astype(np.float64)
        out = warp_image(img, H, (w, h))
        return out
    R = np.eye(3)
    if p["auto"] in ("vertical", "both"):
        Rv, n = estimate_vertical_rotation(img, f_px, "vertical", float(p["max_tilt"]), float(p["strength"]))
        if Rv is not None:
            R = Rv @ R
            ctx.note(f"perspective: vertical correction from {n} lines")
        else:
            ctx.note("perspective: no vertical lines found")
    if p["auto"] in ("horizontal", "both"):
        Hc = _homography_from_R(img.shape, f_px, R)
        Rh, n = estimate_vertical_rotation(warp_image(img, Hc, (w, h)) if p["auto"] == "both" else img, f_px,
                                           "horizontal", float(p["max_tilt"]), float(p["strength"]))
        if Rh is not None:
            R = Rh @ R
            ctx.note(f"perspective: horizontal correction from {n} lines")
    H = _homography_from_R(img.shape, f_px, R)
    v, hz, roll = float(p["vertical"]), float(p["horizontal"]), float(p["rotate"])
    if v or hz or roll:
        H = rotation_homography(img.shape, f_px, 0.3 * v, 0.3 * hz, roll) @ H
    if np.allclose(H, np.eye(3), atol=1e-9):
        return img
    out = warp_image(img, H, (w, h))
    if p["mode"] == "keep":
        return out
    quad = _apply_h(H, _quad(w, h, 3.0))
    asp = parse_aspect(p.get("aspect"), portrait=h > w) if p.get("aspect") not in (None, "original") else None
    rect = max_rect_in_polygon(quad, asp or w / h, (0, 0, w, h))
    cropped = _crop_to_rect(out, rect)
    if cropped is None:         # an extreme correction leaves no usable area: never return a sliver
        ctx.note(f"warning: perspective: auto-crop would leave less than {MIN_CROP_SIDE} px; left uncropped "
                 "(lower the correction/strength or use mode=keep)")
        return out
    return cropped


# --------------------------------------------------------------------------- resize
@op("resize", 21, "display", long_edge=None, short_edge=None, width=None, height=None, megapixels=None, scale=None,
    method="auto", no_upscale=False, changes_geometry=True,
    choices={"method": ["auto", "lanczos", "area", "cubic", "linear"]}, category="geometry")
def resize(img, p, ctx: Ctx):
    """Resize by long edge, short edge, width, height, megapixels or scale. method auto = area (down) / Lanczos (up)."""
    h, w = img.shape[:2]
    s = None
    if p.get("scale"):
        s = float(p["scale"])
    elif p.get("long_edge"):
        s = float(p["long_edge"]) / max(h, w)
    elif p.get("short_edge"):
        s = float(p["short_edge"]) / min(h, w)
    elif p.get("width"):
        s = float(p["width"]) / w
    elif p.get("height"):
        s = float(p["height"]) / h
    elif p.get("megapixels"):
        s = float(np.sqrt(float(p["megapixels"]) * 1e6 / (w * h)))
    if s is None or abs(s - 1.0) < 1e-6 or (p.get("no_upscale") and s > 1.0):
        return img
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    m = p["method"]
    interp = {"area": cv2.INTER_AREA, "lanczos": cv2.INTER_LANCZOS4, "cubic": cv2.INTER_CUBIC,
              "linear": cv2.INTER_LINEAR}[m] if m != "auto" else (cv2.INTER_AREA if s < 1 else cv2.INTER_LANCZOS4)
    out = cv2.resize(np.ascontiguousarray(img, np.float32), (nw, nh), interpolation=interp)
    return np.clip(out, 0.0, 1.0).astype(np.float32)


# --------------------------------------------------------------------------- lens correction
LENSFUN_XML = package_dir() / "data" / "lensfun" / "mil-canon.xml"
_DB_CACHE: dict = {}


def _lensfun_db(xml_paths=None):
    lf = lazy_import("lensfunpy", "lens correction")
    key = tuple(str(x) for x in (xml_paths or [LENSFUN_XML]))
    if key not in _DB_CACHE:
        _DB_CACHE[key] = lf.Database(paths=list(key), load_common=False, load_bundled=False)
    return lf, _DB_CACHE[key]


def _lens_norm(s: str) -> str:
    s = s.lower().replace("canon", "").replace("rf-s", "rfs").replace("rf ", "rf")
    return re.sub(r"[^a-z0-9.\-]", "", s)


def lens_modifier(meta: dict, shape, focal=None, aperture=None, distance=None, scale: float = 0.0, flags=None,
                  camera=None, lens=None, xml_paths=None):
    """Build a lensfunpy Modifier for the image from EXIF-like meta. Returns (modifier, info) or (None, reason)."""
    try:
        lf, db = _lensfun_db(xml_paths)
    except Exception as exc:  # MissingDependency or broken native library
        return None, f"lensfunpy unavailable ({exc})"
    cam_name = camera or " ".join(x for x in (meta.get("Make"), meta.get("Model")) if x)
    model = (camera or meta.get("Model") or "")
    maker = meta.get("Make") or None
    cams = db.find_cameras(maker, model) if model else []
    if not cams and model:
        cams = db.find_cameras(None, model, loose_search=True)
    if not cams:
        return None, f"camera '{cam_name}' not in lensfun data"
    cam = cams[0]
    lname = lens or meta.get("LensModel") or meta.get("Lens") or ""
    if not lname:
        return None, "no lens model in metadata"
    fl = focal or meta.get("FocalLength")
    fl = float(fl) if fl else None
    cands = db.find_lenses(cam, None, lname, loose_search=True)
    target = _lens_norm(lname)
    best, best_r = None, 0.0
    for c in cands:
        if fl and not (c.min_focal - 0.5 <= fl <= c.max_focal + 0.5):
            continue
        r = difflib.SequenceMatcher(None, target, _lens_norm(c.model)).ratio()
        if r > best_r:
            best, best_r = c, r
    if best is None or best_r < 0.72:
        return None, f"lens '{lname}' not matched in lensfun data (best ratio {best_r:.2f})"
    h, w = shape[:2]
    fl = fl or best.min_focal
    ap = float(aperture or meta.get("FNumber") or max(best.min_aperture or 5.6, 5.6))
    dist = float(distance or meta.get("FocusDistance") or 1000.0)
    mod = lf.Modifier(best, cam.crop_factor, w, h)
    mod.initialize(fl, ap, dist, scale=float(scale), pixel_format=np.float32,
                   flags=flags if flags is not None else lf.ModifyFlags.ALL)
    return mod, {"camera": cam.model, "lens": best.model, "focal": fl, "aperture": ap, "ratio": best_r}


def _radial_grid(h, w):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    rn = np.hypot(w - 1, h - 1) / 2.0
    return (xx - cx) / rn, (yy - cy) / rn, cx, cy, rn


def manual_lens_fix(img, k1=0.0, k2=0.0, vignette=0.0, autoscale=True):
    """Manual correction: radial distortion r_src = r (1 + k1 r^2 + k2 r^4) with r normalised to the half diagonal
    (k1 < 0 corrects barrel, k1 > 0 corrects pincushion) and a vignette gain 1 + vignette/100 * r^2."""
    h, w = img.shape[:2]
    out = img
    if vignette:
        X, Y, *_ = _radial_grid(h, w)
        gain = 1.0 + float(vignette) / 100.0 * (X * X + Y * Y)
        out = (out * gain[..., None]).astype(np.float32)
    if k1 or k2:
        X, Y, cx, cy, rn = _radial_grid(h, w)

        def f(z):
            r2 = (X * X + Y * Y) / (z * z)
            return 1.0 + k1 * r2 + k2 * r2 * r2

        z = 1.0
        if autoscale:   # smallest zoom so every output pixel samples inside the source
            def inside(z):
                sc = f(z) / z
                sx, sy = X * sc * rn + cx, Y * sc * rn + cy
                return sx.min() >= 0 and sy.min() >= 0 and sx.max() <= w - 1 and sy.max() <= h - 1
            lo, hi = 1.0, 3.0
            if not inside(1.0):
                for _ in range(24):
                    mid = 0.5 * (lo + hi)
                    lo, hi = (lo, mid) if inside(mid) else (mid, hi)
                z = hi
        sc = f(z) / z
        mapx = (X * sc * rn + cx).astype(np.float32)
        mapy = (Y * sc * rn + cy).astype(np.float32)
        out = _clip_nonneg(cv2.remap(np.ascontiguousarray(out, np.float32), mapx, mapy, cv2.INTER_LANCZOS4,
                                     borderMode=cv2.BORDER_REFLECT), img)
    return out.astype(np.float32)


@op("lens_correct", 7, "any", auto=True, distortion=True, tca=True, vignetting=True, scale=0.0, camera=None,
    lens=None, focal=None, aperture=None, distance=None, k1=0.0, k2=0.0, vignette=0.0, autoscale=True,
    force=False, category="geometry")
def lens_correct(img, p, ctx: Ctx):
    """Lens corrections (vignetting in linear light, then distortion + TCA in one remap) from lensfun for the
    camera/lens in the metadata (bundled Canon RF-S data). Falls back to manual k1/k2/vignette; if lensfunpy
    or the lens is missing, only a note is logged. Camera JPEGs are skipped (already corrected) unless force."""
    manual = p["k1"] or p["k2"] or p["vignette"]
    out = img
    used_auto = False
    if p["auto"] and (ctx.kind != "jpeg" or p["force"]):
        lf_flags = 0
        try:
            lf = lazy_import("lensfunpy", "lens correction")
            F = lf.ModifyFlags
            if p["distortion"]:
                lf_flags |= int(F.DISTORTION) | int(F.GEOMETRY)
            if p["tca"]:
                lf_flags |= int(F.TCA)
            if p["vignetting"]:
                lf_flags |= int(F.VIGNETTING)
            if lf_flags:
                lf_flags |= int(F.SCALE)
        except Exception as exc:
            ctx.note(f"lens_correct: lensfunpy unavailable ({exc}); manual correction only")
            lf_flags = 0
        if lf_flags:
            mod, info = lens_modifier(ctx.meta, img.shape, p["focal"], p["aperture"], p["distance"],
                                      float(p["scale"]), lf_flags, p["camera"], p["lens"])
            if mod is None:
                ctx.note(f"lens_correct: {info}")
            else:
                used_auto = True
                work = np.ascontiguousarray(img, np.float32).copy()
                if p["vignetting"]:
                    mod.apply_color_modification(work)
                if p["distortion"] or p["tca"]:
                    h, w = work.shape[:2]
                    coords = mod.apply_subpixel_geometry_distortion() if p["tca"] else None
                    if coords is None:
                        c2 = mod.apply_geometry_distortion() if p["distortion"] else None
                        if c2 is not None:
                            out = np.stack([cv2.remap(work[..., c], c2[..., 0], c2[..., 1], cv2.INTER_LANCZOS4,
                                                      borderMode=cv2.BORDER_REFLECT) for c in range(3)], -1)
                        else:
                            out = work
                    else:
                        out = np.stack([cv2.remap(work[..., c], coords[..., c, 0], coords[..., c, 1],
                                                  cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT) for c in range(3)], -1)
                    out = _clip_nonneg(out, img).astype(np.float32)
                else:
                    out = work
                ctx.note(f"lens_correct: {info['lens']} @ {info['focal']}mm f/{info['aperture']}")
    elif p["auto"]:
        ctx.note("lens_correct: camera JPEG assumed already corrected in-camera (use force=true to override)")
    if manual and not used_auto:
        out = manual_lens_fix(out, float(p["k1"]), float(p["k2"]), float(p["vignette"]), bool(p["autoscale"]))
    return out
