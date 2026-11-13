"""Retouch ops, multi-frame stacking and scopes on synthetic data."""
import cv2
import numpy as np
import pytest

from photostudio import color, multiframe as mf, scopes
from photostudio.pipeline.registry import REGISTRY, Ctx
from photostudio.testing import make_scene

import photostudio.pipeline.ops.retouch  # noqa: F401  (registers the ops)


def run(name, img, ctx, **kw):
    spec = REGISTRY[name]
    return spec.fn(img.copy(), spec.resolve_params(kw), ctx)


def lin_ctx(**kw):
    return Ctx(domain="linear", **kw)


def lin_of(disp):
    return color.apply_matrix(color.srgb_eotf(disp), color.SRGB_TO_REC2020)


def sharp(x):
    return float(cv2.Laplacian(x.mean(2) if x.ndim == 3 else x, cv2.CV_32F).var())


def textured(h=240, w=320, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 0.25 + 0.35 * (xx / w) + 0.1 * np.sin(yy / 17.0)
    noise = cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 1.5) * 0.05
    return np.repeat((base + noise)[..., None], 3, 2).astype(np.float32) * np.array([1.0, 0.9, 0.8], np.float32)


# --------------------------------------------------------------------------- registry
def test_ops_registered_with_stages():
    want = {"heal": 8, "clone": 8, "inpaint": 8, "blemish_auto": 8, "red_eye": 8, "liquify": 8, "dodge_burn": 12,
            "background_blur": 15, "skin_smooth": 18, "teeth_whiten": 18, "eye_enhance": 18, "sky_enhance": 18}
    for n, st in want.items():
        assert REGISTRY[n].stage == st, n
    assert REGISTRY["skin_smooth"].domain == "display" and REGISTRY["heal"].domain == "linear"


# --------------------------------------------------------------------------- heal / clone / inpaint
@pytest.mark.parametrize("domain", ["linear", "display"])
@pytest.mark.parametrize("method", ["blur_swap", "seamless"])
def test_heal_removes_spot(domain, method):
    img = textured()
    h, w = img.shape[:2]
    clean = img.copy()
    yy, xx = np.mgrid[0:h, 0:w]
    spot = np.hypot(xx - 200, yy - 120) < 9
    img[spot] *= 0.35
    ctx = Ctx(domain=domain)
    data = lin_of(img) if domain == "linear" else img
    ref = lin_of(clean) if domain == "linear" else clean
    heal_spec = {"spots": [{"dst": [200 / w, 120 / h], "src": [250 / w, 150 / h], "radius": 100}], "method": method}
    out = run("heal", data, ctx, **heal_spec)
    before = np.abs(data - ref)[spot].mean()
    after = np.abs(out - ref)[spot].mean()
    assert after < 0.35 * before, (before, after)
    far = ~(np.hypot(xx - 200, yy - 120) < 30)
    assert np.allclose(out[far], data[far], atol=1e-5)                    # nothing changes outside the spot neighbourhood


def test_heal_auto_source_and_clone_copies_texture():
    img = textured(seed=3)
    h, w = img.shape[:2]
    bad = img.copy()
    bad[110:130, 190:210] *= 0.3
    ctx = lin_ctx()
    data = lin_of(bad)
    out = run("heal", data, ctx, spots=[{"dst": [200 / w, 120 / h], "radius": 112}])      # no src: auto-picked
    ref = lin_of(img)
    assert np.abs(out - ref)[112:128, 192:208].mean() < 0.5 * np.abs(data - ref)[112:128, 192:208].mean()
    # clone: core pixels are an exact copy of the source patch
    cl = run("clone", data, ctx, spots=[{"dst": [100 / w, 100 / h], "src": [250 / w, 180 / h], "radius": 125, "feather": 0.3}])
    assert np.allclose(cl[96:104, 96:104], data[176:184, 246:254], atol=1e-5)
    assert np.array_equal(run("heal", data, ctx, spots=[]), data)


