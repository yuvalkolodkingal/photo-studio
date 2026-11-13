"""Registry of optional AI models (never bundled): name, purpose, licence, size, source, cache path.

Everything here is lazy: importing this module imports no heavy dependency. Weights live under
``util.cache_home()/models/<name>`` and are fetched only by an explicit ``photo models download <name>``
(`download()`); masks and ops look models up with `path()` / `installed()` and fall back to non-AI
heuristics when a model is absent.

GPU policy (RTX 4050, 6 GB VRAM): onnxruntime uses CUDAExecutionProvider when present (CPU otherwise);
torch models use fp16 on CUDA. Inference inputs are capped at `MAX_SIDE` px (1024 for segmentation and
518 for depth), which keeps every model below ~3 GB VRAM; results are upsampled with a guided filter.
"""
from __future__ import annotations

import os
import shutil
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .util import MissingDependency, cache_home, have_module, lazy_import

MAX_SIDE = 1024          # longest side fed to segmentation models
DEPTH_SIDE = 518         # Depth Anything native resolution (multiple of 14)
VRAM_BUDGET_GB = 6.0     # target card; keep individual models well below this


@dataclass
class ModelInfo:
    name: str
    purpose: str
    licence: str
    size_mb: float                      # approximate download size
    kind: str                           # url | hf | pip
    source: str                         # URL or Hugging Face repo id
    filename: str = ""                  # file name inside the model dir (url kind)
    requires: tuple = ()                # python modules needed to *run* the model
    note: str = ""
    redistributable: bool = True        # False: weights must never be bundled/redistributed
    extra: dict = field(default_factory=dict)

    def describe(self) -> dict:
        d = asdict(self)
        d["path"] = str(path(self.name))
        d["installed"] = installed(self.name)
        d["deps_ok"] = all(have_module(m) for m in self.requires)
        return d


_RB = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"

REGISTRY: dict[str, ModelInfo] = {m.name: m for m in [
    ModelInfo("isnet-general-use", "ai_subject / ai_background: salient-object matte (default)",
              "code MIT (rembg); IS-Net weights Apache-2.0 upstream", 170, "url", _RB + "isnet-general-use.onnx",
              "isnet-general-use.onnx", ("onnxruntime",), "1024x1024 input, few seconds on CPU",
              extra={"size": 1024, "mean": (0.485, 0.456, 0.406), "std": (1.0, 1.0, 1.0), "post": "minmax"}),
    ModelInfo("u2net", "ai_subject: lighter salient-object matte", "Apache-2.0 (U^2-Net)", 176, "url",
              _RB + "u2net.onnx", "u2net.onnx", ("onnxruntime",), "320x320 input, fastest option",
              extra={"size": 320, "mean": (0.485, 0.456, 0.406), "std": (0.229, 0.224, 0.225), "post": "minmax"}),
    ModelInfo("birefnet-general-lite", "ai_subject: high-quality matte, hair/foliage (lite)", "MIT (BiRefNet)", 224,
              "url", _RB + "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx", "birefnet-general-lite.onnx",
              ("onnxruntime",), "1024x1024; fits 6 GB; tens of seconds on CPU",
              extra={"size": 1024, "mean": (0.485, 0.456, 0.406), "std": (0.229, 0.224, 0.225), "post": "logit"}),
    ModelInfo("birefnet-general", "ai_subject: best matte quality (hair, fur)", "MIT (BiRefNet)", 970, "url",
              _RB + "BiRefNet-general-epoch_244.onnx", "birefnet-general.onnx", ("onnxruntime",),
              "1024x1024; large; GPU recommended",
              extra={"size": 1024, "mean": (0.485, 0.456, 0.406), "std": (0.229, 0.224, 0.225), "post": "logit"}),
    ModelInfo("face-landmarker", "ai_face/eyes/lips/teeth/skin: 478 face landmarks (MediaPipe)",
              "Apache-2.0 (MediaPipe)", 4, "url",
              "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
              "face_landmarker.task", ("mediapipe",), "CPU real time"),
    ModelInfo("haar-face", "ai_face fallback: OpenCV Haar frontal-face cascade", "BSD (OpenCV)", 1, "url",
              "https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/haarcascade_frontalface_default.xml",
              "haarcascade_frontalface_default.xml", ("cv2",), "used only when no landmark model is installed"),
    ModelInfo("haar-eye", "ai_eyes fallback: OpenCV Haar eye cascade", "BSD (OpenCV)", 1, "url",
              "https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/haarcascade_eye.xml",
              "haarcascade_eye.xml", ("cv2",)),
    ModelInfo("depth-anything-v2-small", "depth mask / background blur: monocular depth (ViT-S, 25M params)",
              "Apache-2.0", 100, "url",
              "https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx",
              "depth_anything_v2_small.onnx", ("onnxruntime",), "518 px input; ~1 GB VRAM",
              extra={"size": 518, "hf_id": "depth-anything/Depth-Anything-V2-Small-hf"}),
    ModelInfo("depth-anything-v2-small-hf", "depth: Depth Anything V2 Small, transformers/torch build (fp16 on CUDA)",
              "Apache-2.0", 100, "hf", "depth-anything/Depth-Anything-V2-Small-hf", "", ("transformers", "torch"),
              "alternative to the onnx build; used when the onnx file is absent"),
    ModelInfo("segformer-ade-b0", "ai_sky: SegFormer-B0 ADE20K (class 2 = sky)",
              "NVIDIA SegFormer licence (research / personal use)", 15, "hf",
              "nvidia/segformer-b0-finetuned-ade-512-512", "", ("transformers", "torch"),
              "optional; the heuristic sky mask works without it", redistributable=False),
    ModelInfo("yolo11n-seg", "ai_animal: YOLO11 nano instance segmentation (COCO animals)",
              "AGPL-3.0 (Ultralytics): personal use OK, do not redistribute combined works", 6, "url",
              "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-seg.pt", "yolo11n-seg.pt",
              ("ultralytics",), redistributable=False),
    ModelInfo("big-lama", "inpaint op: LaMa large-mask inpainting", "Apache-2.0 (LaMa; verify LICENSE)", 200, "pip",
              "simple-lama-inpainting", "", ("simple_lama_inpainting",),
              "weights fetched by the pip package on first use; run `photo models download big-lama` to prefetch"),
]}


