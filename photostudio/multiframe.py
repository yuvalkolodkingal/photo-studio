"""Multi-frame tools: HDR merge, focus stacking, astro stacking, star trails, long exposure, median/noise stacks, panorama.

All functions take file paths (decoded with `photostudio.decode.decode`), `Decoded` objects or float32 arrays
(linear Rec.2020 unless `uint8`, which is read as display sRGB) and return float32 *linear* Rec.2020 images
(or `(image, info)` when `return_info=True`). Frames are processed one at a time where possible; median /
kappa-sigma combines run in row bands over a temporary memmap, so 24 MP x 30 frames fits in 7 GB of RAM.
"""
from __future__ import annotations

import math
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

from . import color
from .filters import collapse_pyramid, gaussian, laplacian_pyramid


# --------------------------------------------------------------------------- loading
def load_frame(item, raw_highlight: str = "clip") -> tuple[np.ndarray, dict]:
    """-> (linear float32 HxWx3, metadata dict). uint8 arrays are display sRGB; float arrays are linear."""
    from . import decode as dec

    if isinstance(item, dec.Decoded):
        return item.image.astype(np.float32, copy=False), dict(item.meta or {})
    if isinstance(item, np.ndarray):
        if item.dtype == np.uint8:
            return color.apply_matrix(color.srgb8_to_linear(item), color.SRGB_TO_REC2020), {}
        if item.dtype == np.uint16:
            return color.apply_matrix(color.srgb_eotf(item.astype(np.float32) / 65535.0), color.SRGB_TO_REC2020), {}
        a = item.astype(np.float32, copy=False)
        if a.ndim == 2:
            a = np.repeat(a[..., None], 3, 2)
        return a, {}
    p = Path(item)
    if p.suffix.lower() in dec.RAW_EXTS:
        d = dec.decode_raw(p, highlight=raw_highlight)
    else:
        d = dec.decode(p)
    return d.image, dict(d.meta or {})


def _gain_from_meta(meta: dict) -> float | None:
    t, iso, f = meta.get("shutter"), meta.get("iso"), meta.get("aperture")
    try:
        if t and f:
            return float(t) * (float(iso) / 100.0 if iso else 1.0) / float(f) ** 2
    except (TypeError, ValueError):
        pass
    return None


def estimate_relative_exposures(imgs: Sequence[np.ndarray], ref: int, white: float = 1.0) -> list[float]:
    """Relative signal gain of each frame w.r.t. `ref`, from the median ratio of well-exposed pixels."""
    R = imgs[ref]
    sub = (slice(None, None, 4), slice(None, None, 4))
    r = R[sub].mean(2)
    out = []
    for i, im in enumerate(imgs):
        if i == ref:
            out.append(1.0)
            continue
        v = im[sub].mean(2)
        ok = (r > 0.02 * white) & (r < 0.6 * white) & (v > 0.02 * white) & (v < 0.6 * white)
        if ok.sum() < 200:
            ok = (r > 0.005 * white) & (v > 0.005 * white) & (np.maximum(r, v) < 0.9 * white)
        out.append(float(np.median(v[ok] / r[ok])) if ok.sum() > 50 else 1.0)
    return out


# --------------------------------------------------------------------------- alignment
def _enc_gray8(img: np.ndarray, norm: bool = True) -> np.ndarray:
    lum = np.clip(color.luminance(img), 0, None)
    if norm:
        hi = max(float(np.percentile(lum[::4, ::4], 99.5)), 1e-6)
        lum = lum / hi
    return (np.clip(color.srgb_oetf(np.clip(lum, 0, 1)), 0, 1) * 255.0 + 0.5).astype(np.uint8)


