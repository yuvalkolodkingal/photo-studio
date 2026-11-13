"""Export: develop -> resize -> output sharpen -> watermark/border -> encode with ICC + metadata.

Presets live in ``presets/export/*.yaml`` (``options`` mapping, see below). ``export_image`` is the one-file
entry point (CONTRACT), ``export_batch`` runs many files through a process pool, ``save_array`` encodes an
already-rendered display array, ``build_contact_sheet`` makes a Pillow contact sheet.

Export options (preset ``options`` or keyword overrides)::

    format: jpeg|png|tiff|webp|avif|heif|jxl      bits: 8|16 (png/tiff)        quality: 1..100 (jpeg/webp/avif)
    subsampling: "4:4:4"|"4:2:2"|"4:2:0"   progressive: true   optimize: true   dpi: 300   compression: zlib|lzw|none
    icc: srgb|p3|adobe|none        resize: long_edge | short_edge | megapixels | width+height with fit: contain|pad|cover|exact
    upscale: false   pad_color: "#ffffff"   sharpen: {target: screen|print_matte|print_glossy, amount, radius}
    watermark: {text|image, position, opacity, size, margin, color}      border: {size, color, inner}
    metadata: all|copyright|none   copyright: "..."   artist: "..."   name: "{stem}_{preset}"   dither: false
"""
from __future__ import annotations

import copy
import os
import re
import string
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import icc as _icc
from .presets import PresetError, load_preset
from .util import IMAGE_EXTS, lazy_import, to_uint8

FORMATS = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "tiff": ".tif", "tif": ".tif", "webp": ".webp",
           "avif": ".avif", "heif": ".heic", "heic": ".heic", "jxl": ".jxl"}
EXT_FORMAT = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".tif": "tiff", ".tiff": "tiff", ".webp": "webp",
              ".avif": "avif", ".heic": "heif", ".heif": "heif", ".jxl": "jxl"}
SUBSAMPLING = {"4:4:4": 0, "444": 0, "4:2:2": 1, "422": 1, "4:2:0": 2, "420": 2}
SPACE_FOR_ICC = {"srgb": "srgb", "p3": "p3", "adobe": "adobe"}


class ExportError(RuntimeError):
    pass


# =============================================================================== quantise / encode helpers
def _quantise(img: np.ndarray, bits: int, dither: bool = False, seed: int = 0) -> np.ndarray:
    img = np.clip(np.asarray(img, np.float32), 0, 1)
    if bits == 16:
        return (img * 65535.0 + 0.5).astype(np.uint16)
    if dither:
        rng = np.random.default_rng(seed)
        noise = (rng.random(img.shape[:2], dtype=np.float32) + rng.random(img.shape[:2], dtype=np.float32) - 1.0)
        return np.clip(np.floor(img * 255.0 + 0.5 + noise[..., None]), 0, 255).astype(np.uint8)
    return to_uint8(img)


def _fmt_for(out_path: Path, fmt: str | None) -> str:
    if fmt:
        f = fmt.lower().lstrip(".")
        if f not in FORMATS:
            raise ExportError(f"unknown export format '{fmt}' (jpeg, png, tiff, webp, avif, heif, jxl)")
        return EXT_FORMAT[FORMATS[f]]
    ext = out_path.suffix.lower()
    if ext not in EXT_FORMAT:
        raise ExportError(f"cannot infer an export format from '{out_path.name}'; pass fmt=")
    return EXT_FORMAT[ext]


# ----------------------------------------------------------------------------- EXIF fallback (no exiftool)
_TAG_ORIENT, _TAG_GPS, _TAG_MAKERNOTE = 0x0112, 0x8825, 0x927C
_SERIAL_TAGS = (0xA431, 0xA435, 0xC62F)
_PIXEL_DIM_TAGS = (0xA002, 0xA003)


