"""Filtergraph construction (pure string building — no ffmpeg needed).

Filter order (documented because it matters for quality):

  colour/range normalisation (HDR tone-map, 601->709, full->limited)
  -> denoise (on the camera's YUV, before anything amplifies noise) -> deflicker
  -> retime (speed / slow-mo / fps)             [cheaper to drop frames before the expensive grade]
  -> RGB segment: scale(YUV->GBR, explicit BT.709) -> lut3d (tetrahedral, 10-bit planar RGB) -> back to YUV
  -> deband -> scale / reframe -> sharpen (output sharpening after resize) -> vignette -> grain
  -> letterbox -> fades / text -> final pix_fmt (+ setsar)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .ff import Caps, VideoError, esc_drawtext, esc_filter_value, fmt_num
from .options import GradeOptions, parse_aspect

SCALE_FLAGS = "lanczos+accurate_rnd+full_chroma_int"

DENOISE_LEVELS = {
    "light": "hqdn3d=2:1.5:3:3",
    "medium": "hqdn3d=4:3:6:4.5",
    "strong": "hqdn3d=7:6:9:8",
    "chroma": "hqdn3d=0:5:0:6",
    "temporal": "atadenoise=0a=0.02:0b=0.04:1a=0.02:1b=0.04:2a=0.02:2b=0.04:s=9",
    "nlmeans": "nlmeans=s=3:p=7:pc=5:r=15",
    "nlmeans_strong": "nlmeans=s=6:p=7:pc=5:r=15",
}
SHARPEN_LEVELS = {"light": 0.3, "medium": 0.5, "strong": 0.7}


# ----------------------------------------------------------------------------- graph container
class Graph:
    """Linear filter list with optional multi-pad (`complex`) stages.  Compile to ``-vf`` or ``-filter_complex``."""

    def __init__(self) -> None:
        self.parts: list[tuple[str, str]] = []
        self._u = 0

    def f(self, *filters: str) -> "Graph":
        for s in filters:
            if s:
                self.parts.append(("f", s))
        return self

    def c(self, template: str) -> "Graph":
        """Template with ``[IN]``, ``[OUT]`` and ``{u}`` (unique suffix) tokens."""
        self.parts.append(("c", template))
        return self

    def has_complex(self) -> bool:
        return any(k == "c" for k, _ in self.parts)

    def compile(self, in_label: str = "0:v:0") -> tuple[str, str, str | None]:
        """-> (flag, graph, out_label): flag is '-vf' (out_label None) or '-filter_complex'."""
        if not self.has_complex():
            return "-vf", ",".join(s for _, s in self.parts) or "null", None
        chains: list[str] = []
        cur = f"[{in_label}]"
        pending: list[str] = []
        n = 0

        def flush(nxt: str) -> str:
            nonlocal cur, pending
            if pending:
                chains.append(f"{cur}{','.join(pending)}{nxt}")
                cur = nxt
                pending = []
            return cur

        for kind, s in self.parts:
            if kind == "f":
                pending.append(s)
            else:
                n += 1
                if pending:
                    flush(f"[g{n}a]")
                out = f"[g{n}b]"
                chains.append(s.replace("[IN]", cur).replace("[OUT]", out).replace("{u}", str(n)))
                cur = out
        if pending:
            flush("[vout]")
        else:
            chains[-1] = chains[-1].rsplit("[", 1)[0] + "[vout]"
            cur = "[vout]"
        return "-filter_complex", ";".join(chains), "[vout]"


@dataclass
class LutRef:
    path: str
    start: float | None = None      # seconds on the *grade-point timeline* (None = whole clip)
    end: float | None = None
    label: str = ""


@dataclass
class GraphResult:
    flag: str
    graph: str
    out_label: str | None
    pix_fmt: str
    hdr_mode: str                    # sdr | tonemapped | passthrough
    out_size: tuple[int, int]
    out_fps: float | None
    out_duration: float | None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    color_tags: dict = field(default_factory=dict)
    audio_atempo: str | None = None
    pre_graph: str | None = None         # python-frames path: the decode-side -vf graph

    def args(self) -> list[str]:
        out = [self.flag, self.graph]
        if self.out_label:
            out += ["-map", self.out_label]
        return out


# ----------------------------------------------------------------------------- colour normalisation
_ZMAT = {"bt709": "709", "bt470bg": "470bg", "smpte170m": "170m", "bt2020nc": "2020_ncl", "bt2020c": "2020_cl"}
_ZPRIM = {"bt709": "709", "bt470bg": "470bg", "smpte170m": "170m", "bt2020": "2020", "smpte240m": "240m"}
_ZTRC = {"bt709": "709", "smpte170m": "601", "bt470bg": "601", "smpte2084": "smpte2084", "arib-std-b67": "arib-std-b67",
         "iec61966-2-1": "iec61966-2-1", "bt2020-10": "2020_10", "bt2020-12": "2020_12", "linear": "linear"}


def effective_matrix(info: dict) -> str:
    """Colour matrix to assume for decoding: the tag if present, otherwise BT.709 for HD / BT.601 for SD."""
    m = info.get("color_space") or "unknown"
    if m not in ("unknown", "unspecified", "reserved", ""):
        return m
    return "bt709" if (info.get("height") or 1080) >= 720 else "smpte170m"


def resolve_hdr_mode(info: dict, opts: GradeOptions, preset_hdr: str | None = None) -> str:
    if not info.get("is_hdr"):
        return "sdr"
    want = opts.hdr
    if want == "auto":
        want = preset_hdr or "tonemap"
    return "passthrough" if want == "passthrough" else "tonemapped"


def normalise_filters(info: dict, opts: GradeOptions, caps: Caps, hdr_mode: str, notes: list[str]) -> list[str]:
    """Filters that bring the source to Rec.709 / limited-range YUV 4:2:0 SDR."""
    if hdr_mode == "passthrough":
        return []
    out: list[str] = []
    if hdr_mode == "tonemapped":
        if not (caps.has_filter("zscale") and caps.has_filter("tonemap")):
            raise VideoError("HDR -> SDR tone mapping needs an ffmpeg with zscale (libzimg) and tonemap; "
                             "use --hdr passthrough or install a full ffmpeg build")
        tin = _ZTRC.get(info.get("color_trc") or "", "smpte2084")
        mat = _ZMAT.get(info.get("color_space") or "", "2020_ncl")
        rin = "limited" if info.get("color_range") != "pc" else "full"
        algo = opts.tonemap
        if algo not in ("none", "linear", "gamma", "clip", "reinhard", "hable", "mobius"):
            raise VideoError(f"unknown tonemap algorithm '{algo}'")
        out += [f"zscale=tin={tin}:min={mat}:pin=2020:rin={rin}:p=2020:t=linear:npl=100", "format=gbrpf32le",
                "zscale=p=709", f"tonemap=tonemap={algo}:desat=0", "zscale=t=709:m=709:r=tv", "format=yuv420p"]
        notes.append(f"HDR ({info.get('color_trc')}) tone-mapped to SDR Rec.709 with {algo}")
        return out
    prim, trc = info.get("color_primaries") or "unknown", info.get("color_trc") or "unknown"
    mat = effective_matrix(info)
    is709 = mat == "bt709" and prim in ("bt709", "unknown", "unspecified") and trc in ("bt709", "unknown", "unspecified", "iec61966-2-1")
    rng = info.get("color_range") or "unknown"
    if not is709:
        if caps.has_filter("zscale"):
            zp = _ZPRIM.get(prim) or ("709" if mat == "bt709" else "170m")
            zt = _ZTRC.get(trc) or "709"
            out.append(f"zscale=min={_ZMAT.get(mat, '170m')}:pin={zp}:tin={zt}:rin={'full' if rng == 'pc' else 'limited'}"
                       f":m=709:p=709:t=709:r=tv")
        else:
            iall = {"smpte170m": "bt601-6-525", "bt470bg": "bt601-6-625", "bt2020nc": "bt2020"}.get(mat, "bt601-6-625")
            out.append(f"colorspace=all=bt709:iall={iall}:range=tv")
        notes.append(f"converted {mat}/{prim} source to Rec.709")
    elif rng == "pc":
        out.append("scale=in_range=pc:out_range=tv:flags=accurate_rnd+full_chroma_int")
        notes.append("full-range source squeezed to limited range")
    return out


# ----------------------------------------------------------------------------- individual filters
def denoise_filter(spec: Any, caps: Caps, warnings: list[str]) -> str | None:
    if spec in (None, False, "off", "none", 0):
        return None
    if isinstance(spec, dict):
        method = spec.get("method", "hqdn3d")
        if method == "hqdn3d":
            return "hqdn3d=" + ":".join(fmt_num(float(spec.get(k, d))) for k, d in
                                       (("luma_spatial", 4), ("chroma_spatial", 3), ("luma_tmp", 6), ("chroma_tmp", 4.5)))
        if method == "nlmeans":
            return f"nlmeans=s={fmt_num(float(spec.get('s', 3)))}:p={int(spec.get('p', 7))}:r={int(spec.get('r', 15))}"
        if method == "atadenoise":
            return f"atadenoise=s={int(spec.get('s', 9))}"
        raise VideoError(f"unknown denoise method '{method}'")
    if isinstance(spec, str):
        s = spec.strip().lower()
        if s in DENOISE_LEVELS:
            f = DENOISE_LEVELS[s]
            name = f.split("=")[0]
        elif re.fullmatch(r"[\d.]+(:[\d.]+){1,3}", s):
            f = f"hqdn3d={s}"
            name = "hqdn3d"
        else:
            try:
                return denoise_filter(float(s), caps, warnings)
            except ValueError:
                raise VideoError(f"unknown denoise level '{spec}' (light|medium|strong|chroma|temporal|nlmeans or 0..1)") from None
    else:
        st = max(0.0, min(1.0, float(spec)))
        if st <= 0:
            return None
        ls = 1.0 + 7.0 * st
        f = f"hqdn3d={fmt_num(ls, 2)}:{fmt_num(ls * 0.75, 2)}:{fmt_num(ls * 1.5, 2)}:{fmt_num(ls * 1.125, 2)}"
        name = "hqdn3d"
    if not caps.has_filter(name):
        warnings.append(f"ffmpeg has no '{name}' filter; denoise skipped")
        return None
    return f


def sharpen_filter(spec: Any, caps: Caps, warnings: list[str]) -> str | None:
    if spec in (None, False, "off", "none", 0):
        return None
    method, amount = "cas", None
    if isinstance(spec, dict):
        method = spec.get("method", "cas")
        amount = spec.get("amount")
    elif isinstance(spec, str):
        amount = SHARPEN_LEVELS.get(spec.lower())
        if amount is None:
            try:
                amount = float(spec)
            except ValueError:
                raise VideoError(f"unknown sharpen level '{spec}' (light|medium|strong or 0..1)") from None
    else:
        amount = float(spec)
    amount = 0.5 if amount is None else max(0.0, min(1.0, float(amount)))
    if amount <= 0:
        return None
    if method == "cas" and caps.has_filter("cas"):
        return f"cas=strength={fmt_num(amount, 3)}"
    if method == "cas":
        warnings.append("ffmpeg has no 'cas' filter; used unsharp")
    return f"unsharp=5:5:{fmt_num(0.4 + amount * 1.2, 3)}:3:3:0"


def grain_filter(amount: float | None) -> str | None:
    if not amount or amount <= 0:
        return None
    return f"noise=alls={fmt_num(3 + 27 * max(0.0, min(1.0, amount)), 2)}:allf=t+u"


def vignette_filter(amount: float | None) -> str | None:
    if not amount or amount <= 0:
        return None
    ang = 0.12 + 0.38 * max(0.0, min(1.0, amount))        # fraction of PI/2
    return f"vignette=angle={fmt_num(ang, 4)}*PI/2:mode=forward"


def clarity_filter(amount: float | None) -> str | None:
    """Local contrast on video = a large-radius unsharp mask on luma only."""
    if not amount or amount <= 0:
        return None
    return f"unsharp=23:23:{fmt_num(0.3 + 0.9 * min(1.0, amount), 3)}:5:5:0"


def lut3d_filter(path: str, interp: str = "tetrahedral", enable: str | None = None) -> str:
    f = f"lut3d=file={esc_filter_value(path)}:interp={interp}"
    if enable:
        f += f":enable='{enable}'"
    return f


def atempo_chain(factor: float) -> str:
    """atempo only accepts 0.5..100 per instance; chain for smaller factors."""
    if factor <= 0:
        raise VideoError("speed must be > 0")
    parts: list[str] = []
    f = factor
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    while f > 100.0:
        parts.append("atempo=100")
        f /= 100.0
    parts.append(f"atempo={fmt_num(f, 6)}")
    return ",".join(parts)


def spatial_overrides(spatial_entries: list[dict]) -> dict:
    """Map spatial stack entries (op names known to the pipeline) to ffmpeg option values."""
    out: dict[str, Any] = {}
    for e in spatial_entries:
        n, p = e.get("op"), (e.get("params") or {})
        if n == "denoise":
            lum = p.get("luma", p.get("strength", p.get("amount")))
            out["denoise"] = float(lum) if isinstance(lum, (int, float)) and lum <= 1.0 else (float(lum) / 100.0 if lum else "medium")
        elif n in ("sharpen", "capture_sharpen"):
            a = p.get("amount", 0.5)
            out["sharpen"] = min(1.0, float(a) / (100.0 if float(a) > 1.5 else 1.0))
        elif n == "grain":
            out["grain"] = min(1.0, float(p.get("amount", 0.3)))
        elif n == "vignette":
            out["vignette"] = min(1.0, abs(float(p.get("amount", p.get("strength", 0.3)))))
        elif n in ("local_contrast", "clarity", "texture"):
            out["clarity"] = min(1.0, abs(float(p.get("amount", 0.3))))
        elif n in ("halation", "bloom", "orton"):
            out["bloom"] = min(1.0, float(p.get("amount", p.get("strength", 0.3))))
        elif n == "blur":
            out["blur"] = float(p.get("radius", 2.0))
    return out


def bloom_complex(amount: float) -> str:
    a = max(0.0, min(1.0, amount))
    return ("[IN]split[bl{u}a][bl{u}b];[bl{u}b]gblur=sigma=18,eq=brightness=-0.15[bl{u}c];"
            f"[bl{{u}}a][bl{{u}}c]blend=all_mode=screen:all_opacity={fmt_num(0.15 + 0.5 * a, 3)}[OUT]")


# ----------------------------------------------------------------------------- reframe / letterbox
def reframe_stage(g: Graph, cur_w: int, cur_h: int, opts: GradeOptions, size: tuple[int, int] | None,
                  warnings: list[str]) -> tuple[int, int]:
    """Append scale / reframe filters; returns the resulting (w, h)."""
    rf = opts.reframe
    if rf:
        tw, th = rf.get("width"), rf.get("height")
        asp = parse_aspect(rf.get("aspect"))
        if not (tw and th):
            if not asp:
                raise VideoError("reframe needs width+height or an aspect")
            if size:
                th = size[1]
            else:
                th = min(cur_h, 1920) if asp < 1 else cur_h
            tw = int(round(th * asp / 2.0)) * 2
        tw, th = int(tw), int(th)
        mode = rf.get("mode", "crop")
        x = fmt_num(float(rf.get("x", 0.5)), 3)
        y = fmt_num(float(rf.get("y", 0.5)), 3)
        if mode == "crop":
            g.f(f"scale={tw}:{th}:force_original_aspect_ratio=increase:flags={SCALE_FLAGS}",
                f"crop={tw}:{th}:(iw-{tw})*{x}:(ih-{th})*{y}")
        elif mode == "pad":
            g.f(f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags={SCALE_FLAGS}",
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black")
        elif mode == "blur":
            sig = fmt_num(float(rf.get("blur", 40)), 2)
            g.c(f"[IN]split[rb{{u}}a][rb{{u}}b];"
                f"[rb{{u}}a]scale={tw}:{th}:force_original_aspect_ratio=increase:flags=bilinear,crop={tw}:{th},"
                f"gblur=sigma={sig},eq=brightness=-0.1:saturation=0.9[rb{{u}}c];"
                f"[rb{{u}}b]scale={tw}:{th}:force_original_aspect_ratio=decrease:flags={SCALE_FLAGS}[rb{{u}}d];"
                f"[rb{{u}}c][rb{{u}}d]overlay=(W-w)/2:(H-h)/2[OUT]")
        else:
            raise VideoError(f"unknown reframe mode '{mode}' (crop|pad|blur)")
        g.f("setsar=1")
        return tw, th
    if size:
        w, h = size
        if (w, h) != (cur_w, cur_h):
            g.f(f"scale={w}:{h}:flags={SCALE_FLAGS}")
        return w, h
    return cur_w, cur_h


def letterbox_stage(g: Graph, w: int, h: int, aspect: float, mode: str) -> None:
    """Matte: crop to the wide aspect then pad back to the container, i.e. black bars over the picture."""
    ch = int(round(w / aspect / 2.0)) * 2
    if ch >= h:
        return
    off = (h - ch) // 2
    if mode == "crop":
        g.f(f"crop={w}:{ch}:0:{off}")
    else:
        g.f(f"crop={w}:{ch}:0:{off}", f"pad={w}:{h}:0:{off}:black")


# ----------------------------------------------------------------------------- the main builder
def build_graph(info: dict, opts: GradeOptions, caps: Caps, *, luts: list[LutRef] | None = None,
                final_pix_fmt: str = "yuv420p", spatial: dict | None = None, size: tuple[int, int] | None = None,
                preset_hdr: str | None = None, time_offset: float = 0.0, out_fps_hint: float | None = None,
                shot_apply: str = "timeline", stabilize_filters: list[str] | None = None,
                in_label: str = "0:v:0", split: bool = False) -> GraphResult:
    notes: list[str] = []
    warnings: list[str] = []
    luts = list(luts or [])
    sp = dict(spatial or {})
    in_w, in_h = int(info.get("display_width") or info["width"]), int(info.get("display_height") or info["height"])
    in_fps = float(info.get("fps") or 25.0)
    g = Graph()

    # explicit CLI values win over values derived from the look's spatial ops
    den = opts.denoise if opts.denoise is not None else sp.get("denoise")
    shp = opts.sharpen if opts.sharpen is not None else sp.get("sharpen")
    grain = opts.grain if opts.grain is not None else sp.get("grain")
    vig = opts.vignette if opts.vignette is not None else sp.get("vignette")

    hdr_mode = resolve_hdr_mode(info, opts, preset_hdr)
    g.f(*normalise_filters(info, opts, caps, hdr_mode, notes))

    # ---- temporal clean-up (before retime, on the camera's YUV) ----
    if stabilize_filters:
        g.f(*stabilize_filters)
    if opts.lens:
        lf = lens_filter(opts.lens, caps, warnings)
        if lf:
            g.f(lf)
    d = denoise_filter(den, caps, warnings)
    if d:
        g.f(d)
    if opts.deflicker and opts.deflicker > 1:
        if caps.has_filter("deflicker"):
            g.f(f"deflicker=size={int(opts.deflicker)}:mode=pm")
        else:
            warnings.append("ffmpeg has no 'deflicker' filter; skipped")

    # ---- retime ----
    slow = float(opts.slowmo) if opts.slowmo else 1.0
    speed = float(opts.speed or 1.0)
    if speed <= 0 or slow <= 0:
        raise VideoError("--speed and --slowmo must be > 0")
    time_factor = slow / speed
    out_fps = float(opts.fps) if opts.fps else in_fps / slow
    if opts.blend_frames and opts.blend_frames > 1:
        g.f(f"tmix=frames={int(opts.blend_frames)}")
    if slow != 1.0:
        g.f(f"setpts={fmt_num(slow, 6)}*PTS")
        notes.append(f"slow-motion conform x{fmt_num(slow, 3)}: every source frame kept, {in_fps / slow:.3f} fps")
    if speed != 1.0:
        g.f(f"setpts=PTS/{fmt_num(speed, 6)}")
    if opts.fps and opts.fps_method == "mci" and caps.has_filter("minterpolate"):
        g.f(f"minterpolate=fps={fmt_num(out_fps, 6)}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
        notes.append("motion-compensated frame interpolation (slow)")
    elif opts.fps and opts.fps_method == "blend":
        g.f(f"framerate=fps={fmt_num(out_fps, 6)}")
    elif opts.fps or speed != 1.0:
        if opts.fps and opts.fps_method == "mci":
            warnings.append("no minterpolate in this ffmpeg; used the fps filter")
        g.f(f"fps={fmt_num(out_fps, 6)}")
    # ---- grade ----
    do_grade = bool(luts) or bool(opts.lut)
    if hdr_mode == "passthrough" and do_grade:
        warnings.append("HDR passthrough keeps the PQ signal: Rec.709 look/LUT skipped (use --hdr tonemap to grade)")
        do_grade = False
    post = g
    pre_graph: str | None = None
    if split:
        # python-frames path: geometry first (numpy works at output size), then RGB24 out / RGB24 in
        out_w, out_h = reframe_stage(g, in_w, in_h, opts, size, warnings)
        g.f(f"scale=in_range=tv:in_color_matrix=bt709:flags={SCALE_FLAGS}", "format=rgb24")
        pre_flag, pre_graph, _ = g.compile(in_label)
        post = Graph()
        post.f(f"scale=out_color_matrix=bt709:out_range=tv:flags={SCALE_FLAGS}", "format=yuv444p10le")
        if opts.deband and caps.has_filter("deband"):
            post.f("deband=1thr=0.02:2thr=0.02:3thr=0.02:4thr=0.02:range=16:blur=1")
    else:
        if do_grade:
            _grade_segment(g, info, opts, luts, shot_apply, time_offset, time_factor, in_fps, hdr_mode, notes)
        if opts.deband:
            if caps.has_filter("deband"):
                g.f("deband=1thr=0.02:2thr=0.02:3thr=0.02:4thr=0.02:range=16:blur=1")
            else:
                warnings.append("no deband filter; skipped")
        out_w, out_h = reframe_stage(g, in_w, in_h, opts, size, warnings)
    # ---- detail / look effects ----
    sf = sharpen_filter(shp, caps, warnings)
    if sf:
        post.f(sf)
    cl = clarity_filter(sp.get("clarity"))
    if cl:
        post.f(cl)
    if sp.get("bloom"):
        post.c(bloom_complex(float(sp["bloom"])))
    if sp.get("blur"):
        post.f(f"gblur=sigma={fmt_num(float(sp['blur']), 2)}")
    vf_ = vignette_filter(vig)
    if vf_:
        post.f(vf_)
    gf = grain_filter(grain)
    if gf:
        post.f(gf)
    if opts.letterbox:
        letterbox_stage(post, out_w, out_h, float(opts.letterbox), opts.letterbox_mode)
    # ---- fades / text ----
    dur_in = info.get("duration") or 0.0
    if opts.duration:
        dur_in = min(dur_in, opts.duration) if dur_in else opts.duration
    elif opts.start and dur_in:
        dur_in = max(0.0, dur_in - opts.start)
    out_duration = (dur_in * time_factor) if dur_in else None
    if opts.fade_in and opts.fade_in > 0:
        post.f(f"fade=t=in:st=0:d={fmt_num(opts.fade_in, 3)}")
    if opts.fade_out and opts.fade_out > 0:
        if out_duration:
            post.f(f"fade=t=out:st={fmt_num(max(0.0, out_duration - opts.fade_out), 3)}:d={fmt_num(opts.fade_out, 3)}")
        else:
            warnings.append("fade-out needs a known duration; skipped")
    if opts.text:
        tf = text_filter(opts.text, caps, warnings)
        if tf:
            post.f(tf)
    # ---- final format ----
    tags = color_tags_for(hdr_mode, info)
    post.f("setsar=1", f"format={final_pix_fmt}")
    flag, graph, label = post.compile("0:v:0" if split else in_label)
    res = GraphResult(flag, graph, label, final_pix_fmt, hdr_mode, (out_w, out_h), out_fps, out_duration, notes,
                      warnings, tags)
    res.pre_graph = pre_graph
    if abs(time_factor - 1.0) > 1e-9:
        res.audio_atempo = atempo_chain(1.0 / time_factor)
    return res


def _grade_segment(g: Graph, info: dict, opts: GradeOptions, luts: list[LutRef], shot_apply: str,
                   time_offset: float, time_factor: float, fps: float, hdr_mode: str, notes: list[str]) -> None:
    bits = int(opts.lut_bits)
    if bits not in (8, 10, 12, 16):
        raise VideoError("lut_bits must be 8, 10, 12 or 16")
    work = {8: ("gbrp", "yuv420p"), 10: ("gbrp10le", "yuv420p10le"), 12: ("gbrp12le", "yuv420p12le"),
            16: ("gbrp16le", "yuv420p16le")}[bits]
    to_rgb = [f"scale=in_range=tv:in_color_matrix=bt709:flags={SCALE_FLAGS}", f"format={work[0]}"]
    to_yuv = [f"scale=out_color_matrix=bt709:out_range=tv:flags={SCALE_FLAGS}", f"format={work[1]}"]
    interp = opts.lut_interp

    def tmap(t: float) -> float:
        return max(0.0, (t - time_offset) * time_factor)

    half = 0.5 / max(fps, 1.0) * time_factor
    per_shot = [lr for lr in luts if lr.start is not None]
    whole = [lr for lr in luts if lr.start is None]
    if per_shot and shot_apply == "concat":
        ordered = sorted(per_shot, key=lambda x: x.start)
        n = len(ordered)
        lab_in = [f"[sh{{u}}_{i}]" for i in range(n)]
        lab_out = [f"[so{{u}}_{i}]" for i in range(n)]
        parts = ["[IN]split=" + str(n) + "".join(lab_in)]
        for i, lr in enumerate(ordered):
            st = tmap(lr.start) if i else None
            en = tmap(lr.end) if (lr.end is not None and i < n - 1) else None
            bits_ = []
            if st:
                bits_.append(f"start={fmt_num(st - half, 4)}")
            if en:
                bits_.append(f"end={fmt_num(en - half, 4)}")
            trim = ("trim=" + ":".join(bits_)) if bits_ else "null"
            chain = [trim, "setpts=PTS-STARTPTS", *to_rgb, lut3d_filter(lr.path, interp)]
            chain += [lut3d_filter(w.path, interp) for w in whole]
            chain += [*to_yuv]
            parts.append(f"{lab_in[i]}{','.join(chain)}{lab_out[i]}")
        parts.append("".join(lab_out) + f"concat=n={n}:v=1:a=0[OUT]")
        g.c(";".join(parts))
        notes.append(f"per-shot grade: {n} LUTs, split/trim/concat in one pass")
        return
    g.f(*to_rgb)
    for lr in luts:
        if lr.start is None:
            g.f(lut3d_filter(lr.path, interp))
        else:
            a = tmap(lr.start) - half if lr.start > 0 else None
            b = tmap(lr.end) - half if lr.end is not None else None
            cond = []
            if a is not None:
                cond.append(f"gte(t,{fmt_num(a, 4)})")
            if b is not None:
                cond.append(f"lt(t,{fmt_num(b, 4)})")
            g.f(lut3d_filter(lr.path, interp, "*".join(cond) if cond else None))
    if opts.lut:
        g.f(lut3d_filter(opts.lut, interp))
    g.f(*to_yuv)
    if per_shot:
        notes.append(f"per-shot grade: {len(per_shot)} LUTs gated with enable=gte/lt timeline expressions")


def lens_filter(lens: dict, caps: Caps, warnings: list[str]) -> str | None:
    if lens.get("make") or lens.get("model"):
        if not caps.has_filter("lensfun"):
            warnings.append("ffmpeg was built without lensfun; lens correction by profile skipped "
                            "(use --lens-k1/--lens-k2 for a manual lenscorrection)")
            return None
        parts = [f"make={esc_filter_value(lens.get('make', 'Canon'))}", f"model={esc_filter_value(lens.get('model', ''))}"]
        if lens.get("lens"):
            parts.append(f"lens_model={esc_filter_value(lens['lens'])}")
        parts.append("mode=geometry")
        return "lensfun=" + ":".join(parts)
    k1, k2 = float(lens.get("k1", 0.0)), float(lens.get("k2", 0.0))
    if k1 == 0 and k2 == 0:
        return None
    return f"lenscorrection=k1={fmt_num(k1)}:k2={fmt_num(k2)}"


def text_filter(t: dict, caps: Caps, warnings: list[str]) -> str | None:
    if not caps.has_filter("drawtext"):
        warnings.append("ffmpeg has no drawtext (libfreetype); text burn-in skipped")
        return None
    s = [f"text='{esc_drawtext(t.get('text', ''))}'", f"fontcolor={t.get('color', 'white')}",
         f"fontsize={t.get('size', 'h/22')}", f"x={t.get('x', '(w-text_w)/2')}", f"y={t.get('y', 'h-th*2')}",
         "box=1", "boxcolor=black@0.4", "boxborderw=8"]
    if t.get("font"):
        s.append(f"font='{esc_drawtext(t['font'])}'")
    if t.get("fontfile"):
        s.append(f"fontfile='{esc_filter_value(t['fontfile'])}'")
    if t.get("start") is not None or t.get("end") is not None:
        a, b = float(t.get("start") or 0), t.get("end")
        s.append(f"enable='between(t,{fmt_num(a)},{fmt_num(float(b)) if b is not None else '99999'})'")
    return "drawtext=" + ":".join(s)


def color_tags_for(hdr_mode: str, info: dict) -> dict:
    """Output colour tags (ffmpeg names)."""
    if hdr_mode == "passthrough":
        return {"primaries": info.get("color_primaries") or "bt2020", "trc": info.get("color_trc") or "smpte2084",
                "space": info.get("color_space") or "bt2020nc", "range": "tv"}
    return {"primaries": "bt709", "trc": "bt709", "space": "bt709", "range": "tv"}
