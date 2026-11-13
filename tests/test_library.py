"""Catalog / tags / albums / gallery / library CLI on synthetic libraries (no exiftool, no ML models needed)."""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

from photostudio import testing
from photostudio.catalog import Catalog


def _exif(i: int, day: str = "2026:06:14", base_h: int = 10, step_s: int = 20, model: str = "Canon EOS R50", iso: int = 200,
          focal: float = 35.0):
    t = datetime(2026, 6, 14, base_h, 0, 0) + timedelta(seconds=step_s * i)
    return {"Make": "Canon", "Model": model, "DateTimeOriginal": t.strftime("%Y:%m:%d %H:%M:%S"), "FocalLength": focal,
            "FNumber": 5.6, "ISOSpeedRatings": iso}


def make_library(root: Path, n: int = 6) -> Path:
    """n JPEGs (IMG_0000..), IMG_0000 also has a CR3 (garbage bytes: the catalog must never write into it)."""
    root.mkdir(parents=True, exist_ok=True)
    scene = testing.make_scene(320, 213, seed=5)
    for i in range(n):
        img = np.roll(scene, 11 * i, axis=1)
        testing.write_jpeg(root / f"IMG_{i:04d}.JPG", img, exif=_exif(i, iso=200 if i < 3 else 1600))
    (root / "IMG_0000.CR3").write_bytes(os.urandom(4096))
    return root


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture()
def library(tmp_path):
    return make_library(tmp_path / "photos")


@pytest.fixture()
def cat(library):
    c = Catalog(library)
    c.scan()
    yield c
    c.close()


# ================================================================================================ scan / pairing
def test_scan_pairs_raw_and_jpeg(library):
    with Catalog(library) as c:
        res = c.scan()
        assert res["added"] == 6 and res["found"] == 6
        assert c.count() == 6                       # CR3 + JPG = ONE logical image
        row = c.get("IMG_0000.CR3")
        assert row["raw_path"].endswith("IMG_0000.CR3") and row["jpg_path"].endswith("IMG_0000.JPG")
        assert c.find("IMG_0000.JPG") == c.find("IMG_0000.CR3") == row["id"]
        assert row["camera"] and "R50" in row["camera"]
        assert row["taken_at"].startswith("2026-06-14T10:00")


def test_scan_is_incremental_and_detects_changes(library):
    with Catalog(library) as c:
        c.scan()
        again = c.scan()
        assert again["added"] == 0 and again["unchanged"] == 6 and again["updated"] == 0
        testing.write_jpeg(library / "IMG_9999.JPG", testing.make_scene(320, 213, seed=9), exif=_exif(9))
        assert c.scan()["added"] == 1
        (library / "IMG_9999.JPG").unlink()
        res = c.scan()
        assert res["removed"] == 1 and c.count() == 6


def test_scan_never_writes_into_raw_files(library):
    before = sha(library / "IMG_0000.CR3")
    with Catalog(library) as c:
        c.scan()
        i = c.find("IMG_0000.CR3")
        c.set_rating(i, 5)
        c.add_tags(i, ["people|noa"])
        c.set_flag(i, "pick")
        c.set_label(i, "red")
    assert sha(library / "IMG_0000.CR3") == before


# ================================================================================================ truth = sidecar + xmp
def test_rating_written_to_sidecar_and_xmp_and_db(cat, library):
    i = cat.find("IMG_0001.JPG")
    cat.set_rating(i, 4)
    cat.set_flag(i, "pick")
    cat.set_label(i, "red")
    cat.add_tags(i, ["people|noa", "places|lisbon"])
    sc = json.loads((library / "IMG_0001.JPG.ps.json").read_text())
    assert sc["rating"] == 4 and sc["flag"] == "pick" and sc["label"] == "Red"
    assert "people|noa" in sc["tags"]
    from photostudio.metadata import read_xmp

    x = read_xmp(library / "IMG_0001.JPG.xmp")
    assert x["rating"] == 4 and x["label"] == "Red"
    blob = json.dumps(x)
    assert "noa" in blob and "lisbon" in blob
    row = cat.get(i)
    assert row["rating"] == 4 and row["flag"] == "pick" and row["label"] == "Red"


