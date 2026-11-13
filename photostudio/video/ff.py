"""ffmpeg / ffprobe plumbing: binary resolution, capability detection, command plans, progress parsing.

Everything that builds a command returns a :class:`Plan` (an ordered list of :class:`Step` objects).  A plan
can be printed (``--dry-run``: exact argv lists, no shell strings) or executed with :func:`execute`, which
creates the plan's temp work directory, runs the steps (parsing ``-progress`` output of ffmpeg) and always
removes the temp files afterwards.
"""
from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..util import MissingDependency, lazy_import

ENV_FFMPEG = "PHOTOSTUDIO_FFMPEG"
ENV_FFPROBE = "PHOTOSTUDIO_FFPROBE"

INSTALL_HINT = (
    "Install ffmpeg (Fedora: enable RPM Fusion, then `sudo dnf swap ffmpeg-free ffmpeg --allowerasing` for "
    "x264/x265/vidstab), or `pip install imageio-ffmpeg`, or point PHOTOSTUDIO_FFMPEG at a binary."
)


class VideoError(RuntimeError):
    """A video operation failed (bad input, ffmpeg returned an error, unsupported request)."""


# --------------------------------------------------------------------------- binaries
def _which_exe(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path).expanduser()
    if p.is_file() and os.access(p, os.X_OK):
        return str(p)
    return shutil.which(str(path))


def find_ffmpeg() -> str | None:
    """Resolve the ffmpeg binary: $PHOTOSTUDIO_FFMPEG, then PATH, then the imageio-ffmpeg bundled binary."""
    env = _which_exe(os.environ.get(ENV_FFMPEG))
    if env:
        return env
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return str(exe)
    except Exception:
        pass
    return None


def find_ffprobe() -> str | None:
    """Resolve ffprobe: $PHOTOSTUDIO_FFPROBE, a sibling of the resolved ffmpeg, then PATH."""
    env = _which_exe(os.environ.get(ENV_FFPROBE))
    if env:
        return env
    ff = find_ffmpeg()
    if ff:
        sib = Path(ff).with_name("ffprobe")
        if sib.exists() and os.access(sib, os.X_OK):
            return str(sib)
    return shutil.which("ffprobe")


def ffmpeg_path() -> str:
    p = find_ffmpeg()
    if not p:
        raise MissingDependency(f"ffmpeg not found (needed for video). {INSTALL_HINT}")
    return p


def ffprobe_path() -> str:
    p = find_ffprobe()
    if not p:
        raise MissingDependency(
            f"ffprobe not found (needed for probing video). {INSTALL_HINT} "
            f"(imageio-ffmpeg ships no ffprobe; set {ENV_FFPROBE} or install the ffmpeg package.)")
    return p


def ffmpeg_bin_for_plan() -> str:
    """Binary name used when *building* commands (dry-run works even without ffmpeg installed)."""
    return find_ffmpeg() or "ffmpeg"


# --------------------------------------------------------------------------- capabilities
@dataclass
class Caps:
    version: str = "unknown"
    filters: frozenset = frozenset()
    encoders: frozenset = frozenset()
    assumed: bool = False                 # True when no binary was available and everything is assumed present

    def has_filter(self, name: str) -> bool:
        return self.assumed or name in self.filters

    def has_encoder(self, name: str) -> bool:
        return self.assumed or name in self.encoders

    def describe(self) -> dict:
        keys = ("vidstabdetect", "vidstabtransform", "zscale", "lut3d", "tonemap", "hqdn3d", "nlmeans", "cas",
                "atadenoise", "deflicker", "lensfun", "lagfun", "libplacebo", "drawtext", "loudnorm", "minterpolate")
        enc = ("libx264", "libx265", "h264_nvenc", "hevc_nvenc", "prores_ks", "dnxhd", "libwebp_anim", "gif",
               "libsvtav1", "libvpx-vp9")
        return {"version": self.version, "assumed": self.assumed,
                "filters": {k: self.has_filter(k) for k in keys},
                "encoders": {k: self.has_encoder(k) for k in enc}}


_FILTER_LINE = re.compile(r"^\s*[T.][S.][C.]\s+(\S+)\s+\S+\s")
_ENC_LINE = re.compile(r"^\s*[VAS][F.][S.][X.][B.][D.]\s+(\S+)\s")


@functools.lru_cache(maxsize=4)
def _caps_for(binary: str) -> Caps:
    def out(*args):
        return subprocess.run([binary, "-hide_banner", *args], capture_output=True, text=True, timeout=60).stdout

    ver = out("-version").splitlines()[:1]
    filters = frozenset(m.group(1) for line in out("-filters").splitlines() if (m := _FILTER_LINE.match(line)))
    encoders = frozenset(m.group(1) for line in out("-encoders").splitlines() if (m := _ENC_LINE.match(line)))
    return Caps(ver[0] if ver else "unknown", filters, encoders)


