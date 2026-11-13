"""Web editor (photostudio.web): every endpoint exercised with FastAPI's TestClient on a synthetic library."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from photostudio import sidecar, testing  # noqa: E402
from photostudio.web import create_app  # noqa: E402
from photostudio.web.server import STATIC  # noqa: E402

W, H = 480, 320


def decode(resp) -> np.ndarray:
    assert resp.status_code == 200, resp.text[:300]
    arr = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
    assert arr is not None
    return arr[..., ::-1].astype(np.float32)


@pytest.fixture()
def library(tmp_path, scene) -> Path:
    d = tmp_path / "lib"
    (d / "trip").mkdir(parents=True)
    ex = {"Make": "Canon", "Model": "Canon EOS R50", "ISOSpeedRatings": 200, "FNumber": 8.0, "FocalLength": 35.0,
          "DateTimeOriginal": "2026:11:12 10:00:00"}
    testing.write_jpeg(d / "a.jpg", scene, exif=ex)
    testing.write_jpeg(d / "b.jpg", np.ascontiguousarray(scene[::-1]), exif={**ex, "DateTimeOriginal": "2026:11:12 10:00:05"})
    testing.write_jpeg(d / "trip" / "c.jpg", np.ascontiguousarray(scene[:, ::-1]), exif={**ex, "DateTimeOriginal": "2026:11:13 09:00:00"})
    testing.make_fake_raw(d / "raw1.npz", scene)
    return d


@pytest.fixture()
def client(library):
    app = create_app(library, background_scan=False)
    app.state.ps.lib.start_scan(wait=True)
    with TestClient(app) as c:
        c.root = library
        c.state = app.state.ps
        yield c


def get_image(c, p="a.jpg", **kw):
    r = c.get("/api/image", params={"p": p, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def put(c, p, stack, **kw):
    r = c.put("/api/image/stack", json={"p": p, "stack": stack, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def with_param(stack, op, **params):
    out = []
    hit = False
    for e in stack:
        e = json.loads(json.dumps(e))
        if e["op"] == op and not e.get("mask"):
            e["params"].update(params)
            hit = True
        out.append(e)
    if not hit:
        out.append({"op": op, "params": params})
    return out


# =============================================================================== schema / info
def test_info_and_schema_cover_every_op(client):
    info = client.get("/api/info").json()
    assert info["catalog"] is True and info["n_ops"] > 100
    sch = client.get("/api/schema").json()
    from photostudio.pipeline.registry import all_ops

    assert set(sch["ops"]) == set(all_ops())          # every registered op is reachable from the UI
    ev = next(p for p in sch["ops"]["exposure"]["params"] if p["name"] == "ev")
    assert ev["type"] == "number" and ev["min"] == -5 and ev["max"] == 5
    wb = sch["ops"]["white_balance"]
    assert any(p["name"] == "method" and p["type"] == "choice" for p in wb["params"])
    assert sch["ops"]["crop"]["changes_geometry"] is True
    # heuristics: an op without a curated table entry still gets typed controls
    types = {p["type"] for d in sch["ops"].values() for p in d["params"]}
    assert {"number", "bool", "choice", "color", "vec", "json", "string"} <= types
    assert "linear" in sch["mask_types"] and "ai_subject" in sch["mask_types"]
    r = client.get("/api/schema", headers={"If-None-Match": client.get("/api/schema").headers["etag"]})
    assert r.status_code == 304


# =============================================================================== library
def test_library_listing_and_filters(client):
    items = client.get("/api/library/images").json()
    assert items["catalog"] is True
    names = {i["p"] for i in items["items"]}
    assert {"a.jpg", "b.jpg", "raw1.npz", "trip/c.jpg"} <= names
    folder = client.get("/api/library/images", params={"folder": "trip"}).json()
    assert [i["p"] for i in folder["items"]] == ["trip/c.jpg"]
    folders = client.get("/api/library/folders").json()["folders"]
    assert {f["path"] for f in folders} >= {"", "trip"}

    r = client.post("/api/image/meta", json={"paths": ["a.jpg"], "rating": 4, "flag": "pick", "label": "red", "add_tags": ["places|israel", "kids"]})
    assert r.status_code == 200, r.text
    assert r.json()["updated"][0]["rating"] == 4
    assert [i["p"] for i in client.get("/api/library/images", params={"rating_min": 3}).json()["items"]] == ["a.jpg"]
    assert [i["p"] for i in client.get("/api/library/images", params={"flag": "pick"}).json()["items"]] == ["a.jpg"]
    assert [i["p"] for i in client.get("/api/library/images", params={"label": "red"}).json()["items"]] == ["a.jpg"]
    assert [i["p"] for i in client.get("/api/library/images", params={"tag": "kids"}).json()["items"]] == ["a.jpg"]
    assert [i["p"] for i in client.get("/api/library/images", params={"q": "kids"}).json()["items"]] == ["a.jpg"]
    assert client.get("/api/library/images", params={"edited": "true"}).json()["total"] == 0
    # the sidecar on disk is the single source of truth (what Claude's CLI reads)
    doc = sidecar.load(client.root / "a.jpg")
    assert doc["rating"] == 4 and doc["flag"] == "pick"
    tags = client.get("/api/tags").json()["tags"]
    assert any(t["path"] == "kids" for t in tags)
    assert client.post("/api/image/meta", json={"paths": ["a.jpg"], "rating": 9}).status_code == 422


def test_albums_static_and_smart(client):
    a = client.post("/api/albums", json={"name": "Favourites"}).json()
    assert a["kind"] == "static"
    r = client.post(f"/api/albums/{a['id']}/images", json={"paths": ["a.jpg", "b.jpg"]})
    assert r.json()["changed"] == 2
    got = client.get("/api/library/images", params={"album": f"id:{a['id']}"}).json()
    assert {i["p"] for i in got["items"]} == {"a.jpg", "b.jpg"}
    client.post("/api/image/meta", json={"paths": ["b.jpg"], "rating": 5})
    smart = client.post("/api/albums", json={"name": "Five", "kind": "smart", "rule": "rating>=5"}).json()
    got = client.get("/api/library/images", params={"album": f"id:{smart['id']}"}).json()
    assert [i["p"] for i in got["items"]] == ["b.jpg"]
    assert len(client.get("/api/albums").json()["albums"]) == 2
    assert client.delete(f"/api/albums/{smart['id']}").status_code == 200


def test_plain_library_fallback(library):
    app = create_app(library, use_catalog=False)
    with TestClient(app) as c:
        assert c.get("/api/info").json()["catalog"] is False
        res = c.get("/api/library/images").json()
        assert res["catalog"] is False and {i["p"] for i in res["items"]} >= {"a.jpg", "raw1.npz"}
        assert c.post("/api/image/meta", json={"paths": ["b.jpg"], "rating": 3, "add_tags": ["x|y"]}).status_code == 200
        assert sidecar.load(library / "b.jpg")["rating"] == 3
        assert [i["p"] for i in c.get("/api/library/images", params={"rating_min": 3}).json()["items"]] == ["b.jpg"]
        assert c.get("/api/albums").json()["available"] is False
        assert c.post("/api/albums", json={"name": "x"}).status_code == 422
        assert c.get("/api/thumb", params={"p": "a.jpg", "size": 128}).status_code == 200


def test_thumbnail_cached_and_reflects_edits(client):
    r1 = client.get("/api/thumb", params={"p": "a.jpg", "size": 200})
    img = decode(r1)
    assert max(img.shape[:2]) <= 200
    assert client.get("/api/thumb", params={"p": "a.jpg", "size": 200}, headers={"If-None-Match": r1.headers["etag"]}).status_code == 304
    stack = get_image(client)["stack"]
    res = put(client, "a.jpg", with_param(stack, "exposure", ev=1.5))
    assert res["ops_hash"]
    r2 = client.get("/api/thumb", params={"p": "a.jpg", "size": 200})
    assert r2.content != r1.content and decode(r2).mean() > img.mean()
    assert client.get("/api/library/images", params={"edited": "true"}).json()["total"] == 1   # catalog refreshed after the save
    assert client.get("/api/thumb", params={"p": "raw1.npz", "size": 160}).status_code == 200


# =============================================================================== sidecar load / save
def test_sidecar_roundtrip_and_interop(client):
    img = get_image(client)
    assert img["materialised"] is False and img["stack"] and img["kind"] == "jpeg"
    assert img["meta"]["iso"] == 200
    stack = with_param(img["stack"], "exposure", ev=0.7)
    res = put(client, "a.jpg", stack, label="Exposure", mode="events")
    assert res["mtime_ns"] > 0
    # (1) the CLI reads what the UI wrote
    doc = sidecar.load(client.root / "a.jpg")
    ex = next(e for e in sidecar.get_stack(doc) if e["op"] == "exposure")
    assert ex["params"]["ev"] == 0.7 and doc["versions"]["default"]["mode"] == "events"
    # (2) rating written by the catalog/CLI survives a later UI stack save (read-modify-write)
    client.post("/api/image/meta", json={"paths": ["a.jpg"], "rating": 5, "add_tags": ["keep"]})
    put(client, "a.jpg", with_param(stack, "contrast", contrast=0.2))
    doc = sidecar.load(client.root / "a.jpg")
    assert doc["rating"] == 5 and "keep" in doc["tags"]
    again = get_image(client)
    assert again["materialised"] is True and any(e["op"] == "contrast" for e in again["stack"])
    # (3) bad stacks are rejected, not stored
    assert client.put("/api/image/stack", json={"p": "a.jpg", "stack": [{"nope": 1}]}).status_code == 422
    assert client.put("/api/image/stack", json={"p": "a.jpg", "stack": "x"}).status_code == 422


def test_external_edit_detected_and_conflict(client):
    img = get_image(client)
    s = client.state
    base = put(client, "a.jpg", with_param(img["stack"], "exposure", ev=0.2))
    seq0 = client.get("/api/changes").json()["seq"]
    s.events.poll_once()                       # our own write is not reported as external
    assert not [e for e in client.get("/api/changes", params={"since": seq0}).json()["events"] if e.get("external")]
    # Claude edits through the CLI behind our back
    time.sleep(0.02)
    path = client.root / "a.jpg"
    doc = sidecar.load(path)
    st = sidecar.get_stack(doc)
    st = with_param(st, "exposure", ev=-0.5)
    sidecar.set_stack(doc, st)
    sidecar.save(path, doc)
    evs = s.events.poll_once()
    assert evs and evs[0]["external"] and evs[0]["p"] == "a.jpg"
    got = client.get("/api/changes", params={"since": seq0}).json()
    assert any(e["type"] == "sidecar" and e["external"] for e in got["events"])
    # a stale UI save must not clobber Claude's edit silently
    r = client.put("/api/image/stack", json={"p": "a.jpg", "stack": with_param(img["stack"], "exposure", ev=1.0)})
    assert r.status_code == 409 and r.json()["conflict"] is True
    assert next(e for e in r.json()["image"]["stack"] if e["op"] == "exposure")["params"]["ev"] == -0.5
    # after reloading, saving works again; force overrides
    assert client.put("/api/image/stack", json={"p": "a.jpg", "stack": with_param(img["stack"], "exposure", ev=1.0)}).status_code == 200
    assert base["mtime_ns"] > 0


# =============================================================================== render
def test_render_modified_stack_differs_before_after_draft(client):
    img = get_image(client)
    base_stack = img["stack"]
    r0 = client.post("/api/render", json={"p": "a.jpg", "stack": base_stack, "long_edge": 400, "session": "t"})
    a = decode(r0)
    assert max(a.shape[:2]) == 400 and r0.headers["x-image-size"] == "400,267" and r0.headers["etag"]
    assert r0.headers["x-full-size"] == f"{W},{H}"
    r1 = client.post("/api/render", json={"p": "a.jpg", "stack": with_param(base_stack, "exposure", ev=1.0), "long_edge": 400, "session": "t"})
    b = decode(r1)
    assert r1.content != r0.content and b.mean() > a.mean() + 5
    # draft size
    r2 = client.post("/api/render", json={"p": "a.jpg", "stack": base_stack, "long_edge": 160, "draft": True, "quality": 60, "session": "t"})
    assert max(decode(r2).shape[:2]) == 160 and r2.headers["x-draft"] == "1"
    # before = default stack regardless of the edit that was passed
    rb = client.post("/api/render", json={"p": "a.jpg", "stack": with_param(base_stack, "exposure", ev=2.0), "long_edge": 400, "before": True, "session": "t"})
    assert rb.content == r0.content
    # GET with stored stack + ETag/304
    g = client.get("/api/render", params={"p": "a.jpg", "long_edge": 300})
    assert g.status_code == 200
    assert client.get("/api/render", params={"p": "a.jpg", "long_edge": 300}, headers={"If-None-Match": g.headers["etag"]}).status_code == 304
    # an invalid parameter value is skipped with a warning, not a 500
    bad = client.post("/api/render", json={"p": "a.jpg", "stack": [{"op": "white_balance", "params": {"method": "nope"}}], "long_edge": 200})
    assert bad.status_code == 200 and "skipped" in bad.headers.get("x-warnings", "")
    assert client.post("/api/render", json={"p": "nope.jpg"}).status_code == 404


def test_render_latest_wins(client):
    stack = get_image(client)["stack"]
    r_new = client.post("/api/render", json={"p": "a.jpg", "stack": stack, "long_edge": 300, "session": "lw", "seq": 10})
    assert r_new.status_code == 200 and r_new.headers["x-seq"] == "10"
    r_old = client.post("/api/render", json={"p": "a.jpg", "stack": stack, "long_edge": 310, "session": "lw", "seq": 3})
    assert r_old.status_code == 204 and r_old.headers["x-superseded"] == "1"
    # other sessions are independent
    assert client.post("/api/render", json={"p": "a.jpg", "stack": stack, "long_edge": 300, "session": "other", "seq": 1}).status_code == 200


def test_render_unknown_op_is_warning_and_geometry_nogeo(client):
    stack = get_image(client)["stack"]
    r = client.post("/api/render", json={"p": "a.jpg", "stack": stack + [{"op": "does_not_exist", "params": {}}], "long_edge": 200})
    assert r.status_code == 200 and "does_not_exist" in r.headers["x-warnings"]
    cropped = stack + [{"op": "crop", "params": {"rect": [0.25, 0.25, 0.5, 0.5]}}]
    c = client.post("/api/render", json={"p": "a.jpg", "stack": cropped, "long_edge": 400})
    full = client.post("/api/render", json={"p": "a.jpg", "stack": cropped, "long_edge": 400, "nogeo": True})
    assert decode(c).shape[:2] != decode(full).shape[:2]       # crop applies, but is bypassed while the crop tool is open
    assert max(decode(full).shape[:2]) == 400


def test_region_render_is_one_to_one(client):
    stack = get_image(client)["stack"]
    r = client.post("/api/render/region", json={"p": "a.jpg", "stack": stack, "rect": [0.25, 0.25, 0.75, 0.75]})
    img = decode(r)
    assert abs(img.shape[1] - W // 2) <= 2 and abs(img.shape[0] - H // 2) <= 2     # source is 480x320, so 1:1 = 240x160
    assert [round(float(v), 2) for v in r.headers["x-region"].split(",")] == [0.25, 0.25, 0.75, 0.75]
    big = client.post("/api/render/region", json={"p": "a.jpg", "stack": stack, "rect": [0, 0, 1, 1], "max_px": 200})
    assert max(decode(big).shape[:2]) == 200
    assert client.post("/api/render/region", json={"p": "a.jpg", "rect": "x"}).status_code == 422


def test_raw_render_through_engine(client):
    img = get_image(client, "raw1.npz")
    assert img["kind"] == "raw" and any(e["op"] == "raw" for e in img["stack"])
    r = client.post("/api/render", json={"p": "raw1.npz", "stack": img["stack"], "long_edge": 300})
    assert decode(r).mean() > 20
    r2 = client.post("/api/render", json={"p": "raw1.npz", "stack": with_param(img["stack"], "exposure", ev=-1.0), "long_edge": 300})
    assert decode(r2).mean() < decode(r).mean()


# =============================================================================== scopes
def test_scopes_and_overlays(client):
    stack = get_image(client)["stack"]
    assert client.get("/api/scopes/histogram", params={"session": "scopes"}).status_code == 422    # nothing rendered yet
    client.post("/api/render", json={"p": "a.jpg", "stack": stack, "long_edge": 300, "session": "scopes"})
    h = client.get("/api/scopes/histogram", params={"session": "scopes", "bins": 64}).json()
    assert {"r", "g", "b", "luma", "clip_high_pct", "clip_low_pct", "median"} <= set(h) and len(h["r"]) == 64
    assert sum(h["luma"]) > 1000
    for kind in ("waveform", "vectorscope", "parade", "histogram", "false_color"):
        r = client.get(f"/api/scopes/{kind}.png", params={"session": "scopes"})
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG", kind
    assert client.get("/api/scopes/bogus.png", params={"session": "scopes"}).status_code == 422
    hot = client.post("/api/render", json={"p": "a.jpg", "stack": with_param(stack, "exposure", ev=3.0), "long_edge": 300, "overlay": "clipping", "session": "scopes"})
    plain = client.post("/api/render", json={"p": "a.jpg", "stack": with_param(stack, "exposure", ev=3.0), "long_edge": 300, "session": "scopes"})
    ov = decode(hot)
    assert hot.content != plain.content
    assert (ov[..., 0] > 200).mean() > 0.01 and ov[..., 0].mean() > decode(plain)[..., 0].mean() * 0.5   # red clipping warning present
    assert client.post("/api/render", json={"p": "a.jpg", "stack": stack, "long_edge": 200, "overlay": "bogus"}).status_code == 422


# =============================================================================== masks / local adjustments
def test_mask_preview_and_local_adjustment_with_id(client):
    stack = get_image(client)["stack"]
    spec = {"type": "linear", "p0": [0.5, 0.0], "p1": [0.5, 0.6], "feather": 0}
    r = client.post("/api/mask/preview", json={"p": "a.jpg", "stack": stack, "mask": spec, "long_edge": 300})
    ov = decode(r)
    assert ov.shape[:2] == (200, 300)
    # the top of the frame (mask = 1) is red-tinted, the bottom is not
    assert ov[:20, :, 0].mean() - ov[:20, :, 2].mean() > ov[-20:, :, 0].mean() - ov[-20:, :, 2].mean() + 20
    # a masked, id-tagged local adjustment is persisted and only changes the masked region
    local = {"op": "exposure", "id": "mask:m1:exposure", "params": {"ev": 1.5}, "mask": {**spec, "name": "Sky"}}
    put(client, "a.jpg", stack + [local])
    got = get_image(client)
    mine = [e for e in got["stack"] if e.get("id") == "mask:m1:exposure"]
    assert len(mine) == 1 and mine[0]["mask"]["p1"] == [0.5, 0.6] and mine[0]["mask"]["name"] == "Sky"
    base = decode(client.post("/api/render", json={"p": "a.jpg", "stack": stack, "long_edge": 300}))
    edited = decode(client.post("/api/render", json={"p": "a.jpg", "stack": stack + [local], "long_edge": 300}))
    assert edited[:20].mean() > base[:20].mean() + 10 and abs(edited[-20:].mean() - base[-20:].mean()) < 1.5
    # every drawn mask type renders
    for m in ({"type": "radial", "center": [0.5, 0.5], "radii": [0.2, 0.2], "feather": 30},
              {"type": "brush", "strokes": [{"points": [[0.2, 0.2], [0.5, 0.5]], "radius": 40, "hardness": 0.5, "flow": 1}]},
              {"type": "luminosity", "range": [0.5, 1.0], "softness": 0.1},
              {"type": "color_range", "point": [0.5, 0.1], "tolerance": 20, "falloff": 15},
              {"type": "combine", "mode": "subtract", "masks": [{"type": "radial", "center": [0.5, 0.5], "radii": [0.4, 0.4]},
                                                                  {"type": "radial", "center": [0.5, 0.5], "radii": [0.1, 0.1]}]}):
        assert client.post("/api/mask/preview", json={"p": "a.jpg", "stack": stack, "mask": m, "long_edge": 200}).status_code == 200, m
    assert client.post("/api/mask/preview", json={"p": "a.jpg", "mask": {"type": "nonsense"}}).status_code == 422


def test_ai_masks_endpoint_has_fallbacks(client):
    for t in ("ai_subject", "ai_sky", "ai_face", "ai_background"):
        r = client.post("/api/mask/ai", json={"p": "a.jpg", "type": t, "long_edge": 320})
        assert r.status_code == 200, (t, r.text)
        j = r.json()
        assert 0.0 <= j["coverage"] <= 1.0 and j["spec"]["type"] == t and isinstance(j["notes"], list)
    # a masked AI entry renders (the cached mask is reused: second render is fast and identical)
    stack = get_image(client)["stack"] + [{"op": "exposure", "id": "mask:ai1:exposure", "params": {"ev": 1.0}, "mask": {"type": "ai_sky"}}]
    r1 = client.post("/api/render", json={"p": "a.jpg", "stack": stack, "long_edge": 320})
    r2 = client.post("/api/render", json={"p": "a.jpg", "stack": stack, "long_edge": 320})
    assert r1.content == r2.content


def test_wb_pick(client):
    r = client.post("/api/wb/pick", json={"p": "a.jpg", "x": 0.5, "y": 0.1})
    assert r.status_code == 200
    j = r.json()
    assert 2000 <= j["temperature"] <= 12000 and -150 <= j["tint"] <= 150
    # picking the sky (blue) must ask for a warmer-than-neutral correction, i.e. a lower assumed illuminant temperature is NOT expected
    stack = get_image(client)["stack"]
    base = decode(client.post("/api/render", json={"p": "a.jpg", "stack": stack, "long_edge": 200}))
    fixed = decode(client.post("/api/render", json={"p": "a.jpg", "stack": with_param(stack, "white_balance", temperature=j["temperature"], tint=j["tint"]), "long_edge": 200}))
    top = (slice(0, 30), slice(None))
    # the picked area gets closer to neutral
    assert np.ptp(fixed[top].mean((0, 1))) <= np.ptp(base[top].mean((0, 1))) + 1


# =============================================================================== history / versions / snapshots
def test_history_undo_redo(client):
    st = get_image(client)["stack"]
    s1 = with_param(st, "exposure", ev=0.3)
    s2 = with_param(s1, "contrast", contrast=0.2)
    s3 = with_param(s2, "vibrance", amount=20)
    put(client, "a.jpg", s1, label="Exposure")
    put(client, "a.jpg", s2, label="Contrast")
    last = put(client, "a.jpg", s3, label="Vibrance")
    assert [e["label"] for e in last["history"]["entries"]] == ["Open", "Exposure", "Contrast", "Vibrance"]
    u = client.post("/api/image/undo", json={"p": "a.jpg"}).json()
    assert u["changed"] and not any(e["op"] == "vibrance" for e in u["stack"]) and u["history"]["can_redo"]
    on_disk = sidecar.get_stack(sidecar.load(client.root / "a.jpg"))
    assert not any(e["op"] == "vibrance" for e in on_disk)                # undo is written to the sidecar
    u = client.post("/api/image/undo", json={"p": "a.jpg"}).json()
    assert not any(e["op"] == "contrast" and e["params"].get("contrast") for e in u["stack"])
    r = client.post("/api/image/redo", json={"p": "a.jpg"}).json()
    assert any(e["op"] == "contrast" and e["params"]["contrast"] == 0.2 for e in r["stack"])
    first = client.post("/api/image/history/goto", json={"p": "a.jpg", "id": last["history"]["entries"][0]["id"]}).json()
    assert not any(e["op"] == "exposure" and e["params"].get("ev") for e in first["stack"]) and not first["history"]["can_undo"]
    assert client.post("/api/image/undo", json={"p": "a.jpg"}).json()["changed"] is False
    # a new edit after undo drops the redo branch; same-label drags coalesce into one history entry
    put(client, "a.jpg", with_param(st, "exposure", ev=0.5), label="Exposure")
    out = put(client, "a.jpg", with_param(st, "exposure", ev=0.6), label="Exposure")
    assert [e["label"] for e in out["history"]["entries"]].count("Exposure") == 1 and not out["history"]["can_redo"]
    assert client.get("/api/image/history", params={"p": "a.jpg"}).json()["index"] == out["history"]["index"]


def test_versions_snapshots_reset(client):
    st = get_image(client)["stack"]
    put(client, "a.jpg", with_param(st, "exposure", ev=0.4))
    v = client.post("/api/image/versions", json={"p": "a.jpg", "action": "create", "name": "bw"}).json()
    assert v["version"] == "bw" and {x["name"] for x in v["versions"]} == {"default", "bw"}
    put(client, "a.jpg", with_param(v["stack"], "monochrome", amount=1.0))
    doc = sidecar.load(client.root / "a.jpg")
    assert doc["active"] == "bw" and any(e["op"] == "monochrome" for e in sidecar.get_stack(doc, "bw"))
    assert not any(e["op"] == "monochrome" for e in sidecar.get_stack(doc, "default"))      # virtual copies are independent
    sw = client.post("/api/image/versions", json={"p": "a.jpg", "action": "switch", "name": "default"}).json()
    assert sw["version"] == "default" and not any(e["op"] == "monochrome" for e in sw["stack"])
    rn = client.post("/api/image/versions", json={"p": "a.jpg", "action": "rename", "name": "bw", "new_name": "mono"}).json()
    assert "mono" in {x["name"] for x in rn["versions"]}
    assert client.post("/api/image/versions", json={"p": "a.jpg", "action": "delete", "name": "default"}).status_code == 200
    assert client.post("/api/image/versions", json={"p": "a.jpg", "action": "delete", "name": "mono"}).status_code == 422   # last one
    # snapshots
    snap = client.post("/api/image/snapshots", json={"p": "a.jpg", "action": "create", "name": "before-play"}).json()
    assert any(s["name"] == "before-play" for s in snap["snapshots"])
    cur = get_image(client)
    put(client, "a.jpg", with_param(cur["stack"], "exposure", ev=-1.0))
    rest = client.post("/api/image/snapshots", json={"p": "a.jpg", "action": "restore", "name": "before-play"}).json()
    assert next(e for e in rest["stack"] if e["op"] == "exposure")["params"]["ev"] == next(e for e in cur["stack"] if e["op"] == "exposure")["params"]["ev"]
    assert client.post("/api/image/snapshots", json={"p": "a.jpg", "action": "delete", "name": "before-play"}).status_code == 200
    assert client.post("/api/image/snapshots", json={"p": "a.jpg", "action": "restore", "name": "gone"}).status_code == 422
    # reset keeps an auto snapshot and empties the stack (UI then shows resolved defaults)
    rs = client.post("/api/image/reset", json={"p": "a.jpg"}).json()
    assert rs["materialised"] is False and any(s["name"] == "auto:before-reset" for s in rs["snapshots"])


def test_copy_paste_sync(client):
    st = get_image(client)["stack"]
    put(client, "a.jpg", with_param(with_param(st, "exposure", ev=0.9), "vibrance", amount=33))
    c = client.post("/api/image/copy", json={"p": "a.jpg", "ops": ["exposure", "vibrance"]}).json()
    assert set(c["ops"]) == {"exposure", "vibrance"}
    r = client.post("/api/image/paste", json={"paths": ["b.jpg"], "mode": "merge"}).json()
    assert r["pasted"] == ["b.jpg"]
    b = sidecar.get_stack(sidecar.load(client.root / "b.jpg"))
    assert next(e for e in b if e["op"] == "exposure")["params"]["ev"] == 0.9
    s = client.post("/api/image/sync", json={"src": "a.jpg", "paths": ["trip/c.jpg", "raw1.npz"], "ops": ["vibrance"], "mode": "merge"}).json()
    assert len(s["synced"]) == 2
    assert next(e for e in sidecar.get_stack(sidecar.load(client.root / "trip" / "c.jpg")) if e["op"] == "vibrance")["params"]["amount"] == 33
    assert client.post("/api/image/paste", json={"paths": ["b.jpg"], "mode": "bogus"}).status_code == 422


# =============================================================================== presets
def test_presets_list_apply_and_user_presets(client):
    for kind in ("mode", "art", "picture_style", "export"):
        lst = client.get("/api/presets", params={"kind": kind}).json()["presets"]
        assert lst and all("name" in p and "user" in p and "path" not in p for p in lst), kind
    modes = {p["name"] for p in client.get("/api/presets", params={"kind": "mode"}).json()["presets"]}
    assert {"portrait", "landscape", "night"} <= modes
    st = get_image(client)["stack"]
    r = client.post("/api/presets/apply", json={"p": "a.jpg", "kind": "mode", "name": "landscape"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["set_mode"] and j["mode"] and len(j["stack"]) >= len(st)
    r = client.post("/api/presets/apply", json={"p": "a.jpg", "kind": "art", "name": "cyanotype", "stack": st})
    j = r.json()
    assert j["art"] == "cyanotype" and any(e["op"] == "cyanotype" for e in j["stack"]) and not j["set_mode"]
    rm = client.post("/api/presets/remove", json={"kind": "art", "name": "cyanotype", "stack": j["stack"]}).json()
    assert not any(e["op"] == "cyanotype" for e in rm["stack"])
    ps = client.post("/api/presets/apply", json={"p": "a.jpg", "kind": "picture_style", "name": "landscape", "stack": st})
    assert ps.status_code in (200, 422)
    # applying + saving through the sidecar, then rendering shows the look
    base = decode(client.post("/api/render", json={"p": "a.jpg", "stack": st, "long_edge": 200}))
    look = decode(client.post("/api/render", json={"p": "a.jpg", "stack": j["stack"], "long_edge": 200}))
    assert np.abs(look - base).mean() > 5
    put(client, "a.jpg", j["stack"], mode=None, art="cyanotype")
    assert sidecar.load(client.root / "a.jpg")["versions"]["default"]["art"] == "cyanotype"
    # hover thumbnail
    t = client.get("/api/preset_thumb", params={"p": "a.jpg", "kind": "art", "name": "cyanotype", "size": 128})
    assert max(decode(t).shape[:2]) == 128
    # user presets
    cur = with_param(st, "vibrance", amount=25) + [{"op": "crop", "params": {"rect": [0, 0, 0.5, 0.5]}}]
    sv = client.post("/api/presets/user", json={"name": "My Warm Look", "stack": cur}).json()
    assert sv["name"] == "my_warm_look" and sv["n_ops"] >= 1
    user = client.get("/api/presets", params={"kind": "user"}).json()["presets"]
    assert [p["name"] for p in user] == ["my_warm_look"] and user[0]["user"] is True
    full = client.get("/api/presets/art/my_warm_look").json()
    assert not any(e["op"] == "crop" for e in full["stack"])                 # scope=look drops geometry
    assert any(e["op"] == "vibrance" for e in full["stack"])
    assert client.post("/api/presets/apply", json={"p": "b.jpg", "kind": "art", "name": "my_warm_look"}).status_code == 200
    assert client.delete("/api/presets/user/art/my_warm_look").status_code == 200
    assert client.get("/api/presets", params={"kind": "user"}).json()["presets"] == []
    assert client.post("/api/presets/apply", json={"p": "a.jpg", "kind": "art", "name": "no_such"}).status_code == 422
    assert client.post("/api/presets/user", json={"name": "   ", "stack": cur}).status_code == 422


def test_mode_autodetect(client):
    r = client.post("/api/mode/detect", json={"p": "a.jpg"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert "confidence" in j and "reasons" in j
    ap = client.post("/api/presets/apply", json={"p": "a.jpg", "kind": "mode", "name": "auto"})
    assert ap.status_code == 200 and ap.json()["stack"]


# =============================================================================== export
def test_export_job_and_download(client):
    st = get_image(client)["stack"]
    put(client, "a.jpg", with_param(st, "exposure", ev=0.5))
    r = client.post("/api/export", json={"paths": ["a.jpg", "trip/c.jpg"], "preset": "email_small", "overrides": {"long_edge": 240, "quality": 70}})
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    job = {}
    for _ in range(200):
        job = client.get(f"/api/jobs/{jid}").json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["state"] == "done", job
    assert job["done"] == 2 and job["results"]["ok"] == 2
    outs = [f["out"] for f in job["results"]["files"]]
    assert all(o.startswith("exports/") for o in outs)
    for o in outs:
        full = client.root / o
        assert full.is_file()
        assert max(cv2.imread(str(full)).shape[:2]) <= 240
        dl = client.get("/api/file", params={"p": o})
        assert dl.status_code == 200 and dl.content[:2] == b"\xff\xd8"
    # our own export outputs are not part of the library listing
    assert not [i for i in client.get("/api/library/images").json()["items"] if i["p"].startswith("exports/")]
    # unsaved stack override for a single image
    r = client.post("/api/export", json={"paths": ["b.jpg"], "preset": "web", "overrides": {"long_edge": 200}, "stack": with_param(st, "exposure", ev=2.0), "out_dir": "exports/live"})
    jid = r.json()["id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{jid}").json()
        if job["state"] != "running" and job["state"] != "queued":
            break
        time.sleep(0.1)
    assert job["state"] == "done" and (client.root / "exports" / "live").is_dir()
    # validation
    assert client.post("/api/export", json={"paths": ["a.jpg"], "overrides": {"output_dir": "/etc"}}).status_code == 422
    assert client.post("/api/export", json={"paths": ["a.jpg", "b.jpg"], "stack": st}).status_code == 422
    assert client.post("/api/export", json={"paths": []}).status_code == 422
    assert client.get("/api/jobs/nope").status_code == 404


# =============================================================================== culling
def test_cull_job(client):
    r = client.post("/api/cull/run", json={"paths": ["a.jpg", "b.jpg", "trip/c.jpg"]})
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    job = {}
    for _ in range(300):
        job = client.get(f"/api/jobs/{jid}").json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["state"] == "done", job
    res = job["results"]
    assert len(res) == 3 and all(x["flag"] in ("pick", "maybe", "reject") and "reasons" in x for x in res)
    assert client.get("/api/cull/info", params={"p": "a.jpg"}).status_code == 200


# =============================================================================== security
def test_path_traversal_and_confinement(client, tmp_path):
    secret = tmp_path / "secret.jpg"
    shutil.copy(client.root / "a.jpg", secret)
    for bad in ("../secret.jpg", "trip/../../secret.jpg", "/etc/passwd", str(secret), "..\\secret.jpg", "a.jpg\x00.png", "%2e%2e/secret.jpg"):
        for url, kw in (("/api/image", {}), ("/api/thumb", {}), ("/api/render", {}), ("/api/image/item", {})):
            r = client.get(url, params={"p": bad, **kw})
            assert r.status_code in (400, 403, 404, 422), (url, bad, r.status_code)
            assert r.status_code != 200
    assert client.post("/api/render", json={"p": "../secret.jpg"}).status_code in (403, 404)
    assert client.put("/api/image/stack", json={"p": "../secret.jpg", "stack": []}).status_code in (403, 404)
    assert client.post("/api/image/meta", json={"paths": ["../secret.jpg"], "rating": 1}).status_code in (403, 404)
    assert not (tmp_path / "secret.jpg.ps.json").exists()
    assert client.get("/api/library/images", params={"folder": "../"}).status_code in (403, 404)
    assert client.post("/api/export", json={"paths": ["a.jpg"], "out_dir": "../elsewhere"}).status_code in (403, 404)
    assert not (tmp_path / "elsewhere").exists()
    # symlink pointing outside the root
    (client.root / "link.jpg").symlink_to(secret)
    assert client.get("/api/image", params={"p": "link.jpg"}).status_code == 403
    # only the export folder is downloadable
    assert client.get("/api/file", params={"p": "a.jpg"}).status_code == 403
    assert client.get("/api/file", params={"p": "../secret.jpg"}).status_code == 403
    # non-image files are refused
    (client.root / "notes.txt").write_text("hi")
    assert client.get("/api/image", params={"p": "notes.txt"}).status_code in (403, 422)
    assert client.get("/static/../../../etc/passwd").status_code in (404, 400)


def test_host_and_origin_checks(client):
    assert client.get("/api/info", headers={"Host": "evil.example.com"}).status_code == 403          # DNS rebinding
    assert client.get("/api/info", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    assert client.get("/api/info", headers={"Host": "localhost:9000"}).status_code == 200
    r = client.post("/api/image/meta", json={"paths": ["a.jpg"], "rating": 2}, headers={"Origin": "http://evil.example.com", "Host": "127.0.0.1:8765"})
    assert r.status_code == 403                                                                      # cross-site request
    r = client.post("/api/image/meta", json={"paths": ["a.jpg"], "rating": 2}, headers={"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"})
    assert r.status_code == 200


def test_server_binds_loopback_only():
    import inspect

    from photostudio.web import server

    src = inspect.getsource(server.serve)
    assert 'host="127.0.0.1"' in src and "0.0.0.0" not in inspect.getsource(server)
    assert server._host_ok("127.0.0.1:8765") and server._host_ok("[::1]:8765") and not server._host_ok("192.168.1.5:8765")


# =============================================================================== frontend files
def test_static_files_served_and_local_only(client):
    html = client.get("/").text
    assert "<html" in html.lower()
    refs = re.findall(r'(?:src|href)=["\']([^"\']+)["\']', html)
    assert refs, "index.html references no assets"
    for ref in refs:
        assert not re.match(r"^(https?:)?//", ref), f"external asset in index.html: {ref}"
        assert ref.startswith("/static/") or ref.startswith("#") or ref.startswith("data:"), ref
        if ref.startswith("/static/"):
            assert client.get(ref).status_code == 200, ref
    files = [f for f in STATIC.rglob("*") if f.is_file()]
    assert any(f.suffix == ".js" for f in files) and any(f.suffix == ".css" for f in files)
    for f in files:
        rel = "/static/" + f.relative_to(STATIC).as_posix()
        assert client.get(rel).status_code == 200, rel
        if f.suffix in (".js", ".css", ".html"):
            text = f.read_text(encoding="utf-8")
            assert not re.search(r"""(?:https?:)?//(?:cdn|fonts|unpkg|ajax|cdnjs|code)\.""", text), f"CDN reference in {rel}"
            assert "fonts.googleapis" not in text
            assert not re.search(r"""@import\s+url\(\s*['"]?https?:""", text)


def _js_modules():
    return sorted((STATIC / "js").rglob("*.js"))


def test_js_syntax_and_imports_resolve():
    mods = _js_modules()
    assert mods, "no JS modules"
    names = {m.resolve() for m in mods}
    for m in mods:
        text = m.read_text(encoding="utf-8")
        for spec in re.findall(r"""(?:import|export)[^'"`;]*?from\s*['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)""", text):
            s = spec[0] or spec[1]
            assert s.startswith("."), f"{m.name}: non-relative import {s}"
            assert (m.parent / s).resolve() in names, f"{m.name}: import {s} does not resolve"
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available: JS syntax not checked")
    for m in mods:
        r = subprocess.run([node, "--check", str(m)], capture_output=True, text=True)
        assert r.returncode == 0, f"{m.name}: {r.stderr[:500]}"


def test_serve_command_registered():
    from photostudio import cli

    parser, warnings = cli.build_parser()
    assert not [w for w in warnings if "serve" in w]
    args = parser.parse_args(["serve", "/tmp/x", "--port", "9001", "--open"])
    assert args.port == 9001 and args.open and args.root_dir == "/tmp/x" and callable(args.func)