def test_db_rebuild_from_sidecars_is_equivalent(cat, library):
    ids = {n: cat.find(f"IMG_{n:04d}.JPG") for n in range(6)}
    cat.set_rating([ids[0], ids[1]], 5)
    cat.set_rating(ids[2], 3)
    cat.set_flag(ids[3], "reject")
    cat.set_label(ids[4], "green")
    cat.add_tags([ids[0], ids[5]], ["people|noa"])
    cat.add_tags(ids[1], ["genre|portrait", "mood|calm"])
    cat.set_notes(ids[2], "second shooter frame")
    cat.albums.create("Keepers")
    cat.albums.add("Keepers", [ids[0], ids[1]])

    def snap(c):
        out = {}
        for r in c.images(with_tags=True):
            out[r["primary_path"]] = (r["rating"], r["flag"], r["label"], tuple(sorted(r["tags"])), r.get("notes"))
        return out

    before = snap(cat)
    albums_before = [(a["path"], a["count"]) for a in cat.albums.list()]
    cat.close()
    (library / ".photostudio" / "catalog.sqlite").unlink()
    for ext in ("-wal", "-shm"):
        (library / ".photostudio" / f"catalog.sqlite{ext}").unlink(missing_ok=True)
    with Catalog(library) as c2:
        c2.scan()
        assert snap(c2) == before
        assert [(a["path"], a["count"]) for a in c2.albums.list()] == albums_before


def test_xmp_only_metadata_is_imported_when_sidecar_missing(library):
    from photostudio.metadata import write_xmp

    write_xmp(library / "IMG_0002.JPG", rating=3, label="Blue", keywords=["travel"])
    with Catalog(library) as c:
        c.scan()
        r = c.get("IMG_0002.JPG")
        assert r["rating"] == 3 and r["label"] == "Blue"
        assert "travel" in r["tags"]


def test_external_sidecar_edit_is_picked_up_by_rescan(cat, library):
    i = cat.find("IMG_0003.JPG")
    p = library / "IMG_0003.JPG.ps.json"
    cat.set_rating(i, 1)
    doc = json.loads(p.read_text())
    doc["rating"] = 5
    st = p.stat()
    p.write_text(json.dumps(doc))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    res = cat.scan()
    assert cat.get(i)["rating"] == 5 and res["sidecars_read"] >= 1


# ================================================================================================ queries
def test_filters_sort_and_pagination(cat):
    ids = [cat.find(f"IMG_{n:04d}.JPG") for n in range(6)]
    cat.set_rating(ids[0], 5)
    cat.set_rating(ids[1], 4)
    cat.set_rating(ids[2], 2)
    cat.set_flag(ids[0], "pick")
    cat.set_flag(ids[5], "reject")
    cat.set_label(ids[1], "red")
    cat.add_tags([ids[0], ids[1]], ["people|noa"])
    cat.add_tags(ids[2], ["people|dan"])
    assert {r["id"] for r in cat.images({"rating": 4})} >= {ids[0], ids[1]}
    assert cat.ids("rating>=4") == [ids[0], ids[1]]
    assert cat.ids("flag:pick") == [ids[0]]
    assert cat.ids("flag:reject") == [ids[5]]
    assert cat.ids("label:red") == [ids[1]]
    assert cat.ids("tag:people") == [ids[0], ids[1], ids[2]]                 # hierarchical prefix
    assert cat.ids("tag:people|noa") == [ids[0], ids[1]]
    assert cat.ids("tag:people|noa EXCEPT flag:pick") == [ids[1]]
    assert cat.ids("rating>=4 AND tag:people|noa") == [ids[0], ids[1]]
    assert set(cat.ids("iso>=1000")) == {ids[3], ids[4], ids[5]}
    assert len(cat.ids("camera:R50")) == 6 and cat.ids("camera:Nikon") == []
    assert cat.count("rating>=4") == 2
    rows = cat.images(sort="rating", order="desc", limit=2)
    assert [r["rating"] for r in rows] == [5, 4]
    p1 = cat.images(sort="taken_at", limit=2, offset=0)
    p2 = cat.images(sort="taken_at", limit=2, offset=2)
    assert [r["id"] for r in p1 + p2] == ids[:4]
    # structured filter dict with tags{any,all,none} and date range
    f = {"tags": {"any": ["people|noa", "people|dan"]}, "date_from": "2026-06-14", "date_to": "2026-06-14T23:59:59"}
    assert set(cat.ids(f)) == {ids[0], ids[1], ids[2]}
    assert cat.ids({"tags": {"none": ["people"]}, "rating": 0}) != []
    assert cat.ids("taken:[2026-06-14;2026-06-14]") == ids
    assert cat.ids("taken:2026-07") == []