def capabilities(refresh: bool = False) -> Caps:
    """Filters/encoders of the resolved ffmpeg build.  Without a binary every feature is *assumed* present so
    that command construction (and ``--dry-run``) still works; execution then fails with a clear error."""
    b = find_ffmpeg()
    if not b:
        return Caps(assumed=True)
    if refresh:
        _caps_for.cache_clear()
        _nvenc_cache.clear()
    return _caps_for(b)


_nvenc_cache: dict[str, bool] = {}


def encoder_usable(encoder: str) -> bool:
    """True when a (hardware) encoder both exists in the build and can encode a tiny test clip right now."""
    b = find_ffmpeg()
    if not b or not capabilities().has_encoder(encoder):
        return False
    key = f"{b}:{encoder}"
    if key not in _nvenc_cache:
        try:
            r = subprocess.run([b, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                                "color=c=black:s=640x360:r=25:d=0.2", "-frames:v", "2", "-c:v", encoder,
                                "-f", "null", "-"], capture_output=True, text=True, timeout=30)
            _nvenc_cache[key] = r.returncode == 0
        except Exception:
            _nvenc_cache[key] = False
    return _nvenc_cache[key]


# --------------------------------------------------------------------------- escaping helpers
def esc_filter_value(s: str) -> str:
    """Escape a value inside a filtergraph option (file names, text)."""
    s = str(s).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return s.replace(",", "\\,").replace(";", "\\;").replace("[", "\\[").replace("]", "\\]")


def esc_drawtext(s: str) -> str:
    s = str(s).replace("\\", "\\\\").replace("'", "’").replace(":", "\\:").replace("%", "\\%")
    return s.replace(",", "\\,")


def fmt_num(x: float, nd: int = 6) -> str:
    s = f"{x:.{nd}f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


# --------------------------------------------------------------------------- plans
class LazyArg:
    """An argv element resolved when the step runs (e.g. loudnorm measurements)."""

    def __init__(self, fn: Callable[[], str], placeholder: str):
        self.fn, self.placeholder = fn, placeholder

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.placeholder


def resolve_argv(argv: Sequence[Any]) -> list[str]:
    return [str(a.fn()) if isinstance(a, LazyArg) else str(a) for a in argv]


def preview_argv(argv: Sequence[Any]) -> list[str]:
    return [a.placeholder if isinstance(a, LazyArg) else str(a) for a in argv]


@dataclass
class Step:
    kind: str                                # ffmpeg | python | pipe
    label: str
    argv: list | None = None
    argv2: list | None = None                # pipe: the encoder command (argv is the decoder)
    fn: Callable[["Plan", Callable | None], Any] | None = None
    duration: float | None = None            # expected duration of the output (seconds) for progress %
    capture: str | None = None               # store ffmpeg stderr in plan.state[capture]
    description: str = ""

    def to_json(self) -> dict:
        d: dict[str, Any] = {"kind": self.kind, "label": self.label}
        if self.argv:
            d["cmd"] = shlex.join(preview_argv(self.argv))
        if self.argv2:
            d["cmd2"] = shlex.join(preview_argv(self.argv2))
        if self.description:
            d["description"] = self.description
        return d


@dataclass
class Plan:
    name: str
    steps: list[Step] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    workdir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / f"photostudio-video-{uuid.uuid4().hex[:10]}")

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step

    def commands(self) -> list[list[str]]:
        out: list[list[str]] = []
        for s in self.steps:
            if s.argv:
                out.append(preview_argv(s.argv))
            if s.argv2:
                out.append(preview_argv(s.argv2))
        return out

    def command_lines(self) -> list[str]:
        return [shlex.join(c) for c in self.commands()]

    def to_json(self, dry_run: bool = True) -> dict:
        d = {"dry_run": dry_run, "name": self.name, "steps": [s.to_json() for s in self.steps],
             "commands": self.commands(), "command_lines": self.command_lines(), "outputs": list(self.outputs),
             "workdir": str(self.workdir)}
        if self.notes:
            d["notes"] = list(self.notes)
        if self.warnings:
            d["warnings"] = list(self.warnings)
        if self.info:
            d["info"] = self.info
        return d


def base_cmd(*args: Any, overwrite: bool = True, progress: bool = True, loglevel: str = "error") -> list:
    """`ffmpeg -hide_banner -nostdin -y -v error [-progress pipe:1 -nostats] ...` as an argv list."""
    cmd: list = [ffmpeg_bin_for_plan(), "-hide_banner", "-nostdin", "-y" if overwrite else "-n", "-v", loglevel]
    if progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [a for a in args]
    return cmd


# --------------------------------------------------------------------------- running
ProgressCb = Callable[[dict], None]


