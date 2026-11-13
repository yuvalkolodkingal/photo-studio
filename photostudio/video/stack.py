"""Stack resolution for video: presets (mode/art/video), op classification (colour-only vs spatial) and the
per-shot auto-match op.

A *colour-only* op is a pure per-pixel function of RGB; those can be baked into a 3D LUT and applied with
ffmpeg's ``lut3d`` at near decode speed.  Spatial ops (denoise, sharpen, grain, vignette ...) are mapped to
native ffmpeg filters (see graph.py); raster art ops need the slow numpy path (``--frames python``).
"""
from __future__ import annotations

import copy
import functools
import importlib
from typing import Any

import numpy as np

from ..pipeline import registry
from ..pipeline.registry import Ctx, op

# names that the ffmpeg graph builder knows how to express natively
FFMPEG_SPATIAL = {"denoise", "sharpen", "capture_sharpen", "grain", "vignette", "local_contrast", "clarity",
                  "texture", "halation", "bloom", "orton", "blur"}
# ops that cannot work on video frames at all (geometry belongs to ffmpeg scale/crop/pad)
GEOMETRY_NAMES = {"crop", "rotate", "perspective", "lens_correct", "keystone", "straighten"}
COLOR_MASK_TYPES = {"luminosity", "color_range", "parametric"}


# ----------------------------------------------------------------------------- video-specific op
@op("video_match", 9, "linear", category="video", ev=0.0, gain=[1.0, 1.0, 1.0])
def video_match(img, p, ctx):
    """Per-shot exposure/white-balance match: linear-light gain (EV and per-channel gains), constant per shot."""
    g = np.asarray(p.get("gain") or [1, 1, 1], dtype=np.float32).reshape(1, 1, 3) * (2.0 ** float(p.get("ev", 0.0)))
    return (img * g).astype(np.float32)


# ----------------------------------------------------------------------------- registry access
_OP_MODULES = ("basic", "tone", "color_ops", "detail", "effects", "geometry", "retouch", "art", "art_common")


def ensure_ops() -> dict:
    """Load whatever op modules exist (other agents write them in parallel; a missing one must not break video)."""
    try:
        return registry.all_ops()
    except Exception:
        for m in _OP_MODULES:
            try:
                importlib.import_module(f"photostudio.pipeline.ops.{m}")
            except Exception:
                continue
        return registry.REGISTRY


def spec_for(name: str):
    return ensure_ops().get(name)


def engine_is_spatial(spec, entry: dict | None = None) -> bool | None:
    try:
        eng = importlib.import_module("photostudio.pipeline.engine")
        return bool(eng.is_spatial(spec, entry))
    except Exception:
        return None


# ----------------------------------------------------------------------------- presets
def load_look(kind: str, name: str | None) -> dict | None:
    """Load a mode/art/video preset dict via ``photostudio.presets`` (name or name:variant)."""
    if not name:
        return None
    from .. import presets

    return presets.load_preset(kind, name)


def merge_stacks(*stacks: list[dict] | None) -> list[dict]:
    """Merge stacks left to right with the contract rule (key = op + id; masked entries without id append)."""
    try:
        from ..presets import merge_stack

        out: list[dict] = []
        for s in stacks:
            out = merge_stack(out, s or [])
        return out
    except Exception:  # pragma: no cover - presets module missing
        out = []
        for s in stacks:
            for e in s or []:
                out.append(copy.deepcopy(e))
        return out


def resolve_video_stack(mode: str | None = None, art: str | None = None, user: list[dict] | None = None,
                        preset: dict | None = None) -> tuple[list[dict], dict]:
    """mode < art < video-preset stack < user edits.  No pipeline defaults: video is display-referred already,
    so default tone mapping / sharpening must not be re-applied per frame."""
    info: dict[str, Any] = {"mode": mode, "art": art}
    layers: list[list[dict] | None] = []
    m = load_look("mode", mode)
    if m:
        layers.append(m["stack"])
        info["mode_title"] = m.get("title")
    a = load_look("art", art)
    if a:
        layers.append(a["stack"])
        info["art_title"] = a.get("title")
    if preset and preset.get("stack"):
        layers.append(preset["stack"])
    if user:
        layers.append(user)
    return [e for e in merge_stacks(*layers) if e.get("op") != "raw"], info


# ----------------------------------------------------------------------------- classification
def entry_domain(spec) -> str:
    if spec.domain in ("linear", "display"):
        return spec.domain
    return "linear" if spec.stage <= 17 else "display"