def test_rule_compiler_rejects_unknown_fields_and_is_injection_safe(cat):
    from photostudio.catalog_sql import RuleError

    with pytest.raises(RuleError):
        cat.ids("nonsense>3")
    with pytest.raises(RuleError):
        cat.images(sort="rating; DROP TABLE images")
    assert cat.ids('tag:"x\'); DROP TABLE images;--"') == []
    assert cat.count() == 6


def test_full_text_search(cat):
    i = cat.find("IMG_0002.JPG")
    cat.set_notes(i, "sunset over the harbour")
    cat.add_tags(cat.find("IMG_0004.JPG"), ["places|portugal|lisbon"])
    cat.set_caption(cat.find("IMG_0005.JPG"), "a child laughing in a park")
    assert [r["id"] for r in cat.search("harbour")] == [i]
    assert [r["id"] for r in cat.search("harb")] == [i]                       # prefix
    assert len(cat.search("lisbon")) == 1
    assert len(cat.search("laughing park")) == 1                              # AND of terms
    assert len(cat.search("IMG_0001")) == 1
    assert cat.search("harbour", filter="rating>=3") == []
    assert len(cat.search("R50")) == 6
    assert cat.search("") == [] or isinstance(cat.search(""), list)
    # the index follows edits
    cat.set_notes(i, "")
    assert cat.search("harbour") == []


# ================================================================================================ tags
def test_hierarchical_tag_tree_operations(cat, library):
    from photostudio import tags as T

    a, b, c3 = (cat.find(f"IMG_{n:04d}.JPG") for n in (0, 1, 2))
    cat.add_tags(a, ["people|noa|child"])
    cat.add_tags(b, ["people|noa"])
    cat.add_tags(c3, ["people|dan"])
    tree = {t["path"]: t for t in cat.list_tags()}
    assert tree["people"]["total"] == 3 and tree["people|noa"]["total"] == 2 and tree["people|noa|child"]["count"] == 1
    assert cat.ids("tag:people|noa") == [a, b]

    T.rename_tag(cat, "people|noa", "Noa")
    assert cat.ids("tag:people|Noa") == [a, b]
    assert "people|Noa|child" in cat.get(a)["tags"]
    assert cat.ids("tag:people|noa") == [a, b]                                # tag search is case-insensitive
    sc = json.loads((library / "IMG_0000.CR3.ps.json").read_text())
    assert "people|Noa|child" in sc["tags"]                                   # rename reached the sidecar

    T.move_tag(cat, "people|dan", "friends")
    assert cat.ids("tag:friends|dan") == [c3]
    T.merge_tags(cat, "friends|dan", "people|Noa")
    assert set(cat.ids("tag:people|Noa")) == {a, b, c3}
    T.delete_tag(cat, "people|Noa|child")
    assert "people|Noa|child" not in cat.get(a)["tags"]
    cat.remove_tags(c3, ["people|Noa"])
    assert cat.ids("tag:people|Noa") == [b]                                   # a only had the deleted child tag
    # XMP: leaf names in dc:subject, full path in lr:hierarchicalSubject
    from photostudio.metadata import read_xmp

    blob = json.dumps(read_xmp(library / "IMG_0001.JPG.xmp"))
    assert "people|Noa" in blob
    # text export/import of the tree
    txt = T.tree_text(cat)
    assert "people" in txt and "Noa" in txt


def test_auto_tag_metadata_tier(cat):
    from photostudio import tags as T

    res = T.auto_tag_metadata(cat)
    assert res["images"] == 6 and res["changed"] == 6
    r = cat.get("IMG_0004.JPG")
    paths = set(r["tags"])
    assert any(t.startswith("camera|") for t in paths)
    assert any(t.startswith("iso|") for t in paths)
    assert any(t.startswith("time|") for t in paths)
    srcs = {d["source"] for d in r["tag_details"]}
    assert "auto:meta" in srcs
    # auto tags are replaced (not accumulated) on re-run, and never clobber user tags
    cat.add_tags(r["id"], ["people|noa"])
    T.auto_tag_metadata(cat)
    r2 = cat.get(r["id"])
    assert "people|noa" in r2["tags"] and len(r2["tags"]) == len(set(r2["tags"]))


