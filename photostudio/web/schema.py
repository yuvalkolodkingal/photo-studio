"""Op schema for the UI: registry introspection (defaults, choices, stage, domain, category) plus a curated
``UI_TABLE`` with slider ranges / labels / display scales. Ops (or params) that are not in the table fall back to
name- and default-based heuristics, so every registered op is reachable from the "All ops" panel.

Descriptor of one parameter::

    {name, type: number|int|bool|choice|color|vec|json|string, default, min, max, step, label, unit, scale,
     digits, choices, nullable, hint}

``scale`` is a *display* multiplier (the stored value is always the native op value): a contrast of 0.15 is
shown as 15.
"""
from __future__ import annotations

import math
import re
from typing import Any

from ..pipeline import registry
from ..pipeline.registry import STAGES

N = lambda lo, hi, step=0.01, **kw: {"min": lo, "max": hi, "step": step, **kw}  # noqa: E731

# --------------------------------------------------------------------------------------------- curated ranges
UI_TABLE: dict[str, dict[str, Any]] = {
    "exposure": {"label": "Exposure", "params": {
        "ev": N(-5, 5, 0.01, label="Exposure", unit="EV", digits=2), "black": N(-0.05, 0.05, 0.001, label="Black offset", digits=3)}},
    "white_balance": {"label": "White balance", "params": {
        "temperature": N(2000, 12000, 10, label="Temp", unit="K", digits=0), "tint": N(-150, 150, 1, label="Tint", digits=0)}},
    "temperature": {"label": "Creative warm/cool", "params": {
        "temperature": N(-100, 100, 1, label="Warm / cool", digits=0), "tint": N(-100, 100, 1, label="Tint", digits=0)}},
    "contrast": {"label": "Contrast", "params": {
        "contrast": N(-0.8, 0.8, 0.005, label="Contrast", scale=100, digits=0), "fulcrum": N(0.02, 0.5, 0.005, label="Fulcrum", digits=3)}},
    "highlights_shadows": {"label": "Highlights / shadows", "params": {
        "highlights": N(-100, 100, 1, label="Highlights", digits=0), "shadows": N(-100, 100, 1, label="Shadows", digits=0),
        "whites": N(-100, 100, 1, label="Whites", digits=0), "blacks": N(-100, 100, 1, label="Blacks", digits=0),
        "radius": N(5, 200, 1, label="Radius", unit="px"), "edge_ev": N(0.1, 2.0, 0.05, label="Edge"),
        "tonal_width": N(0.5, 4.0, 0.05), "shadow_pivot": N(-6, 0, 0.1), "highlight_pivot": N(0, 4, 0.1)}},
    "local_contrast": {"label": "Local contrast", "params": {
        "amount": N(-1, 1.5, 0.01, label="Local contrast", scale=100, digits=0), "clarity": N(-1, 1.5, 0.01, label="Clarity", scale=100, digits=0),
        "texture": N(-1, 1.5, 0.01, label="Texture", scale=100, digits=0), "structure": N(-1, 1.5, 0.01, label="Structure", scale=100, digits=0),
        "sigma_ev": N(0.2, 3, 0.05), "protect_extremes": N(0, 1, 0.01), "max_gain_ev": N(0.5, 4, 0.1)}},
    "dehaze": {"label": "Dehaze", "params": {
        "strength": N(-1, 1, 0.01, label="Dehaze", scale=100, digits=0), "distance": N(0.1, 1, 0.01), "patch": N(3, 60, 1),
        "t0": N(0.05, 0.6, 0.01), "refine_radius": N(10, 200, 1)}},
    "vibrance": {"label": "Vibrance", "params": {"amount": N(-100, 100, 1, label="Vibrance", digits=0), "protect_skin": N(0, 1, 0.01, label="Protect skin")}},
    "saturation": {"label": "Saturation", "params": {"amount": N(-100, 100, 1, label="Saturation", digits=0), "gate": N(0, 0.2, 0.005)}},
    "hsl_mixer": {"label": "Colour mixer", "params": {"gate": N(0, 0.2, 0.005), "softness": N(0.2, 3, 0.05)}},
    "color_balance": {"label": "Colour balance (scene)", "params": {
        "contrast": N(-0.8, 0.8, 0.005, scale=100, digits=0), "contrast_fulcrum": N(0.02, 0.5, 0.005),
        "hue_shift": N(-180, 180, 1, unit="deg"), "chroma": N(-100, 100, 1), "saturation": N(-100, 100, 1),
        "vibrance": N(-100, 100, 1), "brilliance": N(-100, 100, 1), "brilliance_shadows": N(-100, 100, 1),
        "brilliance_highlights": N(-100, 100, 1), "protect_skin": N(0, 1, 0.01), "mask_falloff": N(0.3, 3, 0.05),
        "power_pivot": N(0.1, 2, 0.01)}},
    "color_grade": {"label": "Lift / gamma / gain", "params": {
        "contrast": N(-1, 1, 0.01), "pivot": N(0.1, 0.9, 0.005), "saturation": N(-1, 1, 0.01), "strength": N(0, 1, 0.01)}},
    "split_tone": {"label": "Split toning", "params": {
        "shadow_hue": N(0, 360, 1, label="Shadow hue", unit="deg", digits=0), "shadow_sat": N(0, 100, 1, label="Shadow saturation", digits=0),
        "highlight_hue": N(0, 360, 1, label="Highlight hue", unit="deg", digits=0), "highlight_sat": N(0, 100, 1, label="Highlight saturation", digits=0),
        "balance": N(-100, 100, 1, label="Balance", digits=0), "softness": N(0, 1, 0.01), "protect_ends": N(0, 1, 0.01), "strength": N(0, 1, 0.01)}},
    "tone_curve": {"label": "Tone curve", "params": {"strength": N(0, 1, 0.01)}},
    "levels": {"label": "Levels", "params": {
        "black": N(0, 0.5, 0.001, digits=3), "white": N(0.5, 1, 0.001, digits=3), "gamma": N(0.2, 3, 0.01), "out_black": N(0, 0.5, 0.001),
        "out_white": N(0.5, 1, 0.001), "auto_clip": N(0, 0.05, 0.0005)}},
    "monochrome": {"label": "Black & white", "params": {
        "filter_strength": N(0, 2, 0.01, label="Filter strength"), "amount": N(0, 1, 0.01, label="Amount", scale=100, digits=0)}},
    "sharpen": {"label": "Sharpen", "params": {
        "amount": N(0, 3, 0.01, label="Amount", digits=2), "radius": N(0.3, 6, 0.05, label="Radius", unit="px@2k", digits=2),
        "threshold": N(0, 0.1, 0.001, label="Threshold", digits=3), "masking": N(0, 1, 0.01, label="Masking", scale=100, digits=0),
        "halo": N(0, 1, 0.01, label="Halo limit", scale=100, digits=0)}},
    "capture_sharpen": {"label": "Capture sharpen", "params": {
        "amount": N(0, 2, 0.01), "radius": N(0.3, 4, 0.05), "iterations": N(1, 30, 1), "threshold": N(0, 10, 0.1), "damping": N(0, 1, 0.01)}},
    "deconvolve": {"label": "Deconvolution", "params": {
        "radius": N(0.3, 4, 0.05), "iterations": N(1, 40, 1), "amount": N(0, 2, 0.01), "damping": N(0, 2, 0.01), "threshold": N(0, 10, 0.1)}},
    "denoise": {"label": "Denoise", "params": {
        "luma": N(0, 100, 1, label="Luminance", digits=0), "chroma": N(0, 100, 1, label="Colour", digits=0),
        "detail": N(0, 100, 1, label="Detail", digits=0), "scales": N(1, 8, 1), "wb_gain": N(0.5, 4, 0.05)}},
    "lens_correct": {"label": "Lens corrections", "params": {
        "scale": N(-0.2, 0.2, 0.005, label="Scale", digits=3), "k1": N(-0.5, 0.5, 0.001, label="Distortion k1", digits=3),
        "k2": N(-0.5, 0.5, 0.001, label="Distortion k2", digits=3), "vignette": N(-2, 2, 0.01, label="Vignette fix"),
        "focal": N(4, 800, 0.5), "aperture": N(0.7, 64, 0.1)}},
    "ca_correct": {"label": "Chromatic aberration", "params": {
        "red": N(-1, 1, 0.01, label="Red/cyan", digits=2), "blue": N(-1, 1, 0.01, label="Blue/yellow", digits=2), "strength": N(0, 2, 0.01)}},
    "defringe": {"label": "Defringe", "params": {
        "amount": N(0, 100, 1, label="Amount", digits=0), "radius": N(0.5, 12, 0.1, label="Radius"), "threshold": N(0, 0.2, 0.005),
        "chroma_min": N(0, 0.2, 0.005)}},
    "rotate": {"label": "Straighten", "params": {"angle": N(-45, 45, 0.05, label="Angle", unit="deg", digits=2)}},
    "straighten": {"label": "Auto straighten", "params": {"max_angle": N(1, 30, 0.5), "amount": N(0, 1, 0.01), "angle": N(-45, 45, 0.05)}},
    "auto_level": {"label": "Auto level", "params": {"max_angle": N(1, 30, 0.5), "amount": N(0, 1, 0.01), "angle": N(-45, 45, 0.05)}},
    "perspective": {"label": "Perspective", "params": {
        "vertical": N(-100, 100, 1, label="Vertical", digits=0), "horizontal": N(-100, 100, 1, label="Horizontal", digits=0),
        "rotate": N(-45, 45, 0.05, label="Rotate", unit="deg", digits=2), "strength": N(0, 1, 0.01), "focal": N(8, 400, 1), "max_tilt": N(5, 60, 1)}},
    "vignette": {"label": "Vignette", "params": {
        "amount": N(-4, 2, 0.01, label="Amount", unit="EV", digits=2), "midpoint": N(0, 1, 0.01, label="Midpoint", scale=100, digits=0),
        "roundness": N(0, 1, 0.01, label="Roundness", scale=100, digits=0), "feather": N(0, 1, 0.01, label="Feather", scale=100, digits=0),
        "highlights": N(0, 1, 0.01, label="Highlights", scale=100, digits=0), "center_x": N(0, 1, 0.005), "center_y": N(0, 1, 0.005)}},
    "grain": {"label": "Grain", "params": {
        "amount": N(0, 0.2, 0.001, label="Amount", scale=1000, digits=0), "size": N(0.3, 4, 0.05, label="Size", digits=2),
        "roughness": N(0, 1, 0.01, label="Roughness", scale=100, digits=0), "chroma": N(0, 1, 0.01), "midtone_bias": N(0, 1, 0.01),
        "shadow_bias": N(0, 1, 0.01), "clump": N(0, 1, 0.01)}},
    "halation": {"label": "Halation", "params": {
        "threshold": N(0, 2, 0.01, label="Threshold"), "knee": N(0, 1, 0.01), "radius": N(1, 80, 0.5, label="Radius"),
        "strength": N(0, 2, 0.01, label="Strength"), "spread": N(0.5, 8, 0.1)}},
    "bloom": {"label": "Bloom", "params": {
        "threshold": N(0, 2, 0.01), "knee": N(0, 1, 0.01), "radius": N(1, 80, 0.5), "strength": N(0, 2, 0.01), "scales": N(1, 6, 1)}},
    "glow": {"label": "Glow", "params": {
        "threshold": N(0, 1, 0.01), "radius": N(2, 120, 1), "strength": N(0, 2, 0.01), "softness": N(0, 1, 0.01)}},
    "orton": {"label": "Orton", "params": {
        "amount": N(0, 1, 0.01), "radius": N(2, 100, 1), "brightness": N(-1, 1, 0.01), "contrast": N(0.5, 2, 0.01), "protect_highlights": N(0, 1, 0.01)}},
    "soft_focus": {"label": "Soft focus", "params": {
        "amount": N(0, 1, 0.01), "radius": N(1, 60, 0.5), "halo": N(0, 1, 0.01), "contrast_drop": N(0, 1, 0.01)}},
    "tilt_shift": {"label": "Tilt shift", "params": {
        "focus_y": N(0, 1, 0.005), "focus_height": N(0.02, 0.8, 0.005), "angle": N(-90, 90, 0.5), "blur": N(0, 60, 0.5),
        "falloff": N(0, 1, 0.01), "saturation": N(0.5, 2, 0.01), "contrast": N(0.5, 2, 0.01)}},
    "fog_haze": {"label": "Fog / haze", "params": {
        "amount": N(0, 1, 0.01), "shadow_lift": N(0, 1, 0.01), "gradient": N(0, 1, 0.01)}},
    "film_emulation": {"label": "Film emulation", "params": {
        "strength": N(0, 1, 0.01), "intensity": N(0, 1, 0.01), "saturation": N(0, 2, 0.01), "contrast": N(0.5, 2, 0.01),
        "warmth": N(-1, 1, 0.01), "grain": N(0, 3, 0.01), "halation": N(-1, 2, 0.01), "fade": N(0, 1, 0.01)}},
    "tone_map": {"label": "Tone mapper", "params": {
        "contrast": N(0.5, 3, 0.01), "skew": N(-1, 1, 0.01), "hue_preserve": N(0, 1, 0.01), "white": N(0.5, 2, 0.01),
        "black": N(0, 0.2, 0.001), "white_ev": N(0, 8, 0.05), "black_ev": N(-14, -2, 0.1), "latitude": N(0, 1, 0.01),
        "balance": N(-1, 1, 0.01), "highlight_desat": N(0, 1, 0.01)}},
    "skin_smooth": {"label": "Skin smoothing", "params": {
        "amount": N(0, 1, 0.01), "radius": N(1, 30, 0.5), "texture": N(0, 1, 0.01), "smooth_eps": N(0.001, 0.2, 0.001), "even_tone": N(0, 1, 0.01)}},
    "eye_enhance": {"label": "Eye enhance", "params": {
        "amount": N(0, 1, 0.01), "contrast": N(0, 1, 0.01), "saturation": N(0, 1, 0.01), "sclera": N(0, 1, 0.01), "sharpen": N(0, 1, 0.01)}},
    "teeth_whiten": {"label": "Teeth whitening", "params": {"amount": N(0, 1, 0.01), "lift": N(0, 0.2, 0.005)}},
    "sky_enhance": {"label": "Sky enhance", "params": {
        "darken": N(0, 1, 0.01), "saturation": N(0, 1, 0.01), "clouds": N(0, 1, 0.01), "gradient": N(0, 1, 0.01), "cloud_radius": N(2, 80, 1)}},
    "background_blur": {"label": "Background blur", "params": {
        "radius": N(1, 80, 0.5), "focus": N(0, 1, 0.01), "focus_range": N(0, 1, 0.01), "falloff": N(0, 1, 0.01),
        "levels": N(2, 10, 1), "highlight_boost": N(0, 1, 0.01), "edge_grow": N(0, 6, 0.1)}},
    "tone_equalizer": {"label": "Tone equalizer", "params": {
        "bands": N(-4, 4, 0.05, label="Bands (-8..0 EV)"),
        "smoothing": N(0, 1, 0.01), "smoothing_diameter": N(1, 20, 0.5), "edge_ev": N(0.1, 2, 0.05), "diffusion": N(0, 5, 1),
        "exposure_comp": N(-3, 3, 0.05), "contrast_comp": N(0.3, 2, 0.01)}},
    "output_sharpen": {"label": "Output sharpen", "params": {"threshold": N(0, 0.1, 0.001)}},
    "resize": {"label": "Resize", "params": {"long_edge": N(100, 12000, 10), "scale": N(0.05, 4, 0.01)}},
    "letterbox": {"label": "Letterbox", "params": {"aspect": N(1, 4, 0.01), "feather": N(0, 1, 0.01), "offset": N(-1, 1, 0.01)}},
    "border": {"label": "Border", "params": {"width": N(0, 300, 1)}},
}
HEURISTIC_RE = [
    (re.compile(r"(_hue|^hue)$"), N(-180, 180, 1, unit="deg")),
    (re.compile(r"(_sat|_lum)$"), N(-100, 100, 1)),
    (re.compile(r"^(seed)$"), {"type": "int", "min": 0, "max": 9999, "step": 1}),
    (re.compile(r"(angle|rotation)$"), N(-180, 180, 0.5, unit="deg")),
    (re.compile(r"^(intensity|mix|opacity|strength|amount|softness|smooth|flow|hardness)$"), N(0, 1, 0.01)),
    (re.compile(r"^(grain|noise|texture|haze|glow|dust)$"), N(0, 0.3, 0.001)),
    (re.compile(r"(radius|blur|width|size|pitch|spread)$"), N(0, 100, 0.5)),
]
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