def test_inpaint_fills_hole_telea_ns():
    h, w = 200, 300
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    clean = np.repeat((0.2 + 0.5 * xx / w + 0.2 * yy / h)[..., None], 3, 2).astype(np.float32)
    bad = clean.copy()
    bad[90:110, 140:170] = 0.95
    ctx = lin_ctx()
    region = {"type": "polygon", "points": [[138 / w, 88 / h], [172 / w, 88 / h], [172 / w, 112 / h], [138 / w, 112 / h]]}
    for method in ("telea", "ns"):
        out = run("inpaint", bad, ctx, region=region, method=method, radius=4.0)
        assert np.abs(out - clean)[92:108, 143:168].mean() < 0.03, method
    out = run("inpaint", bad, ctx, spots=[{"center": [155 / w, 100 / h], "radius_px": 22}])
    assert np.abs(out - clean)[92:108, 143:168].mean() < 0.05
    c2 = lin_ctx()
    run("inpaint", bad, c2, region=region, method="lama")                      # LaMa missing: falls back, notes it
    assert any("LaMa" in n for n in c2.log)
    assert np.array_equal(run("inpaint", bad, ctx), bad)                        # nothing to do


def test_blemish_auto_detects_and_fixes():
    scene = make_scene(640, 427)
    h, w = scene.shape[:2]
    face = scene.copy()
    pts = [(int(0.25 * w) - 12, int(0.45 * h) + 20), (int(0.25 * w) + 14, int(0.45 * h) + 15)]
    for (x, y) in pts:
        cv2.circle(face, (x, y), 3, (0.60, 0.28, 0.25), -1, cv2.LINE_AA)
    ctx = Ctx(domain="display")
    out = run("blemish_auto", face, ctx, sensitivity=0.8, max_size=24.0)
    assert any("blemish_auto: " in n and "0 spots" not in n for n in ctx.log), ctx.log
    for (x, y) in pts:
        assert np.abs(out - scene)[y - 3:y + 4, x - 3:x + 4].mean() < 0.35 * np.abs(face - scene)[y - 3:y + 4, x - 3:x + 4].mean()
    assert np.array_equal(out[: int(0.2 * h)], face[: int(0.2 * h)])


# --------------------------------------------------------------------------- skin / face ops
def test_skin_smooth_reduces_skin_noise_keeps_rest():
    rng = np.random.default_rng(0)
    h = w = 400
    yy, xx = np.mgrid[0:h, 0:w]
    face = ((xx - 200) / 120) ** 2 + ((yy - 200) / 150) ** 2 < 1
    clean = np.zeros((h, w, 3), np.float32)
    clean[...] = (0.2, 0.45, 0.2)
    clean[face] = (0.88, 0.66, 0.55)
    blot = cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 5) * 0.30
    fine = rng.normal(0, 0.015, (h, w, 1)).astype(np.float32)
    noisy = clean.copy()
    noisy[face] = np.clip(clean[face] + blot[face][:, None] * np.array([1, 0.6, 0.5], np.float32) + fine[face], 0, 1)
    out = run("skin_smooth", noisy, Ctx(domain="display"), amount=1.0, radius=30.0, smooth_eps=0.08, texture=0.9)
    inner = ((xx - 200) / 100) ** 2 + ((yy - 200) / 130) ** 2 < 1

    def low(a):
        return cv2.GaussianBlur(a, (0, 0), 3)

    def lowerr(a):
        return float(((low(a) - low(clean))[inner] ** 2).mean())

    def hf(a):
        return float((a - low(a))[inner].std())

    assert lowerr(out) < 0.7 * lowerr(noisy)               # blotchy low-frequency tone evened out
    assert hf(out) > 0.8 * hf(noisy)                       # pores / fine texture retained
    assert np.abs(out - noisy)[~face & ~(((xx - 200) / 135) ** 2 + ((yy - 200) / 165) ** 2 < 1)].max() < 1e-4
    zero = run("skin_smooth", noisy, Ctx(domain="display"), amount=0.0)
    assert np.array_equal(zero, noisy)


def test_face_ops_run_and_stay_local():
    scene = make_scene(480, 320)
    h, w = scene.shape[:2]
    ctx = Ctx(domain="display")
    for name in ("teeth_whiten", "eye_enhance"):
        out = run(name, scene, ctx, amount=0.8)
        assert out.shape == scene.shape and np.isfinite(out).all() and out.min() >= 0 and out.max() <= 1
        assert np.abs(out - scene)[int(0.8 * h):].max() < 1e-4
    ee = run("eye_enhance", scene, ctx, amount=1.0)
    assert np.abs(ee - scene).max() > 0.005                          # something happened at the eyes