def test_zero_shot_math_and_vocab_without_models():
    from photostudio import tags as T

    vocab = T.load_vocab()
    terms = T.vocab_terms(vocab)
    assert len(terms) > 100
    rng = np.random.default_rng(0)
    emb = rng.normal(size=(5, 8)).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    img = emb[2].copy()
    res = T.zero_shot(img, ["a", "b", "c", "d", "e"], emb, ["g1", "g2", "g3", "g4", "g5"])
    assert res[0][0] == "c" and res[0][1] > 0.9 and all(0 < p <= 1 for _, p in res)
    # exclusive groups keep one tag per group; max_tags caps the output
    near = emb[2] + 0.2 * emb[3]
    both = T.zero_shot(near, ["a", "b", "c", "d", "e"], emb, ["x", "x", "y", "y", "x"], exclusive=("y",), min_prob=0.01)
    assert sum(1 for t, _ in both if t in ("c", "d")) == 1
    assert len(T.zero_shot(near, list("abcde"), emb, list("vwxyz"), min_prob=0.0, max_tags=2)) == 2


def test_dbscan_clusters_unit_embeddings():
    from photostudio import tags as T

    rng = np.random.default_rng(1)
    c1 = np.array([1.0, 0, 0, 0])
    c2 = np.array([0, 1.0, 0, 0])
    pts = np.vstack([c1 + 0.05 * rng.normal(size=(8, 4)), c2 + 0.05 * rng.normal(size=(8, 4)), [[0, 0, 1.0, 0]]])
    pts /= np.linalg.norm(pts, axis=1, keepdims=True)
    lab = T.dbscan(pts, eps=0.4, min_samples=3)
    assert len(set(lab[:8])) == 1 and len(set(lab[8:16])) == 1 and lab[0] != lab[8]
    assert lab[16] == -1


def test_event_splitting_by_time_gap_and_gps_merge():
    from photostudio import tags as T

    base = datetime(2026, 6, 14, 9, 0).timestamp()
    items, i = [], 0
    for blk, start_h in enumerate((0, 1.5, 8, 30)):           # 0 + 1.5 h: same place -> merged; +8 h; next day
        for k in range(10):
            items.append({"id": i, "ts": base + start_h * 3600 + k * 60, "lat": 38.72 + 0.0001 * blk, "lon": -9.14})
            i += 1
    evs = T.detect_events(items)
    assert [e["n"] for e in evs] == [20, 10, 10]
    assert evs[0]["start"].startswith("2026-06-14T09:00")
    fixed = T.detect_events(items, gap_hours=1.0)
    assert len(fixed) == 4
    assert T.detect_events([]) == []
    far = [dict(x, lat=51.5 if x["id"] >= 10 else 38.72) for x in items]
    assert len(T.detect_events(far)) == 4                       # same gap but a different city -> not merged


def test_events_applied_create_tags_and_albums(cat):
    from photostudio import tags as T

    evs = T.suggest_events(cat)
    assert len(evs) == 1 and evs[0]["n"] == 6
    res = T.apply_events(cat, evs, names={1: "Harbour walk"})
    assert cat.ids("tag:events|Harbour*walk") or cat.ids('tag:"events|Harbour walk"')
    assert any("Harbour" in a["path"] for a in cat.albums.list())
    assert res


# ================================================================================================ albums
def test_static_and_smart_albums(cat):
    ids = [cat.find(f"IMG_{n:04d}.JPG") for n in range(6)]
    al = cat.albums
    al.create("Trip")
    assert al.add("Trip", ids[:3]) == 3
    assert al.add("Trip", ids[:3]) == 0                                       # idempotent
    assert [r["id"] for r in al.images("Trip")] == ids[:3]
    assert al.remove("Trip", [ids[0]]) == 1 and al.count("Trip") == 2
    al.create("Trip/Day1")                                                   # hierarchy
    al.add("Trip/Day1", [ids[1]])
    assert al.count("Trip", include_children=True) >= 2
    # smart album: rule JSON and DSL, live membership
    cat.set_rating(ids[4], 5)
    s = al.create_smart("Best", {"all": [{"field": "rating", "op": ">=", "value": 4}]})
    assert [r["id"] for r in al.images("Best")] == [ids[4]]
    cat.set_rating(ids[5], 4)
    assert {r["id"] for r in al.images("Best")} == {ids[4], ids[5]}
    s2 = al.create_smart("BestNotReject", "rating>=4 EXCEPT flag:reject")
    cat.set_flag(ids[5], "reject")
    assert [r["id"] for r in al.images("BestNotReject")] == [ids[4]]
    with pytest.raises(Exception):
        al.add("Best", [ids[0]])                                             # smart albums are rule-defined
    assert cat.count("album:Best") == 2 and cat.ids("album:Trip") == [ids[1], ids[2]]
    # tag + smart: nested rule trees (any/all/none)
    cat.add_tags(ids[0], ["x|y"])
    al.create_smart("Complex", {"any": [{"field": "tag", "value": "x|y"},
                                        {"all": [{"field": "rating", "op": ">=", "value": 5}, {"none": [{"field": "flag", "value": "reject"}]}]}]})
    assert set(r["id"] for r in al.images("Complex")) == {ids[0], ids[4]}
    assert s["kind"] == "smart" and s2["id"] != s["id"]
    # persisted, then restored on a brand-new database
    al.persist()
    names = sorted(a["path"] for a in al.list())
    root = cat.root
    cat.close()
    (root / ".photostudio" / "catalog.sqlite").unlink()
    for ext in ("-wal", "-shm"):
        (root / ".photostudio" / f"catalog.sqlite{ext}").unlink(missing_ok=True)
    with Catalog(root) as c2:
        c2.scan()
        assert sorted(a["path"] for a in c2.albums.list()) == names
        assert c2.albums.count("Trip") == 2


