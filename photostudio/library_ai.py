"""Lazy optional ML helpers shared by cull / tags / albums: CLIP embeddings, the LAION aesthetic head,
InsightFace, MediaPipe face mesh, Florence-2 captions and Recognize-Anything tags.

Nothing here imports a heavy dependency at import time and nothing touches the network: weights are looked
up under ``photostudio.models.models_dir()`` (filled by an explicit ``photo models download``) or an env
override, and a missing piece raises `MissingDependency` with the exact install hint. Every caller in the
library has a non-AI fallback and treats these errors as "feature unavailable".

Expected weight locations (all under ``models_dir()``):
    clip-vit-l-14/ViT-L-14.pt                      OpenAI CLIP ViT-L/14 (env PHOTOSTUDIO_CLIP_WEIGHTS)
    laion-aesthetic/sa_0_4_vit_l_14_linear.pth     LAION linear aesthetic head (768 -> 1)
    insightface/models/buffalo_l/                  InsightFace pack (detection + ArcFace-style recognition)
    florence-2-base/                               HF snapshot of microsoft/Florence-2-base
    ram/ram_plus_swin_large_14m.pth                Recognize Anything (RAM++) checkpoint
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from .util import MissingDependency, have_module, lazy_import

_CACHE: dict = {}


def models_root() -> Path:
    try:
        from .models import models_dir

        return models_dir()
    except Exception:  # models.py is owned by another agent; degrade to our own cache location
        from .util import cache_home

        p = cache_home() / "models"
        p.mkdir(parents=True, exist_ok=True)
        return p


def _device() -> str:
    try:
        torch = lazy_import("torch", "GPU inference")
        if torch.cuda.is_available() and not os.environ.get("PHOTOSTUDIO_CPU"):
            return "cuda"
    except MissingDependency:
        pass
    return "cpu"


def _need(path: Path, what: str, hint: str) -> Path:
    if not path.exists():
        raise MissingDependency(f"{what} not found at {path}. {hint}")
    return path


def status() -> dict:
    """Which optional capabilities are usable right now (for `photo status`, tests and error messages)."""
    root = models_root()
    clip_w = Path(os.environ.get("PHOTOSTUDIO_CLIP_WEIGHTS", root / "clip-vit-l-14" / "ViT-L-14.pt"))
    return {
        "clip": have_module("open_clip") and have_module("torch") and clip_w.exists(),
        "aesthetic": (root / "laion-aesthetic" / "sa_0_4_vit_l_14_linear.pth").exists(),
        "mediapipe": have_module("mediapipe"),
        "insightface": have_module("insightface") and (root / "insightface" / "models" / "buffalo_l").exists(),
        "florence2": have_module("transformers") and (root / "florence-2-base").exists(),
        "ram": have_module("ram") and (root / "ram" / "ram_plus_swin_large_14m.pth").exists(),
        "reverse_geocoder": have_module("reverse_geocoder"),
    }


def to_pil(img: np.ndarray):
    from PIL import Image

    a = np.clip(img, 0, 1)
    return Image.fromarray((a * 255 + 0.5).astype(np.uint8))


# ------------------------------------------------------------------------------------ CLIP
class ClipModel:
    def __init__(self):
        open_clip = lazy_import("open_clip", "CLIP embeddings")
        self.torch = lazy_import("torch", "CLIP embeddings")
        w = Path(os.environ.get("PHOTOSTUDIO_CLIP_WEIGHTS", models_root() / "clip-vit-l-14" / "ViT-L-14.pt"))
        _need(w, "CLIP ViT-L/14 weights", "Download ViT-L-14.pt (OpenAI) into that folder or set PHOTOSTUDIO_CLIP_WEIGHTS")
        self.device = _device()
        model, _, self.preprocess = open_clip.create_model_and_transforms("ViT-L-14", pretrained=str(w),
                                                                         device=self.device)
        self.model = model.eval()
        self.tokenizer = open_clip.get_tokenizer("ViT-L-14")
        self.dim = 768

    def encode_images(self, imgs: list[np.ndarray], batch: int = 16) -> np.ndarray:
        torch = self.torch
        out = []
        for i in range(0, len(imgs), batch):
            x = torch.stack([self.preprocess(to_pil(im)) for im in imgs[i:i + batch]]).to(self.device)
            with torch.no_grad():
                f = self.model.encode_image(x.half() if self.device == "cuda" else x)
            f = f / f.norm(dim=-1, keepdim=True)
            out.append(f.float().cpu().numpy())
        return np.concatenate(out, 0) if out else np.zeros((0, self.dim), np.float32)

    def encode_text(self, texts: list[str], batch: int = 64) -> np.ndarray:
        torch = self.torch
        out = []
        for i in range(0, len(texts), batch):
            tok = self.tokenizer(texts[i:i + batch]).to(self.device)
            with torch.no_grad():
                f = self.model.encode_text(tok)
            f = f / f.norm(dim=-1, keepdim=True)
            out.append(f.float().cpu().numpy())
        return np.concatenate(out, 0) if out else np.zeros((0, self.dim), np.float32)


def clip_model() -> ClipModel:
    if "clip" not in _CACHE:
        _CACHE["clip"] = ClipModel()
    return _CACHE["clip"]


def aesthetic_head():
    """LAION linear aesthetic head (torch Linear(768, 1)); score ~1..10, a within-shoot ranking signal only."""
    if "aes" not in _CACHE:
        torch = lazy_import("torch", "aesthetic scoring")
        p = _need(models_root() / "laion-aesthetic" / "sa_0_4_vit_l_14_linear.pth", "LAION aesthetic head",
                  "Download sa_0_4_vit_l_14_linear.pth (christophschuhmann/improved-aesthetic-predictor)")
        head = torch.nn.Linear(768, 1)
        head.load_state_dict(torch.load(str(p), map_location="cpu"))
        _CACHE["aes"] = head.eval()
    return _CACHE["aes"]


def aesthetic_from_embeddings(emb: np.ndarray) -> np.ndarray:
    torch = lazy_import("torch", "aesthetic scoring")
    head = aesthetic_head()
    with torch.no_grad():
        e = torch.from_numpy(np.asarray(emb, np.float32))
        e = e / e.norm(dim=-1, keepdim=True)
        return head(e).squeeze(-1).numpy()


# ------------------------------------------------------------------------------------ faces
def insightface_app():
    if "insight" not in _CACHE:
        fa = lazy_import("insightface.app", "face recognition")
        root = models_root() / "insightface"
        _need(root / "models" / "buffalo_l", "InsightFace buffalo_l pack",
              "Place the buffalo_l pack there (weights are non-commercial research only and are never bundled)")
        try:
            from .models import ort_providers

            providers = ort_providers()
        except Exception:
            providers = ["CPUExecutionProvider"]
        app = fa.FaceAnalysis(name="buffalo_l", root=str(root), providers=providers)
        app.prepare(ctx_id=0 if "CUDAExecutionProvider" in providers else -1, det_size=(640, 640))
        _CACHE["insight"] = app
    return _CACHE["insight"]


def insightface_faces(img_display: np.ndarray, min_score: float = 0.6) -> list[dict]:
    """[{bbox:[x0,y0,x1,y1] px, score, emb: unit float32 (512,), kps}] for a display float image."""
    app = insightface_app()
    bgr = (np.clip(img_display, 0, 1)[..., ::-1] * 255 + 0.5).astype(np.uint8)
    out = []
    for f in app.get(np.ascontiguousarray(bgr)):
        if float(f.det_score) < min_score:
            continue
        e = np.asarray(f.normed_embedding if getattr(f, "normed_embedding", None) is not None else f.embedding,
                       np.float32)
        e = e / max(float(np.linalg.norm(e)), 1e-9)
        out.append({"bbox": [float(v) for v in f.bbox], "score": float(f.det_score), "emb": e,
                    "kps": np.asarray(f.kps).tolist() if getattr(f, "kps", None) is not None else None})
    return out


def face_mesh():
    """MediaPipe FaceMesh (legacy solutions API, 478 landmarks with irises) or MissingDependency."""
    if "mesh" not in _CACHE:
        mp = lazy_import("mediapipe", "eye-blink detection")
        try:
            fm = mp.solutions.face_mesh.FaceMesh(static_image_mode=True, max_num_faces=12, refine_landmarks=True,
                                                 min_detection_confidence=0.5)
        except AttributeError as exc:
            raise MissingDependency("This mediapipe build has no legacy `solutions.face_mesh` API") from exc
        _CACHE["mesh"] = fm
    return _CACHE["mesh"]


# ------------------------------------------------------------------------------------ captions / tags
def florence_caption(img_display: np.ndarray, task: str = "<DETAILED_CAPTION>") -> str:
    """Florence-2-base caption (local files only; fp16 on CUDA, fp32 on CPU)."""
    if "flo" not in _CACHE:
        tr = lazy_import("transformers", "Florence-2 captions")
        torch = lazy_import("torch", "Florence-2 captions")
        d = _need(models_root() / "florence-2-base", "Florence-2-base snapshot",
                  "huggingface-cli download microsoft/Florence-2-base --local-dir <that folder>")
        dev = _device()
        dt = torch.float16 if dev == "cuda" else torch.float32
        model = tr.AutoModelForCausalLM.from_pretrained(str(d), torch_dtype=dt, trust_remote_code=True,
                                                        local_files_only=True).to(dev).eval()
        proc = tr.AutoProcessor.from_pretrained(str(d), trust_remote_code=True, local_files_only=True)
        _CACHE["flo"] = (model, proc, dev, dt)
    model, proc, dev, dt = _CACHE["flo"]
    torch = lazy_import("torch")
    pil = to_pil(img_display)
    inputs = proc(text=task, images=pil, return_tensors="pt").to(dev, dt)
    with torch.no_grad():
        ids = model.generate(input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"], max_new_tokens=128,
                             num_beams=3, do_sample=False)
    text = proc.batch_decode(ids, skip_special_tokens=False)[0]
    res = proc.post_process_generation(text, task=task, image_size=pil.size)
    return str(res.get(task, "")).strip()


def ram_tags(img_display: np.ndarray, threshold: float = 0.86) -> list[tuple[str, float]]:
    """Recognize-Anything (RAM++) open-vocabulary tags; returns [(tag, score)] above `threshold`."""
    if "ram" not in _CACHE:
        ram = lazy_import("ram", "RAM++ tagging")
        models = lazy_import("ram.models")
        torch = lazy_import("torch", "RAM++ tagging")
        ck = _need(models_root() / "ram" / "ram_plus_swin_large_14m.pth", "RAM++ checkpoint",
                   "Download ram_plus_swin_large_14m.pth from xinyu1205/recognize-anything")
        dev = _device()
        m = models.ram_plus(pretrained=str(ck), image_size=384, vit="swin_l").eval().to(dev)
        _CACHE["ram"] = (ram, m, dev)
    ram, m, dev = _CACHE["ram"]
    torch = lazy_import("torch")
    tf = lazy_import("ram").get_transform(image_size=384)
    x = tf(to_pil(img_display)).unsqueeze(0).to(dev)
    with torch.no_grad():
        tags, _ = ram.inference_ram(x, m)
    return [(t.strip(), 1.0) for t in str(tags).split("|") if t.strip()]