def _exif_bytes(src: str | Path | None, keep: str, copyright_: str | None = None, artist: str | None = None) -> bytes | None:
    """Build EXIF bytes with Pillow only. JPEG/TIFF sources: carry their EXIF (orientation reset, pixel-size
    tags dropped, GPS/serials removed for 'copyright'). Raw sources: rebuild a compact EXIF from the normalised
    metadata. keep: all | copyright | none."""
    from PIL import Image

    ex = Image.Exif()
    if src is not None and keep != "none":
        p = Path(src)
        loaded = False
        if p.suffix.lower() in IMAGE_EXTS and p.exists():
            try:
                with Image.open(p) as im:
                    ex = im.getexif()
                    sub = ex.get_ifd(0x8769) if hasattr(ex, "get_ifd") else {}
                    loaded = bool(len(ex) or len(sub))
            except Exception:  # noqa: BLE001
                ex = Image.Exif()
        if not loaded:
            try:
                from .metadata import read_metadata
                md = read_metadata(p)
            except Exception:  # noqa: BLE001
                md = {}
            tagmap = {"make": 0x010F, "model": 0x0110}
            for k, t in tagmap.items():
                if md.get(k):
                    ex[t] = str(md[k])
            if md.get("datetime"):
                ex[0x0132] = md["datetime"].replace("T", " ").replace("-", ":")
            sub = {}
            if md.get("datetime"):
                sub[0x9003] = md["datetime"].replace("T", " ").replace("-", ":")
            for k, t in (("shutter", 0x829A), ("aperture", 0x829D), ("focal", 0x920A)):
                if md.get(k):
                    sub[t] = float(md[k])
            if md.get("iso"):
                sub[0x8827] = int(md["iso"])
            if md.get("lens"):
                sub[0xA434] = str(md["lens"])
            if sub:
                ex.get_ifd(0x8769).update(sub)
        for t in _PIXEL_DIM_TAGS:
            for ifd in (ex, ex.get_ifd(0x8769)):
                if t in ifd:
                    del ifd[t]
        ex[_TAG_ORIENT] = 1
        if keep == "copyright":
            for ifd in (ex, ex.get_ifd(0x8769)):
                for t in _SERIAL_TAGS + (_TAG_MAKERNOTE,):
                    if t in ifd:
                        del ifd[t]
            if _TAG_GPS in ex:
                del ex[_TAG_GPS]
    if copyright_:
        ex[0x8298] = copyright_
    if artist:
        ex[0x013B] = artist
    if not len(ex):
        return None
    try:
        return ex.tobytes()
    except Exception:  # noqa: BLE001 - malformed foreign EXIF: drop it rather than fail the export
        return None


def _post_metadata(out: Path, src: str | Path | None, keep: str, copyright_: str | None, artist: str | None,
                   warnings: list[str]) -> None:
    """ExifTool pass (full MakerNote copy, GPS/serial policy). Silent no-op when ExifTool is missing."""
    if src is None or keep == "none":
        return
    from . import metadata

    if metadata.exiftool_available():
        ok = metadata.copy_metadata(src, out, keep)
        if not ok:
            warnings.append("exiftool metadata copy failed; kept the Pillow-built EXIF")
        elif copyright_ or artist:
            et = metadata._et()
            args = []
            if copyright_:
                args.append(f"-Copyright={copyright_}")
            if artist:
                args.append(f"-Artist={artist}")
            try:
                et.execute(*args, "-P", "-overwrite_original", str(out))
            except Exception:  # noqa: BLE001
                pass


# =============================================================================== save_array
def _save_jpeg(im, dest, kw: dict, warnings: list[str]) -> None:
    """Write a JPEG with Pillow, robust for very detailed images.

    Progressive and optimised encodes buffer the whole entropy-coded stream; when it does not fit
    ``ImageFile.MAXBLOCK`` Pillow raises ``OSError: broken data stream when writing image file`` (seen with
    halftone-style output). The buffer is raised to cover the worst case (width x height x 4 bytes) for the duration
    of the write; if the encoder still fails the file is rewritten as a baseline, non-optimised JPEG with a warning."""
    from PIL import ImageFile

    old = ImageFile.MAXBLOCK
    need = int(im.size[0]) * int(im.size[1]) * 4 + (1 << 16)
    ImageFile.MAXBLOCK = max(old, need)
    try:
        try:
            im.save(dest, **kw)
        except OSError as exc:
            if not (kw.get("optimize") or kw.get("progressive")):
                raise
            kw2 = dict(kw, optimize=False, progressive=False)
            im.save(dest, **kw2)
            warnings.append(f"JPEG write failed with progressive/optimize ({exc}); saved as baseline instead")
    finally:
        ImageFile.MAXBLOCK = old


