"""Frame tools: stills (incl. best-frame selection), contact sheets, timelapse assembly (deflicker, Ken Burns),
star-trail / lighten-blend video and images, GIF / WebP previews."""
from __future__ import annotations

import glob as _glob
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from ..util import IMAGE_EXTS, RAW_EXTS, VIDEO_EXTS
from . import encode as venc
from . import graph as vgraph
from .ff import (Caps, LazyArg, Plan, Step, VideoError, base_cmd, capabilities, fmt_num,
                 ffmpeg_path)
from .options import GradeOptions, parse_size
from .plan import trim_args, window

STILL_FORMATS = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg", "tif": ".tif", "tiff": ".tif"}


# ----------------------------------------------------------------------------- stills
def _still_filters(info: dict, opts: GradeOptions, caps: Caps, fmt: str, bits: int, notes: list[str]) -> tuple[list[str], list[str]]:
    """-> (filters, output pix_fmt args).  Always goes via RGB with an explicit BT.709 matrix."""
    hdr_mode = vgraph.resolve_hdr_mode(info, opts)
    f = vgraph.normalise_filters(info, opts, caps, hdr_mode, notes)
    f += [f"scale=in_range=tv:in_color_matrix=bt709:flags={vgraph.SCALE_FLAGS}"]
    if fmt in ("jpg", "jpeg"):
        f += ["format=gbrp", f"scale=out_color_matrix=bt601:out_range=pc:flags={vgraph.SCALE_FLAGS}", "format=yuvj444p"]
        return f, ["-q:v", "1", "-pix_fmt", "yuvj444p"]
    pf = "rgb48be" if bits == 16 else "rgb24"
    if fmt in ("tif", "tiff"):
        pf = "rgb48le" if bits == 16 else "rgb24"
    f += [f"format={pf}"]
    return f, ["-pix_fmt", pf]


def _grab_cmd(src: str | Path, t: Any, out: str | Path, filters: list[str], pixargs: list[str], extra: list[str] | None = None) -> list:
    cmd = base_cmd("-ss", t, "-i", str(src), "-an", "-frames:v", "1", "-vf", ",".join(filters), *pixargs,
                   *(extra or []), str(out), progress=False)
    return cmd


def pick_sharpest_time(src: str | Path, info: dict, centre: float, window_s: float = 1.0, width: int = 480) -> float:
    """Decode the frames in [centre - w/2, centre + w/2] at low resolution and return the time of the sharpest
    one (variance of the Laplacian; ties -> closest to the centre)."""
    import cv2

    fps = float(info.get("fps") or 25.0)
    a = max(0.0, centre - window_s / 2.0)
    dw, dh = int(info.get("display_width") or info["width"]), int(info.get("display_height") or info["height"])
    w = min(width, dw)
    h = max(2, int(round(w * dh / dw / 2.0)) * 2)
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-ss", fmt_num(a, 4), "-t", fmt_num(window_s, 4),
           "-i", str(src), "-an", "-vf", f"scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-pix_fmt", "gray",
           "-fps_mode", "passthrough", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True)
    n = len(r.stdout) // (w * h)
    if r.returncode != 0 or n == 0:
        return centre
    best, best_i = -1.0, 0
    for i in range(n):
        g = np.frombuffer(r.stdout[i * w * h:(i + 1) * w * h], np.uint8).reshape(h, w)
        score = float(cv2.Laplacian(g, cv2.CV_32F).var())
        t = a + i / fps
        score *= 1.0 - 0.02 * abs(t - centre) / max(window_s, 1e-6)          # mild preference for the requested moment
        if score > best:
            best, best_i = score, i
    return a + best_i / fps


