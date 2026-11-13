"""Mask geometry, parametric selectivity, combine algebra, refinement, blend identities, AI fallbacks."""
import numpy as np
import pytest

from photostudio import color, models
from photostudio.masks import (BLEND_MODES, blend, build_mask, combine_masks, mask_preview, normalise_spec,
                               refine_mask, trapezoid)
from photostudio.pipeline.registry import Ctx
from photostudio.testing import make_scene

H, W = 200, 300


@pytest.fixture(scope="module")
def scene():
    return make_scene(480, 320)


def dctx(**kw):
    return Ctx(domain="display", **kw)


def blank(h=H, w=W, v=0.5):
    return np.full((h, w, 3), v, np.float32)


# --------------------------------------------------------------------------- drawn
def test_linear_gradient_monotonic_and_range():
    m = build_mask({"type": "linear", "p0": [0.2, 0.5], "p1": [0.8, 0.5]}, blank(), dctx())
    assert m.shape == (H, W) and m.dtype == np.float32
    row = m[H // 2]
    assert row[0] == pytest.approx(1.0) and row[-1] == pytest.approx(0.0)
    assert np.all(np.diff(row) <= 1e-6)                    # monotonic non-increasing
    assert np.allclose(m, m[:1], atol=1e-6)                # constant along the perpendicular
    assert row[int(0.2 * W) - 2] > 0.99 and row[int(0.8 * W) + 2] < 0.01
    assert 0.45 < row[W // 2] < 0.55


def test_linear_gradient_diagonal_and_halfplane_feather():
    m = build_mask({"type": "linear", "p0": [0.0, 0.0], "p1": [1.0, 1.0]}, blank(), dctx())
    assert m[0, 0] > 0.9 and m[-1, -1] < 0.1
    # degenerate: half plane at x=0.5 facing +x (mask=1 for x>0.5?) angle 0 -> normal +x: t grows with x -> mask falls
    spec = {"type": "linear", "p0": [0.5, 0.5], "angle": 0, "feather": 60}
    img = blank(100, 2000)
    mm = build_mask(spec, img, dctx())[50]
    wid = int(((mm > 0.02) & (mm < 0.98)).sum())
    assert 50 <= wid <= 62                                 # ramp is ~feather px wide (2000 px image: scale 1)


def test_radial_inside_outside_feather_invert():
    spec = {"type": "radial", "center": [0.5, 0.5], "radii": [0.2, 0.2], "feather": 0}
    img = blank(400, 400)
    m = build_mask(spec, img, dctx())
    assert m[200, 200] == pytest.approx(1.0) and m[200, 130] > 0.99      # inside 0.2*400=80 px
    assert m[200, 290] < 0.01 and m[5, 5] == 0
    f = build_mask({**spec, "feather": 100}, img, dctx())               # 100@2000 = 20 px at 400
    ramp = f[200, 280:310]
    assert np.all(np.diff(ramp) <= 1e-6) and ramp[0] > 0.9 and ramp[-1] < 0.05
    width = int(((f[200, 200:] > 0.02) & (f[200, 200:] < 0.98)).sum())
    assert 14 <= width <= 21
    inv = build_mask({**spec, "invert": True}, img, dctx())
    assert np.allclose(inv, 1.0 - m, atol=1e-6)
    op = build_mask({**spec, "opacity": 0.4}, img, dctx())
    assert op.max() == pytest.approx(0.4, abs=1e-5)


def test_radial_ellipse_rotation():
    img = blank(300, 300)
    a = build_mask({"type": "ellipse", "center": [0.5, 0.5], "radii": [0.4, 0.1]}, img, dctx())
    b = build_mask({"type": "ellipse", "center": [0.5, 0.5], "radii": [0.4, 0.1], "rotation": 90}, img, dctx())
    assert a[150, 60] > 0.9 and a[60, 150] < 0.1
    assert b[60, 150] > 0.9 and b[150, 60] < 0.1


def test_polygon_and_feather():
    sq = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]
    img = blank(400, 400)
    m = build_mask({"type": "polygon", "points": sq}, img, dctx())
    assert m[200, 200] == pytest.approx(1.0) and m[50, 50] == 0 and m[200, 80] == 0.0 and m[200, 120] > 0.99
    assert abs(m.sum() - 200 * 200) < 0.03 * 200 * 200     # area ~ 200x200 px (boundary pixels are inclusive)
    f = build_mask({"type": "polygon", "points": sq, "feather": 100}, img, dctx())
    assert f.sum() > m.sum() and f[200, 200] == pytest.approx(1.0) and f[200, 5] < 1e-3
    row = f[200, 60:100]
    assert np.all(np.diff(row) >= -1e-6)
    # bezier handles / smooth spline stay inside the control hull region
    sm = build_mask({"type": "path", "smooth": True, "points": sq}, img, dctx())
    assert sm[200, 200] == pytest.approx(1.0) and 0.8 * m.sum() < sm.sum() < 1.5 * m.sum()
    bz = build_mask({"type": "polygon", "points": [{"p": [0.2, 0.5], "out": [0.2, 0.1]},
                                                   {"p": [0.8, 0.5], "in": [0.8, 0.1]}]}, img, dctx())
    assert 0 < bz.sum() < 400 * 400 * 0.2


def test_brush_strokes_paint_and_erase():
    img = blank(300, 300)
    spec = {"type": "brush", "strokes": [{"points": [[0.2, 0.5], [0.8, 0.5]], "radius": 150, "hardness": 0.8}]}
    m = build_mask(spec, img, dctx())
    assert m[150, 150] > 0.99 and m[150, 60] > 0.99 and m[20, 150] == 0
    spec["strokes"].append({"points": [[0.5, 0.5]], "radius": 150, "hardness": 1.0, "erase": True})
    e = build_mask(spec, img, dctx())
    assert e[150, 150] < 0.05 and e[150, 60] > 0.99
    half = build_mask({"type": "brush", "strokes": [{"points": [[0.5, 0.5]], "radius": 100, "flow": 0.5}]}, img, dctx())
    assert half.max() == pytest.approx(0.5, abs=1e-3)


# --------------------------------------------------------------------------- parametric / luminosity / colour
def chart():
    cols = {"red": (0.8, 0.1, 0.1), "green": (0.1, 0.7, 0.15), "blue": (0.1, 0.2, 0.85), "yellow": (0.9, 0.85, 0.1),
            "grey": (0.5, 0.5, 0.5), "white": (0.95, 0.95, 0.95), "black": (0.04, 0.04, 0.04), "skin": (0.88, 0.66, 0.55)}
    img = np.zeros((40, 40 * len(cols), 3), np.float32)
    idx = {}
    for i, (n, c) in enumerate(cols.items()):
        img[:, i * 40:(i + 1) * 40] = c
        idx[n] = slice(i * 40 + 8, i * 40 + 32)
    return img, idx


def test_parametric_hue_selectivity_and_polarity():
    img, idx = chart()
    lch = color.to_oklch(img, "display")
    h_red = float(lch[20, 20, 2])
    spec = {"type": "parametric", "channels": {"h": [h_red - 25, h_red - 10, h_red + 10, h_red + 25], "C": [0.05, 0.1, 0.4, 0.5]}}
    m = build_mask(spec, img, dctx())
    assert m[20, idx["red"]].min() > 0.95
    for n in ("green", "blue", "grey", "white", "black"):
        assert m[20, idx[n]].max() < 0.05, n
    inv = build_mask({**spec, "channels": {"h": {"range": spec["channels"]["h"], "invert": True}}}, img, dctx())
    assert inv[20, idx["red"]].max() < 0.05 and inv[20, idx["green"]].min() > 0.95


def test_trapezoid_ramps_and_hue_wrap():
    v = np.linspace(0, 1, 101).astype(np.float32)
    t = trapezoid(v, (0.2, 0.4, 0.6, 0.8))
    assert t[40] == pytest.approx(1) and t[60] == pytest.approx(1) and t[10] == 0 and t[90] == 0 and 0.4 < t[30] < 0.6
    h = np.array([350.0, 5.0, 180.0], np.float32)
    w = trapezoid(h, (330, 345, 15, 30), circular=True)     # wraps through 0
    assert w[0] == 1 and w[1] == 1 and w[2] == 0


def test_parametric_luminance_ev_on_linear_data():
    ramp = np.geomspace(0.18 / 64, 0.18 * 8, 256).astype(np.float32)
    img = np.repeat(ramp[None, :, None], 8, 0).repeat(3, 2)
    c = Ctx(domain="linear")
    m = build_mask({"type": "parametric", "channels": {"ev": [-3.5, -3, -1, -0.5]}}, img, c)[0]
    ev = np.log2(ramp / 0.18)
    assert m[(ev > -2.9) & (ev < -1.1)].min() > 0.99 and m[ev > 0].max() < 0.01 and m[ev < -4].max() < 0.01


def test_luminosity_masks():
    ramp = np.linspace(0, 1, 256, dtype=np.float32)
    img = np.repeat(ramp[None, :, None], 4, 0).repeat(3, 2)
    L1 = build_mask({"type": "luminosity", "mode": "lights", "level": 1}, img, dctx())[0]
    L3 = build_mask({"type": "luminosity", "mode": "lights", "level": 3}, img, dctx())[0]
    D1 = build_mask({"type": "luminosity", "mode": "darks"}, img, dctx())[0]
    M1 = build_mask({"type": "luminosity", "mode": "midtones"}, img, dctx())[0]
    assert np.all(np.diff(L1) >= -1e-6) and np.all(np.diff(D1) <= 1e-6)
    assert np.all(L3 <= L1 + 1e-6) and L3.mean() < L1.mean()          # higher level = narrower
    assert np.allclose(L1 + D1, 1.0, atol=1e-5)
    assert M1[128] > 0.98 and M1[0] < 0.01 and M1[-1] < 0.01
    # shorthand and linear domain
    assert build_mask("lights", img, dctx()).shape == (4, 256)
    lin = build_mask({"type": "luminosity", "mode": "darks"}, color.srgb_eotf(img), Ctx(domain="linear"))[0]
    assert lin[0] > 0.95 and lin[-1] < 0.05


def test_color_range():
    img, idx = chart()
    m = build_mask({"type": "color_range", "color": "#cc1a1a", "tolerance": 6, "falloff": 6}, img, dctx())
    assert m[20, idx["red"]].min() > 0.95 and m[20, idx["blue"]].max() < 0.01 and m[20, idx["green"]].max() < 0.01
    # picked point + hue-only weighting selects dark and light version of the same hue
    img2 = img.copy()
    img2[:, :40] = (0.4, 0.05, 0.05)
    p = build_mask({"type": "color_range", "point": [20 / 320, 0.5], "lightness_weight": 0.0, "tolerance": 12,
                    "falloff": 8}, img2, dctx())
    assert p[20, 20] > 0.9 and p[20, idx["red"]].min() > 0.9 and p[20, idx["blue"]].max() < 0.05
    m2 = build_mask({"type": "color_range", "color": [0.8, 0.1, 0.1], "metric": "de2000", "tolerance": 8, "falloff": 8},
                    img, dctx())
    assert m2[20, idx["red"]].min() > 0.95 and m2[20, idx["green"]].max() < 0.05


# --------------------------------------------------------------------------- combine
def test_combine_algebra():
    img = blank(100, 100)
    a = {"type": "polygon", "points": [[0, 0], [0.6, 0], [0.6, 1], [0, 1]]}
    b = {"type": "polygon", "points": [[0.4, 0], [1, 0], [1, 1], [0.4, 1]]}
    A, B = build_mask(a, img, dctx()), build_mask(b, img, dctx())
    run = lambda mode: build_mask({"type": "combine", "mode": mode, "masks": [a, b]}, img, dctx())  # noqa: E731
    assert np.allclose(run("union"), np.maximum(A, B))
    assert np.allclose(run("intersect"), np.minimum(A, B))
    assert np.allclose(run("subtract"), A * (1 - B), atol=1e-6)
    assert np.allclose(run("exclude"), np.maximum(A, B) - np.minimum(A, B), atol=1e-6)
    assert run("union")[50].min() > 0.99 and run("intersect")[50, 50] > 0.99 and run("intersect")[50, 5] < 0.01
    assert run("subtract")[50, 10] > 0.99 and run("subtract")[50, 50] < 0.01
    # nested + per-operand invert
    nested = build_mask({"type": "combine", "mode": "intersect",
                         "masks": [{"type": "combine", "mode": "union", "masks": [a, b]}, {**a, "invert": True}]}, img, dctx())
    assert np.allclose(nested, np.minimum(np.maximum(A, B), 1 - A), atol=1e-6)
    assert np.all(combine_masks([A, B], "union", soft=True) >= np.maximum(A, B) - 1e-6)
    with pytest.raises(ValueError):
        combine_masks([A, B], "nonsense")


# --------------------------------------------------------------------------- refine / common
def test_refine_range_and_edge_snapping():
    img = np.zeros((120, 200, 3), np.float32)
    img[:, 100:] = 0.9                                         # image edge at x=100
    spec = {"type": "linear", "p0": [80 / 200, 0.5], "p1": [120 / 200, 0.5]}   # soft ramp 80..120 straddling it
    raw = build_mask(spec, img, dctx())
    ref = build_mask({**spec, "refine": {"guided_radius": 120, "guided_eps": 1e-4}}, img, dctx())
    assert ref.min() >= 0 and ref.max() <= 1 and ref.dtype == np.float32

    g_raw, g_ref = np.abs(np.diff(raw[60])), np.abs(np.diff(ref[60]))
    assert abs(int(np.argmax(g_ref)) - 100) <= 2                 # steepest part of the refined mask sits on the image edge
    assert g_ref.max() > 5 * g_raw.max()                         # and it is much sharper than the raw ramp
    ref2 = build_mask({**spec, "refine": {"guided_radius": 120, "guided_eps": 1e-4, "blur": 1.0, "contrast": 0.2}}, img, dctx())
    assert ref2.min() >= 0 and ref2.max() <= 1
    for c in (-1.0, -0.5, 0.5, 1.0):
        r = refine_mask(np.linspace(0, 1, 50, dtype=np.float32)[None].repeat(5, 0), {"contrast": c}, img[:5, :50])
        assert r.min() >= 0 and r.max() <= 1
        assert r[0, 0] == 0 and r[0, -1] == 1
    soft = refine_mask(np.full((4, 4), 0.5, np.float32), {"contrast": 1.0}, img[:4, :4])
    assert 0.0 <= soft.min() and soft.max() <= 1.0


def test_spec_normalisation_and_errors():
    assert normalise_spec("sky")["type"] == "ai_sky"
    assert normalise_spec({"type": "subtract", "masks": []})["mode"] == "subtract"
    assert normalise_spec("lights")["type"] == "luminosity"
    assert build_mask(None, blank(), dctx()).min() == 1.0
    assert build_mask({"type": "none"}, blank(), dctx()).max() == 0.0
    with pytest.raises(ValueError):
        build_mask({"type": "bogus"}, blank(), dctx())
    m = build_mask({"type": "luminosity", "mode": "lights", "feather": 40}, make_scene(120, 80), dctx())
    assert m.shape == (80, 120)


# --------------------------------------------------------------------------- blend
@pytest.mark.parametrize("mode", sorted(BLEND_MODES))
@pytest.mark.parametrize("domain", ["display", "linear"])
def test_blend_identities(mode, domain):
    rng = np.random.default_rng(3)
    base = rng.random((20, 30, 3)).astype(np.float32) * (1.0 if domain == "display" else 1.5)
    edited = rng.random((20, 30, 3)).astype(np.float32)
    mask = rng.random((20, 30)).astype(np.float32)
    assert np.array_equal(blend(base, edited, mask, mode, 0.0, domain), base)            # opacity 0
    assert np.array_equal(blend(base, edited, np.zeros((20, 30), np.float32), mode, 1.0, domain), base)   # mask 0
    out = blend(base, edited, mask, mode, 0.7, domain)
    assert out.shape == base.shape and out.dtype == np.float32 and np.isfinite(out).all()
    if domain == "display":
        assert out.min() >= -1e-5 and out.max() <= 1 + 1e-5 or mode in ("normal", "add", "subtract")


def test_blend_normal_is_edited_and_partial_alpha():
    rng = np.random.default_rng(4)
    base = rng.random((10, 10, 3)).astype(np.float32)
    edited = rng.random((10, 10, 3)).astype(np.float32)
    assert np.array_equal(blend(base, edited, None, "normal", 1.0, "linear"), edited)
    half = blend(base, edited, np.full((10, 10), 0.5, np.float32), "normal", 0.5, "display")
    assert np.allclose(half, base * 0.75 + edited * 0.25, atol=1e-6)


def test_blend_mode_formulas():
    a = np.full((2, 2, 3), 0.4, np.float32)
    b = np.full((2, 2, 3), 0.6, np.float32)
    assert np.allclose(blend(a, b, None, "multiply", 1, "display"), 0.24)
    assert np.allclose(blend(a, b, None, "screen", 1, "display"), 1 - 0.6 * 0.4)
    assert np.allclose(blend(a, b, None, "overlay", 1, "display"), 2 * 0.4 * 0.6)
    assert np.allclose(blend(a, b, None, "darken", 1, "display"), 0.4)
    assert np.allclose(blend(a, b, None, "lighten", 1, "display"), 0.6)
    assert np.allclose(blend(a, b, None, "difference", 1, "display"), 0.2)
    assert np.allclose(blend(a, b, None, "hard_light", 1, "display"), 1 - 2 * 0.6 * 0.4)
    # linear: multiply with 0.18 fulcrum (a*b/0.18); screen falls back to normal
    assert np.allclose(blend(a, b, None, "multiply", 1, "linear"), 0.4 * 0.6 / 0.18)
    assert np.allclose(blend(a, b, None, "screen", 1, "linear"), b)
    assert np.allclose(blend(a, b, None, "add", 1, "linear"), 1.0)
    # channel modes: luminosity of grey edited over a red base keeps hue/chroma of base, takes L of edited
    red = np.zeros((2, 2, 3), np.float32)
    red[...] = (0.6, 0.35, 0.35)
    grey = np.full((2, 2, 3), 0.5, np.float32)
    lum = blend(red, grey, None, "luminosity", 1, "display")
    L0, L1 = color.to_oklch(red, "display"), color.to_oklch(lum, "display")
    assert abs(L1[0, 0, 0] - color.to_oklch(grey, "display")[0, 0, 0]) < 0.03 and L1[0, 0, 2] == pytest.approx(L0[0, 0, 2], abs=3)
    hue = blend(grey, red, None, "color", 1, "display")
    assert color.to_oklch(hue, "display")[0, 0, 1] > 0.05
    # identity for channel modes when edited == base
    scene = make_scene(40, 30)
    for m in ("luminosity", "chroma", "hue", "color"):
        assert np.allclose(blend(scene, scene, None, m, 1, "display"), scene, atol=2e-3)
    with pytest.raises(ValueError):
        blend(a, b, None, "nope")


# --------------------------------------------------------------------------- AI masks (fallbacks)
def test_ai_sky_fallback(scene):
    c = dctx()
    m = build_mask("sky", scene, c)
    h, w = m.shape
    assert m[: int(0.2 * h), int(0.35 * w):int(0.7 * w)].mean() > 0.85   # blue sky, top centre
    assert m[int(0.9 * h):].mean() < 0.1                            # water/grass bottom
    assert m[int(0.5 * h):int(0.8 * h), int(0.52 * w):int(0.64 * w)].mean() < 0.2    # building
    assert 0.2 < m.mean() < 0.6
    assert any("heuristic" in n for n in c.log)


def test_ai_subject_background_fallback(scene):
    c = dctx()
    s = build_mask("subject", scene, c)
    b = build_mask("ai_background", scene, c)
    assert 0.02 < s.mean() < 0.9 and s.std() > 0.1 and s.min() >= 0 and s.max() <= 1
    assert np.allclose(s + b, 1.0, atol=1e-5)
    assert any("GrabCut" in n for n in c.log)


def test_ai_face_parts_fallback(scene):
    c = dctx()
    h, w = scene.shape[:2]
    face = build_mask("ai_face", scene, c)
    fy, fx = int(0.45 * h), int(0.25 * w)
    assert face[fy, fx] > 0.9 and face[int(0.9 * h), int(0.9 * w)] < 0.01 and face.mean() < 0.12
    eyes = build_mask("ai_eyes", scene, c)
    assert 0 < eyes.mean() < face.mean() * 0.3 and eyes[: int(0.2 * h)].max() < 0.01
    lips = build_mask("ai_lips", scene, c)
    skin = build_mask("ai_skin", scene, c)
    assert lips.max() > 0.3 and lips.mean() < eyes.mean() * 3
    assert skin[fy, fx] > 0.5
    # skin excludes the eyes
    ey = np.unravel_index(np.argmax(eyes), eyes.shape)
    assert skin[ey] < 0.5
    assert build_mask("ai_teeth", scene, c).shape == (h, w)


def test_ai_skin_without_face_and_animal_depth_fallback():
    img = make_scene(300, 200)
    img[:] = (0.3, 0.5, 0.3)                                  # no face anywhere
    c = dctx()
    assert build_mask("skin", img, c).max() < 0.1
    c2 = dctx()
    a = build_mask("animal", make_scene(300, 200), c2)
    assert a.shape == (200, 300) and any("subject" in n for n in c2.log)
    d = build_mask({"type": "depth", "near": 0.0, "far": 0.4}, make_scene(300, 200), c2)
    assert d.shape == (200, 300) and 0 < d.mean() < 1
    assert any("WARNING depth" in n for n in c2.log)
    # depth: lower part of the frame is nearer in the heuristic
    near = build_mask({"type": "depth", "near": 0.0, "far": 0.35}, make_scene(300, 200), c2)
    assert near[150:].mean() > near[:50].mean()


def test_ai_mask_cache_and_source_independence(scene):
    c = dctx()
    m1 = build_mask("sky", scene, c)
    keys = [k for k in c.cache if k[0] == "ai"]
    assert keys
    n_log = len(c.log)
    m2 = build_mask("sky", scene, c)
    assert np.array_equal(m1, m2) and len(c.log) == n_log        # served from ctx.cache, no recompute
    # with ctx.source the AI mask uses the un-edited source, not the (edited) image passed in
    lin = color.apply_matrix(color.srgb_eotf(scene), color.SRGB_TO_REC2020)
    c3 = Ctx(domain="linear", source=lin)
    a = build_mask("sky", lin, c3)
    b = build_mask("sky", lin * 0.3, Ctx(domain="linear", source=lin))
    assert np.allclose(a, b)


def test_ai_mask_with_refine_feather_invert(scene):
    m = build_mask({"type": "ai_sky", "feather": 30, "invert": True, "opacity": 0.8, "refine": {"guided_radius": 6}},
                   scene, dctx())
    assert m.max() <= 0.8 + 1e-6 and m.min() >= 0


# --------------------------------------------------------------------------- preview / models
def test_mask_preview(scene):
    p = mask_preview({"type": "radial", "center": [0.5, 0.5], "radii": [0.2, 0.2]}, scene, dctx())
    assert p.dtype == np.uint8 and p.shape == scene.shape
    r, g = p[int(0.5 * scene.shape[0]), int(0.5 * scene.shape[1])].astype(int)[:2]
    assert r > g + 60                                           # red overlay in the centre
    pl = mask_preview("sky", color.apply_matrix(color.srgb_eotf(scene), color.SRGB_TO_REC2020), Ctx(domain="linear"))
    assert pl.shape == scene.shape


def test_models_registry_is_lazy_and_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTOSTUDIO_CACHE", str(tmp_path))
    av = models.available()
    assert "isnet-general-use" in av and "face-landmarker" in av and "depth-anything-v2-small" in av
    assert not any(v["installed"] for k, v in av.items() if k != "big-lama")
    assert str(tmp_path) in av["u2net"]["path"]
    assert models.usable("u2net") is False
    with pytest.raises(KeyError):
        models.info("nope")
    from photostudio.util import MissingDependency

    with pytest.raises(MissingDependency):
        models.ort_session("u2net")
    rows = models.listing()
    assert all({"name", "licence", "size_mb", "installed"} <= set(r) for r in rows)
