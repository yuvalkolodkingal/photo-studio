"""Shared helpers: lazy optional imports, paths, JSON, hashing, image IO."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

RAW_EXTS = {".cr3", ".cr2", ".crw", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw", ".npz"}
JPEG_EXTS = {".jpg", ".jpeg"}
IMAGE_EXTS = JPEG_EXTS | {".png", ".tif", ".tiff", ".webp", ".heic", ".heif", ".avif", ".jxl"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts"}
ALL_MEDIA = RAW_EXTS | IMAGE_EXTS | VIDEO_EXTS


class MissingDependency(RuntimeError):
    """Raised when an optional dependency (python package, binary or model) is missing."""


_EXTRA_HINT = {
    "rembg": "ai", "mediapipe": "ai", "onnxruntime": "ai", "open_clip": "ai", "transformers": "ai",
    "torch": "ai", "ultralytics": "yolo", "insightface": "faces", "lensfunpy": "lens", "av": "video",
    "scenedetect": "video", "pillow_heif": "heif", "pillow_jxl": "jxl", "astroalign": "astro",
    "reverse_geocoder": "geo", "numba": "fast", "xxhash": "fast", "bm3d": "denoise", "exiftool": "meta",
}


def lazy_import(name: str, purpose: str = ""):
    """Import an optional module or raise a MissingDependency with an install hint."""
    try:
        return importlib.import_module(name)
    except Exception as exc:  # ImportError or broken native libs
        top = name.split(".")[0]
        extra = _EXTRA_HINT.get(top)
        hint = f"pip install -e '.[{extra}]'" if extra else f"pip install {top}"
        raise MissingDependency(
            f"Optional dependency '{name}' is not available{(' (needed for ' + purpose + ')') if purpose else ''}: "
            f"{exc}. Install with: {hint}"
        ) from exc


def have_module(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def which(binary: str) -> str | None:
    return shutil.which(binary)


def require_binary(binary: str, purpose: str = "") -> str:
    p = shutil.which(binary)
    if not p:
        hint = {"exiftool": "sudo dnf install perl-Image-ExifTool", "ffmpeg": "sudo dnf install ffmpeg",
                "ffprobe": "sudo dnf install ffmpeg", "rclone": "sudo dnf install rclone",
                "restic": "sudo dnf install restic"}.get(binary, f"install {binary}")
        raise MissingDependency(f"Binary '{binary}' not found on PATH{(' (needed for ' + purpose + ')') if purpose else ''}. {hint}")
    return p


def run(cmd: list[str], check: bool = True, capture: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, **kw)


# ---------------------------------------------------------------- paths
def data_home() -> Path:
    p = Path(os.environ.get("PHOTOSTUDIO_HOME", Path.home() / ".local/share/photostudio"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_home() -> Path:
    p = Path(os.environ.get("PHOTOSTUDIO_CACHE", Path.home() / ".cache/photostudio"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def preset_dirs() -> list[Path]:
    """Preset search path, lowest priority first (later dirs override earlier)."""
    dirs = []
    for cand in (package_dir() / "presets", package_dir().parent / "presets"):
        if cand.is_dir():
            dirs.append(cand)
    user = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "photostudio" / "presets"
    if user.is_dir():
        dirs.append(user)
    env = os.environ.get("PHOTOSTUDIO_PRESETS")
    if env:
        dirs.extend(Path(p) for p in env.split(os.pathsep) if p)
    return dirs


# ---------------------------------------------------------------- json
class _Enc(json.JSONEncoder):
    def default(self, o: Any):
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, Path):
            return str(o)
        if isinstance(o, set):
            return sorted(o)
        if isinstance(o, bytes):
            return o.hex()
        return super().default(o)


def dumps(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(obj, cls=_Enc, indent=indent, ensure_ascii=False)


def to_jsonable(obj: Any) -> Any:
    return json.loads(dumps(obj, indent=None))


# ---------------------------------------------------------------- hashing
def file_hash(path: str | Path, algo: str = "blake3", chunk: int = 1 << 22) -> str:
    """Content hash. blake3 (default, fast, CC0/Apache) with xxhash / sha256 fallbacks."""
    path = Path(path)
    if algo == "blake3":
        try:
            import blake3

            h = blake3.blake3()
            try:
                h.update_mmap(str(path))
            except Exception:
                with open(path, "rb") as f:
                    for b in iter(lambda: f.read(chunk), b""):
                        h.update(b)
            return h.hexdigest()
        except ImportError:
            algo = "sha256"
    if algo == "xxh128":
        xx = lazy_import("xxhash", "xxh128 hashing")
        h = xx.xxh3_128()
    else:
        import hashlib

        h = hashlib.new(algo)
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def params_hash(obj: Any) -> str:
    import hashlib

    return hashlib.sha1(json.dumps(obj, sort_keys=True, cls=_Enc).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- image helpers
def hex_to_rgb(h: str | list | tuple) -> np.ndarray:
    """'#RRGGBB' (display sRGB) -> float32 array in [0,1]."""
    if isinstance(h, (list, tuple, np.ndarray)):
        a = np.asarray(h, dtype=np.float32)
        return a / 255.0 if a.max() > 1.0 else a
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], dtype=np.float32)


def to_uint8(img: np.ndarray) -> np.ndarray:
    return (np.clip(img, 0, 1) * 255.0 + 0.5).astype(np.uint8)


def from_uint8(img: np.ndarray) -> np.ndarray:
    return img.astype(np.float32) / 255.0


def resize_long(img: np.ndarray, long_edge: int, interp: int | None = None) -> np.ndarray:
    import cv2

    h, w = img.shape[:2]
    s = long_edge / max(h, w)
    if abs(s - 1.0) < 1e-6:
        return img
    if interp is None:
        interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LANCZOS4
    return cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))), interpolation=interp)


def imread_display(path: str | Path) -> np.ndarray:
    """Read any 8/16-bit image file to float32 RGB display values in [0,1] (no colour management)."""
    from PIL import Image, ImageOps

    path = Path(path)
    if path.suffix.lower() in {".heic", ".heif", ".avif"}:
        try:
            ph = importlib.import_module("pillow_heif")
            ph.register_heif_opener()
        except Exception:
            pass
    if path.suffix.lower() in {".tif", ".tiff", ".png"}:
        import cv2

        arr = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if arr is not None:
            if arr.ndim == 2:
                arr = np.stack([arr] * 3, -1)
            if arr.shape[2] == 4:
                arr = arr[..., :3]
            arr = arr[..., ::-1]
            scale = 65535.0 if arr.dtype == np.uint16 else (1.0 if arr.dtype.kind == "f" else 255.0)
            return np.ascontiguousarray(arr.astype(np.float32) / scale)
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        return np.asarray(im, dtype=np.float32) / 255.0


def imwrite_png(path: str | Path, img: np.ndarray) -> None:
    from PIL import Image

    Image.fromarray(to_uint8(img)).save(str(path))


def parse_value(v: str) -> Any:
    """Parse a CLI --set value: JSON if possible, else bare string."""
    try:
        return json.loads(v)
    except Exception:
        lv = v.lower()
        if lv in ("true", "yes", "on"):
            return True
        if lv in ("false", "no", "off"):
            return False
        return v
