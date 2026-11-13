"""Import (verified dual copy), manifests, bit-rot detection, local mirror, 3-2-1 status, rclone / restic command
construction (no binary needed) and the backup CLI. Everything runs on tiny synthetic files."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from photostudio import backup, backup_tools as bt, importer, testing
from photostudio.catalog import Catalog
from photostudio.util import MissingDependency, file_hash


# ================================================================================================ helpers
def make_card(root: Path, n: int = 4, raw_every: int = 2) -> Path:
    """DCIM/100CANON with n JPEGs (every `raw_every`-th also has a CR3 with random bytes, like a Canon R50 card)."""
    d = root / "DCIM" / "100CANON"
    d.mkdir(parents=True)
    sc = testing.make_scene(240, 160, seed=2)
    for i in range(n):
        t = datetime(2026, 6, 14, 10, 0, 0) + timedelta(seconds=30 * i)
        testing.write_jpeg(d / f"IMG_{i:04d}.JPG", np.roll(sc, 9 * i, axis=1),
                           exif={"Make": "Canon", "Model": "Canon EOS R50", "DateTimeOriginal": t.strftime("%Y:%m:%d %H:%M:%S")})
        if i % raw_every == 0:
            (d / f"IMG_{i:04d}.CR3").write_bytes(os.urandom(3000 + i))
    return root


def tree_files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def flip_byte_keep_stat(path: Path, offset: int = 100) -> None:
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[offset] ^= 0xFF
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size


@pytest.fixture()
def card(tmp_path):
    return make_card(tmp_path / "card")


@pytest.fixture()
def imported(tmp_path, card):
    lib, lib2 = tmp_path / "lib", tmp_path / "lib2"
    rep = importer.import_media(card, lib, dest2=lib2, event="Beach Day")
    return lib, lib2, rep


# ================================================================================================ importer
def test_import_dual_copy_with_manifest_verification(card, imported):
    lib, lib2, rep = imported
    assert rep["errors"] == [] and rep["safe_to_format"] is True
    assert rep["counts"]["found"] == 6 and rep["counts"]["copied"] == 6 and rep["counts"]["items"] == 4   # 4 shots, CR3+JPG paired
    day = lib / "2026" / "2026-06-14_Beach-Day"
    assert day.is_dir() and (lib2 / "2026" / "2026-06-14_Beach-Day").is_dir()
    for root in (lib, lib2):
        assert tree_files(root / "2026") >= {f"2026-06-14_Beach-Day/{n}" for n in
                                              ("IMG_0000.CR3", "IMG_0000.JPG", "IMG_0001.JPG", "IMG_0002.CR3", "MANIFEST.b3")}
        m = root / "2026" / "2026-06-14_Beach-Day" / "MANIFEST.b3"
        v = backup.verify_b3(m)
        assert v["clean"] and len(v["ok"]) == 6
        for line in m.read_text().splitlines():                                 # b3sum-compatible lines
            assert re.fullmatch(r"[0-9a-f]{64}  \S+", line), line
    # byte-identical to the card
    for src in (card / "DCIM" / "100CANON").iterdir():
        for root in (lib, lib2):
            assert (root / "2026" / "2026-06-14_Beach-Day" / src.name).read_bytes() == src.read_bytes()
    # RAW + JPEG share a stem; sidecar stubs exist for each shot's primary only; the card is untouched
    assert (day / "IMG_0000.CR3.ps.json").exists() and not (day / "IMG_0000.JPG.ps.json").exists()
    assert len(list((card / "DCIM" / "100CANON").iterdir())) == 6
    assert not list(day.glob(".*.part")) and not list(day.glob("*.part"))
    assert (lib / ".photostudio" / "imports" / f"{rep['session']}.json").exists()
    assert rep["note"].startswith("All files verified")


def test_import_skips_duplicates_by_content_and_never_overwrites(tmp_path, card, imported):
    lib, lib2, rep = imported
    again = importer.import_media(card, lib, dest2=lib2, event="Beach Day")
    assert again["counts"]["copied"] == 0 and again["counts"]["duplicates"] == 6 and again["safe_to_format"]
    # the same content under another name is still a duplicate
    c2 = tmp_path / "card2" / "DCIM" / "101CANON"
    c2.mkdir(parents=True)
    (c2 / "IMG_7777.JPG").write_bytes((card / "DCIM" / "100CANON" / "IMG_0001.JPG").read_bytes())
    dup = importer.import_media(tmp_path / "card2", lib, dest2=lib2, event="Beach Day")
    assert dup["counts"]["duplicates"] == 1 and dup["counts"]["copied"] == 0
    # a different file with a colliding name gets a new name; the existing original is untouched
    c3 = tmp_path / "card3" / "DCIM" / "100CANON"
    c3.mkdir(parents=True)
    other = testing.write_jpeg(c3 / "IMG_0001.JPG", testing.make_scene(200, 120, seed=9),
                               exif={"DateTimeOriginal": "2026:06:14 10:00:30"})
    before = (lib / "2026" / "2026-06-14_Beach-Day" / "IMG_0001.JPG").read_bytes()
    new = importer.import_media(tmp_path / "card3", lib, dest2=lib2, event="Beach Day")
    assert new["counts"]["copied"] == 1 and new["errors"] == []
    assert (lib / "2026" / "2026-06-14_Beach-Day" / "IMG_0001.JPG").read_bytes() == before
    names = [Path(f["dest"]).name for f in new["files"]]
    assert names[0] != "IMG_0001.JPG" and (lib / "2026" / "2026-06-14_Beach-Day" / names[0]).read_bytes() == other.read_bytes()


def test_import_dry_run_writes_nothing(tmp_path, card):
    lib = tmp_path / "lib"
    rep = importer.import_media(card, lib, dry_run=True)
    assert rep["counts"]["would_copy"] == 6 and rep["safe_to_format"] is False and "Dry run" in rep["note"]
    assert not lib.exists() or not tree_files(lib)


def test_delete_after_verify_deletes_only_verified_files(tmp_path, card):
    lib, lib2 = tmp_path / "lib", tmp_path / "lib2"
    rep = importer.import_media(card, lib, dest2=lib2, delete_after_verify=True)
    assert rep["safe_to_format"] and len(rep["deleted_from_source"]) == 6
    assert not list((card / "DCIM" / "100CANON").iterdir())
    assert sum(1 for p in (lib / "2026").rglob("*") if p.suffix in (".JPG", ".CR3")) == 6


def test_failed_second_copy_keeps_card_files_and_is_not_safe(tmp_path):
    card = make_card(tmp_path / "card", n=2)
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    rep = importer.import_media(card, tmp_path / "lib", dest2=blocker / "lib2", delete_after_verify=True)
    assert rep["errors"] and rep["safe_to_format"] is False
    assert rep["deleted_from_source"] == []
    assert len(list((card / "DCIM" / "100CANON").iterdir())) == 3                # 2 JPG + 1 CR3, all still there


def test_second_destination_is_filled_for_already_imported_files(tmp_path, card):
    lib, lib2 = tmp_path / "lib", tmp_path / "lib2"
    importer.import_media(card, lib)
    rep = importer.import_media(card, lib, dest2=lib2, delete_after_verify=False)
    assert rep["counts"]["copied"] == 0 and rep["safe_to_format"]
    assert all(f.get("restored_to_dest2") for f in rep["files"])
    assert tree_files(lib2) >= {f for f in tree_files(lib) if f.endswith((".JPG", ".CR3", "MANIFEST.b3"))}


def test_import_indexes_into_catalog_and_tracks_last_import(tmp_path, card):
    lib = tmp_path / "lib"
    with Catalog(lib) as cat:
        rep = importer.import_media(card, lib, catalog=cat)
        assert cat.count() == 4 and rep["catalog_total"] == 4
        assert len(cat.last_import_ids()) == 4
        # second import of identical data: duplicates are recognised from the catalog too and last-import is not reset to junk
        rep2 = importer.import_media(card, lib, catalog=cat)
        assert rep2["counts"]["copied"] == 0 and cat.count() == 4


def test_detect_dcim_and_card_enumeration(card, tmp_path):
    assert importer.detect_dcim(card).name == "DCIM"
    assert importer.detect_dcim(card / "DCIM" / "100CANON").name in ("100CANON", "DCIM")
    items = importer.enumerate_items(importer.detect_dcim(card))
    assert [len(i.files) for i in items] == [2, 1, 2, 1]
    assert items[0].primary.suffix.upper() == ".CR3"
    with pytest.raises(FileNotFoundError):
        importer.detect_dcim(tmp_path / "does-not-exist")


# ================================================================================================ manifests / bit rot
def test_b3_manifest_roundtrip_and_corruption(imported):
    lib, _, _ = imported
    folder = lib / "2026" / "2026-06-14_Beach-Day"
    res = backup.create_b3_manifest(folder)
    assert res["files"] == 6
    assert backup.verify_b3(folder / "MANIFEST.b3")["clean"]
    flip_byte_keep_stat(folder / "IMG_0001.JPG")
    v = backup.verify_b3(folder / "MANIFEST.b3")
    assert v["corrupt"] == ["IMG_0001.JPG"] and not v["clean"]
    (folder / "IMG_0003.JPG").unlink()
    assert backup.verify_b3(folder / "MANIFEST.b3")["missing"] == ["IMG_0003.JPG"]


def test_bit_rot_detection_distinguishes_corruption_from_edits(imported):
    lib, _, _ = imported
    up = backup.manifest_update(lib)
    assert up["added"] >= 6 and up["unchanged"] == 0
    assert backup.manifest_update(lib)["unchanged"] == up["files"]               # no re-hash when size+mtime are unchanged
    clean = backup.manifest_verify(lib)
    assert clean["corrupt"] == [] and clean["clean"] and clean["checked"] > 0
    day = lib / "2026" / "2026-06-14_Beach-Day"
    flip_byte_keep_stat(day / "IMG_0002.CR3", 500)                               # silent damage: same size AND mtime
    v = backup.manifest_verify(lib)
    assert v["corrupt"] == ["2026/2026-06-14_Beach-Day/IMG_0002.CR3"] and not v["clean"]
    # a legitimate change (new mtime) is "modified", never "corrupt"
    p = day / "IMG_0001.JPG"
    p.write_bytes(p.read_bytes() + b"x")
    # a vanished file is "missing", a new one is "new"
    (day / "IMG_0003.JPG").unlink()
    (day / "IMG_9999.JPG").write_bytes(b"new")
    v = backup.manifest_verify(lib)
    assert "2026/2026-06-14_Beach-Day/IMG_0001.JPG" in v["modified"]
    assert "2026/2026-06-14_Beach-Day/IMG_0001.JPG" not in v["corrupt"]
    assert "2026/2026-06-14_Beach-Day/IMG_0003.JPG" in v["missing"]
    assert "2026/2026-06-14_Beach-Day/IMG_9999.JPG" in v["new"]
    st = backup.status(lib)
    assert st["last_verify"]["corrupt"] == 1 and any("corrupt" in w for w in st["warnings"]) and st["ok"] is False
    # sampling still finds modified/missing and hashes only a share of the rest
    s = backup.manifest_verify(lib, sample=0.3, seed=1)
    assert s["checked"] < clean["checked"] + 1 and s["missing"]


# ================================================================================================ local mirror
def _lib_with_metadata(tmp_path) -> Path:
    card = make_card(tmp_path / "card")
    lib = tmp_path / "lib"
    with Catalog(lib) as cat:
        importer.import_media(card, lib, catalog=cat)
        cat.set_rating(cat.ids()[0], 5)
        cat.add_tags(cat.ids()[1], ["people|noa"])
        cat.albums.create("Best")
    return lib


def test_local_mirror_is_verified_incremental_and_never_overwrites_originals(tmp_path):
    lib = _lib_with_metadata(tmp_path)
    dest = tmp_path / "hdd" / "Photos"
    dry = backup.mirror(lib, dest, dry_run=True)
    assert dry["copied"] > 0 and not dest.exists()
    r = backup.mirror(lib, dest)
    assert r["ok"] and r["copied"] >= 10 and r["errors"] == []
    mirrored = tree_files(dest)
    assert any(f.endswith(".CR3") for f in mirrored) and any(f.endswith(".ps.json") for f in mirrored)
    assert any(f.endswith(".xmp") for f in mirrored)
    assert not any("thumbs" in f or f.endswith(".part") for f in mirrored)
    assert ".photostudio/albums.json" in mirrored
    again = backup.mirror(lib, dest)
    assert again["copied"] == 0 and again["updated"] == 0 and again["skipped"] == r["copied"]
    # a changed sidecar is updated and the old version is kept in the trash
    sc = next(lib.rglob("*.ps.json"))
    doc = json.loads(sc.read_text())
    doc["rating"] = 2
    sc.write_text(json.dumps(doc))
    os.utime(sc, (sc.stat().st_atime, sc.stat().st_mtime + 10))
    up = backup.mirror(lib, dest)
    assert up["updated"] == 1 and up["trashed"] == 1
    trashed = [p for p in (tmp_path / "hdd").rglob("*.ps.json") if "_trash" in p.parts]
    assert len(trashed) == 1 and json.loads(trashed[0].read_text())["rating"] != 2
    # an ORIGINAL that differs at the destination is a conflict: reported, never overwritten
    raw = next(lib.rglob("*.CR3"))
    mirrored_raw = dest / raw.relative_to(lib)
    mirrored_raw.write_bytes(b"different")
    c = backup.mirror(lib, dest)
    assert c["conflicts"] == [raw.relative_to(lib).as_posix()] and c["ok"] is False
    assert mirrored_raw.read_bytes() == b"different"
    # sync mode: files that vanished from the library are moved to the trash, not deleted
    gone = next(lib.rglob("*.JPG"))
    rel = gone.relative_to(lib)
    gone.unlink()
    s = backup.mirror(lib, dest, mode="sync")
    assert s["trashed"] >= 1 and not (dest / rel).exists()
    assert any(p.name == gone.name for p in (tmp_path / "hdd").rglob("*") if "_trash" in p.parts)


def test_restore_verify_proves_the_backup_is_restorable(tmp_path):
    lib = _lib_with_metadata(tmp_path)
    dest = tmp_path / "hdd" / "Photos"
    backup.manifest_update(lib)
    backup.mirror(lib, dest)
    ok = backup.restore_verify(lib, dest, sample=50, seed=0)
    assert ok["ok"] and ok["restored_sample"] > 0 and ok["missing"] == [] and ok["bad_hash"] == []
    victim = next(dest.rglob("*.CR3"))
    flip_byte_keep_stat(victim, 200)
    bad = backup.restore_verify(lib, dest, sample=50, seed=0)
    assert not bad["ok"] and bad["bad_hash"]
    victim.unlink()
    assert backup.restore_verify(lib, dest)["missing"]


def test_catalog_snapshot_is_a_consistent_dated_copy(tmp_path):
    lib = _lib_with_metadata(tmp_path)
    with Catalog(lib) as cat:
        snaps = []
        for _ in range(3):
            snaps.append(backup.snapshot_catalog(cat, keep=2))
            os.utime(snaps[-1], (1, len(snaps)))
            snaps[-1].rename(snaps[-1].with_name(f"catalog-2026010{len(snaps)}-000000.sqlite"))
        left = sorted((lib / ".photostudio" / "snapshots").glob("catalog-*.sqlite"))
        assert len(left) <= 3
        s = backup.snapshot_catalog(cat, keep=2)
        assert len(list(s.parent.glob("catalog-*.sqlite"))) == 2
    con = sqlite3.connect(s)
    assert con.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 4
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


# ================================================================================================ config / status
def _write_cfg(lib: Path, dests: list[dict]) -> None:
    import yaml

    cfg = {"version": 1, "destinations": dests}
    (lib / ".photostudio").mkdir(exist_ok=True)
    backup.config_path(lib).write_text(yaml.safe_dump(cfg))


def test_init_config_and_validation(tmp_path):
    p = backup.init_config(tmp_path)
    assert p.exists() and "3-2-1" in p.read_text()
    p.write_text(p.read_text() + "\n# my edit\n")
    with pytest.raises(FileExistsError):                                       # never clobbers an edited profile
        backup.init_config(tmp_path)
    assert "# my edit" in p.read_text()
    backup.init_config(tmp_path, force=True)
    assert "# my edit" not in p.read_text()
    cfg = backup.load_config(tmp_path)
    assert cfg["destinations"] and backup.validate_config(cfg) == []
    bad = {"destinations": [{"name": "a", "type": "rclone"}, {"name": "a", "type": "nope"}, {"type": "local"}]}
    errs = backup.validate_config(bad)
    assert len(errs) >= 3
    with pytest.raises(KeyError):
        backup.get_destination(cfg, "nonexistent")


def test_three_two_one_status_and_run(tmp_path):
    lib = _lib_with_metadata(tmp_path)
    _write_cfg(lib, [{"name": "hdd", "type": "local", "path": str(tmp_path / "hdd"), "media": "hdd", "offsite": False},
                     {"name": "cloudlike", "type": "local", "path": str(tmp_path / "cloud"), "media": "cloud", "offsite": True}])
    before = backup.status(lib)
    assert before["ok"] is False and before["checks"] == {"three_copies": False, "two_media": False, "one_offsite": False}
    assert any("hdd" in w and "not available" in w for w in before["warnings"])      # drive not there yet
    res = bt.run_backup(lib)
    assert res["ok"] and [r["dest"] for r in res["results"]] == ["hdd", "cloudlike"]
    assert res["snapshot"] and Path(res["snapshot"]).exists()
    assert res["manifest"]["files"] > 0
    after = backup.status(lib)
    assert after["checks"] == {"three_copies": True, "two_media": True, "one_offsite": True}
    assert not [w for w in after["warnings"] if "never backed up" in w or "copies" in w or "off-site" in w]
    assert any("integrity never verified" in w for w in after["warnings"])
    backup.manifest_verify(lib)
    assert not any("never verified" in w for w in backup.status(lib)["warnings"])
    assert (tmp_path / "cloud" / ".photostudio" / "manifest.json").exists()
    # an unmounted drive is reported, not silently created at a root path
    _write_cfg(lib, [{"name": "usb", "type": "local", "path": "/nonexistent-mount/usb/Photos"}])
    r = bt.run_backup(lib)
    assert r["ok"] is False and "not available" in r["results"][0]["error"]
    assert not Path("/nonexistent-mount").exists()
    assert any("not available" in w for w in backup.status(lib)["warnings"])


# ================================================================================================ rclone / restic builders
CFG = backup.DEFAULT_CONFIG


def test_rclone_originals_copy_is_immutable_and_never_deletes():
    cmd = bt.rclone_copy_originals("/photos", "b2crypt:photos", CFG, {"transfers": 4})
    assert cmd[:2] == ["rclone", "copy"] and cmd[2:4] == ["/photos", "b2crypt:photos"]
    assert "--immutable" in cmd and "--checksum" in cmd and "--ignore-case" in cmd and "--use-json-log" in cmd
    assert cmd[cmd.index("--transfers") + 1] == "4"
    for forbidden in ("sync", "purge", "delete", "deletefile", "move", "--delete-excluded", "--delete-before", "--delete-after"):
        assert forbidden not in cmd
    flt = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--filter"]
    assert flt[-1] == "- *" and any(f.startswith("+ *.{") and "cr3" in f for f in flt)
    assert "- *.bak" in flt and "- /.photostudio/catalog.sqlite*" in flt and "- /.photostudio/thumbs/**" in flt
    assert "+ *.ps.json" not in flt                                           # originals copy leaves sidecars to the sync step
    assert "--dry-run" not in cmd and "--dry-run" in bt.rclone_copy_originals("/p", "r:p", CFG, native_dry_run=True)


def test_rclone_metadata_sync_keeps_every_replaced_file():
    cmd = bt.rclone_sync_meta("/photos", "b2crypt:photos", CFG, {}, stamp="20261113-101500")
    assert cmd[:2] == ["rclone", "sync"]
    assert cmd[cmd.index("--backup-dir") + 1] == "b2crypt:_trash/photos/2026-11-13"
    assert cmd[cmd.index("--suffix") + 1] == ".20261113-101500" and "--suffix-keep-extension" in cmd and "--checksum" in cmd
    flt = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--filter"]
    assert "+ *.ps.json" in flt and "+ *.xmp" in flt and "+ /.photostudio/snapshots/**" in flt
    assert not any(f.startswith("+ *.{") for f in flt)                        # sync must never touch originals
    assert "--delete-excluded" not in cmd
    explicit = bt.rclone_sync_meta("/p", "r:photos", CFG, {"backup_dir": "r:old"}, stamp="20260101-000000")
    assert explicit[explicit.index("--backup-dir") + 1] == "r:old"
    with pytest.raises(ValueError):
        bt.rclone_sync_meta("/p", "r:", CFG)                                  # a remote root has no sibling for the backup dir
    assert bt.default_backup_dir("remote:a/b/photos", "2026-01-02") == "remote:a/b/_trash/photos/2026-01-02"
    assert bt.default_backup_dir("/mnt/backup/photos", "2026-01-02") == "/mnt/backup/_trash/photos/2026-01-02"


def test_rclone_check_and_cryptcheck():
    chk = bt.rclone_check("/photos", "b2:photos", CFG, {})
    assert chk[:2] == ["rclone", "check"] and "--one-way" in chk
    cc = bt.rclone_check("/photos", "b2crypt:photos", CFG, {"crypt": True})
    assert cc[:2] == ["rclone", "cryptcheck"] and "--one-way" in cc
    assert bt.rclone_restore_sample("r:photos", "2026/a.CR3", "/tmp/out")[:2] == ["rclone", "copyto"]
    assert bt.split_remote("b2crypt:photos/x") == ("b2crypt", "photos/x") and bt.split_remote("/mnt/x") == ("", "/mnt/x")


def test_restic_commands(tmp_path):
    d = {"repo": "rclone:b2:bucket/restic", "password_file": "~/pw"}
    assert bt.restic_init(d)[:3] == ["restic", "-r", "rclone:b2:bucket/restic"] and bt.restic_init(d)[-1] == "init"
    assert "--password-file" in bt.restic_init(d)
    b = bt.restic_backup(d, files_from=tmp_path / "list.txt", native_dry_run=True)
    assert b[b.index("backup") + 1:][:2] == ["--tag", "photostudio"] and "--files-from" in b and "--dry-run" in b
    c = bt.restic_check(d, "5%")
    assert c[-2:] == ["check", "--read-data-subset=5%"]
    assert bt.restic_snapshots(d)[-2:] == ["snapshots", "--json"]
    f = bt.restic_forget(d)
    assert "--dry-run" in f and "--prune" not in f                            # retention is a dry run unless applied
    for flag in ("--keep-daily", "--keep-weekly", "--keep-monthly", "--keep-yearly"):
        assert flag in f
    assert "--prune" not in bt.restic_forget(d, prune=True) and "--dry-run" in bt.restic_forget(d, prune=True)
    applied = bt.restic_forget(d, prune=True, apply=True)
    assert "--prune" in applied and "--dry-run" not in applied
    no_pw = bt.restic_check({"repo": "/r"})
    assert "--password-file" not in no_pw


def test_filter_rules_follow_enabled_kinds():
    cfg = {**CFG, "include": {"originals": True, "sidecars": False, "xmp": False, "catalog": False, "exports": False}}
    flt = bt.rclone_filters(cfg, ("originals", "sidecars", "xmp", "catalog"))
    assert not any("ps.json" in f or ".xmp" in f for f in flt)
    exp = bt.rclone_filters({**CFG, "include": {**CFG["include"], "exports": True}}, ("exports",))
    assert "+ /exports/**" in exp


# ================================================================================================ runner / dry run
def _remote_cfg(lib: Path) -> None:
    _write_cfg(lib, [{"name": "b2", "type": "rclone", "remote": "b2crypt:photos", "crypt": True, "offsite": True, "media": "cloud"},
                     {"name": "meta", "type": "restic", "repo": "rclone:b2:bucket/restic", "password_file": "~/pw", "offsite": True}])


def test_dry_run_needs_no_binaries_and_changes_nothing(tmp_path, monkeypatch):
    lib = _lib_with_metadata(tmp_path)
    _remote_cfg(lib)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))                   # neither rclone nor restic can be found
    before = tree_files(lib)
    res = bt.run_backup(lib, dry_run=True)
    assert res["ok"] and res["dry_run"] and tree_files(lib) == before
    b2 = res["results"][0]
    assert [c["step"] for c in b2["commands"]] == ["originals", "meta"]
    assert all(c["executed"] is False for c in b2["commands"])
    assert b2["commands"][0]["command"].startswith("rclone copy ") and "--immutable" in b2["commands"][0]["command"]
    assert b2["commands"][1]["command"].startswith("rclone sync ") and "--backup-dir" in b2["commands"][1]["command"]
    assert res["results"][1]["commands"][0]["command"].startswith("restic -r rclone:b2:bucket/restic ") and " backup " in res["results"][1]["commands"][0]["command"]
    assert backup.load_state(lib).get("runs", []) == []                       # a dry run is not recorded as a backup
    plan = bt.plan_backup(lib, "b2")
    assert len(plan) == 1 and plan[0]["commands"][0]["step"] == "originals"
    native = bt.plan_backup(lib, "b2", native_dry_run=True)
    assert all("--dry-run" in c["command"] for c in native[0]["commands"])
    # a real run without the binaries reports the missing dependency instead of crashing
    real = bt.run_backup(lib, "b2", snapshot=False, update_manifest=False)
    assert real["ok"] is False and "rclone" in real["results"][0]["error"]
    assert backup.load_state(lib)["runs"][-1]["ok"] is False


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses a shell script as a fake binary")
def test_run_with_a_fake_rclone_executes_copy_sync_check_in_order(tmp_path, monkeypatch):
    lib = _lib_with_metadata(tmp_path)
    _write_cfg(lib, [{"name": "b2", "type": "rclone", "remote": "b2crypt:photos", "crypt": True, "offsite": True, "media": "cloud"}])
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    exe = bindir / "rclone"
    exe.write_text("#!/bin/sh\necho \"$@\" >> '%s'\n"
                   "echo '{\"level\":\"info\",\"msg\":\"stats\",\"stats\":{\"bytes\":10,\"totalBytes\":10,\"transfers\":2,\"errors\":0}}' >&2\n"
                   "exit 0\n" % log)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    res = bt.run_backup(lib, "b2")
    assert res["ok"], res
    verbs = [line.split()[0] for line in log.read_text().splitlines()]
    assert verbs == ["copy", "sync", "cryptcheck"]
    steps = res["results"][0]["commands"]
    assert [s["step"] for s in steps] == ["originals", "meta", "verify"] and all(s["returncode"] == 0 for s in steps)
    assert steps[0]["summary"]["transfers"] == 2
    assert backup.load_state(lib)["runs"][-1] == {**backup.load_state(lib)["runs"][-1], "dest": "b2", "ok": True}
    # a failing rclone is a failed run, and the verify step does not run after a failed copy
    exe.write_text("#!/bin/sh\necho \"$@\" >> '%s'\necho 'boom' >&2\nexit 3\n" % log)
    log.unlink()
    bad = bt.run_backup(lib, "b2", snapshot=False, update_manifest=False)
    assert bad["ok"] is False and len(log.read_text().splitlines()) == 2        # copy + sync attempted, no check
    assert bt.verify_destination(lib, "b2")["ok"] is False


def test_parse_rclone_json_log():
    text = "\n".join([json.dumps({"level": "info", "msg": "x", "stats": {"bytes": 5, "totalBytes": 9, "transfers": 1, "checks": 3}}),
                      json.dumps({"level": "error", "msg": "Failed to copy: boom"}), "not json"])
    s = bt.parse_rclone_log(text)
    assert s["bytes"] == 5 and s["errors"] == ["Failed to copy: boom"] and s["checks"] == 3


def test_run_command_dry_run_does_not_need_the_binary():
    r = bt.run_command(["definitely-not-installed", "--flag", "a b"], dry_run=True)
    assert r["executed"] is False and r["command"] == "definitely-not-installed --flag 'a b'"
    with pytest.raises(MissingDependency):
        bt.run_command(["definitely-not-installed"])


# ================================================================================================ CLI
def _cli(argv, capsys):
    from photostudio import cli

    rc = cli.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out


def test_backup_cli(tmp_path, capsys):
    lib = _lib_with_metadata(tmp_path)
    base = ["--root", str(lib), "--json", "backup"]
    rc, out = _cli(base + ["init"], capsys)
    assert rc == 0 and Path(json.loads(out)["config"]).exists()
    _write_cfg(lib, [{"name": "hdd", "type": "local", "path": str(tmp_path / "hdd"), "media": "hdd"}])
    rc, out = _cli(base + ["status"], capsys)
    assert rc == 0 and json.loads(out)["checks"]["three_copies"] is False
    rc, out = _cli(base + ["run", "--dry-run"], capsys)
    assert rc == 0 and json.loads(out)["dry_run"] is True and not (tmp_path / "hdd").exists()
    rc, out = _cli(base + ["run"], capsys)
    assert rc == 0 and json.loads(out)["ok"] and (tmp_path / "hdd").exists()
    rc, out = _cli(base + ["verify", "--full"], capsys)
    j = json.loads(out)
    assert rc == 0 and j["clean"] and j["corrupt"] == []
    rc, out = _cli(base + ["verify", "--dest", "hdd"], capsys)
    assert rc == 0 and json.loads(out)["ok"]
    flip_byte_keep_stat(next(lib.rglob("*.CR3")), 300)
    rc, out = _cli(base + ["verify", "--full"], capsys)
    assert json.loads(out)["corrupt"] and json.loads(out)["clean"] is False
    rc, out = _cli(base + ["manifest", "--b3", str(lib / "2026")], capsys)
    assert rc == 0 and json.loads(out)["files"] >= 1
    rc, out = _cli(base + ["snapshot"], capsys)
    assert rc == 0 and Path(json.loads(out)["snapshot"]).exists()
    rc, out = _cli(base + ["run", "--dest", "nope"], capsys)
    assert rc == 1 and "nope" in json.loads(out)["error"]


def test_import_cli_roundtrip(tmp_path, card, capsys):
    lib = tmp_path / "lib"
    rc, out = _cli(["--root", str(lib), "--json", "import", str(card), "--event", "trip", "--dry-run"], capsys)
    assert rc == 0 and json.loads(out)["counts"]["would_copy"] == 6
    rc, out = _cli(["--root", str(lib), "--json", "import", str(card), "--event", "trip", "--dest2", str(tmp_path / "lib2")], capsys)
    j = json.loads(out)
    assert rc == 0 and j["safe_to_format"] is True
    rc, out = _cli(["--root", str(lib), "--json", "ls", "--last-import"], capsys)
    assert rc == 0 and len(json.loads(out)) == 4
    rc, out = _cli(["--root", str(lib), "--json", "rate", "5", "@last-import"], capsys)
    assert rc == 0 and json.loads(out)["images"] == 4
    assert file_hash(next((lib / "2026").rglob("MANIFEST.b3"))) is not None
