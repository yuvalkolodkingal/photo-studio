"""Colour science: working space (linear Rec.2020 D65), transfer functions, Oklab/Oklch,
chromatic adaptation (Bradford / CAT16), CCT <-> xy, luminance helpers, gamut mapping.

Matrices follow darktable's colorspaces_inline_conversions.h / ITU-R BT.2020 / IEC 61966-2-1,
Oklab matrices from Ottosson (https://bottosson.github.io/posts/oklab/).
"""
from __future__ import annotations

import numpy as np

# --------------------------------------------------------------------------- primaries (RGB -> XYZ, D65)
REC2020_TO_XYZ = np.array([[0.6369580, 0.1446169, 0.1688810],
                           [0.2627002, 0.6779981, 0.0593017],
                           [0.0000000, 0.0280727, 1.0609851]], dtype=np.float64)
SRGB_TO_XYZ = np.array([[0.4123908, 0.3575843, 0.1804808],
                        [0.2126390, 0.7151687, 0.0721923],
                        [0.0193308, 0.1191948, 0.9505322]], dtype=np.float64)
P3_TO_XYZ = np.array([[0.4865709, 0.2656677, 0.1982173],
                      [0.2289746, 0.6917385, 0.0792869],
                      [0.0000000, 0.0451134, 1.0439444]], dtype=np.float64)
XYZ_TO_REC2020 = np.linalg.inv(REC2020_TO_XYZ)
XYZ_TO_SRGB = np.linalg.inv(SRGB_TO_XYZ)
XYZ_TO_P3 = np.linalg.inv(P3_TO_XYZ)
REC2020_TO_SRGB = XYZ_TO_SRGB @ REC2020_TO_XYZ
SRGB_TO_REC2020 = XYZ_TO_REC2020 @ SRGB_TO_XYZ
REC2020_TO_P3 = XYZ_TO_P3 @ REC2020_TO_XYZ

SPACES = {
    "srgb": (SRGB_TO_XYZ, XYZ_TO_SRGB),
    "p3": (P3_TO_XYZ, XYZ_TO_P3),
    "rec2020": (REC2020_TO_XYZ, XYZ_TO_REC2020),
}

Y_REC2020 = np.array([0.2627002, 0.6779981, 0.0593017], dtype=np.float32)
Y_SRGB = np.array([0.2126390, 0.7151687, 0.0721923], dtype=np.float32)
MIDDLE_GREY = 0.1845

D65_XY = np.array([0.31271, 0.32902])
D50_XY = np.array([0.34567, 0.35850])

BRADFORD = np.array([[0.8951, 0.2664, -0.1614],
                     [-0.7502, 1.7135, 0.0367],
                     [0.0389, -0.0685, 1.0296]])
CAT16 = np.array([[0.401288, 0.650173, -0.051461],
                  [-0.250268, 1.204414, 0.045854],
                  [-0.002079, 0.048952, 0.953127]])
VON_KRIES = np.array([[0.40024, 0.70760, -0.08081],
                      [-0.22630, 1.16532, 0.04570],
                      [0.0, 0.0, 0.91822]])
CATS = {"bradford": BRADFORD, "cat16": CAT16, "vonkries": VON_KRIES}

# --------------------------------------------------------------------------- Oklab
_OK_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                   [0.2119034982, 0.6806995451, 0.1073969566],
                   [0.0883024619, 0.2817188376, 0.6299787005]])
_OK_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                   [1.9779984951, -2.4285922050, 0.4505937099],
                   [0.0259040371, 0.7827717662, -0.8086757660]])
_OK_M2_INV = np.linalg.inv(_OK_M2)
_OK_M1_INV = np.linalg.inv(_OK_M1)
# Precomposed for linear Rec.2020 input
_OK_M1_2020 = (_OK_M1 @ REC2020_TO_SRGB).astype(np.float32)
_OK_M1_2020_INV = np.linalg.inv(_OK_M1 @ REC2020_TO_SRGB).astype(np.float32)


def apply_matrix(img: np.ndarray, m: np.ndarray) -> np.ndarray:
    """img (...,3) @ m.T in float32."""
    m = np.asarray(m, dtype=np.float32)
    out = img.reshape(-1, 3) @ m.T
    return out.reshape(img.shape).astype(np.float32, copy=False)


def srgb_oetf(x: np.ndarray) -> np.ndarray:
    """Linear -> sRGB-encoded (IEC 61966-2-1); negative values mirrored."""
    x = np.asarray(x, dtype=np.float32)
    a = np.abs(x)
    y = np.where(a <= 0.0031308, 12.92 * a, 1.055 * np.power(np.maximum(a, 1e-12), 1 / 2.4) - 0.055)
    return (np.sign(x) * y).astype(np.float32)


def srgb_eotf(x: np.ndarray) -> np.ndarray:
    """sRGB-encoded -> linear."""
    x = np.asarray(x, dtype=np.float32)
    a = np.abs(x)
    y = np.where(a <= 0.04045, a / 12.92, np.power((a + 0.055) / 1.055, 2.4))
    return (np.sign(x) * y).astype(np.float32)