def _small(a: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    h, w = a.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s >= 1.0:
        return a, 1.0
    return cv2.resize(a, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA), s


def _to3x3(m: np.ndarray) -> np.ndarray:
    return m if m.shape == (3, 3) else np.vstack([m, [0, 0, 1]]).astype(np.float64)


def estimate_alignment(ref: np.ndarray, img: np.ndarray, method: str = "mtb", max_side: int = 900,
                       log: list | None = None) -> np.ndarray:
    """3x3 matrix mapping `img` pixel coords -> `ref` pixel coords.
    mtb (translation, robust to exposure change) | ecc (euclid) | ecc_affine | ecc_homography | feature (ORB+RANSAC homography) | none."""
    I3 = np.eye(3)
    if method in ("none", None):
        return I3
    g0, s = _small(_enc_gray8(ref), max_side)
    g1, _ = _small(_enc_gray8(img), max_side)
    try:
        if method == "mtb":
            mtb = cv2.createAlignMTB(6, 4, True)
            dx, dy = mtb.calculateShift(g0, g1)
            T = np.array([[1, 0, dx / s], [0, 1, dy / s], [0, 0, 1]], np.float64)
            return T
        if method.startswith("ecc"):
            mode = {"ecc": cv2.MOTION_EUCLIDEAN, "ecc_affine": cv2.MOTION_AFFINE,
                    "ecc_homography": cv2.MOTION_HOMOGRAPHY, "ecc_translation": cv2.MOTION_TRANSLATION}[method]
            warp = np.eye(3, 3, dtype=np.float32) if mode == cv2.MOTION_HOMOGRAPHY else np.eye(2, 3, dtype=np.float32)
            # coarse translation seed with MTB (ECC converges poorly from far away). ECC's warp maps ref -> img
            # coords, and MTB's shift moves img onto ref, hence the minus sign.
            try:
                dx, dy = cv2.createAlignMTB(6, 4, True).calculateShift(g0, g1)
                warp[0, 2], warp[1, 2] = -dx, -dy
            except cv2.error:
                pass
            crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5)
            f0, f1 = g0.astype(np.float32) / 255.0, g1.astype(np.float32) / 255.0
            # ECC convention: template(x) ~ input(W(x))  => W maps ref coords -> img coords; we need img -> ref (invert)
            _, warp = cv2.findTransformECC(f0, f1, warp, mode, crit, None, 5)
            W = _to3x3(warp.astype(np.float64))
            S = np.diag([s, s, 1.0])
            Wfull = np.linalg.inv(S) @ W @ S
            return np.linalg.inv(Wfull)
        if method == "feature":
            orb = cv2.ORB_create(3000)
            k0, d0 = orb.detectAndCompute(g0, None)
            k1, d1 = orb.detectAndCompute(g1, None)
            if d0 is None or d1 is None:
                raise cv2.error("no features")
            m = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d1, d0)
            m = sorted(m, key=lambda x: x.distance)[:500]
            p1 = np.float32([k1[x.queryIdx].pt for x in m])
            p0 = np.float32([k0[x.trainIdx].pt for x in m])
            H, inl = cv2.findHomography(p1, p0, cv2.RANSAC, 3.0)
            if H is None:
                raise cv2.error("homography failed")
            S = np.diag([s, s, 1.0])
            return np.linalg.inv(S) @ H @ S
    except cv2.error as exc:
        if log is not None:
            log.append(f"alignment '{method}' failed ({exc}); frame left unaligned")
        return I3
    raise ValueError(f"unknown alignment method '{method}'")


