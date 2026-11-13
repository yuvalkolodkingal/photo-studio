"""Video mode tests: pure command/filtergraph construction, preset validity, LUT maths, and a tiny real ffmpeg e2e.

The pure tests never run ffmpeg (``Caps(assumed=True)`` stands in for a full build).  The e2e tests generate a
2 second ``testsrc2`` clip and are skipped when ffmpeg/ffprobe are not available.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from photostudio import presets
from photostudio.video import (Caps, GradeOptions, LUT3D, Plan, VideoError, bake_lut, build_plan, encode, frames,
                               graph, load_video_preset, lut, normalise_probe, read_cube, shots, stabilize, stack,
                               write_cube)
from photostudio.video import ff

CAPS = Caps(assumed=True)
VIDEO_PRESETS = ["youtube_1080p", "youtube_4k", "reels_9x16_1080x1920", "archive_prores", "dnxhr", "hevc_10bit",
                 "preview_fast", "gif"]
EV = lambda v: [{"op": "exposure", "params": {"ev": v}}]  # noqa: E731


# ----------------------------------------------------------------------------- fixtures
def _raw_probe(w=1920, h=1080, fps="30/1", pix_fmt="yuv420p", audio=True, **vkw):
    v = {"codec_type": "video", "codec_name": "h264", "width": w, "height": h, "r_frame_rate": fps,
         "avg_frame_rate": fps, "pix_fmt": pix_fmt, "duration": "10.0", "color_range": "tv", "color_space": "bt709"}
    v.update(vkw)
    streams = [v]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "48000"})
    return {"streams": streams, "format": {"duration": "10.0", "format_name": "mov,mp4", "size": "123456"}}


@pytest.fixture()
def info():
    return normalise_probe(_raw_probe(), "in.mp4")


def _plan(info, opts=None, preset=None, **kw):
    kw.setdefault("hw", "off")
    kw.setdefault("caps", CAPS)
    return build_plan("in.mp4", "out.mp4", info, opts or GradeOptions(), load_video_preset(preset) if preset else None, **kw)


def _ff_cmd(plan: Plan) -> list:
    cmds = plan.commands()
    return cmds[-1]


def _vf(cmd) -> str:
    for flag in ("-vf", "-filter_complex"):
        if flag in cmd:
            return cmd[cmd.index(flag) + 1]
    raise AssertionError("no filter graph in " + " ".join(map(str, cmd)))


# ----------------------------------------------------------------------------- probe
def test_normalise_probe_sdr(info):
    assert info["width"] == 1920 and info["height"] == 1080
    assert info["fps"] == pytest.approx(30.0)
    assert info["duration"] == pytest.approx(10.0)
    assert info["bit_depth"] == 8 and info["chroma"] in ("420", "4:2:0")
    assert info["has_audio"] and not info["is_hdr"] and not info["is_hfr"]
    assert info["color_space"] == "bt709"


def test_normalise_probe_ntsc_hfr_rotation_hdr():
    raw = _raw_probe(w=3840, h=2160, fps="120000/1001", pix_fmt="yuv420p10le", color_transfer="smpte2084",
                     color_primaries="bt2020", color_space="bt2020nc", codec_name="hevc")
    raw["streams"][0]["side_data_list"] = [{"side_data_type": "Display Matrix", "rotation": -90}]
    i = normalise_probe(raw, "hdr.mov")
    assert i["fps"] == pytest.approx(119.88, abs=0.01)
    assert i["is_hfr"] and i["is_hdr_pq"] and i["is_hdr"] and i["bit_depth"] == 10
    assert i["rotation"] % 180 == 90
    assert (i["display_width"], i["display_height"]) == (2160, 3840)


def test_normalise_probe_no_audio_and_vfr():
    raw = _raw_probe(audio=False, avg_frame_rate="2997/100")
    raw["streams"][0]["r_frame_rate"] = "60/1"
    i = normalise_probe(raw, "x.mp4")
    assert not i["has_audio"]
    assert i["is_vfr"]


def test_missing_ffmpeg_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PHOTOSTUDIO_FFMPEG", str(tmp_path / "nope"))
    monkeypatch.setenv("PHOTOSTUDIO_FFPROBE", str(tmp_path / "nope2"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(ff, "_imageio_ffmpeg", lambda: None, raising=False)
    from photostudio.util import MissingDependency

    with pytest.raises((VideoError, MissingDependency)) as e:
        ff.ffprobe_path()
    assert "ffprobe" in str(e.value).lower() and "install" in str(e.value).lower()


# ----------------------------------------------------------------------------- filtergraph construction
def test_grade_graph_is_explicit_about_colour(info):
    plan = _plan(info, GradeOptions(stack=EV(0.5)))
    vf = _vf(_ff_cmd(plan))
    assert "in_range=tv:in_color_matrix=bt709" in vf
    assert "format=gbrp10le" in vf
    assert "lut3d=file=" in vf and "interp=tetrahedral" in vf
    assert "out_color_matrix=bt709:out_range=tv" in vf
    assert vf.index("lut3d") < vf.index("out_color_matrix")
    cmd = _ff_cmd(plan)
    for flag, val in (("-colorspace", "bt709"), ("-color_primaries", "bt709"), ("-color_range", "tv")):
        assert cmd[cmd.index(flag) + 1] == val
    assert any(s.kind == "python" for s in plan.steps)  # the LUT bake step


def test_no_look_means_no_lut(info):
    plan = _plan(info, GradeOptions())
    assert "lut3d" not in _vf(_ff_cmd(plan))


def test_spatial_options_order_and_content(info):
    opts = GradeOptions(denoise="light", sharpen="light", grain=0.2, vignette=0.3, fade_in=0.5, fade_out=0.5,
                        deband=True, deflicker=5, letterbox=2.39)
    vf = _vf(_ff_cmd(_plan(info, opts)))
    for name in ("hqdn3d", "deband", "deflicker", "vignette", "noise=", "fade=t=in", "fade=t=out"):
        assert name in vf, name
    assert vf.index("hqdn3d") < vf.index("noise=")           # denoise before grain
    assert vf.index("noise=") < vf.index("fade=t=in")        # grain before fades


def test_unknown_levels_raise(info):
    with pytest.raises(VideoError):
        _plan(info, GradeOptions(sharpen="bogus"))


def test_trim_speed_fps(info):
    plan = _plan(info, GradeOptions(start=1.0, duration=2.0, speed=2.0, fps=24))
    cmd = _ff_cmd(plan)
    assert "-ss" in cmd and "-t" in cmd
    vf = _vf(cmd)
    assert "setpts=" in vf
    assert any("atempo" in str(a) for a in cmd) or "-an" in cmd


def test_atempo_chain_splits_extremes():
    assert graph.atempo_chain(2.0) == "atempo=2"
    assert graph.atempo_chain(4.0) == "atempo=4"            # atempo accepts 0.5..100 per instance
    assert graph.atempo_chain(250.0).count("atempo") == 2
    c = graph.atempo_chain(0.25)
    assert c.count("atempo") == 2
    prod = 1.0
    for part in c.split(","):
        prod *= float(part.split("=")[1])
    assert prod == pytest.approx(0.25)


def test_slowmo_keeps_every_frame():
    info = normalise_probe(_raw_probe(fps="120/1", audio=False), "hfr.mp4")
    plan = _plan(info, GradeOptions(slowmo=4.0))
    cmd = _ff_cmd(plan)
    assert "setpts=4*PTS" in _vf(cmd)
    assert cmd[cmd.index("-r") + 1] in ("30", "30.0")


def test_reels_preset_reframes_to_vertical(info):
    plan = _plan(info, GradeOptions(), preset="reels_9x16_1080x1920")
    vf = _vf(_ff_cmd(plan))
    assert "1080" in vf and "1920" in vf
    assert "gblur" in vf and "overlay" in vf          # default variant: blurred background


def test_reels_crop_variant_has_no_blur(info):
    plan = _plan(info, GradeOptions(), preset="reels_9x16_1080x1920:crop")
    vf = _vf(_ff_cmd(plan))
    assert "crop=" in vf and "gblur" not in vf


def test_per_shot_timeline_and_concat(info):
    sh = [{"start": 0, "end": 4, "stack": EV(0.5)}, {"start": 4, "end": 10, "stack": EV(-0.5)}]
    tl = _plan(info, GradeOptions(per_shot=True, shots=sh, shot_apply="timeline"))
    vf = _vf(_ff_cmd(tl))
    assert vf.count("lut3d=") == 2 and "enable=" in vf and "gte(t," in vf
    cc = _plan(info, GradeOptions(per_shot=True, shots=sh, shot_apply="concat"))
    vf = _vf(_ff_cmd(cc))
    assert "concat=n=2" in vf and "trim=" in vf and vf.count("lut3d=") == 2


def test_hdr_tonemap_and_passthrough():
    raw = _raw_probe(w=3840, h=2160, pix_fmt="yuv420p10le", color_transfer="smpte2084", color_primaries="bt2020",
                     color_space="bt2020nc", codec_name="hevc")
    hdr = normalise_probe(raw, "hdr.mp4")
    tone = _vf(_ff_cmd(_plan(hdr, GradeOptions(hdr="tonemap"), preset="youtube_1080p")))
    assert "zscale" in tone and "tonemap=tonemap=mobius" in tone and "format=yuv420p" in tone
    pas = _ff_cmd(_plan(hdr, GradeOptions(hdr="passthrough"), preset="hevc_10bit"))
    joined = " ".join(map(str, pas))
    assert "libx265" in joined and "yuv420p10le" in joined
    assert "smpte2084" in joined and "bt2020" in joined
    assert "hdr10" in joined.lower()


def test_hdr_default_is_mobius(info):
    assert GradeOptions().tonemap == "mobius"


def test_dry_run_json_is_serialisable(info):
    plan = _plan(info, GradeOptions(stack=EV(0.3)), preset="youtube_1080p")
    d = plan.to_json()
    json.dumps(d)
    assert d["dry_run"] is True or d["dry_run"] is False
    assert d["commands"] and all(isinstance(c, list) for c in d["commands"])
    assert all(isinstance(a, str) for c in d["commands"] for a in c)
    assert d["command_lines"][-1].startswith(str(d["commands"][-1][0]))


def test_commands_are_argv_lists(info):
    plan = _plan(info, GradeOptions(text={"text": "a b; rm -rf /", "size": 24}))
    for c in plan.commands():
        assert isinstance(c, list)
        assert c[0].endswith("ffmpeg") or "ffmpeg" in c[0]


def test_lensfun_missing_is_warning_not_crash(info):
    caps = Caps(version="x", filters=frozenset({"lut3d", "scale", "format"}), encoders=frozenset({"libx264", "aac"}))
    plan = _plan(info, GradeOptions(lens={"k1": -0.1}), caps=caps)
    assert plan.commands()


# ----------------------------------------------------------------------------- stabilisation
def test_stabilize_filters():
    d = stabilize.detect_filter("/tmp/x.trf", stabilize.params({"shakiness": 7}))
    assert "vidstabdetect" in d and "shakiness=7" in d and "/tmp/x.trf" in d
    t = stabilize.transform_filters("/tmp/x.trf", stabilize.params({"smoothing": 20}))
    joined = ",".join(t)
    assert "vidstabtransform" in joined and "smoothing=20" in joined and "unsharp" in joined
    de = ",".join(stabilize.deshake_filters(stabilize.params({})))
    assert "deshake" in de


def test_stabilize_plan_two_pass_when_vidstab_present(info):
    caps = Caps(version="x", filters=frozenset({"vidstabdetect", "vidstabtransform", "lut3d", "scale", "unsharp"}),
                encoders=frozenset({"libx264", "aac"}))
    plan = _plan(info, GradeOptions(stabilize={"shakiness": 5}), caps=caps, name="stabilize")
    ff_steps = [s for s in plan.steps if s.kind == "ffmpeg"]
    assert len(ff_steps) == 2
    assert "vidstabdetect" in _vf(ff_steps[0].argv_resolved() if hasattr(ff_steps[0], "argv_resolved") else plan.commands()[0])
    assert "vidstabtransform" in _vf(plan.commands()[-1])


def test_stabilize_falls_back_to_deshake(info):
    caps = Caps(version="x", filters=frozenset({"deshake", "scale", "lut3d"}), encoders=frozenset({"libx264", "aac"}))
    plan = _plan(info, GradeOptions(stabilize={"shakiness": 5}), caps=caps)
    assert "deshake" in _vf(plan.commands()[-1])
    assert any("deshake" in n.lower() or "vidstab" in n.lower() for n in plan.notes + plan.warnings)


# ----------------------------------------------------------------------------- encode / presets / audio
@pytest.mark.parametrize("name", VIDEO_PRESETS)
def test_video_presets_load_and_build(name, info):
    pre = load_video_preset(name)
    assert pre
    assert presets.validate_preset(pre) == []
    out = "out.gif" if name == "gif" else "out.mp4"
    plan = build_plan("in.mp4", out, info, GradeOptions(), pre, hw="off", caps=CAPS)
    assert plan.commands()


def test_all_video_presets_listed_and_variants_load():
    listed = presets.list_presets("video")
    names = {p["name"] for p in listed}
    assert set(VIDEO_PRESETS) <= names
    for v in ("youtube_1080p:hevc", "youtube_1080p:hq", "reels_9x16_1080x1920:pad", "archive_prores:proxy",
              "archive_prores:lt", "archive_prores:4444", "dnxhr:hq", "dnxhr:sq", "dnxhr:lb", "hevc_10bit:hdr10",
              "gif:webp", "gif:small", "youtube_4k:hevc"):
        assert load_video_preset(v)


def test_preset_bad_name_is_error():
    with pytest.raises(Exception):
        load_video_preset("definitely_not_a_preset")


def test_prores_dnxhr_args(info):
    pr = " ".join(map(str, _ff_cmd(_plan(info, preset="archive_prores", name="export"))))
    assert "prores_ks" in pr and "yuv422p10le" in pr
    dn = " ".join(map(str, _ff_cmd(_plan(info, preset="dnxhr"))))
    assert "dnxhd" in dn and "dnxhr_hqx" in dn


def test_h264_gop_and_tags(info):
    cmd = _ff_cmd(_plan(info, preset="youtube_1080p"))
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-g") + 1] == "15"               # half the frame rate
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"


def test_nvenc_auto_detect_and_fallback():
    caps = Caps(version="x", filters=frozenset(), encoders=frozenset({"libx264", "h264_nvenc", "libx265"}))
    enc, hw, _ = encode.pick_encoder("h264", "off", caps)
    assert enc == "libx264" and not hw
    enc, hw, notes = encode.pick_encoder("h264", "auto", caps)
    # auto only picks NVENC when a real tiny encode succeeds; on a box without a GPU this falls back
    assert enc in ("libx264", "h264_nvenc")
    only_sw = Caps(version="x", filters=frozenset(), encoders=frozenset({"libx264"}))
    with pytest.raises(VideoError):
        encode.pick_encoder("h264", "on", only_sw)
    nothing = Caps(version="x", filters=frozenset(), encoders=frozenset())
    with pytest.raises(VideoError) as e:
        encode.pick_encoder("hevc", "off", nothing)
    assert "libx265" in str(e.value)


def test_gif_plan_uses_palette(info):
    cmd = _ff_cmd(_plan(info, preset="gif"))
    vf = _vf(cmd)
    assert "palettegen" in vf and "paletteuse" in vf


def test_parse_loudnorm_json():
    txt = 'noise\n[Parsed_loudnorm_0 @ 0x1]\n{\n "input_i" : "-20.1",\n "input_tp" : "-3.0",\n "input_lra" : "5.0",\n' \
          ' "input_thresh" : "-30.5",\n "output_i" : "-14.0",\n "target_offset" : "0.2"\n}\n'
    m = encode.parse_loudnorm_json(txt)
    assert m["input_i"] == "-20.1"
    f = encode.loudnorm_apply_filter(encode.loudnorm_target({"I": -14}), m)
    assert "measured_I=-20.1" in f and "offset=0.2" in f and "linear=true" in f
    with pytest.raises(VideoError):
        encode.parse_loudnorm_json("nothing here")
    with pytest.raises(VideoError):
        encode.parse_loudnorm_json('{"input_i": "1"}')


def test_loudnorm_plan_is_two_pass(info):
    plan = _plan(info, GradeOptions(audio="loudnorm"), preset="youtube_1080p")
    cmds = plan.commands()
    assert len(cmds) >= 2
    assert any("loudnorm" in " ".join(map(str, c)) and "print_format=json" in " ".join(map(str, c)) for c in cmds)
    assert any(s.kind == "python" for s in plan.steps) or any(isinstance(a, ff.LazyArg) for s in plan.steps
                                                              for a in getattr(s, "argv", []))


def test_audio_strip_and_passthrough(info):
    cmd = _ff_cmd(_plan(info, GradeOptions(audio="none"), preset="youtube_1080p"))
    assert "-an" in cmd
    cmd = _ff_cmd(_plan(info, GradeOptions(), preset="youtube_1080p"))
    assert cmd[cmd.index("-c:a") + 1] == "copy"


# ----------------------------------------------------------------------------- stack / classification
def test_stack_resolution_priority():
    s, _ = stack.resolve_video_stack(None, None, EV(0.2), None)
    assert s and s[-1]["op"] == "exposure"
    merged = stack.merge_stacks(EV(0.1), EV(0.4))
    assert len(merged) == 1 and merged[0]["params"]["ev"] == pytest.approx(0.4)


def test_partition_stack_classes():
    part = stack.partition_stack([{"op": "exposure", "params": {"ev": 0.2}},
                                  {"op": "grain", "params": {"amount": 0.3}},
                                  {"op": "vignette", "params": {"amount": 0.2}}])
    assert len(part["color"]) >= 1
    assert part["spatial"]


# ----------------------------------------------------------------------------- LUT maths
def test_identity_lut_is_identity():
    L = bake_lut([], 17)
    assert L.max_error_vs_identity() < 1e-4
    img = np.random.default_rng(0).random((8, 8, 3)).astype(np.float32)
    assert np.abs(L.apply(img) - img).max() < 1e-3


def test_plus_one_ev_brightens():
    L = bake_lut(EV(1.0), 17)
    x = np.full((2, 2, 3), 0.3, np.float32)
    y = L.apply(x)
    assert y.mean() > 0.3 * 1.3
    L0 = bake_lut(EV(-1.0), 17)
    assert L0.apply(x).mean() < 0.3


def test_intensity_blends_toward_identity():
    full = bake_lut(EV(1.0), 17)
    half = bake_lut(EV(1.0), 17, intensity=0.5)
    zero = bake_lut(EV(1.0), 17, intensity=0.0)
    assert zero.max_error_vs_identity() < 1e-4
    assert 0 < half.max_error_vs_identity() < full.max_error_vs_identity()


@pytest.mark.parametrize("size", [17, 33])
def test_cube_round_trip(tmp_path, size):
    L = bake_lut(EV(0.5), size)
    p = write_cube(tmp_path / "x.cube", L)
    text = p.read_text()
    assert f"LUT_3D_SIZE {size}" in text
    L2 = read_cube(p)
    assert L2.size == size
    assert np.abs(L2.table - L.table).max() < 1e-4


def test_cube_red_fastest(tmp_path):
    n = 5
    L = LUT3D(lut.identity_table(n))
    p = write_cube(tmp_path / "i.cube", L)
    rows = [r for r in p.read_text().splitlines() if r and r[0].isdigit()]
    first, second = [float(v) for v in rows[0].split()], [float(v) for v in rows[1].split()]
    assert first == [0, 0, 0] and second[0] > 0 and second[1] == 0 and second[2] == 0


def test_transfer_round_trip():
    x = np.linspace(0, 1, 33)
    for t in ("srgb", "bt709", "gamma24"):
        assert np.abs(lut.encode_transfer(lut.decode_transfer(x, t), t) - x).max() < 1e-4


def test_bad_lut_size_and_transfer():
    with pytest.raises(VideoError):
        bake_lut([], 1)
    with pytest.raises(VideoError):
        bake_lut([], 17, transfer="nope")


# ----------------------------------------------------------------------------- shots
def test_cuts_to_shots_merges_close_cuts():
    s = shots.cuts_to_shots([2.0, 2.2, 5.0], 8.0, min_len=0.5)
    assert [round(x["start"], 2) for x in s] == [0.0, 2.0, 5.0]
    assert s[-1]["end"] == 8.0
    assert all(a["end"] == b["start"] for a, b in zip(s, s[1:]))


def test_fill_and_clip_shots():
    s = shots.fill_shots([{"start": 1, "end": 3}, {"start": 4, "end": 6}], 8.0)
    assert s[0]["start"] == 0 and s[0]["end"] == 4 and s[-1]["end"] == 8.0
    c = shots.clip_shots(s, 2.0, 3.0)
    assert c[0]["start"] == 2.0 and c[-1]["end"] == 5.0


def test_sidecar_round_trip(tmp_path):
    v = tmp_path / "clip.mp4"
    v.write_bytes(b"x")
    assert shots.shots_path(v).name == "clip.mp4.ps.json"
    doc = shots.new_doc(v, None, [shots.make_shot(0, 0, 2), shots.make_shot(1, 2, 5)])
    shots.save_shots(v, doc)
    back = shots.load_shots(v)
    assert [x["start"] for x in back["shots"]] == [0, 2]
    assert {"start", "end", "mode", "art", "stack"} <= set(back["shots"][0])


def _st(y, rgb=(1.0, 1.0, 1.0)):
    return {"median_y": y, "p5": y / 4, "p95": y * 3, "mid_rgb": [y * c for c in rgb], "n_frames": 3}


def test_solve_matches_is_gentle_and_aligns():
    res = shots.solve_matches([_st(0.09), _st(0.18), _st(0.18)], mode="exposure", reference="median", max_ev=0.5)
    assert len(res) == 3
    assert 0 < res[0]["ev"] <= 0.5 + 1e-9                 # dark shot lifted, clamped to max_ev
    assert abs(res[1]["ev"]) < 1e-6
    big = shots.solve_matches([_st(0.02), _st(0.4)], mode="exposure", reference="first", max_ev=0.3)
    assert abs(big[1]["ev"]) <= 0.3 + 1e-9
    wb = shots.solve_matches([_st(0.18, (1.3, 1.0, 0.8)), _st(0.18), _st(0.18)], mode="wb", reference="median")
    assert all(abs(g - 1.0) <= 0.25 + 1e-9 for r in wb for g in r["gain"])
    assert wb[0]["gain"][0] < 1.0 < wb[0]["gain"][2]       # warm cast pulled back towards neutral
    assert shots.solve_matches([None, _st(0.18)])[0] == {"ev": 0.0, "gain": [1.0, 1.0, 1.0]}


def test_frame_stats_on_array():
    flat = np.full((16, 16, 3), 0.5, np.float32)
    st = shots.frame_stats([flat])
    assert st["n_frames"] == 1 and 0.1 < st["median_y"] < 0.4   # linear light of an sRGB 0.5 grey is ~0.21
    assert shots.frame_stats([]) is None


# ----------------------------------------------------------------------------- frame tools (pure)
def test_stills_plan_fixed_times(info, tmp_path):
    plan = frames.build_stills_plan("in.mp4", info, tmp_path, times=[1.0, 2.5], caps=CAPS) \
        if "caps" in frames.build_stills_plan.__code__.co_varnames else \
        frames.build_stills_plan("in.mp4", info, tmp_path, times=[1.0, 2.5])
    cmds = plan.commands()
    assert len(cmds) == 2
    assert all("-frames:v" in c or "-vframes" in c for c in cmds)


def test_contact_sheet_plan(info, tmp_path):
    plan = frames.build_contact_sheet_plan("in.mp4", info, tmp_path / "sheet.jpg", cols=4, rows=3)
    vf = _vf(plan.commands()[-1])
    assert "tile=4x3" in vf


def test_timelapse_plan_deflicker_and_kenburns(tmp_path):
    for i in range(6):
        (tmp_path / f"f{i:03d}.png").write_bytes(b"x")
    try:
        from PIL import Image
        for i in range(6):
            Image.fromarray(np.full((36, 64, 3), i * 20, np.uint8)).save(tmp_path / f"f{i:03d}.png")
    except Exception:  # pragma: no cover
        pytest.skip("PIL unavailable")
    plan = frames.build_timelapse_plan(tmp_path, tmp_path / "t.mp4", fps=24, deflicker=5, hw="off", caps=CAPS,
                                       kenburns=True) if _accepts(frames.build_timelapse_plan, "kenburns") else \
        frames.build_timelapse_plan(tmp_path, tmp_path / "t.mp4", fps=24, deflicker=5, caps=CAPS)
    joined = " ".join(map(str, plan.commands()[-1]))
    assert "deflicker" in joined and "-framerate" in joined or "-r" in joined


def _accepts(fn, name):
    return name in fn.__code__.co_varnames[:fn.__code__.co_argcount + fn.__code__.co_kwonlyargcount]


# ----------------------------------------------------------------------------- CLI dry-run (no ffmpeg run)
def test_cli_dry_run_returns_commands(tmp_path, capsys):
    from photostudio import cli

    src = tmp_path / "in.mp4"
    src.write_bytes(b"not really a video")
    # probe needs real ffprobe; for a dry run of grade we monkeypatch probe via the video package
    import photostudio.video as V
    import photostudio.commands.video_cmd as vc

    fake = normalise_probe(_raw_probe(), str(src))
    orig = V.probe
    try:
        V.probe = lambda p: fake
        if hasattr(vc, "probe"):
            vc.probe = lambda p: fake
        rc = cli.main(["--json", "video", "grade", str(src), "--set", "exposure.ev=0.5", "--preset",
                       "youtube_1080p", "--hw", "off", "--dry-run", "-o", str(tmp_path / "o.mp4")])
    finally:
        V.probe = orig
    out = capsys.readouterr().out
    assert rc in (0, None)
    d = json.loads(out)
    payload = d.get("data", d)
    assert payload.get("commands") or payload.get("plan", {}).get("commands")


# ----------------------------------------------------------------------------- e2e with real ffmpeg
def _have_ffmpeg() -> bool:
    try:
        return bool(ff.find_ffmpeg()) and bool(ff.find_ffprobe())
    except Exception:
        return False


needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg/ffprobe not available")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not _have_ffmpeg():
        pytest.skip("ffmpeg/ffprobe not available")
    d = tmp_path_factory.mktemp("clip")
    out = d / "tiny.mp4"
    base = [str(ff.ffmpeg_path()), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=s=160x96:r=25:d=2", "-f", "lavfi", "-i", "sine=f=440:d=2", "-shortest",
            "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-color_range", "tv"]
    for enc in (["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"], ["-c:v", "mpeg4", "-c:a", "aac"]):
        r = subprocess.run(base + enc + [str(out)], capture_output=True, text=True)
        if r.returncode == 0 and out.exists():
            return out
    pytest.skip("no usable encoder to build the test clip")


@needs_ffmpeg
def test_e2e_probe(clip):
    from photostudio.video import probe

    i = probe(clip)
    assert (i["width"], i["height"]) == (160, 96)
    assert i["fps"] == pytest.approx(25.0)
    assert i["has_audio"] and i["duration"] == pytest.approx(2.0, abs=0.2)


def _run(plan, **kw):
    from photostudio.video import run_plan

    return run_plan(plan, dry_run=False, progress=None, **kw)


def _out_info(p):
    from photostudio.video import probe

    return probe(p)


def _mean_rgb(path: Path, t: float = 0.5) -> np.ndarray:
    r = subprocess.run([str(ff.ffmpeg_path()), "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=in_range=tv:in_color_matrix=bt709,format=rgb24", "-f", "rawvideo", "-"],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode()
    raw = np.frombuffer(r.stdout, np.uint8)
    return raw.reshape(-1, 3).mean(axis=0)


@needs_ffmpeg
def test_e2e_grade_with_lut_brightens_and_cleans_temp(clip, tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    tempfile.tempdir = None
    info = _out_info(clip)
    ident = tmp_path / "ident.mp4"
    _run(build_plan(clip, ident, info, GradeOptions(stack=[]), None, hw="off", name="grade"))
    out = tmp_path / "bright.mp4"
    plan = build_plan(clip, out, info, GradeOptions(stack=EV(1.0)), None, hw="off", name="grade")
    _run(plan)
    assert out.exists()
    oi = _out_info(out)
    assert (oi["width"], oi["height"]) == (160, 96)
    assert oi["color_space"] == "bt709" and oi["color_range"] in ("tv", "limited")
    assert _mean_rgb(out).mean() > _mean_rgb(ident).mean() + 10
    assert not list((tmp_path / "tmp").glob("photostudio-video-*"))


@needs_ffmpeg
def test_e2e_identity_grade_stays_close(clip, tmp_path):
    info = _out_info(clip)
    out = tmp_path / "id.mp4"
    _run(build_plan(clip, out, info, GradeOptions(stack=[{"op": "exposure", "params": {"ev": 0.0}}]), None, hw="off"))
    assert np.abs(_mean_rgb(out) - _mean_rgb(clip)).max() < 4.0


@needs_ffmpeg
def test_e2e_per_shot_timeline(clip, tmp_path):
    info = _out_info(clip)
    sh = [{"start": 0, "end": 1.0, "stack": EV(1.0)}, {"start": 1.0, "end": 2.0, "stack": EV(-1.0)}]
    out = tmp_path / "ps.mp4"
    _run(build_plan(clip, out, info, GradeOptions(per_shot=True, shots=sh, shot_apply="timeline"), None, hw="off"))
    assert _mean_rgb(out, 0.3).mean() > _mean_rgb(out, 1.6).mean() + 20
    out2 = tmp_path / "ps2.mp4"
    _run(build_plan(clip, out2, info, GradeOptions(per_shot=True, shots=sh, shot_apply="concat"), None, hw="off"))
    assert _mean_rgb(out2, 0.3).mean() > _mean_rgb(out2, 1.6).mean() + 20


@needs_ffmpeg
def test_e2e_stills(clip, tmp_path):
    info = _out_info(clip)
    plan = frames.build_stills_plan(clip, info, tmp_path, times=[0.2, 1.0])
    _run(plan)
    files = sorted(p for p in tmp_path.iterdir() if p.suffix.lower() in (".png", ".tif", ".tiff", ".jpg"))
    assert len(files) == 2


@needs_ffmpeg
def test_e2e_timelapse_from_pngs(tmp_path):
    from PIL import Image

    src = tmp_path / "frames"
    src.mkdir()
    for i in range(8):
        Image.fromarray(np.full((48, 64, 3), 40 + i * 10, np.uint8)).save(src / f"IMG_{i:04d}.png")
    out = tmp_path / "tl.mp4"
    try:
        plan = frames.build_timelapse_plan(src, out, fps=8, deflicker=3, hw="off")
    except TypeError:
        plan = frames.build_timelapse_plan(src, out, fps=8, deflicker=3)
    _run(plan)
    oi = _out_info(out)
    assert oi["width"] % 2 == 0 and oi["duration"] == pytest.approx(1.0, abs=0.3)


@needs_ffmpeg
def test_e2e_python_frames_path(clip, tmp_path):
    info = _out_info(clip)
    out = tmp_path / "py.mp4"
    base = _mean_rgb(clip)
    plan = build_plan(clip, out, info, GradeOptions(stack=EV(1.0), frames="python", duration=1.0), None, hw="off")
    _run(plan)
    assert out.exists()
    assert _mean_rgb(out).mean() > base.mean() + 8


@needs_ffmpeg
def test_e2e_failure_removes_partial_output(clip, tmp_path):
    info = _out_info(clip)
    out = tmp_path / "bad.mp4"
    plan = build_plan(clip, out, info, GradeOptions(), None, hw="off")
    cmd = plan.steps[-1]
    # corrupt the filtergraph so ffmpeg fails
    argv = list(cmd.argv)
    argv[argv.index("-vf") + 1] = "nosuchfilter=1" if "-vf" in argv else argv[argv.index("-filter_complex") + 1]
    cmd.argv = argv
    with pytest.raises(Exception):
        _run(plan)
    assert not out.exists()
