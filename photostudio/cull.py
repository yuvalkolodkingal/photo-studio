"""Explainable culling: per-image analysis, burst / near-duplicate grouping, best-of-group ranking and flag
suggestions with human-readable REASONS. Warnings, not verdicts: nothing is deleted or moved unless the caller
explicitly asks (`apply_cull(..., move_rejects=True)` moves to ``_rejects/``, never deletes).

Signals (research notes A1-A5, thresholds are starting points to tune on real R50 files):
  * sharpness  variance of the Laplacian + Tenengrad on a fixed 1024 px long-edge preview (companion JPEG /
               embedded preview, never a raw render), plus the sharpest-tiles value (subject proxy), face crops and
               an optional subject mask; judged mainly *relative to the burst* (log ratio to the best frame)
  * blur type  gradient-structure-tensor anisotropy (motion) vs isotropic softness (defocus): an unvalidated heuristic
  * exposure   raw-mosaic clipping per CFA channel for raws (decode.raw_clipping), histogram for JPEGs
  * eyes       MediaPipe EAR (indices/thresholds from the LearnOpenCV article; 0.18 closed), Haar fallback
  * horizon    Hough lines, flag beyond 1.5 degrees;  noise: Immerkaer;  aesthetic: LAION head on CLIP (optional)
  * grouping   EXIF time gap <= 0.5 s (bursts) + 64-bit pHash/dHash Hamming <= 10 or CLIP cosine >= 0.9 (scenes)
"""
from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from .util import MissingDependency, resize_long

ANALYSIS_VERSION = 2
LONG_EDGE = 1024

EAR_LEFT = [362, 385, 387, 263, 373, 380]
EAR_RIGHT = [33, 160, 158, 133, 153, 144]

T = {  # thresholds; override per call with cull(..., thresholds={...})
    "ear_closed": 0.18, "ear_squint": 0.22, "ear_open": 0.28, "min_face_frac": 0.05, "max_yaw": 0.30,
    "tilt_deg": 1.5, "tilt_support": 0.3,
    "clip_hi_warn": 0.01, "clip_hi_fail": 0.08, "clip_lo_warn": 0.10, "median_under": 0.10, "median_over": 0.80,
    "median_under_fail": 0.04, "peak_blurry": 25.0, "peak_soft": 60.0, "rel_blurry": 0.25, "rel_soft": 0.5,
    "motion_coh": 0.55, "noise_warn": 7.0, "lap_ref": 600.0, "burst_gap_s": 0.5, "hash_dist": 10,
    "scene_window_s": 60.0, "clip_cos": 0.9, "pick_min": 60.0, "reject_max": 30.0, "dup_reject_gap": 12.0,
}

GENRE_ALIASES = {
    "portrait": "portrait", "people": "portrait", "wedding": "portrait", "event": "portrait", "family": "portrait",
    "wildlife": "wildlife", "animals": "wildlife", "animal": "wildlife", "bird": "wildlife", "birds": "wildlife", "pets": "wildlife",
    "landscape": "landscape", "seascape": "landscape", "architecture": "landscape", "cityscape": "landscape",
    "nature": "landscape", "travel": "general", "sports": "sports", "sport": "sports", "action": "sports",
    "macro": "macro", "night": "night", "astro": "night", "astrophotography": "night", "street": "general",
    "product": "general", "food": "general", "general": "general",
}
WEIGHTS = {  # portrait = the spec's 0.35 face / 0.25 eyes / 0.15 clip / 0.15 aesthetic / 0.10 global
    "general": {"global_sharp": .30, "subject_sharp": .15, "face_sharp": .10, "eyes_open": .10, "clip": .15, "expo": .05,
                "aesthetic": .10, "noise": .05},
    "portrait": {"face_sharp": .35, "eyes_open": .25, "clip": .15, "aesthetic": .15, "global_sharp": .10},
    "wildlife": {"subject_sharp": .45, "global_sharp": .10, "clip": .15, "expo": .05, "aesthetic": .15, "motion": .10},
    "landscape": {"global_sharp": .30, "clip": .20, "expo": .10, "horizon": .15, "aesthetic": .15, "noise": .10},
    "sports": {"subject_sharp": .35, "motion": .25, "global_sharp": .10, "clip": .10, "aesthetic": .10, "eyes_open": .05,
               "face_sharp": .05},
    "macro": {"subject_sharp": .50, "global_sharp": .10, "clip": .10, "noise": .10, "aesthetic": .20},
    "night": {"global_sharp": .30, "subject_sharp": .15, "noise": .25, "clip": .10, "aesthetic": .20},
}

_FACE_BACKENDS: list[Callable] = []   # injectable detectors (tests, custom models): fn(img_rgb01, gray255) -> [face dicts]


def register_face_backend(fn: Callable | None, first: bool = True) -> None:
    """Register (or with None, clear) a custom face/eye backend returning dicts with bbox [x0,y0,x1,y1] in the
    analysis image and optionally ear / ear_left / ear_right / trusted / score."""
    if fn is None:
        _FACE_BACKENDS.clear()
    elif first:
        _FACE_BACKENDS.insert(0, fn)
    else:
        _FACE_BACKENDS.append(fn)


# =========================================================================================== metrics
def to_gray255(img: np.ndarray) -> np.ndarray:
    return (img[..., 0] * 0.299 + img[..., 1] * 0.587 + img[..., 2] * 0.114).astype(np.float32) * 255.0


def lap_var(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_32F).var())


