"""Astro stacking engine, finishing, CLI and GUI on small synthetic star fields."""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from photostudio.astro import engine as E
from photostudio.astro import post

H, W, N = 300, 450, 16


def _field(n_stars, bright=True, seed=1, nudge_at=8, nudge=(30.0, -18.0), drift=(0.6, 0.25), noise=0.004):
    rng = np.random.default_rng(seed)
    xs = rng.uniform(-60, W + 60, n_stars)
    ys = rng.uniform(-60, H * 0.8, n_stars)
    fl = rng.pareto(1.5, n_stars) * 0.02 + 0.006
    if bright:
        xs, ys, fl = np.r_[xs, W * 0.45], np.r_[ys, H * 0.4], np.r_[fl, 0.8]
    yy, xx = np.mgrid[0:H, 0:W]
    frames, offs, off = [], [], np.zeros(2)
    for i in range(N):
        off = off + drift
        if i == nudge_at:
            off = off + nudge
        img = np.full((H, W), 0.01, np.float32)
        for x, y, f in zip(xs + off[0], ys + off[1], fl):
            if -8 < x < W + 8 and -8 < y < H + 8:
                x0, y0 = int(x), int(y)
                sl = (slice(max(0, y0 - 6), y0 + 7), slice(max(0, x0 - 6), x0 + 7))
                img[sl] += f * np.exp(-((xx[sl] - x) ** 2 + (yy[sl] - y) ** 2) / (2 * 1.3 ** 2))
        img[int(H * 0.8):] = 0.03
        img += rng.normal(0, noise, img.shape).astype(np.float32)
        frames.append(np.repeat(img[..., None], 3, 2))
        offs.append(off.copy())
    return frames, np.array(offs)


def _check_transforms(info, offs, tol=0.6):
    ref = info["reference"]
    for k, M in info["transforms"].items():
        M = np.asarray(M)
        p = np.array([W / 2, H / 2, 1.0])
        q = M @ p
        q = q[:2] / q[2]
        want = p[:2] + (offs[ref] - offs[int(k)])
        assert np.linalg.norm(q - want) < tol, (k, q, want)


@pytest.mark.parametrize("n_stars,mode", [(250, "stars"), (3, "ecc")])
def test_stack_registers_through_a_nudge(n_stars, mode):
    frames, offs = _field(n_stars)
    r = E.stack(frames, E.StackOptions(horizon=0.2, jobs=2, work_side=W, group=4))
    i = r.info
    assert i["registration"] == mode
    assert i["frames_used"] == N
    assert i["nudges_before"] == [8]
    _check_transforms(i, offs)
    ref = frames[i["reference"]][..., 1]
    L = r.image[..., 1]
    mad = lambda a: np.median(np.abs(np.diff(a[: int(0.6 * a.shape[0])], axis=1)))   # robust: stars don't count
    assert mad(L) < 0.45 * mad(ref)                          # ~sqrt(16) = 4x less noise, generous bound
    assert r.half_a is not None and r.half_a.shape == r.image.shape
    assert r.coverage.min() >= 0.9 * N


def test_moving_frames_are_dropped():
    frames, _ = _field(250, nudge_at=99)
    rng = np.random.default_rng(3)
    for k in (5, 6):   # two frames shaken far away from the track
        frames[k] = np.roll(frames[k], (int(rng.integers(40, 60)), int(rng.integers(40, 60))), (0, 1))
    r = E.stack(frames, E.StackOptions(horizon=0.2, jobs=2, work_side=W))
    assert {"5", "6"} <= set(r.info["dropped"])


def test_find_segments_adapts_to_jitter():
    rng = np.random.default_rng(0)
    d = np.tile([-2.0, 0.8], (60, 1)) + rng.normal(0, 1.5, (60, 2))
    d[0] = 0
    d[30] += [45, -30]
    segs, nudges, moving = E.find_segments(d, np.ones(60), thr=2.0, min_segment=3)
    assert nudges == [30] and moving == [] and len(segs) == 2


def test_reject_modes_and_crop():
    frames, _ = _field(250, nudge_at=99)
    frames[3][50:52, :] = 1.0            # satellite trail in one frame
    for rej in ("sigma", "winsor", "mean", "max"):
        r = E.stack(frames, E.StackOptions(reject=rej, jobs=2, work_side=W, halves=False, cache="off"))
        assert r.image.shape[2] == 3 and np.isfinite(r.image).all()
        if rej == "sigma":
            M = np.asarray(r.info["transforms"]["3"])
            y = int(round((M @ [200, 51, 1])[1])) - r.info["crop"][0]
            assert r.image[y, 150:250, 1].mean() < 0.1          # trail rejected


