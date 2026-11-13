"""Video mode: probe, per-shot grading through baked 3D LUTs, stabilisation, denoise, export presets, frame tools.

Design rules (see the research notes): stay inside ffmpeg whenever possible (bake colour ops into a .cube and apply
it with ``lut3d``); compute auto parameters once per shot; the numpy frame path (``--frames python``) is the slow
exception for raster art modes.  All builders return a :class:`Plan` whose commands are argv lists.

Environment: ``PHOTOSTUDIO_FFMPEG`` / ``PHOTOSTUDIO_FFPROBE`` select the binaries (else PATH, else imageio-ffmpeg).
"""
from __future__ import annotations

from pathlib import Path

from . import encode, frames, graph, lut, options, plan, probe as _probe_mod, shots, stabilize, stack
from .ff import (Caps, LazyArg, Plan, Step, VideoError, capabilities, execute, ffmpeg_path, ffprobe_path, find_ffmpeg,
                 find_ffprobe, run_plan)
from .lut import LUT3D, bake_lut, read_cube, write_cube
from .options import GradeOptions
from .plan import build_plan, default_output
from .probe import normalise_probe, probe

__all__ = ["Caps", "GradeOptions", "LUT3D", "LazyArg", "Plan", "Step", "VideoError", "bake_lut", "build_plan",
           "capabilities", "default_output", "encode", "execute", "ffmpeg_path", "ffprobe_path", "find_ffmpeg",
           "find_ffprobe", "frames", "graph", "load_video_preset", "list_video_presets", "lut", "normalise_probe", "options",
           "plan", "probe", "read_cube", "run_plan", "shots", "stabilize", "stack", "write_cube"]


def load_video_preset(name: str | None) -> dict | None:
    """Load a ``presets/video/<name>.yaml`` preset (``name`` or ``name:variant``); None for no preset."""
    if not name:
        return None
    from .. import presets

    return presets.load_preset("video", name)


def list_video_presets() -> list[dict]:
    from .. import presets

    return presets.list_presets("video")
