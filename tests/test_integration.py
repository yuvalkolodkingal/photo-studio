"""Regression tests for the integration fixes (docs validation round): JPEG writing, import indexing, mask
shorthands, global flags after nested sub-commands, --loudnorm parsing, --set validation, selectors, packaging,
shot numbering, cull flag source, LUT shoulder, perspective crop guard, scan exclusions, album ids, mask cache."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFile

from photostudio import testing
from photostudio.cli import main

pytestmark = pytest.mark.filterwarnings("ignore")


def run(*argv, expect=0):
    """Run the CLI in-process with --json and return the parsed JSON (stdout of the final JSON document)."""
    import contextlib
    import io

    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = main([str(a) for a in argv])
    out = buf.getvalue().strip()
    assert code == expect, f"exit {code} != {expect}: {out} {err.getvalue()}"
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


# =============================================================================== 1. JPEG write robustness
def test_jpeg_save_survives_very_detailed_image(tmp_path):
    from photostudio import export

    rng = np.random.default_rng(0)
    img = rng.random((800, 1200, 3)).astype(np.float32)             # white noise: worst case for the entropy coder
    # plain Pillow fails on this image with the default MAXBLOCK (that is the bug being guarded)
    import io

    with pytest.raises(OSError):
        Image.fromarray((img * 255).astype(np.uint8)).save(io.BytesIO(), "JPEG", quality=100, subsampling=0,
                                                         progressive=True, optimize=True)
    before = ImageFile.MAXBLOCK
    out = export.save_array(img, tmp_path / "noise.jpg", quality=100)          # defaults: progressive + optimise
    assert ImageFile.MAXBLOCK == before                                       # global restored
    with Image.open(out) as im:
        assert im.size == (1200, 800)
        im.load()


def test_export_preset_web_on_detailed_image(tmp_path):
    from photostudio import export

    rng = np.random.default_rng(1)
    arr = (rng.random((900, 1300, 3)) * 255).astype(np.uint8)
    src = tmp_path / "noise.png"
    Image.fromarray(arr).save(src)
    r = run("--json", "export", src, "--preset", "web", "--out", tmp_path / "o", "--quality", 100,
            "--long-edge", 1300)
    assert r["ok"] == 1, r
    assert export is not None


# =============================================================================== LUT shoulder
def test_bake_lut_preserves_white_by_default_and_shoulder_is_optional():
    from photostudio.pipeline import engine

    ident = engine.bake_lut([], size=9)
    assert np.allclose(ident[8, 8, 8], 1.0, atol=1e-4)                  # white stays 1.0
    assert np.allclose(ident[0, 0, 0], 0.0, atol=1e-4)
    g = np.linspace(0, 1, 9)
    assert np.abs(ident[4, 4, 4] - g[4]).max() < 0.01                   # identity LUT
    shoulder = engine.bake_lut([], size=9, shoulder=True)
    assert 0.97 < shoulder[8, 8, 8, 0] < 0.995                          # the old behaviour, still available


def test_video_engine_backend_keeps_white():
    from photostudio.video import bake_lut

    lut = bake_lut([], 17, backend="engine")
    assert lut.meta["backend"] == "engine"
    assert np.allclose(lut.table[-1, -1, -1], 1.0, atol=1e-4)


# =============================================================================== AI mask cache API
def test_process_level_mask_cache(tmp_path, scene):
    from photostudio import masks
    from photostudio.masks import ai
    from photostudio.pipeline.registry import Ctx

    f = testing.write_jpeg(tmp_path / "m.jpg", scene)
    key = ("ai", "sky", (10, 10))
    val = np.ones((10, 10), np.float32)
    masks.disable_cache()
    try:
        c1 = Ctx(kind="jpeg", path=str(f), domain="display")
        ai._cache_put(c1, key, val)
        assert ai._cache_get(Ctx(kind="jpeg", path=str(f), domain="display"), key) is None   # off by default
        lru = masks.enable_cache(max_mb=1)
        assert masks.cache_enabled()
        ai._cache_put(c1, key, val)
        c2 = Ctx(kind="jpeg", path=str(f), domain="display")                                 # fresh render context
        assert ai._cache_get(c2, key) is not None and lru.stats()["hits"] == 1
        assert key in c2.cache                                                               # promoted to the ctx cache
        import os

        st = os.stat(f)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))                     # file changed -> new identity
        assert ai._cache_get(Ctx(kind="jpeg", path=str(f), domain="display"), key) is None
        assert ai._cache_get(Ctx(kind="jpeg", path="", domain="display"), key) is None       # no file -> never shared
        lru.max_bytes = val.nbytes * 2                                                       # bounded LRU
        for i in range(5):
            lru.put((str(f), i, "display", key), np.ones((10, 10), np.float32))
        assert lru.stats()["bytes"] <= val.nbytes * 2
        masks.clear_cache()
        assert lru.stats()["entries"] == 0
        # custom callables
        store: dict = {}
        masks.set_cache(store.get, store.__setitem__)
        ai._cache_put(c1, key, val)
        assert len(store) == 1
    finally:
        masks.disable_cache()
    assert not masks.cache_enabled()


def test_web_server_uses_mask_cache_api_not_monkeypatching(lib):
    from photostudio import masks
    from photostudio.masks import ai
    from photostudio.web import render as wr

    orig = (ai._cache_get, ai._cache_put)
    masks.disable_cache()
    try:
        assert wr.install_ai_mask_cache(8) is True
        assert masks.cache_enabled()
        assert (ai._cache_get, ai._cache_put) == orig                       # nothing was patched
    finally:
        masks.disable_cache()


# =============================================================================== perspective crop guard
def test_perspective_never_returns_a_sliver():
    from photostudio.pipeline import registry
    from photostudio.pipeline.registry import Ctx

    registry._ensure_loaded()
    img = testing.make_scene(300, 200, seed=1).astype(np.float32)
    spec = registry.REGISTRY["perspective"]
    for params in ({"vertical": 100, "horizontal": 100, "rotate": 45}, {"vertical": 100}, {"auto": "vertical"}):
        ctx = Ctx(kind="jpeg", domain="display")
        out = spec.fn(img, spec.resolve_params(params), ctx)
        assert min(out.shape[:2]) >= 16, (params, out.shape)
    ctx = Ctx(kind="jpeg", domain="display")
    out = spec.fn(img, spec.resolve_params({"vertical": 100, "horizontal": 100, "rotate": 45}), ctx)
    assert out.shape == img.shape                                  # fell back to no crop
    assert any("uncropped" in n or "left uncropped" in n for n in ctx.log)


def test_perspective_auto_vertical_empty_crop_guard(monkeypatch):
    from photostudio.pipeline import registry
    from photostudio.pipeline.ops import geometry as geo
    from photostudio.pipeline.registry import Ctx

    registry._ensure_loaded()
    img = testing.make_scene(320, 240, seed=2).astype(np.float32)
    monkeypatch.setattr(geo, "estimate_vertical_rotation", lambda *a, **k: (np.eye(3), 7))
    monkeypatch.setattr(geo, "max_rect_in_polygon", lambda *a, **k: (10.0, 10.0, 12.0, 11.0))   # degenerate result
    spec = registry.REGISTRY["perspective"]
    ctx = Ctx(kind="jpeg", domain="display")
    out = spec.fn(img, spec.resolve_params({"auto": "vertical", "vertical": 5}), ctx)
    assert min(out.shape[:2]) >= 16
    assert any("left uncropped" in n for n in ctx.log)


# =============================================================================== shared fixtures
@pytest.fixture()
def card_and_lib(tmp_path, scene):
    """A 'card' with three JPEGs taken on two days, and an empty library directory."""
    card = tmp_path / "card"
    card.mkdir()
    days = ["2026:11:12 10:00:00", "2026:11:12 10:05:00", "2026:11:13 09:00:00"]
    for i, d in enumerate(days):
        img = np.ascontiguousarray(np.roll(scene, 40 * i, axis=1))
        testing.write_jpeg(card / f"IMG_{i}.jpg", img, exif={"Make": "Canon", "Model": "Canon EOS R50",
                                                                "DateTimeOriginal": d})
    return card, tmp_path / "library"


@pytest.fixture()
def library(card_and_lib):
    """Imported library: 3 images; id 1 rated 5 + tagged + in an album; @last-import is all three."""
    card, lib = card_and_lib
    run("--json", "import", card, "--dest", lib)
    run("--json", "--root", lib, "rate", 5, "id:1")
    run("--json", "--root", lib, "tag", "add", "keeper", "id:1")
    run("--json", "--root", lib, "album", "create", "Best", "id:1")
    return lib


def edited(lib: Path) -> set[str]:
    """Names of the images whose sidecar holds a non-empty edit stack."""
    out = set()
    for p in lib.rglob("*.ps.json"):
        doc = json.loads(p.read_text())
        if any(v.get("stack") for v in doc.get("versions", {}).values()):
            out.add(p.name.removesuffix(".ps.json"))
    return out


# =============================================================================== 2. import --dest indexes the destination
def test_import_relative_dest_indexes_destination(card_and_lib, tmp_path, monkeypatch):
    card, _ = card_and_lib
    monkeypatch.chdir(tmp_path)
    other_root = tmp_path / "somewhere_else"
    other_root.mkdir()
    rep = run("--json", "--root", other_root, "import", card, "--dest", "other")      # relative --dest, different --root
    assert rep["catalog_total"] == 3, rep
    assert not (other_root / ".photostudio").exists()                                  # the --root library is untouched
    import sqlite3

    n = sqlite3.connect(tmp_path / "other" / ".photostudio" / "catalog.sqlite").execute("SELECT COUNT(*) FROM images").fetchone()[0]
    assert n == 3                                                                      # indexed by the import itself
    assert len(run("--json", "--root", tmp_path / "other", "ls")) == 3
    assert len(run("--json", "--root", tmp_path / "other", "ls", "@last-import")) == 3
    assert len(run("--json", "--root", tmp_path / "other", "ls", "--last-import")) == 3


def test_ls_positional_selector_and_id_query(library):
    assert [r["id"] for r in run("--json", "--root", library, "ls", "id:1")] == [1]
    assert [r["id"] for r in run("--json", "--root", library, "ls", "--query", "id:2")] == [2]
    assert {r["id"] for r in run("--json", "--root", library, "ls", "--query", "id>1")} == {2, 3}
    assert len(run("--json", "--root", library, "ls", "@album:Best")) == 1
    assert run("--json", "--root", library, "ls", "--count", "--query", "id<=2") == {"count": 2}


# =============================================================================== scan ignores exports/, _rejects/, hidden
def test_scan_ignores_exports_rejects_and_hidden_dirs(tmp_path, scene):
    from photostudio.catalog import Catalog

    root = tmp_path / "lib"
    for rel in ("keep.jpg", "sub/keep2.jpg", "exports/out.jpg", "sub/exports/out2.jpg", "_rejects/bad.jpg",
                "2026/_rejects/bad2.jpg", ".photostudio/thumbs/t.jpg", "sub/.photostudio/x.jpg", "Exports/up.jpg"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        testing.write_jpeg(root / rel, scene)
    with Catalog(root) as cat:
        cat.scan()
        names = sorted(r["primary_path"] for r in cat.images())
        assert names == ["keep.jpg", "sub/keep2.jpg"], names
        # an export run next to the images never feeds back into the catalog
        run("--json", "export", root / "keep.jpg", "--preset", "web", "--out", root / "exports")
        assert (root / "exports").exists()
        cat.scan()
        assert cat.count() == 2
        # files indexed earlier under exports/ drop out on the next scan
        cat.conn.execute("INSERT INTO images(uid, primary_path, dir, stem, kind) VALUES('zz','exports/old.jpg','exports','old','jpeg')")
        cat.scan()
        assert cat.count() == 2


# =============================================================================== albums: digit names vs ids
def test_album_resolve_digit_names_and_explicit_ids(tmp_path, scene):
    from photostudio.catalog import Catalog

    root = tmp_path / "lib"
    root.mkdir()
    testing.write_jpeg(root / "a.jpg", scene)
    with Catalog(root) as cat:
        cat.scan()
        a = cat.albums.create("2")                       # named "2" but id 1
        b = cat.albums.create("Other")                   # id 2
        assert (a["id"], b["id"]) == (1, 2)
        assert cat.albums.resolve("2") == a["id"]        # a bare digit string is a NAME
        assert cat.albums.resolve("id:2") == b["id"]     # id:N is explicit
        assert cat.albums.resolve(2) == b["id"]          # python ints stay ids
        assert cat.albums.resolve("id:99") is None
        assert cat.albums.resolve("99") is None
        with pytest.raises(ValueError):
            cat.albums.resolve("id:abc")
        cat.albums.add("id:1", ["a.jpg"])
        assert len(cat.albums.images("2")) == 1 and len(cat.albums.images("id:2")) == 0
        assert cat.ids("album:2") == [1] and cat.ids("album:id:2") == []


def test_web_album_filter_uses_the_same_rules(lib):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from photostudio.web.server import create_app

    with TestClient(create_app(lib)) as c:
        a = c.post("/api/albums", json={"name": "2024"}).json()           # digit-only name
        b = c.post("/api/albums", json={"name": "Other"}).json()
        c.post(f"/api/albums/{a['id']}/images", json={"paths": ["a.jpg"]})
        by_name = c.get("/api/library/images", params={"album": "2024"}).json()
        by_id = c.get("/api/library/images", params={"album": f"id:{a['id']}"}).json()
        other = c.get("/api/library/images", params={"album": f"id:{b['id']}"}).json()
        assert [i["p"] for i in by_name["items"]] == ["a.jpg"] == [i["p"] for i in by_id["items"]]
        assert other["items"] == []


# =============================================================================== 3. --mask shorthands
SHORTHANDS = ["sky", "subject", "background", "face", "skin", "eyes", "lips", "teeth", "animal"]


@pytest.mark.parametrize("name", SHORTHANDS)
def test_mask_shorthand_parses_everywhere(name, jpeg, tmp_path):
    from photostudio import sidecar
    from photostudio.cli import load_mask_arg

    assert load_mask_arg(name) == {"type": f"ai_{name}"}
    run("--json", "edit", jpeg, "--set", "exposure.ev=0.4", "--mask", name)
    stack = sidecar.get_stack(sidecar.load(jpeg))
    masked = [e for e in stack if e.get("mask")]
    assert masked and masked[-1]["mask"]["type"] == f"ai_{name}"
    run("--json", "develop", jpeg, "--no-render", "--set", "exposure.ev=0.2", "--mask", name, "--version", "v2")
    v2 = sidecar.get_stack(sidecar.load(jpeg), "v2")
    assert any(e.get("mask", {}) and e["mask"]["type"] == f"ai_{name}" for e in v2)


def test_mask_file_json_and_errors(jpeg, tmp_path):
    from photostudio import sidecar
    from photostudio.cli import load_mask_arg

    f = tmp_path / "m.json"
    f.write_text(json.dumps({"type": "radial", "cx": 0.3}))
    assert load_mask_arg(f"@{f}")["type"] == "radial"
    run("--json", "edit", jpeg, "--set", "exposure.ev=0.4", "--mask", f"@{f}")
    assert sidecar.get_stack(sidecar.load(jpeg))[-1]["mask"]["cx"] == 0.3
    assert load_mask_arg('{"type":"sky"}') == {"type": "ai_sky"}
    for bad in ("skyy", '{"type":"nonsense"}', "{bad json", '{"feather":3}', f"@{tmp_path}/missing.json"):
        with pytest.raises(ValueError if "missing" not in bad else FileNotFoundError):
            load_mask_arg(bad)
    err = run("--json", "edit", jpeg, "--set", "exposure.ev=1", "--mask", "skyy", expect=1)
    assert "ai_sky" in err["error"] or "sky" in err["error"]


def test_mask_preview_accepts_same_shorthand(jpeg, tmp_path):
    r = run("--json", "mask", "preview", jpeg, "--spec", "sky", "--out", tmp_path / "m.png", "--size", 300)
    assert Path(r["out"]).exists()
    r2 = run("--json", "mask", "preview", jpeg, "--spec", '{"type":"ai_sky"}', "--out", tmp_path / "m2.png", "--size", 300)
    assert r["coverage"] == r2["coverage"]


# =============================================================================== 4. global flags after nested sub-commands
def test_every_nested_subcommand_accepts_global_flags():
    import argparse

    from photostudio.cli import build_parser

    parser, warnings = build_parser()
    assert not warnings
    leaves: list[tuple[str, argparse.ArgumentParser]] = []

    def walk(p, path):
        subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs and path:
            leaves.append((" ".join(path), p))
        for a in subs:
            for name, child in a.choices.items():
                walk(child, path + [name])

    walk(parser, [])
    assert len(leaves) > 60
    for name, p in leaves:
        have = set(p._option_string_actions)
        assert {"--json", "--quiet", "--root", "--debug"} <= have, f"{name} lacks global flags: {sorted(have)}"


def test_global_flags_after_nested_commands(library):
    assert "mask_types" in run("mask", "types", "--json")
    assert isinstance(run("models", "list", "--json", "--quiet"), list)
    assert isinstance(run("tag", "list", "--json", "--root", library), (list, dict))
    assert run("ops", "describe", "exposure", "--json")["op"] == "exposure"
    assert isinstance(run("presets", "list", "art", "--json"), (list, dict))
    assert run("album", "list", "--root", library, "--json")
    assert run("versions", "list", library / "2026" / "2026-11-12" / "IMG_0.jpg", "--json", "--quiet")["active"] == "default"
    assert run("lut", "bake", "--art", "portra_400", "--out", library / "x.cube", "--size", 17, "--json")["size"] == 17


# =============================================================================== 5. --loudnorm
def test_loudnorm_negative_comma_list_parses():
    from photostudio.cli import build_parser, fix_argv
    from photostudio.commands.video_cmd import _parse_loudnorm

    argv = ["video", "export", "in.mp4", "--audio", "loudnorm", "--loudnorm", "-14,-1,11", "-o", "o.mp4"]
    assert "--loudnorm=-14,-1,11" in fix_argv(argv)
    parser, _ = build_parser()
    args = parser.parse_args(fix_argv(argv))
    assert args.loudnorm == "-14,-1,11"
    assert _parse_loudnorm(args.loudnorm) == {"I": -14.0, "TP": -1.0, "LRA": 11.0}
    assert parser.parse_args(["video", "export", "in.mp4", "--loudnorm=-16,-1.5"]).loudnorm == "-16,-1.5"
    assert _parse_loudnorm("-23") == {"I": -23.0}
    with pytest.raises(ValueError):
        _parse_loudnorm("loud")
    with pytest.raises(ValueError):
        _parse_loudnorm("-14,-1,11,5")
    # values that are not a comma list are untouched
    assert fix_argv(["x", "--set", "a=1", "-5", "-14"]) == ["x", "--set", "a=1", "-5", "-14"]


# =============================================================================== 6. --set validation and robust render
def test_set_validation_errors_suggest_and_write_nothing(jpeg):
    from photostudio import sidecar

    cases = {
        "expsure.ev=1": "exposure",                 # op typo
        "exposure.evv=1": "ev",                     # param typo
        "exposure.ev=abc": "number",                # wrong kind
        "tone_map.curve=sigmoyd": "sigmoid",        # choices
        "heal.spots=[{\"center\":[0.1,0.2]}]": "dst",
        "heal.spots=5": "list",
        "exposure=1": "op.param",
    }
    for assignment, needle in cases.items():
        for cmd in ("edit", "develop"):
            extra = ["--no-render"] if cmd == "develop" else []
            err = run("--json", cmd, jpeg, "--set", assignment, *extra, expect=1)
            assert needle in err["error"], (assignment, err)
            assert not sidecar.exists(jpeg), f"{cmd} {assignment} wrote a sidecar"
    err = run("--json", "edit", jpeg, "--set", "expsure.ev=1", expect=1)
    assert "Did you mean 'exposure'" in err["error"]
    err = run("--json", "edit", jpeg, "--set", "exposure.evv=1", expect=1)
    assert "Did you mean 'ev'" in err["error"]
    err = run("--json", "edit", jpeg, "--set", "exposure.ev=1", "--disable", "vignete", expect=1)
    assert "vignette" in err["error"]
    assert not sidecar.exists(jpeg)


def test_valid_sets_still_work(jpeg):
    from photostudio import sidecar

    run("--json", "edit", jpeg, "--set", "exposure.ev=+0.3,tone_map.curve=filmic,vignette.enabled=false")
    run("--json", "edit", jpeg, "--set", "exposure@sky.ev=0.5", "--mask", '{"type":"ai_sky"}')
    run("--json", "edit", jpeg, "--set", 'heal.spots=[{"dst":[0.2,0.3],"src":[0.4,0.3],"radius":20}]')
    run("--json", "edit", jpeg, "--set", "crop.rect=[0.1,0.1,0.8,0.8]")
    run("--json", "edit", jpeg, "--set", "hsl_mixer.red_hue=5")
    run("--json", "edit", jpeg, "--set", "exposure.opacity=0.5,exposure.blend=multiply")
    run("--json", "edit", jpeg, "--set", "exposure.enabled=true")
    stack = sidecar.get_stack(sidecar.load(jpeg))
    assert {e["op"] for e in stack} >= {"exposure", "tone_map", "heal", "crop", "hsl_mixer"}
    r = run("--json", "edit", jpeg, "--remove", "vignette", "--disable", "sharpen")
    assert r[0]["changed"] is True


def test_every_documented_set_example_validates():
    """Every `--set` assignment shown in README / SKILL.md / references passes the write-time validator."""
    import re

    from photostudio.cli import flatten_sets
    from photostudio.pipeline.engine import parse_assignment
    from photostudio.pipeline.validate import validate_param

    root = Path(__file__).resolve().parents[1]
    texts = [root / "README.md", root / "SKILL.md", *sorted((root / "references").glob("*.md"))]
    checked = 0
    for f in texts:
        for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", f.read_text(encoding="utf-8"), re.S):
            for m in re.finditer(r"photo (?:edit|develop)\b[^\n]*?--set\s+(?:'([^']*)'|\"([^\"]*)\"|(\S+))", block):
                raw = next(g for g in m.groups() if g)
                for a in flatten_sets([raw]):
                    if "=" not in a or "." not in a.split("=", 1)[0]:
                        continue
                    op, eid, path, value = parse_assignment(a)
                    validate_param(op, path, value, eid)
                    checked += 1
    assert checked > 10


def test_engine_skips_unrenderable_entries_instead_of_crashing(jpeg):
    from photostudio import sidecar
    from photostudio.pipeline import engine

    doc = sidecar.load(jpeg)
    sidecar.set_stack(doc, [
        {"op": "exposure", "params": {"ev": "not-a-number"}},                       # op blows up at run time
        {"op": "tone_map", "params": {"curve": "bogus"}},                           # fails choices check
        {"op": "vibrance", "params": {"amount": 20}, "mask": {"type": "no_such_mask"}},   # bad mask spec
        {"op": "contrast", "params": {"contrast": 0.2}},                            # fine
        {"op": "not_an_op", "params": {}},
    ])
    sidecar.save(jpeg, doc)
    img, ctx = engine.render_file(jpeg, return_ctx=True)
    assert img.ndim == 3 and np.isfinite(img).all()
    joined = "\n".join(ctx.log)
    for name in ("exposure", "tone_map", "vibrance", "not_an_op"):
        assert f"'{name}'" in joined, (name, ctx.log)
    out = run("--json", "render", jpeg, "--preview", 200, "--out", jpeg.parent / "r.jpg")                  # CLI also works
    assert Path(out[0]["out"]).exists()


# =============================================================================== 7. one selector resolver for every command
def rel_names(lib: Path) -> list[str]:
    return sorted(Path(p).name for p in edited(lib))


@pytest.mark.parametrize("selector, expected", [
    (["id:1"], ["IMG_0.jpg"]),
    (["@all"], ["IMG_0.jpg", "IMG_1.jpg", "IMG_2.jpg"]),
    (["@album:Best"], ["IMG_0.jpg"]),
    (["@tag:keeper"], ["IMG_0.jpg"]),
    (["@last-import"], ["IMG_0.jpg", "IMG_1.jpg", "IMG_2.jpg"]),
    (["@session:2026-11-13"], ["IMG_2.jpg"]),
    (["--query", "rating>=5"], ["IMG_0.jpg"]),
    (["id:2", "id:3"], ["IMG_1.jpg", "IMG_2.jpg"]),
    (["@all", "--query", "id>=2"], ["IMG_1.jpg", "IMG_2.jpg"]),
])
def test_edit_accepts_library_selectors(library, selector, expected):
    r = run("--json", "--root", library, "edit", *selector, "--set", "exposure.ev=0.2")
    assert sorted(Path(x["file"]).name for x in r) == expected
    assert rel_names(library) == expected


def test_develop_render_export_reset_versions_use_selectors(library, tmp_path):
    out = tmp_path / "out"
    r = run("--json", "--root", library, "develop", "id:1", "--mode", "portrait", "--preview", 300, "--out", out)
    assert len(r) == 1 and Path(r[0]["out"]).exists()
    r = run("--json", "--root", library, "develop", "@session:2026-11-12", "--no-render", "--art", "portra_400")
    assert len(r) == 2
    r = run("--json", "--root", library, "render", "@album:Best", "--preview", 200, "--out", out)
    assert len(r) == 1
    ex = run("--json", "--root", library, "export", "@tag:keeper", "--preset", "web", "--out", out / "web")
    assert ex["ok"] == 1
    ex = run("--json", "--root", library, "export", "--query", "rating>=5", "--preset", "email_small", "--out", out / "email")
    assert ex["ok"] == 1
    v = run("--json", "--root", library, "versions", "list", "id:1")
    assert v["active"] == "default"
    run("--json", "--root", library, "versions", "new", "id:1", "alt", "--no-switch")
    assert {x["name"] for x in run("--json", "--root", library, "versions", "list", "id:1")["versions"]} >= {"default", "alt"}
    cs = run("--json", "--root", library, "contact-sheet", "@all", "--out", out / "sheet.jpg", "--cell", 120)
    assert cs["images"] == 3
    run("--json", "--root", library, "reset", "id:1")
    run("--json", "--root", library, "copy-settings", "id:2")
    run("--json", "--root", library, "paste-settings", "id:3")
    info = run("--json", "--root", library, "info", "id:1")
    assert len(info) == 1
    err = run("--json", "--root", library, "versions", "list", "@all", expect=1)        # one image only
    assert "exactly one" in err["error"]


def test_selector_errors_are_clear(library):
    err = run("--json", "--root", library, "edit", "id:99", "--set", "exposure.ev=1", expect=1)
    assert "99" in err["error"]
    err = run("--json", "--root", library, "edit", "@album:Nope", "--set", "exposure.ev=1", expect=1)
    assert "Nope" in err["error"]
    err = run("--json", "--root", library, "edit", "--set", "exposure.ev=1", expect=1)           # nothing selected at all
    assert "selector" in err["error"] or "no input" in err["error"]
    assert not edited(library)


def test_plain_paths_and_globs_still_work_without_a_catalog(lib):
    run("--json", "edit", lib / "a.jpg", "--set", "exposure.ev=0.1")
    r = run("--json", "--root", lib, "edit", "*.jpg", "--set", "exposure.ev=0.1")
    assert len(r) == 2
    assert not (lib / ".photostudio").exists()          # plain paths never create a catalog


def test_video_inputs_accept_selectors(library, tmp_path):
    r = run("--json", "--root", library, "video", "startrails", "@album:Best", "-o", tmp_path / "t.png", "--dry-run")
    assert any("1 frame" in s["label"] for s in r["steps"])
    r = run("--json", "--root", library, "video", "timelapse", "--query", "id>1", "-o", tmp_path / "t.mp4", "--dry-run")
    assert any("2 image" in s["label"] for s in r["steps"])
    r = run("--json", "--root", library, "video", "timelapse", "@all", "-o", tmp_path / "t.mp4", "--dry-run")
    assert any("3 image" in s["label"] for s in r["steps"])
    r = run("--json", "--root", library, "video", "lut", "-o", tmp_path / "x.cube", "--art", "portra_400", "--dry-run",
            "--from-image", "id:1")
    assert r["dry_run"] is True


# =============================================================================== 9. video shots: 1-based numbering
def test_shots_numbering_is_one_based_and_consistent():
    from photostudio.video import shots

    s = shots.cuts_to_shots([2.0, 5.0], 8.0, min_len=0.5)
    assert [x["index"] for x in s] == [1, 2, 3]
    f = shots.fill_shots([{"start": 1, "end": 3}, {"start": 4, "end": 6}], 8.0)
    assert [x["index"] for x in f] == [1, 2]
    assert [x["index"] for x in shots.expand_segments(f, 1.0)] == list(range(1, len(shots.expand_segments(f, 1.0)) + 1))
    assert shots.make_shot(0, 0, 2)["index"] == 1
    st = [{"median_y": 0.1, "mid_rgb": (0.1, 0.1, 0.1)}, {"median_y": 0.4, "mid_rgb": (0.4, 0.4, 0.4)}]
    m = shots.solve_matches(st, mode="exposure", reference="index:1", max_ev=3.0)
    assert m[0]["ev"] == 0.0 and m[1]["ev"] < 0                      # shot 1 is the reference, so shot 2 is darkened
    with pytest.raises(Exception, match="out of range"):
        shots.solve_matches(st, mode="exposure", reference="index:0")


def _tiny_clip(tmp_path: Path) -> Path:
    import shutil
    import subprocess

    ff = shutil.which("ffmpeg")
    if not ff or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not available")
    out = tmp_path / "clip.mp4"
    for enc in (["-c:v", "libx264", "-preset", "ultrafast"], ["-c:v", "mpeg4"]):
        r = subprocess.run([ff, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=96x64:r=25:d=3",
                            "-pix_fmt", "yuv420p", *enc, str(out)], capture_output=True)
        if r.returncode == 0 and out.exists():
            return out
    pytest.skip("no encoder for the test clip")


def test_video_shots_assign_uses_listed_numbers(tmp_path):
    from photostudio.video import shots

    clip = _tiny_clip(tmp_path)
    doc = shots.new_doc(str(clip), None, shots.cuts_to_shots([1.0, 2.0], 3.0, min_len=0.5))
    shots.save_shots(str(clip), doc)
    r = run("--json", "video", "shots", clip, "--assign", "1:art=portra_400", "--assign", "3:mode=night", "--save")
    assert [s["index"] for s in r["shots"]] == [1, 2, 3]                  # what is listed ...
    assert r["shots"][0]["art"] == "portra_400" and r["shots"][2]["mode"] == "night"     # ... is what --assign takes
    assert r["shots"][1]["art"] is None
    saved = shots.load_shots(str(clip))
    assert saved["shots"][0]["art"] == "portra_400" and saved["shots"][0]["index"] == 1
    for bad, needle in (("0:art=x", "no shot 0"), ("4:art=x", "no shot 4"), ("x:art=x", "N:mode=X")):
        err = run("--json", "video", "shots", clip, "--assign", bad, expect=1)
        assert needle in err["error"], err


# =============================================================================== 10. cull --apply keeps manual flags
def _results(cat, flags: dict[int, str]) -> list[dict]:
    return [{"id": i, "path": str(cat.root / cat.get(i)["primary_path"]), "name": f"{i}", "flag": f, "score": 50.0,
             "reasons": []} for i, f in flags.items()]


def test_cull_apply_never_overwrites_manual_flags(library):
    from photostudio import cull as C
    from photostudio.catalog import Catalog

    with Catalog(library) as cat:
        cat.set_flag(1, "pick")                                   # the user's own decision
        cat.set_flag(2, "reject")
        res = _results(cat, {1: "reject", 2: "pick", 3: "pick"})
        rep = C.apply_cull(cat, res)
        assert rep["kept_user_flags"] == 2 and rep["counts"] == {"pick": 1, "reject": 0, "maybe": 0}
        assert (cat.get(1)["flag"], cat.get(2)["flag"], cat.get(3)["flag"]) == ("pick", "reject", "pick")
        assert cat.flag_source(1) == "user" and cat.flag_source(3) == "cull"
        side = json.loads((library / cat.get(3)["primary_path"]).with_name(Path(cat.get(3)["primary_path"]).name + ".ps.json").read_text())
        assert side["flag"] == "pick" and side["flag_source"] == "cull"
        # a later cull may refresh what an earlier cull wrote ...
        C.apply_cull(cat, _results(cat, {3: "reject"}))
        assert cat.get(3)["flag"] == "reject" and cat.flag_source(3) == "cull"
        # ... but never moves or flags a file the user picked
        mv = C.apply_cull(cat, _results(cat, {1: "reject", 3: "reject"}), flags=False, move_rejects=True)
        assert len(mv["moved"]) >= 1 and (library / cat.get(1)["primary_path"]).exists()
        # --overwrite replaces manual flags (and records the new source)
        C.apply_cull(cat, _results(cat, {1: "reject"}), overwrite=True)
        assert cat.get(1)["flag"] == "reject" and cat.flag_source(1) == "cull"
        # manual action afterwards is "user" again, and unflagging clears the source
        cat.set_flag(1, "pick")
        assert cat.flag_source(1) == "user"
        cat.set_flag(1, None)
        assert cat.flag_source(1) is None


def test_cull_cli_apply_respects_manual_flags_and_overwrite(library):
    run("--json", "--root", library, "flag", "pick", "id:1")
    r = run("--json", "--root", library, "cull", "--apply", "--genre", "general")
    assert r["applied"]["kept_user_flags"] >= 1
    assert run("--json", "--root", library, "ls", "--query", "id:1")[0]["flag"] == "pick"
    r = run("--json", "--root", library, "cull", "--apply", "--overwrite", "--genre", "general")
    assert r["applied"]["kept_user_flags"] == 0


# =============================================================================== 8. packaging metadata
def test_pyproject_extras_and_package_data():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / "pyproject.toml").read_text())
    extras = cfg["project"]["optional-dependencies"]
    assert "serve" in extras["all"][0]
    assert "everything" in extras
    for e in ("all", "ai", "jxl", "denoise", "yolo", "faces"):
        assert e in extras["everything"][0]
    assert cfg["project"]["license"] == "MIT"
    pd = cfg["tool"]["setuptools"]["package-data"]["photostudio"]
    assert {"web/static/**", "presets/**/*.yaml", "data/**"} <= set(pd)
    pkg = root / "photostudio"
    assert (pkg / "web" / "static" / "index.html").is_file()
    assert list((pkg / "presets").glob("*/*.yaml")) and (pkg / "data" / "tag_vocab.yaml").is_file()
    lic = (root / "LICENSE").read_text()
    assert lic.startswith("MIT License") and "Copyright (c) 2026 Yuval Kolodkin Gal" in lic
