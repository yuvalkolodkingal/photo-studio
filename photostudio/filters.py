"""Low-level filters shared by ops and masks: guided filter, pyramids, blur helpers, noise."""
from __future__ import annotations

import cv2
import numpy as np


def gaussian(img: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return img
    return cv2.GaussianBlur(img.astype(np.float32), (0, 0), float(sigma), borderType=cv2.BORDER_REFLECT)


def box(img: np.ndarray, r: int) -> np.ndarray:
    r = max(1, int(r))
    return cv2.boxFilter(img.astype(np.float32), -1, (2 * r + 1, 2 * r + 1), borderType=cv2.BORDER_REFLECT)


def guided_filter(guide: np.ndarray, src: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """He et al. guided filter. Uses cv2.ximgproc when available, otherwise a box-filter implementation.
    guide: HxW or HxWx3 float32; src: HxW or HxWxC float32."""
    guide = guide.astype(np.float32)
    src = src.astype(np.float32)
    radius = max(1, int(radius))
    try:
        return cv2.ximgproc.guidedFilter(guide, src, radius, eps)
    except Exception:
        pass
    if guide.ndim == 3:
        guide = guide.mean(axis=2)
    mean_I = box(guide, radius)
    mean_p = box(src, radius)
    if src.ndim == 3:
        mean_I3 = mean_I[..., None]
        cov = box(guide[..., None] * src, radius) - mean_I3 * mean_p
        var = box(guide * guide, radius) - mean_I * mean_I
        a = cov / (var[..., None] + eps)
        b = mean_p - a * mean_I3
        return box(a, radius) * guide[..., None] + box(b, radius)
    cov = box(guide * src, radius) - mean_I * mean_p
    var = box(guide * guide, radius) - mean_I * mean_I
    a = cov / (var + eps)
    b = mean_p - a * mean_I
    return box(a, radius) * guide + box(b, radius)


def fast_guided(guide: np.ndarray, src: np.ndarray, radius: int, eps: float, max_px: int = 1024) -> np.ndarray:
    """Guided filter computed at reduced size for large radii, coefficients upsampled (He 2015)."""
    h, w = src.shape[:2]
    s = min(1.0, max_px / max(h, w))
    if s >= 0.99 or radius < 4:
        return guided_filter(guide, src, radius, eps)
    small = (max(1, int(w * s)), max(1, int(h * s)))
    g = cv2.resize(guide.astype(np.float32), small, interpolation=cv2.INTER_AREA)
    p = cv2.resize(src.astype(np.float32), small, interpolation=cv2.INTER_AREA)
    gg = g.mean(axis=2) if g.ndim == 3 else g
    r = max(1, int(radius * s))
    mean_I, mean_p = box(gg, r), box(p, r)
    if p.ndim == 3:
        cov = box(gg[..., None] * p, r) - mean_I[..., None] * mean_p
        var = box(gg * gg, r) - mean_I * mean_I
        a = cov / (var[..., None] + eps)
        b = mean_p - a * mean_I[..., None]
    else:
        cov = box(gg * p, r) - mean_I * mean_p
        var = box(gg * gg, r) - mean_I * mean_I
        a = cov / (var + eps)
        b = mean_p - a * mean_I
    A = cv2.resize(box(a, r), (w, h), interpolation=cv2.INTER_LINEAR)
    B = cv2.resize(box(b, r), (w, h), interpolation=cv2.INTER_LINEAR)
    G = guide.mean(axis=2) if guide.ndim == 3 else guide
    if A.ndim == 3:
        return A * G[..., None] + B
    return A * G + B


def gaussian_pyramid(img: np.ndarray, levels: int) -> list[np.ndarray]:
    pyr = [img.astype(np.float32)]
    for _ in range(levels - 1):
        if min(pyr[-1].shape[:2]) < 8:
            break
        pyr.append(cv2.pyrDown(pyr[-1]))
    return pyr


def laplacian_pyramid(img: np.ndarray, levels: int) -> list[np.ndarray]:
    g = gaussian_pyramid(img, levels)
    lap = []
    for i in range(len(g) - 1):
        up = cv2.pyrUp(g[i + 1], dstsize=(g[i].shape[1], g[i].shape[0]))
        lap.append(g[i] - up)
    lap.append(g[-1])
    return lap


def collapse_pyramid(lap: list[np.ndarray]) -> np.ndarray:
    img = lap[-1]
    for lvl in reversed(lap[:-1]):
        img = cv2.pyrUp(img, dstsize=(lvl.shape[1], lvl.shape[0])) + lvl
    return img


def noise_field(shape, sigma_px: float = 1.0, seed: int = 0, octaves: int = 1) -> np.ndarray:
    """Smooth zero-mean, unit-std noise (cheap Perlin substitute: blurred white noise octaves)."""
    rng = np.random.default_rng(seed)
    h, w = shape[:2]
    acc = np.zeros((h, w), np.float32)
    amp = 1.0
    for o in range(octaves):
        n = rng.standard_normal((h, w)).astype(np.float32)
        s = sigma_px * (2 ** o)
        if s > 0.3:
            n = gaussian(n, s)
        n /= n.std() + 1e-8
        acc += amp * n
        amp *= 0.5
    acc /= acc.std() + 1e-8
    return acc


def motion_kernel(length: float, angle_deg: float) -> np.ndarray:
    L = max(3, int(round(length)) | 1)
    k = np.zeros((L, L), np.float32)
    c = L // 2
    a = np.radians(angle_deg)
    dx, dy = np.cos(a), np.sin(a)
    for t in np.linspace(-c, c, L * 2):
        x, y = int(round(c + t * dx)), int(round(c + t * dy))
        if 0 <= x < L and 0 <= y < L:
            k[y, x] = 1.0
    return k / k.sum()


def scale_param(px_at_2000: float, img: np.ndarray) -> float:
    """Scale a pixel parameter specified for a 2000 px long edge to the current image size."""
    return float(px_at_2000) * max(img.shape[:2]) / 2000.0
