"""Sidecar (round trip, versions, history, copy/paste), export (formats, ICC, resize, naming, batch) and the CLI end to end."""
from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photostudio import export, icc, sidecar
from photostudio.cli import main
from photostudio.pipeline import engine
from photostudio.util import MissingDependency

pytestmark = pytest.mark.filterwarnings("ignore")


def E(op, **params):
    return {"op": op, "params": params}


# =============================================================================== sidecar
def test_sidecar_round_trip_and_atomic_write(jpeg):
    assert not sidecar.exists(jpeg)
    doc = sidecar.load(jpeg)
    assert doc["active"] == "default" and doc["versions"]["default"]["stack"] == []
    sidecar.set_stack(doc, [E("exposure", ev=0.5), {"op": "vignette", "params": {"amount": -0.2}, "enabled": False}],
                      mode="portrait", art=None)
    p = sidecar.save(jpeg, doc)
    assert p.name == "a.jpg.ps.json" and p.exists()
    again = sidecar.load(jpeg)
    assert again["versions"]["default"]["mode"] == "portrait"
    stack = sidecar.get_stack(again)
    assert stack[0]["op"] == "exposure" and stack[0]["params"]["ev"] == 0.5 and stack[1]["enabled"] is False
    for k in ("op", "params", "enabled", "mask", "blend", "opacity", "v"):
        assert k in stack[0]                                    # contract: fully normalised entries
    sidecar.save(jpeg, again)                                   # second save keeps a .bak and leaves no temp files
    leftovers = [f.name for f in jpeg.parent.iterdir() if f.name.startswith("a.jpg") and f.name not in
                 ("a.jpg", "a.jpg.ps.json", "a.jpg.ps.json.bak")]
    assert not leftovers
    json.loads(p.read_text())                                   # valid JSON on disk


def test_get_stack_returns_copy(jpeg):
    doc = sidecar.load(jpeg)
    sidecar.set_stack(doc, [E("exposure", ev=1.0)])
    s = sidecar.get_stack(doc)
    s[0]["params"]["ev"] = 99
    assert sidecar.get_stack(doc)[0]["params"]["ev"] == 1.0


def test_versions_are_virtual_copies(jpeg):
    doc = sidecar.load(jpeg)
    sidecar.set_stack(doc, [E("exposure", ev=0.5)])
    sidecar.new_version(doc, "bw", switch=True)
    assert doc["active"] == "bw"
    sidecar.set_stack(doc, [E("exposure", ev=-1.0)])
    assert sidecar.get_stack(doc, "default")[0]["params"]["ev"] == 0.5     # original untouched
    assert sidecar.get_stack(doc, "bw")[0]["params"]["ev"] == -1.0
    assert [v["name"] for v in sidecar.list_versions(doc)] == ["default", "bw"]
    with pytest.raises(sidecar.SidecarError):
        sidecar.new_version(doc, "bw")
    sidecar.rename_version(doc, "bw", "mono")
    assert doc["active"] == "mono"
    sidecar.switch_version(doc, "default")
    sidecar.delete_version(doc, "mono")
    assert list(doc["versions"]) == ["default"]
    with pytest.raises(sidecar.SidecarError):
        sidecar.delete_version(doc, "default")
    with pytest.raises(sidecar.SidecarError):
        sidecar.switch_version(doc, "nope")


def test_snapshots_history_and_restore(jpeg):
    doc = sidecar.load(jpeg)
    sidecar.set_stack(doc, [E("exposure", ev=0.5)])
    snap = sidecar.snapshot(doc, "before-bw")
    assert snap["time"] and snap["name"] == "before-bw"
    sidecar.set_stack(doc, [E("exposure", ev=2.0), E("contrast", contrast=0.2)])
    sidecar.restore_snapshot(doc, "before-bw")
    st = sidecar.get_stack(doc)
    assert len(st) == 1 and st[0]["params"]["ev"] == 0.5
    names = [s["name"] for s in sidecar.list_snapshots(doc)]
    assert "before-bw" in names and any(n.startswith("auto:before-restore") for n in names)
    with pytest.raises(sidecar.SidecarError):
        sidecar.restore_snapshot(doc, "missing")


def test_auto_snapshots_are_capped(jpeg):
    doc = sidecar.load(jpeg)
    for i in range(sidecar.MAX_AUTO_SNAPSHOTS + 10):
        sidecar.snapshot(doc, f"auto:{i}")
    assert len([s for s in doc["snapshots"] if s["name"].startswith("auto:")]) == sidecar.MAX_AUTO_SNAPSHOTS