_SRGB_LUT8 = srgb_eotf(np.arange(256, dtype=np.float32) / 255.0)


def srgb8_to_linear(img8: np.ndarray) -> np.ndarray:
    """256-entry inverse-sRGB LUT for 8-bit camera JPEGs."""
    return _SRGB_LUT8[img8]


def luminance(img: np.ndarray, space: str = "rec2020") -> np.ndarray:
    w = Y_REC2020 if space == "rec2020" else Y_SRGB
    return (img[..., 0] * w[0] + img[..., 1] * w[1] + img[..., 2] * w[2]).astype(np.float32)


def luma(img: np.ndarray) -> np.ndarray:
    """Display luma of encoded sRGB values (Rec.709 weights)."""
    return luminance(img, "srgb")


# --------------------------------------------------------------------------- Oklab conversions
def lin_to_oklab(img: np.ndarray, space: str = "rec2020") -> np.ndarray:
    m1 = _OK_M1_2020 if space == "rec2020" else _OK_M1.astype(np.float32)
    lms = apply_matrix(img, m1)
    lms = np.cbrt(lms)
    return apply_matrix(lms, _OK_M2)


def oklab_to_lin(lab: np.ndarray, space: str = "rec2020") -> np.ndarray:
    m1i = _OK_M1_2020_INV if space == "rec2020" else _OK_M1_INV.astype(np.float32)
    lms = apply_matrix(lab, _OK_M2_INV)
    lms = lms * lms * lms
    return apply_matrix(lms, m1i)


def oklab_to_oklch(lab: np.ndarray) -> np.ndarray:
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    C = np.hypot(a, b)
    h = np.degrees(np.arctan2(b, a)) % 360.0
    return np.stack([L, C, h], -1).astype(np.float32)


def oklch_to_oklab(lch: np.ndarray) -> np.ndarray:
    L, C, h = lch[..., 0], lch[..., 1], np.radians(lch[..., 2])
    return np.stack([L, C * np.cos(h), C * np.sin(h)], -1).astype(np.float32)


def display_to_oklab(img: np.ndarray) -> np.ndarray:
    """sRGB-encoded display values -> Oklab."""
    return lin_to_oklab(srgb_eotf(img), "srgb")


def oklab_to_display(lab: np.ndarray) -> np.ndarray:
    return srgb_oetf(oklab_to_lin(lab, "srgb"))


def to_oklch(img: np.ndarray, domain: str) -> np.ndarray:
    lab = lin_to_oklab(img, "rec2020") if domain == "linear" else display_to_oklab(img)
    return oklab_to_oklch(lab)


def from_oklch(lch: np.ndarray, domain: str) -> np.ndarray:
    lab = oklch_to_oklab(lch)
    return oklab_to_lin(lab, "rec2020") if domain == "linear" else oklab_to_display(lab)