def build_stills_plan(src: str | Path, info: dict, out_dir: str | Path, *, times: list[float] | None = None,
                      every: float | None = None, count: int | None = None, best_window: float | None = None,
                      representative: int | None = None, fmt: str = "png", bits: int = 16, quality_name: str | None = None,
                      opts: GradeOptions | None = None, caps: Caps | None = None, shots: list[dict] | None = None) -> Plan:
    """Extract still frames that the photo pipeline can develop (PNG 16-bit / TIFF / JPEG)."""
    caps = caps or capabilities()
    opts = opts or GradeOptions()
    fmt = fmt.lower()
    if fmt not in STILL_FORMATS:
        raise VideoError(f"unknown still format '{fmt}' (png|jpg|tif)")
    ext = STILL_FORMATS[fmt]
    plan = Plan("stills")
    out_dir = Path(out_dir)
    stem = Path(src).stem
    filters, pixargs = _still_filters(info, opts, caps, fmt, bits, plan.notes)
    t0, t1 = window(info, opts)
    dur = max(t1 - t0, 0.0)
    if bits == 16 and fmt in ("jpg", "jpeg"):
        plan.warnings.append("JPEG is 8-bit: use png or tif for 16-bit stills")
    modes = sum(x is not None for x in (times, every, count, representative, shots))
    if modes != 1:
        raise VideoError("choose exactly one of --at/--every/--count/--representative/--per-shot")
    if representative:
        n = max(1, int(round(info.get("fps", 25) * dur / representative)))
        pat = out_dir / f"{stem}_rep_%03d{ext}"
        cmd = base_cmd(*trim_args(opts), "-i", str(src), "-an", "-vf", ",".join([f"thumbnail=n={n}", *filters]),
                       "-fps_mode", "vfr", "-frames:v", str(representative), *pixargs, str(pat), progress=False)
        plan.add(Step("ffmpeg", f"{representative} representative frame(s) (thumbnail filter, batch {n})", cmd))
        plan.outputs.append(str(pat))
        return plan
    if every or count:
        rate = (1.0 / every) if every else (count / dur if dur else 1.0)
        pat = out_dir / f"{stem}_%04d{ext}"
        extra = ["-frames:v", str(count)] if count else []
        cmd = base_cmd(*trim_args(opts), "-i", str(src), "-an", "-vf", ",".join([f"fps={fmt_num(rate, 8)}", *filters]),
                       *extra, *pixargs, str(pat), progress=False)
        plan.add(Step("ffmpeg", f"stills every {fmt_num(1 / rate, 3)} s", cmd, duration=dur or None))
        plan.outputs.append(str(pat))
        if best_window:
            plan.warnings.append("--best applies to --at / --per-shot (explicit moments), not to --every/--count")
        return plan
    centres = list(times) if times is not None else [(s["start"] + s["end"]) / 2.0 for s in (shots or [])]
    for i, t in enumerate(centres):
        out = out_dir / f"{stem}_t{t:09.3f}{ext}"
        if best_window:
            holder: dict[str, float] = {}

            def pick(plan_: Plan, progress=None, _t=t, _h=holder):
                _h["t"] = pick_sharpest_time(src, info, _t, best_window)
                plan_.state.setdefault("best_times", {})[f"{_t}"] = _h["t"]

            plan.add(Step("python", f"best frame near {t:.3f}s (Laplacian variance, +-{best_window / 2:g}s)", fn=pick))
            ts = LazyArg(lambda _h=holder: fmt_num(_h["t"], 4), f"<best time near {t:.3f}>")
            plan.add(Step("ffmpeg", f"extract {out.name}", _grab_cmd(src, ts, out, filters, pixargs)))
        else:
            plan.add(Step("ffmpeg", f"extract {out.name}", _grab_cmd(src, fmt_num(t, 4), out, filters, pixargs)))
        plan.outputs.append(str(out))
    return plan


