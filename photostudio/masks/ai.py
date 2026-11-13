"""AI masks (subject/background/sky/face parts/animal/depth) with lazy model imports and non-AI fallbacks.

Every AI mask is computed at <= 1024 px (models.MAX_SIDE), upsampled to the image with a guided filter against
the full-resolution image (radius ~12 px at 1024, eps 1e-3) and cached in ``ctx.cache`` keyed by type and image
shape. Models are never auto-downloaded (`photo models download <name>`); when one is missing the fallback runs
and a note is appended to ctx.log:

  ai_subject / ai_background  ONNX matte (IS-Net / U2-Net / BiRefNet)      -> GrabCut seeded by centre-weighted saliency
  ai_sky                      SegFormer ADE20K 'sky' class                  -> top-connected blue/bright low-texture region
  ai_face/eyes/lips/teeth/skin MediaPipe Face Landmarker (478 pts)          -> Haar cascade or skin-colour blob + canonical geometry
  ai_animal                   Ultralytics YOLO11-seg (AGPL, personal use)   -> subject mask
  depth                       Depth Anything V2 Small (onnx / transformers) -> defocus + vertical-position heuristic
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .. import color, models
from ..filters import fast_guided
from ..util import MissingDependency, lazy_import
from . import cache as _cache
from .common import get_domain, note, resize_mask, smoothstep, to_display
from .parametric import trapezoid

COCO_ANIMALS = {14: "bird", 15: "cat", 16: "dog", 17: "horse", 18: "sheep", 19: "cow", 20: "elephant", 21: "bear",
                22: "zebra", 23: "giraffe"}


# --------------------------------------------------------------------------- plumbing
def _cache_get(ctx, key):
    """Per-context cache first, then the optional process-level cache (``masks.enable_cache`` / ``set_cache``)."""
    c = getattr(ctx, "cache", None)
    hit = None if c is None else c.get(key)
    if hit is not None:
        return hit
    hit = _cache.process_get(ctx, key)
    if hit is not None and c is not None:
        c[key] = hit
    return hit


def _cache_put(ctx, key, value):
    c = getattr(ctx, "cache", None)
    if c is not None:
        c[key] = value
    _cache.process_put(ctx, key, value)
    return value


def _display_base(img: np.ndarray, ctx) -> np.ndarray:
    """Stable display-referred view for AI input: prefers ctx.source (un-edited linear decode) when its aspect
    matches the image, so AI masks do not change when upstream edits change."""
    domain = get_domain(ctx)
    src = getattr(ctx, "source", None)
    if src is not None and src.ndim == 3:
        h, w = img.shape[:2]
        sh, sw = src.shape[:2]
        if abs(sw / sh - w / h) / (w / h) < 0.01:
            s = src if (sh, sw) == (h, w) else cv2.resize(src, (w, h), interpolation=cv2.INTER_AREA)
            return to_display(s, "linear")
    return to_display(img, domain)


def _small(disp: np.ndarray, long_edge: int = models.MAX_SIDE) -> np.ndarray:
    h, w = disp.shape[:2]
    s = long_edge / max(h, w)
    if s >= 1.0:
        return disp
    return cv2.resize(disp, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)


def upsample_guided(m_small: np.ndarray, disp_full: np.ndarray, radius_at_1024: float = 12.0, eps: float = 1e-3) -> np.ndarray:
    """Edge-aware upsampling of a low-res mask: bilinear resize then guided filter with the full-res image."""
    h, w = disp_full.shape[:2]
    m = resize_mask(m_small.astype(np.float32), (h, w))
    r = max(2, int(round(radius_at_1024 * max(h, w) / 1024.0)))
    return np.clip(fast_guided(disp_full.astype(np.float32), m, r, eps), 0.0, 1.0).astype(np.float32)


def _backend_name() -> str:
    try:
        return "CUDA" if "CUDAExecutionProvider" in models.ort_providers() else "CPU"
    except MissingDependency:
        return "CPU"


# --------------------------------------------------------------------------- subject
def _onnx_matte(name: str, rgb01: np.ndarray) -> np.ndarray:
    """Run an IS-Net / U2-Net / BiRefNet ONNX matte model on a display image (0..1) -> float mask 0..1 at input size."""
    m = models.info(name)
    sess = models.ort_session(name)
    size = int(m.extra["size"])
    h, w = rgb01.shape[:2]
    arr = cv2.resize(rgb01, (size, size), interpolation=cv2.INTER_AREA if max(h, w) > size else cv2.INTER_LINEAR)
    arr = arr.astype(np.float32)
    arr = arr / max(float(arr.max()), 1e-6)
    arr = (arr - np.array(m.extra["mean"], np.float32)) / np.array(m.extra["std"], np.float32)
    inp = np.ascontiguousarray(arr.transpose(2, 0, 1)[None], dtype=np.float32)
    out = sess.run(None, {sess.get_inputs()[0].name: inp})[0]
    pred = np.asarray(out).reshape(out.shape[-3:])[0] if out.ndim >= 3 else np.asarray(out)
    if m.extra.get("post") == "logit" or pred.min() < -1e-3 or pred.max() > 1.001:
        if m.extra.get("post") == "minmax":
            pred = (pred - pred.min()) / max(float(pred.max() - pred.min()), 1e-6)
        else:
            pred = 1.0 / (1.0 + np.exp(-pred))
    else:
        pred = (pred - pred.min()) / max(float(pred.max() - pred.min()), 1e-6)
    return cv2.resize(pred.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)


def saliency_map(disp: np.ndarray) -> np.ndarray:
    """Centre-weighted saliency 0..1: frequency-tuned colour contrast against the image mean and against the
    border colour (background prior), times a centre prior."""
    h, w = disp.shape[:2]
    lab = cv2.cvtColor(np.clip(disp, 0, 1).astype(np.float32), cv2.COLOR_RGB2Lab)
    blur = cv2.GaussianBlur(lab, (0, 0), max(1.0, min(h, w) / 120.0))
    mu = lab.reshape(-1, 3).mean(0)
    s1 = np.linalg.norm(blur - mu, axis=2)
    bw = max(2, int(0.04 * min(h, w)))
    border = np.concatenate([lab[:bw].reshape(-1, 3), lab[-bw:].reshape(-1, 3),
                             lab[:, :bw].reshape(-1, 3), lab[:, -bw:].reshape(-1, 3)])
    bmu = np.median(border, axis=0)
    s2 = np.linalg.norm(blur - bmu, axis=2)

    def n(a):
        lo, hi = np.percentile(a, 1), np.percentile(a, 99)
        return np.clip((a - lo) / max(hi - lo, 1e-6), 0, 1)

    ys = (np.arange(h, dtype=np.float32) + 0.5) / h - 0.5
    xs = (np.arange(w, dtype=np.float32) + 0.5) / w - 0.5
    centre = np.exp(-(xs[None, :] ** 2 / (2 * 0.30 ** 2) + ys[:, None] ** 2 / (2 * 0.30 ** 2)))
    s = (0.5 * n(s1) + 0.5 * n(s2)) * (0.35 + 0.65 * centre)
    return n(s).astype(np.float32)


def grabcut_subject(disp: np.ndarray, iters: int = 4) -> np.ndarray:
    """Fallback subject mask: GrabCut seeded from the saliency map (small image, <= 480 px)."""
    small = _small(disp, 480)
    h, w = small.shape[:2]
    sal = saliency_map(small)
    gc = np.full((h, w), cv2.GC_PR_BGD, np.uint8)
    gc[sal > 0.35] = cv2.GC_PR_FGD
    top = sal > max(0.8, np.percentile(sal, 97))
    gc[top] = cv2.GC_FGD
    gc[sal < 0.06] = cv2.GC_BGD
    bw = max(2, int(0.02 * min(h, w)))
    gc[:bw] = gc[-bw:] = cv2.GC_BGD
    gc[:, :bw] = gc[:, -bw:] = cv2.GC_BGD
    if not (gc == cv2.GC_FGD).any():
        gc[sal >= sal.max() * 0.9] = cv2.GC_FGD
    img8 = (np.clip(small, 0, 1) * 255).astype(np.uint8)[..., ::-1].copy()
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(img8, gc, None, bgd, fgd, iters, cv2.GC_INIT_WITH_MASK)
        fg = ((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD)).astype(np.float32)
    except cv2.error:
        fg = (sal > 0.5).astype(np.float32)
    frac = float(fg.mean())
    if frac < 0.01 or frac > 0.95:
        fg = smoothstep(0.3, 0.6, sal).astype(np.float32)   # GrabCut degenerate: use the soft saliency
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    fg = cv2.GaussianBlur(fg, (0, 0), max(1.0, 0.004 * max(h, w)))
    fg = np.clip(fast_guided(small.astype(np.float32), fg, max(2, int(0.01 * max(h, w))), 1e-3), 0, 1)
    return resize_mask(fg.astype(np.float32), disp.shape[:2])


def subject_mask(img: np.ndarray, ctx, spec: dict | None = None) -> np.ndarray:
    spec = spec or {}
    key = ("ai", "subject", img.shape[:2], spec.get("model"))
    hit = _cache_get(ctx, key)
    if hit is not None:
        return hit
    disp = _display_base(img, ctx)
    small = _small(disp)
    pref = [spec["model"]] if spec.get("model") else ["isnet-general-use", "birefnet-general-lite", "u2net",
                                                      "birefnet-general"]
    out = None
    for name in pref:
        if name in models.REGISTRY and models.usable(name):
            try:
                out = _onnx_matte(name, small)
                note(ctx, f"ai_subject: {name} via onnxruntime ({_backend_name()})")
                break
            except Exception as exc:   # noqa: BLE001  model/runtime problem -> try next / fallback
                note(ctx, f"ai_subject: {name} failed ({exc}); trying next")
    if out is None:
        note(ctx, "ai_subject: no matte model installed (photo models download isnet-general-use); "
                  "using GrabCut + saliency fallback")
        m = grabcut_subject(disp)
        return _cache_put(ctx, key, m)
    m = upsample_guided(out, disp)
    return _cache_put(ctx, key, m)


# --------------------------------------------------------------------------- sky
def sky_heuristic(disp: np.ndarray) -> np.ndarray:
    """Top-connected, blue/bright, low-texture region, refined with a guided filter. Works at <= 512 px."""
    small = _small(disp, 512)
    h, w = small.shape[:2]
    lab = color.display_to_oklab(small)
    C = np.hypot(lab[..., 1], lab[..., 2])
    luma = color.luma(small)
    R, G, B = small[..., 0], small[..., 1], small[..., 2]
    blue = np.clip((B - np.maximum(R, 0.9 * G)) / 0.12, 0, 1) * smoothstep(0.2, 0.4, luma)
    cloud = smoothstep(0.76, 0.88, luma) * (1.0 - smoothstep(0.04, 0.10, C))
    glare = smoothstep(0.92, 0.985, luma)
    mean = cv2.boxFilter(luma, -1, (9, 9))
    var = np.maximum(cv2.boxFilter(luma * luma, -1, (9, 9)) - mean * mean, 0)
    lowtex = 1.0 - smoothstep(0.02, 0.07, np.sqrt(var))
    cand = np.maximum(blue, np.maximum(cloud, glare)) * np.maximum(lowtex, glare)
    binary = (cand > 0.5).astype(np.uint8)
    k = np.ones((3, 3), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, k)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=4)
    top_rows = max(2, int(0.03 * h))
    keep = np.zeros(n, bool)
    touching = np.unique(lbl[:top_rows][lbl[:top_rows] > 0])
    for i in touching:
        if stats[i, cv2.CC_STAT_AREA] >= 0.002 * h * w:
            keep[i] = True
    if not keep.any() and n > 1:   # no sky at the top edge (cropped): take the largest upper-half component
        best = None
        for i in range(1, n):
            cy = stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] / 2
            if cy < 0.5 * h and (best is None or stats[i, cv2.CC_STAT_AREA] > stats[best, cv2.CC_STAT_AREA]):
                best = i
        if best is not None and stats[best, cv2.CC_STAT_AREA] > 0.03 * h * w:
            keep[best] = True
    sky = keep[lbl].astype(np.uint8)
    # fill small holes (birds, sun spots): flood the background from the border, invert
    inv = (1 - sky).astype(np.uint8)
    ff = inv.copy()
    msk = np.zeros((h + 2, w + 2), np.uint8)
    for (x, y) in ((0, h - 1), (w - 1, h - 1)):
        if ff[y, x]:
            cv2.floodFill(ff, msk, (x, y), 2)
    holes = (ff == 1)
    small_holes = holes & (cv2.erode(holes.astype(np.uint8), np.ones((5, 5), np.uint8)) == 0)
    sky = np.where(small_holes, 1, sky).astype(np.float32)
    sky = cv2.GaussianBlur(sky, (0, 0), 1.2)
    sky = np.clip(fast_guided(small.astype(np.float32), sky, max(2, int(0.012 * max(h, w))), 2e-3), 0, 1)
    return resize_mask(sky.astype(np.float32), disp.shape[:2])


def _segformer_sky(disp: np.ndarray) -> np.ndarray:
    torch = lazy_import("torch", "SegFormer sky model")
    tf = lazy_import("transformers", "SegFormer sky model")
    d = str(models.path("segformer-ade-b0"))
    proc = tf.SegformerImageProcessor.from_pretrained(d)
    dev = models.torch_device()
    model = tf.SegformerForSemanticSegmentation.from_pretrained(d).to(dev).eval()
    small = _small(disp, 640)
    inp = proc(images=(np.clip(small, 0, 1) * 255).astype(np.uint8), return_tensors="pt")
    with torch.no_grad():
        logits = model(pixel_values=inp["pixel_values"].to(dev)).logits
        logits = torch.nn.functional.interpolate(logits, size=small.shape[:2], mode="bilinear", align_corners=False)
        prob = torch.softmax(logits, 1)[0, 2].float().cpu().numpy()   # ADE20K class 2 = sky
    return prob.astype(np.float32)


def sky_mask(img: np.ndarray, ctx, spec: dict | None = None) -> np.ndarray:
    key = ("ai", "sky", img.shape[:2])
    hit = _cache_get(ctx, key)
    if hit is not None:
        return hit
    disp = _display_base(img, ctx)
    if models.usable("segformer-ade-b0"):
        try:
            p = _segformer_sky(disp)
            note(ctx, f"ai_sky: SegFormer-B0 ADE20K ({models.torch_device()})")
            return _cache_put(ctx, key, upsample_guided(p, disp))
        except Exception as exc:   # noqa: BLE001
            note(ctx, f"ai_sky: SegFormer failed ({exc}); using heuristic")
    note(ctx, "ai_sky: heuristic (top-connected blue/bright low-texture region + guided filter)")
    m = sky_heuristic(disp)
    m = upsample_guided(m, disp, 8.0) if m.shape != disp.shape[:2] else m
    return _cache_put(ctx, key, m)


# --------------------------------------------------------------------------- skin colour model
def skin_probability(disp: np.ndarray) -> np.ndarray:
    """Soft skin likelihood from Oklch trapezoids (L, C, h) on display-referred data."""
    lch = color.oklab_to_oklch(color.display_to_oklab(np.clip(disp, 0, 1)))
    L, C, h = lch[..., 0], lch[..., 1], lch[..., 2]
    p = (trapezoid(h, (5, 22, 62, 82), smooth=True, circular=True)
         * trapezoid(C, (0.02, 0.05, 0.14, 0.20), smooth=True)
         * trapezoid(L, (0.25, 0.45, 0.93, 0.99), smooth=True))
    return p.astype(np.float32)


# --------------------------------------------------------------------------- faces
# MediaPipe face-mesh contour loops (ordered). 468-477 irises.
LM = {
    "oval": [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176,
             149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109],
    "lips_outer": [61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291, 375, 321, 405, 314, 17, 84, 181, 91, 146],
    "lips_inner": [78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95],
    "eye_r": [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246],
    "eye_l": [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398],
    "brow_r": [70, 63, 105, 66, 107, 55, 65, 52, 53, 46],
    "brow_l": [300, 293, 334, 296, 336, 285, 295, 282, 283, 276],
    "iris_r": [469, 470, 471, 472],
    "iris_l": [474, 475, 476, 477],
}


@dataclass
class Face:
    box: tuple                       # x0, y0, x1, y1 normalised
    landmarks: np.ndarray | None = None   # (478, 2) normalised, or None for the geometric fallback
    source: str = "geometry"
    parts_cache: dict = field(default_factory=dict)

    # ---- helpers
    def _poly(self, key: str, h: int, w: int, blur: float) -> np.ndarray:
        pts = self.landmarks[LM[key]] * np.array([w, h], np.float32)
        return _fill_poly(h, w, pts, blur)

    def part(self, name: str, h: int, w: int) -> np.ndarray:
        """float mask for 'face'|'skin_geom'|'eyes'|'iris'|'lips_outer'|'mouth'|'brows'."""
        blur = max(0.8, 0.0025 * max(h, w))
        if self.landmarks is not None:
            if name == "face":
                return self._poly("oval", h, w, blur)
            if name == "eyes":
                return np.maximum(self._poly("eye_r", h, w, blur), self._poly("eye_l", h, w, blur))
            if name == "iris":
                a = np.maximum(self._poly("iris_r", h, w, blur), self._poly("iris_l", h, w, blur))
                return a
            if name == "lips_outer":
                return self._poly("lips_outer", h, w, blur)
            if name == "mouth":
                return self._poly("lips_inner", h, w, blur)
            if name == "brows":
                return np.maximum(self._poly("brow_r", h, w, blur * 2), self._poly("brow_l", h, w, blur * 2))
        return self._geometric(name, h, w, blur)

    def _geometric(self, name: str, h: int, w: int, blur: float) -> np.ndarray:
        x0, y0, x1, y1 = self.box
        bw, bh = (x1 - x0) * w, (y1 - y0) * h
        cx, cy = 0.5 * (x0 + x1) * w, 0.5 * (y0 + y1) * h
        top = y0 * h
        if name == "face":
            return _ellipse(h, w, cx, cy, 0.5 * bw, 0.5 * bh, blur)
        if name == "eyes":
            m = np.zeros((h, w), np.float32)
            for sx in (-1, 1):
                m = np.maximum(m, _ellipse(h, w, cx + sx * 0.22 * bw, top + 0.39 * bh, 0.115 * bw, 0.05 * bh, blur))
            return m
        if name == "iris":
            m = np.zeros((h, w), np.float32)
            for sx in (-1, 1):
                m = np.maximum(m, _ellipse(h, w, cx + sx * 0.22 * bw, top + 0.39 * bh, 0.05 * bw, 0.05 * bw, blur))
            return m
        if name == "lips_outer":
            return _ellipse(h, w, cx, top + 0.76 * bh, 0.19 * bw, 0.055 * bh, blur)
        if name == "mouth":
            return _ellipse(h, w, cx, top + 0.76 * bh, 0.14 * bw, 0.025 * bh, blur)
        if name == "brows":
            m = np.zeros((h, w), np.float32)
            for sx in (-1, 1):
                m = np.maximum(m, _ellipse(h, w, cx + sx * 0.22 * bw, top + 0.30 * bh, 0.14 * bw, 0.03 * bh, blur))
            return m
        raise KeyError(name)


def _fill_poly(h: int, w: int, pts_px: np.ndarray, blur: float) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    cv2.fillPoly(m, [np.round(pts_px * 16).astype(np.int32)], 255, lineType=cv2.LINE_AA, shift=4)
    f = m.astype(np.float32) / 255.0
    return cv2.GaussianBlur(f, (0, 0), blur) if blur > 0.3 else f


def _ellipse(h: int, w: int, cx: float, cy: float, a: float, b: float, blur: float) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    cv2.ellipse(m, (int(round(cx * 16)), int(round(cy * 16))), (max(1, int(round(a * 16))), max(1, int(round(b * 16)))),
                0, 0, 360, 255, -1, lineType=cv2.LINE_AA, shift=4)
    f = m.astype(np.float32) / 255.0
    return cv2.GaussianBlur(f, (0, 0), blur) if blur > 0.3 else f


def _mediapipe_faces(disp: np.ndarray) -> list[Face]:
    mp = lazy_import("mediapipe", "face landmarks")
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    opts = vision.FaceLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(models.path("face-landmarker"))),
        running_mode=vision.RunningMode.IMAGE, num_faces=8)
    lm = vision.FaceLandmarker.create_from_options(opts)
    small = _small(disp, 1280)
    rgb = np.ascontiguousarray((np.clip(small, 0, 1) * 255).astype(np.uint8))
    res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    faces = []
    for fl in res.face_landmarks:
        pts = np.array([[p.x, p.y] for p in fl], np.float32)
        if len(pts) < 478:
            pts = np.vstack([pts, np.repeat(pts[-1:], 478 - len(pts), 0)])
        faces.append(Face((float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())),
                          pts, "mediapipe"))
    try:
        lm.close()
    except Exception:  # noqa: BLE001
        pass
    return faces


def _haar_cascade(model: str, fname: str):
    p = models.path(model)
    cand = [p] if models.installed(model) else []
    data = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if data:
        from pathlib import Path

        cand.append(Path(data) / fname)
    for c in cand:
        if c.is_file():
            cc = cv2.CascadeClassifier(str(c))
            if not cc.empty():
                return cc
    return None


def _haar_faces(disp: np.ndarray) -> list[Face]:
    cc = _haar_cascade("haar-face", "haarcascade_frontalface_default.xml")
    if cc is None:
        return []
    small = _small(disp, 800)
    g = (color.luma(small) * 255).astype(np.uint8)
    g = cv2.equalizeHist(g)
    h, w = g.shape
    rects = cc.detectMultiScale(g, 1.1, 5, minSize=(max(24, w // 20), max(24, w // 20)))
    return [Face((x / w, y / h, (x + rw) / w, (y + rh) / h), None, "haar") for x, y, rw, rh in rects]


def _skin_blob_faces(disp: np.ndarray) -> list[Face]:
    """Last-resort face finder: large compact skin-coloured blobs -> boxes (geometry model)."""
    small = _small(disp, 400)
    h, w = small.shape[:2]
    p = skin_probability(small)
    b = (p > 0.5).astype(np.uint8)
    b = cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    b = cv2.morphologyEx(b, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(b, connectivity=8)
    faces = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 0.004 * h * w or area / max(bw * bh, 1) < 0.5:
            continue
        ar = bw / max(bh, 1)
        if not (0.4 < ar < 1.4):
            continue
        faces.append((area, Face((x / w, y / h, (x + bw) / w, (y + bh) / h), None, "skin-blob")))
    faces.sort(key=lambda t: -t[0])
    return [f for _, f in faces[:3]]


def detect_faces(img: np.ndarray, ctx) -> list[Face]:
    key = ("ai", "faces", img.shape[:2])
    hit = _cache_get(ctx, key)
    if hit is not None:
        return hit
    disp = _display_base(img, ctx)
    faces: list[Face] = []
    if models.usable("face-landmarker"):
        try:
            faces = _mediapipe_faces(disp)
            if faces:
                note(ctx, "face: MediaPipe Face Landmarker (478 landmarks)")
        except Exception as exc:   # noqa: BLE001
            note(ctx, f"face: MediaPipe failed ({exc}); falling back")
    if not faces:
        faces = _haar_faces(disp)
        if faces:
            note(ctx, "face: OpenCV Haar cascade + canonical geometry (no landmark model)")
    if not faces:
        faces = _skin_blob_faces(disp)
        if faces:
            note(ctx, "face: skin-colour blob + canonical geometry (no landmark/Haar model)")
    return _cache_put(ctx, key, faces)


def face_part_mask(img: np.ndarray, ctx, part: str, spec: dict | None = None) -> np.ndarray:
    """ai_face | ai_eyes | ai_lips | ai_teeth | ai_skin as float masks."""
    spec = spec or {}
    key = ("ai", part, img.shape[:2], spec.get("part"), spec.get("faces"))
    hit = _cache_get(ctx, key)
    if hit is not None:
        return hit
    h, w = img.shape[:2]
    disp = _display_base(img, ctx)
    faces = detect_faces(img, ctx)
    sel = spec.get("faces")
    if sel is not None:
        faces = [faces[i] for i in ([sel] if isinstance(sel, int) else sel) if 0 <= i < len(faces)]
    sk = None
    out = np.zeros((h, w), np.float32)
    if not faces:
        if part == "ai_skin":
            note(ctx, "ai_skin: no face found, using skin-colour range only")
            sk = skin_probability(disp)
            sk = cv2.morphologyEx(sk, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            return _cache_put(ctx, key, np.clip(smoothstep(0.15, 0.6, sk), 0, 1).astype(np.float32))
        note(ctx, f"{part}: no face found -> empty mask")
        return _cache_put(ctx, key, out)
    for f in faces:
        if part == "ai_face":
            m = f.part("face", h, w)
        elif part == "ai_eyes":
            which = spec.get("part", "eye")
            m = f.part("iris", h, w) if which == "iris" else f.part("eyes", h, w)
            if which == "both":
                m = np.maximum(m, f.part("iris", h, w))
        elif part == "ai_lips":
            outer = f.part("lips_outer", h, w)
            m = outer
            if f.landmarks is not None:
                m = np.clip(outer - 0.9 * f.part("mouth", h, w), 0, 1)
            else:   # geometry only: weight by lip-ness (redder than the surrounding skin)
                lab = color.display_to_oklab(disp)
                ring = (cv2.dilate((outer > 0.5).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0) & (outer < 0.05)
                skin_a = float(np.median(lab[..., 1][ring])) if ring.any() else 0.05
                red = smoothstep(skin_a + 0.01, skin_a + 0.05, lab[..., 1])
                m = outer * np.where(red.max() > 0, np.maximum(red, 0.15), 1.0)
        elif part == "ai_teeth":
            mouth = f.part("mouth", h, w)
            lch = color.oklab_to_oklch(color.display_to_oklab(disp))
            bright = smoothstep(0.62, 0.82, lch[..., 0])
            lowc = 1.0 - smoothstep(0.05, 0.11, lch[..., 1])
            m = mouth * bright * lowc
        elif part == "ai_skin":
            if sk is None:
                sk = skin_probability(disp)
            face = f.part("face", h, w)
            excl = np.maximum.reduce([f.part("eyes", h, w), f.part("lips_outer", h, w),
                                      cv2.GaussianBlur(f.part("brows", h, w), (0, 0), 1.0)])
            m = face * (1.0 - np.clip(excl * 1.2, 0, 1)) * smoothstep(0.08, 0.5, sk)
        else:
            raise KeyError(part)
        out = np.maximum(out, m)
    return _cache_put(ctx, key, np.clip(out, 0, 1).astype(np.float32))


# --------------------------------------------------------------------------- animal
def animal_mask(img: np.ndarray, ctx, spec: dict | None = None) -> np.ndarray:
    spec = spec or {}
    key = ("ai", "animal", img.shape[:2], tuple(spec.get("classes", []) or []))
    hit = _cache_get(ctx, key)
    if hit is not None:
        return hit
    disp = _display_base(img, ctx)
    if models.usable("yolo11n-seg"):
        try:
            yolo = lazy_import("ultralytics", "YOLO animal segmentation")
            net = yolo.YOLO(str(models.path("yolo11n-seg")))
            small = _small(disp, 1024)
            res = net.predict((np.clip(small, 0, 1) * 255).astype(np.uint8)[..., ::-1], verbose=False,
                              device=0 if models.torch_device() == "cuda" else "cpu")[0]
            want = set(spec.get("classes") or COCO_ANIMALS.values())
            m = np.zeros(small.shape[:2], np.float32)
            if res.masks is not None:
                for mk, cl in zip(res.masks.data.cpu().numpy(), res.boxes.cls.cpu().numpy()):
                    if COCO_ANIMALS.get(int(cl)) in want or int(cl) in want:
                        m = np.maximum(m, cv2.resize(mk.astype(np.float32), (small.shape[1], small.shape[0])))
            if m.max() > 0:
                note(ctx, "ai_animal: YOLO11n-seg (AGPL-3.0, personal use)")
                return _cache_put(ctx, key, upsample_guided(m, disp))
            note(ctx, "ai_animal: YOLO found no animal; using subject mask")
        except Exception as exc:   # noqa: BLE001
            note(ctx, f"ai_animal: YOLO failed ({exc}); using subject mask")
    else:
        note(ctx, "ai_animal: YOLO model not installed (pip install ultralytics; photo models download yolo11n-seg); "
                  "using subject mask")
    return _cache_put(ctx, key, subject_mask(img, ctx))


# --------------------------------------------------------------------------- depth
def _depth_onnx(disp: np.ndarray) -> np.ndarray:
    sess = models.ort_session("depth-anything-v2-small")
    size = models.DEPTH_SIDE
    small = cv2.resize(disp, (size, size), interpolation=cv2.INTER_AREA if max(disp.shape[:2]) > size else cv2.INTER_LINEAR)
    arr = (small - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    inp = np.ascontiguousarray(arr.transpose(2, 0, 1)[None], dtype=np.float32)
    out = sess.run(None, {sess.get_inputs()[0].name: inp})[0]
    return np.asarray(out).reshape(out.shape[-2:]).astype(np.float32)


def _depth_transformers(disp: np.ndarray) -> np.ndarray:
    torch = lazy_import("torch", "Depth Anything (transformers)")
    tf = lazy_import("transformers", "Depth Anything (transformers)")
    d = str(models.path("depth-anything-v2-small-hf"))
    proc = tf.AutoImageProcessor.from_pretrained(d)
    dev = models.torch_device()
    net = tf.AutoModelForDepthEstimation.from_pretrained(d).to(dev).eval()
    if dev == "cuda":
        net = net.half()
    inp = proc(images=(np.clip(_small(disp, 700), 0, 1) * 255).astype(np.uint8), return_tensors="pt")
    pv = inp["pixel_values"].to(dev)
    if dev == "cuda":
        pv = pv.half()
    with torch.no_grad():
        pred = net(pixel_values=pv).predicted_depth
    return pred[0].float().cpu().numpy()


def depth_heuristic(disp: np.ndarray) -> np.ndarray:
    """Fallback relative depth (0 near .. 1 far): vertical position (lower = nearer) + defocus (blurrier = farther)."""
    small = _small(disp, 480)
    h, w = small.shape[:2]
    luma = color.luma(small)
    gx = cv2.Sobel(luma, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(luma, cv2.CV_32F, 0, 1, ksize=3)
    sharp = cv2.GaussianBlur(np.sqrt(gx * gx + gy * gy), (0, 0), max(3.0, 0.03 * max(h, w)))
    lo, hi = np.percentile(sharp, 5), np.percentile(sharp, 95)
    sharp = np.clip((sharp - lo) / max(hi - lo, 1e-6), 0, 1)
    ypos = np.linspace(1.0, 0.0, h, dtype=np.float32)[:, None] * np.ones((1, w), np.float32)   # top far
    d = 0.65 * ypos + 0.35 * (1.0 - sharp)
    return cv2.GaussianBlur(d, (0, 0), max(2.0, 0.02 * max(h, w))).astype(np.float32)


def depth_map(img: np.ndarray, ctx, spec: dict | None = None) -> np.ndarray:
    """Relative depth 0 (near) .. 1 (far), full image resolution, cached."""
    key = ("ai", "depth", img.shape[:2])
    hit = _cache_get(ctx, key)
    if hit is not None:
        return hit
    disp = _display_base(img, ctx)
    raw = None
    if models.usable("depth-anything-v2-small"):
        try:
            raw = _depth_onnx(_small(disp, models.DEPTH_SIDE * 2))
            note(ctx, f"depth: Depth Anything V2 Small via onnxruntime ({_backend_name()})")
        except Exception as exc:   # noqa: BLE001
            note(ctx, f"depth: onnx model failed ({exc})")
    if raw is None and models.installed("depth-anything-v2-small-hf") and models.available("depth-anything-v2-small-hf")["deps_ok"]:
        try:
            raw = _depth_transformers(disp)
            note(ctx, f"depth: Depth Anything V2 Small via transformers ({models.torch_device()})")
        except Exception as exc:   # noqa: BLE001
            note(ctx, f"depth: transformers model failed ({exc})")
    if raw is not None:
        lo, hi = np.percentile(raw, 1), np.percentile(raw, 99)
        disparity = np.clip((raw - lo) / max(float(hi - lo), 1e-6), 0, 1)   # DA outputs inverse depth (near = large)
        d = 1.0 - disparity
        d = upsample_guided(d, disp, 16.0, 1e-2)
    else:
        note(ctx, "WARNING depth: no depth model installed (photo models download depth-anything-v2-small); "
                  "using defocus/vertical-position heuristic, results are only indicative")
        d = resize_mask(depth_heuristic(disp), disp.shape[:2])
    return _cache_put(ctx, key, d.astype(np.float32))