def test_ops_hash_tracks_effective_edit():
    a = [E("exposure", ev=0.5)]
    b = [E("exposure", ev=0.5), {"op": "vignette", "params": {"amount": -0.5}, "enabled": False}]
    c = [E("exposure", ev=0.6)]
    assert sidecar.ops_hash(a) == sidecar.ops_hash(b)          # disabled entries do not change the render
    assert sidecar.ops_hash(a) != sidecar.ops_hash(c)
    assert sidecar.ops_hash(a) == sidecar.ops_hash([E("exposure", ev=0.5)])


def test_copy_paste_settings_and_copy_between(lib):
    a, b, r = lib / "a.jpg", lib / "b.jpg", lib / "raw1.npz"
    doc = sidecar.load(a)
    sidecar.set_stack(doc, [E("exposure", ev=0.7), E("contrast", contrast=0.1), E("vignette", amount=-0.3)],
                      mode="landscape")
    sidecar.save(a, doc)
    clip = sidecar.copy_settings(a, ops=["exposure", "vignette"])
    assert [e["op"] for e in clip["stack"]] == ["exposure", "vignette"]
    sidecar.paste_settings(b)
    got = sidecar.get_stack(sidecar.load(b))
    assert [e["op"] for e in got] == ["exposure", "vignette"]
    assert sidecar.load(b)["versions"]["default"]["mode"] == "landscape"
    sidecar.copy_between(a, [r], mode="replace")
    assert len(sidecar.get_stack(sidecar.load(r))) == 3
    # merge mode updates matching ops instead of duplicating them
    d2 = sidecar.load(b)
    sidecar.paste_stack(d2, [E("exposure", ev=-0.5)], mode="merge")
    ev = [e for e in sidecar.get_stack(d2) if e["op"] == "exposure"]
    assert len(ev) == 1 and ev[0]["params"]["ev"] == -0.5
    assert any(s["name"] == "auto:before-paste" for s in d2["snapshots"])


def test_reset_keeps_recovery_snapshot(jpeg):
    doc = sidecar.load(jpeg)
    sidecar.set_stack(doc, [E("exposure", ev=0.7)], mode="portrait")
    sidecar.reset(doc)
    assert sidecar.get_stack(doc) == [] and doc["versions"]["default"]["mode"] is None
    sidecar.restore_snapshot(doc, "auto:before-reset")
    assert len(sidecar.get_stack(doc)) == 1


def test_corrupt_sidecar_raises_clean_error(jpeg):
    sidecar.sidecar_path(jpeg).write_text("{not json")
    with pytest.raises(sidecar.SidecarError):
        sidecar.load(jpeg)


# =============================================================================== ICC + save_array
@pytest.fixture()
def disp(scene):
    return np.ascontiguousarray(scene[:160, :240])


@pytest.mark.parametrize("space,needle", [("srgb", "sRGB"), ("p3", "P3"), ("adobe", "Adobe")])
def test_icc_profiles_are_valid(space, needle):
    from PIL import ImageCms

    data = icc.profile_bytes(space)
    prof = ImageCms.ImageCmsProfile(io.BytesIO(data))
    assert needle.lower() in ImageCms.getProfileDescription(prof).lower()
    assert icc.canonical("display p3") == "p3" and icc.canonical("AdobeRGB") == "adobe"


def test_srgb_profile_matches_pillow_builtin(disp):
    from PIL import ImageCms

    im = Image.fromarray((disp * 255).astype(np.uint8))
    ours = ImageCms.ImageCmsProfile(io.BytesIO(icc.profile_bytes("srgb")))
    ref = ImageCms.createProfile("sRGB")
    lab = ImageCms.createProfile("LAB")
    a = ImageCms.profileToProfile(im, ours, ImageCms.ImageCmsProfile(lab), outputMode="LAB")
    b = ImageCms.profileToProfile(im, ref, ImageCms.ImageCmsProfile(lab), outputMode="LAB")
    assert np.abs(np.asarray(a, np.int16) - np.asarray(b, np.int16)).max() <= 2


