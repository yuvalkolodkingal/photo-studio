"""Tests for the core ops: tone.py, color_ops.py, detail.py, geometry.py (synthetic data only)."""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from photostudio import color, testing
from photostudio.pipeline.ops import color_ops as C
from photostudio.pipeline.ops import detail as D
from photostudio.pipeline.ops import geometry as G
from photostudio.pipeline.ops import tone as T
from photostudio.pipeline.registry import REGISTRY, Ctx
from photostudio.util import MissingDependency, to_jsonable

MY_MODULES = {"photostudio.pipeline.ops.tone", "photostudio.pipeline.ops.color_ops",
              "photostudio.pipeline.ops.detail", "photostudio.pipeline.ops.geometry"}
MY_OPS = sorted(n for n, s in REGISTRY.items() if s.fn.__module__ in MY_MODULES)


# --------------------------------------------------------------------------- fixtures / helpers
@pytest.fixture(scope="module")
def disp():
    return testing.make_scene(480, 320, seed=3)


@pytest.fixture(scope="module")
def lin(disp):
    return color.apply_matrix(color.srgb_eotf(disp), color.SRGB_TO_REC2020)


def run(name, img, meta=None, kind="raw", domain=None, **params):
    spec = REGISTRY[name]
    p = spec.resolve_params(params)
    ctx = Ctx(domain=domain or ("display" if spec.domain == "display" else "linear"), meta=meta or {}, kind=kind)
    out = spec.fn(img, p, ctx)
    run.ctx = ctx
    return out


def lum(img):
    return color.luminance(img)


def oklch(img, domain="linear"):
    return color.to_oklch(img, domain)


def hue_diff(a, b):
    return (a - b + 180.0) % 360.0 - 180.0


def flat(h, w, rgb):
    return np.ones((h, w, 3), np.float32) * np.asarray(rgb, np.float32)


def check(out, ref_shape=None):
    assert out.dtype == np.float32 and np.isfinite(out).all()
    if ref_shape is not None:
        assert out.shape == ref_shape


# --------------------------------------------------------------------------- registry
def test_all_expected_ops_registered_with_stages():
    exp = {"tone_curve": (18, "display"), "scene_curve": (12, "linear"), "levels": (18, "display"),
           "highlights_shadows": (12, "linear"), "tone_equalizer": (12, "linear"), "contrast": (13, "linear"),
           "local_contrast": (14, "linear"), "dehaze": (12, "linear"), "hdr_tone": (12, "linear"),
           "color_balance": (13, "linear"), "saturation": (13, "linear"), "vibrance": (13, "linear"),
           "hsl_mixer": (13, "linear"), "color_zones": (13, "linear"), "channel_mixer": (13, "linear"),
           "monochrome": (13, "linear"), "color_grade": (18, "display"), "split_tone": (18, "display"),
           "lut3d": (18, "display"), "temperature": (18, "display"), "invert": (18, "display"),
           "denoise": (5, "linear"), "profiled_denoise": (5, "linear"), "hot_pixels": (5, "linear"),
           "capture_sharpen": (6, "linear"), "ca_correct": (7, "linear"), "defringe": (7, "linear"),
           "sharpen": (14, "linear"), "deconvolve": (14, "linear"), "output_sharpen": (21, "display"),
           "lens_correct": (7, "any"), "crop": (11, "any"), "rotate": (11, "any"), "auto_level": (11, "any"),
           "straighten": (11, "any"), "flip": (11, "any"), "perspective": (11, "any"), "resize": (21, "display")}
    for name, (stage, dom) in exp.items():
        assert name in REGISTRY, name
        assert (REGISTRY[name].stage, REGISTRY[name].domain) == (stage, dom), name
    for name in ("crop", "rotate", "auto_level", "straighten", "flip", "perspective", "resize"):
        assert REGISTRY[name].changes_geometry, name
    assert not REGISTRY["lens_correct"].changes_geometry


def test_describe_is_jsonable_and_defaults_resolve():
    for name in MY_OPS:
        spec = REGISTRY[name]
        json.dumps(to_jsonable(spec.describe()))
        spec.resolve_params({})


@pytest.mark.parametrize("name", MY_OPS)
def test_every_op_runs_with_defaults(name, disp, lin):
    spec = REGISTRY[name]
    img = disp if spec.domain == "display" else lin
    out = run(name, img, meta={"ISO": 800, "Make": "Canon", "Model": "Canon EOS R50"})
    check(out)
    if not spec.changes_geometry:
        assert out.shape == img.shape


# --------------------------------------------------------------------------- identity at neutral params
NEUTRAL = {
    "tone_curve": {}, "scene_curve": {}, "levels": {}, "highlights_shadows": {}, "tone_equalizer": {},
    "contrast": {}, "local_contrast": {}, "dehaze": {}, "hdr_tone": {}, "color_balance": {}, "saturation": {},
    "vibrance": {}, "hsl_mixer": {}, "color_zones": {}, "channel_mixer": {}, "color_grade": {},
    "split_tone": {}, "lut3d": {}, "temperature": {}, "denoise": {"luma": 0, "chroma": 0},
    "profiled_denoise": {"luma": 0, "chroma": 0}, "sharpen": {"amount": 0}, "capture_sharpen": {"amount": 0},
    "deconvolve": {"amount": 0}, "defringe": {"amount": 0}, "ca_correct": {}, "output_sharpen": {"amount": 0},
    "lens_correct": {"auto": False}, "crop": {}, "rotate": {}, "auto_level": {"angle": 0.0}, "flip": {},
    "perspective": {}, "resize": {}, "hot_pixels": {"threshold": 0},
}


@pytest.mark.parametrize("name", sorted(NEUTRAL))
def test_neutral_params_are_identity(name, disp, lin):
    spec = REGISTRY[name]
    img = disp if spec.domain == "display" else lin
    out = run(name, img, **NEUTRAL[name])
    assert out.shape == img.shape
    np.testing.assert_allclose(out, img, atol=1e-6, rtol=1e-6)


# --------------------------------------------------------------------------- tone: curves & levels
def test_pchip_curve_is_monotone_without_overshoot():
    pts = [[0, 0], [0.1, 0.02], [0.12, 0.6], [0.5, 0.62], [0.9, 0.98], [1, 1]]   # nasty steps
    lut = T.pchip_curve(pts, 4096)
    assert lut.min() >= -1e-6 and lut.max() <= 1 + 1e-6
    assert np.all(np.diff(lut) >= -1e-6)
    # passes through control points
    f = T.curve_fn(pts)
    np.testing.assert_allclose(f(np.array([p[0] for p in pts])), [p[1] for p in pts], atol=1e-9)


def test_tone_curve_direction_and_modes(disp):
    pts = [[0, 0], [0.5, 0.65], [1, 1]]
    for mode in ("rgb", "luma", "film", "oklab"):
        out = run("tone_curve", disp, points=pts, mode=mode)
        check(out, disp.shape)
        assert out.min() >= 0 and out.max() <= 1 + 1e-6
        assert color.luma(out).mean() > color.luma(disp).mean() + 0.02, mode
    dark = run("tone_curve", disp, points=[[0, 0], [0.5, 0.35], [1, 1]])
    assert dark.mean() < disp.mean()


def test_tone_curve_luma_mode_preserves_chromaticity():
    x = np.array([[[0.6, 0.3, 0.2], [0.2, 0.15, 0.1]]], np.float32)
    out = run("tone_curve", x, points=[[0, 0], [0.4, 0.5], [1, 1]], mode="luma")
    r0 = x[..., 0] / x[..., 1]
    r1 = out[..., 0] / out[..., 1]
    np.testing.assert_allclose(r0, r1, rtol=2e-3)
    # film-like keeps the channel ordering (hue)
    f = run("tone_curve", x, points=[[0, 0], [0.4, 0.5], [1, 1]], mode="film")
    assert np.all(f[..., 0] >= f[..., 1]) and np.all(f[..., 1] >= f[..., 2])


def test_tone_curve_per_channel_only_changes_that_channel():
    x = flat(8, 8, [0.4, 0.4, 0.4])
    out = run("tone_curve", x, red=[[0, 0], [0.5, 0.7], [1, 1]])
    assert out[0, 0, 0] > 0.4 and abs(out[0, 0, 1] - 0.4) < 1e-4 and abs(out[0, 0, 2] - 0.4) < 1e-4


def test_scene_curve_gain_in_ev():
    x = flat(8, 8, [color.MIDDLE_GREY] * 3)
    out = run("scene_curve", x, points=[[-8, -8], [0, 1.0], [4, 5.0]])   # +1 EV at grey
    np.testing.assert_allclose(out[0, 0, 0] / x[0, 0, 0], 2.0, rtol=0.02)
    # far below the nodes identity extension applies
    y = flat(8, 8, [1e-4] * 3)
    np.testing.assert_allclose(run("scene_curve", y, points=[[-8, -8], [0, 1.0], [4, 5.0]]), y, rtol=0.03)


