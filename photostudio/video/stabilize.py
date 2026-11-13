"""Stabilisation: two-pass vid.stab (detect -> transform + unsharp) with a single-pass ``deshake`` fallback."""
from __future__ import annotations

from .ff import Caps, esc_filter_value, fmt_num

DEFAULTS = {"shakiness": 6, "accuracy": 15, "stepsize": 6, "mincontrast": 0.25, "smoothing": 20, "optzoom": 2,
            "zoomspeed": 0.25, "crop": "keep", "interpol": "bicubic", "tripod": False, "maxshift": -1,
            "maxangle": -1, "sharpen": True}


def params(spec: dict | bool | None) -> dict:
    p = dict(DEFAULTS)
    if isinstance(spec, dict):
        p.update({k: v for k, v in spec.items() if v is not None})
    return p


def detect_filter(trf: str, p: dict) -> str:
    s = (f"vidstabdetect=shakiness={int(p['shakiness'])}:accuracy={int(p['accuracy'])}:stepsize={int(p['stepsize'])}"
         f":mincontrast={fmt_num(float(p['mincontrast']))}:result={esc_filter_value(trf)}")
    if p.get("tripod"):
        s += ":tripod=1"
    return s


def transform_filters(trf: str, p: dict) -> list[str]:
    t = (f"vidstabtransform=input={esc_filter_value(trf)}:smoothing={int(p['smoothing'])}:optzoom={int(p['optzoom'])}"
         f":zoomspeed={fmt_num(float(p['zoomspeed']))}:crop={p['crop']}:interpol={p['interpol']}")
    if p.get("maxshift", -1) != -1:
        t += f":maxshift={int(p['maxshift'])}"
    if p.get("maxangle", -1) != -1:
        t += f":maxangle={fmt_num(float(p['maxangle']))}"
    if p.get("tripod"):
        t += ":tripod=1"
    out = ["format=yuv420p", t]
    if p.get("sharpen", True):
        out.append("unsharp=5:5:0.8:3:3:0.4")        # vid.stab's recommended post-sharpen
    return out


def deshake_filters(p: dict) -> list[str]:
    r = int(min(64, max(8, p["shakiness"] * 6)))
    return ["format=yuv420p", f"deshake=rx={r}:ry={r}:edge=mirror"]


def available(caps: Caps) -> bool:
    return caps.has_filter("vidstabdetect") and caps.has_filter("vidstabtransform")