def test_save_jpeg_options_and_icc(disp, tmp_path):
    out = tmp_path / "o.jpg"
    export.save_array(disp, out, quality=90, icc="p3", subsampling="4:4:4", progressive=True)
    im = Image.open(out)
    assert im.format == "JPEG" and im.size == (240, 160)
    assert im.info.get("icc_profile") == icc.profile_bytes("p3")
    from PIL import JpegImagePlugin

    assert JpegImagePlugin.get_sampling(im) == 0
    assert im.info.get("progressive") or im.info.get("progression")
    export.save_array(disp, tmp_path / "o420.jpg", subsampling="4:2:0")
    assert JpegImagePlugin.get_sampling(Image.open(tmp_path / "o420.jpg")) == 2


def test_jpeg_quality_changes_size(disp, tmp_path):
    export.save_array(disp, tmp_path / "q50.jpg", quality=50)
    export.save_array(disp, tmp_path / "q95.jpg", quality=95)
    assert (tmp_path / "q95.jpg").stat().st_size > (tmp_path / "q50.jpg").stat().st_size


def test_png8_and_png16_with_icc(disp, tmp_path):
    export.save_array(disp, tmp_path / "a.png", bits=8, icc="srgb")
    im8 = Image.open(tmp_path / "a.png")
    assert im8.mode == "RGB" and im8.info.get("icc_profile")
    export.save_array(disp, tmp_path / "b.png", bits=16, icc="adobe")
    import cv2

    raw = cv2.imread(str(tmp_path / "b.png"), cv2.IMREAD_UNCHANGED)
    assert raw.dtype == np.uint16 and raw.shape[:2] == (160, 240)
    back = raw[..., ::-1].astype(np.float32) / 65535.0
    assert np.abs(back - disp).max() < 2e-4                      # 16-bit precision preserved
    assert Image.open(tmp_path / "b.png").info.get("icc_profile") == icc.profile_bytes("adobe")


def test_tiff16_with_icc_and_dpi(disp, tmp_path):
    tifffile = pytest.importorskip("tifffile")
    out = tmp_path / "t.tif"
    export.save_array(disp, out, bits=16, icc="adobe", dpi=300)
    with tifffile.TiffFile(out) as tf:
        page = tf.pages[0]
        arr = page.asarray()
        assert arr.dtype == np.uint16 and arr.shape == (160, 240, 3)
        assert 34675 in page.tags and bytes(page.tags[34675].value) == icc.profile_bytes("adobe")
        assert abs(float(page.tags["XResolution"].value[0] / page.tags["XResolution"].value[1]) - 300) < 1
    assert np.abs(arr.astype(np.float32) / 65535.0 - disp).max() < 2e-4


def test_webp_lossy(disp, tmp_path):
    out = tmp_path / "w.webp"
    export.save_array(disp, out, quality=80)
    im = Image.open(out)
    assert im.format == "WEBP" and im.size == (240, 160)


@pytest.mark.parametrize("fmt", ["avif", "heif", "jxl"])
def test_optional_formats_work_or_explain(fmt, disp, tmp_path):
    out = tmp_path / ("x." + {"avif": "avif", "heif": "heic", "jxl": "jxl"}[fmt])
    try:
        export.save_array(disp, out)
    except (export.ExportError, MissingDependency) as exc:
        assert "install" in str(exc).lower() or "pip" in str(exc)
    else:
        assert out.stat().st_size > 0


def test_unknown_format_is_an_error(disp, tmp_path):
    with pytest.raises(export.ExportError):
        export.save_array(disp, tmp_path / "x.bmp2", fmt="bmp2")


# =============================================================================== resize / sharpen / watermark / border
def test_resize_modes():
    img = np.random.default_rng(0).random((400, 600, 3)).astype(np.float32)
    assert export.resize_for_export(img, {"long_edge": 300}).shape[:2] == (200, 300)
    assert export.resize_for_export(img, {"short_edge": 100}).shape[:2] == (100, 150)
    mp = export.resize_for_export(img, {"megapixels": 0.06}).shape
    assert abs(mp[0] * mp[1] - 60000) < 2500
    assert export.resize_for_export(img, {"long_edge": 1200}).shape[:2] == (400, 600)               # never upscales
    assert export.resize_for_export(img, {"long_edge": 1200, "upscale": True}).shape[:2] == (800, 1200)
    pad = export.resize_for_export(img, {"width": 1080, "height": 1350, "fit": "pad", "upscale": True,
                                         "pad_color": "#ff0000"})
    assert pad.shape == (1350, 1080, 3) and np.allclose(pad[0, 0], [1, 0, 0], atol=1e-3)
    cover = export.resize_for_export(img, {"width": 200, "height": 200, "fit": "cover"})
    assert cover.shape[:2] == (200, 200)
    exact = export.resize_for_export(img, {"width": 120, "height": 50, "fit": "exact"})
    assert exact.shape[:2] == (50, 120)


