"""Engine: stage ordering, JPEG/raw paths, stack merge order, masks, caching, unknown ops, LUT bake, presets, mode detection."""
from __future__ import annotations

import sys

import numpy as np
import pytest

from photostudio import modes, presets
from photostudio.pipeline import engine, registry


def E(op, **params):
    return {"op": op, "params": params}


# --------------------------------------------------------------------------- stage ordering
def test_stage_ordering_is_independent_of_stack_order(jpeg):
    ordered = [E("exposure", ev=0.4), E("contrast", contrast=0.1), E("tone_curve", points=[[0, 0], [0.5, 0.45], [1, 1]]),
               E("vignette", amount=-0.3)]
    shuffled = [ordered[3], ordered[1], ordered[2], ordered[0]]
    a = engine.render(jpeg, ordered, use_cache=False)
    b = engine.render(jpeg, shuffled, use_cache=False)
    assert np.allclose(a, b, atol=1e-6)
    s = [registry.get(e["op"]).stage for e in ordered]
    assert s == sorted(s)


def test_same_stage_keeps_stack_order(jpeg):
    """Two ops of the same stage run in stack order (exposure +1 then -1 is identity, order must not matter for it)."""
    a = engine.render(jpeg, [E("exposure", ev=1.0), E("exposure", ev=-1.0)], use_cache=False)
    base = engine.render(jpeg, [], use_cache=False)
    assert np.abs(a - base).mean() < 5e-3


def test_default_tonemap_and_output_transform_are_implicit(jpeg):
    out, ctx = engine.render(jpeg, [], return_ctx=True)
    assert out.dtype == np.float32 and out.min() >= 0 and out.max() <= 1.0 + 1e-6
    assert ctx.domain == "display"


def test_upto_stage_keeps_linear_intermediate(jpeg):
    out, ctx = engine.render(jpeg, [E("exposure", ev=1.0)], upto_stage=10, return_ctx=True)
    assert "linear_at_stop" in ctx.cache
    assert out.shape[:2] == ctx.cache["linear_at_stop"].shape[:2]


def test_exposure_raises_brightness(jpeg):
    lo = engine.render(jpeg, [E("exposure", ev=-1.0)], use_cache=False).mean()
    hi = engine.render(jpeg, [E("exposure", ev=1.0)], use_cache=False).mean()
    assert hi > lo + 0.1


# --------------------------------------------------------------------------- JPEG identity
def test_jpeg_round_trip_is_near_identity(jpeg, scene):
    from photostudio.testing import write_jpeg  # noqa: F401 - the file was written from `scene`
    from PIL import Image

    ref = np.asarray(Image.open(jpeg).convert("RGB"), np.float32) / 255.0
    out = engine.render(jpeg, engine.default_stack("jpeg"), use_cache=False)
    assert out.shape == ref.shape
    err = np.abs(out - ref)
    assert err.mean() < 1.0 / 255 and err.max() < 8.0 / 255


def test_preview_size_and_aspect(jpeg):
    out = engine.render(jpeg, engine.default_stack("jpeg"), long_edge=240)
    assert max(out.shape[:2]) == 240
    assert abs(out.shape[1] / out.shape[0] - 480 / 320) < 0.02


def test_load_source_lru_cache(jpeg):
    a = engine.load_source(jpeg, 200)
    b = engine.load_source(jpeg, 200)
    assert a.image is b.image
    assert not a.image.flags.writeable            # cached arrays are shared: read-only
    c = engine.load_source(jpeg, 100)
    assert c.image.shape != a.image.shape


# --------------------------------------------------------------------------- raw path
def test_raw_path_fake_npz(raw, scene):
    out, ctx = engine.render(raw, engine.default_stack("raw"), return_ctx=True)
    assert ctx.kind == "raw"
    assert out.shape[:2] == scene.shape[:2]
    assert 0.05 < out.mean() < 0.9
    corr = np.corrcoef(out[..., 1].ravel(), scene[..., 1].ravel())[0, 1]
    assert corr > 0.9                                   # same picture, different exposure and tone map


