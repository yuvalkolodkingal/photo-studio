"""Export presets -> encoder arguments (software with hardware auto-detection), colour tags and audio."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .ff import Caps, LazyArg, VideoError, encoder_usable, fmt_num

SW_ENCODERS = {
    "h264": ["libx264", "h264_nvenc", "libopenh264", "h264_vaapi"],
    "hevc": ["libx265", "hevc_nvenc", "hevc_vaapi"],
    "prores": ["prores_ks", "prores_aw", "prores"],
    "dnxhr": ["dnxhd"],
    "av1": ["libsvtav1", "libaom-av1", "av1_nvenc"],
    "vp9": ["libvpx-vp9"],
}
HW_ENCODERS = {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"}
MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac"}
CONTAINER_EXT = {"mp4": ".mp4", "mov": ".mov", "mkv": ".mkv", "gif": ".gif", "webp": ".webp", "mxf": ".mxf"}

DEFAULT_PRESET_OPTIONS: dict[str, Any] = {
    "container": "mp4",
    "video": {"codec": "h264", "crf": 18, "preset": "slow", "profile": "high", "pix_fmt": "yuv420p", "gop": "half_fps",
              "bframes": 2},
    "audio": {"mode": "auto", "codec": "aac", "bitrate": "384k", "rate": 48000, "channels": 2},
    "movflags": "+faststart",
}


def deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def preset_options(preset: dict | None) -> dict:
    return deep_merge(DEFAULT_PRESET_OPTIONS, (preset or {}).get("options") or {})


def _pick_by_fps(val: Any, fps: float) -> str | None:
    """'45M' or {'<=30': '45M', '>30': '68M'} -> value for this frame rate."""
    if val is None or isinstance(val, (str, int, float)):
        return None if val is None else str(val)
    for k, v in val.items():
        m = re.fullmatch(r"\s*(<=|>=|<|>|==)\s*([\d.]+)\s*", str(k))
        if not m:
            continue
        op, x = m.group(1), float(m.group(2))
        if {"<=": fps <= x, ">=": fps >= x, "<": fps < x, ">": fps > x, "==": abs(fps - x) < 1e-6}[op]:
            return str(v)
    return str(next(iter(val.values())))


def gop_frames(spec: Any, fps: float) -> int:
    if spec in (None, "", "auto"):
        return 0
    if spec == "half_fps":
        return max(1, int(round(fps / 2.0)))
    if spec == "fps":
        return max(1, int(round(fps)))
    s = str(spec)
    if s.endswith("s"):
        return max(1, int(round(float(s[:-1]) * fps)))
    return max(1, int(float(s)))


@dataclass
class EncodeSpec:
    video_args: list[str]
    container: str
    ext: str
    pix_fmt: str
    encoder: str
    hw: bool
    codec: str
    movflags: str | None
    audio: dict
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    no_audio: bool = False
    is_image_format: bool = False


def pick_encoder(codec: str, hw: str, caps: Caps, preferred: str | None = None) -> tuple[str, bool, list[str]]:
    """hw: auto (use NVENC when it really works) | on (require) | off."""
    notes: list[str] = []
    if codec not in SW_ENCODERS:
        raise VideoError(f"unknown video codec '{codec}' in preset (known: {', '.join(SW_ENCODERS)})")
    hw_enc = HW_ENCODERS.get(codec)
    if hw in ("auto", "on") and hw_enc and codec in ("h264", "hevc", "av1"):
        if caps.assumed:
            pass
        elif encoder_usable(hw_enc):
            notes.append(f"hardware encoder {hw_enc} detected and used")
            return hw_enc, True, notes
        elif hw == "on":
            raise VideoError(f"--hw on requested but {hw_enc} is not usable here (no NVIDIA GPU/driver, or not in this ffmpeg build)")
        else:
            notes.append(f"{hw_enc} not usable; using software encoder")
    cands = ([preferred] if preferred else []) + SW_ENCODERS[codec]
    for enc in cands:
        if enc and caps.has_encoder(enc):
            if enc != cands[0] and cands[0] and not caps.assumed:
                notes.append(f"{cands[0]} unavailable; using {enc}")
            return enc, enc.endswith(("_nvenc", "_vaapi")), notes
    raise VideoError(
        f"no usable {codec} encoder in this ffmpeg build (tried {', '.join(c for c in cands if c)}). "
        "On Fedora install ffmpeg from RPM Fusion (`sudo dnf swap ffmpeg-free ffmpeg --allowerasing`).")


def build_encode(popts: dict, *, fps: float, size: tuple[int, int], hdr_mode: str, info: dict, tags: dict,
                 hw: str, caps: Caps, encoder_override: str | None = None) -> EncodeSpec:
    """Translate preset ``options`` (see presets/video/*.yaml) into ffmpeg video arguments."""
    v = dict(popts.get("video") or {})
    container = popts.get("container", "mp4")
    codec = v.get("codec", "h264")
    notes: list[str] = []
    warnings: list[str] = []
    if codec in ("gif", "webp"):
        anim = dict(popts.get("animation") or {})
        if codec == "gif":
            if not caps.has_encoder("gif"):
                raise VideoError("this ffmpeg has no gif encoder")
            return EncodeSpec(["-c:v", "gif", "-loop", str(int(anim.get("loop", 0)))], "gif", ".gif", "rgb24", "gif", False,
                              "gif", None, {}, notes, warnings, no_audio=True, is_image_format=True)
        if not caps.has_encoder("libwebp_anim"):
            raise VideoError("this ffmpeg has no libwebp_anim encoder")
        return EncodeSpec(["-c:v", "libwebp_anim", "-loop", str(int(anim.get("loop", 0))), "-q:v", str(int(anim.get("quality", 70))),
                           "-compression_level", str(int(anim.get("compression", 5)))], "webp", ".webp", "yuv420p",
                          "libwebp_anim", False, "webp", None, {}, notes, warnings, no_audio=True, is_image_format=True)
    enc, is_hw, n = pick_encoder(codec, hw if not v.get("no_hw") else "off", caps, encoder_override or v.get("encoder"))
    notes += n
    pix = v.get("pix_fmt", "yuv420p")
    if hdr_mode == "passthrough":
        pix = info.get("pix_fmt") if str(info.get("pix_fmt", "")).startswith("yuv4") else "yuv420p10le"
        if codec != "hevc":
            warnings.append("HDR passthrough needs an HEVC preset (hevc_10bit); the picture will be mislabelled in H.264")
    if is_hw and enc.endswith("_nvenc") and pix == "yuv420p10le":
        pix = "p010le" if codec in ("hevc", "av1") else "yuv420p"
    gop = gop_frames(v.get("gop", "half_fps"), fps)
    args: list[str] = ["-c:v", enc]
    crf = v.get("crf")
    bf = v.get("bframes")
    maxrate = _pick_by_fps(v.get("maxrate"), fps)
    bufsize = _pick_by_fps(v.get("bufsize"), fps)

    if codec == "h264" and enc == "libx264":
        args += ["-preset", str(v.get("preset", "slow")), "-crf", fmt_num(float(crf if crf is not None else 18))]
        if v.get("profile") and pix in ("yuv420p", "yuv422p"):
            args += ["-profile:v", str(v["profile"])]
        if v.get("tune"):
            args += ["-tune", str(v["tune"])]
        if bf is not None:
            args += ["-bf", str(int(bf))]
        if gop:
            args += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
    elif codec == "hevc" and enc == "libx265":
        args += ["-preset", str(v.get("preset", "medium")), "-crf", fmt_num(float(crf if crf is not None else 22))]
        xp: list[str] = []
        if gop:
            xp += [f"keyint={gop}", f"min-keyint={gop}"]
        if bf is not None:
            xp.append(f"bframes={int(bf)}")
        if hdr_mode == "passthrough":
            xp += ["hdr10=1", "hdr10-opt=1", "repeat-headers=1", f"colorprim={_x265_prim(tags)}",
                   f"transfer={tags.get('trc', 'smpte2084')}", f"colormatrix={_x265_matrix(tags)}"]
            if info.get("master_display"):
                xp.append(f"master-display={info['master_display']}")
            if info.get("max_cll"):
                xp.append(f"max-cll={info['max_cll']}")
        xp += [str(x) for x in v.get("x265_params", []) if x]
        if xp:
            args += ["-x265-params", ":".join(xp)]
        args += ["-tag:v", str(v.get("tag", "hvc1"))]
    elif enc.endswith("_nvenc"):
        nv = dict(v.get("nvenc") or {})
        cq = nv.get("cq", (float(crf) + 1) if crf is not None else 20)
        args += ["-preset", str(nv.get("preset", "p6")), "-tune", str(nv.get("tune", "hq")), "-rc", "vbr",
                 "-cq", fmt_num(float(cq)), "-b:v", "0", "-spatial-aq", "1", "-temporal-aq", "1",
                 "-rc-lookahead", "32"]
        if bf is not None:
            args += ["-bf", str(int(bf))]
        if gop:
            args += ["-g", str(gop)]
        if codec == "h264" and v.get("profile"):
            args += ["-profile:v", str(v["profile"])]
        if codec == "hevc":
            args += ["-tag:v", str(v.get("tag", "hvc1"))]
            if pix == "p010le":
                args += ["-profile:v", "main10"]
        notes.append("NVENC is fast but ~10-15% less efficient than x264/x265 at the same quality; use --hw off for archives")
    elif codec == "prores":
        prof = v.get("profile", "hq")
        pmap = {"proxy": 0, "lt": 1, "standard": 2, "422": 2, "hq": 3, "4444": 4, "4444xq": 5}
        args += ["-profile:v", str(pmap.get(str(prof), prof)), "-vendor", str(v.get("vendor", "apl0"))]
        if str(prof) in ("4444", "4444xq") and pix == "yuv422p10le":
            pix = "yuv444p10le"
    elif codec == "dnxhr":
        prof = str(v.get("profile", "dnxhr_hqx"))
        args += ["-profile:v", prof]
        pix = {"dnxhr_hqx": "yuv422p10le", "dnxhr_444": "yuv444p10le"}.get(prof, "yuv422p")
    elif codec == "av1":
        args += ["-crf", fmt_num(float(crf if crf is not None else 30)), "-b:v", "0"]
        if enc == "libsvtav1":
            args += ["-preset", str(v.get("preset", 6))]
    else:  # openh264 / vaapi / other fallbacks
        if v.get("bitrate"):
            args += ["-b:v", str(v["bitrate"])]
        if gop:
            args += ["-g", str(gop)]
    if maxrate:
        args += ["-maxrate", maxrate]
        args += ["-bufsize", bufsize or maxrate]
    args += ["-pix_fmt", pix]
    # colour tags (so players never guess)
    if tags:
        args += ["-color_primaries", tags["primaries"], "-color_trc", tags["trc"], "-colorspace", tags["space"],
                 "-color_range", tags["range"]]
    args += [str(x) for x in v.get("extra", [])]
    return EncodeSpec(args, container, CONTAINER_EXT.get(container, "." + container), pix, enc, is_hw, codec,
                      popts.get("movflags") if container in ("mp4", "mov") else None, dict(popts.get("audio") or {}),
                      notes, warnings)


def _x265_prim(tags: dict) -> str:
    return {"bt2020": "bt2020", "bt709": "bt709"}.get(tags.get("primaries", "bt2020"), "bt2020")


def _x265_matrix(tags: dict) -> str:
    return {"bt2020nc": "bt2020nc", "bt709": "bt709"}.get(tags.get("space", "bt2020nc"), "bt2020nc")


# ----------------------------------------------------------------------------- audio
def parse_loudnorm_json(stderr: str) -> dict:
    """Extract the JSON object that loudnorm prints (print_format=json) at the end of its stderr."""
    i = stderr.rfind("{")
    j = stderr.find("}", i)
    if i < 0 or j < 0:
        raise VideoError("loudnorm measurement output not found")
    try:
        d = json.loads(stderr[i:j + 1])
    except json.JSONDecodeError as exc:
        raise VideoError(f"could not parse loudnorm measurement: {exc}") from exc
    for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        if k not in d:
            raise VideoError(f"loudnorm measurement is missing '{k}'")
    return d


def loudnorm_target(ln: dict | None) -> dict:
    ln = dict(ln or {})
    return {"I": float(ln.get("I", -14)), "TP": float(ln.get("TP", -1)), "LRA": float(ln.get("LRA", 11))}


def loudnorm_measure_filter(t: dict) -> str:
    return f"loudnorm=I={fmt_num(t['I'])}:TP={fmt_num(t['TP'])}:LRA={fmt_num(t['LRA'])}:print_format=json"


def loudnorm_apply_filter(t: dict, m: dict) -> str:
    return (f"loudnorm=I={fmt_num(t['I'])}:TP={fmt_num(t['TP'])}:LRA={fmt_num(t['LRA'])}"
            f":measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
            f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true:print_format=summary")


def audio_args(spec: EncodeSpec, info: dict, mode_override: str | None, *, atempo: str | None,
               loudnorm_state: dict | None = None, loudnorm_target_: dict | None = None) -> tuple[list[Any], list[str]]:
    """-> (argv fragment, notes).  Audio is passed through by default whenever the codec fits the container."""
    notes: list[str] = []
    a = dict(spec.audio)
    mode = mode_override or a.get("mode", "auto")
    if not info.get("has_audio") or mode == "none":
        return ["-an"], (["no audio stream in the source"] if not info.get("has_audio") else ["audio stripped"])
    src = (info.get("audio") or {}).get("codec")
    codec, bitrate = a.get("codec", "aac"), a.get("bitrate", "256k")
    rate, ch = a.get("rate", 48000), a.get("channels", 2)
    if mode in ("aac", "opus", "mp3", "flac", "ac3"):
        codec, mode = mode, "encode"
        if mode == "encode" and codec == "aac" and not a.get("bitrate"):
            bitrate = "256k"
    need_filter = bool(atempo)
    args: list[Any] = ["-map", "0:a:0?"]
    if mode == "loudnorm":
        if loudnorm_state is None or loudnorm_target_ is None:
            raise VideoError("internal: loudnorm needs a measurement state")
        t = loudnorm_target_
        flt = LazyArg(lambda: (atempo + "," if atempo else "") + loudnorm_apply_filter(t, loudnorm_state["m"]),
                      f"{(atempo + ',') if atempo else ''}loudnorm=I={fmt_num(t['I'])}:TP={fmt_num(t['TP'])}"
                      f":LRA={fmt_num(t['LRA'])}:measured_I=<pass1>:measured_TP=<pass1>:measured_LRA=<pass1>"
                      ":measured_thresh=<pass1>:offset=<pass1>:linear=true")
        args += ["-af", flt, "-c:a", codec, "-b:a", bitrate, "-ar", str(rate)]
        notes.append("two-pass loudnorm (EBU R128) normalisation")
        return args, notes
    if mode == "copy" or (mode == "auto" and not need_filter and spec.container in ("mp4", "mov", "mkv")
                          and (spec.container == "mkv" or src in MP4_AUDIO)):
        if need_filter:
            raise VideoError("audio copy is impossible when the speed changes; use --audio aac or --audio none")
        return args + ["-c:a", "copy"], ["audio passed through untouched"]
    if mode == "pcm" or codec == "pcm_s16le" or codec.startswith("pcm"):
        c = "pcm_s16le" if codec in ("pcm", "pcm_s16le") or mode == "pcm" else codec
        if atempo:
            args += ["-af", atempo]
        return args + ["-c:a", c, "-ar", str(rate)], notes
    if atempo:
        args += ["-af", atempo]
    args += ["-c:a", codec if codec != "copy" else "aac", "-b:a", str(bitrate), "-ar", str(rate)]
    if ch:
        args += ["-ac", str(ch)]
    return args, notes
