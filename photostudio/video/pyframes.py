"""The slow path (``--frames python``): ffmpeg decodes to raw RGB24 frames, numpy applies the *whole* stack
(including raster art modes: posterize, halftone, toon, VHS, glitch ...) and a second ffmpeg encodes them.

Documented cost (research notes, 2-core VM): a numpy float32 per-frame chain runs at ~13 fps for 1080p, about
6x slower than staying inside ffmpeg; use the LUT path whenever the look is colour-only.  Temporal stability:
the pipeline seed is fixed for the whole clip, so seeded effects (grain, paper texture, stroke placement) do
not flicker; ``reseed_grain=True`` re-seeds only grain-like ops per frame for animated film grain.
"""
from __future__ import annotations

import importlib
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .. import color
from ..pipeline.registry import Ctx
from . import lut as vlut
from .ff import Plan, Step, VideoError, base_cmd, fmt_num, resolve_argv

GRAIN_OPS = ("grain", "noise", "film_grain")


def _engine_apply():
    try:
        return importlib.import_module("photostudio.pipeline.engine").apply_stack_to_array
    except Exception:
        return None


def make_frame_fn(stack: list[dict], opts, notes: list[str]) -> Callable[[np.ndarray, int], np.ndarray]:
    """-> f(frame_rgb_float01_video_encoded, index) -> same.  Engine first (contract), own runner as fallback."""
    transfer, seed = opts.transfer, opts.seed
    eng = _engine_apply()
    state = {"engine_ok": eng is not None}

    def own(frame: np.ndarray, i: int) -> np.ndarray:
        return vlut.run_entries(frame, stack, transfer=transfer, seed=seed, frame=i, reseed=GRAIN_OPS,
                                only_color=False, notes=notes, reseed_grain=opts.reseed_grain)

    def via_engine(frame: np.ndarray, i: int) -> np.ndarray:
        lin = color.apply_matrix(vlut.decode_transfer(frame, transfer), color.SRGB_TO_REC2020)
        ctx = Ctx(kind="image", output_space="srgb")
        ctx.seed = seed + (i if opts.reseed_grain else 0)
        out = eng(lin, stack, ctx)
        if transfer != "srgb":
            out = vlut.encode_transfer(color.srgb_eotf(out), transfer)
        return np.clip(out, 0, 1).astype(np.float32)

    def fn(frame: np.ndarray, i: int) -> np.ndarray:
        if state["engine_ok"]:
            try:
                return via_engine(frame, i)
            except Exception as exc:  # unknown op in an in-progress engine, etc. -> built-in runner
                state["engine_ok"] = False
                notes.append(f"engine.apply_stack_to_array failed ({type(exc).__name__}: {exc}); using the built-in runner")
        return own(frame, i)

    return fn


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def run_pipeline(dec_argv: list[str], enc_argv: list[str], size: tuple[int, int], frame_fn: Callable,
                 expected_frames: int | None, progress: Callable | None = None) -> dict:
    w, h = size
    fsz = w * h * 3
    dec = subprocess.Popen(dec_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    enc = subprocess.Popen(enc_argv, stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    errs: dict[str, list[bytes]] = {"dec": [], "enc": []}

    def drain(p, key):
        for line in p.stderr:
            errs[key].append(line)
            del errs[key][:-200]

    ths = [threading.Thread(target=drain, args=(dec, "dec"), daemon=True),
           threading.Thread(target=drain, args=(enc, "enc"), daemon=True)]
    for t in ths:
        t.start()
    n = 0
    t0 = time.time()
    try:
        while True:
            buf = _read_exact(dec.stdout, fsz)
            if len(buf) < fsz:
                break
            arr = np.frombuffer(buf, np.uint8).reshape(h, w, 3).astype(np.float32) / 255.0
            out = frame_fn(arr, n)
            enc.stdin.write((np.clip(out, 0, 1) * 255.0 + 0.5).astype(np.uint8).tobytes())
            n += 1
            if progress and (n % 5 == 0 or n == 1):
                el = max(time.time() - t0, 1e-6)
                pct = min(100.0, 100.0 * n / expected_frames) if expected_frames else None
                progress({"frame": n, "fps": n / el, "percent": pct, "time": el,
                          "eta": ((expected_frames - n) / (n / el)) if expected_frames else None})
        enc.stdin.close()
    except BrokenPipeError as exc:
        raise VideoError("encoder closed the pipe: " + b"".join(errs["enc"]).decode(errors="replace")[-1500:]) from exc
    finally:
        for p in (dec, enc):
            try:
                if p.poll() is None and p is dec:
                    p.kill()
            except Exception:
                pass
    rc_e = enc.wait()
    dec.wait()
    for t in ths:
        t.join(timeout=3)
    if rc_e != 0:
        raise VideoError("ffmpeg encoder failed: " + b"".join(errs["enc"]).decode(errors="replace")[-2000:])
    if n == 0:
        raise VideoError("decoder produced no frames: " + b"".join(errs["dec"]).decode(errors="replace")[-1500:])
    if progress:
        progress({"frame": n, "percent": 100.0, "done": True, "time": time.time() - t0, "fps": n / max(time.time() - t0, 1e-6)})
    return {"frames": n, "seconds": round(time.time() - t0, 2)}


def add_python_step(plan: Plan, src, out, info: dict, opts, stack: list[dict], gres, spec, aargs: list,
                    hw_note: str | None = None) -> None:
    """Add the decode|numpy|encode step to ``plan``."""
    from .plan import trim_args

    w, h = gres.out_size
    fps = gres.out_fps or float(info.get("fps") or 25.0)
    dec = base_cmd(*trim_args(opts), "-i", str(src), "-an", "-vf", gres.pre_graph or "null", "-f", "rawvideo",
                   "-pix_fmt", "rgb24", "-fps_mode", "passthrough", "pipe:1", progress=False)
    a2 = [("1:a:0?" if a == "0:a:0?" else a) for a in aargs]
    enc = base_cmd("-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", fmt_num(fps, 6), "-i", "pipe:0",
                   *trim_args(opts), "-i", str(src), gres.flag, gres.graph,
                   "-map", gres.out_label or "0:v:0", *a2, "-map_metadata", "1", progress=False)
    enc += ["-r", fmt_num(fps, 6), *spec.video_args]
    if spec.movflags:
        enc += ["-movflags", spec.movflags]
    enc += ["-shortest", str(out)] if info.get("has_audio") else [str(out)]
    expected = int(round((gres.out_duration or 0) * fps)) or None
    notes = plan.notes

    def fn(plan_: Plan, progress=None):
        frame_fn = make_frame_fn(stack, opts, notes)
        res = run_pipeline(resolve_argv(dec), resolve_argv(enc), (w, h), frame_fn, expected, progress)
        plan_.state["python_frames"] = res

    notes.append("SLOW PATH: raw frames are piped through numpy (about 13 fps at 1080p on 2 cores; ~6x slower than "
                 "the ffmpeg LUT path). Use --max-width to process smaller frames.")
    plan.add(Step("pipe", f"python frames: {Path(src).name} -> numpy -> {Path(out).name}", argv=dec, argv2=enc, fn=fn,
                  duration=gres.out_duration))