def test_raw_cr3_extension_uses_patched_opener(cr3_lib):
    out = engine.render(cr3_lib / "shot.CR3", engine.default_stack("raw"), long_edge=240)
    assert max(out.shape[:2]) == 240 and out.mean() > 0.02


def test_raw_half_size_preview_is_smaller(raw):
    small = engine.load_source(raw, 160)
    full = engine.load_source(raw)
    assert max(small.image.shape[:2]) <= max(full.image.shape[:2])


# --------------------------------------------------------------------------- merge order
def test_merge_order_defaults_mode_art_user():
    st = engine.resolve_stack("portrait", "standard", [E("exposure", ev=0.9)], kind="raw")
    ev = [e for e in st if e["op"] == "exposure" and not e.get("mask")]
    assert len(ev) == 1 and ev[0]["params"]["ev"] == 0.9             # user beats mode (+0.15)
    mode_only = engine.resolve_stack("portrait", None, None, kind="raw")
    assert [e for e in mode_only if e["op"] == "exposure"][0]["params"]["ev"] == 0.15
    names = [e["op"] for e in st]
    assert "tone_map" in names                                        # pipeline default survives
    sat = [e for e in st if e["op"] == "saturation"][0]
    assert sat["params"]["amount"] == 6                               # art layer applied


def test_merge_key_includes_id_and_masked_entries_append():
    user = [{"op": "exposure", "params": {"ev": -0.5}, "mask": {"type": "linear", "p0": [0.5, 0], "p1": [0.5, 1]}},
            {"op": "exposure", "id": "second", "params": {"ev": 0.2}}]
    st = engine.resolve_stack(None, None, user, kind="jpeg")
    ev = [e for e in st if e["op"] == "exposure"]
    assert len(ev) == 3                                                # default + masked (appended) + id'd
    assert sum(1 for e in ev if e.get("mask")) == 1
    st2 = engine.resolve_stack(None, None, user, kind="jpeg")
    assert st == st2


def test_raw_only_ops_dropped_for_jpeg():
    reg = registry.all_ops()
    raw_only = [n for n, s in reg.items() if s.raw_only]
    assert raw_only                                                    # e.g. 'raw', hot_pixels
    st = engine.resolve_stack("night:astro", None, [E(raw_only[0])], kind="jpeg")
    assert not [e for e in st if reg[e["op"]].raw_only]
    st_raw = engine.resolve_stack(None, None, [E(raw_only[0])], kind="raw")
    assert any(e["op"] == raw_only[0] for e in st_raw)


def test_resolve_unknown_mode_raises():
    with pytest.raises(presets.PresetError):
        engine.resolve_stack("no_such_mode", kind="jpeg")


def test_variant_resolves_and_overrides_base():
    st = engine.resolve_stack("night:astro", None, None, kind="raw")
    ev = [e for e in st if e["op"] == "exposure"][0]
    assert ev["params"]["ev"] == 1.2                                   # variant beats base (0.5)
    base = engine.resolve_stack("night", None, None, kind="raw")
    assert [e for e in base if e["op"] == "exposure"][0]["params"]["ev"] == 0.5


def test_apply_set_grammar():
    st = engine.default_stack("jpeg")
    out = engine.apply_set(st, ["exposure.ev=+0.3", "contrast.contrast=0.1", "vignette@v1.amount=-0.4"])
    d = {(e["op"], e.get("id")): e for e in out}
    assert d[("exposure", None)]["params"]["ev"] == 0.3
    assert d[("contrast", None)]["params"]["contrast"] == 0.1
    assert d[("vignette", "v1")]["params"]["amount"] == -0.4
    masked = engine.apply_set(st, ["exposure.ev=-1"], mask={"type": "linear", "p0": [0.5, 0], "p1": [0.5, 1]})
    assert any(e["op"] == "exposure" and e.get("mask") for e in masked)