def test_output_sharpen_increases_edge_energy(disp):
    s = export.output_sharpen(disp, "screen")
    assert s.shape == disp.shape and s.min() >= 0 and s.max() <= 1
    lap = lambda x: np.abs(np.diff(x.mean(-1), axis=1)).mean()      # noqa: E731
    assert lap(s) > lap(disp)
    assert np.allclose(export.output_sharpen(disp, "screen", amount=0.0), disp)


def test_watermark_and_border(disp):
    wm = export.apply_watermark(disp, {"text": "(c) test", "position": "bottom-right", "opacity": 1.0, "size": 0.08,
                                        "color": "#ffffff"})
    assert wm.shape == disp.shape and np.abs(wm - disp).max() > 0.2
    assert np.abs(wm[: disp.shape[0] // 2] - disp[: disp.shape[0] // 2]).max() < 3e-3      # only touches the corner
    bd = export.apply_border(disp, {"size": 10, "color": "#000000"})
    assert bd.shape[0] > disp.shape[0] and bd.shape[1] > disp.shape[1]
    assert np.allclose(bd[0, 0], 0.0, atol=1e-3)


# =============================================================================== naming
def test_format_name_tokens(jpeg):
    name = export.format_name("{stem}_{date:%Y%m%d}_{seq:03d}_{mode}_{art}", jpeg,
                              {"datetime": "2026-11-12T10:00:00"}, seq=7, mode="portrait", art="warm")
    assert name == "a_20261112_007_portrait_warm"
    assert export.format_name("{stem}-{iso}-{camera}", jpeg, {"iso": 400, "model": "Canon EOS R50"}) == "a-400-CanonEOSR50"
    with pytest.raises(export.ExportError):
        export.format_name("{nonsense}", jpeg, {})
    assert "/" not in export.format_name("a/b{stem}", jpeg, {})


# =============================================================================== export_image
def test_export_image_applies_sidecar_and_preset(jpeg):
    plain = export.export_image(jpeg, preset="web")
    assert plain.parent.name == "exports" and plain.name == "a_web.jpg"
    doc = sidecar.load(jpeg)
    sidecar.set_stack(doc, engine.apply_set(engine.default_stack("jpeg"), ["exposure.ev=-2"]))
    sidecar.save(jpeg, doc)
    dark = export.export_image(jpeg, jpeg.parent / "dark.jpg", preset="web")
    m_plain = np.asarray(Image.open(plain).convert("L"), np.float32).mean()
    m_dark = np.asarray(Image.open(dark).convert("L"), np.float32).mean()
    assert m_dark < m_plain - 20
    im = Image.open(dark)
    assert im.info.get("icc_profile") == icc.profile_bytes("srgb")


def test_export_overrides_and_version(jpeg):
    doc = sidecar.load(jpeg)
    sidecar.new_version(doc, "bright", switch=False)
    sidecar.set_stack(doc, engine.apply_set(engine.default_stack("jpeg"), ["exposure.ev=1"]), "bright")
    sidecar.save(jpeg, doc)
    out = export.export_image(jpeg, jpeg.parent / "o.png", preset="web", version="bright", long_edge=200, format="png")
    im = Image.open(out)
    assert im.format == "PNG" and max(im.size) == 200


EXPECTED = {
    "web": dict(fmt="JPEG", size=(480, 320)),
    "email_small": dict(fmt="JPEG", size=(480, 320)),
    "instagram_portrait": dict(fmt="JPEG", size=(1080, 1350)),
    "instagram_square": dict(fmt="JPEG", size=(1080, 1080)),
    "story_9x16": dict(fmt="JPEG", size=(1080, 1920)),
    "wallpaper_4k": dict(fmt="JPEG", size=(480, 270)),
    "print_tiff16": dict(fmt="TIFF", size=(480, 320)),
    "archive_full": dict(fmt="TIFF", size=(480, 320)),
}


@pytest.mark.parametrize("preset", sorted(EXPECTED))
def test_every_export_preset_runs(preset, jpeg, tmp_path):
    pytest.importorskip("tifffile")
    out = export.export_image(jpeg, tmp_path / "o", preset=preset)
    im = Image.open(out)
    assert im.format == EXPECTED[preset]["fmt"]
    if preset not in ("wallpaper_4k",):
        assert im.size == EXPECTED[preset]["size"], (preset, im.size)
    else:
        assert im.size[0] / im.size[1] == pytest.approx(16 / 9, abs=0.02)


def test_instagram_portrait_pads_not_crops(lib):
    out = export.export_image(lib / "a.jpg", lib / "ig.jpg", preset="instagram_portrait")
    arr = np.asarray(Image.open(out).convert("RGB"))
    assert arr.shape[:2] == (1350, 1080)
    assert arr[0, 0].min() > 240 and arr[-1, -1].min() > 240           # white padding top and bottom


def test_metadata_policies(lib):
    src = lib / "a.jpg"
    full = export.export_image(src, lib / "full.jpg", preset="web", metadata="all")
    none = export.export_image(src, lib / "none.jpg", preset="email_small")
    assert Image.open(full).getexif().get(0x0110) == "Canon EOS R50"
    assert not Image.open(none).getexif().get(0x0110)
    assert not Image.open(none).getexif().get(0x010F)
    cr = export.export_image(src, lib / "cr.jpg", preset="web", metadata="copyright", copyright="(c) me", artist="Me")
    ex = Image.open(cr).getexif()
    assert ex.get(0x8298) == "(c) me" and ex.get(0x013B) == "Me"


def test_export_from_raw(raw, tmp_path):
    out = export.export_image(raw, tmp_path / "r.jpg", preset="web")
    im = Image.open(out)
    assert im.size == (480, 320) and np.asarray(im).std() > 5


def test_export_batch_order_seq_and_errors(lib, tmp_path):
    outdir = tmp_path / "out"
    files = [lib / "a.jpg", lib / "b.jpg", lib / "missing.jpg"]
    res = export.export_batch(files, preset="email_small", out_dir=outdir, jobs=1, name="{stem}_{seq:03d}")
    assert [r["ok"] for r in res] == [True, True, False]
    assert (outdir / "a_001.jpg").exists() and (outdir / "b_002.jpg").exists()
    assert "error" in res[2]
    pool = export.export_batch(files[:2], preset="email_small", out_dir=tmp_path / "pool", jobs=2)
    assert [r["ok"] for r in pool] == [True, True] and Path(pool[0]["out"]).exists()


def test_skip_existing(jpeg):
    a = export.export_image_ex(jpeg, preset="web")
    b = export.export_image_ex(jpeg, preset="web", skip_existing=True)
    assert not a.get("skipped") and b.get("skipped")


def test_contact_sheet(lib, tmp_path):
    out = export.build_contact_sheet([lib / "a.jpg", (lib / "b.jpg", "cap")], tmp_path / "sheet.jpg", columns=2, cell=120)
    im = Image.open(out)
    assert im.size[0] >= 2 * 120


# =============================================================================== CLI end to end
def run(capsys, *argv):
    rc = main([str(a) for a in argv])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def run_json(capsys, *argv):
    rc, out, err = run(capsys, "--json", *argv)
    assert rc in (0, None), (argv, out, err)
    return json.loads(out)


def first(x):
    return x[0] if isinstance(x, list) else x


def test_cli_develop_saves_sidecar_and_renders(capsys, jpeg, tmp_path):
    out = tmp_path / "prev.jpg"
    r = first(run_json(capsys, "develop", jpeg, "--mode", "portrait", "--set", "exposure.ev=+0.3", "--out", out,
                       "--preview", 240))
    assert r["mode"] == "portrait" and r["version"] == "default" and Path(r["out"]).exists()
    assert max(Image.open(out).size) == 240
    doc = sidecar.load(jpeg)
    ev = [e for e in sidecar.get_stack(doc) if e["op"] == "exposure" and not e.get("mask")][0]
    assert ev["params"]["ev"] == pytest.approx(0.3)
    assert doc["versions"]["default"]["mode"] == "portrait"


def test_cli_develop_no_save_sidecar(capsys, jpeg, tmp_path):
    run_json(capsys, "develop", jpeg, "--mode", "landscape", "--no-save-sidecar", "--out", tmp_path / "x.jpg",
             "--preview", 160)
    assert not sidecar.exists(jpeg)


def test_cli_develop_mask_and_edit(capsys, jpeg):
    mask = json.dumps({"type": "linear", "p0": [0.5, 0.2], "p1": [0.5, 0.6]})
    run_json(capsys, "develop", jpeg, "--set", "exposure.ev=-1", "--mask", mask, "--no-render")
    st = sidecar.get_stack(sidecar.load(jpeg))
    masked = [e for e in st if e.get("mask")]
    assert masked and masked[0]["mask"]["type"] == "linear" and masked[0]["params"]["ev"] == -1
    shown = first(run_json(capsys, "edit", jpeg))
    assert shown["changed"] is False and shown["ops_after"] == len(st)
    r = first(run_json(capsys, "edit", jpeg, "--set", "vignette.amount=-0.3"))
    assert r["changed"] is True and r["ops_after"] == len(st) + 1
    r = first(run_json(capsys, "edit", jpeg, "--disable", "vignette"))
    assert [e for e in sidecar.get_stack(sidecar.load(jpeg)) if e["op"] == "vignette"][0]["enabled"] is False
    run_json(capsys, "edit", jpeg, "--remove", "vignette")
    assert not [e for e in sidecar.get_stack(sidecar.load(jpeg)) if e["op"] == "vignette"]


def test_cli_edit_dry_run_does_not_write(capsys, jpeg):
    run_json(capsys, "develop", jpeg, "--no-render")
    before = sidecar.sidecar_path(jpeg).read_text()
    run_json(capsys, "edit", jpeg, "--set", "vignette.amount=-0.9", "--dry-run")
    assert sidecar.sidecar_path(jpeg).read_text() == before


def test_cli_render_matches_engine(capsys, jpeg, tmp_path):
    run_json(capsys, "develop", jpeg, "--set", "exposure.ev=-1", "--no-render")
    out = tmp_path / "r.png"
    r = first(run_json(capsys, "render", jpeg, "--out", out))
    assert out.exists() and r["width"] == 480
    ref = engine.render_file(jpeg)
    got = np.asarray(Image.open(out).convert("RGB"), np.float32) / 255
    assert np.abs(got - ref).mean() < 0.01


def test_cli_versions_and_snapshots(capsys, jpeg):
    run_json(capsys, "develop", jpeg, "--mode", "portrait", "--no-render")
    r = run_json(capsys, "versions", "new", jpeg, "alt")
    assert r["active"] == "alt"
    r = run_json(capsys, "versions", "list", jpeg)
    assert [v["name"] for v in r["versions"]] == ["default", "alt"]
    run_json(capsys, "versions", "snapshot", jpeg, "keep")
    run_json(capsys, "versions", "switch", jpeg, "default")
    run_json(capsys, "versions", "delete", jpeg, "alt")
    assert list(sidecar.load(jpeg)["versions"]) == ["default"]
    snaps = run_json(capsys, "versions", "snapshots", jpeg)
    assert any(s["name"] == "keep" for s in (snaps.get("snapshots") if isinstance(snaps, dict) else snaps))


def test_cli_copy_paste_reset(capsys, lib):
    a, b = lib / "a.jpg", lib / "b.jpg"
    run_json(capsys, "develop", a, "--mode", "landscape", "--no-render")
    run_json(capsys, "copy-settings", a)
    run_json(capsys, "paste-settings", b)
    assert len(sidecar.get_stack(sidecar.load(b))) == len(sidecar.get_stack(sidecar.load(a)))
    run_json(capsys, "reset", b)
    assert sidecar.get_stack(sidecar.load(b)) == []
    run_json(capsys, "paste-settings", b, "--from", a)
    assert sidecar.get_stack(sidecar.load(b))


def test_cli_export_with_preset(capsys, jpeg, tmp_path):
    r = run_json(capsys, "export", jpeg, "--preset", "instagram_square", "--out", tmp_path / "ex")
    assert r["ok"] == 1 and r["failed"] == 0
    out = Path(r["results"][0]["out"])
    assert Image.open(out).size == (1080, 1080)


def test_cli_export_directory_of_files(capsys, lib, tmp_path):
    r = run_json(capsys, "export", lib / "a.jpg", lib / "b.jpg", "--preset", "email_small", "--out", tmp_path / "e")
    assert r["count"] == 2 and r["ok"] == 2


def test_cli_presets_ops_info_status(capsys, jpeg, raw):
    r = run_json(capsys, "presets", "list", "modes")
    names = {p["name"] for p in r}
    assert {"portrait", "night", "street"} <= names
    r = run_json(capsys, "presets", "list", "export")
    assert "web" in {p["name"] for p in r}
    r = run_json(capsys, "presets", "show", "mode", "night:astro")
    assert "stack" in r or "preset" in r
    r = run_json(capsys, "presets", "validate")
    assert r["invalid"] == 0 and r["problems"] == []
    r = run_json(capsys, "ops", "list")
    assert any(o["op"] == "exposure" for o in r["ops"]) and r["count"] > 50
    d = run_json(capsys, "ops", "describe", "exposure")
    assert d["op"] == "exposure" and "ev" in d["defaults"] and d["stage"] == 10
    info = first(run_json(capsys, "info", raw))
    assert info["kind"] == "raw" and info["detected_mode"]["mode"] in modes_names()
    st = run_json(capsys, "status")
    assert isinstance(st, dict) and st


def modes_names():
    from photostudio import modes

    return set(modes.MODE_NAMES)


def test_cli_develop_auto_mode_on_raw(capsys, raw):
    r = first(run_json(capsys, "develop", raw, "--mode", "auto", "--no-render"))
    assert r["mode"] is not None


def test_cli_lut_bake(capsys, tmp_path):
    out = tmp_path / "look.cube"
    run_json(capsys, "lut", "bake", "--art", "standard", "--size", 17, "--out", out)
    text = out.read_text()
    assert "LUT_3D_SIZE 17" in text
    vals = [ln for ln in text.splitlines() if ln and ln[0] in "-0123456789"]
    assert len(vals) == 17 ** 3


def test_cli_errors_are_json_and_nonzero(capsys, tmp_path):
    rc, out, err = run(capsys, "--json", "develop", tmp_path / "nope.jpg")
    assert rc not in (0, None)
    assert "error" in json.loads(out or err)
    rc, out, err = run(capsys, "--json", "develop", "--mode", "does_not_exist", "x.jpg")
    assert rc not in (0, None)


def test_cli_text_output_and_quiet(capsys, jpeg):
    rc, out, err = run(capsys, "presets", "list", "modes")
    assert rc in (0, None) and "portrait" in out
    rc, out, err = run(capsys, "--quiet", "edit", jpeg)
    assert rc in (0, None)


def test_cli_global_flags_after_subcommand(capsys):
    rc, out, err = run(capsys, "presets", "list", "export", "--json")
    assert rc in (0, None) and any(p["name"] == "web" for p in json.loads(out))


def test_cli_plugin_discovery_tolerates_broken_command(monkeypatch, capsys):
    """A command module that raises on import must not take down the CLI."""
    import pkgutil
    import sys
    import types

    import photostudio.commands as cmds

    bad = types.ModuleType("photostudio.commands.zz_broken")

    def register(sub):
        raise RuntimeError("boom")

    bad.register = register
    monkeypatch.setitem(sys.modules, "photostudio.commands.zz_broken", bad)
    real = pkgutil.iter_modules

    def fake(path, prefix=""):
        yield from real(path, prefix)
        if list(path) == list(cmds.__path__):
            yield pkgutil.ModuleInfo(None, "zz_broken", False)

    monkeypatch.setattr(pkgutil, "iter_modules", fake)
    rc, out, err = run(capsys, "--json", "presets", "list", "export")
    assert rc in (0, None) and any(p["name"] == "web" for p in json.loads(out))


def test_cli_calibrate_picture_style_from_pairs(capsys, cr3_lib, scene, tmp_path):
    """Fit a tone curve + matrix from a CR3 and a (slightly different) JPEG of the same scene."""
    from photostudio.testing import write_jpeg

    graded = np.clip(scene ** 0.9 * 1.05, 0, 1)
    write_jpeg(cr3_lib / "shot.JPG", graded)
    out_name = "unit_test_style"
    r = run_json(capsys, "calibrate", "picture-style", "--dir", cr3_lib, "--name", out_name, "--no-save")
    assert r["pairs_used"] == 1 and r["preset"] == "fit_" + out_name
    assert r["residual"]["luma_mae_after"] < r["residual"]["luma_mae_before"]
    assert r["residual"]["chroma_err_after"] <= r["residual"]["chroma_err_before"] * 1.05
    M = np.asarray(r["matrix"])
    assert M.shape == (3, 3) and np.allclose(M.sum(1), 1.0, atol=1e-3)
