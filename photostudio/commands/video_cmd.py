"""``photo video ...``: probe | grade | stabilize | denoise | export | stills | timelapse | shots | lut | preview.

Every sub-command takes ``--dry-run`` (prints and returns the exact ffmpeg argv lists without touching anything)
and reports ffmpeg ``-progress`` progress on stderr when run in a terminal (``--quiet`` / ``--no-progress`` off).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..util import parse_value


# ----------------------------------------------------------------------------- argparse helpers
def _add_io(p, *, positional: bool = True) -> None:
    if positional:
        p.add_argument("input", help="input video file")
    p.add_argument("-o", "--output", help="output file (default: <input>_<suffix>.<ext>)")
    p.add_argument("--start", type=float, help="start time (s)")
    p.add_argument("--duration", type=float, help="duration (s)")
    p.add_argument("--to", type=float, help="end time (s) (alternative to --duration)")


def _add_run(p) -> None:
    p.add_argument("--dry-run", action="store_true", help="print the exact ffmpeg commands (JSON) and do nothing")
    p.add_argument("--no-progress", action="store_true", help="do not print progress")
    p.add_argument("--keep-temp", action="store_true", help="keep the temp work dir (LUTs, transforms)")
    p.add_argument("--no-overwrite", action="store_true", help="fail instead of overwriting the output")


def _add_enc(p, default_preset: str | None = None) -> None:
    p.add_argument("--preset", default=default_preset, help="export preset name[:variant] from presets/video "
                   "(youtube_1080p, youtube_4k, reels_9x16_1080x1920, archive_prores, dnxhr, hevc_10bit, preview_fast, gif)")
    p.add_argument("--hw", choices=["auto", "on", "off"], default="auto",
                   help="hardware encoder (NVENC) policy; auto = use it only if it really works (default: auto)")
    p.add_argument("--encoder", help="force a specific encoder (e.g. libx265)")
    p.add_argument("--audio", choices=["auto", "copy", "aac", "pcm", "loudnorm", "none"],
                   help="audio handling (default: preset; passthrough when compatible)")
    p.add_argument("--loudnorm", metavar="I[,TP[,LRA]]", help="loudnorm targets I,TP,LRA e.g. -14,-1,11 (with --audio loudnorm; a leading minus works with or without =)")


def _add_look(p) -> None:
    g = p.add_argument_group("look (colour: baked into a .cube and applied with lut3d)")
    g.add_argument("--mode", help="shoot-type mode preset (e.g. video, portrait, landscape)")
    g.add_argument("--art", help="art/look preset (colour-only ones go through the LUT)")
    g.add_argument("--stack", help="stack file (JSON/YAML list, {stack:[..]} or a sidecar) layered on top")
    g.add_argument("--from-image", metavar="IMG", help="reuse the colour stack of a developed photo (its .ps.json)")
    g.add_argument("--set", action="append", metavar="OP.PARAM=VALUE", help="override/add a param, e.g. exposure.ev=0.3")
    g.add_argument("--lut", help="apply an external .cube after the look")
    g.add_argument("--intensity", type=float, default=1.0, help="look strength 0..1 (LUT blended with identity)")
    g.add_argument("--lut-size", type=int, choices=[17, 33, 65], default=33)
    g.add_argument("--transfer", choices=["srgb", "bt709", "gamma24"], default="srgb",
                   help="transfer used to decode video values for the stack (srgb = same as photos)")
    g.add_argument("--lut-bits", type=int, choices=[8, 10, 12, 16], default=10, help="RGB working depth of the lut3d stage")
    g.add_argument("--interp", choices=["tetrahedral", "trilinear", "nearest", "pyramid", "prism"], default="tetrahedral")


def _add_shots(p) -> None:
    g = p.add_argument_group("per-shot grading")
    g.add_argument("--per-shot", action="store_true", help="detect shots and bake one LUT per shot")
    g.add_argument("--shots", metavar="FILE|sidecar", help="shot list JSON (default: <video>.ps.json when present)")
    g.add_argument("--detect-method", choices=["auto", "pyscenedetect", "ffmpeg"], default="auto")
    g.add_argument("--threshold", type=float, help="cut threshold (PySceneDetect ContentDetector 27 / ffmpeg 0.3)")
    g.add_argument("--min-shot", type=float, default=0.5, help="minimum shot length in seconds")
    g.add_argument("--auto", choices=["off", "exposure", "wb", "both"], default="off",
                   help="per-shot auto exposure / white-balance match (constant per shot)")
    g.add_argument("--auto-reference", default="median", help="median | first | index:N (shot number, 1 = first) | absolute")
    g.add_argument("--max-ev", type=float, default=1.0, help="max auto exposure change (EV)")
    g.add_argument("--smooth", action="store_true", help="split long shots and smooth auto params over time")
    g.add_argument("--shot-apply", choices=["auto", "timeline", "concat"], default="auto")
    g.add_argument("--save-sidecar", action="store_true", help="write/update <video>.ps.json with the shots")


def _add_fx(p) -> None:
    g = p.add_argument_group("clean-up, detail and finishing (native ffmpeg filters)")
    g.add_argument("--denoise", metavar="LEVEL", help="light|medium|strong|chroma|temporal|nlmeans or 0..1 or a:b:c:d (hqdn3d)")
    g.add_argument("--sharpen", metavar="LEVEL", help="light|medium|strong or 0..1 (cas, applied after resize)")
    g.add_argument("--grain", type=float, help="film grain amount 0..1 (noise filter, temporal)")
    g.add_argument("--vignette", type=float, help="vignette amount 0..1")
    g.add_argument("--deband", action="store_true", help="deband after grading (8-bit footage)")
    g.add_argument("--deflicker", type=int, metavar="N", help="deflicker window in frames")
    g.add_argument("--stabilize", nargs="?", const=6, type=int, metavar="SHAKINESS",
                   help="vid.stab two-pass (deshake fallback); optional shakiness 1-10")
    g.add_argument("--smoothing", type=int, help="stabilisation smoothing (frames each side)")
    g.add_argument("--tripod", action="store_true", help="locked-off shot stabilisation")
    g.add_argument("--lens-k1", type=float, help="manual radial distortion k1 (lenscorrection)")
    g.add_argument("--lens-k2", type=float, help="manual radial distortion k2")
    g.add_argument("--speed", type=float, default=1.0, help=">1 faster, <1 slower")
    g.add_argument("--slowmo", type=float, help="conform 100/120p to slow motion: 4 = quarter speed, every frame kept")
    g.add_argument("--fps", type=float, help="output frame rate")
    g.add_argument("--fps-method", choices=["fps", "blend", "mci"], default="fps")
    g.add_argument("--blend", type=int, default=0, help="tmix N frames (motion blur for speed-ups)")
    g.add_argument("--scale", help="1920x1080 | 1080p | h1080 | w1280 | 50%%")
    g.add_argument("--reframe", metavar="ASPECT|WxH", help="e.g. 9:16 or 1080x1920")
    g.add_argument("--reframe-mode", choices=["crop", "pad", "blur"], default="blur")
    g.add_argument("--letterbox", type=float, metavar="ASPECT", help="matte bars, e.g. 2.39")
    g.add_argument("--fade-in", type=float, default=0.0)
    g.add_argument("--fade-out", type=float, default=0.0)
    g.add_argument("--text", help="burn-in text (drawtext)")
    g.add_argument("--hdr", choices=["auto", "tonemap", "passthrough"], default="auto", help="HDR PQ clip handling")
    g.add_argument("--tonemap", default="mobius", help="tonemap algorithm (mobius|hable|reinhard|...)")
    g.add_argument("--frames", choices=["ffmpeg", "python"], default="ffmpeg",
                   help="python = pipe raw frames through numpy (SLOW; for raster art modes)")
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--reseed-grain", action="store_true", help="python path: new grain seed per frame")
    g.add_argument("--max-width", type=int, help="python path: process frames at most this wide")


def _global_flags():
    """Parent parser with the global flags (--json/--quiet/--root/--debug) for nested sub-commands (shared pattern)."""
    from ..cli import _common_parser

    return _common_parser(False)


def register(subparsers) -> None:
    p = subparsers.add_parser("video", help="video: probe, grade (LUT), per-shot, stabilise, denoise, export, stills, timelapse",
                              description="Video mode (Canon R50 8-bit H.264 / HDR PQ HEVC). ffmpeg does the heavy lifting; "
                                          "colour looks are baked into a .cube and applied with lut3d. "
                                          "Set PHOTOSTUDIO_FFMPEG to choose the binary.")
    vs = _Sub(p.add_subparsers(dest="video_cmd", metavar="<subcommand>", required=True), _global_flags())

    sp = vs.add_parser("probe", help="describe a clip (codec, fps, bit depth, colour tags, HDR, audio)")
    sp.add_argument("input")
    sp.add_argument("--caps", action="store_true", help="also report the ffmpeg build capabilities")
    sp.set_defaults(func=_library_inputs(cmd_probe))

    sp = vs.add_parser("grade", help="apply a look/LUT (+ per-shot match, denoise, stabilise ...) and encode")
    _add_io(sp); _add_look(sp); _add_shots(sp); _add_fx(sp); _add_enc(sp, "youtube_1080p"); _add_run(sp)
    sp.set_defaults(func=_library_inputs(cmd_grade))

    sp = vs.add_parser("stabilize", help="vid.stab two-pass stabilisation (deshake fallback)")
    _add_io(sp); _add_enc(sp); _add_run(sp)
    sp.add_argument("--shakiness", type=int, default=6)
    sp.add_argument("--smoothing", type=int, default=20)
    sp.add_argument("--optzoom", type=int, choices=[0, 1, 2], default=2, help="0 none, 1 static, 2 adaptive zoom")
    sp.add_argument("--crop", choices=["keep", "black"], default="keep")
    sp.add_argument("--tripod", action="store_true")
    sp.add_argument("--no-sharpen", action="store_true", help="skip the post-transform unsharp")
    sp.set_defaults(func=_library_inputs(cmd_stabilize))

    sp = vs.add_parser("denoise", help="temporal/spatial denoise (hqdn3d / atadenoise / nlmeans)")
    _add_io(sp); _add_enc(sp); _add_run(sp)
    sp.add_argument("--level", default="medium", help="light|medium|strong|chroma|temporal|nlmeans|nlmeans_strong|0..1|a:b:c:d")
    sp.set_defaults(func=_library_inputs(cmd_denoise))

    sp = vs.add_parser("export", help="encode with an export preset (colour tags, GOP, faststart, audio)")
    _add_io(sp); _add_look(sp); _add_shots(sp); _add_fx(sp); _add_enc(sp, "youtube_1080p"); _add_run(sp)
    sp.set_defaults(func=_library_inputs(cmd_export))

    sp = vs.add_parser("stills", help="extract stills (PNG16/TIFF/JPEG) the photo pipeline can develop; best-frame selection")
    _add_io(sp); _add_run(sp)
    sp.add_argument("--at", nargs="+", type=float, metavar="T", help="timestamps in seconds")
    sp.add_argument("--every", type=float, metavar="S", help="one still every S seconds")
    sp.add_argument("--count", type=int, help="N evenly spaced stills")
    sp.add_argument("--representative", type=int, metavar="N", help="N representative frames (ffmpeg thumbnail filter)")
    sp.add_argument("--per-shot", action="store_true", help="one still per detected shot (middle)")
    sp.add_argument("--best", nargs="?", const=1.0, type=float, metavar="WINDOW",
                    help="pick the sharpest frame within WINDOW seconds around each moment (default 1.0)")
    sp.add_argument("--format", choices=["png", "jpg", "tif"], default="png")
    sp.add_argument("--bits", type=int, choices=[8, 16], default=16)
    sp.add_argument("--hdr", choices=["auto", "tonemap"], default="auto")
    sp.set_defaults(func=_library_inputs(cmd_stills))

    sp = vs.add_parser("timelapse", help="assemble stills / developed photos into a video (deflicker, Ken Burns, star trails)")
    sp.add_argument("inputs", nargs="*", help="folder, glob, files or selectors (id:N, @album:NAME, @last-import ...; "
                                              "RAW needs --develop)")
    sp.add_argument("--query", help="library filter selecting the stills, e.g. \"taken:2026-06-14 AND rating>=3\"")
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--fps", type=float, default=24.0)
    sp.add_argument("--deflicker", type=int, default=10, help="deflicker window (frames), 0 = off")
    sp.add_argument("--kenburns", nargs="?", const=0.2, type=float, metavar="ZOOM", help="slow zoom (zoompan), default 0.2")
    sp.add_argument("--size", help="output size (default: first image, max 4K)")
    sp.add_argument("--blend", type=int, default=0, help="tmix frames (motion blur)")
    sp.add_argument("--develop", action="store_true", help="render inputs through the photo pipeline first")
    sp.add_argument("--mode"); sp.add_argument("--art")
    sp.add_argument("--long-edge", type=int, help="developed frame long edge")
    sp.add_argument("--trails", nargs="?", const=1.0, type=float, metavar="DECAY",
                    help="star-trail video: lighten-blend accumulation (lagfun), 1.0 = permanent, 0.98 = comet")
    sp.add_argument("--preset", default="youtube_1080p")
    sp.add_argument("--hw", choices=["auto", "on", "off"], default="auto")
    sp.add_argument("--encoder")
    sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--no-progress", action="store_true")
    sp.add_argument("--keep-temp", action="store_true"); sp.add_argument("--no-overwrite", action="store_true")
    sp.set_defaults(func=_library_inputs(cmd_timelapse))

    sp = vs.add_parser("startrails", help="lighten-blend stills into one star-trail image")
    sp.add_argument("inputs", nargs="*", help="files, folder or selectors (id:N, @album:NAME, @last-import ...)")
    sp.add_argument("--query", help="library filter selecting the stills")
    sp.add_argument("-o", "--output", required=True, help=".png or .tif (16-bit)")
    sp.add_argument("--decay", type=float, default=1.0, help="<1 fades trails (comet)")
    sp.add_argument("--develop", action="store_true")
    sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--no-progress", action="store_true")
    sp.set_defaults(func=_library_inputs(cmd_startrails))

    sp = vs.add_parser("shots", help="detect shots, list/assign looks, analyse per-shot auto match, save <video>.ps.json")
    sp.add_argument("input")
    sp.add_argument("--method", choices=["auto", "pyscenedetect", "ffmpeg"], default="auto")
    sp.add_argument("--threshold", type=float)
    sp.add_argument("--min-len", type=float, default=0.5)
    sp.add_argument("--redetect", action="store_true", help="ignore an existing sidecar")
    sp.add_argument("--assign", action="append", metavar="N:mode=X|art=Y", help="N:mode=X or N:art=Y; N is the shot number as listed, starting at 1 (e.g. 2:art=teal_orange, repeatable)")
    sp.add_argument("--default-mode"); sp.add_argument("--default-art")
    sp.add_argument("--analyze", choices=["exposure", "wb", "both"], help="compute and store per-shot auto match values")
    sp.add_argument("--auto-reference", default="median")
    sp.add_argument("--save", action="store_true", help="write <video>.ps.json")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=_library_inputs(cmd_shots))

    sp = vs.add_parser("lut", help="bake a look (mode/art/stack/photo sidecar) into a .cube; --dry-run shows the lut3d command")
    sp.add_argument("input", nargs="?", help="optional video: prints/returns the ffmpeg command that applies the LUT")
    sp.add_argument("-o", "--output", help="output .cube (default: ./look.cube)")
    _add_look(sp)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=_library_inputs(cmd_lut))

    sp = vs.add_parser("preview", help="quick preview: small video, GIF/WebP, or a contact sheet (graded)")
    _add_io(sp); _add_look(sp); _add_shots(sp); _add_fx(sp); _add_run(sp)
    sp.add_argument("--kind", choices=["video", "gif", "webp", "contact"], default="video")
    sp.add_argument("--cols", type=int, default=5); sp.add_argument("--rows", type=int, default=4)
    sp.add_argument("--no-labels", action="store_true")
    sp.add_argument("--hw", choices=["auto", "on", "off"], default="auto")
    sp.add_argument("--encoder"); sp.add_argument("--preset")
    sp.add_argument("--audio", choices=["auto", "copy", "aac", "pcm", "loudnorm", "none"])
    sp.add_argument("--loudnorm")
    sp.set_defaults(func=_library_inputs(cmd_preview))


class _Sub:
    def __init__(self, sub, common):
        self._sub, self._common = sub, common

    def add_parser(self, name, **kw):
        kw["parents"] = list(kw.get("parents", [])) + [self._common]
        return self._sub.add_parser(name, **kw)


# ----------------------------------------------------------------------------- shared plumbing
def _V():
    from .. import video

    return video


def _selectors(items: list[str], args, label: str = "inputs") -> list[str]:
    """Replace library selectors (id:N, @album:X, @last-import ...) by the file paths they select; plain paths, folders and
    globs are left alone for the video code to expand."""
    from ..cli import is_selector, resolve_images

    root = getattr(args, "root", None)
    out: list[str] = []
    for it in items:
        out += [str(p) for p in resolve_images([it], args, label=label)] if is_selector(it, root) else [it]
    return out


def _library_inputs(fn):
    """Wrap a command so ``input`` / ``inputs`` / ``--from-image`` / ``--query`` accept the library selectors like every
    other command (one shared resolver: :func:`photostudio.cli.resolve_images`)."""
    def run(args):
        from ..cli import resolve_images, resolve_one

        if isinstance(getattr(args, "input", None), str):
            args.input = str(resolve_one(args.input, args, label="video"))
        if getattr(args, "inputs", None) is not None or getattr(args, "query", None):
            items = _selectors(list(getattr(args, "inputs", None) or []), args)
            if getattr(args, "query", None):
                items += [str(p) for p in resolve_images([], args, query=args.query, label="inputs")]
            if not items:
                raise ValueError("no inputs: give files, a folder, a selector (id:N, @album:NAME, @last-import ...) or --query")
            args.inputs = items
        if isinstance(getattr(args, "from_image", None), str):
            args.from_image = str(resolve_one(args.from_image, args))
        return fn(args)

    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def _progress(args):
    if getattr(args, "quiet", False) or getattr(args, "no_progress", False) or not sys.stderr.isatty():
        return None
    from ..video.ff import ConsoleProgress

    return ConsoleProgress("video")


def _stack_from_file(path: str) -> list[dict]:
    import yaml

    text = Path(path).expanduser().read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if isinstance(data, dict):
        if "versions" in data:                                      # a photo sidecar
            v = data["versions"].get(data.get("active", "default"), {})
            return list(v.get("stack") or [])
        data = data.get("stack", [])
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a stack list")
    return data


def _sets_to_stack(sets: list[str] | None) -> list[dict]:
    from ..cli import flatten_sets

    out: dict[str, dict] = {}
    order: list[str] = []
    for kv in flatten_sets(sets):
        if "=" not in kv:
            raise ValueError(f"--set expects op.param=value, got '{kv}'")
        key, val = kv.split("=", 1)
        op, _, param = key.partition(".")
        if not param:
            raise ValueError(f"--set expects op.param=value, got '{kv}'")
        if op not in out:
            out[op] = {"op": op, "params": {}, "enabled": True, "mask": None, "blend": "normal", "opacity": 1.0, "v": 1}
            order.append(op)
        out[op]["params"][param.strip()] = parse_value(val.strip())
    return [out[o] for o in order]


def _user_stack(args) -> list[dict]:
    V = _V()
    stack: list[dict] = []
    if getattr(args, "from_image", None):
        from .. import sidecar

        doc = sidecar.load(args.from_image)
        stack = V.stack.merge_stacks(stack, sidecar.get_stack(doc))
        v = doc["versions"].get(doc.get("active", "default"), {})
        if not stack and (v.get("mode") or v.get("art")):
            stack, _ = V.stack.resolve_video_stack(v.get("mode"), v.get("art"))
    if getattr(args, "stack", None):
        stack = V.stack.merge_stacks(stack, _stack_from_file(args.stack))
    stack = V.stack.merge_stacks(stack, _sets_to_stack(getattr(args, "set", None)))
    return stack


def _parse_loudnorm(s: str | None) -> dict | None:
    """``I[,TP[,LRA]]`` (integrated LUFS, true peak dBTP, loudness range LU), e.g. ``-14,-1,11``."""
    if not s:
        return None
    try:
        parts = [float(x) for x in s.strip().split(",")]
    except ValueError:
        raise ValueError(f"--loudnorm expects I[,TP[,LRA]] numbers such as -14,-1,11, got '{s}'") from None
    if len(parts) > 3:
        raise ValueError(f"--loudnorm expects at most three numbers I,TP,LRA, got '{s}'")
    d = {"I": parts[0]}
    if len(parts) > 1:
        d["TP"] = parts[1]
    if len(parts) > 2:
        d["LRA"] = parts[2]
    return d


def _parse_denoise(v):
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return v


def _build_opts(args, info: dict, need_shots: bool = True):
    V = _V()
    kw: dict[str, Any] = {}
    g = lambda n, d=None: getattr(args, n, d)  # noqa: E731
    dur = g("duration")
    if g("to") is not None:
        dur = float(args.to) - float(g("start") or 0.0)
    kw.update(mode=g("mode"), art=g("art"), stack=_user_stack(args), lut=g("lut"), intensity=g("intensity", 1.0),
              lut_size=g("lut_size", 33), lut_interp=g("interp", "tetrahedral"), lut_bits=g("lut_bits", 10),
              transfer=g("transfer", "srgb"), start=g("start"), duration=dur, speed=g("speed", 1.0) or 1.0,
              slowmo=g("slowmo"), fps=g("fps"), fps_method=g("fps_method", "fps"), blend_frames=g("blend", 0) or 0,
              scale=g("scale"), letterbox=g("letterbox"), fade_in=g("fade_in", 0.0) or 0.0, fade_out=g("fade_out", 0.0) or 0.0,
              hdr=g("hdr", "auto"), tonemap=g("tonemap", "mobius"), audio=g("audio"), frames=g("frames", "ffmpeg"),
              seed=g("seed", 0), reseed_grain=bool(g("reseed_grain")), max_width=g("max_width"),
              denoise=_parse_denoise(g("denoise")), sharpen=_parse_denoise(g("sharpen")), grain=g("grain"),
              vignette=g("vignette"), deband=bool(g("deband")), deflicker=g("deflicker") or 0,
              auto=g("auto", "off") or "off", auto_reference=g("auto_reference", "median"), auto_max_ev=g("max_ev", 1.0),
              smooth=bool(g("smooth")), shot_apply=g("shot_apply", "auto"), per_shot=bool(g("per_shot")),
              loudnorm=_parse_loudnorm(g("loudnorm")))
    if kw["audio"] == "auto":
        kw["audio"] = None
    if g("text"):
        kw["text"] = {"text": args.text}
    if g("reframe"):
        r = args.reframe
        rf: dict[str, Any] = {"mode": g("reframe_mode", "blur")}
        if "x" in r.lower() and ":" not in r:
            w, h = r.lower().split("x")
            rf.update(width=int(w), height=int(h))
        else:
            rf["aspect"] = r
        kw["reframe"] = rf
    if g("stabilize") is not None:
        kw["stabilize"] = {"shakiness": int(args.stabilize), **({"smoothing": args.smoothing} if g("smoothing") else {}),
                           **({"tripod": True} if g("tripod") else {})}
    if g("lens_k1") is not None or g("lens_k2") is not None:
        kw["lens"] = {"k1": g("lens_k1") or 0.0, "k2": g("lens_k2") or 0.0}
    return V.GradeOptions(**kw)


def _resolve_shots(args, src: str, info: dict, opts):
    """Fill opts.shots from --shots / sidecar / detection; returns (opts, doc, method)."""
    V = _V()
    import dataclasses

    if not (opts.per_shot or getattr(args, "shots", None)):
        return opts, None, None
    doc = None
    method = None
    if getattr(args, "shots", None):
        p = Path(args.shots)
        doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        if doc is None:
            raise FileNotFoundError(f"shot list not found: {args.shots}")
        shots = doc["shots"]
        method = "file"
    else:
        doc = V.shots.load_shots(src)
        if doc and doc.get("shots"):
            shots = doc["shots"]
            method = "sidecar"
        else:
            shots, method = V.shots.detect_shots(src, info, getattr(args, "detect_method", "auto"),
                                                 getattr(args, "threshold", None), getattr(args, "min_shot", 0.5))
            doc = V.shots.new_doc(src, info, shots, opts.mode, opts.art, opts.stack)
    return dataclasses.replace(opts, shots=shots, per_shot=True), doc, method


def _default_out(args, src: str, suffix: str, preset: dict | None) -> str:
    if args.output:
        return args.output
    V = _V()
    ext = V.encode.CONTAINER_EXT.get(V.encode.preset_options(preset).get("container", "mp4"), ".mp4")
    return str(V.default_output(src, suffix, ext))


def _run(plan, args):
    V = _V()
    res = V.run_plan(plan, dry_run=bool(getattr(args, "dry_run", False)), progress=_progress(args),
                     keep_temp=bool(getattr(args, "keep_temp", False)))
    return res


def _finish(res: dict, plan, opts=None) -> dict:
    res.setdefault("notes", list(plan.notes))
    if plan.warnings:
        res["warnings"] = list(plan.warnings)
    if plan.state.get("auto"):
        res["auto_match"] = plan.state["auto"]
    return res


# ----------------------------------------------------------------------------- commands
def cmd_probe(args):
    V = _V()
    info = V.probe(args.input)
    if args.caps:
        info = {**info, "ffmpeg": V.capabilities().describe(), "ffmpeg_path": V.find_ffmpeg()}
    return info


def _grade_like(args, suffix: str, default_preset: str | None = None, name: str = "grade"):
    V = _V()
    src = args.input
    info = V.probe(src)
    opts = _build_opts(args, info)
    opts, doc, method = _resolve_shots(args, src, info, opts)
    preset = V.load_video_preset(getattr(args, "preset", None) or default_preset)
    out = _default_out(args, src, suffix, preset)
    plan = V.build_plan(src, out, info, opts, preset, hw=getattr(args, "hw", "auto"), overwrite=not args.no_overwrite,
                        encoder=getattr(args, "encoder", None), name=name)
    return V, src, info, opts, doc, method, plan


def cmd_grade(args):
    V, src, info, opts, doc, method, plan = _grade_like(args, "graded")
    res = _run(plan, args)
    res = _finish(res, plan, opts)
    if method:
        res["shot_detection"] = method
    if getattr(args, "save_sidecar", False) and doc is not None and not args.dry_run:
        auto = plan.state.get("auto")
        if auto and len(auto) == len(doc["shots"]):
            for s, a in zip(doc["shots"], auto):
                s["auto"] = a
        res["sidecar"] = str(V.shots.save_shots(src, doc))
    return res


def cmd_export(args):
    V, src, info, opts, doc, method, plan = _grade_like(args, args.preset.split(":")[0] if args.preset else "export", "youtube_1080p", "export")
    return _finish(_run(plan, args), plan, opts)


def cmd_stabilize(args):
    V = _V()
    info = V.probe(args.input)
    st = {"shakiness": args.shakiness, "smoothing": args.smoothing, "optzoom": args.optzoom, "crop": args.crop,
          "tripod": args.tripod, "sharpen": not args.no_sharpen}
    dur = args.duration if args.to is None else args.to - (args.start or 0)
    opts = V.GradeOptions(stabilize=st, start=args.start, duration=dur, audio=None if args.audio in (None, "auto") else args.audio,
                          loudnorm=_parse_loudnorm(args.loudnorm))
    preset = V.load_video_preset(args.preset)
    out = _default_out(args, args.input, "stab", preset)
    plan = V.build_plan(args.input, out, info, opts, preset, hw=args.hw, overwrite=not args.no_overwrite,
                        encoder=args.encoder, name="stabilize")
    return _finish(_run(plan, args), plan, opts)


def cmd_denoise(args):
    V = _V()
    info = V.probe(args.input)
    dur = args.duration if args.to is None else args.to - (args.start or 0)
    opts = V.GradeOptions(denoise=_parse_denoise(args.level), start=args.start, duration=dur,
                          audio=None if args.audio in (None, "auto") else args.audio, loudnorm=_parse_loudnorm(args.loudnorm))
    preset = V.load_video_preset(args.preset)
    out = _default_out(args, args.input, "denoised", preset)
    plan = V.build_plan(args.input, out, info, opts, preset, hw=args.hw, overwrite=not args.no_overwrite,
                        encoder=args.encoder, name="denoise")
    return _finish(_run(plan, args), plan, opts)


def cmd_stills(args):
    V = _V()
    info = V.probe(args.input)
    dur = args.duration if args.to is None else args.to - (args.start or 0)
    opts = V.GradeOptions(start=args.start, duration=dur, hdr=args.hdr)
    shots = None
    if args.per_shot:
        doc = V.shots.load_shots(args.input)
        if doc:
            shots = V.shots.fill_shots(doc["shots"], info.get("duration") or 0.0)
        else:
            shots, _ = V.shots.detect_shots(args.input, info)
        shots = V.shots.clip_shots(shots, args.start, dur)
    out_dir = args.output or str(Path(args.input).with_suffix("")) + "_stills"
    plan = V.frames.build_stills_plan(args.input, info, out_dir, times=args.at, every=args.every, count=args.count,
                                      representative=args.representative, fmt=args.format, bits=args.bits,
                                      best_window=args.best, opts=opts, shots=shots)
    if not args.dry_run:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    res = _run(plan, args)
    res["output_dir"] = out_dir
    if plan.state.get("best_times"):
        res["best_times"] = plan.state["best_times"]
    return _finish(res, plan)


def cmd_timelapse(args):
    V = _V()
    preset = V.load_video_preset(args.preset)
    plan = V.frames.build_timelapse_plan(
        args.inputs, args.output, fps=args.fps, preset=preset, deflicker=args.deflicker,
        kenburns=args.kenburns, size=args.size, blend=args.blend, develop=args.develop, mode=args.mode, art=args.art,
        develop_long_edge=args.long_edge, hw=args.hw, encoder=args.encoder, overwrite=not args.no_overwrite,
        trails=({"decay": args.trails} if args.trails is not None else None),
        name="startrails-video" if args.trails is not None else "timelapse")
    return _finish(_run(plan, args), plan)


def cmd_startrails(args):
    V = _V()
    plan = V.frames.build_startrails_image_plan(args.inputs, args.output, decay=args.decay, develop=args.develop)
    return _finish(_run(plan, args), plan)


def cmd_shots(args):
    V = _V()
    info = V.probe(args.input)
    doc = None if args.redetect else V.shots.load_shots(args.input)
    method = "sidecar"
    if doc is None:
        shots, method = V.shots.detect_shots(args.input, info, args.method, args.threshold, args.min_len)
        doc = V.shots.new_doc(args.input, info, shots, args.default_mode, args.default_art)
    else:
        if args.default_mode:
            doc["default"]["mode"] = args.default_mode
        if args.default_art:
            doc["default"]["art"] = args.default_art
    for a in args.assign or []:
        idx, _, rest = a.partition(":")
        k, _, v = rest.partition("=")
        if not (idx.isdigit() and k in ("mode", "art") and v):
            raise ValueError(f"--assign expects N:mode=X or N:art=Y with N the shot number as listed (1 = first shot), got '{a}'")
        n = int(idx)
        if not 1 <= n <= len(doc["shots"]):
            raise ValueError(f"--assign {a}: there is no shot {n}; shots are numbered 1..{len(doc['shots'])} as listed "
                             "by `photo video shots INPUT`")
        doc["shots"][n - 1][k] = None if v in ("none", "null", "-") else v
    for i, sh in enumerate(doc["shots"]):                      # shot numbers are 1-based everywhere (listing, --assign, sidecar)
        sh["index"] = i + 1
    res: dict[str, Any] = {"detection": method, "count": len(doc["shots"]), "shots": doc["shots"]}
    if args.analyze:

        shots = V.shots.fill_shots(doc["shots"], info.get("duration") or 0.0)
        opts = V.GradeOptions(auto=args.analyze, auto_reference=args.auto_reference)
        plan = V.Plan("shots-analyze")
        norm = V.graph.normalise_filters(info, opts, V.capabilities(), V.graph.resolve_hdr_mode(info, opts), [])
        plan.add(V.Step("python", "auto-match", fn=V.shots.make_auto_step_fn(args.input, info, shots, opts, norm)))
        if args.dry_run:
            res["plan"] = plan.to_json()
        else:
            V.execute(plan, progress=_progress(args))
        # execute() cleans up; state survives on the plan object
        auto = plan.state.get("auto")
        if auto:
            for s, a in zip(doc["shots"], auto):
                s["auto"] = a
            res["auto_match"] = auto
    if args.save and not args.dry_run:
        res["sidecar"] = str(V.shots.save_shots(args.input, doc))
    res["sidecar_path"] = str(V.shots.shots_path(args.input))
    return res


def cmd_lut(args):
    V = _V()
    stack, info_ = V.stack.resolve_video_stack(args.mode, args.art, _user_stack(args))
    lut = V.bake_lut(stack, args.lut_size, transfer=args.transfer, intensity=args.intensity, title=f"photostudio {args.mode or ''} {args.art or ''}".strip())
    out = args.output or "look.cube"
    res: dict[str, Any] = {"output": out, "size": lut.size, "transfer": args.transfer, "baked_ops": lut.meta["baked"],
                           "spatial_ops_for_ffmpeg": lut.meta["spatial"], "not_in_lut": lut.meta["unsupported"],
                           "max_deviation_from_identity": round(lut.max_error_vs_identity(), 5), "notes": lut.meta["notes"]}
    if not args.dry_run:
        V.lut.write_cube_preferring_core(out, lut)
    if args.input:
        res["ffmpeg_command"] = [V.ff.ffmpeg_bin_for_plan() if hasattr(V, "ff") else "ffmpeg", "-i", args.input, "-vf",
                                 f"scale=in_range=tv:in_color_matrix=bt709,format=gbrp10le,lut3d=file={out}:interp={args.interp},"
                                 "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-c:a", "copy", "out.mp4"]
    res["dry_run"] = bool(args.dry_run)
    return res


def cmd_preview(args):
    V = _V()
    src = args.input
    info = V.probe(src)
    opts = _build_opts(args, info)
    if opts.duration is None and opts.start is None and (info.get("duration") or 0) > 10 and args.kind != "contact":
        import dataclasses

        opts = dataclasses.replace(opts, duration=10.0)
    opts, doc, method = _resolve_shots(args, src, info, opts)
    if args.kind == "contact":
        out = args.output or str(Path(src).with_name(Path(src).stem + "_contact.jpg"))
        plan = V.frames.build_contact_sheet_plan(src, info, out, cols=args.cols, rows=args.rows, labels=not args.no_labels, opts=opts)
        return _finish(_run(plan, args), plan)
    name = {"video": "preview_fast", "gif": "gif", "webp": "gif:webp"}[args.kind]
    preset = V.load_video_preset(args.preset or name)
    out = _default_out(args, src, "preview", preset)
    plan = V.build_plan(src, out, info, opts, preset, hw=args.hw, overwrite=not args.no_overwrite, encoder=args.encoder,
                        name="preview")
    return _finish(_run(plan, args), plan)