def test_album_export_zip_and_contact_sheet(cat, tmp_path):
    from photostudio import albums as A

    ids = [cat.find(f"IMG_{n:04d}.JPG") for n in range(3)]
    cat.albums.create("Share")
    cat.albums.add("Share", ids)
    out = tmp_path / "share_out"
    z = tmp_path / "share.zip"
    res = A.export_album(cat, "Share", out, zip_path=z)
    files = sorted(p.name for p in out.iterdir())
    assert len(files) >= 3 and any(f.startswith("001_") for f in files)
    assert (out / "001_IMG_0000.CR3").exists() or (out / "001_IMG_0000.JPG").exists()
    with zipfile.ZipFile(z) as zf:
        assert len(zf.namelist()) == len(files)
    assert res["errors"] == []
    dry = A.export_album(cat, "Share", tmp_path / "nothing", dry_run=True)
    assert not (tmp_path / "nothing").exists() or not list((tmp_path / "nothing").iterdir())
    assert dry["dry_run"] is True
    sheet = tmp_path / "sheet.pdf"
    A.contact_sheet(cat, "Share", sheet, cols=3)
    assert sheet.exists() and sheet.read_bytes()[:4] == b"%PDF"


# ================================================================================================ thumbs
def test_thumbnails_cached_and_keyed_by_ops_hash(cat, library):
    from photostudio import sidecar

    i = cat.find("IMG_0001.JPG")
    t1 = cat.thumb(i, 160)
    assert t1.exists()
    m = t1.stat().st_mtime_ns
    assert cat.thumb(i, 160) == t1 and t1.stat().st_mtime_ns == m           # cache hit
    primary = library / "IMG_0001.JPG"
    doc = sidecar.load(primary)
    sidecar.set_stack(doc, [{"op": "exposure", "params": {"ev": 1.0}}])
    sidecar.save(primary, doc)
    cat.scan()
    t2 = cat.thumb(i, 160)
    assert t2 != t1 and t2.exists()                                          # different recipe -> different thumb
    assert cat.get(i)["has_edits"]
    stats = cat.thumbs(sizes=(160,))
    assert stats["failed"] == 0
    assert cat.prune_thumbs() >= 0


# ================================================================================================ gallery
def test_static_gallery_html(cat, tmp_path):
    from photostudio.gallery import build_gallery

    ids = [cat.find(f"IMG_{n:04d}.JPG") for n in range(4)]
    cat.albums.create("Web")
    cat.albums.add("Web", ids)
    cat.add_tags(ids[0], ["people|noa"])
    out = tmp_path / "gal"
    rep = build_gallery(cat, "Web", out, title="My <Gallery>", description="Summer & sun")
    html = (out / "index.html").read_text(encoding="utf-8")
    assert rep["images"] == 4 and rep["errors"] == []
    assert "My &lt;Gallery&gt;" in html and "Summer &amp; sun" in html
    assert "{{" not in html and "</script><" not in html.split("const DATA")[0] if "const DATA" in html else True
    assert len(list(out.rglob("*.jpg"))) >= 8                                 # thumb + web size per image
    for ext in ("http://", "https://", "//cdn"):
        assert ext not in html.replace("http://www.w3.org", "")               # no network at view time
    for jpg in out.rglob("*.jpg"):
        assert cv2.imread(str(jpg)) is not None
    # every img src resolves inside the output folder
    import re

    for src in re.findall(r'<img src="([^"]+)"', html):
        assert (out / src).exists(), src
    # the originals are never copied unless asked
    assert not any(p.suffix.lower() == ".cr3" for p in out.rglob("*"))


