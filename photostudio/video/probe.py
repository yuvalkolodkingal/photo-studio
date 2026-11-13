"""ffprobe JSON -> one normalised clip description (codec, size, fps, bit depth, colour tags, HDR, audio...)."""
from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..util import MissingDependency
from .ff import VideoError, ffprobe_path

_PQ = {"smpte2084", "smpte-st-2084", "pq"}
_HLG = {"arib-std-b67", "hlg"}
_BT2020_PRIMS = {"bt2020"}

# PyAV exposes colour tags as ints (H.273 code points)
_H273_PRIM = {1: "bt709", 4: "bt470m", 5: "bt470bg", 6: "smpte170m", 7: "smpte240m", 9: "bt2020", 11: "smpte431", 12: "smpte432"}
_H273_TRC = {1: "bt709", 4: "gamma22", 5: "gamma28", 6: "smpte170m", 7: "smpte240m", 8: "linear", 13: "iec61966-2-1",
             14: "bt2020-10", 15: "bt2020-12", 16: "smpte2084", 18: "arib-std-b67"}
_H273_MAT = {0: "gbr", 1: "bt709", 5: "bt470bg", 6: "smpte170m", 7: "smpte240m", 9: "bt2020nc", 10: "bt2020c"}
_H273_RANGE = {1: "tv", 2: "pc"}


