"""Decoding: rawpy for raw (decode-only: black/white, WB, highlight unclip, demosaic, metadata, previews),
inverse-sRGB for camera JPEGs. Both produce float32 linear Rec.2020 (D65) scene data.

Raw path (spec section 1):
    postprocess(gamma=(1,1), no_auto_bright=True, output_bps=16, use_camera_wb=True,
                output_color=raw, highlight_mode=Ignore, demosaic=DHT, user_flip=0)
With highlight_mode=Ignore LibRaw normalises WB by the *largest* multiplier so no channel clips; we undo
that with scale = max(mul)/min(mul) so the least-amplified channel reaches 1.0 at sensor white. The
per-channel clip level after scaling is mul[c]/min(mul) (used by highlight reconstruction).
The camera matrix is applied in float here (LibRaw's integer CLIP would truncate negatives).
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import color
from .util import JPEG_EXTS, RAW_EXTS, MissingDependency, imread_display, lazy_import

DEMOSAIC = {"linear": "LINEAR", "vng": "VNG", "ppg": "PPG", "ahd": "AHD", "dcb": "DCB", "dht": "DHT", "aahd": "AAHD"}


@dataclass
class Decoded:
    image: np.ndarray                  # float32 HxWx3 linear Rec.2020
    kind: str                          # 'raw' | 'jpeg' | 'image'
    path: str
    meta: dict = field(default_factory=dict)
    clip_levels: np.ndarray | None = None   # per-channel clip in camera RGB after WB (raw only)
    raw_info: dict = field(default_factory=dict)


def is_raw(path: str | Path) -> bool:
    return Path(path).suffix.lower() in RAW_EXTS


# --------------------------------------------------------------------------- raw opening (patchable)
def _rawpy_imread(path: str):
    rawpy = lazy_import("rawpy", "raw decoding")
    return rawpy.imread(path)


def open_raw(path: str | Path):
    """Return a rawpy.RawPy-compatible object. `.npz` files are synthetic raws (see photostudio.testing)."""
    p = str(path)
    if p.lower().endswith(".npz"):
        from .testing import FakeRawPy

        return FakeRawPy.load(p)
    return _rawpy_imread(p)


def _enum(name: str, member: str):
    try:
        import rawpy

        return getattr(getattr(rawpy, name), member)
    except Exception:
        return member  # FakeRawPy accepts strings


def raw_dump(path: str | Path) -> dict:
    """Phase-0 golden dump: black/white levels, multipliers, matrices, sizes, crops."""
    with open_raw(path) as raw:
        info: dict[str, Any] = {}
        for attr in ("black_level_per_channel", "white_level", "camera_white_level_per_channel",
                     "camera_whitebalance", "daylight_whitebalance", "color_desc", "num_colors",
                     "raw_pattern", "rgb_xyz_matrix", "color_matrix", "raw_type"):
            try:
                v = getattr(raw, attr)
                if isinstance(v, np.ndarray):
                    v = v.tolist()
                elif isinstance(v, bytes):
                    v = v.decode(errors="replace")
                elif v is not None and not isinstance(v, (int, float, str, list, tuple)):
                    v = str(v)
                info[attr] = v
            except Exception as exc:  # noqa: BLE001
                info[attr] = f"<unavailable: {exc}>"
        try:
            s = raw.sizes
            info["sizes"] = {k: getattr(s, k) for k in ("raw_height", "raw_width", "height", "width",
                                                        "top_margin", "left_margin", "iheight", "iwidth", "flip")}
        except Exception:
            pass
        try:
            info["raw_inset_crops"] = [str(c) for c in raw.sizes.raw_inset_crops]
        except Exception:
            pass
        try:
            import rawpy

            info["libraw_version"] = list(rawpy.libraw_version)
            info["rawpy_version"] = rawpy.__version__
        except Exception:
            pass
        checks = []
        wl = info.get("white_level")
        if isinstance(wl, (int, float)) and not (4000 <= wl <= 65535):
            checks.append(f"white_level {wl} outside plausible 12-16 bit range")
        bl = info.get("black_level_per_channel")
        if isinstance(bl, list) and isinstance(wl, (int, float)) and any(b >= wl * 0.25 for b in bl):
            checks.append("black level suspiciously high")
        info["sanity_warnings"] = checks
        return info


def camera_to_rec2020(rgb_xyz_matrix: np.ndarray) -> np.ndarray:
    """LibRaw cam_xyz (XYZ D65 -> camera, rows R,G,B[,G2]) -> 3x3 camera RGB (WB applied) -> linear Rec.2020.
    Rows of (cam_xyz @ xyz_from_rec2020) are normalised to sum 1 so that Rec.2020 white maps to camera
    (1,1,1), mirroring LibRaw's own pseudo-inverse construction."""
    cam_xyz = np.asarray(rgb_xyz_matrix, dtype=np.float64)[:3, :3]
    if not np.any(cam_xyz):
        return np.eye(3)
    cam_2020 = cam_xyz @ color.REC2020_TO_XYZ
    cam_2020 = cam_2020 / cam_2020.sum(axis=1, keepdims=True)
    return np.linalg.inv(cam_2020)