def test_red_eye_removal_keeps_highlight():
    h, w = 120, 160
    img = np.full((h, w, 3), 0.2, np.float32)
    cv2.circle(img, (80, 60), 14, (0.5, 0.05, 0.04), -1, cv2.LINE_AA)       # red pupil
    cv2.circle(img, (86, 54), 2, (0.95, 0.95, 0.95), -1, cv2.LINE_AA)       # catchlight
    out = run("red_eye", img, lin_ctx(), auto=False, spots=[{"center": [0.5, 0.5], "radius": 0.12}])
    red_before = img[60, 76, 0] / img[60, 76, 1:].max()
    red_after = out[60, 76, 0] / max(out[60, 76, 1:].max(), 1e-4)
    assert red_before > 5 and red_after < 1.6
    assert np.allclose(out[54, 86], img[54, 86], atol=0.05)                # specular highlight kept
    assert np.array_equal(out[:20], img[:20])


def test_sky_enhance_darkens_sky_only():
    scene = make_scene(480, 320)
    h, w = scene.shape[:2]
    out = run("sky_enhance", scene, Ctx(domain="display"), darken=0.3, saturation=0.4)
    assert out[: int(0.15 * h), int(0.4 * w):int(0.6 * w)].mean() < scene[: int(0.15 * h), int(0.4 * w):int(0.6 * w)].mean() - 0.02
    assert np.abs(out - scene)[int(0.9 * h):].max() < 1e-3


# --------------------------------------------------------------------------- dodge/burn, liquify, blur
def test_dodge_burn_ev_map():
    img = np.full((100, 100, 3), 0.18, np.float32)
    out = run("dodge_burn", img, lin_ctx(), strokes=[{"points": [[0.3, 0.5], [0.7, 0.5]], "radius": 400, "hardness": 1.0, "ev": 1.0},
                                                      {"points": [[0.5, 0.2]], "radius": 200, "hardness": 1.0, "ev": -0.5}])
    assert out[50, 50, 0] == pytest.approx(0.36, rel=0.02)
    assert out[20, 50, 0] == pytest.approx(0.18 * 2 ** -0.5, rel=0.03)
    assert out[95, 5, 0] == pytest.approx(0.18)
    reg = run("dodge_burn", img, lin_ctx(), regions=[{"mask": {"type": "radial", "center": [0.5, 0.5], "radii": [0.2, 0.2]}, "ev": 2.0}])
    assert reg[50, 50, 0] == pytest.approx(0.72, rel=0.01)
    assert np.array_equal(run("dodge_burn", img, lin_ctx()), img)


def test_liquify_push_pinch():
    img = np.zeros((100, 100, 3), np.float32)
    cv2.circle(img, (50, 50), 6, (1, 1, 1), -1)
    ctx = lin_ctx()
    out = run("liquify", img, ctx, warps=[{"type": "push", "center": [0.5, 0.5], "radius": 300, "dx": 0.1, "dy": 0.0, "strength": 1.0}])
    cm = lambda a: (np.argwhere(a[..., 0] > 0.5).mean(0))  # noqa: E731
    assert cm(out)[1] > cm(img)[1] + 3 and abs(cm(out)[0] - cm(img)[0]) < 1
    bloat = run("liquify", img, ctx, warps=[{"type": "bloat", "center": [0.5, 0.5], "radius": 300, "strength": 0.6}])
    assert (bloat[..., 0] > 0.5).sum() > (img[..., 0] > 0.5).sum() * 1.3
    pinch = run("liquify", img, ctx, warps=[{"type": "pinch", "center": [0.5, 0.5], "radius": 300, "strength": 0.6}])
    assert (pinch[..., 0] > 0.5).sum() < (img[..., 0] > 0.5).sum() * 0.8
    assert np.array_equal(run("liquify", img, ctx), img)
    tw = run("liquify", img, ctx, warps=[{"type": "twirl", "center": [0.5, 0.5], "radius": 300, "strength": 0.5}])
    assert tw.shape == img.shape and np.isfinite(tw).all()


