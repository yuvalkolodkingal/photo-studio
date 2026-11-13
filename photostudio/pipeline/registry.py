"""Op registry: every pipeline operation declares its fixed stage, domain, default params, version and
an optional migrate_params() upgrade path. The *stage* (code) fixes execution order; the edit stack
(sidecar) only says which ops are enabled and with which params."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

# Fixed execution order (spec table). Stage numbers are stable identifiers.
STAGES = {
    1: "raw_prepare", 2: "raw_white_balance", 3: "highlight_reconstruction", 4: "demosaic",
    5: "denoise", 6: "capture_sharpen", 7: "lens", 8: "retouch", 9: "camera_matrix",
    10: "exposure_color_calibration", 11: "geometry", 12: "tone_equalizer_dehaze", 13: "color_grading",
    14: "local_contrast", 15: "optical_looks_slot_A", 16: "tone_mapper", 17: "gamut_map_oetf",
    18: "look_slot_B", 19: "effects", 20: "raster_art_slot_C", 21: "output",
}
SLOT_STAGE = {"A": 15, "B": 18, "C": 20}
TONEMAP_STAGE = 16


@dataclass
class Ctx:
    domain: str = "linear"            # 'linear' (scene-referred Rec.2020) | 'display' (encoded output space)
    kind: str = "raw"                 # raw | jpeg | image
    meta: dict = field(default_factory=dict)
    path: str = ""
    seed: int = 0
    output_space: str = "srgb"
    cache: dict = field(default_factory=dict)
    source: np.ndarray | None = None  # linear source (post-decode) for AI masks / references
    log: list = field(default_factory=list)
    full_long_edge: int | None = None  # long edge of the full-res image (preview scaling of px params)
    shoulder: bool = True             # tone_map 'none' (JPEG): soft highlight shoulder; False = identity (LUT baking)

    def px(self, value_at_2000: float, img: np.ndarray) -> float:
        """Scale a pixel-size parameter defined at a 2000 px long edge to the current image."""
        return float(value_at_2000) * max(img.shape[:2]) / 2000.0

    def note(self, msg: str) -> None:
        self.log.append(msg)


@dataclass
class OpSpec:
    name: str
    fn: Callable[[np.ndarray, dict, Ctx], np.ndarray]
    stage: int
    domain: str = "any"               # linear | display | any
    defaults: dict = field(default_factory=dict)
    version: int = 1
    raw_only: bool = False
    changes_geometry: bool = False
    doc: str = ""
    category: str = "core"
    migrate: Callable[[dict, int], dict] | None = None
    choices: dict = field(default_factory=dict)

    def resolve_params(self, params: dict | None, version: int | None = None) -> dict:
        p = dict(params or {})
        if self.migrate and version is not None and version < self.version:
            p = self.migrate(p, version)
        out = {**self.defaults, **p}
        for k, allowed in self.choices.items():
            if k in out and out[k] not in allowed:
                raise ValueError(f"op '{self.name}': param {k}={out[k]!r} not in {allowed}")
        return out

    def describe(self) -> dict:
        return {"op": self.name, "stage": self.stage, "stage_name": STAGES.get(self.stage), "domain": self.domain,
                "version": self.version, "raw_only": self.raw_only, "category": self.category,
                "defaults": self.defaults, "choices": self.choices, "doc": self.doc}


REGISTRY: dict[str, OpSpec] = {}


def op(name: str, stage: int, domain: str = "any", version: int = 1, raw_only: bool = False,
       changes_geometry: bool = False, category: str = "core", choices: dict | None = None,
       migrate: Callable | None = None, **defaults: Any):
    def deco(fn):
        REGISTRY[name] = OpSpec(name, fn, stage, domain, defaults, version, raw_only, changes_geometry,
                                (fn.__doc__ or "").strip().split("\n")[0], category, migrate, choices or {})
        return fn
    return deco


def get(name: str) -> OpSpec:
    _ensure_loaded()
    if name not in REGISTRY:
        raise KeyError(f"unknown op '{name}'. Known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name]


import threading

_loaded = False
_LOAD_LOCK = threading.RLock()
OPS_MODULES = ("basic", "tone", "color_ops", "detail", "geometry", "effects", "art", "retouch")
LOAD_ERRORS: dict[str, str] = {}


def _ensure_loaded():
    """Import every ops module tolerantly: a missing or broken module logs a warning and is skipped, so one
    agent's bug never takes the whole pipeline down (see LOAD_ERRORS for the reasons)."""
    global _loaded
    if _loaded:
        return
    import importlib
    import logging

    with _LOAD_LOCK:
        if _loaded:
            return
        _load_all(importlib, logging)
        _loaded = True


def _load_all(importlib, logging):
    for name in OPS_MODULES:
        full = f"{__package__}.ops.{name}"
        try:
            importlib.import_module(full)
        except ModuleNotFoundError as exc:
            if exc.name == full:  # module simply not written yet
                LOAD_ERRORS[name] = "missing"
                logging.getLogger("photostudio").debug("ops module %s not present", full)
                continue
            LOAD_ERRORS[name] = f"{type(exc).__name__}: {exc}"
            logging.getLogger("photostudio").warning("ops module %s failed to import: %s", full, exc)
        except Exception as exc:  # noqa: BLE001
            LOAD_ERRORS[name] = f"{type(exc).__name__}: {exc}"
            logging.getLogger("photostudio").warning("ops module %s failed to import: %s", full, exc)


def all_ops() -> dict[str, OpSpec]:
    _ensure_loaded()
    return REGISTRY