def save_array(img_display: np.ndarray, out_path: str | Path, fmt: str | None = None, quality: int = 92,
               icc: str | None = "srgb", bits: int = 8, metadata_from: str | Path | None = None,
               keep_metadata: str = "all", *, subsampling: str = "4:4:4", progressive: bool = True,
               optimize: bool = True, dpi: int | None = None, compression: str | None = None,
               lossless: bool = False, dither: bool = False, copyright: str | None = None,
               artist: str | None = None, warnings: list[str] | None = None) -> Path:
    """Encode a display-referred float array (already in ``icc`` primaries/encoding) to ``out_path``.

    ``fmt`` defaults to the file extension. ``bits=16`` applies to png/tiff (others are 8-bit). ICC profiles
    for sRGB / Display P3 / Adobe RGB are generated (``photostudio.icc``) and embedded; ``icc=None`` embeds
    none. Metadata: ``metadata_from`` (a source image) with ``keep_metadata`` all | copyright | none."""
    from PIL import Image

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    warnings = warnings if warnings is not None else []
    f = _fmt_for(out, fmt)
    img = np.asarray(img_display, np.float32)
    if img.ndim == 2:
        img = np.stack([img] * 3, -1)
    prof = _icc.profile_bytes(_icc.canonical(icc)) if icc else None
    bits = 16 if (bits == 16 and f in ("png", "tiff")) else 8
    exif = _exif_bytes(metadata_from, keep_metadata, copyright, artist) if f in ("jpeg", "webp", "png", "avif", "heif") else None
    tmp = out.with_name(out.name + ".part")
    try:
        if f == "jpeg":
            q = int(np.clip(quality, 1, 100))
            kw: dict[str, Any] = dict(format="JPEG", quality=q, subsampling=SUBSAMPLING.get(str(subsampling), 0),
                                      progressive=bool(progressive), optimize=bool(optimize))
            if prof:
                kw["icc_profile"] = prof
            if exif:
                kw["exif"] = exif
            if dpi:
                kw["dpi"] = (dpi, dpi)
            _save_jpeg(Image.fromarray(_quantise(img, 8, dither)), tmp, kw, warnings)
        elif f == "png":
            if bits == 16:
                cv2 = lazy_import("cv2", "16-bit PNG")
                ok, buf = cv2.imencode(".png", _quantise(img, 16)[..., ::-1].copy(), [cv2.IMWRITE_PNG_COMPRESSION, 6])
                if not ok:
                    raise ExportError("cv2 could not encode 16-bit PNG")
                data = _icc.embed_png_icc(bytes(buf), prof) if prof else bytes(buf)
                tmp.write_bytes(data)
            else:
                kw = dict(format="PNG", optimize=bool(optimize))
                if prof:
                    kw["icc_profile"] = prof
                if exif:
                    kw["exif"] = exif
                if dpi:
                    kw["dpi"] = (dpi, dpi)
                Image.fromarray(_quantise(img, 8, dither)).save(tmp, **kw)
        elif f == "tiff":
            tifffile = lazy_import("tifffile", "TIFF export")
            comp = (compression or "zlib")
            comp = None if comp in ("none", "raw") else comp
            extra = [(34675, "B", len(prof), prof, True)] if prof else []
            try:
                tifffile.imwrite(str(tmp), _quantise(img, bits, dither), photometric="rgb", compression=comp,
                                 resolution=(dpi, dpi) if dpi else None, resolutionunit="INCH" if dpi else None,
                                 extratags=extra, software="photostudio", metadata=None)
            except Exception as exc:  # noqa: BLE001 - e.g. lzw without imagecodecs -> deflate
                warnings.append(f"tiff compression '{comp}' unavailable ({exc}); used zlib")
                tifffile.imwrite(str(tmp), _quantise(img, bits, dither), photometric="rgb", compression="zlib",
                                 resolution=(dpi, dpi) if dpi else None, resolutionunit="INCH" if dpi else None,
                                 extratags=extra, software="photostudio", metadata=None)
        elif f == "webp":
            kw = dict(format="WEBP", quality=int(np.clip(quality, 1, 100)), lossless=bool(lossless), method=4)
            if prof:
                kw["icc_profile"] = prof
            if exif:
                kw["exif"] = exif
            Image.fromarray(_quantise(img, 8, dither)).save(tmp, **kw)
        elif f in ("avif", "heif"):
            if f == "heif" or not _pil_has("avif"):
                ph = lazy_import("pillow_heif", f"{f.upper()} export")
                (ph.register_avif_opener if f == "avif" else ph.register_heif_opener)()
            kw = dict(format="AVIF" if f == "avif" else "HEIF", quality=-1 if lossless else int(np.clip(quality, 1, 100)))
            if prof:
                kw["icc_profile"] = prof
            if exif:
                kw["exif"] = exif
            if f == "avif":
                kw["subsampling"] = str(subsampling) if subsampling in ("4:4:4", "4:2:2", "4:2:0") else "4:4:4"
            Image.fromarray(_quantise(img, 8, dither)).save(tmp, **kw)
        elif f == "jxl":
            lazy_import("pillow_jxl", "JPEG XL export")
            kw = dict(format="JXL", quality=int(np.clip(quality, 1, 100)), lossless=bool(lossless))
            if prof:
                kw["icc_profile"] = prof
            Image.fromarray(_quantise(img, 8, dither)).save(tmp, **kw)
        else:  # pragma: no cover
            raise ExportError(f"unsupported format {f}")
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    if f in ("tiff", "jpeg", "webp", "png", "avif", "heif"):
        _post_metadata(out, metadata_from, keep_metadata, copyright, artist, warnings)
    return out


