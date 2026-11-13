"""Backup foundations: BLAKE3 manifests with bit-rot detection, 3-2-1 status, backup profile config, a verifying
pure-Python local mirror, catalog snapshots / export / import in open formats, and restore verification.

Two manifest flavours (both BLAKE3, both rebuildable):
  * ``MANIFEST.b3``  per import folder, b3sum-compatible (`<hex>  <name>` per line), written by the importer;
    `b3sum -c MANIFEST.b3` works inside the folder.
  * ``<root>/.photostudio/manifest.json``  whole-library integrity DB with size + mtime per file. Rule (bitrot's):
    SAME size and mtime but a DIFFERENT hash is corruption; a changed size/mtime is a legitimate edit and is
    re-hashed by `manifest_update`. Updating never re-hashes unchanged files, so it cannot launder corruption.
Tiers: originals (RAW/JPEG/video, immutable) + sidecars + XMP + dated catalog snapshots; never thumbnails, exports
or model weights (reproducible).
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .util import ALL_MEDIA, dumps, file_hash, which

MANIFEST_NAME = "MANIFEST.b3"
INTEGRITY_FILE = "manifest.json"
STATE_FILE = "backup_state.json"
CONFIG_FILE = "backup.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "working": {"media": "ssd"},
    "include": {"originals": True, "sidecars": True, "xmp": True, "catalog": True, "exports": False},
    "exclude": ["**/.photostudio/thumbs/**", "**/*.part", "**/*.tmp", "**/*.bak", "**/.DS_Store", "**/Thumbs.db",
                "**/__pycache__/**"],
    "exports_dirs": ["exports", "_exports"],
    "destinations": [],
    "verify": {"sample": 0.1, "full_every_days": 90},
}
CONFIG_TEMPLATE = """# photostudio backup profile (3-2-1): working copy + >= 1 other medium + >= 1 off-site.
version: 1
working: {media: ssd}
include:                # what goes into a backup. Exports and thumbnails are reproducible and left out.
  originals: true       # RAW / JPEG / video: copied immutably (never overwritten)
  sidecars: true        # *.ps.json edit recipes, ratings, tags
  xmp: true             # *.xmp standard metadata
  catalog: true         # dated catalog snapshots + albums.json + this file
  exports: false
exclude: ["**/.photostudio/thumbs/**", "**/*.part", "**/*.tmp", "**/*.bak"]
destinations:
  - name: external-hdd          # local / external drive: verified pure-Python mirror
    type: local
    path: /run/media/YOU/BACKUP/Photos
    media: hdd
    offsite: false
    mode: copy                  # copy: never deletes | sync: removed files go to _trash/<date>, never deleted
  # - name: b2-crypt              # off-site: rclone (copy --immutable for originals, sync --backup-dir for sidecars)
  #   type: rclone
  #   remote: "b2crypt:photos"
  #   backup_dir: "b2crypt:_trash/photos"
  #   media: cloud
  #   offsite: true
  #   crypt: true                 # use `rclone cryptcheck` for verification
  #   transfers: 16
  # - name: restic-meta           # versioned snapshots of sidecars + catalog
  #   type: restic
  #   repo: "rclone:b2:my-bucket/restic"
  #   password_file: ~/.config/photostudio/restic.pass
  #   what: [sidecars, xmp, catalog]
  #   media: cloud
  #   offsite: true
verify: {sample: 0.1, full_every_days: 90}
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ================================================================================================ config
def meta_dir(root: str | Path) -> Path:
    d = Path(root).expanduser() / ".photostudio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path(root: str | Path) -> Path:
    return meta_dir(root) / CONFIG_FILE


def init_config(root: str | Path, force: bool = False) -> Path:
    p = config_path(root)
    if p.exists() and not force:
        raise FileExistsError(f"{p} already exists (use force to overwrite)")
    p.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return p