NULLABLE_STR = {"camera", "lens", "path", "aspect"}
NULLABLE_NUM = {"a": N(0, 0.01, 0.00001), "b": N(0, 0.01, 0.00001), "sigma": N(0, 0.2, 0.001), "distance": N(0.1, 100, 0.1),
                "ppi": N(72, 600, 1), "short_edge": N(100, 12000, 10), "height": N(100, 12000, 10), "width": N(100, 12000, 10),
                "megapixels": N(0.1, 100, 0.1), "long_edge": N(100, 12000, 10), "scale": N(0.05, 4, 0.01), "radius": N(0.5, 100, 0.5),
                "amount": N(0, 3, 0.01), "focal": N(4, 800, 0.5), "aperture": N(0.7, 64, 0.1)}

CATEGORY_GROUPS = {"core": "Core", "tone": "Tone", "color": "Colour", "detail": "Detail", "geometry": "Geometry",
                   "optics": "Optics", "retouch": "Retouch", "effects": "Effects", "look": "Looks", "film": "Film",
                   "cinema": "Cinema", "photographer": "Photographer", "process": "Process", "art": "Raster art"}


def _heuristic(name: str, default: Any) -> dict:
    for rx, d in HEURISTIC_RE:
        if rx.search(name):
            d = dict(d)
            if isinstance(default, (int, float)) and not isinstance(default, bool):
                if default > d.get("max", 1):
                    d["max"] = math.ceil(default * 2.5)
                if default < d.get("min", 0):
                    d["min"] = math.floor(default * 2.5)
            return d
    if isinstance(default, float) or isinstance(default, int):
        d = float(default)
        if d == 0:
            return N(-1, 1, 0.01)
        if 0 < d <= 1:
            return N(0, 1, 0.01)
        if d > 1:
            hi = 10 ** math.ceil(math.log10(d * 2.0))
            return N(0, hi, hi / 200 if hi > 20 else 0.1 if hi > 5 else 0.05)
        return N(math.floor(d * 3), -math.floor(d * 3), 0.01)
    return {}


