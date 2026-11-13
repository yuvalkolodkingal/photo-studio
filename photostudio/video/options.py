"""Option dataclasses shared by the plan builders and the CLI."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from typing import Any

from .ff import VideoError


@dataclass
class GradeOptions:
    # ---- colour -------------------------------------------------------------------------------
    mode: str | None = None
    art: str | None = None
    stack: list = field(default_factory=list)        # user edits (merged on top of mode/art)
    lut: str | None = None                           # external .cube applied after the baked look
    intensity: float = 1.0                           # LUT strength (0 = identity)
    lut_size: int = 33
    lut_interp: str = "tetrahedral"
    lut_bits: int = 10                               # working bit depth of the RGB segment (8/10/12/16)
    transfer: str = "srgb"                           # srgb | bt709 | gamma24 (see lut.py)
    # ---- per-shot -----------------------------------------------------------------------------
    per_shot: bool = False
    shots: list | None = None                        # [{start, end, mode, art, stack}] (seconds, input timeline)
    auto: str = "off"                                # off | exposure | wb | both (per-shot match)
    auto_reference: str = "median"                   # median | first | index:N | absolute
    auto_max_ev: float = 1.0
    auto_strength: float = 1.0
    shot_apply: str = "auto"                         # auto | timeline | concat
    smooth: bool = False
    segment_seconds: float = 4.0
    # ---- spatial ------------------------------------------------------------------------------
    denoise: Any = None                              # level name | 0..1 | "a:b:c:d" | dict
    sharpen: Any = None                              # level name | 0..1 | dict(method, amount)
    grain: float | None = None                       # 0..1
    vignette: float | None = None                    # 0..1
    deband: bool = False
    deflicker: int = 0                               # window (frames), 0 = off
    stabilize: dict | None = None                    # {shakiness, smoothing, ...}
    lens: dict | None = None                         # {k1, k2} or {make, model, lens}
    # ---- timing -------------------------------------------------------------------------------
    start: float | None = None
    duration: float | None = None
    speed: float = 1.0                               # >1 faster, <1 slower (frames dropped/duplicated per fps)
    slowmo: float | None = None                      # HFR conform factor: setpts=f*PTS keeping every frame
    fps: float | None = None
    fps_method: str = "fps"                          # fps | blend | mci
    blend_frames: int = 0                            # tmix frames for speed-ups
    # ---- geometry -----------------------------------------------------------------------------
    scale: str | None = None                         # "1920x1080" | "1080p" | "h1080" | "w1280" | "50%"
    reframe: dict | None = None                      # {aspect|width,height, mode: blur|crop|pad, x}
    letterbox: float | None = None                   # 2.39
    letterbox_mode: str = "matte"                    # matte | crop
    # ---- fx -----------------------------------------------------------------------------------
    fade_in: float = 0.0
    fade_out: float = 0.0
    text: dict | None = None
    # ---- HDR / audio --------------------------------------------------------------------------
    hdr: str = "auto"                                # auto | tonemap | passthrough
    tonemap: str = "mobius"
    audio: str | None = None                         # None = preset default | copy | aac | loudnorm | none
    loudnorm: dict | None = None
    # ---- engine -------------------------------------------------------------------------------
    frames: str = "ffmpeg"                           # ffmpeg | python
    seed: int = 0
    reseed_grain: bool = False
    max_width: int | None = None                     # python path: downscale before numpy

    @classmethod
    def from_dict(cls, d: dict) -> "GradeOptions":
        names = {f.name for f in fields(cls)}
        bad = set(d) - names
        if bad:
            raise VideoError(f"unknown option(s): {', '.join(sorted(bad))}")
        return cls(**d)


_SIZE_RE = re.compile(r"^(\d+)x(\d+)$")
_NAMED = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440), "2160p": (3840, 2160),
          "4k": (3840, 2160), "uhd": (3840, 2160), "fhd": (1920, 1080), "hd": (1280, 720)}


def parse_size(spec: str | None, src_w: int, src_h: int) -> tuple[int, int] | None:
    """'1920x1080' | '1080p' | 'h1080' | 'w1280' | '50%' -> even (w, h), preserving aspect when one side is given."""
    if not spec:
        return None
    s = str(spec).strip().lower()
    even = lambda v: max(2, int(round(v / 2.0)) * 2)  # noqa: E731
    if (m := _SIZE_RE.match(s)):
        return even(int(m.group(1))), even(int(m.group(2)))
    if s in _NAMED:
        return _NAMED[s]
    if s.endswith("%"):
        f = float(s[:-1]) / 100.0
        return even(src_w * f), even(src_h * f)
    if s.startswith("h") and s[1:].isdigit():
        h = int(s[1:])
        return even(src_w * h / src_h), even(h)
    if s.startswith("w") and s[1:].isdigit():
        w = int(s[1:])
        return even(w), even(src_h * w / src_w)
    raise VideoError(f"cannot parse size '{spec}' (use 1920x1080, 1080p, h1080, w1280 or 50%)")


def parse_aspect(spec: str | float | None) -> float | None:
    if spec in (None, ""):
        return None
    if isinstance(spec, (int, float)):
        return float(spec)
    s = str(spec)
    if ":" in s:
        a, b = s.split(":", 1)
        return float(a) / float(b)
    if "/" in s:
        a, b = s.split("/", 1)
        return float(a) / float(b)
    return float(s)
