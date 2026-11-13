"""Tests for the art modes: effects.py + art_* op modules and photostudio/presets/art/*.yaml (synthetic scene only)."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import numpy as np
import pytest
import yaml

from photostudio import color, presets, testing
from photostudio.pipeline import registry as _registry
from photostudio.pipeline.registry import REGISTRY, Ctx

_registry._ensure_loaded()

ROOT = Path(__file__).resolve().parents[1]
ART_DIR = ROOT / "photostudio" / "presets" / "art"
ART_MODULES = {f"photostudio.pipeline.ops.{m}" for m in ("effects", "art_pop", "art_paint", "art_film", "art_process")}
ART_OPS = sorted(n for n, s in REGISTRY.items() if s.fn.__module__ in ART_MODULES)
# Brush-stroke / segmentation ops may take several seconds at 1600 px (documented as slow).
SLOW_OPS = {"painterly", "van_gogh", "monet", "pointillism", "klimt", "watercolor", "gum_bichromate"}
ALLOWED_TAGS = {"pop", "painterly", "photographer", "cinema", "film", "process", "retro", "raster", "bw", "color", "slow"}


@pytest.fixture(scope="module")
def disp():
    return testing.make_scene(480, 320, seed=3)


@pytest.fixture(scope="module")
def lin(disp):
    return color.apply_matrix(color.srgb_eotf(disp), color.SRGB_TO_REC2020).astype(np.float32)


def _input_for(name, disp, lin):
    return lin if REGISTRY[name].domain == "linear" else disp


def run(name, img, seed=0, **params):
    spec = REGISTRY[name]
    p = spec.resolve_params(params)
    ctx = Ctx(domain="display" if spec.domain == "display" else "linear", kind="image", seed=seed)
    return spec.fn(np.array(img, np.float32, copy=True), p, ctx)


def hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32) / 255.0


def frac_in_palette(img, palette, tol=0.06):
    pal = np.stack([hex_rgb(c) for c in palette])
    d = np.abs(img[..., None, :] - pal[None, None]).max(axis=-1).min(axis=-1)
    return float((d <= tol).mean())


def _load_sheet_module():
    spec = importlib.util.spec_from_file_location("make_art_contact_sheet", ROOT / "scripts" / "make_art_contact_sheet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- op inventory
def test_op_inventory():
    assert len(ART_OPS) >= 80
    for must in ("grain", "vignette", "halation", "bloom", "orton", "soft_focus", "light_leak", "chromatic_shift", "posterize", "threshold",
                 "halftone", "duotone", "gradient_map", "scanlines", "noise", "border", "letterbox", "tilt_shift", "lens_blur",
                 "kmeans_quantize", "xdog", "paper_texture", "warhol", "lichtenstein", "fairey_hope", "banksy_stencil", "haring",
                 "pencil_sketch", "ink_xdog", "oil_paint", "painterly", "watercolor", "cel_toon", "pointillism", "low_poly", "pixel_art",
                 "mosaic", "mondrian", "klimt", "glitch", "vhs", "gameboy", "thermal", "xray", "solarize", "cyanotype", "sepia",
                 "wet_plate", "daguerreotype", "platinum", "lith", "infrared_bw", "aerochrome", "lomo", "cross_process", "risograph",
                 "day_for_night", "bleach_bypass", "technicolor_3strip", "teal_orange", "matrix_green", "blade_runner", "wes_anderson",
                 "film_emulation", "adams_zone", "moriyama", "salgado", "fan_ho", "leiter", "eggleston", "mccurry", "maier", "crewdson",
                 "sugimoto_sea", "lik_hdr"):
        assert must in ART_OPS, must
    for n in ("halation", "bloom", "lens_blur", "spectral_mix"):
        assert REGISTRY[n].stage == 15 and REGISTRY[n].domain == "linear"


def test_docstrings_present():
    for n in ART_OPS:
        assert REGISTRY[n].doc and len(REGISTRY[n].doc) > 20, n


# --------------------------------------------------------------------------- every op
@pytest.mark.parametrize("name", ART_OPS)
def test_op_runs(name, disp, lin):
    img = _input_for(name, disp, lin)
    out = run(name, img)
    assert out.shape == img.shape, name
    assert out.dtype == np.float32, name
    assert np.isfinite(out).all(), name
    if REGISTRY[name].domain == "display":
        assert out.min() >= -1e-5 and out.max() <= 1.0 + 1e-5, (name, out.min(), out.max())
    else:
        assert out.min() >= -1e-4, name
    assert float(np.abs(out - img).mean()) > 1e-4, f"{name} output is indistinguishable from the input"


@pytest.mark.parametrize("name", ART_OPS)
def test_op_deterministic(name, disp, lin):
    img = _input_for(name, disp, lin)
    a, b = run(name, img, seed=7), run(name, img, seed=7)
    assert np.allclose(a, b, atol=1e-6), f"{name} is not deterministic for a fixed seed"


@pytest.mark.parametrize("name", ["grain", "noise", "glitch", "dust_scratches", "light_leak", "risograph", "moriyama", "expired"])
def test_seed_changes_stochastic_ops(name, disp):
    if name == "expired":
        a, b = (run("film_emulation", disp, seed=s, stock="expired") for s in (1, 2))
    else:
        a, b = run(name, disp, seed=1), run(name, disp, seed=2)
    assert float(np.abs(a - b).mean()) > 1e-5, name


@pytest.mark.parametrize("name", ART_OPS)
def test_op_input_not_mutated(name, disp, lin):
    img = _input_for(name, disp, lin)
    before = img.copy()
    spec = REGISTRY[name]
    spec.fn(img, spec.resolve_params({}), Ctx(domain=spec.domain if spec.domain != "any" else "display", kind="image"))
    assert np.array_equal(before, img), f"{name} modified its input in place"


def test_ops_handle_odd_sizes_and_extremes():
    odd = testing.make_scene(97, 61, seed=2)
    for n in ("vignette", "grain", "halftone", "pixel_art", "mosaic", "low_poly", "watercolor", "cel_toon", "warhol", "glitch", "vhs", "gameboy"):
        out = run(n, odd)
        assert out.shape == odd.shape and np.isfinite(out).all(), n
    for fill in (0.0, 1.0):
        flat = np.full((64, 96, 3), fill, np.float32)
        for n in ("film_emulation", "adams_zone", "thermal", "cyanotype", "xray", "duotone", "posterize", "lik_hdr", "bleach_bypass"):
            out = run(n, flat)
            assert np.isfinite(out).all() and out.min() >= -1e-5 and out.max() <= 1 + 1e-5, (n, fill)


def test_px_params_scale_with_resolution():
    big = testing.make_scene(960, 640, seed=2)
    small = np.ascontiguousarray(big[::2, ::2])
    a = run("halftone", big, pitch=14, mode="bw")
    b = run("halftone", small, pitch=14, mode="bw")
    # a 2x smaller image must have dots 2x smaller: the dot count per image stays similar
    ga = (a.mean(axis=2) > 0.5).mean()
    gb = (b.mean(axis=2) > 0.5).mean()
    assert abs(ga - gb) < 0.12


# --------------------------------------------------------------------------- palette ops
def test_gameboy_palette(disp):
    out = run("gameboy", disp)
    pal = REGISTRY["gameboy"].defaults["palette"]
    assert frac_in_palette(out, pal, 0.02) > 0.99
    assert len({tuple(np.round(p * 255).astype(int)) for p in out.reshape(-1, 3)[::50]}) <= 4


def test_fairey_hope_palette(disp):
    out = run("fairey_hope", disp, smooth=0.0)
    assert frac_in_palette(out, REGISTRY["fairey_hope"].defaults["palette"], 0.04) > 0.97


def test_pixel_art_colour_count(disp):
    out = run("pixel_art", disp, colors=12, saturation=1.0, outline=0.0)
    u = np.unique(np.round(out.reshape(-1, 3) * 255).astype(np.uint8), axis=0)
    assert len(u) <= 12 + 2
    # blocky: neighbouring pixels inside a cell are identical most of the time
    same = (np.abs(out[:, 1:] - out[:, :-1]).max(axis=-1) < 1e-6).mean()
    assert same > 0.6


def test_mondrian_palette(disp):
    out = run("mondrian", disp)
    assert frac_in_palette(out, REGISTRY["mondrian"].defaults["palette"], 0.05) > 0.95


def test_warhol_palette_without_texture(disp):
    for grid, n_panels in (("1x1", 1), ("2x2", 4)):
        out = run("warhol", disp, texture=0.0, grid=grid, smooth=20.0)
        mod = importlib.import_module("photostudio.pipeline.ops.art_pop")
        allcols = {c for pal in mod.WARHOL_PALETTES.values() for c in pal} | {"#000000", "#111111", "#1a1a1a", "#0a0a0a"}
        assert out.shape == disp.shape
        flat = (np.abs(out[:, 1:] - out[:, :-1]).max(axis=-1) < 0.02).mean()
        assert flat > 0.8, f"warhol {grid} should be flat colour"
    pal_names = list(mod.WARHOL_PALETTES)
    assert len(pal_names) >= 4
    # four-panel layout: each quadrant is a different palette -> different mean colour
    out = run("warhol", disp, texture=0.0, grid="2x2")
    h, w = out.shape[:2]
    quads = [out[:h // 2, :w // 2], out[:h // 2, w // 2:], out[h // 2:, :w // 2], out[h // 2:, w // 2:]]
    means = [q.reshape(-1, 3).mean(axis=0) for q in quads]
    dists = [np.abs(means[i] - means[j]).max() for i in range(4) for j in range(i + 1, 4)]
    assert min(dists) > 0.02


def test_kmeans_quantize_with_palette(disp):
    pal = ["#1B1464", "#FF2D55", "#FFD60A", "#FFFFFF"]
    out = run("kmeans_quantize", disp, palette=pal, smooth=0.0)
    assert frac_in_palette(out, pal, 0.03) > 0.97


def test_kmeans_quantize_k(disp):
    out = run("kmeans_quantize", disp, k=5)
    u = np.unique(np.round(out.reshape(-1, 3) * 255).astype(np.uint8), axis=0)
    assert len(u) <= 5 + 3


def test_posterize_with_palette(disp):
    pal = ["#000000", "#7A1F3D", "#E9A23B", "#FFF5DC"]
    out = run("posterize", disp, palette=pal, smooth=0.0)
    assert frac_in_palette(out, pal, 0.03) > 0.97


def test_threshold_is_binary(disp):
    out = run("threshold", disp)
    u = np.unique(np.round(out.reshape(-1, 3) * 255).astype(np.uint8), axis=0)
    assert len(u) <= 4


def test_wes_anderson_exact_palette():
    mod = importlib.import_module("photostudio.pipeline.ops.art_film")
    assert mod.WES["GrandBudapest2"] == ["#E6A0C4", "#C6CDF7", "#D8A499", "#7294D4"]
    assert len(mod.WES) >= 20 and "Zissou1" in mod.WES and "Darjeeling1" in mod.WES
    assert mod.WES["Darjeeling1"] == ["#FF0000", "#00A08A", "#F2AD00", "#F98400", "#5BBCD6"]
    img = testing.make_scene(240, 160, seed=1)
    out = run("wes_anderson", img, palette="GrandBudapest2", blend=1.0, flat=1.0, grain=0.0, saturation=1.0, gain=0.0, black=0.0,
              soften_edges=0.0, warm=0.0)
    assert frac_in_palette(out, mod.WES["GrandBudapest2"], 0.06) > 0.9
    # blend < 1 keeps the original in the image
    mixed = run("wes_anderson", img, palette="GrandBudapest2", blend=0.45)
    assert float(np.abs(mixed - img).mean()) < float(np.abs(out - img).mean())


def test_wes_palettes_differ(disp):
    a = run("wes_anderson", disp, palette="GrandBudapest2")
    b = run("wes_anderson", disp, palette="Darjeeling1")
    assert float(np.abs(a - b).mean()) > 0.01


# --------------------------------------------------------------------------- behaviour of individual looks
def test_bw_looks_are_monochrome(disp):
    for n in ("adams_zone", "salgado", "fan_ho", "maier", "sugimoto_sea"):
        out = run(n, disp)
        sat = (out.max(axis=-1) - out.min(axis=-1)).mean()
        assert sat < 0.09, (n, sat)
    for stock in ("tri_x_400", "hp5_plus"):
        out = run("film_emulation", disp, stock=stock)
        assert (out.max(axis=-1) - out.min(axis=-1)).mean() < 0.09, stock


def test_film_stocks_distinct(disp):
    mod = importlib.import_module("photostudio.pipeline.ops.art_film")
    outs = {s: run("film_emulation", disp, stock=s, grain=0.0) for s in mod.STOCKS}
    assert len(outs) >= 17
    names = list(outs)
    close = [(a, b) for i, a in enumerate(names) for b in names[i + 1:] if float(np.abs(outs[a] - outs[b]).mean()) < 0.004]
    assert not close, f"indistinguishable film stocks: {close}"


def test_film_strength_zero_is_identity(disp):
    out = run("film_emulation", disp, stock="portra_400", strength=0.0, grain=0.0)
    assert float(np.abs(out - disp).mean()) < 0.01


def test_letterbox_and_border_keep_size(disp):
    for n, kw in (("letterbox", {"aspect": 2.39}), ("border", {"style": "polaroid"}), ("border", {"style": "rough"})):
        out = run(n, disp, **kw)
        assert out.shape == disp.shape
    lb = run("letterbox", disp, aspect=2.39, color="#000000")
    assert lb[:5].max() < 0.01 and lb[-5:].max() < 0.01


def test_vignette_darkens_corners(disp):
    out = run("vignette", disp, amount=-0.6)
    c = np.concatenate([out[:20, :20].ravel(), out[-20:, -20:].ravel()]).mean()
    c0 = np.concatenate([disp[:20, :20].ravel(), disp[-20:, -20:].ravel()]).mean()
    assert c < c0 * 0.85


def test_grain_zero_mean_ish(disp):
    out = run("grain", disp, amount=0.05)
    assert abs(float(out.mean() - disp.mean())) < 0.02
    assert float(np.abs(out - disp).mean()) > 0.003


def test_halation_adds_energy_near_highlights(lin):
    out = run("halation", lin, strength=1.0)
    assert (out >= lin - 1e-5).all() or float(out.sum()) > float(lin.sum())
    assert float(out[..., 0].sum()) > float(lin[..., 0].sum())


def test_halftone_cmyk_runs_and_is_dotted(disp):
    out = run("halftone", disp, mode="cmyk", pitch=10)
    assert float(out.std(axis=(0, 1)).mean()) > 0.05


def test_low_poly_and_mosaic_are_piecewise_flat(disp):
    for n in ("low_poly", "mosaic"):
        out = run(n, disp)
        flat = (np.abs(out[:, 1:] - out[:, :-1]).max(axis=-1) < 0.04).mean()
        assert flat > 0.45, (n, flat)   # per-tile light variation and grout lines keep mosaic below 'perfectly flat'


def test_xdog_mostly_black_and_white(disp):
    out = run("ink_xdog", disp, style="ink")
    g = out.mean(axis=-1)
    assert ((g < 0.2) | (g > 0.8)).mean() > 0.7


def test_negative_inverts(disp):
    out = run("xray", disp, mode="negative")
    assert float(np.abs(out - (1 - disp)).mean()) < 0.06


def test_thermal_ramps(disp):
    for ramp in ("ironbow", "white_hot", "rainbow"):
        out = run("thermal", disp, ramp=ramp)
        assert float(out.std()) > 0.05


# --------------------------------------------------------------------------- speed
@pytest.fixture(scope="module")
def big():
    return testing.make_scene(1600, 1067, seed=4)


@pytest.mark.parametrize("name", ART_OPS)
def test_op_speed_at_1600(name, big):
    spec = REGISTRY[name]
    img = color.apply_matrix(color.srgb_eotf(big), color.SRGB_TO_REC2020).astype(np.float32) if spec.domain == "linear" else big
    p = spec.resolve_params({})
    ctx = Ctx(domain="display" if spec.domain == "display" else "linear", kind="image")
    t0 = time.perf_counter()
    out = spec.fn(img.copy(), p, ctx)
    dt = time.perf_counter() - t0
    assert out.shape == img.shape and np.isfinite(out).all()
    limit = 12.0 if name in SLOW_OPS else 3.5
    assert dt < limit, f"{name} took {dt:.2f}s at 1600 px (limit {limit}s)"


# --------------------------------------------------------------------------- presets
PRESET_FILES = sorted(ART_DIR.glob("*.yaml"))
PRESET_NAMES = [p.stem for p in PRESET_FILES]


def test_preset_count_and_names():
    assert len(PRESET_FILES) >= 55
    for must in ("warhol_silkscreen", "lichtenstein_comic", "van_gogh_swirl", "monet_impressionist", "cyanotype", "wes_anderson_pastel",
                 "tri_x_400", "portra_400", "kodachrome_64", "blade_runner_haze", "adams_zone_bw", "daguerreotype", "risograph",
                 "pixel_art", "glitch", "vhs_tape", "gameboy_camera", "teal_orange", "bleach_bypass", "day_for_night"):
        assert must in PRESET_NAMES, must


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_preset_schema(name):
    d = yaml.safe_load((ART_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    assert d["name"] == name and d["kind"] == "art"
    assert isinstance(d["title"], str) and d["title"].strip()
    assert isinstance(d["description"], str) and len(d["description"]) > 30
    assert isinstance(d["tags"], list) and d["tags"] and set(d["tags"]) <= ALLOWED_TAGS, d["tags"]
    assert isinstance(d["stack"], list) and d["stack"]
    for e in d["stack"]:
        assert e["op"] in REGISTRY, e
    assert presets.validate_preset(d) == []
    loaded = presets.load_preset("art", name)
    assert loaded["stack"], name
    for vn in (d.get("variants") or {}):
        assert presets.load_preset("art", f"{name}:{vn}")["stack"]


def test_preset_listing_and_tags():
    items = presets.list_presets("art")
    assert len(items) >= 55 and not [i for i in items if i.get("error")]
    tags = {t for i in items for t in i["tags"]}
    for t in ("pop", "painterly", "photographer", "cinema", "film", "process", "retro"):
        assert t in tags, t


def test_no_trademarked_titles_for_living_artists():
    titles = " ".join(yaml.safe_load(p.read_text(encoding="utf-8"))["title"] for p in PRESET_FILES).lower()
    for bad in ("banksy", "fairey", "mccurry", "haring", "eggleston", "crewdson", "leiter", "moriyama", "salgado", "sugimoto"):
        assert bad not in titles, bad


@pytest.fixture(scope="module")
def sheet():
    return _load_sheet_module()


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_preset_renders(name, sheet, disp):
    d = yaml.safe_load((ART_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    out, backend = sheet.render_stack(disp, d["stack"], seed=0)
    assert backend in ("engine", "fallback")
    assert out.shape == disp.shape and out.dtype == np.float32
    assert np.isfinite(out).all() and out.min() >= 0.0 and out.max() <= 1.0
    assert float(np.abs(out - disp).mean()) > 0.002, f"preset {name} is indistinguishable from the input"
    out2, _ = sheet.render_stack(disp, d["stack"], seed=0)
    assert np.allclose(out, out2, atol=1e-5), f"preset {name} is not deterministic"


def test_fallback_runner_agrees_with_engine_stage_order(sheet, disp):
    d = yaml.safe_load((ART_DIR / "cinestill_800t.yaml").read_text(encoding="utf-8"))
    fb = sheet.run_stack_fallback(disp, d["stack"])
    assert fb.shape == disp.shape and np.isfinite(fb).all() and 0 <= fb.min() and fb.max() <= 1
    eng, backend = sheet.render_stack(disp, d["stack"])
    if backend == "engine":
        assert float(np.abs(fb - eng).mean()) < 0.05


def test_variants_render(sheet, disp):
    rendered = 0
    for label, d in sheet.collect_presets(include_variants=True):
        if ":" not in label:
            continue
        out, _ = sheet.render_stack(disp, d["stack"])
        assert out.shape == disp.shape and np.isfinite(out).all() and out.min() >= 0 and out.max() <= 1, label
        rendered += 1
    assert rendered >= 60


def test_slot_a_ops_run_before_tone_map(sheet, disp):
    d = yaml.safe_load((ART_DIR / "tintype_wet_plate.yaml").read_text(encoding="utf-8"))
    ops = [e["op"] for e in d["stack"]]
    assert ops[0] == "spectral_mix" and REGISTRY["spectral_mix"].stage == 15
    out, _ = sheet.render_stack(disp, d["stack"])
    sat = (out.max(axis=-1) - out.min(axis=-1)).mean()
    assert sat < 0.12


def test_engine_resolves_art_preset_by_name(disp):
    from photostudio.pipeline import engine
    stack = engine.resolve_stack(art="cyanotype", kind="image")
    assert any(e["op"] == "cyanotype" for e in stack)
    stack = engine.resolve_stack(art="wes_anderson_pastel:zissou", kind="image")
    wes = [e for e in stack if e["op"] == "wes_anderson"]
    assert wes and wes[0]["params"]["palette"] == "Zissou1"