def load_config(root: str | Path) -> dict:
    import copy

    import yaml

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    p = config_path(root)
    if p.exists():
        user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def validate_config(cfg: dict) -> list[str]:
    errs = []
    names = set()
    for d in cfg.get("destinations", []):
        n = d.get("name")
        if not n:
            errs.append("destination without a name")
        elif n in names:
            errs.append(f"duplicate destination name '{n}'")
        names.add(n)
        t = d.get("type")
        if t == "local" and not d.get("path"):
            errs.append(f"{n}: local destination needs 'path'")
        elif t == "rclone" and not d.get("remote"):
            errs.append(f"{n}: rclone destination needs 'remote' (e.g. b2crypt:photos)")
        elif t == "restic" and not d.get("repo"):
            errs.append(f"{n}: restic destination needs 'repo'")
        elif t not in ("local", "rclone", "restic"):
            errs.append(f"{n}: unknown type '{t}' (local|rclone|restic)")
    return errs


def get_destination(cfg: dict, name: str | None) -> list[dict]:
    dests = cfg.get("destinations", [])
    if name is None:
        return dests
    sel = [d for d in dests if d.get("name") == name]
    if not sel:
        raise KeyError(f"no destination '{name}' in backup.yaml (have: {', '.join(d.get('name', '?') for d in dests) or 'none'})")
    return sel