def _pil_has(feature: str) -> bool:
    from PIL import features
    try:
        return bool(features.check(feature))
    except Exception:  # noqa: BLE001
        return False


# =============================================================================== resize / sharpen / decorate
def resize_for_export(img: np.ndarray, opts: dict) -> np.ndarray:
    """Apply the preset resize options. Downscale uses INTER_AREA, upscale Lanczos; never upscales unless
    ``upscale``."""
    import cv2

    h, w = img.shape[:2]
    upscale = bool(opts.get("upscale", False))
    rs = opts.get("resize") or {}
    long_edge = opts.get("long_edge", rs.get("long_edge"))
    short_edge = opts.get("short_edge", rs.get("short_edge"))
    mp = opts.get("megapixels", rs.get("megapixels"))
    bw, bh = opts.get("width", rs.get("width")), opts.get("height", rs.get("height"))
    fit = str(opts.get("fit", rs.get("fit", "contain"))).lower()

    def scale_to(img, s):
        if abs(s - 1.0) < 1e-4 or (s > 1 and not upscale):
            return img
        interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LANCZOS4
        nh, nw = max(1, round(img.shape[0] * s)), max(1, round(img.shape[1] * s))
        return np.clip(cv2.resize(img, (nw, nh), interpolation=interp), 0, 1 if img.max() <= 1.0001 else None)

    if bw and bh:
        bw, bh = int(bw), int(bh)
        if fit == "exact":
            return cv2.resize(img, (bw, bh), interpolation=cv2.INTER_AREA if bw < w else cv2.INTER_LANCZOS4)
        if fit == "cover":
            s = max(bw / w, bh / h)
            im2 = scale_to(img, s) if (s < 1 or upscale) else img
            hh, ww = im2.shape[:2]
            if ww >= bw and hh >= bh:
                tw, th = bw, bh
            else:       # source smaller than the target (no upscaling): largest centred crop with the target aspect
                k = min(ww / bw, hh / bh)
                tw, th = max(1, round(bw * k)), max(1, round(bh * k))
            x0, y0 = (ww - tw) // 2, (hh - th) // 2
            return np.ascontiguousarray(im2[y0:y0 + th, x0:x0 + tw])
        s = min(bw / w, bh / h)
        im2 = scale_to(img, s)
        if fit == "pad":
            col = _color(opts.get("pad_color", "#ffffff"))
            canvas = np.empty((bh, bw, 3), np.float32)
            canvas[:] = col
            hh, ww = im2.shape[:2]
            y0, x0 = (bh - hh) // 2, (bw - ww) // 2
            canvas[y0:y0 + hh, x0:x0 + ww] = im2
            return canvas
        return im2
    if long_edge:
        return scale_to(img, float(long_edge) / max(h, w))
    if short_edge:
        return scale_to(img, float(short_edge) / min(h, w))
    if mp:
        return scale_to(img, float(np.sqrt(float(mp) * 1e6 / (h * w))))
    return img


def _color(c) -> np.ndarray:
    from .util import hex_to_rgb
    return hex_to_rgb(c).astype(np.float32)


SHARPEN_TARGETS = {          # (radius px at 2048 long edge, amount)
    "screen": (0.8, 0.6), "web": (0.8, 0.6), "print_matte": (1.2, 0.9), "print_glossy": (0.9, 0.6), "print": (1.0, 0.8),
}