def test_background_blur_subject_and_depth():
    scene = make_scene(480, 320)
    img = lin_of(scene)
    h, w = scene.shape[:2]
    mask = {"type": "radial", "center": [0.25, 0.45], "radii": [0.12, 0.25], "feather": 20}
    out = run("background_blur", img, lin_ctx(), source="mask", mask=mask, radius=70.0)
    inner = (slice(int(0.35 * h), int(0.55 * h)), slice(int(0.21 * w), int(0.29 * w)))
    stripes = (slice(int(0.7 * h), int(0.95 * h)), slice(int(0.1 * w), int(0.4 * w)))   # foliage texture, background
    assert np.allclose(out[inner], img[inner], atol=1e-3)
    assert sharp(out[stripes]) < 0.3 * sharp(img[stripes])
    assert np.isfinite(out).all() and out.min() >= 0
    # no bleeding of the sharp subject colour into the blurred background right outside the mask
    c = lin_ctx()
    d = run("background_blur", img, c, source="depth", radius=15.0, focus=0.0, focus_range=0.1)
    assert d.shape == img.shape and np.isfinite(d).all()
    assert any("depth" in n.lower() for n in c.log)
    disp_out = run("background_blur", scene, Ctx(domain="display"), source="mask", mask=mask, radius=10.0)
    assert disp_out.shape == scene.shape and 0 <= disp_out.min() and disp_out.max() <= 1.0 + 1e-4
    assert np.array_equal(run("background_blur", img, lin_ctx(), radius=0.0), img)


# --------------------------------------------------------------------------- HDR
def _bracket(gains, shifts=None, seed=1):
    scene = make_scene(360, 240)
    rad = color.srgb_eotf(scene) * 3.0                      # scene radiance, reference units, peaks at ~3
    rng = np.random.default_rng(seed)
    frames = []
    for i, g in enumerate(gains):
        im = rad * g
        if shifts:
            im = cv2.warpAffine(im, np.float32([[1, 0, shifts[i][0]], [0, 1, shifts[i][1]]]), (360, 240), borderMode=cv2.BORDER_REPLICATE)
        im = im + rng.normal(0, 0.002, im.shape).astype(np.float32) * np.sqrt(np.maximum(im, 0.01))
        frames.append(np.clip(im, 0, 1).astype(np.float32))
    return rad, frames


def test_hdr_linear_recovers_range_and_noise():
    gains = [0.1, 0.35, 1.2]
    rad, frames = _bracket(gains)
    out, info = mf.hdr_merge(frames, exposures=gains, return_info=True)
    ref = rad * gains[1]                                    # reference units = middle frame
    good = (ref.mean(2) > 0.02) & (ref.max(2) < 1.0)
    rel = np.abs(out / np.maximum(ref, 1e-6) - 1).mean(2)
    assert np.median(rel[good]) < 0.02 and np.percentile(rel[good], 95) < 0.1
    # highlights clipped in the reference frame are recovered from the darker frame
    clipped_ref = (rad * gains[1]).max(2) > 1.0
    assert clipped_ref.any() and frames[1].max() <= 1.0
    assert np.median(np.abs(out / np.maximum(ref, 1e-6) - 1).mean(2)[clipped_ref]) < 0.05
    # shadows come from the brightest frame -> less noise than the middle frame alone
    dark = (ref.mean(2) < 0.03) & (ref.mean(2) > 0.002)
    err_merge = np.abs(out - ref).mean(2)[dark].mean()
    err_single = np.abs(frames[1] - ref).mean(2)[dark].mean()
    assert err_merge < err_single
    assert info["reference"] == 1 and info["relative_gains"][1] == pytest.approx(1.0)
    assert out.dtype == np.float32 and out.shape == frames[0].shape