# ================================================================================================ file classification
def glob_to_re(pat: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("/**", i):
            out += "(?:/.*)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile("^" + out + "$", re.IGNORECASE)


def excluded(rel: str, cfg: dict) -> bool:
    if any(glob_to_re(p).match(rel) for p in cfg.get("exclude", [])):
        return True
    return False


def classify(rel: str, cfg: dict | None = None) -> str | None:
    """'originals' | 'sidecars' | 'xmp' | 'catalog' | 'exports' | None (not backed up / never)."""
    cfg = cfg or DEFAULT_CONFIG
    low = rel.lower()
    parts = rel.split("/")
    if parts[0] == ".photostudio":
        if low.startswith(".photostudio/snapshots/") or low in (".photostudio/albums.json", f".photostudio/{CONFIG_FILE}",
                                                                 f".photostudio/{INTEGRITY_FILE}"):
            return "catalog"
        return None
    if any(p.startswith(".") for p in parts):
        return None
    if excluded(rel, cfg):
        return None
    if parts[0] in cfg.get("exports_dirs", []):
        return "exports"
    if low.endswith(".ps.json"):
        return "sidecars"
    if low.endswith(".xmp"):
        return "xmp"
    if os.path.splitext(low)[1] in ALL_MEDIA or parts[-1] == MANIFEST_NAME:
        return "originals"
    return None


def enabled_kinds(cfg: dict, kinds: Iterable | None = None) -> set[str]:
    inc = cfg.get("include", {})
    base = {k for k in ("originals", "sidecars", "xmp", "catalog", "exports") if inc.get(k)}
    return base & set(kinds) if kinds else base


def iter_files(root: str | Path, cfg: dict | None = None, kinds: Iterable[str] | None = None) -> Iterator[tuple[str, str, Path]]:
    """Yield (relpath, kind, abspath) for everything that belongs in a backup of `root`."""
    cfg = cfg or DEFAULT_CONFIG
    want = enabled_kinds(cfg, kinds)
    root = Path(root)
    for dp, dns, fns in os.walk(root):
        rel_dir = Path(dp).relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        dns[:] = sorted(d for d in dns if not d.startswith(".") or (rel_dir == "" and d == ".photostudio"))
        if rel_dir == ".photostudio":
            dns[:] = [d for d in dns if d == "snapshots"]
        elif rel_dir.startswith(".photostudio/") and not rel_dir.startswith(".photostudio/snapshots"):
            dns[:] = []
        for fn in sorted(fns):
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            k = classify(rel, cfg)
            if k and k in want:
                yield rel, k, Path(dp) / fn


# ================================================================================================ b3sum manifests
def read_b3(path: str | Path) -> dict[str, str]:
    """Parse a b3sum-style manifest (`<hex>  <path>`; a leading `*` binary marker is tolerated)."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.rstrip("\n")
        if not line.strip() or line.startswith("#"):
            continue
        h, _, name = line.partition(" ")
        name = name.lstrip(" ").lstrip("*")
        if h and name:
            out[name] = h
    return out


def write_b3(path: str | Path, entries: dict[str, str]) -> Path:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("".join(f"{h}  {n}\n" for n, h in sorted(entries.items())), encoding="utf-8")
    os.replace(tmp, p)
    return p


def update_b3(folder: str | Path, entries: dict[str, str]) -> Path:
    """Merge `entries` (name -> hash, relative to `folder`) into folder/MANIFEST.b3."""
    p = Path(folder) / MANIFEST_NAME
    cur = read_b3(p) if p.exists() else {}
    cur.update(entries)
    return write_b3(p, cur)


def verify_b3(manifest: str | Path, base: str | Path | None = None, algo: str = "blake3") -> dict:
    manifest = Path(manifest)
    base = Path(base) if base else manifest.parent
    res = {"ok": [], "corrupt": [], "missing": []}
    for name, h in read_b3(manifest).items():
        f = base / name
        if not f.exists():
            res["missing"].append(name)
        elif file_hash(f, algo) == h:
            res["ok"].append(name)
        else:
            res["corrupt"].append(name)
    res["clean"] = not res["corrupt"] and not res["missing"]
    return res


def create_b3_manifest(folder: str | Path, recursive: bool = True, cfg: dict | None = None) -> dict:
    """(Re)build folder/MANIFEST.b3 for every original in the folder (b3sum-compatible)."""
    folder = Path(folder)
    cfg = cfg or DEFAULT_CONFIG
    entries = {}
    for dp, dns, fns in os.walk(folder):
        dns[:] = sorted(d for d in dns if not d.startswith(".")) if recursive else []
        for fn in sorted(fns):
            if os.path.splitext(fn)[1].lower() in ALL_MEDIA and not fn.startswith("."):
                f = Path(dp) / fn
                entries[f.relative_to(folder).as_posix()] = file_hash(f)
    p = write_b3(folder / MANIFEST_NAME, entries)
    return {"manifest": str(p), "files": len(entries)}


def collect_known_hashes(*roots: str | Path) -> dict[str, str]:
    """hash -> path (absolute) from every MANIFEST.b3 below the roots (import-time duplicate detection)."""
    known: dict[str, str] = {}
    for r in roots:
        r = Path(r)
        if not r.exists():
            continue
        for dp, dns, fns in os.walk(r):
            dns[:] = [d for d in dns if not d.startswith(".")]
            if MANIFEST_NAME in fns:
                try:
                    for n, h in read_b3(Path(dp) / MANIFEST_NAME).items():
                        known.setdefault(h, str(Path(dp) / n))
                except OSError:
                    continue
    return known


# ================================================================================================ integrity manifest (json)
def integrity_path(root: str | Path) -> Path:
    return meta_dir(root) / INTEGRITY_FILE


def manifest_load(root: str | Path) -> dict:
    p = integrity_path(root)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"version": 1, "algo": "blake3", "created": now_iso(), "updated": None, "files": {}}


def manifest_save(root: str | Path, m: dict) -> Path:
    m["updated"] = now_iso()
    p = integrity_path(root)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(m, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, p)
    return p


def _hash_algo(m: dict) -> str:
    a = m.get("algo", "blake3")
    if a == "blake3":
        try:
            import blake3  # noqa: F401
        except ImportError:
            return "sha256"
    return a


def manifest_update(root: str | Path, cfg: dict | None = None, kinds: Iterable[str] = ("originals",),
                    forget_missing: bool = False, progress: Callable[[int, int], None] | None = None) -> dict:
    """Add new files and re-hash files whose size/mtime changed. Files with unchanged size+mtime are NOT re-hashed
    (so corruption remains detectable by `manifest_verify`)."""
    root = Path(root)
    cfg = cfg or load_config(root)
    m = manifest_load(root)
    if not m["files"]:
        m["algo"] = _hash_algo(m)
    algo = m.get("algo", "blake3")
    files = m["files"]
    seen = set()
    added = modified = touched = unchanged = 0
    todo = list(iter_files(root, cfg, kinds))
    for n, (rel, kind, p) in enumerate(todo):
        seen.add(rel)
        st = p.stat()
        cur = files.get(rel)
        if cur and cur["s"] == st.st_size and cur["m"] == st.st_mtime_ns:
            unchanged += 1
        else:
            h = file_hash(p, algo)
            if cur is None:
                added += 1
            elif cur["h"] == h:
                touched += 1
            else:
                modified += 1
            files[rel] = {"h": h, "s": st.st_size, "m": st.st_mtime_ns, "k": kind}
        if progress and n % 50 == 0:
            progress(n + 1, len(todo))
    kset = enabled_kinds(cfg, kinds)
    missing = [r for r, v in files.items() if r not in seen and v.get("k", "originals") in kset and not (root / r).exists()]
    if forget_missing:
        for r in missing:
            del files[r]
    manifest_save(root, m)
    return {"files": len(files), "added": added, "modified": modified, "touched": touched, "unchanged": unchanged,
            "missing": missing, "forgotten": len(missing) if forget_missing else 0, "algo": algo}


def manifest_verify(root: str | Path, cfg: dict | None = None, kinds: Iterable[str] = ("originals",),
                    sample: float | None = None, seed: int | None = None,
                    progress: Callable[[int, int], None] | None = None) -> dict:
    """Compare the library against its integrity manifest.
      corrupt   size + mtime identical to the manifest but the content hash differs (bit rot / silent damage)
      modified  size or mtime changed (a legitimate edit; run `manifest_update`)
      missing   in the manifest, gone from disk;   new   on disk, not in the manifest
    `sample` (0..1) hashes only that share of the unchanged files (random); modified/missing/new are always found."""
    root = Path(root)
    cfg = cfg or load_config(root)
    m = manifest_load(root)
    algo = _hash_algo(m)
    kset = enabled_kinds(cfg, kinds)
    files = {r: v for r, v in m["files"].items() if v.get("k", "originals") in kset}
    res: dict[str, Any] = {"corrupt": [], "modified": [], "missing": [], "new": [], "ok": 0, "checked": 0}
    on_disk = {rel: p for rel, _, p in iter_files(root, cfg, kinds)}
    res["new"] = sorted(set(on_disk) - set(files))
    rng = random.Random(seed)
    hashable = []
    for rel, v in sorted(files.items()):
        p = on_disk.get(rel) or (root / rel)
        if not p.exists():
            res["missing"].append(rel)
            continue
        st = p.stat()
        if st.st_size != v["s"] or st.st_mtime_ns != v["m"]:
            res["modified"].append(rel)
        else:
            hashable.append((rel, v, p))
    if sample is not None and 0 < sample < 1:
        hashable = rng.sample(hashable, max(1, int(round(len(hashable) * sample)))) if hashable else []
    for n, (rel, v, p) in enumerate(hashable):
        res["checked"] += 1
        if file_hash(p, algo) == v["h"]:
            res["ok"] += 1
        else:
            res["corrupt"].append(rel)
        if progress and n % 25 == 0:
            progress(n + 1, len(hashable))
    res["clean"] = not res["corrupt"] and not res["missing"]
    res["algo"] = algo
    state = load_state(root)
    state["last_verify"] = {"time": now_iso(), "checked": res["checked"], "corrupt": len(res["corrupt"]),
                            "missing": len(res["missing"]), "sample": sample}
    save_state(root, state)
    return res


# ================================================================================================ state + 3-2-1 status
def load_state(root: str | Path) -> dict:
    p = meta_dir(root) / STATE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"runs": [], "last_verify": None}


def save_state(root: str | Path, st: dict) -> None:
    p = meta_dir(root) / STATE_FILE
    st["runs"] = st.get("runs", [])[-200:]
    p.write_text(json.dumps(st, indent=1), encoding="utf-8")


def record_run(root: str | Path, dest: str, typ: str, ok: bool, summary: dict) -> None:
    st = load_state(root)
    st.setdefault("runs", []).append({"time": now_iso(), "dest": dest, "type": typ, "ok": ok, "summary": summary})
    save_state(root, st)


def _days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    return (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 86400.0


def status(root: str | Path, cfg: dict | None = None) -> dict:
    """3-2-1 report: >= 3 copies, >= 2 distinct media, >= 1 off-site; destination availability; freshness."""
    root = Path(root)
    cfg = cfg or load_config(root)
    st = load_state(root)
    copies = [{"name": "working", "type": "working", "path": str(root), "media": cfg.get("working", {}).get("media", "ssd"),
               "offsite": False, "available": root.exists(), "last_run": None}]
    warnings = []
    for d in cfg.get("destinations", []):
        t = d.get("type")
        if t == "local":
            avail = Path(d["path"]).expanduser().exists()
        elif t == "rclone":
            avail = bool(which("rclone"))
        else:
            avail = bool(which("restic"))
        runs = [r for r in st.get("runs", []) if r["dest"] == d.get("name") and r["ok"]]
        last = runs[-1]["time"] if runs else None
        age = _days_since(last)
        if not avail:
            warnings.append(f"{d.get('name')}: not available right now ({'drive not mounted?' if t == 'local' else t + ' binary missing'})")
        elif last is None:
            warnings.append(f"{d.get('name')}: never backed up")
        elif age is not None and age > 14:
            warnings.append(f"{d.get('name')}: last successful backup {age:.0f} days ago")
        copies.append({"name": d.get("name"), "type": t, "path": d.get("path") or d.get("remote") or d.get("repo"),
                       "media": d.get("media", "other"), "offsite": bool(d.get("offsite")), "available": avail, "last_run": last,
                       "ever_ok": last is not None})
    # copies that count: working + destinations that have completed at least one successful run
    live = [c for c in copies if c["type"] == "working" or c.get("ever_ok")]
    checks = {"three_copies": len(live) >= 3, "two_media": len({c["media"] for c in live}) >= 2,
              "one_offsite": any(c["offsite"] for c in live)}
    if not checks["three_copies"]:
        warnings.append(f"only {len(live)} verified copy/copies (3 wanted)")
    if not checks["two_media"]:
        warnings.append("all copies are on the same kind of medium")
    if not checks["one_offsite"]:
        warnings.append("no off-site copy yet")
    lv = st.get("last_verify")
    if lv is None:
        warnings.append("integrity never verified (photo backup verify)")
    elif (_days_since(lv["time"]) or 0) > cfg.get("verify", {}).get("full_every_days", 90):
        warnings.append(f"last integrity check {_days_since(lv['time']):.0f} days ago")
    if lv and lv.get("corrupt"):
        warnings.append(f"last verify found {lv['corrupt']} corrupt file(s)")
    mp = integrity_path(root)
    return {"root": str(root), "copies": copies, "checks": checks, "ok": all(checks.values()) and not (lv and lv.get("corrupt")),
            "last_verify": lv, "manifest_files": len(manifest_load(root)["files"]) if mp.exists() else 0,
            "config": str(config_path(root)) if config_path(root).exists() else None, "warnings": warnings,
            "config_errors": validate_config(cfg)}


# ================================================================================================ local mirror
def _copy_verified(src: Path, dst: Path, algo: str = "blake3") -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("." + dst.name + ".part")
    shutil.copyfile(src, tmp)
    sh = file_hash(src, algo)
    if file_hash(tmp, algo) != sh:
        tmp.unlink(missing_ok=True)
        raise IOError(f"verification failed after copying {src.name} (hash mismatch)")
    shutil.copystat(src, tmp)
    os.replace(tmp, dst)
    return sh


def mirror(src_root: str | Path, dest_root: str | Path, cfg: dict | None = None, kinds: Iterable[str] | None = None,
           mode: str = "copy", checksum: bool = False, dry_run: bool = False, overwrite_originals: bool = False,
           trash_root: str | Path | None = None, progress: Callable[[int, int], None] | None = None) -> dict:
    """rsync-like verified mirror. New files are copied (and re-hashed), unchanged files skipped (size+mtime, or
    hash with `checksum`). Originals are IMMUTABLE: a differing original is reported as a conflict and never
    overwritten (like `rclone --immutable`). Sidecars/XMP/catalog files may change: the previous version is moved
    to `<trash_root>/<date>/` first. `mode='sync'` also moves files that vanished from the source to the trash;
    nothing is ever deleted."""
    src_root, dest_root = Path(src_root), Path(dest_root).expanduser()
    cfg = cfg or load_config(src_root)
    day = datetime.now().strftime("%Y-%m-%d")
    trash = Path(trash_root) if trash_root else dest_root.parent / "_trash" / dest_root.name / day
    res: dict[str, Any] = {"copied": 0, "updated": 0, "skipped": 0, "conflicts": [], "trashed": 0, "errors": [], "bytes": 0,
                           "dry_run": dry_run, "dest": str(dest_root), "plan": []}
    if not dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)
    files = list(iter_files(src_root, cfg, kinds))
    src_rel = set()
    for n, (rel, kind, p) in enumerate(files):
        src_rel.add(rel)
        d = dest_root / rel
        try:
            if not d.exists():
                res["plan"].append(("copy", rel))
                if not dry_run:
                    _copy_verified(p, d)
                res["copied"] += 1
                res["bytes"] += p.stat().st_size
            else:
                ss, ds = p.stat(), d.stat()
                same = ss.st_size == ds.st_size and (abs(ss.st_mtime - ds.st_mtime) < 2.0 if not checksum
                                                    else file_hash(p) == file_hash(d))
                if same:
                    res["skipped"] += 1
                elif kind == "originals" and not overwrite_originals:
                    res["conflicts"].append(rel)
                else:
                    res["plan"].append(("update", rel))
                    if not dry_run:
                        t = trash / rel
                        t.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(d), str(t))
                        res["trashed"] += 1
                        _copy_verified(p, d)
                    res["updated"] += 1
                    res["bytes"] += ss.st_size
        except Exception as exc:
            res["errors"].append(f"{rel}: {exc}")
        if progress and n % 25 == 0:
            progress(n + 1, len(files))
    if mode == "sync":
        want = enabled_kinds(cfg, kinds)
        for rel, kind, p in list(iter_files(dest_root, {**cfg, "include": {k: True for k in want}}, kinds)):
            if rel not in src_rel:
                res["plan"].append(("trash", rel))
                if not dry_run:
                    t = trash / rel
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(t))
                res["trashed"] += 1
    res["ok"] = not res["errors"] and not res["conflicts"]
    return res


# ================================================================================================ catalog snapshot / export
def snapshot_catalog(cat, keep: int = 10) -> Path:
    """Dated consistent copy of the SQLite catalog (`VACUUM INTO`), keeping the newest `keep`."""
    d = cat.meta_dir / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    p, k = d / f"catalog-{stamp}.sqlite", 1
    while p.exists():                                  # two snapshots within one second must not collide (VACUUM INTO refuses)
        k += 1
        p = d / f"catalog-{stamp}-{k}.sqlite"
    cat.conn.commit()
    cat.conn.execute("VACUUM INTO ?", (str(p),))
    for old in sorted(d.glob("catalog-*.sqlite"))[:-keep] if keep else []:
        old.unlink(missing_ok=True)
    return p


def export_catalog(cat, out_dir: str | Path, formats: Iterable[str] = ("json", "csv", "tags", "albums"),
                   xmp: bool = False, sqlite_copy: bool = False) -> dict:
    """Dump the catalog to open formats so nothing is locked in: images.json (+ratings/flags/labels/tags/albums),
    images.csv, tags.txt (indented tree), albums.json (static members + smart rules); `xmp=True` also refreshes every
    XMP sidecar from the sidecars; `sqlite_copy=True` adds a snapshot of the database."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    formats = set(formats)
    rows = cat.images(sort="path")
    data = []
    for r in rows:
        g = cat.get(r["id"])
        data.append({
            "path": r["primary_path"], "raw": r["raw_path"], "jpg": r["jpg_path"], "uid": r["uid"], "content_hash": r["content_hash"],
            "taken_at": r["taken_at"], "camera": r["camera"], "lens": r["lens"], "iso": r["iso"], "shutter": r["shutter"],
            "aperture": r["aperture"], "focal": r["focal"], "width": r["width"], "height": r["height"],
            "gps": [r["gps_lat"], r["gps_lon"]] if r["gps_lat"] is not None else None, "rating": r["rating"], "flag": r["flag"],
            "label": r["label"], "notes": r["notes"], "caption": r["caption"], "mode": r["mode"], "genre": r["genre"],
            "has_edits": bool(r["has_edits"]), "tags": g["tag_details"], "albums": g["albums"]})
    res: dict[str, Any] = {"out": str(out), "images": len(data), "files": []}
    if "json" in formats:
        p = out / "images.json"
        p.write_text(dumps({"version": 1, "exported": now_iso(), "root": str(cat.root), "images": data}), encoding="utf-8")
        res["files"].append(str(p))
    if "csv" in formats:
        p = out / "images.csv"
        cols = ["path", "taken_at", "camera", "lens", "iso", "shutter", "aperture", "focal", "rating", "flag", "label", "tags",
                "albums", "caption", "notes"]
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for d in data:
                w.writerow([("; ".join(t["path"] for t in d["tags"]) if c == "tags" else "; ".join(d["albums"]) if c == "albums"
                             else d.get(c)) for c in cols])
        res["files"].append(str(p))
    if "tags" in formats:
        from .tags import tree_text

        p = out / "tags.txt"
        p.write_text(tree_text(cat), encoding="utf-8")
        res["files"].append(str(p))
    if "albums" in formats:
        p = out / "albums.json"
        p.write_text(dumps(cat.albums.dump()), encoding="utf-8")
        res["files"].append(str(p))
    if xmp:
        res["xmp_written"] = cat.rewrite_sidecars()
    if sqlite_copy:
        res["sqlite"] = str(snapshot_catalog(cat))
    return res


def import_catalog(cat, in_dir: str | Path) -> dict:
    """Re-apply an export: ratings, flags, labels, tags, notes, captions (matched by relative path) and albums."""
    d = Path(in_dir)
    n = missing = 0
    jf = d / "images.json"
    if jf.exists():
        for it in json.loads(jf.read_text(encoding="utf-8"))["images"]:
            i = cat.find(it["path"])
            if i is None:
                missing += 1
                continue

            def mut(doc, it=it):
                from .catalog import norm_label, norm_tag, store_assignments

                doc["rating"] = int(it.get("rating") or 0)
                doc["flag"] = it.get("flag") if it.get("flag") in ("pick", "reject") else None
                doc["label"] = norm_label(it.get("label"))
                doc["notes"] = it.get("notes") or ""
                if it.get("caption"):
                    doc["caption"] = it["caption"]
                store_assignments(doc, {norm_tag(t["path"]): {"source": t.get("source", "user"), "confidence": t.get("confidence"),
                                                              "confirmed": 1 if t.get("confirmed", 1) else 0} for t in it.get("tags", [])})
            cat._commit(i, mut)
            n += 1
    alb = {}
    af = d / "albums.json"
    if af.exists():
        alb = cat.albums.load(json.loads(af.read_text(encoding="utf-8")))
    return {"images": n, "not_in_catalog": missing, "albums": alb}


# ================================================================================================ restore verification
def restore_verify(root: str | Path, dest: str | Path, sample: int = 10, seed: int | None = None,
                   kinds: Iterable[str] = ("originals", "sidecars", "xmp")) -> dict:
    """Prove a LOCAL backup is restorable: every file the library manifest knows must exist in `dest`, and a random
    sample is copied to a temp dir and re-hashed against the manifest (a real restore, not just a listing)."""
    import tempfile

    root, dest = Path(root), Path(dest).expanduser()
    cfg = load_config(root)
    m = manifest_load(root)
    algo = _hash_algo(m)
    files = {r: v for r, v in m["files"].items() if v.get("k", "originals") in set(kinds)}
    missing = [r for r in files if not (dest / r).exists()]
    present = [r for r in files if r not in set(missing)]
    rng = random.Random(seed)
    pick = rng.sample(present, min(sample, len(present))) if present else []
    bad = []
    with tempfile.TemporaryDirectory(prefix="ps-restore-") as td:
        for r in pick:
            t = Path(td) / Path(r).name
            shutil.copyfile(dest / r, t)
            if file_hash(t, algo) != files[r]["h"]:
                bad.append(r)
    # sidecars/xmp are not in the originals-only manifest: just check presence against the library
    extra_missing = []
    for rel, kind, _ in iter_files(root, cfg, [k for k in kinds if k != "originals"]):
        if not (dest / rel).exists():
            extra_missing.append(rel)
    return {"dest": str(dest), "manifest_files": len(files), "missing": missing, "other_missing": extra_missing,
            "restored_sample": len(pick), "bad_hash": bad, "ok": not (missing or bad or extra_missing),
            "note": "sample restored to a temp dir and re-hashed against the BLAKE3 manifest"}