def _frac(s: Any) -> float:
    try:
        if s in (None, "", "0/0", "N/A"):
            return 0.0
        return float(Fraction(str(s)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def bit_depth_from_pix_fmt(pix_fmt: str | None, bits_raw: Any = None) -> int:
    b = _int(bits_raw)
    if b:
        return b
    pf = pix_fmt or ""
    for n in (16, 14, 12, 10, 9):
        if f"{n}le" in pf or f"{n}be" in pf or pf.endswith(str(n)):
            return n
    return 8


def chroma_from_pix_fmt(pix_fmt: str | None) -> str:
    pf = pix_fmt or ""
    if "444" in pf or pf.startswith(("gbr", "rgb", "bgr")):
        return "444"
    if "422" in pf:
        return "422"
    if "420" in pf or pf.startswith(("nv12", "nv21", "p010")):
        return "420"
    if "gray" in pf:
        return "400"
    return "420"


def _rotation(stream: dict) -> int:
    rot = None
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            rot = sd["rotation"]
    if rot is None:
        rot = (stream.get("tags") or {}).get("rotate")
    r = _int(rot) % 360
    return r - r % 90


def _hdr_metadata(frames: list[dict]) -> dict:
    """Mastering-display / content-light-level from the first frame's side data."""
    out: dict[str, Any] = {}
    for fr in frames[:1]:
        for sd in fr.get("side_data_list") or []:
            t = sd.get("side_data_type", "")
            if "Mastering display" in t:
                try:
                    f = lambda k: _frac(sd[k])  # noqa: E731
                    # x265 master-display=G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min) in units of 0.00002 / 0.0001 cd/m2
                    c = lambda k: int(round(f(k) * 50000))  # noqa: E731
                    out["master_display"] = (
                        f"G({c('green_x')},{c('green_y')})B({c('blue_x')},{c('blue_y')})"
                        f"R({c('red_x')},{c('red_y')})WP({c('white_point_x')},{c('white_point_y')})"
                        f"L({int(round(f('max_luminance') * 10000))},{int(round(f('min_luminance') * 10000))})")
                except Exception:
                    pass
            if "Content light level" in t:
                try:
                    out["max_cll"] = f"{int(sd['max_content'])},{int(sd['max_average'])}"
                except Exception:
                    pass
    return out


def normalise_probe(raw: dict, path: str | Path = "", frames: list[dict] | None = None) -> dict:
    """Pure function: ffprobe `-show_format -show_streams -of json` output -> normalised dict."""
    fmt = raw.get("format") or {}
    streams = raw.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    if v is None:
        raise VideoError(f"no video stream found in {path}")
    a = [s for s in streams if s.get("codec_type") == "audio"]

    pix_fmt = v.get("pix_fmt")
    depth = bit_depth_from_pix_fmt(pix_fmt, v.get("bits_per_raw_sample"))
    rot = _rotation(v)
    w, h = _int(v.get("width")), _int(v.get("height"))
    dw, dh = (h, w) if rot in (90, 270) else (w, h)
    r_fps, avg_fps = _frac(v.get("r_frame_rate")), _frac(v.get("avg_frame_rate"))
    fps = avg_fps or r_fps
    dur = _frac(v.get("duration")) or _frac(fmt.get("duration"))
    nb = _int(v.get("nb_frames")) or (int(round(dur * fps)) if dur and fps else 0)
    trc = (v.get("color_transfer") or "unknown").lower()
    prim = (v.get("color_primaries") or "unknown").lower()
    mat = (v.get("color_space") or "unknown").lower()
    rng = (v.get("color_range") or ("pc" if str(pix_fmt or "").startswith("yuvj") else "unknown")).lower()
    if rng == "full":
        rng = "pc"
    elif rng == "limited":
        rng = "tv"
    is_pq = trc in _PQ
    is_hlg = trc in _HLG
    sar = v.get("sample_aspect_ratio")
    sar_f = _frac(sar.replace(":", "/")) if sar and sar not in ("0:1", "N/A") else 1.0
    audio = None
    if a:
        s = a[0]
        audio = {"codec": s.get("codec_name"), "sample_rate": _int(s.get("sample_rate")),
                 "channels": _int(s.get("channels")), "layout": s.get("channel_layout"),
                 "bit_rate": _int(s.get("bit_rate")), "streams": len(a)}
    tags = {**(fmt.get("tags") or {}), **(v.get("tags") or {})}
    info = {
        "path": str(path), "container": fmt.get("format_name"), "format_long_name": fmt.get("format_long_name"),
        "size_bytes": _int(fmt.get("size")), "bit_rate": _int(fmt.get("bit_rate")),
        "duration": dur, "codec": v.get("codec_name"), "profile": v.get("profile"),
        "width": w, "height": h, "display_width": dw, "display_height": dh, "rotation": rot,
        "sar": sar_f, "fps": round(fps, 6), "r_frame_rate": round(r_fps, 6), "nb_frames": nb,
        "pix_fmt": pix_fmt, "bit_depth": depth, "chroma": chroma_from_pix_fmt(pix_fmt),
        "color_range": rng, "color_space": mat, "color_primaries": prim, "color_trc": trc,
        "field_order": v.get("field_order"),
        "is_hdr_pq": bool(is_pq), "is_hlg": bool(is_hlg), "is_hdr": bool(is_pq or is_hlg),
        "is_bt2020": prim in _BT2020_PRIMS or mat.startswith("bt2020"),
        "is_hfr": fps >= 90.0, "is_vfr": bool(r_fps and avg_fps and abs(r_fps - avg_fps) / max(r_fps, 1e-6) > 0.02),
        "has_audio": bool(a), "audio": audio, "audio_streams": len(a),
        "creation_time": tags.get("creation_time"),
    }
    info["is_sdr_709"] = (not info["is_hdr"]) and prim in ("bt709", "unknown") and mat in ("bt709", "unknown")
    info.update(_hdr_metadata(frames or []))
    info["summary"] = (f"{info['codec']} {dw}x{dh}@{info['fps']:.3f} {depth}-bit {info['chroma']} "
                       f"{'HDR PQ' if is_pq else 'HLG' if is_hlg else 'SDR'} {dur:.2f}s"
                       + (f" audio:{audio['codec']}" if audio else " no-audio"))
    return info


def _run_probe(args: list[str]) -> dict:
    exe = ffprobe_path()
    r = subprocess.run([exe, "-v", "error", "-of", "json", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoError(f"ffprobe failed: {r.stderr.strip() or r.stdout.strip()}")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VideoError(f"ffprobe returned invalid JSON: {exc}") from exc


def probe(path: str | Path) -> dict:
    """Probe a video file.  Uses ffprobe; if only ffmpeg (e.g. imageio-ffmpeg) is available, falls back to PyAV."""
    p = Path(path)
    if not p.exists():
        raise VideoError(f"video file not found: {p}")
    try:
        raw = _run_probe(["-show_format", "-show_streams", str(p)])
    except MissingDependency:
        return _probe_pyav(p)
    frames: list[dict] = []
    v = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {})
    if (v.get("color_transfer") or "").lower() in _PQ | _HLG:
        try:
            frames = _run_probe(["-select_streams", "v:0", "-read_intervals", "%+#1", "-show_frames",
                                 "-show_entries", "frame=side_data_list", str(p)]).get("frames", [])
        except VideoError:
            frames = []
    return normalise_probe(raw, p, frames)


def _probe_pyav(p: Path) -> dict:
    av = __import__("av")
    with av.open(str(p)) as c:
        vs = next((s for s in c.streams if s.type == "video"), None)
        if vs is None:
            raise VideoError(f"no video stream found in {p}")
        cc = vs.codec_context
        streams = [{
            "codec_type": "video", "codec_name": cc.name, "profile": getattr(cc, "profile", None),
            "width": cc.width, "height": cc.height, "pix_fmt": getattr(cc.pix_fmt, "name", None) if not isinstance(cc.pix_fmt, str) else cc.pix_fmt,
            "r_frame_rate": str(vs.base_rate or vs.average_rate or 0), "avg_frame_rate": str(vs.average_rate or 0),
            "duration": str(float(vs.duration * vs.time_base)) if vs.duration else None, "nb_frames": str(vs.frames or ""),
            "color_range": _H273_RANGE.get(int(getattr(cc, "color_range", 0) or 0), "unknown"),
            "color_space": _H273_MAT.get(int(getattr(cc, "colorspace", 0) or 0), "unknown"),
            "color_primaries": _H273_PRIM.get(int(getattr(cc, "color_primaries", 0) or 0), "unknown"),
            "color_transfer": _H273_TRC.get(int(getattr(cc, "color_trc", 0) or 0), "unknown"),
        }]
        for s in c.streams:
            if s.type == "audio":
                streams.append({"codec_type": "audio", "codec_name": s.codec_context.name,
                                "sample_rate": str(s.codec_context.sample_rate), "channels": s.codec_context.channels
                                if hasattr(s.codec_context, "channels") else 2})
        fmt = {"format_name": c.format.name, "duration": str(c.duration / 1e6) if c.duration else None,
               "size": str(p.stat().st_size), "tags": dict(c.metadata or {})}
    return normalise_probe({"streams": streams, "format": fmt}, p)