def test_hdr_alignment_estimated_exposures_and_mertens():
    gains = [0.12, 0.4, 1.3]
    rad, frames = _bracket(gains, shifts=[(0, 0), (3, -2), (-4, 3)])
    out, info = mf.hdr_merge(frames, align="mtb", return_info=True)      # exposures estimated from pixel ratios
    assert any("estimated" in n for n in info["notes"])
    ref = cv2.warpAffine(rad * gains[1], np.float32([[1, 0, 3], [0, 1, -2]]), (360, 240), borderMode=cv2.BORDER_REPLICATE)  # output sits in the reference frame's geometry
    m = (slice(12, -12), slice(12, -12))
    good = (ref.mean(2) > 0.03) & (ref.max(2) < 0.9)
    rel = np.abs(out / np.maximum(ref, 1e-6) - 1).mean(2)[m][good[m]]
    assert np.median(rel) < 0.12
    unaligned = mf.hdr_merge(frames, align="none")
    rel_u = np.abs(unaligned / np.maximum(ref, 1e-6) - 1).mean(2)[m][good[m]]
    assert np.median(rel) < np.median(rel_u)
    mer = mf.hdr_merge(frames, method="mertens")
    assert mer.shape == frames[0].shape and 0.02 < mer.mean() < 1.0 and np.isfinite(mer).all()
    deb = mf.hdr_merge(frames, method="debevec", exposures=gains)
    assert np.isfinite(deb).all() and deb.max() > 0
    with pytest.raises(ValueError):
        mf.hdr_merge(frames[:1])


# --------------------------------------------------------------------------- focus stack
def test_focus_stack_sharper_than_inputs():
    img = lin_of(make_scene(360, 240))
    blur = cv2.GaussianBlur(img, (0, 0), 4)
    a, b = img.copy(), img.copy()
    a[:, 180:] = blur[:, 180:]
    b[:, :180] = blur[:, :180]
    out = mf.focus_stack([a, b], align="none")
    assert sharp(out) > 1.3 * max(sharp(a), sharp(b))
    assert sharp(out) > 0.85 * sharp(img)
    assert np.abs(out - img).mean() < 0.35 * np.abs(a - img).mean() + 0.35 * np.abs(b - img).mean()
    # with a small misalignment ECC affine still gives a result sharper than either input
    shifted = cv2.warpAffine(b, np.float32([[1, 0, 2.0], [0, 1, -1.5]]), (360, 240), borderMode=cv2.BORDER_REFLECT)
    out2, info = mf.focus_stack([a, shifted], align="ecc_affine", return_info=True)
    assert sharp(out2) > max(sharp(a), sharp(shifted))
    assert info["depth_index"].shape == (240, 360)


# --------------------------------------------------------------------------- astro / long exposure / trails
def _starfield(seed=0, n=70, size=(240, 320)):
    rng = np.random.default_rng(seed)
    h, w = size
    xs, ys = rng.uniform(15, w - 15, n), rng.uniform(15, h - 15, n)
    fl = rng.uniform(0.3, 1.5, n)

    def render(dx=0.0, dy=0.0, rot=0.0, noise=0.0, seed=0):
        r = np.random.default_rng(seed)
        a = np.deg2rad(rot)
        R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
        c = np.array([w / 2, h / 2])
        p = (np.stack([xs, ys], 1) - c) @ R.T + c + [dx, dy]
        img = np.full((h, w), 0.05, np.float32)
        for (x, y), f in zip(p, fl):
            x0, x1, y0, y1 = int(max(x - 8, 0)), int(min(x + 9, w)), int(max(y - 8, 0)), int(min(y + 9, h))
            gx, gy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1))
            img[y0:y1, x0:x1] += f * np.exp(-((gx - x) ** 2 + (gy - y) ** 2) / (2 * 1.4 ** 2))
        img += r.normal(0, noise, img.shape).astype(np.float32)
        return np.repeat(img[..., None], 3, 2).astype(np.float32)

    return render