def test_levels(disp):
    out = run("levels", disp, black=0.2, white=0.8)
    assert out.min() >= 0 and out.max() <= 1
    assert color.luma(out).std() > color.luma(disp).std()
    bright = run("levels", disp, gamma=1.6)
    assert bright.mean() > disp.mean()
    auto = run("levels", disp * 0.5 + 0.2, auto=True)
    assert color.luma(auto).min() < 0.02 and color.luma(auto).max() > 0.98


# --------------------------------------------------------------------------- tone: local tonal ops
def _two_zone(dark=0.02, bright=0.6, h=96, w=192):
    x = np.zeros((h, w, 3), np.float32)
    x[:, : w // 2] = dark
    x[:, w // 2:] = bright
    return x


def test_highlights_shadows_directions():
    x = _two_zone()
    L, R = (slice(20, 76), slice(10, 70)), (slice(20, 76), slice(120, 180))
    s = run("highlights_shadows", x, shadows=80)
    assert s[L].mean() / x[L].mean() > 1.8
    assert abs(s[R].mean() / x[R].mean() - 1) < 0.04
    h = run("highlights_shadows", x, highlights=-80)
    assert h[R].mean() / x[R].mean() < 0.65
    assert abs(h[L].mean() / x[L].mean() - 1) < 0.05


def test_tone_equalizer_curve_interpolates_nodes():
    bands = [0, 0.5, 1, -1, 0.2, 0, 0.7, 0, -0.3]
    grid, g = T.tone_equalizer_curve(bands, smoothing=0.0)
    for ev, b in zip(range(-8, 1), bands):
        assert abs(np.interp(ev, grid, g) - b) < 1e-3
    assert g[0] == pytest.approx(bands[0]) and g[-1] == pytest.approx(bands[-1])   # flat extension
    _, g2 = T.tone_equalizer_curve(bands, smoothing=0.5)
    assert np.abs(np.diff(g2)).max() < np.abs(np.diff(g)).max()   # smoothing reduces the steepest slope


def test_tone_equalizer_band_brightens_only_that_zone():
    x = np.zeros((96, 288, 3), np.float32)
    x[:, :96] = 2 ** -6 * np.array([1.0, 0.9, 0.8])      # -6 EV (rms-norm) region
    x[:, 96:192] = 0.18 * np.array([1.0, 0.9, 0.8]) / 0.9
    x[:, 192:] = 0.8 * np.array([1.0, 0.9, 0.8])
    bands = [0, 0, 1.5, 0, 0, 0, 0, 0, 0]               # +1.5 EV at -6 EV only
    out = run("tone_equalizer", x, bands=bands, smoothing=0.2, smoothing_diameter=2.0)
    g_dark = out[30:60, 20:70] / x[30:60, 20:70]
    g_mid = out[30:60, 120:170] / x[30:60, 120:170]
    g_hi = out[30:60, 220:270] / x[30:60, 220:270]
    assert g_dark.mean() > 2.4                            # 2^1.5 = 2.83
    assert abs(g_mid.mean() - 1) < 0.05 and abs(g_hi.mean() - 1) < 0.02
    # per-pixel gain is shared by all channels -> chromaticity preserved
    np.testing.assert_allclose(g_dark[..., 0], g_dark[..., 2], rtol=1e-3)
    assert "tone_equalizer_mask" in run.ctx.cache


def test_tone_equalizer_highlight_band_and_exposure_comp():
    x = _two_zone(0.02, 1.0)
    out = run("tone_equalizer", x, bands=[0] * 8 + [-1.0], smoothing=0.2, smoothing_diameter=2.0)
    assert out[20:70, 130:180].mean() < 0.6                      # 0 EV band pulled down by ~1 EV
    assert abs(out[20:70, 10:60].mean() / 0.02 - 1) < 0.05
    out2 = run("tone_equalizer", x, bands=[0] * 8 + [-1.0], smoothing=0.2, smoothing_diameter=2.0, exposure_comp=-3.0)
    assert out2[20:70, 130:180].mean() > out[20:70, 130:180].mean()   # mask shifted out of the 0 EV band


def test_contrast(lin):
    f = color.MIDDLE_GREY
    g = flat(4, 4, [f, f, f])
    out = run("contrast", g, contrast=0.6)
    np.testing.assert_allclose(lum(out), lum(g), rtol=1e-4)           # luminance at the fulcrum is fixed
    hi, lo = flat(4, 4, [0.5] * 3), flat(4, 4, [0.05] * 3)
    assert run("contrast", hi, contrast=0.5)[0, 0, 0] > 0.5 and run("contrast", lo, contrast=0.5)[0, 0, 0] < 0.05
    assert lum(run("contrast", lin, contrast=0.5)).std() > lum(lin).std()
    px = np.array([[[0.5, 0.25, 0.1]]], np.float32)
    o = run("contrast", px, contrast=0.4)
    np.testing.assert_allclose(o[0, 0, 0] / o[0, 0, 1], 2.0, rtol=1e-4)   # ratios preserved


def _detail_energy(img, s_small, s_large):
    L = np.log2(np.maximum(lum(img), 1e-4))
    band = cv2.GaussianBlur(L, (0, 0), s_small) - cv2.GaussianBlur(L, (0, 0), s_large)
    return float(band.std())


@pytest.fixture(scope="module")
def textured():
    rng = np.random.default_rng(5)
    base = cv2.GaussianBlur(rng.random((256, 384)).astype(np.float32), (0, 0), 3) * 3 + 0.5
    fine = cv2.GaussianBlur(rng.random((256, 384)).astype(np.float32), (0, 0), 1.0)
    x = np.clip(0.05 + 0.12 * base + 0.05 * fine, 0.01, 1.0)
    return np.stack([x, x * 0.9, x * 0.8], -1).astype(np.float32)


@pytest.mark.parametrize("method", ["laplacian", "guided"])
def test_local_contrast_boosts_detail_and_negative_softens(textured, method):
    out = run("local_contrast", textured, amount=0.6, method=method)
    soft = run("local_contrast", textured, amount=-0.6, method=method)
    e0 = _detail_energy(textured, 2, 8)
    assert _detail_energy(out, 2, 8) > 1.15 * e0
    assert _detail_energy(soft, 2, 8) < 0.9 * e0
    # mean brightness is roughly preserved
    assert abs(np.log2(lum(out).mean() / lum(textured).mean())) < 0.25


def test_local_contrast_scale_weights(textured):
    fine_e0, coarse_e0 = _detail_energy(textured, 1, 3), _detail_energy(textured, 6, 30)
    tex = run("local_contrast", textured, texture=0.8)
    stru = run("local_contrast", textured, structure=0.8)
    f_gain_tex = _detail_energy(tex, 1, 3) / fine_e0
    c_gain_tex = _detail_energy(tex, 6, 30) / coarse_e0
    f_gain_str = _detail_energy(stru, 1, 3) / fine_e0
    c_gain_str = _detail_energy(stru, 6, 30) / coarse_e0
    assert f_gain_tex > c_gain_tex and c_gain_str > f_gain_str


def test_local_laplacian_leaves_large_edges_alone():
    x = np.zeros((128, 192), np.float32)
    x[:, 96:] = 3.0                                   # a 3 EV step in log2 units (> sigma_r)
    out = T.local_laplacian(x, sigma_r=1.0, alpha=0.5)
    assert out[:, :80].max() < 0.05 and out[:, 112:].min() > 2.95       # no halo overshoot away from the edge
    assert out.max() < 3.1 and out.min() > -0.1


def test_dehaze_removes_and_adds_haze(lin):
    t = 0.55
    A = np.array([0.7, 0.72, 0.75], np.float32)
    hazy = (lin * 0.5 * t + A * (1 - t)).astype(np.float32)
    out = run("dehaze", hazy, strength=0.9)
    assert lum(out).std() > 1.25 * lum(hazy).std()
    assert T.dark_channel(out, 9).mean() < T.dark_channel(hazy, 9).mean()
    more = run("dehaze", hazy, strength=-0.8)
    assert lum(more).std() < lum(hazy).std()
    assert np.isfinite(out).all()


def test_hdr_tone(lin):
    base = lin * 0.5
    lift = run("hdr_tone", base, shadows=1.5)
    dark = lum(base) < np.percentile(lum(base), 10)
    assert lum(lift)[dark].mean() > 1.3 * lum(base)[dark].mean()
    comp = run("hdr_tone", base, compress=0.6)
    r0 = np.percentile(lum(base), 99) / np.percentile(lum(base), 1)
    r1 = np.percentile(lum(comp), 99) / np.percentile(lum(comp), 1)
    assert r1 < 0.6 * r0
    det = run("hdr_tone", base, detail=1.0)
    assert _detail_energy(det, 1.5, 6) > _detail_energy(base, 1.5, 6)


# --------------------------------------------------------------------------- colour: balance, saturation, hsl
@pytest.fixture(scope="module")
def swatches():
    """Linear swatches: grey ramp rows + saturated colours (red, green, blue, yellow, skin)."""
    cols = {"red": (0.8, 0.1, 0.1), "green": (0.15, 0.6, 0.15), "blue": (0.1, 0.15, 0.8), "yellow": (0.7, 0.65, 0.1),
            "skin": (0.55, 0.38, 0.30), "grey": (0.18, 0.18, 0.18), "dark": (0.01, 0.01, 0.01),
            "bright": (0.9, 0.9, 0.9), "lowblue": (0.18, 0.2, 0.26), "lowskin": (0.24, 0.2, 0.18)}
    names = list(cols)
    img = np.zeros((16, 16 * len(names), 3), np.float32)
    for i, n in enumerate(names):
        img[:, i * 16:(i + 1) * 16] = color.apply_matrix(np.array(cols[n], np.float32)[None, None], color.SRGB_TO_REC2020)
    return img, {n: (slice(None), slice(i * 16, (i + 1) * 16)) for i, n in enumerate(names)}


def sw(swatches, img, name):
    return img[swatches[1][name]].reshape(-1, 3).mean(0)


def test_color_balance_shadow_and_highlight_tints(swatches):
    img, _ = swatches
    out = run("color_balance", img, shadows=[240, 60, 0])   # blue-ish tint in shadows
    d = sw(swatches, out, "dark") / sw(swatches, img, "dark")
    b = sw(swatches, out, "bright") / sw(swatches, img, "bright")
    assert d[2] / d[0] > 1.25 and abs(b[2] / b[0] - 1) < 0.08
    out2 = run("color_balance", img, highlights=[40, 60, 0])  # orange highlights
    b2 = sw(swatches, out2, "bright") / sw(swatches, img, "bright")
    d2 = sw(swatches, out2, "dark") / sw(swatches, img, "dark")
    assert b2[0] / b2[2] > 1.2 and abs(d2[0] / d2[2] - 1) < 0.08


def test_color_balance_luma_and_global_tint(swatches):
    img, _ = swatches
    out = run("color_balance", img, **{"global": [120, 30, 0]})
    g = sw(swatches, out, "grey")
    assert g[1] > g[0] and g[1] > g[2]                      # green cast on neutral
    up = run("color_balance", img, highlights=[0, 0, 100])  # +1 stop in highlights
    assert sw(swatches, up, "bright")[1] > 1.6 * sw(swatches, img, "bright")[1]


def test_color_balance_cdl_and_contrast(swatches):
    img, _ = swatches
    out = run("color_balance", img, slope=[2, 2, 2])
    np.testing.assert_allclose(out, img * 2, rtol=1e-5)
    off = run("color_balance", img, offset=[0.01, 0, 0])
    assert np.allclose(off[..., 1], img[..., 1]) and np.all(off[..., 0] > img[..., 0])
    pw = run("color_balance", img, power=[1.0, 1.0, 1.2])
    assert np.all(pw[..., 2] <= img[..., 2] + 1e-6)           # power > 1 darkens below the pivot
    c = run("color_balance", img, contrast=0.5, contrast_fulcrum=0.18)
    np.testing.assert_allclose(lum(c[:, 16 * 5:16 * 6]), lum(img[:, 16 * 5:16 * 6]), rtol=1e-3)   # grey fulcrum
    assert sw(swatches, c, "bright")[1] > sw(swatches, img, "bright")[1]


def test_color_balance_chroma_saturation_vibrance_brilliance(swatches):
    img, _ = swatches
    base = oklch(img)
    sat = oklch(run("color_balance", img, saturation=50))
    assert np.all(sat[..., 1] >= base[..., 1] - 1e-3)
    assert sat[swatches[1]["red"]][..., 1].mean() > 1.3 * base[swatches[1]["red"]][..., 1].mean()
    desat = oklch(run("color_balance", img, saturation=-100))
    assert desat[..., 1].max() < 0.02
    ch = run("color_balance", img, chroma=-100)
    np.testing.assert_allclose(ch[..., 0], ch[..., 2], atol=2e-3)      # linear chroma -100 % = grey at same Y
    np.testing.assert_allclose(lum(ch), lum(img), rtol=2e-3)
    bri = run("color_balance", img, brilliance=40)
    assert lum(bri).mean() > lum(img).mean() * 1.3
    hs = oklch(run("color_balance", img, hue_shift=30))
    d = hue_diff(hs[swatches[1]["red"]][..., 2], base[swatches[1]["red"]][..., 2]).mean()
    assert 25 < d < 35


def test_saturation_op_gates_neutrals(swatches):
    img, _ = swatches
    out = run("saturation", img, amount=60)
    np.testing.assert_allclose(sw(swatches, out, "grey"), sw(swatches, img, "grey"), rtol=1e-3)
    assert oklch(out)[swatches[1]["blue"]][..., 1].mean() > 1.4 * oklch(img)[swatches[1]["blue"]][..., 1].mean()
    lin_mode = run("saturation", img, amount=-100, mode="linear")
    np.testing.assert_allclose(lin_mode[..., 0], lin_mode[..., 2], atol=1e-4)


def test_vibrance_favours_low_chroma_and_protects_skin(swatches):
    img, sl = swatches
    out = run("vibrance", img, amount=80, protect_skin=0.8)
    b, o = oklch(img), oklch(out)
    gain = lambda n: o[sl[n]][..., 1].mean() / b[sl[n]][..., 1].mean()
    assert gain("lowblue") > gain("blue") + 0.15                  # low chroma boosted more than saturated
    assert gain("lowblue") > gain("lowskin") + 0.1                  # skin hue protected
    assert gain("blue") >= 0.99
    neutral = run("vibrance", img, amount=80)
    np.testing.assert_allclose(sw(swatches, neutral, "grey"), sw(swatches, img, "grey"), rtol=1e-3)


def test_skin_weight(disp):
    skin = flat(4, 4, [0.88, 0.66, 0.55])
    blue = flat(4, 4, [0.1, 0.2, 0.8])
    grey = flat(4, 4, [0.5, 0.5, 0.5])
    assert C.skin_weight(skin, "display").mean() > 0.6
    assert C.skin_weight(blue, "display").mean() < 0.05 and C.skin_weight(grey, "display").mean() < 0.05


def test_hue_wheel_helpers():
    for h in (0, 60, 120, 180, 240, 300):
        assert abs(hue_diff(C.oklch_hue_to_hsv(C.hsv_hue_to_oklch(h)), h)) < 0.2
    assert abs(C.hsv_hue_to_oklch(0) - 29.2) < 2.0        # sRGB red in Oklch
    assert abs(C.hsv_hue_to_oklch(240) - 264.0) < 2.0     # sRGB blue
    rgb = C.wheel_to_rgb(0, 50)                           # red wheel -> more red than blue
    assert rgb[0] > 1 > rgb[2]
    np.testing.assert_allclose(color.luminance(rgb[None, None]), 1.0, rtol=1e-3)


def test_band_weights_partition_of_unity():
    h = np.linspace(0, 360, 721)
    for soft in (1.0, 0.5):
        W = C.band_weights(h, soft)
        np.testing.assert_allclose(W.sum(0), 1.0, atol=1e-5)
        assert W.min() >= 0
    cen = C.band_centers_oklch()
    Wc = C.band_weights(cen)
    np.testing.assert_allclose(np.diag(Wc), 1.0, atol=1e-5)


def test_hsl_mixer_band_selectivity(swatches):
    img, sl = swatches
    b0 = oklch(img)
    out = run("hsl_mixer", img, blue_sat=100)
    o = oklch(out)
    ch = lambda n, arr: arr[sl[n]][..., 1].mean()
    assert ch("blue", o) > 1.6 * ch("blue", b0)
    for n in ("red", "green", "grey", "skin"):
        assert abs(ch(n, o) - ch(n, b0)) < 0.04 * max(ch(n, b0), 0.05), n
    # hue shift of the green band moves greens only
    out = run("hsl_mixer", img, green_hue=60)
    o = oklch(out)
    assert abs(hue_diff(o[sl["green"]][..., 2], b0[sl["green"]][..., 2]).mean()) > 10
    assert abs(hue_diff(o[sl["red"]][..., 2], b0[sl["red"]][..., 2]).mean()) < 1.0
    # luminance of yellow band
    out = run("hsl_mixer", img, yellow_lum=60)
    assert oklch(out)[sl["yellow"]][..., 0].mean() > 1.1 * b0[sl["yellow"]][..., 0].mean()
    assert abs(oklch(out)[sl["blue"]][..., 0].mean() - b0[sl["blue"]][..., 0].mean()) < 0.01
    # neutral grey untouched even with strong settings
    out = run("hsl_mixer", img, red_hue=100, blue_sat=100, green_lum=100)
    np.testing.assert_allclose(sw(swatches, out, "grey"), sw(swatches, img, "grey"), rtol=1e-3)


def test_color_zones_hue_curve(swatches):
    img, sl = swatches
    b0 = oklch(img)
    out = run("color_zones", img, chroma=[[0, 0], [90, 0], [120, -1.0], [150, 0], [240, 0]])
    o = oklch(out)
    assert o[sl["green"]][..., 1].mean() < 0.5 * b0[sl["green"]][..., 1].mean()
    assert abs(o[sl["red"]][..., 1].mean() - b0[sl["red"]][..., 1].mean()) < 0.01
    out = run("color_zones", img, hue_shift=[[0, 0], [100, 0], [120, 40], [140, 0]], mix=1.0)
    assert abs(hue_diff(oklch(out)[sl["green"]][..., 2], b0[sl["green"]][..., 2]).mean()) > 10
    # lightness selector: darken only dark-ish colours
    out = run("color_zones", img, selector="lightness", lightness=[[0, 0], [0.5, 0], [0.8, -1.0], [1.0, -1.0]])
    assert lum(out)[sl["bright"]].mean() < lum(img)[sl["bright"]].mean()


def test_channel_mixer_and_monochrome(swatches):
    img, sl = swatches
    swap = run("channel_mixer", img, matrix=[[0, 0, 1], [0, 1, 0], [1, 0, 0]])
    np.testing.assert_allclose(swap[..., 0], img[..., 2])
    norm = run("channel_mixer", img, matrix=[[2, 2, 2], [0, 2, 0], [0, 0, 2]], normalize=True)
    np.testing.assert_allclose(sw(swatches, norm, "grey"), sw(swatches, img, "grey"), rtol=1e-5)
    neutral = run("monochrome", img)
    np.testing.assert_allclose(neutral[..., 0], neutral[..., 1])
    np.testing.assert_allclose(neutral[..., 0], lum(img), rtol=1e-4)
    red = run("monochrome", img, filter="red")
    blue = run("monochrome", img, filter="blue")
    rr = lambda o: o[sl["red"]][..., 0].mean() / o[sl["blue"]][..., 0].mean()
    assert rr(red) > 2 * rr(neutral) and rr(blue) < 0.5 * rr(neutral)
    for f in C.FILTERS:
        o = run("monochrome", img, filter=f)
        assert np.isfinite(o).all() and o.min() >= 0
    np.testing.assert_allclose(sw(swatches, run("monochrome", img, filter="orange"), "grey"),
                               sw(swatches, img, "grey"), rtol=1e-3)                # weights normalised: grey stays
    mixed = run("monochrome", img, weights=[1, 0, 0])
    np.testing.assert_allclose(mixed[..., 1], img[..., 0], rtol=1e-5)


# --------------------------------------------------------------------------- LUTs
def test_identity_cube_roundtrip_and_apply(tmp_path, disp):
    lut = C.identity_lut(17)
    path = C.write_cube(tmp_path / "id.cube", lut, "ident")
    l2 = C.load_cube(path)
    assert l2.kind == "3d" and l2.size == 17 and l2.title == "ident"
    np.testing.assert_allclose(l2.table, lut.table, atol=1e-6)
    out = C.apply_cube(disp, l2)
    np.testing.assert_allclose(out, disp, atol=1e-5)
    out = run("lut3d", disp, path=str(path))
    np.testing.assert_allclose(out, disp, atol=1e-5)


def test_cube_parser_handles_comments_domain_and_ordering(tmp_path):
    n = 2
    rows = []
    for b in range(n):
        for g in range(n):
            for r in range(n):    # red fastest
                rows.append(f"{r * 0.5:.3f} {g * 1.0:.3f} {b * 2.0:.3f}")
    text = ("# comment\nTITLE \"t\"\n\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 2 2 2\nLUT_3D_SIZE 2\n" + "\n".join(rows) + "\n")
    p = tmp_path / "x.cube"
    p.write_text(text)
    lut = C.load_cube(p)
    assert lut.size == 2 and np.allclose(lut.domain_max, 2.0)
    assert np.allclose(lut.table[1, 0, 0], [0.5, 0, 0]) and np.allclose(lut.table[0, 1, 0], [0, 1, 0])
    assert np.allclose(lut.table[0, 0, 1], [0, 0, 2])
    # domain scaling: input 2.0 (domain max) -> table corner
    out = C.apply_cube(np.array([[[2.0, 2.0, 2.0], [1.0, 0.0, 0.0]]], np.float32), lut)
    np.testing.assert_allclose(out[0, 0], [0.5, 1, 2], atol=1e-6)
    np.testing.assert_allclose(out[0, 1], [0.25, 0, 0], atol=1e-6)         # halfway on the red axis (linear)
    # write / reload keeps the domain
    p2 = C.write_cube(tmp_path / "y.cube", lut)
    l3 = C.load_cube(p2)
    assert np.allclose(l3.domain_max, 2.0) and np.allclose(l3.table, lut.table, atol=1e-5)


def test_1d_cube(tmp_path):
    p = tmp_path / "g.cube"
    vals = np.linspace(0, 1, 5) ** 2
    p.write_text("LUT_1D_SIZE 5\n" + "\n".join(f"{v:.6f} {v:.6f} {v:.6f}" for v in vals) + "\n")
    lut = C.load_cube(p)
    assert lut.kind == "1d"
    x = np.array([[[0.0, 0.25, 1.0], [0.5, 0.75, 0.125]]], np.float32)
    out = C.apply_cube(x, lut)
    np.testing.assert_allclose(out[0, 0], [0.0, 0.0625, 1.0], atol=1e-5)
    np.testing.assert_allclose(out[0, 1], [0.25, 0.5625, 0.5 * (0 + vals[1] * 0) + np.interp(0.125, np.linspace(0, 1, 5), vals)], atol=1e-5)
    with pytest.raises(ValueError):
        q = tmp_path / "bad.cube"
        q.write_text("LUT_3D_SIZE 2\n0 0 0\n")
        C.load_cube(q)


def test_tetrahedral_accuracy_vs_analytic():
    def f(im):  # smooth nonlinear colour transform
        r, g, b = im[..., 0], im[..., 1], im[..., 2]
        return np.stack([r ** 2, g * b, 0.5 * r + 0.5 * g], -1)

    lut = C.lut_from_function(f, 33)
    rng = np.random.default_rng(0)
    x = rng.random((200, 200, 3)).astype(np.float32)
    tet = C.apply_cube(x, lut, interp="tetrahedral")
    tri = C.apply_cube(x, lut, interp="trilinear")
    assert np.abs(tet - f(x)).max() < 1.1e-3 and np.abs(tri - f(x)).max() < 1.1e-3
    # affine functions are reproduced exactly (both) -> validates the six tetrahedra and the index order
    M = np.array([[0.7, 0.2, 0.1], [0.1, 0.8, 0.1], [0.3, -0.1, 0.8]], np.float32)
    aff = lambda im: im @ M.T + np.array([0.01, -0.02, 0.03], np.float32)
    lut2 = C.lut_from_function(aff, 9)
    for interp in ("tetrahedral", "trilinear"):
        np.testing.assert_allclose(C.apply_cube(x, lut2, interp=interp), aff(x), atol=2e-5)
    # a bilinear cross term: tetrahedral differs from trilinear (different interpolant) but both are close
    cross = lambda im: np.stack([im[..., 0] * im[..., 1]] * 3, -1)
    lut3 = C.lut_from_function(cross, 5)
    a, b = C.apply_cube(x, lut3, interp="tetrahedral"), C.apply_cube(x, lut3, interp="trilinear")
    np.testing.assert_allclose(b, cross(x), atol=1e-5)         # trilinear reproduces bilinear exactly
    assert 1e-4 < np.abs(a - b).max() < 0.06


def test_lut_strength_and_baked_lut_matches_function(disp):
    inv = lambda im: 1.0 - im
    lut = C.lut_from_function(inv, 17)
    full = C.apply_cube(disp, lut, 1.0)
    half = C.apply_cube(disp, lut, 0.5)
    np.testing.assert_allclose(full, 1 - disp, atol=1e-5)
    np.testing.assert_allclose(half, 0.5 * disp + 0.5 * (1 - disp), atol=1e-5)


def test_hald_clut_identity_and_op(tmp_path, disp):
    hald = C.make_hald_identity(4)               # 64x64 image -> 16^3 LUT
    assert hald.shape == (64, 64, 3)
    lut = C.load_hald(hald)
    assert lut.size == 16
    np.testing.assert_allclose(lut.table, C.identity_lut(16).table, atol=1e-6)
    # a modified Hald (invert) applied through the op via a PNG file (16-bit via cv2)
    inv = 1.0 - hald
    path = tmp_path / "inv_hald.png"
    cv2.imwrite(str(path), (inv[..., ::-1] * 65535 + 0.5).astype(np.uint16))
    out = run("lut3d", disp, path=str(path))
    assert np.abs(out - (1 - disp)).max() < 0.02
    with pytest.raises(ValueError):
        C.load_hald(np.zeros((50, 50, 3), np.float32))
    with pytest.raises(FileNotFoundError):
        run("lut3d", disp, path="definitely_missing_lut")


# --------------------------------------------------------------------------- display looks
def test_color_grade_wheels(disp):
    out = run("color_grade", disp, lift=[240, 50, 0])
    dark = color.luma(disp) < 0.2
    bright = color.luma(disp) > 0.8
    d = (out - disp)[dark].mean(0)
    assert d[2] > d[0] + 0.01
    assert np.abs((out - disp)[bright]).mean() < 0.05 and np.abs((out - disp)[dark]).mean() > 2.5 * np.abs((out - disp)[bright]).mean()
    out = run("color_grade", disp, gain=[40, 60, 0])
    d = (out - disp)[bright].mean(0)
    assert d[0] > d[2]
    c = run("color_grade", disp, contrast=0.4)
    assert color.luma(c).std() > color.luma(disp).std()
    assert c.min() >= 0 and c.max() <= 1


def test_split_tone_protects_ends():
    g = np.linspace(0, 1, 64, dtype=np.float32)
    ramp = np.repeat(np.repeat(g[None, :, None], 8, 0), 3, 2)
    out = run("split_tone", ramp, shadow_hue=240, shadow_sat=80, highlight_hue=40, highlight_sat=80)
    lab = color.display_to_oklab(out)
    a_b = lambda i: lab[0, i, 1:]
    # shadows pushed toward blue (negative b), highlights toward orange (positive b, a)
    assert a_b(12)[1] < -0.01 and a_b(52)[1] > 0.01
    assert np.hypot(*a_b(0)) < 0.5 * np.hypot(*a_b(12)) and np.hypot(*a_b(63)) < 0.5 * np.hypot(*a_b(52))
    bal = run("split_tone", ramp, shadow_hue=240, shadow_sat=80, highlight_hue=40, highlight_sat=80, balance=100)
    # balance moves the split toward the shadows: a mid-dark pixel becomes highlight-tinted
    assert color.display_to_oklab(bal)[0, 24, 1] > lab[0, 24, 1]


def test_temperature_and_invert(disp):
    warm = run("temperature", disp, temperature=60)
    cool = run("temperature", disp, temperature=-60)
    assert warm[..., 0].mean() / warm[..., 2].mean() > disp[..., 0].mean() / disp[..., 2].mean()
    assert cool[..., 0].mean() / cool[..., 2].mean() < disp[..., 0].mean() / disp[..., 2].mean()
    mag = run("temperature", flat(4, 4, [0.5] * 3), tint=60)
    assert mag[0, 0, 1] < mag[0, 0, 0]                    # +tint = magenta (less green)
    np.testing.assert_allclose(run("invert", disp), 1 - disp, atol=1e-6)


# --------------------------------------------------------------------------- detail: noise machinery
def poisson_gauss_noise(img, a, b, seed=0):
    rng = np.random.default_rng(seed)
    return (img + rng.standard_normal(img.shape).astype(np.float32) * np.sqrt(a * np.maximum(img, 0) + b)).astype(np.float32)


def test_estimate_sigma_and_fit_profile():
    rng = np.random.default_rng(1)
    x = np.full((256, 256), 0.3, np.float32) + rng.normal(0, 0.02, (256, 256)).astype(np.float32)
    assert abs(D.estimate_sigma(x) - 0.02) < 0.002
    # fit a, b from a gradient image
    ramp = np.tile(np.linspace(0.02, 0.9, 512, dtype=np.float32), (256, 1))
    a, b = 0.002, 1e-5
    n = poisson_gauss_noise(ramp, a, b, 2)
    fa, fb = D.fit_noise_profile(n)
    assert 0.5 * a < fa < 1.6 * a and fb < 5e-4


def test_gat_roundtrip_and_variance_stabilisation():
    a, b = 0.002, 1e-5
    y = np.linspace(0, 1, 1000, dtype=np.float32)
    np.testing.assert_allclose(D.gat_inverse(D.gat_forward(y, a, b), a, b), y, atol=2 * a)
    rng = np.random.default_rng(0)
    for level in (0.05, 0.3, 0.8):
        s = level + rng.standard_normal(200000).astype(np.float32) * np.sqrt(a * level + b)
        assert abs(D.gat_forward(s, a, b).std() - 1.0) < 0.08


def test_atrous_decomposition_is_exact_and_noise_factors():
    rng = np.random.default_rng(3)
    x = rng.random((64, 80)).astype(np.float32)
    det, res = D.atrous_decompose(x, 4)
    np.testing.assert_allclose(sum(det) + res, x, atol=1e-5)
    f = D._atrous_noise_factors(8)
    assert f[0] > f[1] > f[2] > f[3] > 0 and 0.7 < f[0] < 1.0     # Starck table: 0.889, 0.200, 0.086, ...


@pytest.fixture(scope="module")
def noisy_scene(lin):
    a, b = D.noise_model_for_iso(3200)
    clean = (lin * 0.4).astype(np.float32)
    return clean, poisson_gauss_noise(clean, a, b, 0)


SKY = (slice(0, 24), slice(250, 360))


def test_denoise_reduces_flat_noise_and_preserves_edges(noisy_scene):
    clean, noisy = noisy_scene
    base_std = (noisy - clean)[SKY].std()
    out = run("denoise", noisy, meta={"ISO": 3200})
    check(out, noisy.shape)
    err = (out - clean)[SKY]
    assert err.std() < 0.4 * base_std and abs(err.mean()) < 0.1 * base_std
    # an edge in the scene (building / sky boundary) keeps most of its gradient
    gy = lambda im: np.abs(np.diff(lum(im)[60:180, 180:260], axis=1)).max()
    # use a synthetic step instead for a clean measurement
    step = np.zeros((96, 160, 3), np.float32) + 0.05
    step[:, 80:] = 0.4
    a, b = D.noise_model_for_iso(1600)
    ns = poisson_gauss_noise(step, a, b, 1)
    o = run("denoise", ns, meta={"ISO": 1600})
    edge = lum(o)[:, 82].mean() - lum(o)[:, 78].mean()
    assert edge > 0.8 * (lum(step)[:, 82].mean() - lum(step)[:, 78].mean())
    assert (lum(o) - lum(step))[:, 10:60].std() < 0.5 * (lum(ns) - lum(step))[:, 10:60].std()


@pytest.mark.parametrize("method", ["bilateral", "nlm"])
def test_denoise_other_methods(noisy_scene, method):
    clean, noisy = noisy_scene
    out = run("denoise", noisy, meta={"ISO": 3200}, method=method)
    check(out, noisy.shape)
    assert (out - clean)[SKY].std() < 0.6 * (noisy - clean)[SKY].std()


def test_denoise_bm3d_optional(noisy_scene):
    _, noisy = noisy_scene
    try:
        import bm3d  # noqa: F401
    except Exception:
        with pytest.raises(MissingDependency):
            run("denoise", noisy[:64, :64], method="bm3d")
        return
    out = run("denoise", noisy[:64, :64], method="bm3d")
    check(out)


def test_denoise_chroma_vs_luma_and_iso_adapt():
    rng = np.random.default_rng(7)
    base = flat(128, 128, [0.2, 0.2, 0.2])
    # chroma-only noise (zero-mean across channels)
    n = rng.standard_normal((128, 128, 1)).astype(np.float32) * 0.03 * np.array([1, -2, 1], np.float32) / np.sqrt(6)
    noisy = base + n
    out = run("denoise", noisy, meta={"ISO": 800}, luma=0, chroma=100)
    oc = lambda im: oklch(im)[..., 1].mean()
    assert oc(out) < 0.5 * oc(noisy)
    # luma-only strength must leave the chroma noise alone
    out_l = run("denoise", noisy, meta={"ISO": 800}, luma=100, chroma=0)
    assert oc(out_l) > 0.8 * oc(noisy)
    # ISO adaptivity: same input, higher ISO -> stronger smoothing
    clean, nz = base, poisson_gauss_noise(base, *D.noise_model_for_iso(800), 3)
    lo = run("denoise", nz, meta={"ISO": 100})
    hi = run("denoise", nz, meta={"ISO": 6400})
    assert (hi - clean).std() < (lo - clean).std()
    assert D._iso_factor({"ISO": 6400}) > D._iso_factor({"ISO": 400}) > D._iso_factor({"ISO": 100})


def test_profiled_denoise_matches_denoise(noisy_scene):
    _, noisy = noisy_scene
    a = run("profiled_denoise", noisy, meta={"ISO": 3200})
    b = run("denoise", noisy, meta={"ISO": 3200}, method="wavelet")
    np.testing.assert_allclose(a, b, atol=1e-5)


def test_hot_pixels_removed(lin):
    x = (lin * 0.3).astype(np.float32)
    rng = np.random.default_rng(2)
    ys, xs = rng.integers(8, x.shape[0] - 8, 12), rng.integers(8, x.shape[1] - 8, 12)
    hot = x.copy()
    hot[ys, xs, 1] = 0.9
    out = run("hot_pixels", hot, meta={"ISO": 400})
    assert np.abs(out[ys, xs, 1] - x[ys, xs, 1]).max() < 0.1 * 0.9
    mask = np.ones(x.shape[:2], bool)
    mask[ys, xs] = False
    assert np.abs(out - x)[mask].mean() < 5e-4           # legit detail left alone
    dead = x.copy()
    dead[ys, xs, 0] = 0.0
    assert np.abs(run("hot_pixels", dead, dead=True, meta={"ISO": 400})[ys, xs, 0] - x[ys, xs, 0]).max() < \
        np.abs(dead[ys, xs, 0] - x[ys, xs, 0]).max()


# --------------------------------------------------------------------------- detail: sharpening
def blurred_scene(lin, sigma=1.5):
    return cv2.GaussianBlur(lin, (0, 0), sigma)


def grad_energy(img):
    L = color.srgb_oetf(np.maximum(lum(img), 0))
    return float(np.hypot(cv2.Sobel(L, cv2.CV_32F, 1, 0), cv2.Sobel(L, cv2.CV_32F, 0, 1)).mean())


def test_sharpen_increases_gradients_and_preserves_hue(lin):
    soft = blurred_scene(lin, 1.2)
    out = run("sharpen", soft, amount=1.0, radius=5.0, masking=0, halo=0)       # sigma ~1.2 px at 480 px
    assert grad_energy(out) > 1.15 * grad_energy(soft)
    # chromaticity preserved
    r0 = soft[..., 0] / np.maximum(soft[..., 1], 1e-4)
    r1 = out[..., 0] / np.maximum(out[..., 1], 1e-4)
    m = soft[..., 1] > 0.05
    np.testing.assert_allclose(r1[m], r0[m], rtol=1e-3)
    # amount 0 identity, larger amount sharper
    assert grad_energy(run("sharpen", soft, amount=2.0, radius=5.0, masking=0, halo=0)) > grad_energy(out)


def test_sharpen_threshold_halo_and_masking():
    rng = np.random.default_rng(0)
    flatn = np.clip(0.3 + rng.normal(0, 0.003, (96, 96)).astype(np.float32), 0, 1)
    img = np.repeat(flatn[..., None], 3, 2)
    plain = run("sharpen", img, amount=2.0, radius=8.0, threshold=0.0, masking=0)     # sigma ~0.4 px at 96 px
    gated = run("sharpen", img, amount=2.0, radius=8.0, threshold=0.05, masking=0)
    assert np.abs(gated - img).mean() < 0.3 * np.abs(plain - img).mean()
    step = np.zeros((64, 128, 3), np.float32) + 0.1
    step[:, 64:] = 0.6
    step = cv2.GaussianBlur(step, (0, 0), 1.5)
    free = run("sharpen", step, amount=2.0, radius=40.0, halo=0.0, masking=0)      # sigma ~1.9 px at 128 px
    clamped = run("sharpen", step, amount=2.0, radius=40.0, halo=1.0, masking=0)
    assert free.max() > 0.6 * 1.03 or free.min() < 0.1 * 0.97                 # overshoot exists without clamp
    assert clamped.max() <= step.max() * 1.001 + 1e-4 and clamped.min() >= step.min() * 0.999 - 1e-4


def test_deconvolution_recovers_blur(lin):
    sigma = 1.5
    ref = (lin * 0.5).astype(np.float32)
    soft = cv2.GaussianBlur(ref, (0, 0), sigma)
    mse = lambda a: float(np.mean((a - ref) ** 2))
    # radius is given at 2000 px long edge: scale for the 480 px test image
    r2000 = sigma * 2000.0 / 480.0
    out = run("deconvolve", soft, radius=r2000, iterations=15, damping=0.0, threshold=0.0, meta={"ISO": 100})
    assert mse(out) < 0.8 * mse(soft)
    assert grad_energy(out) > grad_energy(soft)


def test_capture_sharpen_respects_noise_gate_and_scale(lin):
    a, b = D.noise_model_for_iso(1600)
    base = (lin * 0.4).astype(np.float32)
    soft = cv2.GaussianBlur(base, (0, 0), 0.9)
    out = run("capture_sharpen", soft, radius=0.9, iterations=10, amount=1.0, meta={"ISO": 100, "FNumber": 5.6})
    assert grad_energy(out) > 1.1 * grad_energy(soft)
    flat_noisy = poisson_gauss_noise(flat(96, 96, [0.2] * 3), a * 3, b, 4)
    o = run("capture_sharpen", flat_noisy, radius=0.9, meta={"ISO": 1600}, threshold=4.0)
    assert o.std() < 1.5 * flat_noisy.std()                 # noise not amplified in flat areas
    # preview below native resolution: faded + noted
    spec = REGISTRY["capture_sharpen"]
    ctx = Ctx(full_long_edge=6000)
    p = spec.resolve_params({"radius": 0.8})
    spec.fn(soft, p, ctx)
    assert any("preview" in m for m in ctx.log)
    assert D.richardson_lucy(np.ones((16, 16), np.float32), 1.0, 5).shape == (16, 16)


def test_output_sharpen_mediums(disp):
    soft = cv2.GaussianBlur(disp, (0, 0), 0.8)
    for medium in ("screen", "web", "print"):
        out = run("output_sharpen", soft, medium=medium)
        assert out.min() >= 0 and out.max() <= 1
        assert grad_energy(out) > grad_energy(soft)
    pr = run("output_sharpen", soft, medium="print", ppi=600, amount=1.0)
    assert grad_energy(pr) > grad_energy(soft)


# --------------------------------------------------------------------------- detail: fringes & CA
def test_defringe_reduces_fringe_chroma_only_near_edges():
    img = np.zeros((128, 256, 3), np.float32) + 0.02
    img[:, 128:] = 0.8                                   # dark | bright edge at x = 128
    purple = color.apply_matrix(np.array([0.5, 0.1, 0.6], np.float32)[None, None], color.SRGB_TO_REC2020)[0, 0]
    green = color.apply_matrix(np.array([0.2, 0.6, 0.2], np.float32)[None, None], color.SRGB_TO_REC2020)[0, 0]
    img[:, 124:128] = purple                            # purple fringe along the edge
    img[20:60, 20:60] = purple                          # purple object far from the edge
    img[80:120, 190:230] = green                        # green object, far from edges? (inside bright area border)
    fr = (slice(30, 100), slice(124, 128))
    far = (slice(30, 50), slice(25, 55))
    out = run("defringe", img, amount=100, radius=4.0)
    c0, c1 = oklch(img)[..., 1], oklch(out)[..., 1]
    assert c1[fr].mean() < 0.5 * c0[fr].mean()
    assert c1[far].mean() > 0.95 * c0[far].mean()
    neutral = run("defringe", img, amount=100, radius=4.0, mode="neutral")
    assert oklch(neutral)[..., 1][fr].mean() < 0.1 * c0[fr].mean()
    # a fringe whose hue is not purple/green (red) is left alone
    img2 = img.copy()
    red = color.apply_matrix(np.array([0.8, 0.1, 0.1], np.float32)[None, None], color.SRGB_TO_REC2020)[0, 0]
    img2[:, 124:128] = red
    o2 = run("defringe", img2, amount=100, radius=4.0)
    assert oklch(o2)[..., 1][fr].mean() > 0.95 * oklch(img2)[..., 1][fr].mean()


def test_ca_correct_manual_and_auto(disp, lin):
    h, w = lin.shape[:2]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    k = 0.0025
    aberrated = lin.copy()
    # simulate lateral CA: red magnified by +k, blue by -k (relative to green)
    aberrated[..., 0] = D._radial_remap(lin[..., 0], 1.0 / (1.0 + k), cx, cy)
    aberrated[..., 2] = D._radial_remap(lin[..., 2], 1.0 / (1.0 - k), cx, cy)
    err = lambda im: float(np.abs(im - lin)[20:-20, 20:-20].mean())
    fixed = run("ca_correct", aberrated, red=k, blue=-k)
    assert err(fixed) < 0.7 * err(aberrated)
    kr, kb = D.estimate_lateral_ca(aberrated)
    assert kr > 0.5 * k and kb < -0.5 * k and abs(kr - k) < 0.6 * k and abs(kb + k) < 0.6 * k
    auto = run("ca_correct", aberrated, auto=True)
    assert err(auto) < 0.8 * err(aberrated)
    assert any("ca_correct auto" in m for m in run.ctx.log)
    # no CA -> auto estimate ~ 0 and image ~ unchanged
    kr0, kb0 = D.estimate_lateral_ca(lin)
    assert abs(kr0) < 0.0012 and abs(kb0) < 0.0012


# --------------------------------------------------------------------------- geometry
@pytest.fixture(scope="module")
def geo_img(lin):
    return (lin * 0.7 + 0.05).astype(np.float32)     # strictly positive everywhere (480 x 320 landscape 3:2)


@pytest.fixture(scope="module")
def geo_smooth(geo_img):
    return cv2.GaussianBlur(geo_img, (0, 0), 2.0)    # no Lanczos/cubic undershoot -> any zero pixel is border fill


def test_parse_aspect():
    assert G.parse_aspect("3:2") == pytest.approx(1.5)
    assert G.parse_aspect("3:2", portrait=True) == pytest.approx(2 / 3)
    assert G.parse_aspect("9:16") == pytest.approx(9 / 16) and G.parse_aspect("9:16", False) == pytest.approx(9 / 16)
    assert G.parse_aspect("16:9", portrait=True) == pytest.approx(9 / 16)
    assert G.parse_aspect("square") == 1.0 and G.parse_aspect("1:1", portrait=True) == 1.0
    assert G.parse_aspect(1.25) == 1.25 and G.parse_aspect([4, 5]) == pytest.approx(0.8)
    assert G.parse_aspect(None) is None and G.parse_aspect("free") is None
    assert G.parse_aspect("golden") == pytest.approx(1.618, abs=1e-3)
    assert G.parse_aspect("4:5L") == pytest.approx(1.25) and G.parse_aspect("3:2P") == pytest.approx(2 / 3)
    with pytest.raises(ValueError):
        G.parse_aspect("nonsense")


@pytest.mark.parametrize("aspect,expect", [("1:1", 1.0), ("4:5", 0.8), ("3:2", 1.5), ("16:9", 16 / 9), ("9:16", 9 / 16)])
def test_crop_aspect_dims(geo_img, aspect, expect):
    out = run("crop", geo_img, aspect=aspect, orientation="landscape" if aspect in ("3:2", "16:9") else "auto")
    h, w = out.shape[:2]
    assert abs(w / h - expect) < 0.02
    assert h <= geo_img.shape[0] and w <= geo_img.shape[1]
    # largest possible: touches at least one dimension
    assert h == geo_img.shape[0] or w == geo_img.shape[1]


def test_crop_rect_anchor_and_portrait_orientation(geo_img):
    out = run("crop", geo_img, rect=[0.25, 0.25, 0.5, 0.5])
    assert out.shape[:2] == (160, 240)
    np.testing.assert_array_equal(out, geo_img[80:240, 120:360])
    left = run("crop", geo_img, aspect="1:1", anchor=[0.0, 0.5])
    np.testing.assert_array_equal(left, geo_img[:, :320])
    right = run("crop", geo_img, aspect="1:1", anchor=[1.0, 0.5])
    np.testing.assert_array_equal(right, geo_img[:, -320:])
    port = np.ascontiguousarray(np.rot90(geo_img))
    out = run("crop", port, aspect="3:2")               # auto: follows the portrait orientation -> 2:3
    assert abs(out.shape[1] / out.shape[0] - 2 / 3) < 0.02
    # rect + aspect: adjusted around the rect centre, stays in the rect
    out = run("crop", geo_img, rect=[0.1, 0.1, 0.6, 0.6], aspect="1:1")
    assert abs(out.shape[1] / out.shape[0] - 1) < 0.02 and out.shape[0] <= 0.6 * 320 + 1


def test_rotate_dims_autocrop_and_no_black(geo_img, geo_smooth):
    h, w = geo_img.shape[:2]
    out = run("rotate", geo_smooth, angle=6.0)
    oh, ow = out.shape[:2]
    assert oh < h and ow < w and abs(ow / oh - w / h) < 0.02
    assert out.min() > 0.04                                  # no black corners from the border fill
    big = run("rotate", geo_smooth, angle=20.0)
    assert big.shape[0] < oh and big.min() > 0.04
    exp = run("rotate", geo_img, angle=10.0, mode="expand")
    assert exp.shape[0] > h and exp.shape[1] > w and exp.min() == 0.0
    keep = run("rotate", geo_img, angle=10.0, mode="keep")
    assert keep.shape == geo_img.shape
    assert run("rotate", geo_img, angle=0.0) is geo_img
    # exact quarter turn in expand mode swaps the dimensions
    q = run("rotate", geo_img, angle=90.0, mode="expand")
    assert abs(q.shape[0] - w) <= 1 and abs(q.shape[1] - h) <= 1
    # rotation direction: CCW positive moves the right edge up (a bright feature at the right goes up)
    mark = np.zeros((100, 200, 3), np.float32) + 0.1
    mark[48:52, 180:196] = 1.0
    rot = run("rotate", mark, angle=10.0, mode="keep")
    ys, xs = np.nonzero(rot[..., 0] > 0.5)
    assert ys.mean() < 50 - 5


def test_rotate_crop_custom_aspect(geo_smooth):
    out = run("rotate", geo_smooth, angle=5.0, aspect="1:1")
    assert abs(out.shape[0] - out.shape[1]) <= 1 and out.min() > 0.04


@pytest.fixture(scope="module")
def lines_img():
    img = np.full((400, 600, 3), 0.7, np.float32)
    for y in range(60, 360, 45):
        cv2.line(img, (30, y), (570, y), (0.1, 0.1, 0.1), 3)
    for x in range(60, 560, 60):
        cv2.line(img, (x, 30), (x, 370), (0.15, 0.15, 0.15), 3)
    return img


@pytest.mark.parametrize("angle", [-4.0, 2.5, 6.0])
def test_detect_tilt_and_auto_level(lines_img, angle):
    h, w = lines_img.shape[:2]
    M = cv2.getRotationMatrix2D(((w - 1) / 2, (h - 1) / 2), angle, 1.0)
    tilted = cv2.warpAffine(lines_img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
    est = G.detect_tilt(tilted, "horizon")
    assert est is not None and abs(est + angle) < 0.35          # correction = -(applied) angle
    assert abs(G.detect_tilt(tilted, "verticals") + angle) < 0.35
    assert abs(G.detect_tilt(tilted, "auto") + angle) < 0.35
    out = run("auto_level", tilted)
    est2 = G.detect_tilt(out, "horizon")
    assert est2 is None or abs(est2) < 0.3
    assert out.shape[0] < h
    assert any("auto_level" in m for m in run.ctx.log)
    half = run("auto_level", tilted, amount=0.5)
    assert abs(G.detect_tilt(half, "horizon") + angle / 2) < 0.4
    # the straighten alias does the same
    assert run("straighten", tilted).shape == out.shape


def test_auto_level_no_lines_is_noop_with_note():
    rng = np.random.default_rng(0)
    noise = cv2.GaussianBlur(rng.random((200, 300, 3)).astype(np.float32), (0, 0), 6) * 0.2 + 0.3
    out = run("auto_level", noise)
    assert out.shape == noise.shape
    assert any("no dominant line" in m for m in run.ctx.log)
    manual = run("auto_level", noise, angle=3.0)
    assert manual.shape[0] < noise.shape[0]


def test_flip(geo_img):
    np.testing.assert_array_equal(run("flip", geo_img, horizontal=True), geo_img[:, ::-1])
    np.testing.assert_array_equal(run("flip", geo_img, vertical=True), geo_img[::-1])
    out = run("flip", geo_img, turns=1)
    assert out.shape[:2] == geo_img.shape[1::-1] and out.flags["C_CONTIGUOUS"]
    np.testing.assert_array_equal(run("flip", geo_img, turns=2), geo_img[::-1, ::-1])


def test_max_rect_in_polygon():
    poly = [[0, 0], [100, 0], [100, 60], [0, 60]]
    x0, y0, x1, y1 = G.max_rect_in_polygon(poly, 1.0)
    assert abs((x1 - x0) - 60) < 0.5 and abs((y1 - y0) - 60) < 0.5
    tri = [[0, 0], [100, 0], [50, 80]]                        # triangle: rectangle must sit inside
    r = G.max_rect_in_polygon(tri, 2.0)
    pts = np.array([[r[0], r[1]], [r[2], r[1]], [r[2], r[3]], [r[0], r[3]]])
    assert G._inside_convex(pts, np.array(tri, float)).all()
    assert (r[2] - r[0]) > 20


def test_perspective_manual_sliders_and_corners(geo_img, geo_smooth):
    up = run("perspective", geo_img, vertical=40, mode="keep")
    check(up, geo_img.shape)
    # +vertical widens the top: content near the top corners is pushed outward (black/border appears at the bottom)
    top_valid = (up[2:6, :, :].max(axis=2) > 0).mean()
    bot_valid = (up[-6:-2, :, :].max(axis=2) > 0).mean()
    assert top_valid > bot_valid
    cr = run("perspective", geo_smooth, vertical=40)                   # auto-cropped to the valid area
    assert cr.shape[0] < geo_img.shape[0] and cr.min() > 0.04
    # roll via `rotate` slider rotates (CCW positive) like the rotate op
    r = run("perspective", geo_img, rotate=5.0, mode="keep")
    assert not np.allclose(r, geo_img)
    # 4-point correction: map a quad to the full frame
    h, w = geo_img.shape[:2]
    quad = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]
    out = run("perspective", geo_img, corners=quad)
    ref = cv2.resize(geo_img[h // 4:3 * h // 4, w // 4:3 * w // 4], (w, h), interpolation=cv2.INTER_CUBIC)
    assert np.abs(out[10:-10, 10:-10] - ref[10:-10, 10:-10]).mean() < 0.02


def _vertical_dev(img):
    segs, s = G._line_segments(img)
    dx, dy = segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1]
    phi = (np.degrees(np.arctan2(dy, dx)) + 90) % 180 - 90
    dev = np.where(phi >= 0, phi - 90.0, phi + 90.0)
    sel = np.abs(dev) < 25
    L = np.hypot(dx, dy)[sel]
    d = np.abs(dev[sel])
    order = np.argsort(d)
    cum = np.cumsum(L[order])
    return float(d[order][np.searchsorted(cum, 0.5 * cum[-1])]), int(sel.sum())     # length-weighted median


def test_perspective_auto_vertical_reduces_convergence():
    W, H = 900, 600
    img = np.full((H, W, 3), 0.8, np.float32)
    for x in range(80, 830, 50):
        cv2.line(img, (x, 40), (x, 560), (0.1, 0.1, 0.1), 3)
    for y in range(70, 560, 55):
        cv2.line(img, (60, y), (840, y), (0.15, 0.15, 0.15), 3)
    f = 0.9 * W
    Hh = G.rotation_homography(img.shape, f, pitch=-12.0)
    warped = cv2.warpPerspective(img, Hh, (W, H), borderMode=cv2.BORDER_CONSTANT, borderValue=0.8)
    before, n0 = _vertical_dev(warped)
    assert before > 2.5 and n0 > 10
    focal35 = f / W * 36.0
    out = run("perspective", warped, auto="vertical", focal=focal35, mode="keep")
    after, _ = _vertical_dev(out)
    assert after < 0.4 * before
    assert any("vertical correction" in m for m in run.ctx.log)
    half = run("perspective", warped, auto="vertical", focal=focal35, mode="keep", strength=0.5)
    mid, _ = _vertical_dev(half)
    assert after < mid < before
    cropped = run("perspective", warped, auto="vertical", focal=focal35)       # default: auto-crop
    assert cropped.shape[0] < H and cropped.shape[1] < W
    both = run("perspective", warped, auto="both", focal=focal35, mode="keep")
    check(both)
    # no lines -> no-op + note
    blank = np.full((200, 300, 3), 0.5, np.float32)
    assert run("perspective", blank, auto="vertical").shape == blank.shape
    assert any("no vertical lines" in m for m in run.ctx.log)


def test_resize(disp):
    h, w = disp.shape[:2]
    out = run("resize", disp, long_edge=240)
    assert out.shape[:2] == (160, 240)
    assert run("resize", disp, short_edge=160).shape[:2] == (160, 240)
    assert run("resize", disp, width=96).shape[:2] == (64, 96)
    assert run("resize", disp, height=64).shape[:2] == (64, 96)
    mp = run("resize", disp, megapixels=0.05)
    assert abs(mp.shape[0] * mp.shape[1] - 50000) / 50000 < 0.03
    up = run("resize", disp, scale=2.0, method="lanczos")
    assert up.shape[:2] == (2 * h, 2 * w) and up.min() >= 0 and up.max() <= 1
    assert run("resize", disp, long_edge=960, no_upscale=True) is disp
    for m in ("area", "cubic", "linear"):
        assert run("resize", disp, scale=0.5, method=m).shape[:2] == (h // 2, w // 2)
    np.testing.assert_allclose(run("resize", disp, scale=0.5).mean(), disp.mean(), atol=0.005)


# --------------------------------------------------------------------------- lens correction
R50_META = {"Make": "Canon", "Model": "Canon EOS R50", "LensModel": "RF-S18-45mm F4.5-6.3 IS STM",
            "FocalLength": 18.0, "FNumber": 4.5, "ISO": 400}


def test_lensfun_matches_bundled_r50_kit_lens():
    pytest.importorskip("lensfunpy")
    mod, info = G.lens_modifier(R50_META, (320, 480))
    assert mod is not None, info
    assert "18-45" in info["lens"] and info["camera"].endswith("R50")
    mod, info = G.lens_modifier({**R50_META, "LensModel": "Canon RF-S 18-150mm F3.5-6.3 IS STM", "FocalLength": 50.0}, (320, 480))
    assert mod is not None and "18-150" in info["lens"]
    mod, info = G.lens_modifier({**R50_META, "LensModel": "Sigma 35mm F1.4 ART"}, (320, 480))
    assert mod is None and "not matched" in info
    mod, info = G.lens_modifier({"Make": "Nikon", "Model": "D850"}, (320, 480))
    assert mod is None


def test_lens_correct_vignetting_and_distortion():
    pytest.importorskip("lensfunpy")
    white = flat(320, 480, [0.4] * 3)
    out = run("lens_correct", white, meta=R50_META, distortion=False, tca=False, scale=1.0)
    c = out[160, 240, 0]
    corner = out[2, 2, 0]
    assert corner > c * 1.05 and abs(c - 0.4) < 0.01                        # corners brightened, centre unchanged
    assert any("RF-S 18-45" in m for m in run.ctx.log)
    # distortion remap changes a grid near the edges but keeps the centre
    grid = np.full((320, 480, 3), 0.9, np.float32)
    for x in range(0, 480, 40):
        cv2.line(grid, (x, 0), (x, 319), (0.1, 0.1, 0.1), 2)
    for y in range(0, 320, 40):
        cv2.line(grid, (0, y), (479, y), (0.1, 0.1, 0.1), 2)
    fixed = run("lens_correct", grid, meta=R50_META, vignetting=False, tca=False)
    check(fixed, grid.shape)
    assert np.abs(fixed - grid)[:40].mean() > 5 * np.abs(fixed - grid)[140:180, 200:280].mean() or \
        np.abs(fixed - grid)[:40].mean() > 0.01
    # camera JPEGs are skipped (already corrected) unless forced
    skipped = run("lens_correct", white, meta=R50_META, kind="jpeg")
    np.testing.assert_array_equal(skipped, white)
    assert any("JPEG" in m for m in run.ctx.log)
    forced = run("lens_correct", white, meta=R50_META, kind="jpeg", force=True, distortion=False, tca=False, scale=1.0)
    assert forced[2, 2, 0] > forced[160, 240, 0]


def test_lens_correct_graceful_fallbacks(monkeypatch):
    img = flat(120, 160, [0.4] * 3)
    out = run("lens_correct", img, meta={"Make": "Canon", "Model": "Canon EOS R50", "LensModel": "Mystery 50mm"})
    np.testing.assert_array_equal(out, img)
    assert any("lens_correct" in m for m in run.ctx.log)
    out = run("lens_correct", img, meta={})
    np.testing.assert_array_equal(out, img)
    # lensfunpy missing -> note + no crash (+ manual fallback still applied)

    def boom(name, purpose=""):
        raise MissingDependency("lensfunpy not installed")

    monkeypatch.setattr(G, "lazy_import", boom)
    out = run("lens_correct", img, meta=R50_META)
    np.testing.assert_array_equal(out, img)
    assert any("unavailable" in m for m in run.ctx.log)
    man = run("lens_correct", img, meta=R50_META, vignette=50)
    assert man[0, 0, 0] > man[60, 80, 0]


def test_manual_lens_fix():
    grid = np.full((320, 480, 3), 0.9, np.float32)
    for x in range(0, 480, 40):
        cv2.line(grid, (x, 0), (x, 319), (0.1, 0.1, 0.1), 2)
    base = np.full((320, 480, 3), 0.4, np.float32)
    v = run("lens_correct", base, auto=False, vignette=40)
    assert v[0, 0, 0] > 1.3 * base[0, 0, 0] and abs(v[160, 240, 0] - 0.4) < 2e-3
    d = run("lens_correct", grid, auto=False, k1=-0.12)
    check(d, grid.shape)
    np.testing.assert_allclose(d[150:170, 235:245], grid[150:170, 235:245], atol=0.15)   # centre ~unchanged
    assert np.abs(d - grid)[:, :40].mean() > np.abs(d - grid)[:, 220:260].mean()
    # pincushion correction (k1>0) would sample outside: autoscale zooms so that no border pixels are replicated
    p = run("lens_correct", grid, auto=False, k1=0.1, autoscale=True)
    check(p, grid.shape)
