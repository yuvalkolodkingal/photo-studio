"""Registration and integration of untracked night-sky frames (Sequator / DSS / Siril style, tuned for a fixed tripod
or a hand-rested camera).

Pipeline
    1. load + calibrate every frame in parallel (master dark / bias / flat, optional lensfun distortion correction),
       keep a small background-subtracted luminance preview, the star list and a noise estimate
    2. frame-to-frame motion by phase correlation of the star previews (foreground masked) -> sky drift, camera
       nudges (jumps away from the steady drift) and "moving" segments that are dropped
    3. registration to a reference frame, chosen automatically:
         stars  >= ~12 usable stars: triangle / vote star matching (astroalign when installed), similarity, then a
                RANSAC homography from all matched stars (absorbs wide-field projection and residual distortion)
         ecc    star-poor city skies (one or two bright stars): frames are grouped into short translation-aligned
                mini-stacks inside each steady segment (raises SNR so faint stars appear), each mini-stack is
                registered with an ECC homography against the reference mini-stack (multi-start, initialised by a
                global phase correlation so it survives nudges), and the frame transform is the composition
    4. integration in two streaming passes over all frames, run on a process pool with a memory budget:
         pass 1 weighted mean and variance per pixel (and the unaligned ground mean for "freeze ground");
         pass 2 kappa-sigma rejection or winsorization against pass-1 statistics, weights 1/noise^2, plus two
         independent half stacks (alternate frames) for split-half validation of faint detections.
       Warped frames are cached on disk as float16 between the passes when there is room, so each raw file is
       decoded once.
    5. crop to the area covered by (nearly) all frames.

Everything works on float32 linear data; the result is linear (finish with `photostudio.astro.post`).
"""
from __future__ import annotations

import math
import multiprocessing as mp
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import cv2
import numpy as np

from .. import multiframe as mf

FITS_EXTS = {".fit", ".fits", ".fts"}


class Cancelled(RuntimeError):
    pass


# --------------------------------------------------------------------------- options / result
@dataclass
class StackOptions:
    align: str = "auto"              # auto | stars | ecc | scene | none   (scene = align on the landscape/city)
    reject: str = "sigma"            # sigma | winsor | mean | max
    kappa: float = 2.5
    weights: str = "noise"           # noise (1/sigma^2) | equal
    keep: float = 1.0                # fraction of frames kept (best star count / noise)
    group: int = 8                   # mini-stack size for ecc registration
    reference: int | None = None     # index of the base frame (default: middle of the longest steady segment)
    lens: bool = False               # lensfun distortion / vignetting correction before registration
    darks: Sequence = ()
    flats: Sequence = ()
    bias: Sequence = ()
    freeze_ground: object = None     # None | 'auto' (AI sky mask) | mask spec dict | HxW array (1 = sky)
    horizon: float | None = None     # alternative to freeze_ground: bottom fraction of the frame that is ground
    halves: bool = True              # also integrate two independent half stacks
    nudge_px: float = 8.0            # minimum jump (full-res px beyond the drift) that counts as a nudge
    min_segment: int = 3             # steady segments shorter than this are treated as "camera moving"
    crop: bool = True
    jobs: int = 0                    # 0 = auto (cores - 2, limited by memory)
    mem_gb: float = 0.0              # 0 = 60 % of available RAM
    cache: str = "auto"              # auto | on | off  (float16 warped-frame cache between passes)
    workdir: str | None = None       # scratch space for accumulators and the cache (default: system temp)
    draft: bool = False              # half-size raw decode (4x faster, for previews)
    work_side: int = 2000            # long edge of the registration previews
    homography: bool = True          # allow homography refinement (wide-field projection / residual distortion)
    min_cover: float = 0.9           # crop to where at least this fraction of the frames contributes


@dataclass
class StackResult:
    image: np.ndarray                # HxWx3 float32 linear
    half_a: np.ndarray | None = None
    half_b: np.ndarray | None = None
    coverage: np.ndarray | None = None   # HxW frames contributing
    info: dict = field(default_factory=dict)


ProgressFn = Callable[[str, float, str], None]   # (stage, fraction 0..1, message)


# --------------------------------------------------------------------------- frame loading (runs in workers)
def load_any(path, draft: bool = False) -> tuple[np.ndarray, dict]:
    """Linear float32 HxWx3 + meta for raw, TIFF/JPEG/PNG, FITS (Siril / DSS output, bottom-up) or arrays."""
    if isinstance(path, (str, Path)) and Path(path).suffix.lower() in FITS_EXTS:
        from astropy.io import fits   # optional; only needed for FITS input

        d = np.asarray(fits.getdata(str(path)))
        if np.issubdtype(d.dtype, np.integer):
            d = d.astype(np.float32) / float(np.iinfo(d.dtype).max)
        d = d.astype(np.float32)
        if d.ndim == 3 and d.shape[0] in (1, 3):
            d = np.moveaxis(d, 0, -1)
        if d.ndim == 2:
            d = d[..., None]
        if d.shape[2] == 1:
            d = np.repeat(d, 3, 2)
        return np.ascontiguousarray(d[::-1]), {}
    if draft and isinstance(path, (str, Path)):
        from .. import decode as dec

        if Path(path).suffix.lower() in dec.RAW_EXTS:
            d = dec.decode_raw(path, half_size=True, highlight="clip")
            return d.image, dict(d.meta or {})
    return mf.load_frame(path)


_G: dict = {}   # per-process globals (masters, options), set by _init


def _init(g: dict) -> None:
    global _G
    _G = g
    cv2.setNumThreads(1)


def _lens_fix(img: np.ndarray, meta: dict) -> tuple[np.ndarray, str]:
    try:
        from ..pipeline import registry
        from ..pipeline.registry import Ctx

        spec = registry.get("lens_correct")
        ctx = Ctx(domain="linear", kind="raw", meta=meta)
        out = spec.fn(img, spec.resolve_params({}), ctx)
        return out.astype(np.float32, copy=False), "; ".join(ctx.log)
    except Exception as exc:  # noqa: BLE001
        return img, f"lens correction skipped ({exc})"