def sharpness_metrics(gray: np.ndarray, grid: int = 8) -> dict:
    """Variance of Laplacian, Tenengrad, and the sharpest-tiles value (subject proxy for shallow depth of field)."""
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    g2 = gx * gx + gy * gy
    ten = float(g2[g2 > 1024.0].sum() / g2.size)  # |grad| > 32 on the 0..255 Sobel scale, noise floor excluded
    h, w = gray.shape
    th, tw = max(1, h // grid), max(1, w // grid)
    tiles = []
    for r in range(grid):
        for c in range(grid):
            t = lap[r * th:(r + 1) * th, c * tw:(c + 1) * tw]
            if t.size:
                tiles.append(float(t.var()))
    tiles = np.sort(np.asarray(tiles))[::-1]
    k = max(3, int(round(len(tiles) * 0.05)))
    peak = float(tiles[:k].mean())
    return {"lap_var": float(lap.var()), "tenengrad": ten, "sharp_peak": peak,
            "focus_area": float((tiles >= 0.25 * peak).mean()) if peak > 0 else 0.0}


def motion_anisotropy(gray: np.ndarray) -> dict:
    """Structure-tensor coherence of strong gradients. Motion blur removes gradient energy along the motion
    direction so edges align (coherence -> 1, angle gives the blur direction); defocus keeps the scene's own
    orientation mix. UNVALIDATED heuristic: only used to label an image that is already judged soft."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    m = (gx * gx + gy * gy) > 400.0
    if m.sum() < 200:
        return {"motion_anisotropy": 0.0, "motion_angle_deg": None}
    jxx, jyy, jxy = float((gx[m] ** 2).sum()), float((gy[m] ** 2).sum()), float((gx[m] * gy[m]).sum())
    coh = math.sqrt((jxx - jyy) ** 2 + 4 * jxy ** 2) / max(jxx + jyy, 1e-9)
    grad_angle = 0.5 * math.degrees(math.atan2(2 * jxy, jxx - jyy))
    return {"motion_anisotropy": float(coh), "motion_angle_deg": float(((grad_angle + 90 + 90) % 180) - 90)}


def noise_sigma(gray: np.ndarray) -> float:
    """Immerkaer fast noise estimate (0..255 scale) on the analysis image."""
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    h, w = gray.shape
    if h < 4 or w < 4:
        return 0.0
    conv = cv2.filter2D(gray, cv2.CV_32F, k)[1:-1, 1:-1]
    return float(math.sqrt(math.pi / 2) / 6.0 * np.abs(conv).mean())


def horizon_tilt(gray: np.ndarray) -> dict:
    """Median angle of long near-horizontal Hough lines. tilt_deg > 0: the line descends to the right."""
    g = resize_long(gray, 640) if max(gray.shape) > 640 else gray
    h, w = g.shape
    edges = cv2.Canny(cv2.GaussianBlur(g.astype(np.uint8), (0, 0), 1.2), 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=max(40, w // 10), minLineLength=int(0.35 * w),
                            maxLineGap=max(4, int(0.02 * w)))
    if lines is None:
        return {"tilt_deg": None, "tilt_support": 0.0}
    angs, lens = [], []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        a = math.degrees(math.atan2(y2 - y1, x2 - x1))
        a = (a + 90) % 180 - 90
        if abs(a) <= 20:
            angs.append(a)
            lens.append(math.hypot(x2 - x1, y2 - y1))
    if not angs:
        return {"tilt_deg": None, "tilt_support": 0.0}
    angs, lens = np.asarray(angs), np.asarray(lens)
    o = np.argsort(angs)
    cum = np.cumsum(lens[o])
    med = float(angs[o][np.searchsorted(cum, cum[-1] / 2)])
    return {"tilt_deg": med, "tilt_support": float(lens.sum() / w)}


def exposure_stats(img: np.ndarray) -> dict:
    """Histogram stats of the display preview (JPEG-side; raw clipping is measured separately)."""
    lum = img[..., 0] * 0.2126 + img[..., 1] * 0.7152 + img[..., 2] * 0.0722
    p1, p50, p99 = (float(v) for v in np.percentile(lum, [1, 50, 99]))
    lin = lambda v: ((v + 0.055) / 1.055) ** 2.4 if v > 0.04045 else v / 12.92  # noqa: E731
    mx = img.max(axis=2)
    return {"mean_lum": float(lum.mean()), "median_lum": p50, "p1": p1, "p99": p99,
            "dr_p1_p99": float(math.log2((lin(p99) + 1e-4) / (lin(p1) + 1e-4))),
            "prev_clip_hi": float((mx >= 0.992).mean()), "prev_clip_lo": float((mx <= 0.01).mean())}


# =========================================================================================== faces / eyes
def eye_aspect_ratio(p: np.ndarray) -> float:
    """EAR = (|P2-P6| + |P3-P5|) / (2 |P1-P4|) for six landmarks in P1..P6 order (pixel coordinates)."""
    p = np.asarray(p, np.float64)
    d = lambda a, b: float(np.linalg.norm(p[a] - p[b]))  # noqa: E731
    return (d(1, 5) + d(2, 4)) / (2.0 * max(d(0, 3), 1e-9))


def face_from_landmarks(pts: np.ndarray, h: int) -> dict:
    """MediaPipe 468/478 pixel landmarks -> face dict with EAR (left/right/mean), bbox, yaw proxy, trust flag."""
    pts = np.asarray(pts, np.float64)
    el, er = eye_aspect_ratio(pts[EAR_LEFT]), eye_aspect_ratio(pts[EAR_RIGHT])
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    eye_w = pts[263][0] - pts[33][0]
    yaw = float((pts[1][0] - (pts[33][0] + pts[263][0]) / 2) / eye_w) if abs(eye_w) > 1e-6 else 1.0
    big = (y1 - y0) >= T["min_face_frac"] * h
    ex = lambda idx: [float(pts[idx][:, 0].min()), float(pts[idx][:, 1].min()),  # noqa: E731
                      float(pts[idx][:, 0].max()), float(pts[idx][:, 1].max())]
    return {"bbox": [float(x0), float(y0), float(x1), float(y1)], "ear_left": float(el), "ear_right": float(er),
            "ear": float((el + er) / 2), "yaw": yaw, "trusted": bool(big and abs(yaw) <= T["max_yaw"]),
            "eye_boxes": [ex(EAR_LEFT), ex(EAR_RIGHT)], "backend": "mediapipe"}


def _mediapipe_faces(img: np.ndarray, gray: np.ndarray) -> list[dict]:
    from . import library_ai

    mesh = library_ai.face_mesh()
    h, w = gray.shape
    res = mesh.process((np.clip(img, 0, 1) * 255).astype(np.uint8))
    out = []
    for lm in res.multi_face_landmarks or []:
        pts = np.array([[p.x * w, p.y * h] for p in lm.landmark], np.float64)
        out.append(face_from_landmarks(pts, h))
    return out


def _haar(name: str):
    cands = []
    try:
        from .models import installed, path

        key = {"haarcascade_frontalface_default.xml": "haar-face", "haarcascade_eye.xml": "haar-eye"}.get(name)
        if key and installed(key):
            cands.append(Path(path(key)))
    except Exception:
        pass
    try:
        cands.append(Path(cv2.data.haarcascades) / name)
    except Exception:
        pass
    for c in cands:
        if c.is_file() and c.stat().st_size > 1000:
            cc = cv2.CascadeClassifier(str(c))
            if not cc.empty():
                return cc
    return None


def _haar_faces(img: np.ndarray, gray: np.ndarray) -> list[dict]:
    fc = _haar("haarcascade_frontalface_default.xml")
    if fc is None:
        raise MissingDependency("no Haar cascade available (photo models download haar-face)")
    ec = _haar("haarcascade_eye.xml")
    g8 = cv2.equalizeHist(np.clip(gray, 0, 255).astype(np.uint8))
    h = g8.shape[0]
    out = []
    for (x, y, w, hh) in fc.detectMultiScale(g8, 1.1, 5, minSize=(int(T["min_face_frac"] * h),) * 2):
        f = {"bbox": [float(x), float(y), float(x + w), float(y + hh)], "trusted": False, "backend": "haar", "ear": None}
        if ec is not None:
            roi = g8[y:y + int(hh * 0.6), x:x + w]
            f["eyes_found"] = int(len(ec.detectMultiScale(roi, 1.1, 4, minSize=(max(8, w // 10),) * 2)))
        out.append(f)
    return out


def detect_faces(img: np.ndarray, gray: np.ndarray, backend: Callable | None = None) -> tuple[list[dict], str | None]:
    """First working backend wins: injected -> MediaPipe FaceMesh -> Haar. Returns (faces, backend name | None)."""
    chain: list[tuple[str, Callable]] = []
    if backend is not None:
        chain.append(("custom", backend))
    chain += [("custom", b) for b in _FACE_BACKENDS]
    chain += [("mediapipe", _mediapipe_faces), ("haar", _haar_faces)]
    for name, fn in chain:
        try:
            faces = fn(img, gray)
            for f in faces:
                f.setdefault("backend", name)
            return faces, name
        except MissingDependency:
            continue
        except Exception:
            continue
    return [], None


def _crop(gray: np.ndarray, bbox) -> np.ndarray | None:
    h, w = gray.shape
    x0, y0, x1, y1 = (int(round(v)) for v in bbox)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return gray[y0:y1, x0:x1]


# =========================================================================================== analyze
def _hashes(img: np.ndarray) -> dict:
    try:
        import imagehash
        from PIL import Image

        pil = Image.fromarray((np.clip(resize_long(img, 256), 0, 1) * 255 + 0.5).astype(np.uint8))
        return {"phash": str(imagehash.phash(pil, hash_size=8)), "dhash": str(imagehash.dhash(pil, hash_size=8))}
    except Exception:
        return {"phash": None, "dhash": None}


def analyze(path: str | Path, *, faces: bool = True, aesthetic: bool = False, subject_mask: np.ndarray | None = None,
            face_backend: Callable | None = None, long_edge: int = LONG_EDGE, clip_embedding: bool = False,
            thresholds: dict | None = None) -> dict:
    """Analyse one image (raw or JPEG). All metrics come from the preview (companion JPEG / embedded JPEG) at a
    fixed long edge, except clipping which is measured on the raw mosaic when `path` is a raw."""
    from . import decode

    t = {**T, **(thresholds or {})}
    path = Path(path)
    prev = decode.preview_display(path, long_edge=long_edge)
    if max(prev.shape[:2]) > long_edge:
        prev = resize_long(prev, long_edge)
    gray = to_gray255(prev)
    h, w = gray.shape
    a: dict[str, Any] = {"path": str(path), "analyzed_v": ANALYSIS_VERSION, "analysis_size": [w, h]}
    a.update(sharpness_metrics(gray))
    a.update(motion_anisotropy(gray))
    a["noise"] = noise_sigma(gray)
    a.update(horizon_tilt(gray))
    ex = exposure_stats(prev)
    a.update({k: ex[k] for k in ("mean_lum", "median_lum", "dr_p1_p99", "p1", "p99")})
    a["clip_hi"], a["clip_lo"], a["clip_source"] = ex["prev_clip_hi"], ex["prev_clip_lo"], "preview"
    if decode.is_raw(path):
        try:
            rc = decode.raw_clipping(path)
        except MissingDependency:
            rc = {"error": "rawpy missing"}
        if "error" not in rc:
            a["clip_hi"], a["clip_lo"], a["clip_source"] = rc["clip_hi"], rc["clip_lo"], "raw"
            a["clip_channels"] = {k: v["clip_hi"] for k, v in rc.items() if isinstance(v, dict)}
    a["over"] = bool(a["clip_hi"] >= t["clip_hi_warn"] or a["median_lum"] > t["median_over"])
    a["under"] = bool(a["median_lum"] < t["median_under"] and ex["p99"] < 0.5)
    a.update(_hashes(prev))
    # ---- faces / eyes / face sharpness
    a.update({"n_faces": 0, "eyes_closed": None, "ear_min": None, "sharp_face": None, "sharp_face_min": None,
              "face_ratio": None, "faces": [], "face_backend": None})
    if faces:
        fl, backend = detect_faces(prev, gray, face_backend)
        a["face_backend"] = backend
        keep = []
        for f in fl:
            c = _crop(gray, f["bbox"])
            f["sharp"] = lap_var(c) if c is not None else None
            f["height_frac"] = (f["bbox"][3] - f["bbox"][1]) / h
            keep.append(f)
        a["n_faces"] = len(keep)
        if keep:
            big = max(keep, key=lambda f: (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]))
            main = [f for f in keep if (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]) >=
                    0.4 * (big["bbox"][2] - big["bbox"][0]) * (big["bbox"][3] - big["bbox"][1])]
            ss = [f["sharp"] for f in main if f.get("sharp") is not None]
            if ss:
                a["sharp_face"] = float(big["sharp"] if big.get("sharp") is not None else max(ss))
                a["sharp_face_min"] = float(min(ss))
                a["face_ratio"] = float(a["sharp_face"] / max(a["lap_var"], 1e-6))
            ears = [f["ear"] for f in keep if f.get("ear") is not None and f.get("trusted", True)
                    and f["height_frac"] >= t["min_face_frac"]]
            if ears:
                a["ear_min"] = float(min(ears))
                a["eyes_closed"] = bool(min(ears) < t["ear_closed"])
            elif backend == "haar" and any(f.get("eyes_found") == 0 for f in keep if f["height_frac"] >= 0.08):
                a["eyes_unverified"] = True
        a["faces"] = [{k: v for k, v in f.items() if k != "eye_boxes"} for f in keep]
    # ---- subject sharpness (explicit mask)
    a["sharp_subject"] = None
    if subject_mask is not None:
        m = cv2.resize(np.asarray(subject_mask, np.float32), (w, h)) > 0.5
        if m.any():
            ys, xs = np.where(m)
            c = gray[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            if c.size > 64:
                a["sharp_subject"] = lap_var(c)
    # ---- optional aesthetic / CLIP embedding (lazy, never required)
    a["aesthetic"] = None
    if aesthetic or clip_embedding:
        try:
            from . import library_ai

            emb = library_ai.clip_model().encode_images([prev])
            a["clip_emb"] = emb[0]
            if aesthetic:
                a["aesthetic"] = float(library_ai.aesthetic_from_embeddings(emb)[0])
        except MissingDependency as exc:
            a["aesthetic_error"] = str(exc).split(".")[0]
    a["blur_type"] = blur_kind(a, t) if a["sharp_peak"] < t["peak_soft"] else "none"
    a["issues"] = absolute_issues(a, t)
    return a


def blur_kind(a: dict, t: dict | None = None) -> str:
    t = t or T
    return "motion" if (a.get("motion_anisotropy") or 0) >= t["motion_coh"] else "defocus"


def absolute_issues(a: dict, t: dict | None = None) -> list[str]:
    """Context-free issue codes (relative judgments are added by `cull`)."""
    t = t or T
    out = []
    if a["sharp_peak"] < t["peak_blurry"]:
        out.append("blurry")
    if a.get("eyes_closed"):
        out.append("eyes_closed")
    if a["clip_hi"] >= t["clip_hi_warn"] or a["median_lum"] > t["median_over"]:
        out.append("overexposed")
    if a["under"]:
        out.append("underexposed")
    if a.get("tilt_deg") is not None and abs(a["tilt_deg"]) > t["tilt_deg"] and a.get("tilt_support", 0) >= t["tilt_support"]:
        out.append("tilted")
    if a["noise"] > t["noise_warn"]:
        out.append("noisy")
    return out


# =========================================================================================== grouping
def _popcount64(x: np.ndarray) -> np.ndarray:
    return np.unpackbits(x.astype(">u8").view(np.uint8).reshape(-1, 8), axis=1).sum(1)


def _hex64(h) -> int | None:
    try:
        return int(h, 16) if h else None
    except (TypeError, ValueError):
        return None


def group_images(items: list[dict], time_gap: float | None = None, hash_dist: int | None = None,
                 scene_window: float | None = None, clip_cos: float | None = None,
                 embeddings: dict[int, np.ndarray] | None = None, thresholds: dict | None = None) -> list[dict]:
    """items[i] = {ts: epoch seconds | None, phash: hex, dhash: hex}. Returns per item {burst, scene} ids (None for
    singletons). Burst = consecutive frames <= time_gap apart; scene = union of bursts and frames within
    `scene_window` seconds whose pHash/dHash Hamming distance <= hash_dist (or CLIP cosine >= clip_cos)."""
    t = {**T, **(thresholds or {})}
    time_gap = t["burst_gap_s"] if time_gap is None else time_gap
    hash_dist = t["hash_dist"] if hash_dist is None else hash_dist
    scene_window = t["scene_window_s"] if scene_window is None else scene_window
    clip_cos = t["clip_cos"] if clip_cos is None else clip_cos
    n = len(items)
    ts = np.array([it.get("ts") if it.get("ts") is not None else np.nan for it in items], np.float64)
    order = sorted(range(n), key=lambda i: (np.isnan(ts[i]), ts[i] if not np.isnan(ts[i]) else i))
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # bursts: pure time clusters
    burst_of = [None] * n
    run: list[int] = []
    bursts: list[list[int]] = []
    for i in order:
        if run and not np.isnan(ts[i]) and not np.isnan(ts[run[-1]]) and ts[i] - ts[run[-1]] <= time_gap:
            run.append(i)
        else:
            if len(run) > 1:
                bursts.append(run)
            run = [i]
    if len(run) > 1:
        bursts.append(run)
    for bid, b in enumerate(bursts, start=1):
        for i in b:
            burst_of[i] = bid
        for x, y in zip(b, b[1:]):
            union(x, y)
    # near-duplicates: hash Hamming (and CLIP cosine) inside the scene window
    ph = [_hex64(it.get("phash")) for it in items]
    dh = [_hex64(it.get("dhash")) for it in items]
    pos = {idx: k for k, idx in enumerate(order)}
    ordered_ts = np.array([ts[i] for i in order])
    emb_ok = embeddings is not None and len(embeddings) > 0
    for k, i in enumerate(order):
        if np.isnan(ts[i]):
            hi = len(order)
        else:
            hi = int(np.searchsorted(ordered_ts[~np.isnan(ordered_ts)], ts[i] + scene_window, side="right"))
            hi = min(len(order), hi)
        cand = [order[j] for j in range(k + 1, hi)]
        if not cand:
            continue
        for arr, ok in ((ph, ph[i] is not None), (dh, dh[i] is not None)):
            if not ok:
                continue
            sel = [j for j in cand if arr[j] is not None]
            if not sel:
                continue
            xs = np.array([arr[i] ^ arr[j] for j in sel], dtype=np.uint64)
            d = _popcount64(xs)
            for j, dist in zip(sel, d):
                if dist <= hash_dist:
                    union(i, j)
        if emb_ok and i in embeddings:
            for j in cand:
                if j in embeddings and float(np.dot(embeddings[i], embeddings[j])) >= clip_cos:
                    union(i, j)
    comps: dict[int, list[int]] = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)
    scene_of = [None] * n
    sid = 0
    for _, members in sorted(comps.items(), key=lambda kv: min(kv[1])):
        if len(members) > 1:
            sid += 1
            for i in members:
                scene_of[i] = sid
    _ = pos
    return [{"burst": burst_of[i], "scene": scene_of[i]} for i in range(n)]


# =========================================================================================== scoring
def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def sharp_abs(v: float | None, t: dict) -> float | None:
    return None if v is None else _clamp(math.log10(1 + max(v, 0)) / math.log10(1 + t["lap_ref"]))


def sharp_rel(v: float | None, best: float | None) -> float | None:
    """1.0 at the best frame, 0 at >= 8x lower variance (log scale)."""
    if v is None or not best or best <= 0:
        return None
    return _clamp(1.0 - (math.log(best) - math.log(max(v, 1e-6))) / math.log(8.0))


def genre_weights(genre: str | None) -> tuple[str, dict]:
    g = GENRE_ALIASES.get((genre or "general").lower(), "general")
    w = dict(WEIGHTS[g])
    try:  # mode YAML may carry its own weights
        from .presets import load_preset

        p = load_preset("mode", genre)
        opt = (p.get("options") or {})
        cw = opt.get("cull_weights") or (opt.get("cull") or {}).get("weights") or p.get("cull_weights")
        if isinstance(cw, dict) and cw:
            w = {k: float(v) for k, v in cw.items()}
    except Exception:
        pass
    return g, w


def detect_genre(paths: list[str], metas: list[dict] | None = None) -> str:
    """Majority genre of up to 5 sample frames via modes.detect_mode (lazy; ENGINE-owned), else 'general'."""
    try:
        from .metadata import read_metadata
        from .modes import detect_mode
    except Exception:
        return "general"
    if not paths:
        return "general"
    idx = sorted({0, len(paths) // 4, len(paths) // 2, 3 * len(paths) // 4, len(paths) - 1})
    votes: dict[str, int] = {}
    for i in idx:
        try:
            md = metas[i] if metas and metas[i] else read_metadata(paths[i])
            m = detect_mode(md).get("mode")
            if m:
                votes[m] = votes.get(m, 0) + 1
        except Exception:
            continue
    return max(votes, key=votes.get) if votes else "general"


def _features(a: dict, grp: list[dict], t: dict) -> dict[str, float | None]:
    best = lambda k: max((x[k] for x in grp if x.get(k) is not None), default=None)  # noqa: E731
    solo = len(grp) < 2

    def comb(key: str):
        v = a.get(key)
        if v is None:
            return None
        ab = sharp_abs(v, t)
        rel = None if solo else sharp_rel(v, best(key))
        return ab if rel is None else 0.6 * rel + 0.4 * ab

    f: dict[str, float | None] = {"global_sharp": comb("lap_var"), "face_sharp": comb("sharp_face"),
                                  "subject_sharp": comb("sharp_subject") if a.get("sharp_subject") is not None else comb("sharp_peak")}
    ear = a.get("ear_min")
    f["eyes_open"] = None if ear is None else _clamp((ear - 0.10) / (t["ear_open"] - 0.10))
    f["clip"] = 1.0 - _clamp(a["clip_hi"] / 0.05) * 0.8 - _clamp(a["clip_lo"] / 0.25) * 0.2
    med = a["median_lum"]
    f["expo"] = 1.0 - max(_clamp((0.2 - med) / 0.2), _clamp((med - 0.7) / 0.3))
    aes = a.get("aesthetic")
    if aes is None:
        f["aesthetic"] = None
    else:
        ab = _clamp((aes - 1) / 9.0)
        mx = max((x["aesthetic"] for x in grp if x.get("aesthetic") is not None), default=aes)
        f["aesthetic"] = ab if solo else 0.5 * ab + 0.5 * _clamp(1 - (mx - aes) / 2.0)
    td = a.get("tilt_deg")
    f["horizon"] = None if td is None or a.get("tilt_support", 0) < t["tilt_support"] else _clamp(1 - abs(td) / 5.0)
    soft = (f["global_sharp"] or 1.0) < 0.6
    f["motion"] = 0.2 if (soft and (a.get("motion_anisotropy") or 0) >= t["motion_coh"]) else 1.0
    f["noise"] = 1.0 - _clamp((a["noise"] - 2.0) / 10.0)
    return f


def _score(feat: dict, weights: dict) -> float:
    num = den = 0.0
    for k, w in weights.items():
        v = feat.get(k)
        if v is None or w <= 0:
            continue
        num += w * v
        den += w
    return 100.0 * num / den if den > 0 else 50.0


def _read_times(paths: list[str], metas: list[dict] | None) -> list[float | None]:
    from datetime import datetime

    from .metadata import read_metadata

    out: list[float | None] = []
    for i, p in enumerate(paths):
        md = metas[i] if metas and metas[i] else None
        try:
            if md is None:
                md = read_metadata(p)
            if md.get("ts") is not None:
                out.append(float(md["ts"]))
                continue
            dt = md.get("datetime")
            if dt:
                sub = md.get("subsec")
                s = float("0." + "".join(c for c in str(sub) if c.isdigit())) if sub not in (None, "") else 0.0
                out.append(datetime.fromisoformat(dt).timestamp() + s)
                continue
        except Exception:
            pass
        try:
            out.append(Path(p).stat().st_mtime)
        except OSError:
            out.append(None)
    return out


def cull(paths: list[str | Path], target: float | None = None, genre: str | None = None, *,
         analyses: dict[str, dict] | None = None, metas: list[dict] | None = None, group: bool = True,
         aesthetic: bool = False, thresholds: dict | None = None, embeddings: dict[int, np.ndarray] | None = None,
         progress: Callable[[int, int], None] | None = None, force: bool = False, **analyze_kw) -> list[dict]:
    """Analyse, group and rank `paths`. Returns one dict per input (same order) with: score (0..100), flag (the
    SUGGESTED flag: pick | maybe | reject), reasons (human-readable warnings), issues (codes), group info, the
    features behind the score and a trimmed analysis. `target` (0<t<=1 or percent) caps picks to that share."""
    t = {**T, **(thresholds or {})}
    paths = [str(p) for p in paths]
    n = len(paths)
    if target is not None:
        target = float(target) / (100.0 if target > 1 else 1.0)
        if not 0 < target <= 1:
            raise ValueError("target must be in (0, 1] or (1, 100] percent")
    ans: list[dict] = []
    for i, p in enumerate(paths):
        a = (analyses or {}).get(p)
        if a is None or force or a.get("analyzed_v") != ANALYSIS_VERSION or (aesthetic and a.get("aesthetic") is None
                                                                             and "aesthetic_error" not in a):
            a = analyze(p, aesthetic=aesthetic, thresholds=thresholds, **analyze_kw)
            if analyses is not None:
                analyses[p] = a
        ans.append(a)
        if progress:
            progress(i + 1, n)
    g_name, weights = genre_weights(genre if genre else detect_genre(paths, metas))
    # ---- grouping
    gi = [{"burst": None, "scene": None} for _ in paths]
    if group and n > 1:
        ts = _read_times(paths, metas)
        gi = group_images([{"ts": ts[i], "phash": ans[i].get("phash"), "dhash": ans[i].get("dhash")} for i in range(n)],
                          embeddings=embeddings, thresholds=thresholds)
    members: dict[int, list[int]] = {}
    for i, g in enumerate(gi):
        if g["scene"] is not None:
            members.setdefault(g["scene"], []).append(i)
    # ---- features, scores, per-image reasons
    res: list[dict] = []
    for i, a in enumerate(ans):
        grp_idx = members.get(gi[i]["scene"], [i]) if gi[i]["scene"] is not None else [i]
        grp = [ans[j] for j in grp_idx]
        feat = _features(a, grp, t)
        res.append({"path": paths[i], "name": Path(paths[i]).name, "score": round(_score(feat, weights), 1), "flag": "maybe",
                    "reasons": [], "issues": list(a.get("issues", [])), "severity": {}, "group": gi[i]["scene"],
                    "burst": gi[i]["burst"], "group_size": len(grp_idx), "group_rank": 1, "best_of_group": gi[i]["scene"] is None,
                    "genre": g_name, "features": {k: (None if v is None else round(float(v), 3)) for k, v in feat.items()},
                    "analysis": _brief(a)})
    for sid, idxs in members.items():
        order = sorted(idxs, key=lambda k: (-res[k]["score"], k))
        for rank, k in enumerate(order, start=1):
            res[k]["group_rank"] = rank
            res[k]["best_of_group"] = rank == 1
    for i, r in enumerate(res):
        _explain(r, ans[i], [ans[j] for j in members.get(gi[i]["scene"], [])] or [ans[i]],
                 [res[j] for j in members.get(gi[i]["scene"], [])], paths, t)
    _suggest(res, target, t)
    return res


def _brief(a: dict) -> dict:
    keys = ("lap_var", "tenengrad", "sharp_peak", "sharp_face", "sharp_subject", "face_ratio", "n_faces", "ear_min",
            "eyes_closed", "clip_hi", "clip_lo", "clip_source", "median_lum", "tilt_deg", "noise", "aesthetic", "blur_type",
            "motion_anisotropy", "face_backend")
    return {k: (round(a[k], 4) if isinstance(a.get(k), float) else a.get(k)) for k in keys if k in a}


def _explain(r: dict, a: dict, grp: list[dict], grp_res: list[dict], paths: list[str], t: dict) -> None:
    reasons, issues, sev = r["reasons"], [], r["severity"]
    best_lap = max(x["lap_var"] for x in grp)
    solo = len(grp) < 2
    ratio = math.sqrt(a["lap_var"] / best_lap) if best_lap > 0 else 1.0   # std-dev ratio, 1.0 = as sharp as the best
    soft_abs = a["sharp_peak"] < t["peak_soft"]
    if a["sharp_peak"] < t["peak_blurry"] or (not solo and ratio < t["rel_blurry"]):
        sev["blurry"] = "fail"
    elif (not solo and ratio < t["rel_soft"]) or soft_abs:
        sev["blurry"] = "warn"
    if "blurry" in sev:
        kind = blur_kind(a, t)
        if not solo and ratio < 1:
            msg = (f"{'blurry' if sev['blurry'] == 'fail' else 'softer'}: sharpness {ratio * 100:.0f}% of the sharpest frame "
                   f"in its group (Laplacian variance {a['lap_var']:.0f} vs {best_lap:.0f})")
        else:
            msg = f"soft: no sharp region found (sharpest-tile Laplacian variance {a['sharp_peak']:.0f})"
        ang = a.get("motion_angle_deg")
        reasons.append(msg + (f"; looks like motion blur around {ang:.0f} deg (heuristic)" if kind == "motion" and ang is not None
                              else "; looks like defocus / soft focus (heuristic)"))
    elif a.get("sharp_face") is not None and not solo:
        bf = max((x["sharp_face"] for x in grp if x.get("sharp_face") is not None), default=0)
        if bf > 0 and math.sqrt(a["sharp_face"] / bf) < t["rel_soft"]:
            sev["face_soft"] = "warn"
            reasons.append(f"face softer than the best frame in its group ({100 * math.sqrt(a['sharp_face'] / bf):.0f}% of its face sharpness)")
    if a.get("face_ratio") is not None and a["face_ratio"] < 0.2 and a["sharp_peak"] > t["peak_soft"] * 4 and a["n_faces"]:
        sev["focus_miss"] = "info"
        reasons.append(f"face is much softer than the rest of the frame (ratio {a['face_ratio']:.2f}): focus may have missed the subject")
    if a.get("eyes_closed"):
        sev["eyes_closed"] = "fail"
        reasons.append(f"eyes closed (EAR {a['ear_min']:.2f} < {t['ear_closed']:.2f})")
    elif a.get("ear_min") is not None and a["ear_min"] < t["ear_squint"]:
        sev["squint"] = "warn"
        issues.append("squint")
        reasons.append(f"eyes half closed or squinting (EAR {a['ear_min']:.2f})")
    if a.get("eyes_unverified"):
        sev["eyes_unverified"] = "info"
        reasons.append("no eyes found in a large face (Haar fallback, low confidence; install mediapipe for real blink detection)")
    src = "raw pixels" if a.get("clip_source") == "raw" else "pixels"
    if a["clip_hi"] >= t["clip_hi_fail"]:
        sev["overexposed"] = "fail"
    elif a["clip_hi"] >= t["clip_hi_warn"] or a["median_lum"] > t["median_over"]:
        sev["overexposed"] = "warn"
    if "overexposed" in sev:
        ch = a.get("clip_channels")
        worst = f" (worst channel {max(ch, key=ch.get)} {100 * max(ch.values()):.1f}%)" if ch else ""
        reasons.append(f"highlights clipped: {100 * a['clip_hi']:.1f}% of {src}{worst}" if a["clip_hi"] >= t["clip_hi_warn"]
                       else f"overexposed (median luma {a['median_lum']:.2f})")
    if a["under"]:
        sev["underexposed"] = "fail" if a["median_lum"] < t["median_under_fail"] else "warn"
        reasons.append(f"underexposed (median luma {a['median_lum']:.2f})")
    if a["clip_lo"] >= t["clip_lo_warn"] and a.get("clip_source") == "raw":
        sev["crushed"] = "warn"
        reasons.append(f"shadows at the black level: {100 * a['clip_lo']:.0f}% of raw pixels")
    if a.get("tilt_deg") is not None and abs(a["tilt_deg"]) > t["tilt_deg"] and a.get("tilt_support", 0) >= t["tilt_support"]:
        sev["tilted"] = "info"
        reasons.append(f"horizon tilted {a['tilt_deg']:+.1f} deg (possible; fix with the geometry op)")
    if a["noise"] > t["noise_warn"]:
        sev["noisy"] = "info"
        reasons.append(f"noisy (sigma {a['noise']:.1f} on the 1024 px preview)")
    if len(grp_res) > 1:
        best = next(x for x in grp_res if x["group_rank"] == 1)
        if r["group_rank"] == 1:
            reasons.insert(0, f"best of {len(grp_res)} similar frames (score {r['score']:.0f})")
        else:
            reasons.insert(0, f"near-duplicate: ranked {r['group_rank']}/{len(grp_res)} in its group; "
                              f"{Path(best['path']).name} scores {best['score']:.0f} vs {r['score']:.0f}")
    for code in sev:
        if code in ("blurry", "eyes_closed", "overexposed", "underexposed", "tilted", "noisy") and code not in issues:
            issues.append(code)
    r["issues"] = issues
    r["_dup_gap"] = (max(x["score"] for x in grp_res) - r["score"]) if len(grp_res) > 1 else 0.0


def _suggest(res: list[dict], target: float | None, t: dict) -> None:
    n = len(res)
    for r in res:
        fail = [c for c, s in r["severity"].items() if s == "fail"]
        # a closed-eye frame is only a reject when a better frame exists; alone it is a warning
        if fail == ["eyes_closed"] and r["group_size"] < 2:
            fail = []
        r["_fail"] = bool(fail)
    if target is None:
        for r in res:
            if r["_fail"]:
                r["flag"] = "reject"
            elif r["group"] is not None and not r["best_of_group"]:
                r["flag"] = "reject" if r["_dup_gap"] >= t["dup_reject_gap"] else "maybe"
            elif r["score"] >= t["pick_min"]:
                r["flag"] = "pick"
            elif r["score"] < t["reject_max"]:
                r["flag"] = "reject"
            else:
                r["flag"] = "maybe"
    else:
        k = max(1, int(round(n * target)))
        pool = [i for i, r in enumerate(res) if not r["_fail"]]
        pool.sort(key=lambda i: (not res[i]["best_of_group"], -res[i]["score"]))
        picks = set(pool[:k])
        for i, r in enumerate(res):
            if i in picks:
                r["flag"] = "pick"
                r["reasons"].append(f"selected for the {target * 100:.0f}% target (score {r['score']:.0f})")
            elif r["_fail"]:
                r["flag"] = "reject"
            elif r["group"] is not None and not r["best_of_group"] and r["_dup_gap"] >= t["dup_reject_gap"]:
                r["flag"] = "reject"
            else:
                r["flag"] = "maybe" if r["score"] >= t["reject_max"] else "reject"
    for r in res:
        r.pop("_fail", None)
        r.pop("_dup_gap", None)
        if r["flag"] == "reject" and not r["reasons"]:
            r["reasons"].append(f"low overall score ({r['score']:.0f})")
        if r["flag"] == "pick" and not r["reasons"]:
            r["reasons"].append(f"good overall score ({r['score']:.0f}), no warnings")


def summarize(results: list[dict]) -> dict:
    c = {"pick": 0, "maybe": 0, "reject": 0}
    for r in results:
        c[r["flag"]] += 1
    groups = {r["group"] for r in results if r["group"] is not None}
    return {"images": len(results), **c, "groups": len(groups),
            "issues": {k: sum(1 for r in results if k in r["issues"]) for k in
                       ("blurry", "eyes_closed", "overexposed", "underexposed", "tilted", "noisy")}}


# =========================================================================================== catalog glue
def _src(im: dict) -> str:
    """The file whose pixels/clipping we analyse: the raw if present (clipping), else the JPEG."""
    return im.get("raw") or im["path"]


def analyze_catalog(cat, ids: list[int] | None = None, force: bool = False, aesthetic: bool = False,
                    progress: Callable[[int, int], None] | None = None, **kw) -> dict:
    """Analyse images (cached in the catalog; stale/old-version entries recomputed). Returns a summary."""
    ids = ids if ids is not None else cat.ids()
    done = skipped = failed = 0
    errs = []
    for k, i in enumerate(ids):
        im = cat.get(i)
        cached = None if force else cat.get_analysis(i, fresh_only=True)
        if cached and cached.get("analyzed_v") == ANALYSIS_VERSION and not (aesthetic and cached.get("aesthetic") is None):
            skipped += 1
        else:
            try:
                a = analyze(_src(im), aesthetic=aesthetic, **kw)
                cat.set_analysis(i, a, clip_emb=a.pop("clip_emb", None))
                done += 1
            except Exception as exc:
                failed += 1
                errs.append(f"{im['filename']}: {type(exc).__name__}: {exc}")
        if progress:
            progress(k + 1, len(ids))
    return {"analyzed": done, "cached": skipped, "failed": failed, "errors": errs[:20]}


def group_catalog(cat, ids: list[int] | None = None, store: bool = True, **kw) -> dict:
    ids = ids if ids is not None else cat.ids()
    rows = [cat.get(i) for i in ids]
    items = []
    for r in rows:
        a = r["analysis"] or {}
        items.append({"ts": r["taken_ts"], "phash": a.get("phash"), "dhash": a.get("dhash")})
    emb = cat.get_embeddings(ids)
    embs = {k: emb[i] for k, i in enumerate(ids) if i in emb} or None
    gi = group_images(items, embeddings=embs, **kw)
    if store:
        cat.set_groups({ids[k]: {"burst_id": g["burst"], "scene_id": g["scene"]} for k, g in enumerate(gi)})
    scenes: dict[int, list[int]] = {}
    for k, g in enumerate(gi):
        if g["scene"] is not None:
            scenes.setdefault(g["scene"], []).append(ids[k])
    missing = sum(1 for it in items if it["phash"] is None)
    return {"images": len(ids), "bursts": len({g["burst"] for g in gi if g["burst"] is not None}), "scenes": len(scenes),
            "grouped_images": sum(len(v) for v in scenes.values()), "without_hashes": missing,
            "groups": [{"scene": s, "images": v} for s, v in scenes.items()]}


def cull_catalog(cat, ids: list[int] | None = None, target: float | None = None, genre: str | None = None,
                 aesthetic: bool = False, progress=None, store_groups: bool = True, **kw) -> list[dict]:
    """Cull catalog images using cached analyses; results carry the catalog image id and are returned in `ids` order."""
    ids = ids if ids is not None else cat.ids()
    rows = [cat.get(i) for i in ids]
    paths = [_src(r) for r in rows]
    cache = {}
    for r, p in zip(rows, paths):
        a = cat.get_analysis(r["id"], fresh_only=True)
        if a and a.get("analyzed_v") == ANALYSIS_VERSION:
            cache[p] = a
    before = set(cache)
    metas = [{"ts": r["taken_ts"], "datetime": r["taken_at"]} for r in rows]
    emb = cat.get_embeddings(ids)
    embs = {k: emb[i] for k, i in enumerate(ids) if i in emb} or None
    gname = genre or next((r["genre"] or r["mode"] for r in rows if r["genre"] or r["mode"]), None)
    res = cull(paths, target=target, genre=gname, analyses=cache, metas=metas, aesthetic=aesthetic, embeddings=embs,
               progress=progress, **kw)
    for r, p, out in zip(rows, paths, res):
        out["id"] = r["id"]
        out["rel"] = r["primary_path"]
        if p not in before and p in cache:
            a = cache[p]
            cat.set_analysis(r["id"], a, clip_emb=a.pop("clip_emb", None))
    if store_groups:
        cat.set_groups({r["id"]: {"burst_id": o["burst"], "scene_id": o["group"],
                                  "burst_rank": o["group_rank"] if o["group"] is not None else None}
                        for r, o in zip(rows, res)})
    return res


def apply_cull(cat, results: list[dict], flags: bool = True, rate: bool = False, label_maybe: str | None = None,
               clear_maybe: bool = False, move_rejects: bool = False, rejects_dir: str = "_rejects",
               dry_run: bool = False, overwrite: bool = False) -> dict:
    """Write the suggested flags (and optionally ratings / a label for 'maybe') to sidecar + XMP. Only runs when
    asked for. `move_rejects` moves rejected files (raw, JPEG, sidecars) under `<root>/_rejects/`, never deleting.

    A flag a person set (sidecar ``flag_source`` "user", or no source recorded) is never replaced unless ``overwrite``;
    those images are listed in ``kept_user_flags`` and are not moved either. Flags written by an earlier cull
    (``flag_source`` "cull") are refreshed."""
    by = {"pick": [], "reject": [], "maybe": []}
    for r in results:
        by[r["flag"]].append(r.get("id") if r.get("id") is not None else r["path"])
    kept: list = []
    if not overwrite:
        for k in by:
            keep_ids = []
            for t in by[k]:
                src = cat.flag_source(t)
                if src not in (None, "cull"):
                    kept.append(t)
                else:
                    keep_ids.append(t)
            by[k] = keep_ids
    rep: dict[str, Any] = {"dry_run": dry_run, "counts": {k: len(v) for k, v in by.items()}, "moved": [],
                           "kept_user_flags": len(kept)}
    if kept:
        rep["kept_user_flags_ids"] = kept[:200]
        rep["note"] = (f"{len(kept)} image(s) already carry a flag you set and were left alone; "
                       "use --overwrite to replace manual flags")
    if dry_run:
        return rep
    if flags:
        if by["pick"]:
            cat.set_flag(by["pick"], "pick", source="cull")
        if by["reject"]:
            cat.set_flag(by["reject"], "reject", source="cull")
        if clear_maybe and by["maybe"]:
            cat.set_flag(by["maybe"], None, source="cull")
    if label_maybe and by["maybe"]:
        cat.set_label(by["maybe"], label_maybe)
    if rate:
        for r in results:
            if r.get("id") is None or cat.get(r["id"])["rating"]:
                continue
            s = r["score"]
            cat.set_rating(r["id"], 5 if s >= 80 else 4 if s >= 65 else 3 if s >= 50 else 2 if s >= 30 else 1)
    if move_rejects and by["reject"]:
        for i in by["reject"]:
            im = cat.get(i)
            src_dir = Path(im["path"]).parent
            dest_dir = cat.root / rejects_dir / (im["dir"] or "")
            dest_dir.mkdir(parents=True, exist_ok=True)
            names = [Path(x).name for x in (im.get("raw_path"), im.get("jpg_path")) if x]
            for nm in dict.fromkeys(names):
                for suffix in ("", ".ps.json", ".ps.json.bak", ".xmp"):
                    f = src_dir / (nm + suffix)
                    t = dest_dir / f.name
                    if f.exists() and not t.exists():
                        shutil.move(str(f), str(t))
                        rep["moved"].append(str(t))
        rep["rescan"] = cat.scan()
    return rep