def _probe_image(domain: str, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    hi = 1.5 if domain == "linear" else 1.0
    return (rng.random((32, 32, 3), dtype=np.float32) * hi).astype(np.float32)


def _make_ctx() -> Ctx:
    return Ctx(domain="linear", kind="jpeg", seed=1234, output_space="srgb")


def _params_key(name: str, params: dict) -> str:
    from ..util import params_hash

    return f"{name}:{params_hash(params)}"


@functools.lru_cache(maxsize=512)
def _pointwise_cached(key: str, name: str, params_json: str) -> bool:
    import json

    spec = spec_for(name)
    if spec is None:
        return False
    params = json.loads(params_json)
    dom = entry_domain(spec)
    img = _probe_image(dom)
    rng = np.random.default_rng(99)
    perm = rng.permutation(img.shape[0] * img.shape[1])
    flat = img.reshape(-1, 3)
    try:
        a = np.asarray(spec.fn(img.copy(), params, _make_ctx()), dtype=np.float32)
        if a.shape != img.shape:
            return False
        b = np.asarray(spec.fn(flat[perm].reshape(img.shape).copy(), params, _make_ctx()), dtype=np.float32)
        c = np.asarray(spec.fn(img[:16].copy(), params, _make_ctx()), dtype=np.float32)
    except Exception:
        return False
    ok_perm = np.allclose(b.reshape(-1, 3), a.reshape(-1, 3)[perm], atol=2e-4, rtol=1e-3)
    ok_crop = np.allclose(c, a[:16], atol=2e-4, rtol=1e-3)
    return bool(ok_perm and ok_crop)


def is_pointwise(name: str, params: dict) -> bool:
    """Empirical test: shuffling pixels or cropping the image must not change the per-pixel result."""
    import json

    return _pointwise_cached(_params_key(name, params), name, json.dumps(params, sort_keys=True, default=str))


def _mask_is_colour(mask: dict | None) -> bool:
    if not mask:
        return True
    t = mask.get("type")
    if t == "combine":
        return all(_mask_is_colour(m) for m in mask.get("masks", []))
    return t in COLOR_MASK_TYPES and not mask.get("refine") and float(mask.get("feather", 0) or 0) == 0.0


def classify_entry(entry: dict) -> tuple[str, str]:
    """-> ('color'|'spatial'|'geometry'|'unsupported'|'disabled', reason)."""
    name = entry.get("op")
    if not entry.get("enabled", True):
        return "disabled", "disabled"
    if name in ("raw",):
        return "disabled", "raw-only"
    if name in GEOMETRY_NAMES:
        return "geometry", "geometry is done by ffmpeg scale/crop/pad"
    spec = spec_for(name)
    if spec is None:
        if name in FFMPEG_SPATIAL:
            return "spatial", "native ffmpeg filter"
        if name == "lut3d":
            return "color", "lut3d"
        return "unsupported", f"unknown op '{name}'"
    if spec.raw_only:
        return "disabled", "raw-only"
    if spec.changes_geometry:
        return "geometry", "changes geometry"
    if name in FFMPEG_SPATIAL:
        return "spatial", "native ffmpeg filter"
    sp = engine_is_spatial(spec, entry)
    if sp:
        return "unsupported", "spatial op without an ffmpeg equivalent (use --frames python)"
    if not _mask_is_colour(entry.get("mask")):
        return "unsupported", "spatially varying mask (use --frames python)"
    try:
        params = spec.resolve_params(entry.get("params"), entry.get("v"))
    except Exception as exc:
        return "unsupported", f"bad params: {exc}"
    if not is_pointwise(name, params):
        return "unsupported", "not a per-pixel colour op (image-dependent or spatial; use --frames python)"
    return "color", "per-pixel"


def partition_stack(stack: list[dict]) -> dict:
    """Split a stack into LUT-bakeable colour ops, ffmpeg-mappable spatial ops and unsupported ones."""
    out: dict[str, list] = {"color": [], "spatial": [], "unsupported": [], "geometry": []}
    for e in stack:
        kind, why = classify_entry(e)
        if kind == "disabled":
            continue
        if kind == "color":
            out["color"].append(e)
        elif kind == "spatial":
            out["spatial"].append(e)
        else:
            out[kind if kind in out else "unsupported"].append({"op": e.get("op"), "reason": why})
    return out