# ----------------------------------------------------------------------------- contact sheet
def build_contact_sheet_plan(src: str | Path, info: dict, out: str | Path, *, cols: int = 5, rows: int = 4,
                             thumb_width: int = 320, labels: bool = True, opts: GradeOptions | None = None,
                             caps: Caps | None = None) -> Plan:
    caps = caps or capabilities()
    opts = opts or GradeOptions()
    plan = Plan("contact-sheet")
    n = cols * rows
    t0, t1 = window(info, opts)
    dur = max(t1 - t0, 0.001)
    hdr_mode = vgraph.resolve_hdr_mode(info, opts)
    f = vgraph.normalise_filters(info, opts, caps, hdr_mode, plan.notes)
    f += [f"fps={fmt_num(n / dur, 8)}", f"scale={thumb_width}:-2:flags=area:in_range=tv:in_color_matrix=bt709", "format=rgb24"]
    if labels:
        if caps.has_filter("drawtext"):
            f.append("drawtext=text='%{pts\\:hms}':x=6:y=h-th-6:fontsize=14:fontcolor=white:box=1:boxcolor=black@0.5")
        else:
            plan.warnings.append("no drawtext in this ffmpeg: contact sheet without timestamps")
    f.append(f"tile={cols}x{rows}:padding=4:margin=4:color=0x111111")
    cmd = base_cmd(*trim_args(opts), "-i", str(src), "-an", "-vf", ",".join(f), "-frames:v", "1",
                   *(["-q:v", "2"] if str(out).lower().endswith((".jpg", ".jpeg")) else []), str(out), progress=False)
    plan.add(Step("ffmpeg", f"contact sheet {cols}x{rows}", cmd, duration=dur))
    plan.outputs.append(str(out))
    return plan


# ----------------------------------------------------------------------------- timelapse / star trails
def discover_inputs(inputs: list[str | Path] | str | Path, develop: bool = False) -> tuple[list[Path], str]:
    """Folder / glob / list of files -> sorted image list and the common extension ('' if mixed)."""
    items: list[Path] = []
    seq = inputs if isinstance(inputs, (list, tuple)) else [inputs]
    for it in seq:
        p = Path(str(it))
        if p.is_dir():
            items += sorted(q for q in p.iterdir() if q.suffix.lower() in (IMAGE_EXTS | (RAW_EXTS if develop else set())))
        elif any(ch in str(it) for ch in "*?["):
            items += sorted(Path(x) for x in _glob.glob(str(it)))
        elif p.exists():
            items.append(p)
        else:
            raise VideoError(f"input not found: {p}")
    items = [p for p in items if p.suffix.lower() not in VIDEO_EXTS]
    if not items:
        raise VideoError("no still images found")
    raw = [p for p in items if p.suffix.lower() in RAW_EXTS]
    if raw and not develop:
        raise VideoError(f"{len(raw)} RAW file(s) given: add --develop to render them through the photo pipeline first")
    exts = {p.suffix.lower() for p in items}
    return items, (next(iter(exts)) if len(exts) == 1 and not develop else "")