def _nice_label(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def describe_param(op: str, name: str, default: Any, choices: dict | None, table: dict) -> dict:
    ui = (table.get(op) or {}).get("params", {}).get(name, {})
    d: dict[str, Any] = {"name": name, "default": default, "label": ui.get("label") or _nice_label(name)}
    ch = (choices or {}).get(name)
    if ch:
        d["type"] = "choice"
        d["choices"] = list(ch)
        d["nullable"] = None in ch
        return d
    if isinstance(default, bool):
        d["type"] = "bool"
        return d
    if isinstance(default, str):
        d["type"] = "color" if HEX_RE.match(default) else "string"
        return d
    if default is None and not ui and name in NULLABLE_STR:
        d["type"], d["nullable"] = "string", True
        return d
    if default is None and not ui and name in NULLABLE_NUM:
        ui = NULLABLE_NUM[name]
    if isinstance(default, (int, float)) or (default is None and (ui or any(rx.search(name) for rx, _ in HEURISTIC_RE))):
        h = {**_heuristic(name, default), **ui}
        is_int = isinstance(default, int) and not isinstance(default, bool) and h.get("step", 1) >= 1
        d["type"] = h.get("type") or ("int" if is_int else "number")
        d["nullable"] = default is None
        for k in ("min", "max", "step", "unit", "scale", "digits"):
            if k in h:
                d[k] = h[k]
        d.setdefault("min", 0)
        d.setdefault("max", 1)
        d.setdefault("step", 0.01)
        if default is None:
            d["default"] = None
        return d
    if isinstance(default, (list, tuple)):
        if default and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in default) and len(default) <= 9:
            d["type"] = "vec"
            d["size"] = len(default)
            h = {**_heuristic(name, default[0] if default else 0), **ui}
            d.update({k: h[k] for k in ("min", "max", "step") if k in h})
            return d
        if default and all(isinstance(v, str) and HEX_RE.match(v) for v in default):
            d["type"] = "colors"
            return d
    d["type"] = "json"
    d["nullable"] = default is None
    return d