# ================================================================================================ CLI
def _run(argv, capsys):
    from photostudio import cli

    rc = cli.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_cli_end_to_end(library, capsys):
    base = ["--root", str(library), "--json"]
    rc, out, _ = _run(base + ["scan"], capsys)
    assert rc == 0 and json.loads(out)["total"] == 6
    rc, out, _ = _run(base + ["rate", "4", "IMG_0001.JPG", "IMG_0002.JPG"], capsys)
    assert rc == 0 and json.loads(out)["images"] == 2
    rc, out, _ = _run(base + ["flag", "pick", "--query", "rating>=4"], capsys)
    assert rc == 0
    rc, out, _ = _run(base + ["tag", "add", "people|noa", "IMG_0001.JPG"], capsys)
    assert rc == 0
    rc, out, _ = _run(base + ["ls", "--rating", "4", "--tag", "people"], capsys)
    rows = json.loads(out)
    assert rc == 0 and [r["file"] for r in rows] == ["IMG_0001.JPG"]
    rc, out, _ = _run(base + ["ls", "--count", "--flag", "pick"], capsys)
    assert json.loads(out)["count"] == 2
    rc, out, _ = _run(base + ["album", "create", "A", "IMG_0001.JPG"], capsys)
    assert rc == 0
    rc, out, _ = _run(base + ["album", "smart", "Rated", "--rule", "rating>=4"], capsys)
    assert rc == 0
    rc, out, _ = _run(base + ["album", "show", "Rated"], capsys)
    assert len(json.loads(out)["images"]) == 2
    rc, out, _ = _run(base + ["search", "IMG_0003"], capsys)
    assert len(json.loads(out)) == 1
    rc, out, _ = _run(base + ["tag", "list", "--tree"], capsys)
    assert rc == 0 and "people" in out
    with pytest.raises(SystemExit):
        _run(base + ["rate", "9", "IMG_0001.JPG"], capsys)
    capsys.readouterr()
    rc, out, err = _run(base + ["ls", "--query", "bogus>1"], capsys)
    assert rc == 1 and "error" in json.loads(out)
    rc, out, _ = _run(base + ["catalog", "export", str(library.parent / "dump")], capsys)
    assert rc == 0 and (library.parent / "dump" / "images.json").exists()
    dump = json.loads((library.parent / "dump" / "images.json").read_text())
    assert len(dump["images"] if isinstance(dump, dict) else dump) == 6


def test_catalog_export_then_import_restores_metadata(cat, tmp_path):
    from photostudio import backup

    ids = [cat.find(f"IMG_{n:04d}.JPG") for n in range(3)]
    cat.set_rating(ids[0], 5)
    cat.add_tags(ids[1], ["a|b"])
    cat.albums.create("Keep")
    cat.albums.add("Keep", [ids[2]])
    backup.export_catalog(cat, tmp_path / "dump")
    cat.set_rating(ids[0], 0)
    cat.remove_tags(ids[1], ["a|b"])
    cat.albums.remove("Keep", [ids[2]])
    backup.import_catalog(cat, tmp_path / "dump")
    assert cat.get(ids[0])["rating"] == 5 and "a|b" in cat.get(ids[1])["tags"]
    assert cat.albums.count("Keep") == 1


def test_moved_files_are_relinked_keeping_tags_and_albums(cat, library):
    i = cat.find("IMG_0002.JPG")
    cat.set_rating(i, 4)
    cat.add_tags(i, ["people|noa"])
    cat.albums.create("Keep")
    cat.albums.add("Keep", [i])
    sub = library / "2026" / "June"
    sub.mkdir(parents=True)
    for suffix in ("", ".ps.json", ".xmp"):
        f = library / f"IMG_0002.JPG{suffix}"
        if f.exists():
            f.rename(sub / f.name)
    res = cat.scan()
    assert res["relinked"] == 1 and res["removed"] == 0 and cat.count() == 6
    row = cat.get(i)                                                          # same id: nothing was lost
    assert row["primary_path"] == "2026/June/IMG_0002.JPG" and row["rating"] == 4 and "people|noa" in row["tags"]
    assert [r["id"] for r in cat.albums.images("Keep")] == [i]
