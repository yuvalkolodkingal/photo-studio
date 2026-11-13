"""Culling: sharpness / exposure / tilt / noise / eyes metrics, burst grouping, ranking, reasons, target share,
genre weights and catalog glue. Synthetic images only; the blink path uses landmark geometry and an injected face
backend (MediaPipe is optional and not required)."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

from photostudio import cull as C
from photostudio import testing
from photostudio.catalog import Catalog

W, H = 640, 427


def scene(seed: int) -> np.ndarray:
    """Different seeds must look different to pHash (make_scene's seed only changes grain): shift the picture."""
    return np.ascontiguousarray(np.roll(testing.make_scene(W, H, seed=seed), (seed * 131) % W, axis=1))


def save(path: Path, img: np.ndarray, t: datetime | None = None, subsec: str | None = None) -> Path:
    exif = {"Make": "Canon", "Model": "Canon EOS R50", "FocalLength": 50.0, "FNumber": 2.8, "ISOSpeedRatings": 400}
    if t is not None:
        exif["DateTimeOriginal"] = t.strftime("%Y:%m:%d %H:%M:%S")
    testing.write_jpeg(path, np.clip(img, 0, 1).astype(np.float32), exif=exif)
    return path


def blur(img, sigma):
    return cv2.GaussianBlur(img, (0, 0), sigma)


def motion(img, length=25):
    k = np.zeros((length, length), np.float32)
    k[length // 2, :] = 1.0 / length
    return cv2.filter2D(img, -1, k)


def sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _clean_backends():
    C.register_face_backend(None)
    yield
    C.register_face_backend(None)


# ================================================================================================ metrics
def test_sharpness_ranks_sharp_over_soft_over_blurry(tmp_path):
    base = scene(2)
    a = {n: C.analyze(save(tmp_path / f"{n}.jpg", img)) for n, img in
         {"sharp": base, "soft": blur(base, 2.0), "blurry": blur(base, 5.0)}.items()}
    assert a["sharp"]["lap_var"] > a["soft"]["lap_var"] > a["blurry"]["lap_var"]
    assert a["sharp"]["tenengrad"] > a["soft"]["tenengrad"] > a["blurry"]["tenengrad"]
    assert a["sharp"]["sharp_peak"] > a["soft"]["sharp_peak"] > a["blurry"]["sharp_peak"]
    assert "blurry" in a["blurry"]["issues"] and "blurry" not in a["sharp"]["issues"]
    assert a["blurry"]["blur_type"] == "defocus"
    assert a["sharp"]["analyzed_v"] == C.ANALYSIS_VERSION
    # sharpness is scale-normalised: the same picture at half the pixel count scores the same order of magnitude
    small = C.analyze(save(tmp_path / "small.jpg", cv2.resize(base, (W // 2, H // 2), interpolation=cv2.INTER_AREA)))
    assert small["lap_var"] > a["soft"]["lap_var"]


def test_motion_blur_is_directional_defocus_is_not(tmp_path):
    base = scene(2)
    mo = C.analyze(save(tmp_path / "m.jpg", motion(base, 31)))
    de = C.analyze(save(tmp_path / "d.jpg", blur(base, 4.0)))
    sh = C.analyze(save(tmp_path / "s.jpg", base))
    assert mo["motion_anisotropy"] > 0.7
    assert mo["motion_anisotropy"] > de["motion_anisotropy"] + 0.3
    assert mo["motion_anisotropy"] > sh["motion_anisotropy"] + 0.2
    assert C.blur_kind({**mo, "motion_anisotropy": 0.9}) in ("motion", "defocus")
    assert C.blur_kind({**de, "motion_anisotropy": 0.1}) == "defocus"


def test_exposure_and_clipping(tmp_path):
    base = scene(4)
    over = C.analyze(save(tmp_path / "o.jpg", np.clip(base * 3.5, 0, 1)))
    under = C.analyze(save(tmp_path / "u.jpg", base * 0.04))
    ok = C.analyze(save(tmp_path / "k.jpg", base))
    assert over["clip_hi"] > 0.5 and "overexposed" in over["issues"] and over["over"]
    assert under["median_lum"] < 0.1 and "underexposed" in under["issues"] and under["under"]
    assert ok["clip_hi"] < 0.05 and "overexposed" not in ok["issues"] and "underexposed" not in ok["issues"]
    assert 0.2 < ok["median_lum"] < 0.8


def test_raw_clipping_is_measured_on_the_mosaic(tmp_path, monkeypatch):
    from photostudio import decode

    raw = testing.make_fake_raw(tmp_path / "r.npz", scene(3))
    monkeypatch.setattr(decode, "raw_clipping", lambda p, *a, **k: {"clip_hi": 0.2, "clip_lo": 0.0, "G": {"clip_hi": 0.2}})
    a = C.analyze(raw)
    assert a["clip_source"] == "raw" and a["clip_hi"] == pytest.approx(0.2)
    assert "overexposed" in a["issues"]


def test_horizon_tilt_detected(tmp_path):
    def horizon(angle):
        img = np.zeros((H, W, 3), np.float32)
        img[: H // 2] = 0.7
        img[H // 2:] = 0.2
        M = cv2.getRotationMatrix2D((W / 2, H / 2), angle, 1.0)
        img = cv2.warpAffine(img, M, (W, H), borderMode=cv2.BORDER_REFLECT)
        return blur(img, 1.0) + np.random.default_rng(1).normal(0, 0.01, img.shape).astype(np.float32)

    level = C.analyze(save(tmp_path / "l.jpg", horizon(0.0)))
    tilted = C.analyze(save(tmp_path / "t.jpg", horizon(3.0)))
    assert abs(level["tilt_deg"]) < 0.7 and "tilted" not in level["issues"]
    assert abs(abs(tilted["tilt_deg"]) - 3.0) < 0.7 and "tilted" in tilted["issues"]
    # an image without a dominant line is not judged
    assert C.analyze(save(tmp_path / "n.jpg", scene(9)))["tilt_deg"] is None or "tilted" not in \
        C.analyze(save(tmp_path / "n.jpg", scene(9)))["issues"]


def test_noise_estimate_orders_and_is_roughly_calibrated(tmp_path):
    base = scene(6)
    rng = np.random.default_rng(0)
    clean = C.analyze(save(tmp_path / "c.jpg", base))
    noisy = C.analyze(save(tmp_path / "n.jpg", base + rng.normal(0, 0.06, base.shape).astype(np.float32)))
    assert noisy["noise"] > clean["noise"] * 2
    assert 8 < noisy["noise"] < 20                                       # true sigma is 0.06 * 255 = 15.3
    assert "noisy" in noisy["issues"] and "noisy" not in clean["issues"]


def test_eye_aspect_ratio_open_vs_closed():
    def eye(openness):
        w, h = 30.0, 30.0 * openness
        return np.array([[0, 0], [w * .3, -h / 2], [w * .7, -h / 2], [w, 0], [w * .7, h / 2], [w * .3, h / 2]])

    assert C.eye_aspect_ratio(eye(0.30)) > 0.28
    assert C.eye_aspect_ratio(eye(0.05)) < 0.18
    assert C.eye_aspect_ratio(eye(0.30)) == pytest.approx(0.30, abs=1e-6)


def _landmarks(open_: bool) -> np.ndarray:
    pts = np.zeros((478, 2))
    pts[:, 0] = 300.0
    pts[:, 1] = 200.0
    pts[0:10] = [[300, 100]] * 10                              # give the face a vertical extent
    pts[10] = [300, 330]
    pts[1] = [300, 200]                                        # nose tip, centred
    op = 9.0 if open_ else 1.2

    def put(idx, cx, flip):
        d = -1 if flip else 1
        shape = [(-15, 0), (-7, -op), (7, -op), (15, 0), (7, op), (-7, op)]
        for k, (dx, dy) in zip(idx, shape):
            pts[k] = [cx + d * dx, 180 + dy]

    put(C.EAR_RIGHT, 250, False)
    put(C.EAR_LEFT, 350, True)
    return pts


def test_face_from_landmarks_flags_closed_eyes():
    op = C.face_from_landmarks(_landmarks(True), H)
    cl = C.face_from_landmarks(_landmarks(False), H)
    assert op["ear"] > 0.28 and cl["ear"] < 0.18
    assert op["backend"] == "mediapipe" and op["trusted"]


def test_blink_pipeline_with_injected_face_backend(tmp_path):
    marker = scene(5).copy()
    closed = marker.copy()
    closed[:10, :10] = 1.0                                     # marker the fake detector reads

    def backend(img, gray):
        ear = 0.08 if img[2, 2].min() > 0.98 else 0.31
        return [{"bbox": [200.0, 60.0, 420.0, 330.0], "ear": ear, "ear_left": ear, "ear_right": ear, "trusted": True}]

    C.register_face_backend(backend)
    ok = C.analyze(save(tmp_path / "open.jpg", marker))
    bl = C.analyze(save(tmp_path / "blink.jpg", closed))
    assert ok["n_faces"] == 1 and ok["eyes_closed"] is False and bl["eyes_closed"] is True
    assert bl["ear_min"] == pytest.approx(0.08) and "eyes_closed" in bl["issues"] and "eyes_closed" not in ok["issues"]
    assert ok["face_backend"] == "custom" and ok["sharp_face"] is not None
    # an untrusted face (profile / tiny) never produces an eyes-closed verdict
    C.register_face_backend(None)
    C.register_face_backend(lambda i, g: [{"bbox": [10.0, 10.0, 40.0, 40.0], "ear": 0.05, "trusted": False}])
    assert C.analyze(save(tmp_path / "x.jpg", marker))["eyes_closed"] is None
    # no backend at all (no mediapipe / no cascades) degrades quietly
    C.register_face_backend(None)
    assert C.analyze(tmp_path / "x.jpg", faces=False)["n_faces"] == 0


# ================================================================================================ grouping
def _hash(img: np.ndarray) -> dict:
    import imagehash
    from PIL import Image

    pil = Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))
    return {"phash": str(imagehash.phash(pil)), "dhash": str(imagehash.dhash(pil))}


def test_burst_and_scene_grouping():
    a = scene(1)
    b = np.ascontiguousarray(1.0 - a[::-1])                     # a clearly different picture
    base = datetime(2026, 6, 14, 10, 0, 0).timestamp()
    items = [
        {"ts": base, **_hash(a)}, {"ts": base + 0.2, **_hash(a)}, {"ts": base + 0.4, **_hash(blur(a, 1.0))},    # burst
        {"ts": base + 20, **_hash(a)},                                                                           # same scene later
        {"ts": base + 25, **_hash(b)},                                                                           # different scene
        {"ts": base + 4000, **_hash(a)},                                                                         # same look, much later
        {"ts": None, **_hash(b)},                                                                                # no timestamp
    ]
    g = C.group_images(items)
    assert g[0]["burst"] == g[1]["burst"] == g[2]["burst"] is not None
    assert g[3]["burst"] != g[0]["burst"]
    assert g[0]["scene"] == g[1]["scene"] == g[2]["scene"] == g[3]["scene"] is not None
    assert g[4]["scene"] != g[0]["scene"]
    assert g[5]["scene"] != g[0]["scene"]                      # outside the 60 s window
    assert len(g) == len(items)                                # NaN timestamps are tolerated
    assert C.group_images([]) == []
    assert C.group_images([items[0]])[0]["scene"] is None


def test_clip_embeddings_join_visually_similar_frames():
    base = datetime(2026, 6, 14, 10, 0, 0).timestamp()
    items = [{"ts": base, "phash": "0" * 16, "dhash": "0" * 16}, {"ts": base + 10, "phash": "f" * 16, "dhash": "f" * 16}]
    e = np.array([1.0, 0.0, 0.0], np.float32)
    g = C.group_images(items, embeddings={0: e, 1: e * 0.99 + np.array([0, 0.05, 0], np.float32)})
    assert g[0]["scene"] == g[1]["scene"] is not None
    g2 = C.group_images(items, embeddings={0: e, 1: np.array([0, 1.0, 0], np.float32)})
    assert g2[0]["scene"] is None or g2[0]["scene"] != g2[1]["scene"]


# ================================================================================================ ranking / reasons
@pytest.fixture()
def shoot(tmp_path):
    """12 frames: burst A (sharp / soft / blurry, 1 s apart), burst B (sharp + motion), singles with defects."""
    d = tmp_path / "shoot"
    d.mkdir()
    t0 = datetime(2026, 6, 14, 10, 0, 0)
    A, B = scene(21), scene(22)
    spec = [
        ("A1_soft", blur(A, 1.5), 0), ("A2_sharp", A, 1), ("A3_blurry", blur(A, 5), 2),
        ("B1_motion", motion(B, 41), 200), ("B2_sharp", B, 201),
        ("C_over", np.clip(scene(23) * 3.5, 0, 1), 600),
        ("D_under", scene(24) * 0.04, 900),
        ("E_good", scene(25), 1200),
        ("F_noisy", scene(26) + np.random.default_rng(3).normal(0, 0.07, (H, W, 3)).astype(np.float32), 1500),
        ("G_good", scene(27), 1800),
    ]
    paths = []
    for name, img, off in spec:
        paths.append(save(d / f"{name}.jpg", img, t0 + timedelta(seconds=off)))
    return paths


def test_cull_ranks_groups_and_explains(shoot):
    before = {p: sha(p) for p in shoot}
    res = C.cull(shoot, genre="general")
    by = {r["name"].split(".")[0]: r for r in res}
    assert [r["path"] for r in res] == [str(p) for p in shoot]                 # input order preserved
    # burst A: the sharp frame is the best of its group, the blurry one is a reject with a reason
    a = [by["A1_soft"], by["A2_sharp"], by["A3_blurry"]]
    assert len({r["group"] for r in a}) == 1 and a[0]["group"] is not None and a[0]["group_size"] == 3
    assert by["A2_sharp"]["best_of_group"] and by["A2_sharp"]["group_rank"] == 1
    assert by["A2_sharp"]["flag"] == "pick"
    assert by["A3_blurry"]["flag"] == "reject" and "blurry" in by["A3_blurry"]["issues"]
    assert any("blurry" in s.lower() for s in by["A3_blurry"]["reasons"])
    assert by["A2_sharp"]["score"] > by["A1_soft"]["score"] > by["A3_blurry"]["score"]
    # burst B: motion-blurred frame loses to its sharp sibling
    assert by["B2_sharp"]["score"] > by["B1_motion"]["score"] and by["B2_sharp"]["best_of_group"]
    # defects on singles
    assert by["C_over"]["flag"] == "reject" and "overexposed" in by["C_over"]["issues"]
    assert any("clip" in s.lower() or "overexpos" in s.lower() or "highlight" in s.lower() for s in by["C_over"]["reasons"])
    assert by["D_under"]["flag"] == "reject" and "underexposed" in by["D_under"]["issues"]
    assert by["E_good"]["flag"] == "pick" and by["G_good"]["flag"] == "pick" and by["E_good"]["issues"] == []
    assert "noisy" in by["F_noisy"]["issues"]
    for r in res:
        assert r["flag"] in ("pick", "maybe", "reject") and 0 <= r["score"] <= 100
        assert all(isinstance(s, str) for s in r["reasons"])
        assert set(r["features"]) and "analysis" in r
    # culling is advisory: nothing is touched or deleted
    assert {p: sha(p) for p in shoot} == before
    assert not list(shoot[0].parent.glob("*.ps.json"))
    s = C.summarize(res)
    assert s["images"] == len(shoot) and s["pick"] + s["maybe"] + s["reject"] == len(shoot)


def test_target_percentage_caps_picks(shoot):
    res = C.cull(shoot, genre="general", target=0.2)
    picks = [r for r in res if r["flag"] == "pick"]
    assert 1 <= len(picks) <= math.ceil(0.2 * len(shoot))
    best = max(res, key=lambda r: r["score"])
    assert best["flag"] == "pick"
    pct = C.cull(shoot, genre="general", target=20)                           # percent spelling
    assert [r["flag"] for r in pct] == [r["flag"] for r in res]
    wide = C.cull(shoot, genre="general", target=1.0)
    assert sum(r["flag"] == "pick" for r in wide) >= len(picks)
    with pytest.raises(ValueError):
        C.cull(shoot, target=-1)
    # a demoted would-be pick says why
    demoted = [r for r in res if r["flag"] == "maybe" and r["score"] >= 60]
    assert all(r["reasons"] for r in demoted)


def test_analysis_cache_is_reused(shoot, monkeypatch):
    cache: dict = {}
    C.cull(shoot[:3], analyses=cache)
    assert len(cache) == 3
    calls = []
    real = C.analyze
    monkeypatch.setattr(C, "analyze", lambda *a, **k: calls.append(a) or real(*a, **k))
    C.cull(shoot[:3], analyses=cache)
    assert calls == []
    C.cull(shoot[:3], analyses=cache, force=True)
    assert len(calls) == 3


def test_eyes_closed_penalised_most_for_portraits(tmp_path):
    base = scene(31)
    closed = base.copy()
    closed[:10, :10] = 1.0

    def backend(img, gray):
        ear = 0.08 if img[2, 2].min() > 0.98 else 0.31
        return [{"bbox": [200.0, 60.0, 420.0, 330.0], "ear": ear, "trusted": True}]

    C.register_face_backend(backend)
    t0 = datetime(2026, 6, 14, 10, 0, 0)
    p_open = save(tmp_path / "open.jpg", base, t0)
    p_closed = save(tmp_path / "closed.jpg", closed, t0 + timedelta(seconds=1))
    res = C.cull([p_open, p_closed], genre="portrait")
    o, c = res
    assert c["flag"] == "reject" and c["issues"].count("eyes_closed") == 1 and any("eye" in s.lower() for s in c["reasons"])
    assert o["score"] > c["score"] + 15 and o["flag"] == "pick"
    land = C.cull([p_open, p_closed], genre="landscape")
    gap_portrait = o["score"] - c["score"]
    gap_land = land[0]["score"] - land[1]["score"]
    assert gap_portrait > gap_land


def test_genre_weights_follow_the_spec_and_aliases():
    name, w = C.genre_weights("portrait")
    assert name == "portrait" and w["face_sharp"] == pytest.approx(0.35) and w["eyes_open"] == pytest.approx(0.25)
    assert w["clip"] == pytest.approx(0.15) and w["aesthetic"] == pytest.approx(0.15) and w["global_sharp"] == pytest.approx(0.10)
    for g, ww in C.WEIGHTS.items():
        assert sum(ww.values()) == pytest.approx(1.0), g
    assert C.genre_weights("birds")[0] == "wildlife" and C.genre_weights("wedding")[0] == "portrait"
    assert C.genre_weights(None)[0] in C.WEIGHTS
    assert C.genre_weights("something-unknown")[0] == "general"


def test_genre_detection_from_metadata(tmp_path):
    paths = [tmp_path / f"{i}.jpg" for i in range(3)]
    metas = [{"focal35": 600, "aperture": 6.3, "iso": 1600, "shutter": 1 / 2000}] * 3
    assert C.genre_weights(C.detect_genre([str(p) for p in paths], metas))[0] in C.WEIGHTS
    assert C.genre_weights(C.detect_genre([], None))[0] in C.WEIGHTS


# ================================================================================================ catalog glue
def _catalog(paths):
    root = Path(paths[0]).parent
    c = Catalog(root)
    c.scan()
    return c


def test_catalog_analyze_cache_cull_and_apply(shoot):
    with _catalog(shoot) as cat:
        first = C.analyze_catalog(cat)
        assert first["analyzed"] == len(shoot) and first["failed"] == 0
        again = C.analyze_catalog(cat)
        assert again["analyzed"] == 0 and again["cached"] == len(shoot)
        assert cat.get(1)["analysis"]["lap_var"] > 0
        assert cat.ids("issue:blurry")                                           # analysis is queryable
        assert cat.ids("sharpness>100")
        grp = C.group_catalog(cat)
        assert cat.ids("scene>=1") and grp
        res = C.cull_catalog(cat, genre="general")
        assert len(res) == len(shoot) and all("id" in r for r in res)
        names = {r["name"].split(".")[0]: r for r in res}
        before = {p: sha(p) for p in shoot}
        dry = C.apply_cull(cat, res, dry_run=True)
        assert cat.count("flag:pick") == 0 and cat.count("flag:reject") == 0
        assert dry["dry_run"] is True and sum(dry["counts"].values()) == len(shoot)
        out = C.apply_cull(cat, res, flags=True)
        assert out["counts"] == dry["counts"] and out["moved"] == []
        assert cat.ids("flag:reject") and cat.ids("flag:pick")
        rejected = cat.get(names["A3_blurry"]["id"])
        assert rejected["flag"] == "reject"
        sc = json.loads(Path(str(shoot[2]) + ".ps.json").read_text())
        assert sc["flag"] == "reject"
        assert {p: sha(p) for p in shoot} == before                              # image bytes untouched
        # rejects can be moved aside, never deleted
        mv = C.apply_cull(cat, res, flags=False, move_rejects=True)
        rd = cat.root / "_rejects"
        assert rd.is_dir() and list(rd.glob("*A3_blurry*"))
        assert sum(1 for _ in rd.glob("*.jpg")) == len([r for r in res if r["flag"] == "reject"])
        assert len(mv["moved"]) >= len([r for r in res if r["flag"] == "reject"])
        n_rej = len([r for r in res if r["flag"] == "reject"])
        assert cat.count() == len(shoot) - n_rej                                 # _rejects/ is never indexed
        assert not cat.ids("folder:_rejects") and mv["rescan"]["removed"] == n_rej
        shutil.move(str(next(rd.glob("*A3_blurry.jpg"))), str(shoot[2]))          # put one back and scan: it is indexed again
        cat.scan()
        assert cat.count() == len(shoot) - n_rej + 1


def test_cull_cli_never_writes_without_apply(shoot, capsys):
    from photostudio import cli

    root = str(shoot[0].parent)
    rc = cli.main(["--root", root, "--json", "cull", "--target", "0.3", "--genre", "general"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["summary"]["images"] == len(shoot) and "note" in out and "applied" not in out
    assert not list(Path(root).glob("*.ps.json"))
    assert all(isinstance(r["reasons"], str) for r in out["results"])
    rc = cli.main(["--root", root, "--json", "cull", "--apply", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and "applied" in out
    assert not list(Path(root).glob("*.ps.json"))
    rc = cli.main(["--root", root, "--json", "cull", "--apply"])
    capsys.readouterr()
    assert rc == 0 and list(Path(root).glob("*.ps.json"))
    rc = cli.main(["--root", root, "--json", "analyze", "--show", "2"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and len(out["analysis"]) == 2 and "clip_emb" not in out["analysis"][0]
    rc = cli.main(["--root", root, "--json", "group"])
    assert rc == 0
    capsys.readouterr()