def warp_to_ref(img: np.ndarray, T: np.ndarray, shape: tuple[int, int], interp: int = cv2.INTER_CUBIC,
                fill: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Warp `img` with the 3x3 transform T (img -> ref coords). Returns (warped, valid 0/1 float mask)."""
    h, w = shape
    ident = np.allclose(T, np.eye(3), atol=1e-9) and img.shape[:2] == (h, w)
    if ident:
        return img, np.ones((h, w), np.float32)
    ones = np.ones(img.shape[:2], np.float32)
    if np.allclose(T[2], [0, 0, 1]):
        M = T[:2]
        out = cv2.warpAffine(img, M, (w, h), flags=interp, borderMode=cv2.BORDER_CONSTANT, borderValue=fill)
        val = cv2.warpAffine(ones, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    else:
        out = cv2.warpPerspective(img, T, (w, h), flags=interp, borderMode=cv2.BORDER_CONSTANT, borderValue=fill)
        val = cv2.warpPerspective(ones, T, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return out, (val > 0.999).astype(np.float32)


def align_frames(frames: Sequence[np.ndarray], method: str = "mtb", ref: int = 0, log: list | None = None,
                 interp: int = cv2.INTER_CUBIC) -> tuple[list[np.ndarray], list[np.ndarray], list[np.ndarray]]:
    """Align linear frames to frames[ref]. Returns (aligned, valid masks, transforms)."""
    out, vals, Ts = [], [], []
    shape = frames[ref].shape[:2]
    for i, f in enumerate(frames):
        T = np.eye(3) if i == ref else estimate_alignment(frames[ref], f, method, log=log)
        w, v = warp_to_ref(f, T, shape, interp)
        out.append(w)
        vals.append(v)
        Ts.append(T)
    return out, vals, Ts


# --------------------------------------------------------------------------- HDR
def _smooth01(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def hdr_merge(frames: Iterable, method: str = "linear", align: str = "mtb", exposures: Sequence[float] | None = None,
              white: float = 1.0, noise: tuple[float, float] = (2e-3, 2e-5), deghost: bool = False,
              ghost_sigma: float = 4.0, mertens_weights: tuple[float, float, float] = (1.0, 1.0, 1.0),
              return_info: bool = False, raw_highlight: str = "clip"):
    """Merge an exposure bracket.

    linear   (default, for raw / linear frames) inverse-variance weighted radiance in the *reference frame's* units:
             E_i = I_i / r_i,  w_i = r_i^2 / (a*I_i + b) * (1 - smoothstep(0.90, 0.98, max_c(I_i)/white)),  E = sum w E / sum w.
             Pixels clipped in every frame take the darkest frame. `exposures` are relative shutter/ISO/aperture gains
             (else EXIF `shutter*iso/aperture^2`, else estimated from pixel ratios).
    mertens  exposure fusion (contrast x saturation x well-exposedness) on display-encoded copies -> linear.
    debevec  Debevec calibration + merge on 8-bit display copies (for JPEG brackets); needs `exposures`.
    """
    items = list(frames)
    if len(items) < 2:
        raise ValueError("hdr_merge needs at least two frames")
    loaded = [load_frame(f, raw_highlight) for f in items]
    imgs = [a for a, _ in loaded]
    metas = [m for _, m in loaded]
    log: list[str] = []
    gains = None
    if exposures is not None:
        gains = [float(g) for g in exposures]
    else:
        mg = [_gain_from_meta(m) for m in metas]
        if all(g is not None for g in mg):
            gains = mg
    order = np.argsort(gains) if gains is not None else None
    ref = int(order[len(order) // 2]) if order is not None else len(imgs) // 2
    if gains is None:
        gains = estimate_relative_exposures(imgs, ref, white)
        log.append("exposures estimated from pixel ratios")
    r = [g / gains[ref] for g in gains]
    aligned, valids, Ts = align_frames(imgs, align, ref, log)
    shape = aligned[ref].shape[:2]

    if method == "mertens":
        mm = cv2.createMergeMertens(*[float(x) for x in mertens_weights])
        # OpenCV's MergeMertens always divides its input by 255 (also for float32), so feed 0..255 values
        disp = [np.ascontiguousarray(color.srgb_oetf(np.clip(a / max(white, 1e-6), 0, 1))[..., ::-1] * 255.0).astype(np.float32)
                for a in aligned]
        res = mm.process(disp)[..., ::-1]
        out = color.srgb_eotf(np.clip(res, 0, 1)).astype(np.float32) * white
    elif method == "debevec":
        times = np.asarray(gains, np.float32)
        u8 = [np.ascontiguousarray((np.clip(color.srgb_oetf(np.clip(a / white, 0, 1)), 0, 1) * 255 + 0.5).astype(np.uint8)[..., ::-1])
              for a in aligned]
        resp = cv2.createCalibrateDebevec().process(u8, times)
        hdr = cv2.createMergeDebevec().process(u8, times, resp)[..., ::-1].astype(np.float32)
        k = float(np.median(aligned[ref][::4, ::4].mean(2)) / max(float(np.median(hdr[::4, ::4].mean(2))), 1e-9))
        out = np.maximum(hdr * k, 0)
    elif method == "linear":
        a_, b_ = noise
        num = np.zeros_like(aligned[0])
        den = np.zeros(shape, np.float32)
        dark_i = int(np.argmin(r))
        Eref = aligned[ref] / r[ref]
        for i, (I, v) in enumerate(zip(aligned, valids)):
            mx = I.max(axis=2) / white
            sat = 1.0 - _smooth01(0.90, 0.98, mx)
            var = (a_ * np.maximum(I.mean(2), 0) + b_) / (r[i] ** 2)
            w = v * sat / var
            E = I / r[i]
            if deghost and i != ref:
                diff = np.abs(E - Eref).max(axis=2)
                tol = ghost_sigma * np.sqrt(var + (a_ * np.maximum(aligned[ref].mean(2), 0) + b_) / (r[ref] ** 2))
                bad = cv2.dilate((diff > tol).astype(np.uint8), np.ones((5, 5), np.uint8))
                w = w * (1.0 - gaussian(bad.astype(np.float32), 2.0).clip(0, 1))
            num += E * w[..., None]
            den += w
        out = num / np.maximum(den[..., None], 1e-20)
        dead = den < 1e-6   # unseen in every valid frame: darkest frame regardless of clipping
        if dead.any():
            out[dead] = (aligned[dark_i] / r[dark_i])[dead]
    else:
        raise ValueError("hdr method must be linear | mertens | debevec")
    out = out.astype(np.float32)
    if return_info:
        return out, {"method": method, "reference": ref, "relative_gains": r, "transforms": [T.tolist() for T in Ts], "notes": log}
    return out


# --------------------------------------------------------------------------- focus stacking
def focus_stack(frames: Iterable, align: str = "ecc_affine", levels: int = 5, energy_sigma: float = 2.0,
                return_info: bool = False, raw_highlight: str = "clip"):
    """Laplacian-pyramid max-contrast fusion. Frames are aligned (ECC affine handles focus breathing), then for each
    pyramid level the coefficient with the highest local energy (smoothed squared magnitude over channels) wins, and
    the coarsest Gaussian level is the energy-weighted mean. Streaming: only the running winner pyramid is kept."""
    items = list(frames)
    if len(items) < 2:
        raise ValueError("focus_stack needs at least two frames")
    ref_img, _ = load_frame(items[0], raw_highlight)
    shape = ref_img.shape[:2]
    log: list[str] = []
    best_c: list[np.ndarray] | None = None
    best_e: list[np.ndarray] | None = None
    top_num = top_den = None
    depth = np.zeros(shape, np.int16)
    for i, it in enumerate(items):
        img = ref_img if i == 0 else load_frame(it, raw_highlight)[0]
        if i > 0:
            T = estimate_alignment(ref_img, img, align, log=log)
            img, valid = warp_to_ref(img, T, shape)
            img = np.where(valid[..., None] > 0, img, ref_img)   # borders: fall back to the reference
        # contrast measured on a perceptual (sqrt) copy, coefficients taken from the linear data
        lap = laplacian_pyramid(img, levels)
        meas = laplacian_pyramid(np.sqrt(np.maximum(img, 0)), levels)
        energy = [gaussian((m * m).sum(2), energy_sigma * (1.0 + 0.5 * k)) for k, m in enumerate(meas[:-1])]
        top_e = gaussian((meas[-1] ** 2).sum(2), 1.0) + 1e-8
        if best_c is None:
            best_c, best_e = [c.copy() for c in lap[:-1]], [e.copy() for e in energy]
            top_num, top_den = lap[-1] * top_e[..., None], top_e.copy()
        else:
            for k in range(len(energy)):
                win = energy[k] > best_e[k]
                best_c[k][win] = lap[k][win]
                best_e[k][win] = energy[k][win]
                if k == 0:
                    depth[win] = i
            top_num += lap[-1] * top_e[..., None]
            top_den += top_e
    out = collapse_pyramid(best_c + [top_num / top_den[..., None]])
    out = np.maximum(out, 0).astype(np.float32)
    if return_info:
        return out, {"depth_index": depth, "n": len(items), "notes": log}
    return out


# --------------------------------------------------------------------------- stacking engine (bands over memmap)
class _Store:
    """Temporary memmap of N aligned frames + validity masks; combined in row bands."""

    def __init__(self, n: int, shape: tuple[int, int]):
        self.dir = tempfile.mkdtemp(prefix="photostudio_stack_")
        h, w = shape
        self.x = np.lib.format.open_memmap(str(Path(self.dir) / "x.npy"), mode="w+", dtype=np.float32, shape=(n, h, w, 3))
        self.v = np.lib.format.open_memmap(str(Path(self.dir) / "v.npy"), mode="w+", dtype=np.uint8, shape=(n, h, w))
        self.n, self.shape = n, shape

    def put(self, i: int, img: np.ndarray, valid: np.ndarray):
        self.x[i] = img
        self.v[i] = (valid > 0.5).astype(np.uint8)

    def close(self):
        try:
            del self.x, self.v
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


def combine_band(X: np.ndarray, V: np.ndarray, method: str, kappa: float = 2.5, iters: int = 3) -> np.ndarray:
    """X (N,h,w,3) frames, V (N,h,w) validity -> (h,w,3). mean | median | kappa_sigma | max | min."""
    Vb = V.astype(bool)[..., None]
    cnt = Vb.sum(0)
    if method == "mean":
        return ((X * Vb).sum(0) / np.maximum(cnt, 1)).astype(np.float32)
    if method == "max":
        return np.where(Vb, X, -np.inf).max(0).clip(0, None).astype(np.float32)
    if method == "min":
        return np.where(Vb, X, np.inf).min(0).astype(np.float32)
    if method == "median":
        Xn = np.where(Vb, X, np.nan)
        with np.errstate(all="ignore"):
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = np.nanmedian(Xn, axis=0)
        return np.nan_to_num(res, nan=0.0).astype(np.float32)
    if method in ("kappa_sigma", "sigma_clip", "kappa"):
        keep = Vb.copy()
        for _ in range(iters):
            c = np.maximum(keep.sum(0), 1)
            mean = (X * keep).sum(0) / c
            var = (((X - mean) ** 2) * keep).sum(0) / np.maximum(c - 1, 1)
            std = np.sqrt(var)
            nk = keep & (np.abs(X - mean) <= kappa * std + 1e-9)
            nk = np.where(nk.sum(0, keepdims=True) > 0, nk, keep)   # never reject everything
            keep = nk
        return ((X * keep).sum(0) / np.maximum(keep.sum(0), 1)).astype(np.float32)
    raise ValueError(f"unknown combine method '{method}' (mean|median|kappa_sigma|max|min)")


def _combine_store(store: _Store, method: str, kappa: float, iters: int, band_rows: int | None = None) -> np.ndarray:
    h, w = store.shape
    if band_rows is None:
        band_rows = int(max(16, min(h, (160 * 1024 * 1024) // max(1, store.n * w * 3 * 4 * 3))))
    out = np.zeros((h, w, 3), np.float32)
    for y0 in range(0, h, band_rows):
        y1 = min(h, y0 + band_rows)
        out[y0:y1] = combine_band(np.asarray(store.x[:, y0:y1]), np.asarray(store.v[:, y0:y1]), method, kappa, iters)
    return out


def stack_frames(frames: Iterable, method: str = "median", align: str = "none", kappa: float = 2.5, iters: int = 3,
                 return_info: bool = False, raw_highlight: str = "clip"):
    """Generic aligned stack (mean | median | kappa_sigma | max | min) of any number of frames."""
    items = list(frames)
    first, _ = load_frame(items[0], raw_highlight)
    shape = first.shape[:2]
    log: list[str] = []
    if method in ("mean", "max", "min") and align == "none":   # streaming, no memmap
        acc = first.astype(np.float64) if method == "mean" else first.copy()
        for it in items[1:]:
            f = load_frame(it, raw_highlight)[0]
            if method == "mean":
                acc += f
            elif method == "max":
                np.maximum(acc, f, out=acc)
            else:
                np.minimum(acc, f, out=acc)
        out = (acc / len(items) if method == "mean" else acc).astype(np.float32)
        return (out, {"n": len(items), "notes": log}) if return_info else out
    store = _Store(len(items), shape)
    try:
        for i, it in enumerate(items):
            f = first if i == 0 else load_frame(it, raw_highlight)[0]
            if i > 0 and align != "none":
                f, v = warp_to_ref(f, estimate_alignment(first, f, align, log=log), shape)
            else:
                v = np.ones(shape, np.float32)
            store.put(i, f, v)
        out = _combine_store(store, method, kappa, iters)
    finally:
        store.close()
    return (out, {"n": len(items), "notes": log}) if return_info else out


def long_exposure(frames: Iterable, align: str = "none", **kw):
    """Simulated long exposure: linear mean of N frames (noise falls by sqrt(N); moving water/clouds blur)."""
    return stack_frames(frames, "mean", align, **kw)


def median_stack(frames: Iterable, align: str = "none", **kw):
    """Median stack: noise reduction and removal of moving objects (crowds, cars)."""
    return stack_frames(frames, "median", align, **kw)


def star_trails(frames: Iterable, mode: str = "lighten", fade: float = 0.0, return_info: bool = False,
                raw_highlight: str = "clip"):
    """Star trails by lighten (max) blending of unaligned frames. mode 'comet' decays the accumulated trail by `fade`
    per frame (0..1) so older positions fade like a comet tail."""
    items = list(frames)
    acc = None
    for it in items:
        f = load_frame(it, raw_highlight)[0]
        if acc is None:
            acc = f.copy()
            continue
        if mode == "comet" and fade > 0:
            acc *= (1.0 - fade)
        np.maximum(acc, f, out=acc)
    out = acc.astype(np.float32)
    return (out, {"n": len(items)}) if return_info else out


# --------------------------------------------------------------------------- astro
def detect_stars(img: np.ndarray, max_stars: int = 120, k: float = 4.5, min_sep: float = 6.0,
                 work_side: int = 2000) -> np.ndarray:
    """Star centroids (n,3): x, y (full-res px), flux, brightest first. Background = opened + blurred luminance; stars =
    local maxima above k*MAD sigma whose neighbours are not near-background (rejects hot pixels)."""
    lum = np.clip(color.luminance(img), 0, None)
    lum, s = _small(lum.astype(np.float32), work_side)
    r0 = lum - cv2.GaussianBlur(cv2.morphologyEx(lum, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17))),
                                (0, 0), 8)
    r = cv2.GaussianBlur(r0, (0, 0), 1.0)
    sigma = 1.4826 * float(np.median(np.abs(r - np.median(r)))) + 1e-9
    thr = k * sigma
    peak = (r == cv2.dilate(r, np.ones((5, 5), np.uint8))) & (r > thr)
    ys, xs = np.where(peak)
    if len(xs) == 0:
        return np.zeros((0, 3), np.float32)
    H, W = r.shape
    keep = (xs >= 3) & (xs < W - 3) & (ys >= 3) & (ys < H - 3)
    xs, ys = xs[keep], ys[keep]
    order = np.argsort(-r[ys, xs])
    pts: list[tuple[float, float, float]] = []
    for idx in order:
        x, y = int(xs[idx]), int(ys[idx])
        c0 = r0[y, x]
        ring = r0[y - 2:y + 3, x - 2:x + 3].copy()
        ring[2, 2] = 0
        if c0 > 0 and ring.sum() / 24.0 < 0.06 * c0:   # single hot pixel
            continue
        if any((x - px) ** 2 + (y - py) ** 2 < min_sep ** 2 for px, py, _ in pts):
            continue
        patch = np.clip(r[y - 2:y + 3, x - 2:x + 3], 0, None)
        tot = float(patch.sum())
        if tot <= 0:
            continue
        gy, gx = np.mgrid[-2:3, -2:3]
        cx, cy = x + float((patch * gx).sum() / tot), y + float((patch * gy).sum() / tot)
        pts.append((cx, cy, tot))
        if len(pts) >= max_stars:
            break
    a = np.array(pts, np.float32).reshape(-1, 3)
    a[:, :2] /= s
    return a


def _fit_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray | None:
    M, inl = cv2.estimateAffinePartial2D(src.astype(np.float32), dst.astype(np.float32), method=cv2.RANSAC,
                                         ransacReprojThreshold=2.0, maxIters=2000)
    return None if M is None else _to3x3(M.astype(np.float64))


def vote_align(src: np.ndarray, dst: np.ndarray, centre: tuple[float, float], max_rot_deg: float = 3.0,
               K: int = 60, tol: float = 3.0) -> tuple[np.ndarray | None, int]:
    """Star-pattern registration without astroalign: vote for the translation over a grid of small rotations, then ICP
    with a similarity fit. src/dst: (n,2) star xy. Returns (3x3 src->dst, n_matches)."""
    from scipy.spatial import cKDTree

    s, d = src[:K], dst[:K]
    if len(s) < 3 or len(d) < 3:
        return None, 0
    c = np.array(centre, np.float32)
    best = (0, 0.0, np.zeros(2, np.float32))
    for th in np.deg2rad(np.arange(-max_rot_deg, max_rot_deg + 1e-6, 0.25)):
        R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]], np.float32)
        p = (s - c) @ R.T + c
        diff = (d[None, :, :] - p[:, None, :]).reshape(-1, 2)
        keys = np.round(diff / (2 * tol)).astype(np.int64)
        kk = keys[:, 0] * 100003 + keys[:, 1]
        u, cnt = np.unique(kk, return_counts=True)
        j = int(np.argmax(cnt))
        if cnt[j] > best[0]:
            sel = kk == u[j]
            best = (int(cnt[j]), th, diff[sel].mean(0))
    if best[0] < 3:
        return None, 0
    th, t = best[1], best[2]
    R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]], np.float64)
    T = np.eye(3)
    T[:2, :2] = R
    T[:2, 2] = (c - R @ c.astype(np.float64)) + t
    tree = cKDTree(dst)
    nmatch = 0
    for tol_i in (tol * 2, tol * 1.5, tol, tol, tol):
        p = src @ T[:2, :2].T + T[:2, 2]
        dist, idx = tree.query(p, distance_upper_bound=tol_i)
        ok = np.isfinite(dist)
        if ok.sum() < 3:
            break
        M = _fit_similarity(src[ok], dst[idx[ok]])
        if M is None:
            break
        T, nmatch = M, int(ok.sum())
    return (T, nmatch) if nmatch >= 3 else (None, 0)


def star_transform(ref_stars: np.ndarray, stars: np.ndarray, shape: tuple[int, int], log: list | None = None) -> np.ndarray | None:
    """3x3 similarity mapping a frame's star positions onto the reference's. astroalign when installed, else vote+ICP."""
    if len(ref_stars) < 3 or len(stars) < 3:
        return None
    try:
        import astroalign as aa

        T, _ = aa.find_transform(stars[:, :2].astype(np.float64), ref_stars[:, :2].astype(np.float64))
        return np.asarray(T.params, np.float64)
    except Exception as exc:   # noqa: BLE001  ImportError, MaxIterError, ValueError...
        if log is not None and not isinstance(exc, ImportError):
            log.append(f"astroalign failed ({type(exc).__name__}); trying star-pattern voting")
    T, n = vote_align(stars[:, :2], ref_stars[:, :2], (shape[1] / 2, shape[0] / 2))
    return T


def _master(frames, how: str = "median") -> np.ndarray | None:
    if not frames:
        return None
    arrs = [load_frame(f)[0] for f in frames]
    if len(arrs) == 1:
        return arrs[0]
    st = np.stack(arrs)
    return (np.median(st, 0) if how == "median" else st.mean(0)).astype(np.float32)


def astro_stack(frames: Iterable, combine: str = "kappa_sigma", kappa: float = 2.5, iterations: int = 3, ref: int = 0,
                darks: Iterable | None = None, flats: Iterable | None = None, bias: Iterable | None = None,
                sky_mask=None, keep_fraction: float = 1.0, max_stars: int = 120, return_info: bool = False,
                raw_highlight: str = "clip"):
    """Star-aligned stack of a night-sky sequence.

    1. optional calibration: (light - master_dark) / (master_flat - master_bias, normalised to mean 1)
    2. star detection (+ hot-pixel rejection), similarity transform per frame (astroalign if installed, otherwise
       rotation-grid translation voting + ICP, then ECC on the blurred luminance as last resort)
    3. warp, then combine: kappa_sigma (default) | mean | median | max (lighten)
    4. `sky_mask` (array HxW, mask spec dict, or 'auto' = ai_sky): stars aligned inside the mask, the *unaligned* frame stack
       (static foreground) outside it ("freeze ground"), joined with the feathered mask.
    keep_fraction < 1 drops the frames with the fewest detected stars (clouds, bad tracking)."""
    items = list(frames)
    if len(items) < 2:
        raise ValueError("astro_stack needs at least two frames")
    log: list[str] = []
    dark = _master(list(darks) if darks is not None else [])
    flat = _master(list(flats) if flats is not None else [])
    bias_m = _master(list(bias) if bias is not None else [])
    if flat is not None and bias_m is not None and flat.shape == bias_m.shape:
        flat = flat - bias_m
    if flat is not None:
        flat = np.maximum(flat, 1e-6)
        flat = flat / flat.reshape(-1, 3).mean(0)
    def cal(a):
        return _calibrate(a, dark, flat, bias_m)

    first = cal(load_frame(items[ref], raw_highlight)[0])
    shape = first.shape[:2]
    stars_ref = detect_stars(first, max_stars)
    log.append(f"reference: {len(stars_ref)} stars")
    star_lists = {ref: stars_ref}
    for i, it in enumerate(items):          # pass 1: star lists only (frames are re-read in pass 2 to bound memory)
        if i != ref:
            star_lists[i] = detect_stars(cal(load_frame(it, raw_highlight)[0]), max_stars)
    order = sorted(star_lists, key=lambda j: -len(star_lists[j]))
    keep_n = max(2, int(round(len(items) * keep_fraction)))
    chosen = set(order[:keep_n]) | {ref}
    n = len(chosen)
    store = _Store(n, shape)
    orig = _Store(n, shape) if sky_mask is not None else None
    used, Ts = [], []
    try:
        slot = 0
        for i in range(len(items)):
            if i not in chosen:
                continue
            img = first if i == ref else cal(load_frame(items[i], raw_highlight)[0])
            if i == ref:
                T = np.eye(3)
            else:
                T = star_transform(stars_ref, star_lists[i], shape, log)
                if T is None:
                    T = estimate_alignment(first, img, "ecc", max_side=1200, log=log)
                    if np.allclose(T, np.eye(3)):
                        log.append(f"frame {i}: alignment failed, dropped")
                        continue
            w, v = warp_to_ref(img, T, shape)
            store.put(slot, w, v)
            if orig is not None:
                orig.put(slot, img, np.ones(shape, np.float32))
            used.append(i)
            Ts.append(T.tolist())
            slot += 1
        store.n = slot
        if slot < 2:
            raise RuntimeError("astro_stack: fewer than 2 frames could be aligned")
        if orig is not None:
            orig.n = slot
        sky = _combine_store(_view(store, slot), combine, kappa, iterations)
        out = sky
        if orig is not None:
            ground = _combine_store(_view(orig, slot), "median" if combine == "kappa_sigma" else combine, kappa, iterations)
            m = _resolve_sky_mask(sky_mask, first)
            out = sky * m[..., None] + ground * (1.0 - m[..., None])
    finally:
        store.close()
        if orig is not None:
            orig.close()
    out = out.astype(np.float32)
    if return_info:
        return out, {"used_frames": used, "transforms": Ts, "stars_reference": int(len(stars_ref)), "notes": log}
    return out


class _view:   # restrict a store to its first n slots
    def __init__(self, st: _Store, n: int):
        self.x, self.v, self.n, self.shape = st.x[:n], st.v[:n], n, st.shape


def _calibrate(a: np.ndarray, dark, flat, bias) -> np.ndarray:
    out = a
    if dark is not None and dark.shape == a.shape:
        out = out - dark
    elif bias is not None and bias.shape == a.shape:
        out = out - bias
    if flat is not None and flat.shape == a.shape:
        out = out / flat
    return out.astype(np.float32, copy=False)


def _resolve_sky_mask(sky_mask, ref_img: np.ndarray) -> np.ndarray:
    if isinstance(sky_mask, np.ndarray):
        m = sky_mask.astype(np.float32)
        return m if m.shape == ref_img.shape[:2] else cv2.resize(m, (ref_img.shape[1], ref_img.shape[0]))
    from .masks import build_mask
    from .pipeline.registry import Ctx

    spec = {"type": "ai_sky"} if sky_mask in (True, "auto") else sky_mask
    return build_mask(spec, ref_img, Ctx(domain="linear", source=ref_img))


# --------------------------------------------------------------------------- panorama
def panorama(frames: Iterable, mode: str = "panorama", max_side: int = 3000, return_info: bool = False,
             raw_highlight: str = "clip"):
    """cv2.Stitcher panorama (feature matching, bundle adjustment, multiband blending). OpenCV's stitcher is 8-bit, so the
    result is display-referred sRGB converted back to linear (for >8-bit use Hugin on 16-bit TIFFs)."""
    items = [load_frame(f, raw_highlight)[0] for f in frames]
    if len(items) < 2:
        raise ValueError("panorama needs at least two frames")
    u8 = []
    for a in items:
        hi = max(float(np.percentile(a[::4, ::4], 99.5)), 1e-6)
        d = color.srgb_oetf(np.clip(a / hi, 0, 1))
        d, _ = _small(d, max_side)
        u8.append(np.ascontiguousarray((d * 255 + 0.5).astype(np.uint8)[..., ::-1]))
    st = cv2.Stitcher.create(cv2.Stitcher_SCANS if mode == "scans" else cv2.Stitcher_PANORAMA)
    status, pano = st.stitch(u8)
    if status != cv2.Stitcher_OK:
        names = {1: "need more images (ERR_NEED_MORE_IMGS)", 2: "homography estimation failed", 3: "camera parameter adjustment failed"}
        raise RuntimeError(f"stitching failed: {names.get(int(status), status)}")
    out = color.srgb_eotf(pano[..., ::-1].astype(np.float32) / 255.0).astype(np.float32)
    return (out, {"size": list(out.shape[:2])}) if return_info else out