def test_inner_crop_finds_largest_rectangle():
    cov = np.zeros((200, 300), np.uint16)
    cov[20:180, 30:290] = 10
    cov[0:10, 0:300] = 10      # a separate strip that must not win
    r, c = E._inner_crop(cov, 9)
    assert r.start >= 20 and r.stop <= 180 and c.start >= 30 and c.stop <= 290
    assert (r.stop - r.start) > 130 and (c.stop - c.start) > 230


def test_finish_pipeline_and_validation():
    frames, _ = _field(250, nudge_at=99)
    r = E.stack(frames, E.StackOptions(jobs=2, work_side=W))
    g = r.image.copy()
    g += np.linspace(0, 0.02, g.shape[1], dtype=np.float32)[None, :, None]     # light-pollution gradient
    flat, bg = post.remove_gradient(g)
    assert abs(flat[:, :40].mean() - flat[:, -40:].mean()) < 0.003
    disp, info = post.finish(g, post.FinishOptions(to_srgb=False))
    assert disp.shape == g.shape and 0 <= disp.min() and disp.max() <= 1
    assert 0.02 < np.median(disp) < 0.25                                      # auto brightness
    v = post.validate_sources(r.image, r.half_a, r.half_b)
    assert v["confirmed"] >= 10 and v["half_test_false_alarm_rate"] < 0.02


def test_cli_stack_and_finish(tmp_path):
    import tifffile

    from photostudio.cli import main

    frames, _ = _field(250, nudge_at=99)
    files = []
    for k, f in enumerate(frames[:8]):
        p = tmp_path / f"f{k:02d}.tif"
        tifffile.imwrite(p, (np.clip(f, 0, 1) * 65535).astype(np.uint16))
        files.append(str(p))
    out = tmp_path / "out.tif"
    assert main(["--json", "astro", "stack", *files, "--out", str(out), "--jobs", "2", "--validate"]) == 0
    assert out.exists() and (tmp_path / "out_linear.tif").exists()
    rep = json.loads((tmp_path / "out_report.json").read_text())
    assert rep["frames_used"] == 8
    assert main(["--json", "astro", "finish", str(tmp_path / "out_linear.tif"), "--out", str(tmp_path / "fin.jpg"),
                 "--hdr", "--denoise", "0.8"]) == 0
    assert (tmp_path / "fin.jpg").exists()


def test_gui_end_to_end(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import tifffile
    from fastapi.testclient import TestClient

    from photostudio.astro import gui

    monkeypatch.setenv("PHOTOSTUDIO_ASTRO_ROOTS", str(tmp_path))
    frames, _ = _field(250, nudge_at=99)
    files = []
    for k, f in enumerate(frames[:6]):
        p = tmp_path / f"g{k}.tif"
        tifffile.imwrite(p, (np.clip(f, 0, 1) * 65535).astype(np.uint16))
        files.append(str(p))
    c = TestClient(gui.create_app(workers=2))
    assert "Astro Stacker" in c.get("/").text
    b = c.get("/api/browse", params={"path": str(tmp_path)}).json()
    assert len(b["files"]) == 6
    assert c.get("/api/thumb", params={"path": files[0], "size": 200}).headers["content-type"] == "image/jpeg"
    assert c.get("/api/browse", params={"path": "/etc"}).status_code == 403
    assert c.post("/api/stack", json={"lights": files}, headers={"Origin": "http://evil.example"}).status_code == 403
    jid = c.post("/api/stack", json={"lights": files, "options": {"jobs": 2}, "out": str(tmp_path / "s.jpg")}).json()["job"]
    for _ in range(600):
        st = c.get(f"/api/job/{jid}").json()
        if st["state"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert st["state"] == "done", st
    assert st["info"]["frames_used"] == 6 and (tmp_path / "s.jpg").exists()
    assert c.post(f"/api/job/{jid}/refinish", json={"hdr": True, "denoise": 0}).json()["ok"]
    assert c.get(f"/api/job/{jid}/preview.jpg").content[:2] == b"\xff\xd8"