def _image_size(p: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(p) as im:
        return im.size


def _kenburns(kb: dict | float | bool | None, n: int, w: int, h: int, fps: float) -> list[str]:
    if not kb:
        return []
    k = dict(kb) if isinstance(kb, dict) else {"zoom": 0.2 if kb is True else float(kb)}
    z = float(k.get("zoom", 0.2))
    px, py = float(k.get("x", 0.5)), float(k.get("y", 0.5))
    return [f"scale={w * 2}:-2:flags=lanczos", f"zoompan=z='1+{fmt_num(z)}*on/{max(n - 1, 1)}':x='(iw-iw/zoom)*{fmt_num(px)}'"
            f":y='(ih-ih/zoom)*{fmt_num(py)}':d=1:s={w}x{h}:fps={fmt_num(fps, 6)}"]


def build_timelapse_plan(inputs, out: str | Path, *, fps: float = 24.0, preset: dict | None = None, deflicker: int = 10,
                         kenburns: Any = None, size: str | None = None, blend: int = 0, develop: bool = False,
                         mode: str | None = None, art: str | None = None, develop_long_edge: int | None = None,
                         hw: str = "auto", caps: Caps | None = None, encoder: str | None = None, overwrite: bool = True,
                         trails: dict | None = None, name: str = "timelapse") -> Plan:
    """Assemble stills (or photos developed through the pipeline) into a video: deflicker, optional Ken Burns,
    optional ``tmix`` motion blur, optional star-trail accumulation (``trails={'decay': 0.99}``)."""
    caps = caps or capabilities()
    plan = Plan(name)
    items, common = discover_inputs(inputs, develop)
    n = len(items)
    popts = venc.preset_options(preset)
    src_pattern: str | None = None
    prep: Step | None = None
    if develop:
        dev_dir = plan.workdir / "developed"

        def develop_fn(plan_: Plan, progress=None, _items=items):
            from .. import util
            from ..pipeline import engine

            dev_dir.mkdir(parents=True, exist_ok=True)
            for i, p in enumerate(_items):
                if mode or art:
                    stack = engine.resolve_stack(mode, art, None, engine.source_kind(p), None)
                    img = engine.render(p, stack, long_edge=develop_long_edge)
                else:
                    img = engine.render_file(p, long_edge=develop_long_edge)
                util.imwrite_png(dev_dir / f"f_{i:06d}.png", img)
                if progress:
                    progress({"percent": 100.0 * (i + 1) / len(_items), "frame": i + 1})

        plan.add(Step("python", f"develop {n} photo(s) through the photo pipeline (mode={mode}, art={art})", fn=develop_fn))
        src_pattern = str(dev_dir / "f_%06d.png")
        w0, h0 = None, None
        in_args = ["-framerate", fmt_num(fps, 6), "-start_number", "0", "-i", src_pattern]
    elif common:
        pat = items[0].parent / ("*" + common)
        if len({p.parent for p in items}) == 1 and len(items) == len(sorted(items[0].parent.glob("*" + common))):
            in_args = ["-framerate", fmt_num(fps, 6), "-pattern_type", "glob", "-i", str(pat)]
        else:
            in_args = None
    else:
        in_args = None
    if in_args is None:                         # mixed / selective lists: concat demuxer list file
        lst = plan.workdir / "frames.txt"

        def write_list(plan_: Plan, progress=None, _items=items):
            lst.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            for p in _items:
                lines.append("file '" + str(p.resolve()).replace("'", "'\\''") + "'")
                lines.append(f"duration {1.0 / fps:.8f}")
            lst.write_text("\n".join(lines) + "\n", encoding="utf-8")

        plan.add(Step("python", f"write concat list for {n} image(s)", fn=write_list))
        in_args = ["-f", "concat", "-safe", "0", "-i", str(lst)]

    try:
        w0, h0 = _image_size(items[0]) if not develop else (None, None)
    except Exception:
        w0, h0 = 1920, 1080
    if develop and w0 is None:
        w0, h0 = (develop_long_edge or 1920), None
        h0 = int(round(w0 * 2 / 3 / 2.0)) * 2
    tw, th = (parse_size(size, w0, h0) if size else
              (min(w0, 3840) // 2 * 2, min(h0, 2160) // 2 * 2))
    if popts.get("size") and not size:
        sz = popts["size"]
        if "max_height" in sz and th > sz["max_height"]:
            tw, th = parse_size(f"h{sz['max_height']}", tw, th)
    f: list[str] = [f"scale=in_range=pc:in_color_matrix=bt601:flags={vgraph.SCALE_FLAGS}", "format=gbrp16le",
                    f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags={vgraph.SCALE_FLAGS}",
                    f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black",
                    f"scale=out_color_matrix=bt709:out_range=tv:flags={vgraph.SCALE_FLAGS}", "format=yuv420p10le"]
    if deflicker and deflicker > 1:
        if caps.has_filter("deflicker"):
            f.append(f"deflicker=size={int(deflicker)}:mode=pm")
        else:
            plan.warnings.append("no deflicker filter in this ffmpeg; skipped")
    if blend and blend > 1:
        f.append(f"tmix=frames={int(blend)}")
    if trails is not None:
        decay = float(trails.get("decay", 1.0))
        if caps.has_filter("lagfun"):
            f.append(f"lagfun=decay={fmt_num(decay, 4)}")
            plan.notes.append(f"lighten-blend accumulation with lagfun (decay {decay:g}: 1.0 = permanent trails)")
        else:
            k = int(trails.get("window", 12))
            f += ["tblend=all_mode=lighten"] * max(1, k - 1)
            plan.warnings.append(f"no lagfun: sliding {k}-frame lighten window via chained tblend")
    f += _kenburns(kenburns, n, tw, th, fps)
    spec = venc.build_encode(popts, fps=fps, size=(tw, th), hdr_mode="sdr", info={"pix_fmt": "yuv420p10le"},
                             tags={"primaries": "bt709", "trc": "bt709", "space": "bt709", "range": "tv"}, hw=hw, caps=caps,
                             encoder_override=encoder)
    plan.notes += spec.notes
    plan.warnings += spec.warnings
    f += ["setsar=1", f"format={spec.pix_fmt}"]
    cmd = base_cmd(*in_args, "-vf", ",".join(f), "-r", fmt_num(fps, 6), *spec.video_args, "-an",
                   *(["-movflags", spec.movflags] if spec.movflags else []), str(out), overwrite=overwrite)
    plan.add(Step("ffmpeg", f"encode {Path(out).name} ({n} frames @ {fps:g} fps, {spec.encoder})", cmd, duration=n / fps))
    plan.outputs.append(str(out))
    plan.info = {"frames": n, "size": [tw, th], "fps": fps, "encoder": spec.encoder}
    return plan


def build_startrails_image_plan(inputs, out: str | Path, *, decay: float = 1.0, develop: bool = False) -> Plan:
    """Lighten-blend (max) all frames into one star-trail image (streamed: one frame in memory at a time).
    ``decay < 1`` makes trails fade (comet effect); the result is a 16-bit PNG/TIFF."""
    plan = Plan("startrails-image")
    items, _ = discover_inputs(inputs, develop)

    def fn(plan_: Plan, progress=None, _items=items):
        import cv2

        acc = None
        for i, p in enumerate(_items):
            if p.suffix.lower() in RAW_EXTS:
                from ..pipeline import engine

                img = (engine.render_file(p) * 65535.0).astype(np.float32)
            else:
                a = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
                if a is None:
                    raise VideoError(f"cannot read {p}")
                if a.ndim == 2:
                    a = np.stack([a] * 3, -1)
                a = a[..., :3]
                img = (a.astype(np.float32) * (257.0 if a.dtype == np.uint8 else 1.0))[..., ::-1]
            acc = img if acc is None else np.maximum(acc * decay, img) if decay < 1.0 else np.maximum(acc, img)
            if progress:
                progress({"percent": 100.0 * (i + 1) / len(_items), "frame": i + 1})
        arr = np.clip(acc, 0, 65535).astype(np.uint16)[..., ::-1]
        if not cv2.imwrite(str(out), np.ascontiguousarray(arr)):
            raise VideoError(f"cannot write {out}")

    plan.add(Step("python", f"lighten-blend {len(items)} frame(s) -> {Path(out).name}", fn=fn))
    plan.outputs.append(str(out))
    return plan


# ----------------------------------------------------------------------------- GIF / WebP
def wrap_gif_palette(flag: str, graph: str, label: str | None, anim: dict) -> tuple[str, str, str]:
    """Append split / palettegen / paletteuse to a compiled graph -> (flag, graph, out_label)."""
    stats = anim.get("stats", "diff")
    dither = anim.get("dither", "bayer")
    mc = int(anim.get("max_colors", 256))
    if flag == "-vf":
        src = f"[0:v]{graph},split[a][b]"
    else:
        src = f"{graph};{label}split[a][b]"
    tail = (f";[a]palettegen=max_colors={mc}:stats_mode={stats}[p];[b][p]paletteuse=dither={dither}"
            + (":bayer_scale=5" if dither == "bayer" else "") + ":diff_mode=rectangle[gif]")
    return "-filter_complex", src + tail, "[gif]"