def describe_op(spec: registry.OpSpec, table: dict | None = None) -> dict:
    table = UI_TABLE if table is None else table
    base = spec.describe()
    ui = table.get(spec.name, {})
    base["label"] = ui.get("label") or _nice_label(spec.name)
    base["params"] = [describe_param(spec.name, k, v, spec.choices, table) for k, v in spec.defaults.items()]
    base["changes_geometry"] = bool(spec.changes_geometry)
    base["curated"] = spec.name in table
    return base


_CACHE: dict[str, Any] = {}


def build_schema() -> dict:
    """The full UI schema: all registered ops (name -> descriptor), stage table, categories and mask types."""
    if "schema" in _CACHE:
        return _CACHE["schema"]
    ops = registry.all_ops()
    try:
        from ..masks import MASK_TYPES
    except Exception:  # noqa: BLE001
        MASK_TYPES = ["linear", "radial", "brush", "luminosity", "color_range", "ai_subject", "combine"]
    sch = {
        "ops": {n: describe_op(s) for n, s in sorted(ops.items(), key=lambda kv: (kv[1].stage, kv[0]))},
        "stages": {str(k): v for k, v in STAGES.items()},
        "categories": CATEGORY_GROUPS,
        "mask_types": list(MASK_TYPES),
        "load_errors": dict(registry.LOAD_ERRORS),
    }
    if len(sch["ops"]) >= 30:          # never cache a half-loaded registry (see server.create_app)
        _CACHE["schema"] = sch
    return sch


def op_defaults(name: str) -> dict:
    return dict(registry.all_ops()[name].defaults)