def _calibrated(path) -> tuple[np.ndarray, dict]:
    img, meta = load_any(path, _G.get("draft", False))
    img = mf._calibrate(img, _G.get("dark"), _G.get("flat"), _G.get("bias"))
    if _G.get("lens"):
        img, note = _lens_fix(img, meta)
        meta = {**meta, "_lens_note": note}
    return img, meta


def star_preview(img: np.ndarray, scale: float, ground: np.ndarray | None = None) -> np.ndarray:
    """Small float32 luminance with the sky background and extended structure removed (white top-hat), hot pixels
    suppressed and the ground masked out: what registration looks at."""
    lum = np.maximum(img[..., 0] * 0.2627 + img[..., 1] * 0.6780 + img[..., 2] * 0.0593, 0).astype(np.float32)
    lum = cv2.medianBlur(lum, 3)
    h, w = lum.shape
    small = cv2.resize(lum, (max(8, int(round(w * scale))), max(8, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
    bg = cv2.morphologyEx(small, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    bg = cv2.GaussianBlur(bg, (0, 0), 6)
    p = small - bg
    if ground is not None:
        p = p * ground
    return p.astype(np.float32)


def _noise(p: np.ndarray) -> float:
    v = p[::2, ::2]
    return float(1.4826 * np.median(np.abs(v - np.median(v))) + 1e-12)


def _pass0(i: int):
    """Load one frame -> (i, preview, stars (full px), noise, meta-lite)."""
    img, meta = _calibrated(_G["files"][i])
    s = _G["scale"] if _G.get("scale") else min(1.0, _G["work_side"] / max(img.shape[:2]))
    gm = _G.get("ground_small")
    prev = star_preview(img, s, gm)
    stars = mf.detect_stars(img if gm is None else img * cv2.resize(gm, img.shape[1::-1])[..., None], max_stars=200)
    lite = {k: meta[k] for k in ("shutter", "iso", "aperture", "focal", "datetime", "_lens_note") if k in meta}
    nz = _noise(prev)
    # keep only reliable stars (>= 12 sigma in the preview): noise peaks must not drive registration decisions
    sm = cv2.GaussianBlur(prev, (0, 0), 1.0)
    h, w = sm.shape
    if len(stars):
        xi = np.clip((stars[:, 0] * s).round().astype(int), 0, w - 1)
        yi = np.clip((stars[:, 1] * s).round().astype(int), 0, h - 1)
        stars = stars[cv2.dilate(sm, np.ones((3, 3), np.uint8))[yi, xi] > 12 * nz]
    gray = None
    if _G.get("want_gray"):              # scene registration: a tone-mapped luminance preview for feature matching
        lum = np.maximum(img.mean(2), 0)
        lum = cv2.resize(lum, (prev.shape[1], prev.shape[0]), interpolation=cv2.INTER_AREA)
        hi = float(np.percentile(lum, 99.5)) or 1.0
        gray = (np.clip(np.log1p(200 * lum / hi) / np.log1p(200), 0, 1) * 255).astype(np.uint8)
    return i, prev, stars, nz, img.shape[:2], s, {**lite, "_gray": gray}


# --------------------------------------------------------------------------- motion, nudges, segments
def _for_ecc(p: np.ndarray) -> np.ndarray:
    q = np.clip(p - 2.0 * _noise(p), 0, None)       # drop the noise floor: correlate stars, not noise
    hi = float(np.percentile(q, 99.97)) or 1.0
    q = np.clip(q / hi, 0, 1)
    return cv2.GaussianBlur(q, (0, 0), 1.5).astype(np.float32)


def _phase(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, float]:
    """Shift of b relative to a (content of a moved by d appears in b), and the peak response."""
    win = cv2.createHanningWindow(a.shape[::-1], cv2.CV_32F)
    (dx, dy), r = cv2.phaseCorrelate(_for_ecc(a), _for_ecc(b), win)
    return np.array([dx, dy], np.float64), float(r)


def track_bright(previews: Sequence[np.ndarray], min_snr: float = 15.0) -> tuple[np.ndarray, np.ndarray] | None:
    """Follow the brightest star through the sequence (local window around the predicted position, global re-acquire
    after a nudge, flux consistency check). Returns (positions (n,2) preview px, ok mask) or None when no single star
    dominates the field (then phase correlation is used)."""
    n = len(previews)
    pos = np.zeros((n, 2))
    ok = np.zeros(n, bool)
    sm0 = cv2.GaussianBlur(previews[0], (0, 0), 1.2)
    noise = _noise(previews[0])
    y, x = np.unravel_index(int(np.argmax(sm0)), sm0.shape)
    flux0 = float(sm0[y, x])
    if flux0 < min_snr * noise:
        return None
    second = sm0.copy()
    second[max(0, y - 10):y + 11, max(0, x - 10):x + 11] = 0
    if float(second.max()) > 0.8 * flux0:          # no dominant star: ambiguous to track
        return None
    h, w = sm0.shape
    r = max(6, int(0.02 * max(h, w)))

    def centroid(sm, yy, xx):
        y0, y1, x0, x1 = max(0, yy - 3), min(h, yy + 4), max(0, xx - 3), min(w, xx + 4)
        p = np.clip(sm[y0:y1, x0:x1] - np.median(sm), 0, None)
        gy, gx = np.mgrid[y0:y1, x0:x1]
        t = p.sum() or 1.0
        return np.array([(gx * p).sum() / t, (gy * p).sum() / t])

    pos[0], ok[0] = centroid(sm0, y, x), True
    vel = np.zeros(2)
    for i in range(1, n):
        sm = cv2.GaussianBlur(previews[i], (0, 0), 1.2)
        px, py = (pos[i - 1] + vel).round().astype(int)
        y0, y1, x0, x1 = max(0, py - r), min(h, py + r + 1), max(0, px - r), min(w, px + r + 1)
        found = None
        if y1 > y0 and x1 > x0:
            win = sm[y0:y1, x0:x1]
            yy, xx = np.unravel_index(int(np.argmax(win)), win.shape)
            if 0.33 * flux0 < win[yy, xx] < 3 * flux0:
                found = (y0 + yy, x0 + xx)
        if found is None:                          # nudge: re-acquire globally
            yy, xx = np.unravel_index(int(np.argmax(sm)), sm.shape)
            if 0.33 * flux0 < sm[yy, xx] < 3 * flux0:
                found = (yy, xx)
        if found is None:
            pos[i] = pos[i - 1] + vel
            continue
        pos[i], ok[i] = centroid(sm, *found), True
        step = pos[i] - pos[i - 1]
        if np.linalg.norm(step - vel) < r / 2 or i == 1:
            vel = 0.7 * vel + 0.3 * step if i > 1 else step
    return pos, ok


def find_segments(d: np.ndarray, resp: np.ndarray, thr: float, min_segment: int) -> tuple[list[list[int]], list[int], list[int]]:
    """Split a sequence at camera nudges. d[i] = motion from frame i-1 to i (d[0] = 0). A nudge is a step that departs
    from the median drift by more than thr, or a step where phase correlation lost the stars. Returns (segments,
    nudge frame indices, frames dropped as moving)."""
    n = len(d)
    steady = np.median(d[1:], 0) if n > 1 else np.zeros(2)
    dev = np.linalg.norm(d - steady, axis=1)
    # adaptive: a hand-rested camera jitters by a few px every frame (harmless, every frame is registered on its
    # own); a nudge is a step far outside this sequence's own jitter
    if n > 2:
        thr = max(thr, 12.0 * float(np.median(dev[1:])))
    rmed = np.median(resp[1:]) if n > 1 else 1.0
    jump = (dev > thr) | (resp < 0.25 * rmed)
    jump[0] = False
    segs, cur, nudges = [], [0], []
    for i in range(1, n):
        if jump[i]:
            nudges.append(i)
            segs.append(cur)
            cur = [i]
        else:
            cur.append(i)
    segs.append(cur)
    good = [s for s in segs if len(s) >= min_segment]
    moving = sorted(i for s in segs if len(s) < min_segment for i in s)
    if not good:   # everything is short: keep the longest so there is something to stack
        good = [max(segs, key=len)]
        moving = sorted(set(moving) - set(good[0]))
    return good, nudges, moving


def _T(dx: float, dy: float) -> np.ndarray:
    return np.array([[1, 0, dx], [0, 1, dy], [0, 0, 1]], np.float64)


def _ecc(ref, img, M, motion, mask=None):
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 300, 1e-7)
    W = M.astype(np.float32) if motion == cv2.MOTION_HOMOGRAPHY else M[:2].astype(np.float32)
    try:
        cc, W = cv2.findTransformECC(ref, img, W, motion, crit, mask, 5)
    except cv2.error:
        return None, M
    out = np.eye(3)
    if motion == cv2.MOTION_HOMOGRAPHY:
        out = W.astype(np.float64) / W[2, 2]
    else:
        out[:2] = W
    return float(cc), out


def _plausible(M: np.ndarray, M0: np.ndarray, shape, max_shift: float, max_scale: float = 0.01) -> bool:
    """Reject ECC solutions that wander: corners must stay within max_shift of the coarser solution and the
    scale must stay ~1 (a fixed lens does not zoom between frames)."""
    h, w = shape
    c = np.array([[[0, 0], [w, 0], [0, h], [w, h]]], np.float64)
    a, b = cv2.perspectiveTransform(c, M)[0], cv2.perspectiveTransform(c, M0)[0]
    sc = math.sqrt(abs(np.linalg.det(M[:2, :2])))
    return bool(np.abs(a - b).max() < max_shift and abs(sc - 1) < max_scale)


def _ecc_register(ref: np.ndarray, img: np.ndarray, inits: Sequence[np.ndarray], mask=None, homography: bool = True):
    """Coarse-to-fine ECC (translation -> euclidean -> homography) from each initial guess; maps ref -> img coords.
    Each refinement is kept only if it raises the correlation and stays plausible. Returns (cc, M) or (None, None)."""
    best = (None, None)
    h, w = ref.shape
    for M0 in inits:
        cc, M = _ecc(ref, img, M0, cv2.MOTION_TRANSLATION, mask)
        if cc is None or not _plausible(M, M0, (h, w), 0.05 * max(h, w), 1e-6):
            continue
        cc2, M2 = _ecc(ref, img, M, cv2.MOTION_EUCLIDEAN, mask)
        if cc2 is not None and cc2 >= cc - 1e-4 and _plausible(M2, M, (h, w), 6.0):
            cc, M = cc2, M2
        if homography:
            cc3, M3 = _ecc(ref, img, M, cv2.MOTION_HOMOGRAPHY, mask)
            if cc3 is not None and cc3 > cc + 0.002 and _plausible(M3, M, (h, w), 4.0):
                cc, M = cc3, M3
        if best[0] is None or cc > best[0]:
            best = (cc, M)
    return best


def _refine_homography(ref_xy: np.ndarray, xy: np.ndarray, M: np.ndarray, tol: float = 3.0) -> tuple[np.ndarray, int]:
    """Given a similarity M (frame -> ref), match all stars and fit a RANSAC homography when there are enough pairs."""
    from scipy.spatial import cKDTree

    p = cv2.perspectiveTransform(xy[None].astype(np.float64), M)[0]
    dist, idx = cKDTree(ref_xy).query(p, distance_upper_bound=tol)
    ok = np.isfinite(dist)
    if ok.sum() < 15:
        return M, int(ok.sum())
    H, inl = cv2.findHomography(xy[ok], ref_xy[idx[ok]], cv2.RANSAC, 1.5)
    if H is None or inl.sum() < 12:
        return M, int(ok.sum())
    return H / H[2, 2], int(inl.sum())


# --------------------------------------------------------------------------- integration workers
def _warp(img: np.ndarray, M: np.ndarray, shape) -> tuple[np.ndarray, np.ndarray]:
    h, w = shape
    out = cv2.warpPerspective(img, M, (w, h), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    valid = cv2.warpPerspective(np.ones(img.shape[:2], np.float32), M, (w, h), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0.999
    return np.maximum(out, 0).astype(np.float32), valid


def _mm(path, shape, dtype=np.float32, mode="w+"):
    return np.lib.format.open_memmap(str(path), mode=mode, dtype=dtype, shape=tuple(shape))


def _pass1(job):
    """Weighted sums over a chunk of frames: S1, S2 (HxWx3), W (HxW), optional ground sum G; fills the cache."""
    wid, chunk = job
    g, H, W = _G, *_G["shape"]
    S1 = np.zeros((H, W, 3), np.float32)
    S2 = np.zeros((H, W, 3), np.float32)
    Wt = np.zeros((H, W), np.float32)
    G = np.zeros((H, W, 3), np.float32) if g.get("freeze") else None
    cache = _mm(g["cache"], (g["n_used"], H, W, 3), np.float16, "r+") if g.get("cache") else None
    vcache = _mm(g["vcache"], (g["n_used"], H, W), np.uint8, "r+") if g.get("cache") else None
    for slot in chunk:
        if g["cancel"].is_set():
            break
        i = g["used"][slot]
        img, _ = _calibrated(g["files"][i])
        x, v = _warp(img, np.asarray(g["mats"][slot]), (H, W))
        w = float(g["weights"][slot])
        vw = v.astype(np.float32) * w
        S1 += x * vw[..., None]
        S2 += x * x * vw[..., None]
        Wt += vw
        if G is not None:
            G += img
        if cache is not None:
            cache[slot] = x.astype(np.float16)
            vcache[slot] = v
        g["tick"].put(1)
    base = Path(g["dir"])
    np.save(base / f"p1_S1_{wid}.npy", S1)
    np.save(base / f"p1_S2_{wid}.npy", S2)
    np.save(base / f"p1_W_{wid}.npy", Wt)
    if G is not None:
        np.save(base / f"p1_G_{wid}.npy", G)
    return wid


def _pass2(job):
    """Rejection / winsorization against the pass-1 mean and sigma; full and half (A/B) accumulators."""
    wid, chunk = job
    g, H, W = _G, *_G["shape"]
    mean = np.load(Path(g["dir"]) / "mean.npy", mmap_mode="r")
    sig = np.load(Path(g["dir"]) / "sigma.npy", mmap_mode="r")
    k = float(g["kappa"])
    acc = {n: np.zeros((H, W, 3), np.float32) for n in ("K", "A", "B")}
    wts = {n: np.zeros((H, W), np.float32) for n in ("K", "A", "B")}
    cnt = np.zeros((H, W), np.uint16)
    cache = _mm(g["cache"], (g["n_used"], H, W, 3), np.float16, "r") if g.get("cache") else None
    vcache = _mm(g["vcache"], (g["n_used"], H, W), np.uint8, "r") if g.get("cache") else None
    band = 512
    for slot in chunk:
        if g["cancel"].is_set():
            break
        if cache is None:
            i = g["used"][slot]
            img, _ = _calibrated(g["files"][i])
            xf, vf = _warp(img, np.asarray(g["mats"][slot]), (H, W))
        w = float(g["weights"][slot])
        half = "A" if g["half_of"][slot] == 0 else "B"
        for y0 in range(0, H, band):                      # bands keep the temporaries small
            y1 = min(H, y0 + band)
            if cache is not None:
                x = cache[slot, y0:y1].astype(np.float32)
                v = vcache[slot, y0:y1].astype(bool)
            else:
                x, v = xf[y0:y1], vf[y0:y1]
            m, s = mean[y0:y1], sig[y0:y1]
            if g["reject"] == "winsor":
                x = np.clip(x, m - k * s, m + k * s)
                keep = v
            elif g["reject"] == "sigma":
                bad = (np.abs(x - m) > k * s + 1e-7).any(-1)       # reject the pixel in all channels together
                keep = v & ~bad
            else:
                keep = v
            kw = keep.astype(np.float32) * w
            xk = x * kw[..., None]
            acc["K"][y0:y1] += xk
            wts["K"][y0:y1] += kw
            acc[half][y0:y1] += xk
            wts[half][y0:y1] += kw
            cnt[y0:y1] += v
        g["tick"].put(1)
    base = Path(g["dir"])
    for n in ("K", "A", "B"):
        np.save(base / f"p2_{n}_{wid}.npy", acc[n])
        np.save(base / f"p2_{n}w_{wid}.npy", wts[n])
    np.save(base / f"p2_C_{wid}.npy", cnt)
    return wid


def _pmax(job):
    """reject='max' (lighten): running maximum, single pass."""
    wid, chunk = job
    g, H, W = _G, *_G["shape"]
    M = np.zeros((H, W, 3), np.float32)
    cnt = np.zeros((H, W), np.uint16)
    for slot in chunk:
        if g["cancel"].is_set():
            break
        img, _ = _calibrated(g["files"][g["used"][slot]])
        x, v = _warp(img, np.asarray(g["mats"][slot]), (H, W))
        np.maximum(M, x, out=M)
        cnt += v
        g["tick"].put(1)
    np.save(Path(g["dir"]) / f"pm_{wid}.npy", M)
    np.save(Path(g["dir"]) / f"pmC_{wid}.npy", cnt)
    return wid


# --------------------------------------------------------------------------- helpers
def _available_gb() -> float:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 ** 2
    except OSError:
        pass
    return 8.0


def plan_jobs(shape, opts: StackOptions, n_frames: int) -> int:
    """Worker count from cores and a RAM budget (each worker holds ~10 full-frame float32 planes during pass 2)."""
    h, w = shape
    per = h * w * 3 * 4 * (7 if opts.halves else 5) / 1024 ** 3 + 0.6      # + raw decoder overhead
    budget = opts.mem_gb or 0.6 * _available_gb()
    cores = max(1, (os.cpu_count() or 2) - 2)
    j = opts.jobs or cores
    return int(max(1, min(j, n_frames, budget // per)))


def analyse_workers(shape, opts: StackOptions, n_frames: int) -> int:
    """Pass-0 workers each hold one decoded frame plus the raw decoder's buffers (~4.5 full-frame float planes for a
    24 MP CR3, measured: an unbounded pool of 14 peaked at 11 GB and was OOM-killed)."""
    h, w = shape
    per = h * w * 3 * 4 * 4.5 / 1024 ** 3 + 0.25
    sc = min(1.0, opts.work_side / max(h, w))
    parent = n_frames * h * w * sc * sc * (2 + (1 if opts.align == "scene" else 0)) / 1024 ** 3   # f16 previews (+u8 gray)
    budget = (opts.mem_gb or 0.6 * _available_gb()) - parent - _rss_gb()
    cores = max(1, (os.cpu_count() or 2) - 2)
    return int(max(1, min(opts.jobs or cores, cores, n_frames, budget // per)))


class _HalfList(list):
    """Registration previews kept at float16 (500 x 2000 px previews are 5 GB as float32 in the parent process,
    which together with the integration workers overran a 12 GB cap); reads come back as float32."""

    def __getitem__(self, i):
        v = list.__getitem__(self, i)
        return v.astype(np.float32) if isinstance(v, np.ndarray) else v

    def __setitem__(self, i, v):
        list.__setitem__(self, i, v.astype(np.float16) if isinstance(v, np.ndarray) else v)


def _rss_gb() -> float:
    try:
        return int(open("/proc/self/statm").read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1024 ** 3
    except (OSError, ValueError):
        return 0.0


def _inner_crop(cov: np.ndarray, need: float, step: int = 8) -> tuple[slice, slice]:
    """Largest axis-aligned rectangle where coverage >= need (maximal-rectangle-in-histogram on a downsampled mask)."""
    H, W = cov.shape
    ok = cv2.resize((cov >= need).astype(np.uint8), (max(1, W // step), max(1, H // step)),
                    interpolation=cv2.INTER_NEAREST).astype(bool)
    h, w = ok.shape
    heights = np.zeros(w, np.int64)
    best = (0, 0, 0, 0, 0)   # area, r0, r1, c0, c1  (downsampled, half-open)
    for r in range(h):
        heights = np.where(ok[r], heights + 1, 0)
        stack: list[int] = []
        for c in range(w + 1):
            cur = heights[c] if c < w else 0
            while stack and heights[stack[-1]] >= cur:
                top = stack.pop()
                left = stack[-1] + 1 if stack else 0
                area = heights[top] * (c - left)
                if area > best[0]:
                    best = (int(area), r - int(heights[top]) + 1, r + 1, left, c)
            stack.append(c)
    if best[0] == 0:
        return slice(0, H), slice(0, W)
    _, r0, r1, c0, c1 = best
    return slice(r0 * step + (step if r0 else 0), min(H, r1 * step) - (step if r1 < h else 0)), \
        slice(c0 * step + (step if c0 else 0), min(W, c1 * step) - (step if c1 < w else 0))


def _resolve_ground(opts: StackOptions, ref_img: np.ndarray) -> np.ndarray | None:
    """Sky mask (1 = sky) in reference-frame pixels, or None."""
    h, w = ref_img.shape[:2]
    if opts.freeze_ground is not None:
        m = mf._resolve_sky_mask(opts.freeze_ground, ref_img)
        return np.clip(m, 0, 1).astype(np.float32)
    if opts.horizon:
        m = np.ones((h, w), np.float32)
        m[int(round(h * (1 - opts.horizon))):] = 0
        return cv2.GaussianBlur(m, (0, 0), max(2.0, h / 400))
    return None


def _ctx():
    # forkserver, never fork: rawpy/LibRaw and OpenCV use OpenMP, and forking a process that has started OpenMP
    # threads can deadlock the children
    return mp.get_context("forkserver")


# --------------------------------------------------------------------------- main entry
def stack(files: Sequence, opts: StackOptions | None = None, progress: ProgressFn | None = None,
          cancel: threading.Event | None = None) -> StackResult:
    """Register and integrate night-sky frames. See the module docstring for the algorithm."""
    opts = opts or StackOptions()
    files = [str(f) if isinstance(f, Path) else f for f in files]
    n = len(files)
    if n < 2:
        raise ValueError("astro stack needs at least two frames")
    cancel = cancel or threading.Event()
    say = progress or (lambda stage, frac, msg="": None)
    t0 = time.time()
    notes: list[str] = []

    def check():
        if cancel.is_set():
            raise Cancelled("cancelled")

    workdir = Path(tempfile.mkdtemp(prefix="ps_astro_", dir=opts.workdir))
    try:
        # ---- masters ----------------------------------------------------------------------------------------
        say("calibration", 0.0, "building master frames")
        dark = mf._master(list(opts.darks)) if opts.darks else None
        flat = mf._master(list(opts.flats)) if opts.flats else None
        bias = mf._master(list(opts.bias)) if opts.bias else None
        if flat is not None:
            if bias is not None and flat.shape == bias.shape:
                flat = flat - bias
            flat = np.maximum(flat, 1e-6)
            flat = flat / flat.reshape(-1, 3).mean(0)
        G = {"files": files, "dark": dark, "flat": flat, "bias": bias, "lens": opts.lens, "draft": opts.draft,
             "work_side": opts.work_side, "want_gray": opts.align == "scene"}
        _init(G)
        ref_guess = opts.reference if opts.reference is not None else n // 2
        ref_img, _ = _calibrated(files[ref_guess])
        shape = ref_img.shape[:2]
        sky = _resolve_ground(opts, ref_img)
        scale = min(1.0, opts.work_side / max(shape))
        G["scale"] = scale
        if sky is not None:
            gs = cv2.resize(sky, (int(round(shape[1] * scale)), int(round(shape[0] * scale))), interpolation=cv2.INTER_AREA)
            # registration must not see the horizon / tree edges (static structure locks the alignment at zero
            # shift): erode the sky by a margin and use a hard mask
            m = max(8, int(0.015 * max(gs.shape)))
            G["ground_small"] = cv2.erode((gs > 0.5).astype(np.uint8), np.ones((2 * m + 1, 2 * m + 1), np.uint8)).astype(np.float32)
        del ref_img
        jobs = plan_jobs(shape, opts, n)
        notes.append(f"{jobs} worker processes, frame {shape[1]}x{shape[0]}")

        # ---- pass 0: previews, stars, noise -----------------------------------------------------------------
        check()
        say("analyse", 0.0, f"reading {n} frames")
        previews, stars, noise, metas = _HalfList([None] * n), [None] * n, np.zeros(n), [None] * n
        with _ctx().Pool(analyse_workers(shape, opts, n), initializer=_init, initargs=(G,)) as pool:
            for k, (i, p, st, sg, shp, _s, lite) in enumerate(pool.imap_unordered(_pass0, range(n))):
                if tuple(shp) != tuple(shape):
                    raise ValueError(f"frame {files[i]} is {shp[1]}x{shp[0]}, expected {shape[1]}x{shape[0]}")
                previews[i], stars[i], noise[i], metas[i] = p, st, sg, lite
                say("analyse", (k + 1) / n, f"frame {k + 1}/{n}: {len(st)} stars")
                if cancel.is_set():
                    pool.terminate()
                    check()
        nstars = np.array([len(s) for s in stars])
        lens_notes = {m.get("_lens_note") for m in metas if m and m.get("_lens_note")}
        notes += sorted(lens_notes)

        # ---- motion + nudges ----------------------------------------------------------------------------------
        check()
        say("motion", 0.0, "measuring drift and camera nudges")
        d = np.zeros((n, 2))
        resp = np.ones(n)
        tb = track_bright(previews) if np.median(nstars) < 12 and opts.align != "scene" else None
        if opts.align == "scene":                # landscape registration does not need the sky motion track
            pass
        elif tb is not None:                     # star-poor sky with one dominant star: follow it
            tpos, tok = tb
            d[1:] = np.diff(tpos, axis=0)
            resp[1:] = (tok[1:] & tok[:-1]).astype(float) + 1e-3
            notes.append(f"motion: tracked the brightest star ({int(tok.sum())}/{n} frames)")
        else:
            for i in range(1, n):
                d[i], resp[i] = _phase(previews[i - 1], previews[i])
        thr = opts.nudge_px * scale
        segments, nudges, moving = find_segments(d, resp, thr, opts.min_segment)
        if opts.align == "scene":              # each frame is matched to the base on its own: keep them all
            segments, moving = [list(range(n))], []
        pos = np.cumsum(d, 0)                                 # sky position per frame (preview px)
        if moving:
            notes.append(f"dropped {len(moving)} frames taken while the camera moved")
        if nudges:
            notes.append(f"camera nudged before frames {', '.join(str(i + 1) for i in nudges)}")

        # ---- candidate frames, reference ---------------------------------------------------------------------
        cand = sorted(i for s in segments for i in s)
        if opts.keep < 1.0:
            score = nstars[cand] / np.maximum(noise[cand] / np.median(noise[cand]), 1e-6)
            k = max(2, int(round(len(cand) * opts.keep)))
            kept = set(np.array(cand)[np.argsort(-score)[:k]].tolist())
            notes.append(f"kept the best {k} of {len(cand)} steady frames")
            cand = sorted(kept)
        if opts.reference is not None and opts.reference in cand:
            ref = opts.reference
        else:
            longest = max(segments, key=len)
            mids = [i for i in longest if i in cand] or cand
            ref = mids[len(mids) // 2]
        mode = opts.align
        if mode == "auto":
            mode = "stars" if np.median(nstars[cand]) >= 12 else "ecc"
            notes.append(f"registration: {mode} (median {int(np.median(nstars[cand]))} stars per frame)")

        # ---- registration ---------------------------------------------------------------------------------------
        check()
        say("register", 0.0, f"aligning to frame {ref + 1}")
        D = np.diag([1 / scale, 1 / scale, 1.0])
        Di = np.linalg.inv(D)
        mats: dict[int, np.ndarray] = {}
        quality: dict[int, float] = {}
        dropped: dict[int, str] = {i: "camera moving" for i in moving}
        if mode == "none":
            mats = {i: np.eye(3) for i in cand}
        elif mode == "scene":   # static landscape / city: SIFT features + RANSAC homography on the whole frame
            sift = cv2.SIFT_create(4000)
            grays = {i: metas[i].pop("_gray") for i in cand}
            kr, dr = sift.detectAndCompute(grays[ref], None)
            bf = cv2.BFMatcher()
            for k, i in enumerate(cand):
                check()
                if i == ref:
                    mats[i], quality[i] = np.eye(3), float(len(kr))
                    continue
                ki, di = sift.detectAndCompute(grays[i], None)
                good = [] if di is None or dr is None else \
                    [a for a, b in (m for m in bf.knnMatch(di, dr, k=2) if len(m) == 2) if a.distance < 0.75 * b.distance]
                if len(good) < 12:
                    dropped[i] = "does not overlap the base frame"
                    continue
                src = np.float32([ki[a.queryIdx].pt for a in good])
                dst = np.float32([kr[a.trainIdx].pt for a in good])
                Hp, inl = cv2.findHomography(src, dst, cv2.RANSAC, 1.5)
                nin = int(inl.sum()) if inl is not None else 0
                if Hp is None or nin < 40:
                    dropped[i] = f"scene match too weak ({nin} inliers)"
                    continue
                mats[i], quality[i] = D @ Hp @ Di, float(nin)
                say("register", (k + 1) / len(cand), f"frame {i + 1}: {nin} matched features")
        elif mode == "stars":
            rs = stars[ref][:, :2]
            for k, i in enumerate(cand):
                if i == ref:
                    mats[i], quality[i] = np.eye(3), float(len(rs))
                    continue
                M = mf.star_transform(stars[ref], stars[i], shape, notes)
                if M is None:
                    dropped[i] = "star matching failed"
                    continue
                M, nm = _refine_homography(rs, stars[i][:, :2], M)
                mats[i], quality[i] = M, float(nm)
                say("register", (k + 1) / len(cand), f"frame {i + 1}: {nm} matched stars")
        else:   # ecc on mini-stacks
            groups = []
            for s in segments:
                s = [i for i in s if i in cand]
                groups += [s[j:j + opts.group] for j in range(0, len(s), max(1, opts.group))]
            groups = [g for g in groups if g]

            def gstack(g):
                acc = np.zeros_like(previews[g[0]])
                for i in g:
                    dx, dy = pos[g[0]] - pos[i]
                    acc += cv2.warpAffine(previews[i], _T(dx, dy)[:2].astype(np.float32), acc.shape[::-1],
                                          flags=cv2.INTER_LINEAR)
                return _for_ecc(acc / len(g))

            tracked = tb is not None
            fallback_track = 0
            gref = next(g for g in groups if ref in g)
            R = gstack(gref)
            rdx, rdy = pos[gref[0]] - pos[ref]
            to_ref_frame = np.linalg.inv(_T(rdx, rdy))        # gref[0] coords -> ref frame coords
            emask = None
            if "ground_small" in G:
                emask = (G["ground_small"] > 0.5).astype(np.uint8)
            for k, g in enumerate(groups):
                check()
                if g is gref:
                    Hg, cc = np.eye(3), 1.0
                else:
                    I = gstack(g)
                    inits = [_T(*(pos[g[0]] - pos[gref[0]]))]          # from the motion track (handles nudges)
                    dd, _ = _phase(R, I)
                    if np.linalg.norm(dd - (pos[g[0]] - pos[gref[0]])) > 3:
                        inits.append(_T(*dd))                          # global phase correlation as a second opinion
                    cc, Hh = _ecc_register(R, I, inits, emask, homography=opts.homography)
                    if (cc is None or cc < 0.3) and tracked:
                        Hh, cc = inits[0], float(cc or 0.0)              # too few stars for ECC: use the track
                        fallback_track += len(g)
                    elif cc is None or cc < 0.3:
                        for i in g:
                            dropped[i] = f"mini-stack registration failed (cc={cc if cc is not None else 0:.2f})"
                        continue
                    Hg = np.linalg.inv(Hh)                   # group coords -> gref[0] coords
                for i in g:
                    dx, dy = pos[g[0]] - pos[i]
                    Mp = to_ref_frame @ Hg @ _T(dx, dy)          # frame i -> ref (preview px)
                    mats[i] = D @ Mp @ Di
                    quality[i] = float(cc)
                say("register", (k + 1) / len(groups), f"mini-stack {k + 1}/{len(groups)} cc={cc:.2f}")

            if fallback_track:
                notes.append(f"{fallback_track} frames registered by star tracking only (too few stars for ECC)")
        used = [i for i in cand if i in mats]
        if len(used) < 2:
            raise RuntimeError("fewer than two frames could be registered")
        w = 1.0 / np.maximum(noise[used], 1e-12) ** 2 if opts.weights == "noise" else np.ones(len(used))
        w = w / w.mean()
        half_of = [k % 2 for k in range(len(used))]

        # ---- integration ----------------------------------------------------------------------------------------
        H, W = shape
        nu = len(used)
        need = nu * H * W * 3 * 2 + nu * H * W
        want_cache = opts.cache == "on" or (opts.cache == "auto" and opts.reject in ("sigma", "winsor")
                                             and shutil.disk_usage(workdir).free > need * 1.3)
        G.update({"shape": shape, "used": used, "mats": [mats[i].tolist() for i in used], "weights": w.tolist(),
                  "half_of": half_of, "n_used": nu, "dir": str(workdir), "kappa": opts.kappa, "reject": opts.reject,
                  "freeze": sky is not None, "cancel": cancel})
        if want_cache:
            G["cache"], G["vcache"] = str(workdir / "cache.npy"), str(workdir / "vcache.npy")
            _mm(G["cache"], (nu, H, W, 3), np.float16)
            _mm(G["vcache"], (nu, H, W), np.uint8)
        # free the registration data, then size the integration pool from the memory that is really left
        import gc

        previews = None
        for m in metas:
            if m:
                m.pop("_gray", None)
        if mode == "scene":
            grays = None   # noqa: F841
        gc.collect()
        h_, w_ = shape
        per = h_ * w_ * 3 * 4 * (7 if opts.halves else 5) / 1024 ** 3 + 0.6
        left = (opts.mem_gb or 0.6 * _available_gb()) - _rss_gb()
        jobs = int(max(1, min(jobs, left // per)))
        notes.append(f"integration with {jobs} workers ({left:.1f} GB left for them)")
        chunks = [list(range(j, nu, jobs)) for j in range(jobs)]
        chunks = [(k, c) for k, c in enumerate(chunks) if c]
        mpc = _ctx()
        mgr_q = mpc.Manager() if mpc.get_start_method() != "fork" else None
        G["tick"] = mgr_q.Queue() if mgr_q else mpc.Queue()
        if mgr_q:   # Event objects cannot cross a forkserver boundary; use a manager event
            ev = mgr_q.Event()
            G["cancel"] = ev
            threading.Thread(target=lambda: (cancel.wait(), ev.set()), daemon=True).start()

        def run(fn, stage, label):
            done = 0
            with mpc.Pool(len(chunks), initializer=_init, initargs=(G,)) as pool:
                res = pool.map_async(fn, chunks)
                while not res.ready() or not G["tick"].empty():
                    try:
                        G["tick"].get(timeout=0.5)
                        done += 1
                        say(stage, done / nu, f"{label} {done}/{nu}")
                    except Exception:  # noqa: BLE001  queue.Empty
                        pass
                    if cancel.is_set():
                        pool.terminate()
                        raise Cancelled("cancelled")
                res.get()

        def total(prefix, shp, dtype=np.float32):
            acc = np.zeros(shp, np.float64 if dtype == np.float32 else np.uint32)
            for k, _c in chunks:
                acc += np.load(workdir / f"{prefix}_{k}.npy")
                os.remove(workdir / f"{prefix}_{k}.npy")
            return acc

        half_a = half_b = None
        if opts.reject == "max":
            run(_pmax, "integrate", "frame")
            out = np.zeros((H, W, 3), np.float32)
            for k, _c in chunks:
                np.maximum(out, np.load(workdir / f"pm_{k}.npy"), out=out)
            cov = total("pmC", (H, W), np.uint16)
            ground = None
        else:
            run(_pass1, "integrate", "pass 1: frame")
            S1 = total("p1_S1", (H, W, 3))
            S2 = total("p1_S2", (H, W, 3))
            Wt = total("p1_W", (H, W))
            ground = total("p1_G", (H, W, 3)) / nu if sky is not None else None
            Wd = np.maximum(Wt, 1e-9)[..., None]
            mean = S1 / Wd
            var = np.maximum(S2 / Wd - mean ** 2, 0) * (nu / max(nu - 1, 1))
            del S1, S2
            if opts.reject == "mean":
                out, cov = mean.astype(np.float32), (Wt > 0).astype(np.uint32) * nu
            else:
                np.save(workdir / "mean.npy", mean.astype(np.float32))
                np.save(workdir / "sigma.npy", np.sqrt(var).astype(np.float32))
                del var
                run(_pass2, "integrate", "pass 2: frame")
                K = total("p2_K", (H, W, 3))
                Kw = total("p2_Kw", (H, W))[..., None]
                out = np.where(Kw > 0, K / np.maximum(Kw, 1e-9), mean).astype(np.float32)
                if opts.halves:
                    A, Aw = total("p2_A", (H, W, 3)), total("p2_Aw", (H, W))[..., None]
                    B, Bw = total("p2_B", (H, W, 3)), total("p2_Bw", (H, W))[..., None]
                    half_a = np.where(Aw > 0, A / np.maximum(Aw, 1e-9), mean).astype(np.float32)
                    half_b = np.where(Bw > 0, B / np.maximum(Bw, 1e-9), mean).astype(np.float32)
                else:
                    for p in ("p2_A", "p2_Aw", "p2_B", "p2_Bw"):
                        for k, _c in chunks:
                            os.remove(workdir / f"{p}_{k}.npy")
                cov = total("p2_C", (H, W), np.uint16)
            del mean
        if mgr_q:
            mgr_q.shutdown()

        # ---- freeze ground: composite the unaligned ground under the aligned sky --------------------------------
        if sky is not None and ground is not None:
            m = sky[..., None]
            out = (out * m + ground.astype(np.float32) * (1 - m)).astype(np.float32)
            if half_a is not None:
                half_a = (half_a * m + ground * (1 - m)).astype(np.float32)
                half_b = (half_b * m + ground * (1 - m)).astype(np.float32)
            cov = np.where(sky > 0.5, cov, nu)

        # ---- crop ---------------------------------------------------------------------------------------------------
        crop = (slice(None), slice(None))
        if opts.crop:
            crop = _inner_crop(cov, opts.min_cover * nu)
        out = np.ascontiguousarray(out[crop])
        if half_a is not None:
            half_a, half_b = np.ascontiguousarray(half_a[crop]), np.ascontiguousarray(half_b[crop])
        cov = np.ascontiguousarray(cov[crop]).astype(np.uint16)

        exp = [metas[i].get("shutter") for i in used if metas[i]]
        total_exp = float(sum(float(e) for e in exp if e)) if exp and all(exp) else None
        info = {
            "frames_total": n, "frames_used": len(used), "reference": ref, "registration": mode,
            "rejection": opts.reject, "kappa": opts.kappa, "workers": jobs, "cache": bool(want_cache),
            "segments": [[s[0], s[-1]] for s in segments], "nudges_before": nudges,
            "dropped": {str(i): r for i, r in sorted(dropped.items())},
            "drift_px_per_frame": (np.linalg.norm(np.median(d[1:], 0)) / scale) if n > 1 else 0.0,
            "stars_per_frame_median": int(np.median(nstars)),
            "noise_gain_vs_single_frame": float(math.sqrt(len(used))),
            "total_exposure_s": total_exp,
            "crop": [crop[0].start or 0, crop[0].stop or H, crop[1].start or 0, crop[1].stop or W],
            "seconds": round(time.time() - t0, 1),
            "frames": [{"index": i, "file": str(files[i]), "stars": int(nstars[i]), "noise": float(noise[i]),
                        "weight": float(w[k]), "quality": float(quality.get(i, 0.0)), "half": "AB"[half_of[k]]}
                       for k, i in enumerate(used)],
            "transforms": {str(i): np.round(mats[i], 6).tolist() for i in used},
            "notes": notes,
        }
        say("done", 1.0, f"{len(used)} of {n} frames stacked in {info['seconds']} s")
        return StackResult(out, half_a, half_b, cov, info)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