def decode_raw(path: str | Path, demosaic: str = "dht", wb: str | list = "camera", half_size: bool = False,
               highlight: str = "reconstruct", meta: dict | None = None) -> Decoded:
    """Decode a raw file to linear Rec.2020. wb: camera | daylight | [r,g,b,g2] multipliers."""
    with open_raw(path) as raw:
        cam_wb = np.asarray(raw.camera_whitebalance, dtype=np.float64)
        day_wb = np.asarray(raw.daylight_whitebalance, dtype=np.float64)
        if isinstance(wb, (list, tuple, np.ndarray)):
            mul = np.asarray(wb, dtype=np.float64)
        elif wb == "daylight":
            mul = day_wb
        else:
            mul = cam_wb
        if mul.size < 3 or not np.all(np.isfinite(mul[:3])) or mul[:3].min() <= 0:
            mul = day_wb if day_wb[:3].min() > 0 else np.array([2.0, 1.0, 1.5, 1.0])
        if mul.size == 3:
            mul = np.append(mul, mul[1])
        if mul[3] == 0:
            mul[3] = mul[1]
        kw = dict(gamma=(1, 1), no_auto_bright=True, output_bps=16, user_wb=list(map(float, mul[:4])),
                  output_color=_enum("ColorSpace", "raw"), highlight_mode=_enum("HighlightMode", "Ignore"),
                  demosaic_algorithm=_enum("DemosaicAlgorithm", DEMOSAIC.get(demosaic, "DHT")),
                  half_size=half_size, user_flip=0)
        try:
            out16 = raw.postprocess(**kw)
        except Exception:
            kw["demosaic_algorithm"] = _enum("DemosaicAlgorithm", "AHD")
            out16 = raw.postprocess(**kw)
        m3 = mul[:3] / mul[:3].min()
        scale = float(m3.max())
        cam = out16.astype(np.float32) * (scale / 65535.0)
        clip = m3.astype(np.float32)  # channel c saturates at m3[c] after scaling
        info = {"wb_multipliers": mul.tolist(), "scale": scale, "demosaic": demosaic, "half_size": half_size}
        try:
            M = camera_to_rec2020(raw.rgb_xyz_matrix)
        except Exception:
            M = color.SRGB_TO_REC2020
        info["cam_to_rec2020"] = np.round(M, 6).tolist()
        try:
            flip = int(raw.sizes.flip)
        except Exception:
            flip = 0
    if highlight != "none":
        from .pipeline.ops.basic import reconstruct_highlights

        cam = reconstruct_highlights(cam, clip, method=highlight)
    lin = color.apply_matrix(cam, M)
    lin = _apply_flip(lin, flip)
    from .metadata import read_metadata

    md = meta if meta is not None else read_metadata(path)
    return Decoded(lin, "raw", str(path), md, clip, info)


def _apply_flip(img: np.ndarray, flip: int) -> np.ndarray:
    # LibRaw flip: 0 none, 3 180, 5 90 CCW, 6 90 CW
    if flip == 3:
        return np.ascontiguousarray(img[::-1, ::-1])
    if flip == 5:
        return np.ascontiguousarray(np.rot90(img, 1))
    if flip == 6:
        return np.ascontiguousarray(np.rot90(img, -1))
    return img