def output_sharpen(img: np.ndarray, target: str = "screen", amount: float | None = None, radius: float | None = None,
                   dpi: float | None = None, threshold: float = 0.004) -> np.ndarray:
    """Luminance-only unsharp mask tuned to the *output* size (radius scales with the long edge for screen
    targets and with dpi for print). Hue is preserved because the same detail is added to every channel."""
    import cv2

    r0, a0 = SHARPEN_TARGETS.get(target, SHARPEN_TARGETS["screen"])
    a = float(a0 if amount is None else amount)
    if a <= 0:
        return img
    if radius is None:
        radius = r0 * (float(dpi) / 300.0 if (dpi and target.startswith("print")) else max(img.shape[:2]) / 2048.0)
    radius = max(0.3, float(radius))
    y = img @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    detail = y - cv2.GaussianBlur(y, (0, 0), radius, borderType=cv2.BORDER_REFLECT)
    mag = np.abs(detail)
    detail = np.where(mag < threshold, detail * (mag / threshold) ** 2, detail)   # soft noise gate
    return np.clip(img + (a * detail)[..., None], 0, 1).astype(np.float32)


def _font(size: int):
    from PIL import ImageFont

    for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(cand, size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # old Pillow
        return ImageFont.load_default()


def apply_watermark(img: np.ndarray, spec: dict) -> np.ndarray:
    """Text or image watermark. spec: text | image (path), position (bottom-right|bottom-left|top-right|top-left|
    center), opacity 0..1, size (fraction of the long edge: text height / image width), margin (fraction), color."""
    from PIL import Image, ImageDraw

    h, w = img.shape[:2]
    pos = spec.get("position", "bottom-right")
    opacity = float(spec.get("opacity", 0.6))
    margin = int(float(spec.get("margin", 0.025)) * max(h, w))
    base = Image.fromarray(to_uint8(img)).convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    if spec.get("image"):
        wm = Image.open(spec["image"]).convert("RGBA")
        tw = max(8, int(float(spec.get("size", 0.15)) * w))
        wm = wm.resize((tw, max(1, round(wm.height * tw / wm.width))), Image.LANCZOS)
        alpha = wm.getchannel("A").point(lambda a: int(a * opacity))
        wm.putalpha(alpha)
        tw_, th_ = wm.size
    elif spec.get("text"):
        size = max(10, int(float(spec.get("size", 0.03)) * max(h, w)))
        font = _font(size)
        draw = ImageDraw.Draw(layer)
        bbox = draw.textbbox((0, 0), str(spec["text"]), font=font)
        tw_, th_ = bbox[2] - bbox[0], bbox[3] - bbox[1]
        wm = Image.new("RGBA", (tw_ + 4, th_ + 4), (0, 0, 0, 0))
        col = tuple(int(c * 255) for c in _color(spec.get("color", "#ffffff"))) + (int(255 * opacity),)
        d2 = ImageDraw.Draw(wm)
        shadow = (0, 0, 0, int(120 * opacity))
        d2.text((3 - bbox[0], 3 - bbox[1]), str(spec["text"]), font=font, fill=shadow)
        d2.text((2 - bbox[0], 2 - bbox[1]), str(spec["text"]), font=font, fill=col)
        tw_, th_ = wm.size
    else:
        return img
    x = {"left": margin, "right": w - tw_ - margin, "center": (w - tw_) // 2}[
        "left" if "left" in pos else ("right" if "right" in pos else "center")]
    y = {"top": margin, "bottom": h - th_ - margin, "center": (h - th_) // 2}[
        "top" if "top" in pos else ("bottom" if "bottom" in pos else "center")]
    layer.paste(wm, (max(0, x), max(0, y)), wm)
    out = Image.alpha_composite(base, layer).convert("RGB")
    return np.asarray(out, np.float32) / 255.0


def apply_border(img: np.ndarray, spec: dict) -> np.ndarray:
    """Add a solid border. spec: size (fraction of the long edge, or px if >= 1), color, inner (thin inner
    keyline: {size px, color})."""
    h, w = img.shape[:2]
    s = float(spec.get("size", 0.03))
    px = int(round(s if s >= 1 else s * max(h, w)))
    if px <= 0:
        return img
    col = _color(spec.get("color", "#ffffff"))
    inner = spec.get("inner")
    work = img
    if inner:
        ip = max(1, int(inner.get("size", 2)))
        work = img.copy()
        ic = _color(inner.get("color", "#000000"))
        work[:ip], work[-ip:], work[:, :ip], work[:, -ip:] = ic, ic, ic, ic
    out = np.empty((h + 2 * px, w + 2 * px, 3), np.float32)
    out[:] = col
    out[px:px + h, px:px + w] = work
    return out


def soft_proof(img_display: np.ndarray, paper_icc: str | Path, source: str = "srgb", intent: str = "relative",
               gamut_warning: bool = False) -> np.ndarray:
    """Soft-proof an sRGB/P3/Adobe display array against a paper/printer ICC (ImageCms proof transform),
    returning the display-referred preview; ``gamut_warning`` paints out-of-gamut pixels grey."""
    from PIL import Image, ImageCms

    src = _icc.pil_profile(source)
    paper = ImageCms.getOpenProfile(str(paper_icc))
    disp = _icc.pil_profile("srgb")
    flags = ImageCms.Flags.SOFTPROOFING | ImageCms.Flags.BLACKPOINTCOMPENSATION
    if gamut_warning:
        flags |= ImageCms.Flags.GAMUTCHECK
    ri = {"perceptual": 0, "relative": 1, "saturation": 2, "absolute": 3}[intent]
    t = ImageCms.buildProofTransform(src, disp, paper, "RGB", "RGB", renderingIntent=ri,
                                     proofRenderingIntent=ri, flags=flags)
    out = ImageCms.applyTransform(Image.fromarray(to_uint8(img_display)), t)
    return np.asarray(out, np.float32) / 255.0


# =============================================================================== naming
class _Date(datetime):
    def __format__(self, spec: str) -> str:
        return super().__format__(spec or "%Y-%m-%d")


def _as_date(meta: dict, path: Path) -> datetime:
    dt = meta.get("datetime")
    try:
        d = datetime.fromisoformat(dt) if dt else datetime.fromtimestamp(path.stat().st_mtime)
    except (ValueError, OSError):
        d = datetime.now()
    return _Date(d.year, d.month, d.day, d.hour, d.minute, d.second)


def format_name(template: str, src: Path, meta: dict, **fields) -> str:
    """Photo-Mechanic style naming: {stem} {name} {date[:%Y%m%d]} {time} {seq[:04d]} {album_pos} {mode} {art}
    {preset} {version} {rating} {camera} {lens} {iso} {ext}. Unknown tokens raise ExportError."""
    d = _as_date(meta, src)
    vals = {"stem": src.stem, "name": src.name, "date": d, "time": d.strftime("%H%M%S"), "seq": 1, "album_pos": 1,
            "mode": "", "art": "", "preset": "", "version": "", "rating": 0, "camera": (meta.get("model") or "").replace(" ", ""),
            "lens": (meta.get("lens") or "").replace(" ", ""), "iso": int(meta.get("iso") or 0), "ext": ""}
    vals.update({k: v for k, v in fields.items() if v is not None})
    try:
        out = string.Formatter().vformat(template, (), vals)
    except (KeyError, ValueError, IndexError) as exc:
        raise ExportError(f"bad export name template '{template}': {exc} (tokens: {', '.join(sorted(vals))})") from exc
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", out).strip() or src.stem


# =============================================================================== export_image
def load_export_options(preset: str | dict | None, overrides: dict | None = None) -> dict:
    opts: dict[str, Any] = {}
    if isinstance(preset, dict):
        opts = copy.deepcopy(preset.get("options", preset))
    elif preset:
        try:
            opts = copy.deepcopy(load_preset("export", preset).get("options", {}))
        except PresetError as exc:
            raise ExportError(str(exc)) from exc
    for k, v in (overrides or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(opts.get(k), dict):
            opts[k] = {**opts[k], **v}
        else:
            opts[k] = v
    opts.setdefault("format", "jpeg")
    return opts


def default_out_path(src: Path, opts: dict, preset_name: str, fields: dict, meta: dict) -> Path:
    ext = FORMATS.get(str(opts["format"]).lower(), ".jpg")
    template = opts.get("name") or "{stem}_{preset}"
    name = format_name(template, src, meta, preset=preset_name, **fields)
    if "{ext}" not in template and not name.lower().endswith(tuple(EXT_FORMAT)):
        name += ext
    return src.parent / opts.get("output_dir", "exports") / name


def export_image_ex(path: str | Path, out_path: str | Path | None = None, preset: str | dict | None = "web",
                    version: str | None = None, **overrides) -> dict:
    """Export one image and return ``{out, format, width, height, bytes, seconds, warnings, ...}``."""
    from .pipeline import engine

    t0 = time.time()
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(src)
    fields = {k: overrides.pop(k) for k in ("seq", "album_pos") if k in overrides}
    stack_override = overrides.pop("stack", None)
    preset_name = preset if isinstance(preset, str) else (preset or {}).get("name", "custom")
    opts = load_export_options(preset, overrides)
    warnings: list[str] = []
    stack = stack_override if stack_override is not None else engine.stack_for(src, version)
    from . import sidecar

    doc_info = sidecar.load(src)
    vname = version or doc_info["active"]
    vinfo = doc_info["versions"].get(vname, {})
    space = _icc.canonical(opts.get("icc", "srgb")) if opts.get("icc", "srgb") not in (None, "none") else "srgb"
    render_edge = opts.get("render_long_edge")
    img, ctx = engine.render(src, stack, long_edge=render_edge, output_space=space, return_ctx=True, use_cache=False)
    warnings += [m for m in ctx.log if m.startswith("warning")]
    meta = ctx.meta
    fields.update({"mode": vinfo.get("mode") or "", "art": vinfo.get("art") or "", "version": vname,
                   "rating": doc_info.get("rating", 0)})

    img = resize_for_export(img, opts)
    sharp = opts.get("sharpen")
    if sharp:
        s = sharp if isinstance(sharp, dict) else {"amount": float(sharp)}
        img = output_sharpen(img, s.get("target", "screen"), s.get("amount"), s.get("radius"), opts.get("dpi"))
    if opts.get("watermark"):
        img = apply_watermark(img, opts["watermark"])
    if opts.get("border"):
        img = apply_border(img, opts["border"])

    if out_path is None:
        out = default_out_path(src, opts, preset_name, fields, meta)
    else:
        out = Path(out_path)
        if out.is_dir() or str(out_path).endswith(("/", os.sep)) or not out.suffix:
            out.mkdir(parents=True, exist_ok=True)
            tmpl = opts.get("name") or "{stem}_{preset}"
            nm = format_name(tmpl, src, meta, preset=preset_name, **fields)
            if "{ext}" not in tmpl and not nm.lower().endswith(tuple(EXT_FORMAT)):
                nm += FORMATS.get(str(opts["format"]).lower(), ".jpg")
            out = out / nm
    fmt = None if (out_path is not None and Path(out_path).suffix.lower() in EXT_FORMAT) else opts["format"]
    if opts.get("skip_existing") and out.exists():
        return {"out": str(out), "skipped": True, "seconds": 0.0, "warnings": warnings}
    keep = opts.get("metadata", "all")
    save_array(img, out, fmt=fmt, quality=int(opts.get("quality", 92)),
               icc=None if opts.get("icc", "srgb") in (None, "none") else space, bits=int(opts.get("bits", 8)),
               metadata_from=src, keep_metadata=keep, subsampling=str(opts.get("subsampling", "4:4:4")),
               progressive=bool(opts.get("progressive", True)), optimize=bool(opts.get("optimize", True)),
               dpi=opts.get("dpi"), compression=opts.get("compression"), lossless=bool(opts.get("lossless", False)),
               dither=bool(opts.get("dither", False)), copyright=opts.get("copyright"), artist=opts.get("artist"),
               warnings=warnings)
    return {"out": str(out), "format": _fmt_for(out, None), "width": int(img.shape[1]), "height": int(img.shape[0]),
            "bytes": out.stat().st_size, "preset": preset_name, "icc": space, "bits": int(opts.get("bits", 8)),
            "seconds": round(time.time() - t0, 3), "warnings": warnings}


def export_image(path: str | Path, out_path: str | Path | None = None, preset: str | dict | None = "web",
                 version: str | None = None, **overrides) -> Path:
    """Develop ``path`` with its sidecar stack and export it (CONTRACT). Returns the written file path."""
    return Path(export_image_ex(path, out_path, preset, version, **overrides)["out"])


# =============================================================================== batch
def _worker(args):
    path, out_dir, preset, version, overrides = args
    try:
        r = export_image_ex(path, out_dir, preset, version, **overrides)
        return {"src": str(path), "ok": True, **r}
    except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
        return {"src": str(path), "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def export_batch(paths: list[str | Path], preset: str | dict | None = "web", out_dir: str | Path | None = None,
                 version: str | None = None, jobs: int | None = None, name: str | None = None,
                 progress=None, **overrides) -> list[dict]:
    """Export many files. ``jobs`` > 1 uses a process pool (default: min(4, cpu count); 1 = in-process).
    ``seq``/``album_pos`` tokens are assigned in input order. Returns one result dict per input, in order."""
    tasks = []
    for i, p in enumerate(paths, 1):
        ov = dict(overrides)
        ov.update({"seq": i, "album_pos": i})
        if name:
            ov["name"] = name
        tasks.append((str(p), str(out_dir) if out_dir else None, preset, version, ov))
    if jobs is None:
        jobs = min(4, os.cpu_count() or 1)
    results: list[dict | None] = [None] * len(tasks)
    if jobs <= 1 or len(tasks) <= 1:
        for i, t in enumerate(tasks):
            results[i] = _worker(t)
            if progress:
                progress(i + 1, len(tasks), results[i])
        return results  # type: ignore[return-value]
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(_worker, t): i for i, t in enumerate(tasks)}
        done = 0
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:  # noqa: BLE001
                results[i] = {"src": tasks[i][0], "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            done += 1
            if progress:
                progress(done, len(tasks), results[i])
    return results  # type: ignore[return-value]


# =============================================================================== contact sheet
def build_contact_sheet(items: list, out_path: str | Path, columns: int = 5, cell: int = 360, margin: int = 24,
                        captions: bool = True, title: str | None = None, page: str | None = None,
                        background: str = "#202020", fg: str = "#e8e8e8") -> Path:
    """Contact sheet of previews. ``items``: paths (previews come from companion/embedded JPEGs, never a full
    raw render), (path, caption) tuples, or float display arrays. ``page`` = 'A4' / 'letter' renders at 300 dpi
    with the cell size chosen to fit one page (cols x rows); saved as JPEG/PNG/PDF by extension."""
    from PIL import Image, ImageDraw

    from .decode import preview_display

    entries = []
    for it in items:
        if isinstance(it, np.ndarray):
            entries.append((it, ""))
        elif isinstance(it, (tuple, list)):
            entries.append((it[0], str(it[1])))
        else:
            entries.append((it, Path(it).stem))
    n = len(entries)
    cols = max(1, int(columns))
    rows = max(1, -(-n // cols))
    cap_h = 28 if captions else 0
    title_h = 56 if title else 0
    if page:
        pw, ph = {"a4": (2480, 3508), "letter": (2550, 3300), "a3": (3508, 4961)}[page.lower()]
        cell = (pw - margin * (cols + 1)) // cols
        full_rows = max(1, (ph - title_h - margin) // (cell + cap_h + margin))
        rows_needed = rows
        if rows_needed > full_rows:
            pass  # sheet grows past one page: caller splits into pages
        W, H = pw, max(ph, title_h + margin + rows_needed * (cell + cap_h + margin))
    else:
        W = margin + cols * (cell + margin)
        H = title_h + margin + rows * (cell + cap_h + margin)
    sheet = Image.new("RGB", (W, H), background)
    draw = ImageDraw.Draw(sheet)
    font = _font(max(11, cap_h - 10))
    if title:
        draw.text((margin, margin // 2 + 4), title, fill=fg, font=_font(34))
    for i, (src, cap) in enumerate(entries):
        r, c = divmod(i, cols)
        x0, y0 = margin + c * (cell + margin), title_h + margin + r * (cell + cap_h + margin)
        try:
            arr = src if isinstance(src, np.ndarray) else preview_display(src, cell * 2)
        except Exception:  # noqa: BLE001
            arr = np.full((cell, cell, 3), 0.25, np.float32)
        im = Image.fromarray(to_uint8(arr))
        im.thumbnail((cell, cell), Image.LANCZOS)
        sheet.paste(im, (x0 + (cell - im.width) // 2, y0 + (cell - im.height) // 2))
        if captions and cap:
            draw.text((x0, y0 + cell + 4), cap[:36], fill=fg, font=font)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".pdf":
        sheet.save(out, "PDF", resolution=300.0)
    else:
        sheet.save(out, quality=90) if out.suffix.lower() in (".jpg", ".jpeg") else sheet.save(out)
    return out
