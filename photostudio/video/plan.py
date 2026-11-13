"""Plan builders: turn (probe info + options + export preset) into a :class:`~photostudio.video.ff.Plan`."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from . import encode as venc
from . import graph as vgraph
from . import lut as vlut
from . import shots as vshots
from . import stabilize as vstab
from . import stack as vstack
from .ff import (Caps, Plan, Step, base_cmd, capabilities, fmt_num)
from .options import GradeOptions, parse_size


def default_output(src: str | Path, suffix: str, ext: str) -> Path:
    p = Path(src)
    return p.with_name(f"{p.stem}_{suffix}{ext}")


# ----------------------------------------------------------------------------- helpers
def _resolve_size(info: dict, opts: GradeOptions, popts: dict, warnings: list[str]) -> tuple[int, int] | None:
    dw, dh = int(info.get("display_width") or info["width"]), int(info.get("display_height") or info["height"])
    if opts.scale:
        return parse_size(opts.scale, dw, dh)
    if opts.max_width and dw > opts.max_width and not popts.get("reframe"):
        return parse_size(f"w{int(opts.max_width)}", dw, dh)
    sz = popts.get("size")
    if not sz or popts.get("reframe"):
        return None
    even = lambda v: max(2, int(round(v / 2.0)) * 2)  # noqa: E731
    if "width" in sz and "height" in sz:
        return even(sz["width"]), even(sz["height"])
    if "width" in sz:
        return parse_size(f"w{int(sz['width'])}", dw, dh)
    if "height" in sz:
        return parse_size(f"h{int(sz['height'])}", dw, dh)
    scale = 1.0
    if "max_height" in sz:
        scale = min(scale, sz["max_height"] / dh)
    if "max_width" in sz:
        scale = min(scale, sz["max_width"] / dw)
    if "max_long_edge" in sz:
        scale = min(scale, sz["max_long_edge"] / max(dw, dh))
    if scale >= 1.0:
        return None                            # never upscale from a preset cap
    return even(dw * scale), even(dh * scale)


def _resolve_fps(info: dict, opts: GradeOptions, popts: dict, notes: list[str]) -> GradeOptions:
    if opts.fps or opts.slowmo:
        return opts
    f = popts.get("fps")
    src = float(info.get("fps") or 0)
    tgt = None
    if isinstance(f, (int, float)):
        tgt = float(f)
    elif isinstance(f, dict):
        if f.get("max") and src > float(f["max"]) + 0.01:
            tgt = float(f["max"])
        if f.get("min") and src < float(f["min"]) - 0.01:
            tgt = float(f["min"])
    if tgt and abs(tgt - src) > 0.01:
        notes.append(f"frame rate {src:.3f} -> {tgt:g} fps (preset)")
        if info.get("is_hfr"):
            notes.append("source looks like 100/120p: pass --slowmo 4 for the camera's quarter-speed slow motion "
                         "instead of dropping frames")
        return dataclasses.replace(opts, fps=tgt)
    return opts


def trim_args(opts: GradeOptions) -> list[str]:
    a: list[str] = []
    if opts.start:
        a += ["-ss", fmt_num(opts.start, 4)]
    if opts.duration:
        a += ["-t", fmt_num(opts.duration, 4)]
    return a


def window(info: dict, opts: GradeOptions) -> tuple[float, float]:
    a = float(opts.start or 0.0)
    d = float(info.get("duration") or 0.0)
    b = min(d, a + opts.duration) if opts.duration else d
    return a, b


# ----------------------------------------------------------------------------- main builder
def build_plan(src: str | Path, out: str | Path, info: dict, opts: GradeOptions, preset: dict | None = None, *,
               hw: str = "auto", caps: Caps | None = None, overwrite: bool = True, encoder: str | None = None,
               name: str = "grade", workdir: Path | None = None) -> Plan:
    """The one builder behind ``grade``, ``export``, ``denoise``, ``preview`` and the per-shot flow."""
    caps = caps or capabilities()
    plan = Plan(name)
    if workdir:
        plan.workdir = Path(workdir)
    out = Path(out)
    plan.outputs.append(str(out))
    popts = venc.preset_options(preset)
    notes, warnings = plan.notes, plan.warnings
    if preset:
        notes.append(f"export preset: {preset.get('name')}")

    # preset-provided reframe / fps / hdr policy (CLI options win)
    if opts.reframe is None and popts.get("reframe"):
        opts = dataclasses.replace(opts, reframe=dict(popts["reframe"]))
    opts = _resolve_fps(info, opts, popts, notes)
    size = _resolve_size(info, opts, popts, warnings)
    preset_hdr = popts.get("hdr")

    # ---- look: stack -> classification
    stack, look_info = vstack.resolve_video_stack(opts.mode, opts.art, opts.stack, preset)
    part = vstack.partition_stack(stack)
    spatial = vgraph.spatial_overrides(part["spatial"])
    for u in part["unsupported"] + part["geometry"]:
        warnings.append(f"look op '{u['op']}' not applied in the ffmpeg path: {u['reason']}")

    fps_in = float(info.get("fps") or 25.0)
    t0, t1 = window(info, opts)
    hdr_mode = vgraph.resolve_hdr_mode(info, opts, preset_hdr)
    norm_notes: list[str] = []
    norm = vgraph.normalise_filters(info, opts, caps, hdr_mode, norm_notes)

    # ---- LUTs (python steps: they are baked right before ffmpeg runs)
    luts: list[vgraph.LutRef] = []
    shot_list: list[dict] | None = None
    do_auto = opts.auto not in ("off", None, "")
    if opts.frames == "python":
        pass                                               # the numpy path applies the whole stack itself
    elif opts.shots or opts.per_shot or do_auto:
        shot_list = vshots.fill_shots(opts.shots or [vshots.make_shot(0, 0.0, info.get("duration") or 0.0)],
                                      info.get("duration") or 0.0)
        shot_list = vshots.clip_shots(shot_list, opts.start, opts.duration)
        if opts.smooth and do_auto:
            shot_list = vshots.expand_segments(shot_list, opts.segment_seconds)
        single = len(shot_list) == 1
        shot_stacks: list[list[dict]] = []
        for i, s in enumerate(shot_list):
            st, _ = vstack.resolve_video_stack(s.get("mode") or opts.mode, s.get("art") or opts.art,
                                               vstack.merge_stacks(opts.stack, s.get("stack") or []), preset)
            shot_stacks.append(st)
            lp = plan.workdir / f"shot_{i:03d}.cube"
            luts.append(vgraph.LutRef(str(lp), None if single else s["start"], None if single else s["end"], f"shot {i}"))
        if do_auto:
            plan.add(Step("python", f"auto-match {len(shot_list)} shot(s) on sampled frames", fn=vshots.make_auto_step_fn(
                src, info, shot_list, opts, norm), description=f"{opts.auto} vs {opts.auto_reference}"))

        def bake_shots(plan_: Plan, progress=None, _stacks=shot_stacks, _luts=luts):
            auto = plan_.state.get("auto") or [None] * len(_stacks)
            cache: dict[str, Any] = {}
            for i, (st, lr) in enumerate(zip(_stacks, _luts)):
                full = list(st)
                if auto[i]:
                    me = vshots.match_entry(auto[i])
                    if me:
                        full = [me] + full
                sig = vlut.lut_signature(full, opts.lut_size, opts.transfer, opts.intensity)
                if sig not in cache:
                    cache[sig] = vlut.bake_lut(full, opts.lut_size, transfer=opts.transfer, intensity=opts.intensity,
                                               title=f"photostudio shot {i}", seed=opts.seed)
                vlut.write_cube(lr.path, cache[sig])
                if progress:
                    progress({"percent": 100.0 * (i + 1) / len(_stacks)})
            plan_.state["luts"] = {lr.path: lr.label for lr in _luts}

        plan.add(Step("python", f"bake {len(luts)} per-shot LUT(s) ({opts.lut_size}^3)", fn=bake_shots))
        notes.append(f"{len(shot_list)} shot(s)" + ("; sidecar-defined" if opts.shots else ""))
    elif part["color"]:
        lp = plan.workdir / "look.cube"
        luts.append(vgraph.LutRef(str(lp), None, None, "look"))

        def bake_one(plan_: Plan, progress=None, _stack=stack, _lp=lp):
            lut = vlut.bake_lut(_stack, opts.lut_size, transfer=opts.transfer, intensity=opts.intensity,
                                title="photostudio look", seed=opts.seed)
            vlut.write_cube(_lp, lut)
            plan_.state["lut_meta"] = lut.meta

        plan.add(Step("python", f"bake look LUT ({opts.lut_size}^3, {opts.transfer}): "
                                f"{', '.join(e['op'] for e in part['color'])}", fn=bake_one))

    # ---- encode spec
    final_fps = opts.fps or (fps_in / opts.slowmo if opts.slowmo else fps_in)
    tags = vgraph.color_tags_for(hdr_mode, info)
    spec = venc.build_encode(popts, fps=final_fps, size=size or (info["width"], info["height"]), hdr_mode=hdr_mode,
                             info=info, tags=tags, hw=hw, caps=caps, encoder_override=encoder)
    notes += spec.notes
    warnings += spec.warnings
    # ---- stabilisation
    stab_filters: list[str] | None = None
    if opts.stabilize is not None:
        sp = vstab.params(opts.stabilize)
        if vstab.available(caps):
            trf = str(plan.workdir / "transforms.trf")
            plan.add(Step("ffmpeg", "stabilise pass 1 (vidstabdetect)", base_cmd(
                *trim_args(opts), "-i", str(src), "-an", "-vf",
                ",".join([*norm, "format=yuv420p", vstab.detect_filter(trf, sp)]), "-f", "null", "-"),
                duration=(t1 - t0) or None))
            stab_filters = vstab.transform_filters(trf, sp)
            notes.append("two-pass vid.stab (pass 2 + unsharp in the main encode); expect ~5-10% crop/zoom")
        else:
            warnings.append("ffmpeg has no vid.stab: using single-pass deshake (lower quality)")
            stab_filters = vstab.deshake_filters(sp)

    split = opts.frames == "python"
    shot_apply = opts.shot_apply
    if shot_apply == "auto":
        shot_apply = "timeline" if len(luts) <= 12 else "concat"
    gres = vgraph.build_graph(info, opts, caps, luts=luts, final_pix_fmt=spec.pix_fmt, spatial=spatial, size=size,
                              preset_hdr=preset_hdr, time_offset=t0, out_fps_hint=final_fps, shot_apply=shot_apply,
                              stabilize_filters=stab_filters, split=split)
    notes += gres.notes
    warnings += gres.warnings
    if spec.codec == "gif":
        from .frames import wrap_gif_palette

        gres.flag, gres.graph, gres.out_label = wrap_gif_palette(gres.flag, gres.graph, gres.out_label,
                                                                dict(popts.get("animation") or {}))
        notes.append("GIF: two-stage palette (palettegen stats_mode=diff + paletteuse bayer dither) in one pass")
    for n in norm_notes:
        if n not in notes:
            notes.append(n)
    plan.info = {"input": str(src), "summary": info.get("summary"), "hdr_mode": gres.hdr_mode,
                 "encoder": spec.encoder, "hw": spec.hw, "pix_fmt": spec.pix_fmt, "output_size": list(gres.out_size),
                 "output_fps": gres.out_fps, "look": {k: v for k, v in look_info.items() if v}}
    if part["color"]:
        plan.info["baked_ops"] = [e["op"] for e in part["color"]]
    if gres.hdr_mode == "passthrough":
        notes.append("HDR10 passthrough: PQ/BT.2020 tags + mastering metadata preserved, no grading")

    # ---- audio
    t_ln = None
    ln_state: dict = {}
    mode = opts.audio
    if (mode or spec.audio.get("mode")) == "loudnorm" and info.get("has_audio"):
        t_ln = venc.loudnorm_target(opts.loudnorm or spec.audio.get("loudnorm"))
        mode = "loudnorm"
        plan.add(Step("ffmpeg", "loudnorm pass 1 (measure)", base_cmd(
            *trim_args(opts), "-i", str(src), "-vn", "-map", "0:a:0", "-af", venc.loudnorm_measure_filter(t_ln),
            "-f", "null", "-", progress=False, loglevel="info"), capture="loudnorm_raw", duration=(t1 - t0) or None))

        def parse_ln(plan_: Plan, progress=None):
            ln_state["m"] = venc.parse_loudnorm_json(plan_.state["loudnorm_raw"])
            plan_.state["loudnorm"] = ln_state["m"]

        plan.add(Step("python", "parse loudnorm measurement", fn=parse_ln))
    if spec.no_audio:
        aargs, anotes = ["-an"], []
    else:
        aargs, anotes = venc.audio_args(spec, info, mode, atempo=gres.audio_atempo, loudnorm_state=ln_state,
                                        loudnorm_target_=t_ln)
    notes += anotes

    # ---- the main encode
    if split:
        from . import pyframes

        pyframes.add_python_step(plan, src, out, info, opts, stack, gres, spec, aargs, hw_note=None)
        return plan
    cmd = base_cmd(*trim_args(opts), "-i", str(src), *gres.args(), overwrite=overwrite)
    if not gres.out_label:
        cmd += ["-map", "0:v:0"]
    cmd += aargs
    cmd += ["-map_metadata", "0"]
    if gres.out_fps and (opts.fps or opts.slowmo or abs(gres.out_fps - fps_in) > 0.01):
        cmd += ["-r", fmt_num(gres.out_fps, 6)]
    cmd += spec.video_args
    if spec.movflags:
        cmd += ["-movflags", spec.movflags]
    cmd.append(str(out))
    plan.add(Step("ffmpeg", f"encode {Path(out).name} ({spec.encoder})", cmd, duration=gres.out_duration))
    return plan