def models_dir() -> Path:
    p = cache_home() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def info(name: str) -> ModelInfo:
    if name not in REGISTRY:
        raise KeyError(f"unknown model '{name}'. Known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name]


def path(name: str) -> Path:
    """Expected location of a model (file for url models, directory for hf models)."""
    m = info(name)
    d = models_dir() / m.name
    return d / m.filename if m.kind == "url" else d


def installed(name: str) -> bool:
    m = info(name)
    p = path(name)
    if m.kind == "url":
        return p.is_file() and p.stat().st_size > 1000
    if m.kind == "hf":
        return p.is_dir() and any(p.iterdir())
    if m.kind == "pip":
        return have_module(m.requires[0]) if m.requires else False
    return False


def available(name: str | None = None) -> dict:
    """Status of one model or all: installed (weights on disk), deps_ok (python deps importable), usable."""
    def one(n: str) -> dict:
        m = info(n)
        deps = all(have_module(r) for r in m.requires)
        inst = installed(n)
        return {"installed": inst, "deps_ok": deps, "usable": inst and deps, "path": str(path(n))}
    if name is not None:
        return one(name)
    return {n: one(n) for n in REGISTRY}


def usable(name: str) -> bool:
    return available(name)["usable"]


def listing() -> list[dict]:
    return [REGISTRY[n].describe() for n in sorted(REGISTRY)]


def download(name: str, force: bool = False, progress=None) -> Path:
    """Download a model into the cache (explicit user action; the only place the network is touched)."""
    m = info(name)
    dest = path(name)
    if installed(name) and not force and m.kind != "pip":
        return dest
    if m.kind == "url":
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        req = urllib.request.Request(m.source, headers={"User-Agent": "photostudio-model-fetch"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if tmp.stat().st_size < 1000:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"download of {name} returned an empty file from {m.source}")
        shutil.move(str(tmp), str(dest))
        return dest
    if m.kind == "hf":
        hub = lazy_import("huggingface_hub", "model download")
        dest.mkdir(parents=True, exist_ok=True)
        hub.snapshot_download(repo_id=m.source, local_dir=str(dest))
        return dest
    if m.kind == "pip":
        raise MissingDependency(f"'{name}' is a pip package: run  pip install {m.source}  (weights are fetched on first use)")
    raise ValueError(m.kind)


def remove(name: str) -> bool:
    p = path(name)
    target = p.parent if info(name).kind == "url" else p
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
        return True
    return False


# ---------------------------------------------------------------- runtime helpers (GPU aware)
_SESSIONS: dict = {}


def ort_providers() -> list[str]:
    """CUDA first when onnxruntime-gpu is installed, otherwise CPU. PHOTOSTUDIO_CPU=1 forces CPU."""
    ort = lazy_import("onnxruntime", "ONNX model inference")
    avail = ort.get_available_providers()
    if os.environ.get("PHOTOSTUDIO_CPU"):
        return ["CPUExecutionProvider"]
    out = [p for p in ("CUDAExecutionProvider",) if p in avail]
    return out + ["CPUExecutionProvider"]


def ort_session(name: str):
    """Cached onnxruntime InferenceSession for a registry model (raises MissingDependency if unavailable)."""
    if name in _SESSIONS:
        return _SESSIONS[name]
    ort = lazy_import("onnxruntime", f"model '{name}'")
    if not installed(name):
        raise MissingDependency(f"model '{name}' is not downloaded. Run: photo models download {name}")
    so = ort.SessionOptions()
    so.log_severity_level = 3
    sess = ort.InferenceSession(str(path(name)), sess_options=so, providers=ort_providers())
    _SESSIONS[name] = sess
    return sess


def torch_device() -> str:
    try:
        torch = lazy_import("torch", "GPU inference")
        if torch.cuda.is_available() and not os.environ.get("PHOTOSTUDIO_CPU"):
            return "cuda"
    except MissingDependency:
        pass
    return "cpu"


def clear_sessions() -> None:
    _SESSIONS.clear()