def decode_jpeg(path: str | Path, meta: dict | None = None) -> Decoded:
    """8-bit display JPEG -> linear Rec.2020 via 256-entry inverse sRGB LUT. Enters after raw stages."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        a8 = np.asarray(im, dtype=np.uint8)
    lin_srgb = color.srgb8_to_linear(a8)
    lin = color.apply_matrix(lin_srgb, color.SRGB_TO_REC2020)
    from .metadata import read_metadata

    md = meta if meta is not None else read_metadata(path)
    return Decoded(lin, "jpeg", str(path), md)


def decode_image(path: str | Path, meta: dict | None = None) -> Decoded:
    disp = imread_display(path)
    lin = color.apply_matrix(color.srgb_eotf(disp), color.SRGB_TO_REC2020)
    from .metadata import read_metadata

    md = meta if meta is not None else read_metadata(path)
    return Decoded(lin, "image", str(path), md)


def decode(path: str | Path, **raw_kw) -> Decoded:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    ext = p.suffix.lower()
    if ext in RAW_EXTS:
        return decode_raw(p, **raw_kw)
    if ext in JPEG_EXTS:
        return decode_jpeg(p)
    return decode_image(p)


# --------------------------------------------------------------------------- previews
def embedded_preview(path: str | Path) -> np.ndarray | None:
    """Largest embedded JPEG (for CR3: the full-size track-1 render) as display float32, or None.
    Small PRVW/THMB tiers need ExifTool (-b -PreviewImage / -ThumbnailImage)."""
    p = Path(path)
    if p.suffix.lower() not in RAW_EXTS:
        return None
    try:
        with open_raw(p) as raw:
            th = raw.extract_thumb()
    except Exception:
        return None
    fmt = str(getattr(th, "format", ""))
    if "JPEG" in fmt.upper():
        from PIL import Image

        with Image.open(io.BytesIO(th.data)) as im:
            return np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    data = np.asarray(th.data)
    if data.ndim == 3:
        return data.astype(np.float32) / 255.0
    return None


def small_preview_exiftool(path: str | Path, tag: str = "PreviewImage") -> np.ndarray | None:
    from .metadata import _et

    et = _et()
    if et is None:
        return None
    try:
        res = et.execute("-b", f"-{tag}", str(path), raw_bytes=True)
    except TypeError:
        return None
    except Exception:
        return None
    if not res:
        return None
    from PIL import Image

    try:
        with Image.open(io.BytesIO(res)) as im:
            return np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    except Exception:
        return None


def companion_jpeg(path: str | Path) -> Path | None:
    p = Path(path)
    for ext in (".JPG", ".jpg", ".JPEG", ".jpeg"):
        c = p.with_suffix(ext)
        if c.exists() and c != p:
            return c
    return None


def preview_display(path: str | Path, long_edge: int = 1024) -> np.ndarray:
    """Fast display preview: companion/embedded JPEG for raws (never render raws for the grid)."""
    from .util import resize_long

    p = Path(path)
    img = None
    if p.suffix.lower() in RAW_EXTS:
        cj = companion_jpeg(p)
        if cj is not None:
            img = imread_display(cj)
        else:
            img = embedded_preview(p)
        if img is None:
            d = decode_raw(p, half_size=True, highlight="clip")
            img = color.srgb_oetf(np.clip(color.apply_matrix(d.image * 2 ** 0.7, color.REC2020_TO_SRGB), 0, 1))
    else:
        img = imread_display(p)
    return resize_long(img, long_edge) if max(img.shape[:2]) > long_edge else img


def raw_clipping(path: str | Path) -> dict:
    """Clipping fractions measured on the raw mosaic per CFA channel (never on the JPEG)."""
    try:
        with open_raw(path) as raw:
            mos = np.asarray(raw.raw_image_visible)
            pat = np.asarray(raw.raw_colors_visible)
            try:
                wl = np.asarray(raw.camera_white_level_per_channel, dtype=np.float64)
                if wl is None or wl.size == 0 or not np.all(wl > 0):
                    raise ValueError
            except Exception:
                wl = np.full(4, float(raw.white_level))
            bl = np.asarray(raw.black_level_per_channel, dtype=np.float64)
    except MissingDependency:
        raise
    except Exception as exc:
        return {"error": str(exc)}
    res = {}
    names = ["R", "G", "B", "G2"]
    hi_any = np.zeros(mos.shape, bool)
    for c in range(4):
        sel = pat == c
        if not sel.any():
            continue
        v = mos[sel].astype(np.float64)
        thr = bl[c] + (wl[c] - bl[c]) * 0.995
        hi = float((v >= thr).mean())
        lo = float((v <= bl[c] + 2).mean())
        res[names[c]] = {"clip_hi": round(hi, 5), "clip_lo": round(lo, 5)}
        hi_any |= sel & (mos >= thr)
    res["clip_hi"] = float(max(r["clip_hi"] for r in res.values()))
    res["clip_lo"] = float(np.mean([r["clip_lo"] for r in res.values() if isinstance(r, dict)]))
    return res


def write_meta_npz(meta: dict) -> str:
    return json.dumps(meta)