# --------------------------------------------------------------------------- masks
LIN = {"type": "linear", "p0": [0.5, 0.35], "p1": [0.5, 0.65], "feather": 0.0}


def _top_bottom(img):
    h = img.shape[0]
    return img[: h // 5].mean(), img[-h // 5:].mean()


def _masked_exposure_stack():
    return [E("exposure", ev=0.0), {"op": "exposure", "id": "grad", "params": {"ev": -2.0}, "mask": dict(LIN)}]


def test_mask_darkens_only_masked_region(jpeg):
    """Linear mask: full effect on the p0 side (top), none on the p1 side (bottom)."""
    plain = engine.render(jpeg, [E("exposure", ev=0.0)], use_cache=False)
    masked = engine.render(jpeg, _masked_exposure_stack(), use_cache=False)
    mt, mb = _top_bottom(masked)
    pt, pb = _top_bottom(plain)
    assert pt - mt > 0.05
    assert abs(pb - mb) < 0.01


def test_mask_fallback_without_masks_package(jpeg, monkeypatch):
    """The engine must still apply linear masks when photostudio.masks is unavailable."""
    monkeypatch.setitem(sys.modules, "photostudio.masks", None)
    stack = [E("exposure", ev=0.0), {"op": "exposure", "id": "g", "params": {"ev": -2.0},
                                     "mask": {"type": "linear", "x0": 0.5, "y0": 0.35, "x1": 0.5, "y1": 0.65}}]
    plain = engine.render(jpeg, [E("exposure", ev=0.0)], use_cache=False)
    masked = engine.render(jpeg, stack, use_cache=False)
    assert np.abs(masked - plain).max() > 0.05


def test_mask_stub_module_is_used(jpeg, monkeypatch):
    """A stub photostudio.masks with build_mask/blend is honoured (contract with the masks owner)."""
    import types

    calls = {"n": 0}
    stub = types.ModuleType("photostudio.masks")

    def build_mask(spec, img, ctx):
        calls["n"] += 1
        m = np.zeros(img.shape[:2], np.float32)
        m[:, : img.shape[1] // 2] = 1.0
        return m

    def blend(base, edited, mask, mode="normal", opacity=1.0, domain="linear"):
        return base + (edited - base) * mask[..., None] * opacity

    stub.build_mask, stub.blend = build_mask, blend
    monkeypatch.setitem(sys.modules, "photostudio.masks", stub)
    out = engine.render(jpeg, [{"op": "exposure", "params": {"ev": -2.0}, "mask": {"type": "stub"}}], use_cache=False)
    plain = engine.render(jpeg, [], use_cache=False)
    assert calls["n"] >= 1
    w = out.shape[1]
    assert out[:, : w // 4].mean() < plain[:, : w // 4].mean() - 0.1
    assert abs(out[:, -w // 4:].mean() - plain[:, -w // 4:].mean()) < 1e-3


def test_mask_opacity_zero_is_noop(jpeg):
    st = [{"op": "exposure", "params": {"ev": -2.0}, "mask": dict(LIN), "opacity": 0.0}]
    a = engine.render(jpeg, st, use_cache=False)
    b = engine.render(jpeg, [], use_cache=False)
    assert np.abs(a - b).max() < 1e-4


def test_premask_with_geometry_pulls_mask_back(jpeg):
    """The mask lives in post-geometry coordinates: after a vertical flip the darkened band is the *bottom* of the
    source (it is the top of the flipped output), although exposure runs before the flip."""
    base = [{"op": "exposure", "id": "g", "params": {"ev": -2.0}, "mask": dict(LIN)}]
    plain = engine.render(jpeg, [], use_cache=False)
    unflipped = engine.render(jpeg, base, use_cache=False)
    flipped = engine.render(jpeg, base + [E("flip", vertical=True)], use_cache=False)[::-1]     # back to source view
    n = plain.shape[0] // 5
    d_un = np.abs(unflipped - plain).mean(axis=(1, 2))
    d_fl = np.abs(flipped - plain).mean(axis=(1, 2))
    assert d_un[:n].mean() > 0.1 and d_un[-n:].mean() < 0.01
    assert d_fl[-n:].mean() > 0.05 and d_fl[:n].mean() < 0.02


# --------------------------------------------------------------------------- geometry & caching
def test_geometry_changes_size(jpeg):
    out = engine.render(jpeg, [E("crop", rect=[0.0, 0.0, 0.5, 1.0])], use_cache=False)
    assert out.shape[:2] == (320, 240)


def test_step_cache_resumes_prefix(jpeg):
    stack = [E("exposure", ev=0.2), E("contrast", contrast=0.1)]
    _, c1 = engine.render(jpeg, stack, long_edge=240, return_ctx=True)
    assert c1.cache["render_stats"]["resumed_from"] == 0
    stack2 = [E("exposure", ev=0.2), E("contrast", contrast=0.1), E("saturation", amount=10)]
    out2, c2 = engine.render(jpeg, stack2, long_edge=240, return_ctx=True)
    stats = c2.cache["render_stats"]
    assert stats["resumed_from"] > 0 and stats["executed"] < len(stack2) + 2
    out3 = engine.render(jpeg, stack2, long_edge=240, use_cache=False)
    assert np.allclose(out2, out3, atol=1e-5)                            # cached path == fresh path


def test_cached_result_not_mutated_by_caller(jpeg):
    stack = [E("exposure", ev=0.2)]
    a = engine.render(jpeg, stack, long_edge=240)
    a[:] = 0.0
    b = engine.render(jpeg, stack, long_edge=240)
    assert b.mean() > 0.05


def test_param_change_invalidates_cache(jpeg):
    a = engine.render(jpeg, [E("exposure", ev=0.0)], long_edge=240)
    b = engine.render(jpeg, [E("exposure", ev=1.5)], long_edge=240)
    assert b.mean() > a.mean() + 0.05


# --------------------------------------------------------------------------- tolerance
def test_unknown_op_is_skipped_with_note(jpeg):
    out, ctx = engine.render(jpeg, [E("definitely_not_an_op", x=1), E("exposure", ev=0.5)], return_ctx=True, use_cache=False)
    assert out.shape[2] == 3
    assert any("definitely_not_an_op" in n for n in ctx.log)


def test_disabled_op_is_ignored(jpeg):
    a = engine.render(jpeg, [{"op": "exposure", "params": {"ev": 2.0}, "enabled": False}], use_cache=False)
    b = engine.render(jpeg, [], use_cache=False)
    assert np.allclose(a, b, atol=1e-6)


def test_bad_param_value_is_skipped_with_a_warning(jpeg):
    """A stored bad value never makes the image unrenderable: the entry is skipped and a warning is logged."""
    out, ctx = engine.render(jpeg, [E("perspective", mode="nonsense"), E("exposure", ev=0.5)], use_cache=False,
                             return_ctx=True)
    assert out.ndim == 3
    assert any("perspective" in n and "skipped" in n for n in ctx.log)
    plain = engine.render(jpeg, [E("exposure", ev=0.5)], use_cache=False)
    assert np.allclose(out, plain, atol=1e-6)               # the good entry still applied


def test_output_space_p3_differs_from_srgb(jpeg):
    a = engine.render(jpeg, [], output_space="srgb", use_cache=False)
    b = engine.render(jpeg, [], output_space="p3", use_cache=False)
    assert np.abs(a - b).max() > 0.005


# --------------------------------------------------------------------------- LUT bake
def test_bake_lut_identity_and_exposure():
    ident = engine.bake_lut([], size=9)
    assert ident.shape == (9, 9, 9, 3)
    g = np.linspace(0, 1, 9)
    assert abs(ident[0, 0, 8, 0] - 1.0) < 0.05 and abs(ident[8, 0, 0, 2] - 1.0) < 0.05   # [b][g][r]
    assert np.abs(ident[4, 4, 4] - g[4]).max() < 0.05
    bright = engine.bake_lut([E("exposure", ev=1.0)], size=9)
    assert bright[3, 3, 3].mean() > ident[3, 3, 3].mean() + 0.05


def test_bake_lut_skips_spatial_ops(tmp_path):
    skipped: list[str] = []
    lut = engine.bake_lut([E("exposure", ev=0.5), E("vignette", amount=-0.5), E("sharpen", amount=1.0)], size=5,
                          skipped=skipped)
    assert "vignette" in skipped and "sharpen" in skipped and "exposure" not in skipped
    assert lut.shape == (5, 5, 5, 3)
    cube = engine.write_cube(tmp_path / "x.cube", lut)
    text = cube.read_text()
    assert "LUT_3D_SIZE 5" in text and len(text.splitlines()) >= 5 ** 3
    back = engine.apply_lut3d(np.full((4, 4, 3), 0.5, np.float32), lut)
    assert back.shape == (4, 4, 3)


# --------------------------------------------------------------------------- render_file + sidecar
def test_render_file_uses_sidecar(jpeg):
    from photostudio import sidecar

    doc = sidecar.load(jpeg)
    sidecar.set_stack(doc, engine.apply_set(engine.default_stack("jpeg"), ["exposure.ev=-1.5"]))
    sidecar.save(jpeg, doc)
    dark = engine.render_file(jpeg, long_edge=240)
    plain = engine.render(jpeg, engine.default_stack("jpeg"), long_edge=240)
    assert dark.mean() < plain.mean() - 0.05


# --------------------------------------------------------------------------- presets
def test_all_shipped_presets_validate():
    total = 0
    for kind in ("mode", "export", "picture_style"):
        names = [p["name"] for p in presets.list_presets(kind)]
        assert names
        for n in names:
            d = presets.load_preset(kind, n)
            assert presets.validate_preset(d) == [], (kind, n)
            for v in d.get("variants") or {}:
                assert presets.validate_preset(presets.load_preset(kind, f"{n}:{v}")) == [], (kind, n, v)
            total += 1
    assert total >= 25


def test_expected_modes_and_export_presets_exist():
    modes_ = {p["name"] for p in presets.list_presets("mode")}
    assert {"portrait", "animal", "landscape", "night", "macro", "street", "product", "sports", "architecture",
            "events", "video", "auto"} <= modes_
    exp = {p["name"] for p in presets.list_presets("export")}
    assert {"web", "instagram_portrait", "instagram_square", "story_9x16", "print_tiff16", "archive_full",
            "wallpaper_4k", "email_small"} <= exp
    ig = presets.load_preset("export", "instagram_portrait")["options"]
    assert (ig["width"], ig["height"]) == (1080, 1350)


def test_validate_catches_problems():
    bad = {"name": "x", "kind": "mode", "stack": [{"op": "nope"}, {"op": "exposure", "params": {"zzz": 1}},
                                                   {"op": "exposure", "mask": {"foo": 1}}]}
    probs = presets.validate_preset(bad)
    assert len(probs) >= 3


def test_user_preset_save_and_load():
    stack = [E("exposure", ev=0.33)]
    path = presets.save_user_preset("art", "my_look", {"title": "Mine", "stack": stack})
    assert path.exists()
    d = presets.load_preset("art", "my_look")
    assert d["stack"][0]["params"]["ev"] == 0.33
    assert any(p["name"] == "my_look" for p in presets.list_presets("art"))


@pytest.mark.parametrize("mode", [p for p in ["portrait", "animal:birds", "landscape", "night:astro", "night:moon", "macro",
                                              "street", "street:color", "product:food", "sports", "architecture",
                                              "events:travel", "video", "auto"]])
def test_every_mode_renders_on_jpeg_and_raw(mode, jpeg, raw):
    for f in (jpeg, raw):
        st = engine.resolve_stack(mode, None, None, kind=engine.source_kind(f))
        out = engine.render(f, st, long_edge=200)
        assert out.shape[2] == 3 and np.isfinite(out).all() and 0.0 <= out.min() and out.max() <= 1.0 + 1e-6
        assert out.std() > 0.01


def test_picture_styles_render(jpeg):
    for name in ("standard", "portrait", "landscape", "neutral", "faithful", "monochrome", "monochrome:sepia"):
        st = engine.resolve_stack(None, name, None, kind="jpeg")
        out = engine.render(jpeg, st, long_edge=160)
        assert np.isfinite(out).all()
    mono = engine.render(jpeg, engine.resolve_stack(None, "monochrome", None, kind="jpeg"), long_edge=160)
    assert np.abs(mono[..., 0] - mono[..., 1]).mean() < 0.02          # monochrome style is grey


def test_street_default_is_bw_and_color_variant_is_not(jpeg):
    bw = engine.render(jpeg, engine.resolve_stack("street", None, None, kind="jpeg"), long_edge=200)
    col = engine.render(jpeg, engine.resolve_stack("street:color", None, None, kind="jpeg"), long_edge=200)
    assert np.abs(bw[..., 0] - bw[..., 2]).mean() < 0.03
    assert np.abs(col[..., 0] - col[..., 2]).mean() > 0.03


# --------------------------------------------------------------------------- mode detection
def meta(**kw):
    base = {"make": "Canon", "model": "Canon EOS R50", "iso": 200.0, "shutter": 1 / 250, "aperture": 8.0, "focal": 24.0,
            "datetime": "2026-11-12T10:00:00"}
    base.update(kw)
    return base


def test_detect_mode_returns_contract_keys():
    d = modes.detect_mode(meta(), None)
    assert {"mode", "confidence", "reasons"} <= set(d)
    assert d["mode"] in modes.MODE_NAMES and 0.0 <= d["confidence"] <= 1.0


def test_detect_night_long_exposure():
    d = modes.detect_mode(meta(shutter=20.0, iso=3200.0, aperture=2.8, focal=18.0, datetime="2026-11-12T23:10:00"), None)
    assert d["mode"] == "night"
    assert d.get("variant") in ("astro", "blue_hour", "moon", None)


def test_detect_macro_tag():
    d = modes.detect_mode(meta(macro_mode=1, MacroMode=1, focus_distance=0.2), None)
    assert d["mode"] in ("macro", "events", "product", "landscape") and d["reasons"]


def test_detect_sports_burst_servo():
    d = modes.detect_mode(meta(shutter=1 / 2000, iso=1600.0, focal=150.0, drive="continuous", ContinuousDrive=1,
                               focus_mode="AI Servo", FocusMode=2, continuous_drive=1), None)
    assert d["mode"] in ("sports", "animal", "events")


def test_mode_preset_name_and_auto_mode_in_resolve(jpeg):
    d = {"mode": "night", "variant": "astro"}
    assert modes.mode_preset_name(d) == "night:astro"
    st, info = engine.resolve_stack_ex("auto", None, None, "jpeg", meta())
    assert info["mode"] is not None and "auto" in info
    assert isinstance(st, list) and st


def test_picture_style_fit_recovers_exposure_curve():
    from photostudio.picture_style import fit_tone_curve

    x = np.linspace(0.0, 1.0, 400)
    y = x ** 0.8
    pts = fit_tone_curve(x, y, n_points=9)
    pts = np.asarray(pts)
    assert pts.shape[1] == 2
    assert np.abs(np.interp(0.5, pts[:, 0], pts[:, 1]) - 0.5 ** 0.8) < 0.03
