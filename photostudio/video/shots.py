"""Shots: scene-cut detection, per-shot auto exposure / white-balance matching and the ``<video>.ps.json`` sidecar.

Rules (from the research notes): auto parameters are computed once per shot on a few sampled frames (first,
middle, last; median) and never per frame (flicker).  Long shots can be split into short segments whose
parameters are smoothed over time (EMA + maximum step) -- optional.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .. import color
from ..util import lazy_import
from . import lut as vlut
from .ff import VideoError, ffmpeg_path, fmt_num

SCHEMA = 1


# ----------------------------------------------------------------------------- sidecar
def shots_path(video: str | Path) -> Path:
    p = Path(video)
    return p.with_name(p.name + ".ps.json")


def new_doc(video: str | Path, info: dict | None = None, shots: list[dict] | None = None, mode: str | None = None,
            art: str | None = None, stack: list | None = None) -> dict:
    t = time.strftime("%Y-%m-%dT%H:%M:%S")
    return {"schema": SCHEMA, "kind": "video", "source": Path(video).name,
            "video": {k: (info or {}).get(k) for k in ("duration", "fps", "width", "height", "codec", "is_hdr")},
            "default": {"mode": mode, "art": art, "stack": list(stack or [])},
            "shots": list(shots or []), "created": t, "modified": t}


def load_shots(video: str | Path) -> dict | None:
    p = shots_path(video)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VideoError(f"{p}: invalid JSON: {exc}") from exc
    if not isinstance(doc.get("shots"), list):
        raise VideoError(f"{p}: missing 'shots' list")
    return doc


def save_shots(video: str | Path, doc: dict) -> Path:
    p = shots_path(video)
    doc["modified"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def make_shot(i: int, start: float, end: float, fps: float = 0.0) -> dict:
    """``i`` is the 0-based position; the stored ``index`` is the 1-based shot number that the CLI lists and accepts
    (``photo video shots --assign 1:art=...`` is the first shot)."""
    d = {"index": i + 1, "start": round(float(start), 6), "end": round(float(end), 6), "mode": None, "art": None, "stack": []}
    if fps:
        d["frames"] = int(round((end - start) * fps))
    return d


# ----------------------------------------------------------------------------- detection
def cuts_to_shots(cuts: list[float], duration: float, min_len: float = 0.5, fps: float = 0.0) -> list[dict]:
    """Cut times (first frame of each new shot) -> contiguous shot list; cuts closer than min_len are merged."""
    pts = sorted(c for c in cuts if 0.0 < c < duration)
    keep: list[float] = []
    last = 0.0
    for c in pts:
        if c - last >= min_len and duration - c >= min(min_len, duration / 2):
            keep.append(c)
            last = c
    bounds = [0.0, *keep, duration]
    return [make_shot(i, bounds[i], bounds[i + 1], fps) for i in range(len(bounds) - 1)]


def detect_cuts_ffmpeg(path: str | Path, threshold: float = 0.3, width: int = 320, start: float | None = None,
                       duration: float | None = None) -> list[float]:
    """Fallback detector: ffmpeg ``select='gt(scene,T)'`` + ``showinfo`` (pts_time of every selected frame)."""
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "info"]
    if start:
        cmd += ["-ss", fmt_num(start)]
    if duration:
        cmd += ["-t", fmt_num(duration)]
    cmd += ["-i", str(path), "-an", "-vf", f"scale={width}:-2:flags=fast_bilinear,select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoError(f"ffmpeg scene detection failed: {r.stderr[-500:]}")
    return [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", r.stderr)]


def detect_cuts_scenedetect(path: str | Path, threshold: float = 27.0, min_len_frames: int = 15) -> list[float]:
    sd = lazy_import("scenedetect", "scene detection (PySceneDetect)")
    det = sd.ContentDetector(threshold=threshold, min_scene_len=min_len_frames)
    scenes = sd.detect(str(path), det, start_in_scene=True)
    cuts = []
    for a, _b in scenes[1:]:
        cuts.append(float(a.seconds if hasattr(a, "seconds") else a.get_seconds()))
    return cuts


def detect_shots(path: str | Path, info: dict, method: str = "auto", threshold: float | None = None,
                 min_len: float = 0.5, start: float | None = None, duration: float | None = None) -> tuple[list[dict], str]:
    """-> (shots, method_used).  auto = PySceneDetect when installed, else ffmpeg scene scores."""
    dur = info.get("duration") or 0.0
    fps = info.get("fps") or 25.0
    if method in ("auto", "pyscenedetect", "scenedetect"):
        try:
            cuts = detect_cuts_scenedetect(path, threshold or 27.0, max(1, int(round(min_len * fps))))
            used = "pyscenedetect"
        except Exception as exc:
            if method != "auto":
                raise
            cuts = detect_cuts_ffmpeg(path, (threshold / 90.0) if threshold else 0.3, start=start, duration=duration)
            used = f"ffmpeg-scene (PySceneDetect unavailable: {type(exc).__name__})"
    elif method in ("ffmpeg", "ffmpeg-scene"):
        cuts = detect_cuts_ffmpeg(path, threshold if threshold is not None else 0.3, start=start, duration=duration)
        used = "ffmpeg-scene"
    else:
        raise VideoError(f"unknown shot detection method '{method}' (auto|pyscenedetect|ffmpeg)")
    return cuts_to_shots(cuts, dur, min_len, fps), used


def fill_shots(shots: list[dict], duration: float) -> list[dict]:
    """Make shots contiguous over [0, duration] (user-edited sidecars may have gaps/overlaps)."""
    out = sorted((dict(s) for s in shots), key=lambda s: float(s["start"]))
    if not out:
        return [make_shot(0, 0.0, duration)]
    out[0]["start"] = 0.0
    for a, b in zip(out, out[1:]):
        a["end"] = b["start"]
    out[-1]["end"] = max(float(out[-1]["end"]), duration) if duration else float(out[-1]["end"])
    for i, s in enumerate(out):
        s["index"] = i + 1
        s["start"], s["end"] = float(s["start"]), float(s["end"])
    return out


def clip_shots(shots: list[dict], start: float | None, duration: float | None) -> list[dict]:
    """Restrict to the trimmed window [start, start+duration] (times stay on the input timeline)."""
    if not start and not duration:
        return shots
    a = float(start or 0.0)
    b = a + float(duration) if duration else float("inf")
    out = []
    for s in shots:
        if s["end"] <= a or s["start"] >= b:
            continue
        d = dict(s)
        d["start"], d["end"] = max(s["start"], a), min(s["end"], b)
        out.append(d)
    return out


def expand_segments(shots: list[dict], seg_seconds: float) -> list[dict]:
    """Split long shots into ~seg_seconds sub-segments (``parent`` = original index) for smoothed auto-match."""
    out: list[dict] = []
    for s in shots:
        L = s["end"] - s["start"]
        n = max(1, int(round(L / seg_seconds))) if L > 1.5 * seg_seconds else 1
        for k in range(n):
            d = dict(s)
            d["start"] = s["start"] + L * k / n
            d["end"] = s["start"] + L * (k + 1) / n
            d["parent"] = s.get("index", len(out) + 1)
            out.append(d)
    for i, d in enumerate(out):
        d["index"] = i + 1
    return out


# ----------------------------------------------------------------------------- frame sampling
def grab_frames(path: str | Path, info: dict, times: list[float], width: int = 480,
                norm_filters: list[str] | None = None) -> list[np.ndarray]:
    """Decode one frame at each time as float32 RGB (0..1, video-encoded), downscaled to ``width`` px wide."""
    dw, dh = int(info.get("display_width") or info["width"]), int(info.get("display_height") or info["height"])
    w = min(width, dw)
    h = max(2, int(round(w * dh / dw / 2.0)) * 2)
    flt = list(norm_filters or [])
    flt += [f"scale={w}:{h}:flags=area:in_range=tv:in_color_matrix=bt709", "format=rgb24"]
    frames = []
    for t in times:
        cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-ss", fmt_num(max(0.0, t), 4), "-i", str(path),
               "-an", "-frames:v", "1", "-vf", ",".join(flt), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 or len(r.stdout) < w * h * 3:
            continue
        frames.append(np.frombuffer(r.stdout[: w * h * 3], np.uint8).reshape(h, w, 3).astype(np.float32) / 255.0)
    return frames


def sample_times(shot: dict, fps: float, n: int = 3) -> list[float]:
    a, b = float(shot["start"]), float(shot["end"])
    eps = 1.5 / max(fps, 1.0)
    if b - a <= 2 * eps or n <= 1:
        return [min(max(a, (a + b) / 2), max(a, b - eps))]
    return [a + eps + (b - a - 2 * eps) * k / (n - 1) for k in range(n)]


# ----------------------------------------------------------------------------- statistics & matching
def frame_stats(frames: list[np.ndarray], transfer: str = "srgb") -> dict | None:
    """Linear-light shot statistics (median of per-frame values): median luminance, mid-tone mean RGB, p5/p95."""
    if not frames:
        return None
    meds, rgbs, p5, p95 = [], [], [], []
    for f in frames:
        lin = vlut.decode_transfer(f, transfer).reshape(-1, 3)
        y = lin @ color.Y_SRGB
        meds.append(float(np.median(y)))
        p5.append(float(np.percentile(y, 5)))
        p95.append(float(np.percentile(y, 95)))
        sel = (y > 0.02) & (y < 0.85)
        rgbs.append(lin[sel].mean(0) if sel.sum() > 50 else lin.mean(0))
    return {"median_y": float(np.median(meds)), "p5": float(np.median(p5)), "p95": float(np.median(p95)),
            "mid_rgb": np.median(np.asarray(rgbs), axis=0).tolist(), "n_frames": len(frames)}


def solve_matches(stats: list[dict | None], *, mode: str = "both", reference: str = "median", max_ev: float = 1.0,
                  strength: float = 1.0, max_gain: float = 0.25, target_median: float = 0.18) -> list[dict]:
    """Per-shot {ev, gain[3]} that moves each shot toward the reference exposure / colour balance.

    reference: median (median of all shots) | first | index:N | absolute (mid-grey 18% + neutral gray-world).
    Limits keep it gentle: |EV| <= max_ev, each channel gain within +-max_gain (8-bit footage has no latitude).
    """
    valid = [s for s in stats if s]
    ident = {"ev": 0.0, "gain": [1.0, 1.0, 1.0]}
    if not valid:
        return [dict(ident) for _ in stats]
    if reference == "absolute" or (len(valid) == 1 and reference == "median"):
        ref_y = target_median
        ref_rgb = None
        ref_neutral = True
    elif reference == "first":
        ref = valid[0]
        ref_y, ref_rgb, ref_neutral = ref["median_y"], np.asarray(ref["mid_rgb"]), False
    elif reference.startswith("index:"):
        n = int(reference.split(":")[1])                       # shot number as listed: 1 is the first shot
        if not 1 <= n <= len(stats):
            raise VideoError(f"reference index:{n} is out of range (shots are numbered 1..{len(stats)})")
        ref = stats[n - 1]
        if not ref:
            raise VideoError("reference shot has no usable frames")
        ref_y, ref_rgb, ref_neutral = ref["median_y"], np.asarray(ref["mid_rgb"]), False
    else:
        ref_y = float(np.median([s["median_y"] for s in valid]))
        rgbs = np.asarray([np.asarray(s["mid_rgb"]) / max(float(np.asarray(s["mid_rgb"]) @ color.Y_SRGB), 1e-6) for s in valid])
        ref_rgb, ref_neutral = np.median(rgbs, axis=0), False
    out = []
    for s in stats:
        if not s or s["median_y"] < 0.004:
            out.append(dict(ident))
            continue
        ev = 0.0
        if mode in ("both", "exposure"):
            ev = float(np.clip(np.log2(max(ref_y, 1e-4) / max(s["median_y"], 1e-4)) * strength, -max_ev, max_ev))
        gain = np.ones(3)
        if mode in ("both", "wb"):
            rgb = np.asarray(s["mid_rgb"], dtype=np.float64)
            ratio = rgb / max(float(rgb @ color.Y_SRGB), 1e-6)       # chroma at unit luminance
            if ref_neutral:
                want = np.ones(3)
            else:
                want = np.asarray(ref_rgb, dtype=np.float64)
            g = want / np.maximum(ratio, 1e-6)
            g = np.power(g, strength)
            g = np.clip(g, 1.0 - max_gain, 1.0 + max_gain)
            g = g / max(float(g @ color.Y_SRGB), 1e-6)               # pure colour change, exposure handled by ev
            gain = np.clip(g, 1.0 - 1.2 * max_gain, 1.0 + 1.2 * max_gain)
        out.append({"ev": round(ev, 4), "gain": [round(float(x), 4) for x in gain]})
    return out


def smooth_matches(matches: list[dict], parents: list[int], alpha: float = 0.5, max_step_ev: float = 0.15,
                   max_step_gain: float = 0.03) -> list[dict]:
    """EMA over consecutive sub-segments of the same shot with a clamped step (no visible jumps)."""
    out = [dict(m, gain=list(m["gain"])) for m in matches]
    for i in range(1, len(out)):
        if parents[i] != parents[i - 1]:
            continue
        prev, cur = out[i - 1], out[i]
        tgt_ev = alpha * cur["ev"] + (1 - alpha) * prev["ev"]
        cur["ev"] = round(float(prev["ev"] + np.clip(tgt_ev - prev["ev"], -max_step_ev, max_step_ev)), 4)
        gs = []
        for c in range(3):
            tgt = alpha * cur["gain"][c] + (1 - alpha) * prev["gain"][c]
            gs.append(round(float(prev["gain"][c] + np.clip(tgt - prev["gain"][c], -max_step_gain, max_step_gain)), 4))
        cur["gain"] = gs
    return out


def match_entry(m: dict) -> dict | None:
    if abs(m["ev"]) < 1e-3 and all(abs(g - 1) < 1e-3 for g in m["gain"]):
        return None
    return {"op": "video_match", "params": {"ev": m["ev"], "gain": m["gain"]}, "enabled": True, "mask": None,
            "blend": "normal", "opacity": 1.0, "v": 1}


# ----------------------------------------------------------------------------- python step factories
def make_auto_step_fn(path: str | Path, info: dict, shots: list[dict], opts, norm_filters: list[str]) -> Callable:
    """Returns fn(plan, progress) that samples frames per shot, solves the matches, stores them in plan.state."""

    def fn(plan, progress=None):
        stats = []
        for i, s in enumerate(shots):
            frames = grab_frames(path, info, sample_times(s, info.get("fps") or 25.0), norm_filters=norm_filters)
            stats.append(frame_stats(frames, opts.transfer))
            if progress:
                progress({"percent": 100.0 * (i + 1) / len(shots), "time": s["end"]})
        m = solve_matches(stats, mode=opts.auto, reference=opts.auto_reference, max_ev=opts.auto_max_ev,
                          strength=opts.auto_strength)
        if opts.smooth:
            m = smooth_matches(m, [s.get("parent", s["index"]) for s in shots])
        plan.state["auto"] = m
        plan.state["auto_stats"] = stats

    return fn