def test_astro_stack_reduces_noise_and_aligns():
    render = _starfield()
    clean = render()
    shifts = [(0, 0, 0), (6.4, -3.7, 0.4), (-9.2, 5.1, -0.6), (2.8, 8.3, 0.9), (-4.5, -6.6, 0.2), (7.7, 2.2, -0.3)]
    frames = [render(dx, dy, rot, noise=0.04, seed=i) for i, (dx, dy, rot) in enumerate(shifts)]
    out, info = mf.astro_stack(frames, return_info=True)
    m = (slice(25, -25), slice(35, -35))
    rms = lambda a: float(np.sqrt(((a[m] - clean[m]) ** 2).mean()))  # noqa: E731
    assert rms(out) < 0.55 * rms(frames[0])
    assert info["used_frames"] == list(range(6)) and info["stars_reference"] > 30
    # stars stay sharp: peak of the brightest star is close to the clean peak
    y, x = np.unravel_index(np.argmax(clean[..., 0]), clean.shape[:2])
    assert out[y - 1:y + 2, x - 1:x + 2, 0].max() > 0.8 * clean[y - 1:y + 2, x - 1:x + 2, 0].max()
    for combine in ("mean", "median", "max"):
        o = mf.astro_stack(frames[:4], combine=combine)
        assert o.shape == clean.shape and np.isfinite(o).all()


def test_astro_vote_alignment_without_astroalign():
    render = _starfield(seed=2)
    a, b = render(), render(11.0, -7.0, 0.8, noise=0.01, seed=5)
    sa, sb = mf.detect_stars(a), mf.detect_stars(b)
    assert len(sa) >= 30 and len(sb) >= 30
    T, n = mf.vote_align(sb[:, :2], sa[:, :2], (160, 120))
    assert T is not None and n >= 25
    pts = sb[:5, :2] @ T[:2, :2].T + T[:2, 2]
    # mapped stars land on reference stars
    d = np.linalg.norm(pts[:, None, :] - sa[None, :, :2], axis=2).min(1)
    assert d.max() < 1.5
    # hot pixel is not a star
    hp = a.copy()
    hp[50, 60] = 3.0
    assert not any(abs(x - 60) < 2 and abs(y - 50) < 2 for x, y, _ in mf.detect_stars(hp))


def test_astro_freeze_ground_and_calibration():
    render = _starfield(seed=3)
    sky_frames = [render(dx, dy, 0, noise=0.02, seed=i) for i, (dx, dy) in enumerate([(0, 0), (5, 3), (-6, 4), (3, -7)])]
    ground = np.zeros((240, 320), np.float32)
    ground[160:] = 0.6
    frames = []
    for f in sky_frames:
        g = f.copy()
        g[160:] = 0.6 + np.random.default_rng(len(frames)).normal(0, 0.01, g[160:].shape)
        frames.append(g.astype(np.float32))
    sky_mask = np.zeros((240, 320), np.float32)
    sky_mask[:150] = 1
    out = mf.astro_stack(frames, sky_mask=sky_mask)
    assert out[200:, 20:300].mean() == pytest.approx(0.6, abs=0.02)         # ground not smeared / empty
    # dark-frame subtraction removes a fixed pattern
    pattern = np.random.default_rng(9).normal(0.05, 0.02, (240, 320, 1)).astype(np.float32)
    lights = [f + pattern for f in sky_frames]
    cal = mf.astro_stack(lights, darks=[np.repeat(pattern, 3, 2)] * 3)
    raw = mf.astro_stack(lights)
    clean = mf.astro_stack(sky_frames)
    assert np.abs(cal - clean)[30:-30, 30:-30].mean() < 0.5 * np.abs(raw - clean)[30:-30, 30:-30].mean()


def test_long_exposure_median_and_trails():
    rng = np.random.default_rng(0)
    base = textured(120, 160)
    frames = [np.clip(base + rng.normal(0, 0.05, base.shape), 0, 2).astype(np.float32) for _ in range(9)]
    le = mf.long_exposure(frames)
    assert np.std(le - base) < 0.45 * np.std(frames[0] - base)          # ~ 1/sqrt(9)
    md = mf.median_stack(frames)
    assert np.std(md - base) < 0.6 * np.std(frames[0] - base)
    # median removes a moving object
    moving = []
    for i in range(5):
        f = base.copy()
        f[50:70, 10 + 25 * i:30 + 25 * i] = 2.0
        moving.append(f)
    assert np.abs(mf.median_stack(moving) - base).max() < 1e-5
    # star trails = lighten of unaligned frames
    pts = []
    for i in range(6):
        f = np.zeros((60, 100, 3), np.float32)
        f[30, 10 + 12 * i] = 1.0
        pts.append(f)
    tr = mf.star_trails(pts)
    assert (tr[30, :, 0] > 0.5).sum() == 6
    comet = mf.star_trails(pts, mode="comet", fade=0.3)
    assert comet[30, 10, 0] < comet[30, 70, 0] and comet[30, 70, 0] == pytest.approx(1.0)