def rgb_to_hsv(img: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(np.clip(img, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)  # H 0..360, S,V 0..1


def hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(hsv.astype(np.float32), cv2.COLOR_HSV2RGB)


def rgb_to_lab(img_display: np.ndarray) -> np.ndarray:
    """CIELAB (D65) from display sRGB, via OpenCV (float input expected 0..1)."""
    import cv2

    return cv2.cvtColor(np.clip(img_display, 0, 1).astype(np.float32), cv2.COLOR_RGB2Lab)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_Lab2RGB)


# --------------------------------------------------------------------------- CCT / chromaticity
def cct_to_xy(T: float) -> np.ndarray:
    """CCT (K) -> CIE xy. CIE daylight locus >= 4000 K, Kang (2002) Planckian fit below."""
    T = float(np.clip(T, 1667.0, 25000.0))
    if T >= 4000.0:
        if T <= 7000.0:
            x = -4.6070e9 / T**3 + 2.9678e6 / T**2 + 0.09911e3 / T + 0.244063
        else:
            x = -2.0064e9 / T**3 + 1.9018e6 / T**2 + 0.24748e3 / T + 0.237040
        y = -3.0 * x * x + 2.870 * x - 0.275
    else:
        x = -0.2661239e9 / T**3 - 0.2343589e6 / T**2 + 0.8776956e3 / T + 0.179910
        if T <= 2222.0:
            y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
        else:
            y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    return np.array([x, y])


def xy_to_uv60(xy):
    x, y = xy
    d = -2 * x + 12 * y + 3
    return np.array([4 * x / d, 6 * y / d])


def uv60_to_xy(uv):
    u, v = uv
    d = 2 * u - 8 * v + 4
    return np.array([3 * u / d, 2 * v / d])


def cct_tint_to_xy(T: float, tint: float = 0.0) -> np.ndarray:
    """CCT plus tint (Lightroom-like: +tint = magenta) -> xy. Tint is Duv * -3000 (approx.)."""
    xy = cct_to_xy(T)
    if abs(tint) < 1e-9:
        return xy
    uv = xy_to_uv60(xy)
    uv2 = xy_to_uv60(cct_to_xy(T + 50.0))
    t = uv2 - uv
    n = np.array([-t[1], t[0]])
    n /= np.linalg.norm(n) + 1e-12
    if n[1] < 0:  # make normal point "up" (greener, +Duv)
        n = -n
    duv = -tint / 3000.0
    return uv60_to_xy(uv + n * duv)


def xy_to_xyz(xy, Y: float = 1.0) -> np.ndarray:
    x, y = xy
    return np.array([x * Y / y, Y, (1 - x - y) * Y / y])


def xyz_to_xy(XYZ) -> np.ndarray:
    s = float(np.sum(XYZ))
    return np.array([XYZ[0] / s, XYZ[1] / s]) if s > 0 else D65_XY.copy()


def xy_to_cct(xy) -> float:
    """McCamy (1992) approximation, adequate for a slider readout."""
    x, y = xy
    n = (x - 0.3320) / (0.1858 - y)
    return float(449.0 * n**3 + 3525.0 * n**2 + 6823.3 * n + 5520.33)


def cat_matrix(src_xy, dst_xy=D65_XY, method: str = "cat16") -> np.ndarray:
    """3x3 XYZ->XYZ von-Kries style adaptation from src white to dst white."""
    M = CATS[method.lower()]
    ws = M @ xy_to_xyz(src_xy)
    wd = M @ xy_to_xyz(dst_xy)
    return np.linalg.inv(M) @ np.diag(wd / ws) @ M


def cat_rec2020(src_xy, dst_xy=D65_XY, method: str = "cat16") -> np.ndarray:
    """Adaptation as a single 3x3 on linear Rec.2020 data."""
    return XYZ_TO_REC2020 @ cat_matrix(src_xy, dst_xy, method) @ REC2020_TO_XYZ


# --------------------------------------------------------------------------- gamut
def gamut_compress_negatives(img: np.ndarray, threshold: float = 0.8) -> np.ndarray:
    """Pull negative / imaginary colours toward the achromatic axis (simplified ACES RGC)."""
    ach = np.max(img, axis=-1, keepdims=True)
    safe = np.maximum(np.abs(ach), 1e-8)
    d = (ach - img) / safe  # distance from achromatic: 0 = neutral, >1 = negative component
    t, lim, p = threshold, 1.4, 1.2
    s = (lim - t) / (((1 - t) / (lim - t)) ** (-p) - 1) ** (1 / p)
    x = np.maximum(d - t, 0) / s
    dc = np.where(d > t, t + (d - t) / np.power(1 + np.power(x, p), 1 / p), d)
    out = ach - dc * safe
    return np.where(ach > 0, out, 0.0).astype(np.float32)


def gamut_map_to_unit(lin_rgb: np.ndarray, space_lab: str = "srgb", iters: int = 10) -> np.ndarray:
    """Map linear RGB (in the output primaries) into [0,1]^3 by reducing Oklab chroma at constant
    L and hue (darktable v6 style), then clip residual lightness."""
    img = lin_rgb.astype(np.float32, copy=True)
    flat = img.reshape(-1, 3)
    bad = np.any((flat < -1e-5) | (flat > 1.0 + 1e-5), axis=1)
    if not bad.any():
        return np.clip(img, 0, 1)
    px = flat[bad]
    # Oklab in the given primaries via sRGB-referenced matrix (approximation for P3/2020 is fine for mapping)
    M = _OK_M1.astype(np.float32)
    lab = (np.cbrt(px @ M.T)) @ _OK_M2.T.astype(np.float32)
    L = np.clip(lab[:, 0:1], 0.0, 1.0)
    ab = lab[:, 1:3]
    lo = np.zeros((px.shape[0], 1), np.float32)
    hi = np.ones((px.shape[0], 1), np.float32)
    Mi = _OK_M1_INV.astype(np.float32)
    M2i = _OK_M2_INV.astype(np.float32)

    def to_rgb(scale):
        l2 = np.concatenate([L, ab * scale], 1)
        lms = l2 @ M2i.T
        return (lms ** 3) @ Mi.T

    for _ in range(iters):
        mid = (lo + hi) * 0.5
        rgb = to_rgb(mid)
        ok = np.all((rgb >= -1e-4) & (rgb <= 1.0 + 1e-4), axis=1, keepdims=True)
        lo = np.where(ok, mid, lo)
        hi = np.where(ok, hi, mid)
    flat[bad] = np.clip(to_rgb(lo), 0, 1)
    return np.clip(flat.reshape(img.shape), 0, 1)


def rec2020_to_output(lin2020: np.ndarray, space: str = "srgb") -> np.ndarray:
    """Linear Rec.2020 -> linear output primaries (unclipped)."""
    if space == "rec2020":
        return lin2020
    m = REC2020_TO_SRGB if space == "srgb" else REC2020_TO_P3
    return apply_matrix(lin2020, m)


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0 + 1e-12), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def delta_e_oklab(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.linalg.norm(a - b, axis=-1)


def delta_e2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    from skimage.color import deltaE_ciede2000

    return deltaE_ciede2000(lab1, lab2)