def _parse_progress_block(block: dict, duration: float | None) -> dict:
    t_us = None
    for k in ("out_time_us", "out_time_ms"):
        if block.get(k, "").lstrip("-").isdigit():
            t_us = int(block[k])
            break
    t = (t_us or 0) / 1e6
    ev = {"frame": int(block["frame"]) if block.get("frame", "").isdigit() else None,
          "fps": float(block["fps"]) if _isfloat(block.get("fps")) else None,
          "time": t, "speed": block.get("speed"), "done": block.get("progress") == "end"}
    ev["percent"] = (min(100.0, 100.0 * t / duration) if duration and duration > 0 else None)
    if ev["done"]:
        ev["percent"] = 100.0
    return ev


def _isfloat(x: Any) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def run_ffmpeg(argv: Sequence[Any], duration: float | None = None, progress_cb: ProgressCb | None = None,
               capture_stderr: bool = False) -> str:
    """Run one ffmpeg command, parsing `-progress pipe:1` key=value blocks.  Returns the stderr text (the last
    ~64 KiB).  Raises VideoError with that tail on a non-zero exit."""
    args = resolve_argv(argv)
    if not Path(args[0]).exists() and not shutil.which(args[0]):
        ffmpeg_path()  # raises MissingDependency with the install hint
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, bufsize=1)
    tail: list[str] = []

    def _drain():
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line)
            if len(tail) > 2000:
                del tail[:1000]

    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    block: dict[str, str] = {}
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        block[k] = v
        if k == "progress":
            if progress_cb:
                progress_cb(_parse_progress_block(block, duration))
            block = {}
    rc = proc.wait()
    th.join(timeout=5)
    err = "".join(tail)[-65536:]
    if rc != 0:
        raise VideoError(f"ffmpeg failed (exit {rc}): {shlex.join(args[:12])} ...\n{err[-4000:]}")
    return err


class ConsoleProgress:
    """Single-line progress printer on stderr (only used from the CLI)."""

    def __init__(self, label: str = "", stream=None):
        import sys

        self.stream = stream or sys.stderr
        self.label = label
        self.t0 = time.time()
        self._last = 0.0

    def __call__(self, ev: dict) -> None:
        now = time.time()
        if now - self._last < 0.25 and not ev.get("done"):
            return
        self._last = now
        pct = ev.get("percent")
        txt = f"\r[{self.label}] " + (f"{pct:5.1f}%" if pct is not None else f"t={ev.get('time', 0):.1f}s")
        if ev.get("fps"):
            txt += f"  {ev['fps']:.1f} fps"
        if ev.get("speed"):
            txt += f"  {ev['speed']}"
        if ev.get("eta") is not None:
            txt += f"  eta {ev['eta']:.0f}s"
        self.stream.write(txt + ("\n" if ev.get("done") else ""))
        self.stream.flush()


def execute(plan: Plan, progress: ProgressCb | None = None, keep_temp: bool = False) -> dict:
    """Run a plan.  The temp work directory is created first and removed at the end (even on error)."""
    t0 = time.time()
    plan.workdir.mkdir(parents=True, exist_ok=True)
    timings = []
    existing = {o: Path(o).exists() for o in plan.outputs}
    try:
        for step in plan.steps:
            ts = time.time()
            if step.kind == "ffmpeg":
                err = run_ffmpeg(step.argv or [], step.duration, _labelled(progress, step.label))
                if step.capture:
                    plan.state[step.capture] = err
            elif step.kind == "python":
                assert step.fn is not None
                step.fn(plan, _labelled(progress, step.label))
            elif step.kind == "pipe":
                assert step.fn is not None
                step.fn(plan, _labelled(progress, step.label))
            else:  # pragma: no cover
                raise VideoError(f"unknown step kind {step.kind}")
            timings.append({"label": step.label, "seconds": round(time.time() - ts, 3)})
    except BaseException:
        for o, was in existing.items():           # never leave a truncated output behind
            if not was and Path(o).exists():
                try:
                    Path(o).unlink()
                except OSError:
                    pass
        raise
    finally:
        if not keep_temp:
            shutil.rmtree(plan.workdir, ignore_errors=True)
    res = {"ok": True, "dry_run": False, "outputs": list(plan.outputs), "seconds": round(time.time() - t0, 3),
           "steps": timings}
    if plan.notes:
        res["notes"] = list(plan.notes)
    if plan.warnings:
        res["warnings"] = list(plan.warnings)
    return res


def _labelled(cb: ProgressCb | None, label: str) -> ProgressCb | None:
    if cb is None:
        return None

    def inner(ev: dict) -> None:
        ev.setdefault("label", label)
        cb(ev)
    return inner


def run_plan(plan: Plan, dry_run: bool = False, progress: ProgressCb | None = None, keep_temp: bool = False) -> dict:
    """Dry-run -> the plan as JSON (exact commands); otherwise execute and return a result dict."""
    if dry_run:
        return plan.to_json(dry_run=True)
    ffmpeg_path()
    res = execute(plan, progress=progress, keep_temp=keep_temp)
    res["commands"] = plan.commands()
    return res


def need_pyav():
    return lazy_import("av", "PyAV frame access")