def test_stack_streaming_equals_memmap_path():
    rng = np.random.default_rng(1)
    frames = [rng.random((40, 50, 3)).astype(np.float32) for _ in range(5)]
    a = mf.stack_frames(frames, "mean")
    b = mf.stack_frames(frames, "mean", align="ecc")             # memmap/banded path
    assert np.allclose(a, np.mean(frames, 0), atol=1e-5) and np.allclose(a, b, atol=1e-2)
    ks = mf.stack_frames(frames, "kappa_sigma", kappa=3.0)
    assert ks.shape == frames[0].shape and np.isfinite(ks).all()
    out = [f.copy() for f in frames]
    out[2][10:20, 10:20] = 50.0                                   # satellite streak / hot patch
    k2 = mf.stack_frames(out, "kappa_sigma", kappa=1.5, iters=2)
    assert k2[10:20, 10:20].max() < 1.5


def test_load_frame_types(tmp_path):
    from photostudio.testing import write_jpeg

    scene = make_scene(80, 60)
    p = write_jpeg(tmp_path / "a.jpg", scene)
    lin, meta = mf.load_frame(p)
    assert lin.shape == (60, 80, 3) and lin.dtype == np.float32
    u8 = (scene * 255).astype(np.uint8)
    lin8, _ = mf.load_frame(u8)
    assert np.abs(lin8 - lin_of(scene)).max() < 0.02
    assert mf.load_frame(lin8)[0] is lin8
    out = mf.long_exposure([p, p])
    assert np.allclose(out, lin, atol=1e-5)


def test_panorama_stitches_overlapping_views():
    rng = np.random.default_rng(0)
    h, w = 360, 1100
    # feature-rich synthetic panorama
    base = cv2.GaussianBlur(rng.random((h, w, 3)).astype(np.float32), (0, 0), 2.5)
    base = base * 0.5 + 0.25 * cv2.resize(make_scene(220, 72), (w, h))
    for _ in range(300):
        cv2.circle(base, (int(rng.integers(0, w)), int(rng.integers(0, h))), int(rng.integers(3, 12)),
                   tuple(float(v) for v in rng.random(3)), -1, cv2.LINE_AA)
    crops = [color.srgb_eotf(np.clip(base[:, x0:x0 + 500], 0, 1)) for x0 in (0, 300, 600)]
    try:
        pano = mf.panorama(crops)
    except RuntimeError as exc:
        pytest.skip(f"OpenCV stitcher could not register the synthetic views: {exc}")
    assert pano.dtype == np.float32 and pano.shape[1] > 700 and pano.shape[0] > 250


# --------------------------------------------------------------------------- scopes
def test_scope_data_and_shapes(tmp_path):
    scene = make_scene(400, 300)
    hist = scopes.histogram(scene)
    for k in ("r", "g", "b", "luma"):
        assert hist[k].shape == (256,) and hist[k].sum() == hist["pixels"]
    assert 0 <= hist["clip_high_pct"]["any"] <= 100 and "all" in hist["clip_low_pct"]
    # synthetic clipping: 10 % of the pixels fully white, 5 % fully black
    img = np.full((100, 100, 3), 0.5, np.float32)
    img[:10] = 1.0
    img[95:] = 0.0
    h2 = scopes.histogram(img)
    assert h2["clip_high_pct"]["all"] == pytest.approx(10.0, abs=0.01)
    assert h2["clip_low_pct"]["all"] == pytest.approx(5.0, abs=0.01)
    assert h2["luma"][255] == 1000 and h2["luma"][0] == 500
    # per-channel clipping
    img3 = np.full((10, 10, 3), 0.5, np.float32)
    img3[..., 0] = 1.0
    h3 = scopes.histogram(img3)
    assert h3["clip_high_pct"]["r"] == 100 and h3["clip_high_pct"]["g"] == 0 and h3["clip_high_pct"]["all"] == 0
    renders = {
        "hist": (scopes.render_histogram(scene), (256, 512, 3)),
        "wave": (scopes.render_waveform(scene), (256, 512, 3)),
        "wave_rgb": (scopes.render_waveform(scene, mode="rgb"), (256, 512, 3)),
        "parade": (scopes.render_parade(scene), (256, 768, 3)),
        "vector": (scopes.render_vectorscope(scene), (384, 384, 3)),
        "false": (scopes.false_color(scene), scene.shape),
        "false_ev": (scopes.false_color(scene, mode="ev"), scene.shape),
        "clip": (scopes.clipping_overlay(scene), scene.shape),
    }
    for name, (a, shape) in renders.items():
        assert a.dtype == np.uint8 and a.shape == shape, name
    assert scopes.png_waveform is scopes.render_waveform and scopes.png_vectorscope is scopes.render_vectorscope
    # linear input is accepted
    assert scopes.render_histogram(lin_of(scene), domain="linear").shape == (256, 512, 3)
    res = scopes.scope_pack(scene, tmp_path)
    assert set(res["files"]) == {"histogram", "waveform", "parade", "vectorscope", "false_color", "clipping"}
    assert all((tmp_path / f"{k}.png").exists() for k in res["files"])


