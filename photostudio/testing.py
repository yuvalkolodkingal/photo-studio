"""Synthetic test material: a display-referred test scene, JPEG writer, and a rawpy-compatible fake raw
(`FakeRawPy`) stored as `.npz` so the full raw path can run end to end without a real CR3."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import color

R50_XYZ_TO_CAM = np.array([[9269, -2012, -1107], [-3990, 11762, 2527], [-569, 2093, 4913]], dtype=np.float64) / 10000.0


def make_scene(w: int = 960, h: int = 640, seed: int = 1) -> np.ndarray:
    """Display sRGB float32 test scene: sky + sun, hills, foliage, water, a building with verticals,
    a face (skin, eyes, lips), colour patches, fine stripes and text-like detail."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = xx / w, yy / h
    img = np.zeros((h, w, 3), np.float32)
    # sky gradient
    top, hor = np.array([0.18, 0.38, 0.75]), np.array([0.72, 0.82, 0.95])
    t = np.clip(v / 0.55, 0, 1)[..., None]
    img[:] = top * (1 - t) + hor * t
    # sun with glow
    d = np.hypot(u - 0.78, (v - 0.16) * h / w)
    img += np.clip(1.0 - d / 0.035, 0, 1)[..., None] * 1.0
    img += (np.exp(-(d / 0.09) ** 2) * 0.35)[..., None] * np.array([1.0, 0.85, 0.6])
    # hills
    ridge = 0.55 + 0.05 * np.sin(u * 9) + 0.03 * np.sin(u * 23 + 1)
    hill = v > ridge
    img[hill] = np.array([0.25, 0.42, 0.18]) * (0.8 + 0.4 * rng.random((hill.sum(), 1)))
    # foliage texture band
    fol = (v > 0.72) & (u < 0.45)
    noise = cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 1.2)
    img[fol] = (np.array([0.12, 0.45, 0.10]) * (0.6 + 0.9 * noise[fol])[:, None])
    # water
    wat = (v > 0.85) & (u >= 0.45)
    img[wat] = np.array([0.10, 0.30, 0.45]) + 0.05 * np.sin(xx[wat] * 0.8)[:, None]
    # building with verticals
    bx0, bx1, by0 = int(0.50 * w), int(0.66 * w), int(0.30 * h)
    img[by0:int(0.85 * h), bx0:bx1] = np.array([0.78, 0.74, 0.68])
    for x in range(bx0 + 6, bx1 - 6, max(4, w // 80)):
        img[by0 + 8:int(0.83 * h), x:x + 2] = np.array([0.25, 0.22, 0.2])
    for y in range(by0 + 12, int(0.83 * h), max(6, h // 30)):
        img[y:y + 3, bx0 + 4:bx1 - 4] = np.array([0.95, 0.85, 0.45])  # lit windows
    # face
    fc = (0.25, 0.45)
    fd = ((u - fc[0]) / 0.075) ** 2 + ((v - fc[1]) / 0.16) ** 2
    face = fd < 1
    img[face] = np.array([0.88, 0.66, 0.55]) * (0.92 + 0.08 * (1 - fd[face]))[:, None]
    for ex in (-0.028, 0.028):
        e = ((u - fc[0] - ex) / 0.014) ** 2 + ((v - fc[1] + 0.035) / 0.010) ** 2 < 1
        img[e] = np.array([0.96, 0.96, 0.96])
        p = ((u - fc[0] - ex) / 0.006) ** 2 + ((v - fc[1] + 0.035) / 0.008) ** 2 < 1
        img[p] = np.array([0.15, 0.25, 0.45])
    lips = ((u - fc[0]) / 0.03) ** 2 + ((v - fc[1] - 0.075) / 0.010) ** 2 < 1
    img[lips] = np.array([0.72, 0.22, 0.25])
    hair = (((u - fc[0]) / 0.085) ** 2 + ((v - fc[1] + 0.04) / 0.17) ** 2 < 1) & ~face & (v < fc[1])
    img[hair] = np.array([0.18, 0.11, 0.07])
    # colour patches (row along bottom-left)
    patches = ["#735244", "#c29682", "#627a9d", "#576c43", "#8580b1", "#67bdaa", "#d67e2c", "#505ba6",
               "#c15a63", "#5e3c6c", "#9dbc40", "#e0a32e", "#383d96", "#469449", "#af363c", "#e7c71f",
               "#bb5695", "#0885a1", "#f3f3f2", "#c8c8c8", "#a0a0a0", "#7a7a79", "#555555", "#343434"]
    pw = max(4, int(0.035 * w))
    for i, hx in enumerate(patches):
        r, c = divmod(i, 12)
        x0, y0 = int(0.02 * w) + c * pw, int(0.06 * h) + r * pw
        img[y0:y0 + pw - 2, x0:x0 + pw - 2] = color.srgb_oetf(color.srgb_eotf(
            np.array([int(hx[j:j + 2], 16) / 255 for j in (1, 3, 5)], np.float32)))
    # fine stripes (sharpness target)
    sy0, sx0 = int(0.20 * h), int(0.02 * w)
    for i in range(0, int(0.18 * w), 2):
        img[sy0:sy0 + int(0.06 * h), sx0 + i] = 0.05
    # text-like strokes
    for i in range(12):
        x0 = int((0.70 + 0.02 * i) * w)
        cv2.line(img, (x0, int(0.9 * h)), (x0 + int(0.01 * w), int(0.95 * h)), (0.05, 0.05, 0.05), 1)
    return np.clip(img, 0, 1).astype(np.float32)


def write_jpeg(path: str | Path, img_display: np.ndarray, quality: int = 92, exif: dict | None = None) -> Path:
    from PIL import Image

    im = Image.fromarray((np.clip(img_display, 0, 1) * 255 + 0.5).astype(np.uint8))
    kw = {"quality": quality, "subsampling": 0}
    if exif:
        ex = Image.Exif()
        tagmap = {"Make": 0x010F, "Model": 0x0110, "DateTime": 0x0132}
        for k, v in exif.items():
            if k in tagmap:
                ex[tagmap[k]] = v
        sub = {}
        submap = {"DateTimeOriginal": 0x9003, "ExposureTime": 0x829A, "FNumber": 0x829D, "ISOSpeedRatings": 0x8827,
                  "FocalLength": 0x920A, "SubSecTimeOriginal": 0x9291, "LensModel": 0xA434}
        for k, v in exif.items():
            if k in submap:
                sub[submap[k]] = v
        if sub:
            ifd = ex.get_ifd(0x8769)
            ifd.update(sub)
        kw["exif"] = ex
    im.save(str(path), **kw)
    return Path(path)


# --------------------------------------------------------------------------- fake raw
@dataclass
class _Sizes:
    raw_height: int
    raw_width: int
    height: int
    width: int
    top_margin: int = 0
    left_margin: int = 0
    iheight: int = 0
    iwidth: int = 0
    flip: int = 0
    pixel_aspect: float = 1.0


@dataclass
class _Thumb:
    format: str
    data: bytes


class FakeRawPy:
    """Minimal rawpy.RawPy look-alike backed by an RGGB uint16 mosaic."""

    def __init__(self, mosaic: np.ndarray, meta: dict, black: int = 512, white: int = 16383,
                 cam_wb=(2.1, 1.0, 1.55, 1.0), day_wb=(2.0, 1.0, 1.45, 1.0), thumb: bytes | None = None):
        self.raw_image_visible = mosaic
        self.raw_image = mosaic
        h, w = mosaic.shape
        pat = np.array([[0, 1], [3, 2]], dtype=np.uint8)
        self.raw_pattern = pat
        self.raw_colors_visible = np.tile(pat, (h // 2 + 1, w // 2 + 1))[:h, :w]
        self.black_level_per_channel = [black] * 4
        self.white_level = white
        self.camera_white_level_per_channel = None
        self.camera_whitebalance = list(cam_wb)
        self.daylight_whitebalance = list(day_wb)
        self.rgb_xyz_matrix = np.vstack([R50_XYZ_TO_CAM, np.zeros((1, 3))]).astype(np.float32)
        self.color_matrix = np.zeros((3, 4), np.float32)
        self.color_desc = b"RGBG"
        self.num_colors = 3
        self.raw_type = "Flat"
        self.sizes = _Sizes(h, w, h, w, iheight=h, iwidth=w)
        self.meta = meta
        self._thumb = thumb

    # context manager like rawpy
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    @classmethod
    def load(cls, path: str) -> "FakeRawPy":
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            thumb = bytes(z["thumb"].tobytes()) if "thumb" in z else None
            return cls(z["mosaic"], meta, int(z["black"]), int(z["white"]), tuple(z["cam_wb"]),
                       tuple(z["day_wb"]), thumb)

    def extract_thumb(self):
        if self._thumb is None:
            raise RuntimeError("no thumbnail")
        return _Thumb("JPEG", self._thumb)

    def postprocess(self, gamma=(1, 1), no_auto_bright=True, output_bps=16, user_wb=None, use_camera_wb=False,
                    output_color="raw", highlight_mode="Ignore", demosaic_algorithm="DHT", half_size=False,
                    user_flip=0, **kw):
        mos = self.raw_image_visible.astype(np.float32)
        bl = self.black_level_per_channel[0]
        norm = np.clip((mos - bl) / (self.white_level - bl), 0, 1)
        mul = np.asarray(user_wb if user_wb is not None else (self.camera_whitebalance if use_camera_wb else [1, 1, 1, 1]),
                         dtype=np.float64)
        hl = str(highlight_mode)
        dnorm = mul[:3].max() if ("Ignore" in hl or "1" == hl) else mul[:3].min()
        # bilinear demosaic of an RGGB pattern
        h, w = norm.shape
        planes = np.zeros((h, w, 3), np.float32)
        masks = np.zeros((h, w, 3), np.float32)
        cols = self.raw_colors_visible
        for c, ch in ((0, 0), (1, 1), (3, 1), (2, 2)):
            sel = cols == c
            planes[..., ch][sel] = norm[sel] * mul[c if c < 3 else 1] / dnorm
            masks[..., ch][sel] = 1
        k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float32)
        out = np.zeros_like(planes)
        for ch in range(3):
            num = cv2.filter2D(planes[..., ch], -1, k, borderType=cv2.BORDER_REFLECT)
            den = cv2.filter2D(masks[..., ch], -1, k, borderType=cv2.BORDER_REFLECT)
            out[..., ch] = np.where(masks[..., ch] > 0, planes[..., ch], num / np.maximum(den, 1e-6))
        out = np.clip(out, 0, 1)
        if half_size:
            out = cv2.resize(out, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        return (out * 65535 + 0.5).astype(np.uint16)


def make_fake_raw(path: str | Path, scene_display: np.ndarray | None = None, exposure_ev: float = -0.7,
                  meta: dict | None = None, cam_wb=(2.1, 1.0, 1.55, 1.0), thumb: bool = True,
                  noise: float = 0.002, seed: int = 0) -> Path:
    """Build a `.npz` synthetic raw from a display scene: display -> linear Rec.2020 -> camera RGB ->
    divide by WB -> mosaic -> 14-bit with black level 512."""
    from .decode import camera_to_rec2020

    if scene_display is None:
        scene_display = make_scene()
    lin2020 = color.apply_matrix(color.srgb_eotf(scene_display), color.SRGB_TO_REC2020)
    lin2020 = lin2020 * (2.0 ** exposure_ev)
    M = camera_to_rec2020(R50_XYZ_TO_CAM)
    cam = color.apply_matrix(lin2020, np.linalg.inv(M))
    mul = np.asarray(cam_wb, np.float64)
    m3 = mul[:3] / mul[:3].min()
    sensor = cam / m3.astype(np.float32)
    rng = np.random.default_rng(seed)
    sensor = sensor + rng.normal(0, noise, sensor.shape).astype(np.float32)
    sensor = np.clip(sensor, 0, 1)
    h, w = sensor.shape[:2]
    h, w = h - h % 2, w - w % 2
    sensor = sensor[:h, :w]
    mos = np.zeros((h, w), np.float32)
    mos[0::2, 0::2] = sensor[0::2, 0::2, 0]
    mos[0::2, 1::2] = sensor[0::2, 1::2, 1]
    mos[1::2, 0::2] = sensor[1::2, 0::2, 1]
    mos[1::2, 1::2] = sensor[1::2, 1::2, 2]
    black, white = 512, 16383
    mosaic = (black + mos * (white - black) + 0.5).astype(np.uint16)
    md = {"Make": "Canon", "Model": "Canon EOS R50", "LensModel": "RF-S18-45mm F4.5-6.3 IS STM", "ISO": 400,
          "ExposureTime": 1 / 250, "FNumber": 8.0, "FocalLength": 24.0, "DateTimeOriginal": "2026:11:12 10:00:00",
          "SubSecTimeOriginal": "00", "PictureStyle": 0x81, "EasyMode": 0, "ImageWidth": w, "ImageHeight": h}
    if meta:
        md.update(meta)
    arrays = dict(mosaic=mosaic, meta=np.array(json.dumps(md)), black=np.array(black), white=np.array(white),
                  cam_wb=np.array(cam_wb, np.float64), day_wb=np.array((2.0, 1.0, 1.45, 1.0)))
    if thumb:
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray((np.clip(scene_display[:h, :w], 0, 1) * 255).astype(np.uint8)).save(buf, "JPEG", quality=85)
        arrays["thumb"] = np.frombuffer(buf.getvalue(), dtype=np.uint8)
    path = Path(path)
    np.savez_compressed(path, **arrays)
    return path
