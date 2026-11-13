"""Canon Picture Style calibration: fit a tone curve + 3x3 matrix from CR3+JPEG pairs.

Canon publishes no Picture Style curves, so the only principled emulation is to *measure* it (spec section on
Canon colour science): develop each CR3 with the default pipeline, compare with the camera's own JPEG of the same
frame, and least-squares fit

  1. a luminance tone curve (display -> display, monotone, applied by the ``tone_curve`` op in luma mode), then
  2. a 3x3 colour matrix on luminance-normalised linear Rec.2020 data with every row summing to 1 (neutrals stay
     neutral; applied by the ``channel_mixer`` op ahead of the tone mapper).

Pixels are sampled from heavily downsampled frames; clipped and near-black pixels are excluded. The result is a
``picture_style`` preset (``fit_<name>``) saved in the user preset directory. A residual 33^3 LUT (the spec's step
c) is intentionally not fitted here: with 50-200 pairs a smooth matrix + curve is the robust part.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import color
from .util import imread_display

LUMA = color.Y_SRGB


def find_pairs(directory: str | Path, style: str | int | None = None, limit: int = 200) -> list[tuple[Path, Path]]:
    """(raw, companion JPEG) pairs in a directory, optionally only frames shot with a given Picture Style
    (name or code, from the JPEG's MakerNote)."""
    from .decode import companion_jpeg
    from .modes import PICTURE_STYLE
    from .util import RAW_EXTS

    want = None
    if style is not None:
        inv = {v: k for k, v in PICTURE_STYLE.items()}
        want = inv[style] if isinstance(style, str) and style in inv else (int(style, 0) if isinstance(style, str) else style)
    pairs = []
    for raw in sorted(Path(directory).rglob("*")):
        if raw.suffix.lower() not in RAW_EXTS:
            continue
        jpg = companion_jpeg(raw)
        if jpg is None:
            continue
        if want is not None:
            try:
                from .metadata import read_metadata
                if read_metadata(jpg).get("picture_style") != want:
                    continue
            except Exception:  # noqa: BLE001
                pass
        pairs.append((raw, jpg))
        if len(pairs) >= limit:
            break
    return pairs


def _small(img: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    import cv2

    return cv2.resize(img, size, interpolation=cv2.INTER_AREA)


def _isotonic(y: np.ndarray) -> np.ndarray:
    """Monotone non-decreasing fit (pool adjacent violators)."""
    y = y.astype(np.float64).copy()
    w = np.ones_like(y)
    i = 0
    vals, wts, counts = [], [], []
    for yi, wi in zip(y, w):
        vals.append(yi), wts.append(wi), counts.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            v = (vals[-2] * wts[-2] + vals[-1] * wts[-1]) / (wts[-2] + wts[-1])
            ww = wts[-2] + wts[-1]
            c = counts[-2] + counts[-1]
            vals[-2:], wts[-2:], counts[-2:] = [v], [ww], [c]
    del i
    return np.concatenate([np.full(c, v) for v, c in zip(vals, counts)])


def fit_tone_curve(base_y: np.ndarray, ref_y: np.ndarray, n_points: int = 17, n_bins: int = 48) -> list[list[float]]:
    """Monotone luminance curve base -> ref from binned medians; returns ``n_points`` control points on [0, 1]."""
    order = np.argsort(base_y)
    bx, ry = base_y[order], ref_y[order]
    edges = np.linspace(0, len(bx), n_bins + 1).astype(int)
    xs = np.array([bx[a:b].mean() for a, b in zip(edges[:-1], edges[1:]) if b > a])
    ys = np.array([np.median(ry[a:b]) for a, b in zip(edges[:-1], edges[1:]) if b > a])
    ys = _isotonic(ys)
    xs = np.concatenate([[0.0], xs, [1.0]])
    ys = np.concatenate([[min(ys[0], ys[0] * 0.5)], ys, [max(ys[-1], min(1.0, ys[-1]))]])
    # light smoothing keeps the curve free of bin noise
    k = np.array([1, 2, 1], float) / 4.0
    pad = np.pad(ys, 1, mode="edge")
    ys = np.convolve(pad, k, mode="valid")
    ys = _isotonic(np.clip(ys, 0, 1))
    grid = np.linspace(0.0, 1.0, n_points)
    out = np.interp(grid, xs, ys)
    out = np.maximum.accumulate(np.clip(out, 0, 1))
    return [[round(float(x), 5), round(float(y), 5)] for x, y in zip(grid, out)]


def fit_matrix(x_lin: np.ndarray, y_lin: np.ndarray, ridge: float = 1e-3) -> np.ndarray:
    """3x3 matrix with unit row sums mapping luminance-normalised ``x_lin`` (Nx3) to ``y_lin`` (least squares,
    ridge towards identity). Both in linear Rec.2020."""
    yx = (x_lin @ color.Y_REC2020)[:, None]
    yy = (y_lin @ color.Y_REC2020)[:, None]
    keep = (yx[:, 0] > 0.01) & (yy[:, 0] > 0.01)           # ratios of near-black pixels are all noise
    if keep.sum() >= 8:
        x_lin, y_lin, yx, yy = x_lin[keep], y_lin[keep], yx[keep], yy[keep]
    xn, yn = x_lin / np.maximum(yx, 1e-6), y_lin / np.maximum(yy, 1e-6)
    w = np.clip(yx[:, 0], 0.01, 1.0) ** 0.5                 # bright pixels carry more weight (less noisy ratios)
    M = np.zeros((3, 3))
    for c in range(3):
        # m = [a, b, 1-a-b]  ->  yn_c - xn_2 = a (xn_0 - xn_2) + b (xn_1 - xn_2)
        A = np.stack([xn[:, 0] - xn[:, 2], xn[:, 1] - xn[:, 2]], 1) * w[:, None]
        t = (yn[:, c] - xn[:, 2]) * w
        # ridge toward the identity row
        ident = np.eye(3)[c]
        a0, b0 = ident[0], ident[1]
        lhs = A.T @ A + ridge * len(A) * np.eye(2)
        rhs = A.T @ t + ridge * len(A) * np.array([a0, b0])
        a, b = np.linalg.solve(lhs, rhs)
        M[c] = [a, b, 1.0 - a - b]
    return M


def fit_picture_style(pairs: list[tuple[str | Path, str | Path]], name: str = "custom", size: tuple[int, int] = (96, 64),
                      n_points: int = 17, long_edge: int = 640) -> dict:
    """Fit a Picture-Style emulation from CR3+JPEG pairs. Returns ``{preset, curve, matrix, residual, pairs}``."""
    from .pipeline import engine

    if len(pairs) < 1:
        raise ValueError("need at least one CR3+JPEG pair")
    stack = engine.default_stack("raw")
    xs_lin, ys_lin, bys, rys, bgr = [], [], [], [], []
    used = 0
    for raw, jpg in pairs:
        try:
            base = engine.render(raw, stack, long_edge=long_edge, use_cache=False)
            lin = engine.load_source(raw, long_edge).image
            ref = imread_display(jpg)
        except Exception:  # noqa: BLE001 - skip unreadable pairs, report count
            continue
        sz = size if base.shape[1] >= base.shape[0] else (size[1], size[0])
        b_s, r_s, l_s = _small(base, sz), _small(ref, sz), _small(np.asarray(lin), sz)
        r_lin = color.apply_matrix(color.srgb_eotf(r_s), color.SRGB_TO_REC2020)
        yr = r_s @ LUMA
        ok = ((yr > 0.03) & (yr < 0.97) & ((b_s @ LUMA) < 0.97)).reshape(-1)
        bys.append((b_s @ LUMA).reshape(-1))
        rys.append(yr.reshape(-1))
        bgr.append(b_s.reshape(-1, 3))
        xs_lin.append(l_s.reshape(-1, 3)[ok])
        ys_lin.append(r_lin.reshape(-1, 3)[ok])
        used += 1
    if not used:
        raise ValueError("no readable CR3+JPEG pairs")
    by, ry = np.concatenate(bys), np.concatenate(rys)
    X, Y = np.concatenate(xs_lin), np.concatenate(ys_lin)
    curve = fit_tone_curve(by, ry, n_points)
    M = fit_matrix(X.astype(np.float64), Y.astype(np.float64))
    # residuals: base render vs reference, before/after the fitted curve (luma domain) and matrix chroma
    cx, cy = np.array(curve).T
    after_y = np.interp(by, cx, cy)
    res = {"luma_mae_before": float(np.abs(by - ry).mean()), "luma_mae_after": float(np.abs(after_y - ry).mean()),
           "samples": int(len(by)), "colour_samples": int(len(X))}
    yx = (X @ color.Y_REC2020)[:, None]
    yy = (Y @ color.Y_REC2020)[:, None]
    ok = (yx[:, 0] > 0.01) & (yy[:, 0] > 0.01)
    xn, yn = X[ok] / yx[ok], Y[ok] / yy[ok]
    res["chroma_err_before"] = float(np.abs(xn - yn).mean())
    res["chroma_err_after"] = float(np.abs(xn @ M.T - yn).mean())
    preset = {
        "name": f"fit_{name}", "kind": "picture_style", "title": f"Fitted Picture Style: {name}",
        "description": f"FITTED from {used} CR3+JPEG pairs (tone curve + unit-row-sum 3x3 matrix, least squares). "
                       "Approximation of the camera's own rendering; re-fit after changing the pipeline defaults.",
        "tags": ["fitted", "picture-style"],
        "stack": [
            {"op": "channel_mixer", "params": {"matrix": [[round(float(v), 5) for v in row] for row in M]}},
            {"op": "tone_curve", "params": {"points": curve, "mode": "luma"}},
        ],
        "options": {"fitted_pairs": used, "residual": res},
    }
    return {"preset": preset, "pairs_used": used, "pairs_given": len(pairs), "residual": res,
            "matrix": M.round(5).tolist(), "curve": curve}