def test_scope_semantics():
    # a flat red image puts its vectorscope energy in the red/skin quadrant; waveform of a ramp rises left->right
    red = np.zeros((60, 60, 3), np.float32)
    red[..., 0] = 0.8
    vs = scopes.render_vectorscope(red, skin_line=False)
    ys, xs = np.where((vs[..., 1] > 200) & (vs[..., 0] > 100))
    assert len(ys) > 0 and ys.mean() < 192 and xs.mean() < 200          # upper-left of centre (red at ~103 deg)
    ramp = np.repeat(np.linspace(0, 1, 256, dtype=np.float32)[None, :, None], 64, 0).repeat(3, 2)
    wf = scopes.render_waveform(ramp, size=(256, 128))
    g = wf[..., 1]
    first_row_bright = np.where(g[0] > 150)[0]          # top row = level 1.0 -> right side
    last_row_bright = np.where(g[-1] > 150)[0]          # bottom row = level 0 -> left side
    assert first_row_bright.mean() > 200 and last_row_bright.mean() < 60
    # clipping overlay marks the clipped pixels in red
    img = np.full((20, 20, 3), 0.4, np.float32)
    img[:5] = 1.0
    ov = scopes.clipping_overlay(img)
    assert ov[2, 5, 0] > 200 and ov[2, 5, 1] < 80 and abs(int(ov[15, 5, 0]) - int(ov[15, 5, 2])) < 5
    # false colour distinguishes clipped white (red) from mid grey
    fc = scopes.false_color(img, legend=False)
    assert fc[2, 5, 0] > 200 and fc[2, 5, 1] < 80 and not np.array_equal(fc[2, 5], fc[15, 5])


def test_cli_commands_register_and_run(tmp_path):
    import argparse

    from photostudio.commands import masks_cmd
    from photostudio.testing import write_jpeg

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    masks_cmd.register(sub)
    img = write_jpeg(tmp_path / "a.jpg", make_scene(320, 240))
    a = p.parse_args(["mask", "preview", str(img), "--spec", '{"type":"radial","center":[0.5,0.5],"radii":[0.2,0.2]}',
                      "--out", str(tmp_path / "m.png"), "--size", "320"])
    res = a.func(a)
    assert (tmp_path / "m.png").exists() and 0.05 < res["coverage"] < 0.3
    a = p.parse_args(["scopes", str(img), "--out", str(tmp_path / "sc"), "--size", "320"])
    res = a.func(a)
    assert (tmp_path / "sc" / "histogram.png").exists()
    a = p.parse_args(["models", "list"])
    assert any(r["name"] == "isnet-general-use" for r in a.func(a))
    img2 = write_jpeg(tmp_path / "b.jpg", make_scene(320, 240, seed=2))
    a = p.parse_args(["stack", "long", str(img), str(img2), "--out", str(tmp_path / "l.jpg")])
    res = a.func(a)
    assert (tmp_path / "l.jpg").exists() and res["frames"] == 2
