"""Library catalog: a rebuildable SQLite cache over sidecars + XMP (the sources of truth).

Layout under the library root::

    <root>/.photostudio/catalog.sqlite   WAL database (this module)
    <root>/.photostudio/thumbs/          thumbnail cache keyed by (image, size, ops hash, source stamp)
    <root>/.photostudio/albums.json      mirror of albums (they have no sidecar to live in)
    <root>/IMG_0001.CR3, IMG_0001.JPG    one logical image (RAW + companion JPEG paired by basename)
    <root>/IMG_0001.CR3.ps.json          edit/rating/flag/label/tag sidecar (contract format)
    <root>/IMG_0001.CR3.xmp              standard XMP (xmp:Rating, xmp:Label, dc:subject, lr:hierarchicalSubject)

Every rating / flag / label / tag change is written to the sidecar AND the standard XMP first and only then
mirrored into the database, so ``rebuild()`` (delete the DB, rescan) reproduces the same state. Raw files are
never opened for writing; the companion JPEG gets in-file metadata when ExifTool exists, else a ``.xmp``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import catalog_sql as csql
from .catalog_data import CatalogDataMixin
from .catalog_sql import RuleError, compile_rule, filter_to_rule
from .util import IMAGE_EXTS, JPEG_EXTS, RAW_EXTS, file_hash, params_hash

SCAN_EXTS = RAW_EXTS | IMAGE_EXTS
COMPANION_EXTS = JPEG_EXTS | {".heic", ".heif"}
# folders the catalog never indexes: export output, culled rejects (hidden dirs, including .photostudio, are always skipped)
SCAN_SKIP_DIRS = {"exports", "_rejects"}
SIDECAR_SUFFIX = ".ps.json"
STD_LABELS = {"red": "Red", "yellow": "Yellow", "green": "Green", "blue": "Blue", "purple": "Purple"}
TAG_KIND_ROOTS = {"people": "person", "places": "place", "events": "event"}
AUTO_CONFIRMED_SOURCES = {"auto:geo": True}


# ------------------------------------------------------------------------------------------ small helpers
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm_tag(t: str) -> str:
    """'a | b|c ' -> 'a|b|c' (no empty parts)."""
    parts = [p.strip() for p in str(t).split("|")]
    return "|".join(p for p in parts if p)


def tag_kind(path: str, auto: bool = False) -> str:
    k = TAG_KIND_ROOTS.get(path.split("|")[0].lower())
    return k or ("auto" if auto else "keyword")


def norm_label(label: str | None) -> str | None:
    if label is None or str(label).strip().lower() in ("", "none", "null"):
        return None
    s = str(label).strip()
    return STD_LABELS.get(s.lower(), s)


def doc_assignments(doc: dict) -> dict[str, dict]:
    """Sidecar doc -> {tag path: {source, confidence, confirmed}}. `tags` holds confirmed paths (user or
    promoted auto); `auto_tags` carries provenance for machine-made ones (confirmed or not)."""
    out: dict[str, dict] = {}
    confirmed = {norm_tag(t) for t in doc.get("tags", []) if norm_tag(t)}
    for p in confirmed:
        out[p] = {"source": "user", "confidence": None, "confirmed": 1}
    for a in doc.get("auto_tags", []) or []:
        p = norm_tag(a.get("tag", ""))
        if not p:
            continue
        out[p] = {"source": a.get("source", "auto"), "confidence": a.get("confidence"),
                  "confirmed": 1 if (p in confirmed or a.get("confirmed")) else 0}
    return out


def store_assignments(doc: dict, assign: dict[str, dict]) -> None:
    doc["tags"] = sorted(p for p, a in assign.items() if a.get("confirmed"))
    auto = [{"tag": p, "source": a["source"], "confidence": a.get("confidence"), "confirmed": bool(a.get("confirmed"))}
            for p, a in sorted(assign.items()) if str(a.get("source", "user")) != "user"]
    if auto:
        doc["auto_tags"] = auto
    else:
        doc.pop("auto_tags", None)


def _place_from_tags(paths: Iterable[str]) -> dict:
    for p in sorted(paths):
        parts = p.split("|")
        if parts[0].lower() == "places" and len(parts) >= 2:
            d = {"country": parts[1]}
            if len(parts) >= 3:
                d["state"] = parts[2]
            if len(parts) >= 4:
                d["city"] = parts[3]
            return d
    return {}


# ------------------------------------------------------------------------------------------ sidecar glue
def _sc_module():
    try:
        from . import sidecar

        return sidecar
    except Exception:
        return None


def sidecar_file(primary: Path) -> Path:
    return primary.with_name(primary.name + SIDECAR_SUFFIX)


def _default_doc(name: str) -> dict:
    return {"schema": 1, "source": name, "active": "default",
            "versions": {"default": {"mode": None, "art": None, "stack": [], "created": now_iso(), "modified": now_iso()}},
            "snapshots": [], "rating": 0, "flag": None, "label": None, "tags": [], "notes": ""}


def load_doc(primary: Path) -> dict:
    sc = _sc_module()
    if sc is not None:
        return sc.load(primary)
    p = sidecar_file(primary)
    if not p.exists():
        return _default_doc(primary.name)
    doc = json.loads(p.read_text(encoding="utf-8"))
    for k, v in _default_doc(primary.name).items():
        doc.setdefault(k, v)
    return doc


def save_doc(primary: Path, doc: dict) -> Path:
    sc = _sc_module()
    if sc is not None:
        return sc.save(primary, doc)
    p = sidecar_file(primary)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def doc_ops_hash(doc: dict) -> tuple[str, bool, str | None, list[dict]]:
    """(ops_hash of the active version's enabled entries, has_edits, mode, version summaries)."""
    versions = doc.get("versions") or {}
    active = doc.get("active") if doc.get("active") in versions else (next(iter(versions), None))
    summaries = []
    for name, v in versions.items():
        stack = [e for e in v.get("stack", []) if e.get("enabled", True)]
        h = params_hash([{k: e.get(k) for k in ("op", "id", "params", "mask", "blend", "opacity", "v")} for e in stack]) \
            if stack else ""
        summaries.append({"name": name, "ops_hash": h, "n_ops": len(stack), "mode": v.get("mode"), "art": v.get("art"),
                          "created": v.get("created"), "modified": v.get("modified"), "is_default": name == active})
    cur = next((s for s in summaries if s["is_default"]), None)
    return (cur["ops_hash"] if cur else ""), bool(cur and cur["n_ops"]), (cur["mode"] if cur else None), summaries


def _fts_query(text: str) -> str:
    toks = re.findall(r"[\w]+", text, flags=re.UNICODE)
    return " ".join('"%s"*' % t.replace('"', "") for t in toks)


# ------------------------------------------------------------------------------------------ Catalog
class Catalog(CatalogDataMixin):
    def __init__(self, root: str | Path, create: bool = True):
        self.root = Path(root).expanduser().resolve()
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.root.is_dir():
            raise FileNotFoundError(self.root)
        self.meta_dir = self.root / ".photostudio"
        self.db_path = self.meta_dir / "catalog.sqlite"
        self.thumb_dir = self.meta_dir / "thumbs"
        self._lock = threading.RLock()
        self.warnings: list[str] = []
        self.fts_enabled = True
        self._albums = None
        self._albums_pending = False
        self.conn: sqlite3.Connection
        self._open()

    # -- connection -----------------------------------------------------------------------------
    def _open(self) -> None:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        fresh = not self.db_path.exists()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(csql.DDL)
        try:
            self.conn.execute(csql.FTS_DDL)
        except sqlite3.OperationalError:
            self.fts_enabled = False
            self.conn.execute(csql.FTS_FALLBACK_DDL)
        ver = self.get_meta("schema_version")
        if ver is None:
            self.set_meta("schema_version", str(csql.SCHEMA_VERSION))
            self.set_meta("created", now_iso())
        elif int(ver) > csql.SCHEMA_VERSION:
            raise RuntimeError(f"catalog schema {ver} is newer than this photostudio ({csql.SCHEMA_VERSION})")
        self.conn.commit()
        if fresh:
            try:
                self._albums_pending = self.albums.json_path.exists()
                self.albums.load_from_disk()
            except Exception as exc:  # a broken albums.json must not make the catalog unusable
                self.warnings.append(f"albums.json not restored: {exc}")

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    @property
    def albums(self):
        if self._albums is None:
            from .albums import AlbumManager

            self._albums = AlbumManager(self)
        return self._albums

    # album convenience delegates (contract: "and albums API")
    def create_album(self, name: str, **kw):
        return self.albums.create(name, **kw)

    def add_to_album(self, album, images):
        return self.albums.add(album, images)

    def list_albums(self, **kw):
        return self.albums.list(**kw)

    # -- meta -----------------------------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))

    # -- paths ----------------------------------------------------------------------------------
    def rel(self, p: str | Path) -> str:
        """Path relative to the library root (posix); paths outside the root are returned unchanged."""
        p = Path(p)
        if not p.is_absolute():
            if (self.root / p).exists() or not (Path.cwd() / p).exists():
                return Path(os.path.normpath(p.as_posix())).as_posix()
            p = Path.cwd() / p
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    def _abs(self, p: str | Path) -> Path:
        """Absolute path of a scan target: relative paths are taken against the library root, or against the current
        directory when they only exist there (e.g. ``photo import SRC --dest relative/dir``)."""
        p = Path(p).expanduser()
        if p.is_absolute():
            return p
        if not (self.root / p).exists() and (Path.cwd() / p).exists():
            return Path.cwd() / p
        return self.root / p

    def abspath(self, rel: str | None) -> Path | None:
        return (self.root / rel) if rel else None

    def _row_dict(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["path"] = str(self.root / d["primary_path"])
        d["raw"] = str(self.root / d["raw_path"]) if d.get("raw_path") else None
        d["jpg"] = str(self.root / d["jpg_path"]) if d.get("jpg_path") else None
        d["filename"] = Path(d["primary_path"]).name
        return d

    def find(self, image: Any) -> int | None:
        """Resolve an id / path (raw, jpg or primary, absolute or relative) / row dict to an image id."""
        if isinstance(image, dict):
            image = image.get("id", image.get("path"))
        if isinstance(image, int):
            r = self.conn.execute("SELECT id FROM images WHERE id=?", (image,)).fetchone()
            return r[0] if r else None
        s = str(image)
        for cand in {self.rel(s), s}:
            r = self.conn.execute("SELECT id FROM images WHERE primary_path=? OR raw_path=? OR jpg_path=?",
                                  (cand, cand, cand)).fetchone()
            if r:
                return r[0]
        return None

    def _ids(self, target: Any) -> list[int]:
        items = target if isinstance(target, (list, tuple, set)) else [target]
        out = []
        for t in items:
            i = self.find(t)
            if i is None:
                raise KeyError(f"not in catalog: {t} (run `photo scan`)")
            out.append(i)
        return out

    def _row(self, image: Any) -> sqlite3.Row:
        i = self.find(image)
        if i is None:
            raise KeyError(f"not in catalog: {image} (run `photo scan`)")
        return self.conn.execute("SELECT * FROM images WHERE id=?", (i,)).fetchone()

    # -- scanning -------------------------------------------------------------------------------
    def _discover(self, subpaths: list | None) -> list[tuple[Path, Path | None]]:
        groups: dict[tuple[str, str], list[Path]] = {}
        bases = [self.root] if not subpaths else [self._abs(p) for p in subpaths]
        for base in bases:
            if base.is_file():
                for f in sorted(base.parent.iterdir()):
                    if f.is_file() and f.stem.lower() == base.stem.lower() and f.suffix.lower() in SCAN_EXTS:
                        groups.setdefault((str(f.parent), f.stem.lower()), []).append(f)
                continue
            for dp, dns, fns in os.walk(base):
                dns[:] = sorted(d for d in dns if not d.startswith(".") and d.lower() not in SCAN_SKIP_DIRS)
                for fn in sorted(fns):
                    if fn.startswith("."):
                        continue
                    stem, ext = os.path.splitext(fn)
                    if ext.lower() in SCAN_EXTS:
                        groups.setdefault((dp, stem.lower()), []).append(Path(dp) / fn)
        items: list[tuple[Path, Path | None]] = []
        for key in sorted(groups):
            files = sorted(set(groups[key]), key=lambda p: p.name)
            raws = [f for f in files if f.suffix.lower() in RAW_EXTS]
            comps = [f for f in files if f.suffix.lower() in COMPANION_EXTS]
            others = [f for f in files if f not in raws and f not in comps]
            if raws:
                items.append((raws[0], comps[0] if comps else None))
                items += [(f, None) for f in raws[1:] + comps[1:]]
            elif comps:
                items.append((comps[0], None))
                items += [(f, None) for f in comps[1:]]
            items += [(f, None) for f in others]
        return items

    def _read_meta(self, primary: Path, comp: Path | None) -> dict:
        from .metadata import read_metadata

        md: dict = {}
        try:
            md = read_metadata(primary)
        except Exception as exc:
            self.warnings.append(f"metadata unreadable for {primary.name}: {exc}")
        if comp is not None and (md.get("datetime") is None or md.get("make") is None or not md.get("width")):
            try:
                cm = read_metadata(comp)
                for k, v in cm.items():
                    if k != "xmp":
                        md.setdefault(k, v)
            except Exception:
                pass
        if not md.get("width") and primary.suffix.lower() in IMAGE_EXTS:
            try:
                from PIL import Image

                with Image.open(primary) as im:
                    md["width"], md["height"] = im.size
            except Exception:
                pass
        return md

    def _media_columns(self, primary: Path, comp: Path | None, md: dict) -> dict:
        st = primary.stat()
        cst = comp.stat() if comp else None
        taken = md.get("datetime")
        sub = md.get("subsec")
        subs = 0.0
        if sub not in (None, ""):
            try:
                subs = float("0." + re.sub(r"\D", "", str(sub)))
            except ValueError:
                subs = 0.0
        if taken:
            try:
                ts = datetime.fromisoformat(taken).timestamp() + subs
            except ValueError:
                taken, ts = None, None
        if not taken:
            ts = st.st_mtime
            taken = datetime.fromtimestamp(ts).isoformat(timespec="seconds")
            subs = 0.0
        w, h = md.get("width"), md.get("height")
        camera = md.get("model")
        make = md.get("make")
        if camera and make and not str(camera).lower().startswith(str(make).lower().split()[0]):
            camera = f"{make} {camera}"
        is_raw = primary.suffix.lower() in RAW_EXTS
        rel = self.rel(primary)
        return {
            "dir": "" if Path(rel).parent.as_posix() == "." else Path(rel).parent.as_posix(),
            "stem": primary.stem, "primary_path": rel,
            "raw_path": rel if is_raw else None,
            "jpg_path": self.rel(comp) if comp else (None if is_raw else rel),
            "kind": ("raw+jpg" if comp else "raw") if is_raw else ("jpg" if primary.suffix.lower() in JPEG_EXTS else "image"),
            "file_size": st.st_size, "file_mtime": st.st_mtime,
            "jpg_size": cst.st_size if cst else None, "jpg_mtime": cst.st_mtime if cst else None,
            "taken_at": taken, "taken_subsec": subs, "taken_ts": ts, "width": w, "height": h,
            "orientation": md.get("orientation"), "make": make, "camera": camera, "lens": md.get("lens"),
            "iso": md.get("iso"), "shutter": md.get("shutter"), "aperture": md.get("aperture"),
            "focal": md.get("focal"), "focal35": md.get("focal35"), "serial": md.get("serial"),
            "file_number": str(md["file_number"]) if md.get("file_number") is not None else None,
            "gps_lat": md.get("gps_lat"), "gps_lon": md.get("gps_lon"), "gps_alt": md.get("gps_alt"),
        }

    @staticmethod
    def _uid_base(cols: dict) -> str:
        key = "|".join(str(cols.get(k) or "") for k in ("camera", "serial", "taken_at", "taken_subsec", "file_number", "stem"))
        return hashlib.sha1(key.lower().encode()).hexdigest()[:20]

    def _unique_uid(self, base: str, own_id: int | None = None) -> str:
        uid, n = base, 1
        while True:
            r = self.conn.execute("SELECT id FROM images WHERE uid=?", (uid,)).fetchone()
            if r is None or r[0] == own_id:
                return uid
            n += 1
            uid = f"{base}-{n}"

    def scan(self, paths: list | None = None, hash_files: bool = False, prune: bool = True,
             progress: Callable[[int, int], None] | None = None) -> dict:
        """Incremental scan: new/changed (size, mtime) files are re-read, sidecars/XMP reconciled, vanished files
        removed (moved files are re-linked by uid so albums/tags/analysis survive). On a brand-new database the
        album memberships saved in albums.json are re-applied once the images exist."""
        res = self._scan_impl(paths, hash_files, prune, progress)
        if self._albums_pending:
            self._albums_pending = False
            try:
                res["albums_restored"] = self.albums.load_from_disk()
            except Exception as exc:
                self.warnings.append(f"albums.json not fully restored: {exc}")
        return res

    def _scan_impl(self, paths: list | None = None, hash_files: bool = False, prune: bool = True,
                   progress: Callable[[int, int], None] | None = None) -> dict:
        """Incremental scan: new/changed (size, mtime) files are re-read, sidecars/XMP reconciled, vanished files
        removed (moved files are re-linked by uid so albums/tags/analysis survive)."""
        with self._lock:
            t0 = time.time()
            self.warnings = []
            items = self._discover(paths)
            existing = {r["primary_path"]: r for r in self.conn.execute("SELECT * FROM images")}
            stats = {"found": len(items), "added": 0, "updated": 0, "unchanged": 0, "removed": 0, "relinked": 0,
                     "sidecars_read": 0, "xmp_imported": 0, "conflicts": 0}
            seen: set[str] = set()
            missing_by_uid: dict = {}
            scope = None
            if paths:
                scope = [self.rel(self._abs(p)) for p in paths]
            for i, (primary, comp) in enumerate(items):
                rel = self.rel(primary)
                seen.add(rel)
                try:
                    self._upsert(primary, comp, existing.get(rel), stats, hash_files, missing_by_uid, existing, seen)
                except Exception as exc:
                    self.warnings.append(f"{rel}: {type(exc).__name__}: {exc}")
                if progress and (i % 50 == 0 or i == len(items) - 1):
                    progress(i + 1, len(items))
                if i % 200 == 199:
                    self.conn.commit()
            if prune:
                for rel, row in existing.items():
                    if rel in seen:
                        continue
                    if scope is not None and not any(rel == s or rel.startswith(s.rstrip("/") + "/") for s in scope):
                        continue
                    self.conn.execute("DELETE FROM images WHERE id=?", (row["id"],))
                    self.conn.execute("DELETE FROM images_fts WHERE rowid=?", (row["id"],))
                    stats["removed"] += 1
            self.set_meta("last_scan", now_iso())
            self.conn.commit()
            stats["seconds"] = round(time.time() - t0, 3)
            stats["warnings"] = list(self.warnings)
            stats["total"] = self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            return stats

    def _upsert(self, primary: Path, comp: Path | None, row: sqlite3.Row | None, stats: dict, hash_files: bool,
                missing_by_uid: dict, existing: dict, seen: set) -> None:
        st = primary.stat()
        cst = comp.stat() if comp else None
        sc_p, xmp_p = sidecar_file(primary), primary.with_name(primary.name + ".xmp")
        sc_m = sc_p.stat().st_mtime if sc_p.exists() else 0.0
        xmp_m = xmp_p.stat().st_mtime if xmp_p.exists() else 0.0
        media_changed = self._media_changed(row, primary, comp, st, cst)
        sc_changed = row is None or abs(sc_m - (row["sidecar_mtime"] or 0)) > 1e-6
        xmp_changed = row is None or abs(xmp_m - (row["xmp_mtime"] or 0)) > 1e-6
        if row is not None and not (media_changed or sc_changed or xmp_changed):
            stats["unchanged"] += 1
            return
        with self.conn:
            if media_changed:
                md = self._read_meta(primary, comp)
                cols = self._media_columns(primary, comp, md)
                base_uid = self._uid_base(cols)
                if row is None:
                    # moved file? re-link a vanished row with the same uid instead of creating a duplicate
                    old = None
                    cand = self.conn.execute("SELECT * FROM images WHERE uid=?", (base_uid,)).fetchone()
                    if cand is not None and cand["primary_path"] not in seen and not (self.root / cand["primary_path"]).exists():
                        old = cand
                    if old is not None:
                        sets = ", ".join(f"{k}=?" for k in cols)
                        self.conn.execute(f"UPDATE images SET {sets} WHERE id=?", [*cols.values(), old["id"]])
                        image_id = old["id"]
                        existing.pop(old["primary_path"], None)
                        seen.add(old["primary_path"])
                        row = self.conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
                        stats["relinked"] += 1
                    else:
                        uid = self._unique_uid(base_uid)
                        now = now_iso()
                        cols.update({"uid": uid, "added_at": now, "imported_at": now})
                        cur = self.conn.execute(f"INSERT INTO images({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                                                list(cols.values()))
                        image_id = cur.lastrowid
                        stats["added"] += 1
                else:
                    image_id = row["id"]
                    sets = ", ".join(f"{k}=?" for k in cols)
                    self.conn.execute(f"UPDATE images SET {sets} WHERE id=?", [*cols.values(), image_id])
                    stats["updated"] += 1
                if hash_files:
                    self.conn.execute("UPDATE images SET content_hash=?, hash_algo=? WHERE id=?",
                                      (file_hash(primary), "blake3", image_id))
            else:
                image_id = row["id"]
                stats["updated"] += 1
            self._reconcile_doc(image_id, primary, row, sc_p, xmp_p, sc_m, xmp_m, sc_changed, xmp_changed, stats)

    def _media_changed(self, row, primary: Path, comp: Path | None, st, cst) -> bool:
        if row is None:
            return True
        if row["file_size"] != st.st_size or abs((row["file_mtime"] or 0) - st.st_mtime) > 1e-3:
            return True
        is_raw = primary.suffix.lower() in RAW_EXTS
        if is_raw:
            want = self.rel(comp) if comp else None
            if (row["jpg_path"] or None) != want:
                return True
            if comp and (row["jpg_size"] != cst.st_size or abs((row["jpg_mtime"] or 0) - cst.st_mtime) > 1e-3):
                return True
        return False

    def _reconcile_doc(self, image_id: int, primary: Path, row, sc_p: Path, xmp_p: Path, sc_m: float, xmp_m: float,
                       sc_changed: bool, xmp_changed: bool, stats: dict) -> None:
        from .metadata import read_xmp

        sc_exists, xmp_exists = sc_p.exists(), xmp_p.exists()
        doc = None
        if sc_exists:
            try:
                doc = load_doc(primary)
                stats["sidecars_read"] += 1
            except Exception as exc:
                self.warnings.append(f"{sc_p.name}: unreadable sidecar ignored ({exc})")
        use_xmp = xmp_exists and ((not sc_exists) or doc is None or (row is not None and xmp_changed and not sc_changed))
        if xmp_exists and sc_exists and xmp_changed and sc_changed and row is not None:
            stats["conflicts"] += 1
            self.warnings.append(f"{primary.name}: sidecar and XMP both changed; sidecar wins")
        if use_xmp:
            x = read_xmp(xmp_p)
            if doc is None:
                doc = _default_doc(primary.name)
            changed = False
            r = x.get("rating")
            if isinstance(r, int):
                if r < 0:
                    if doc.get("flag") != "reject":
                        doc["flag"], changed = "reject", True
                        doc["flag_source"] = "user"            # changed outside photo-studio: a person's decision
                elif r != doc.get("rating", 0) or doc.get("flag") == "reject" and "reject" not in str(x.get("flags", "")):
                    doc["rating"], changed = r, True
                    if doc.get("flag") == "reject":
                        doc["flag"] = None
                        doc.pop("flag_source", None)
            if "label" in x and norm_label(x["label"]) != doc.get("label"):
                doc["label"], changed = norm_label(x["label"]), True
            flags = str(x.get("flags", ""))
            if "pick" in flags and doc.get("flag") != "pick":
                doc["flag"], changed = "pick", True
                doc["flag_source"] = "user"
            xt = x.get("hierarchical") or x.get("keywords")
            if xt is not None:
                new = sorted({norm_tag(t) for t in xt if norm_tag(t)})
                if new != sorted(doc.get("tags", [])):
                    assign = doc_assignments(doc)
                    assign = {p: a for p, a in assign.items() if a["source"] != "user" and not a["confirmed"]}
                    for p in new:
                        assign[p] = {"source": "user", "confidence": None, "confirmed": 1}
                    store_assignments(doc, assign)
                    changed = True
            if changed or not sc_exists:
                save_doc(primary, doc)
                sc_m = sc_p.stat().st_mtime
                stats["xmp_imported"] += 1
        if doc is None:
            doc = _default_doc(primary.name)
        self._apply_doc(image_id, doc)
        self.conn.execute("UPDATE images SET sidecar_mtime=?, xmp_mtime=? WHERE id=?", (sc_m, xmp_m, image_id))

    # -- doc -> db ------------------------------------------------------------------------------
    def _ensure_tag(self, path: str, auto: bool = False) -> int:
        r = self.conn.execute("SELECT id FROM tags WHERE path=?", (path,)).fetchone()
        if r:
            return r[0]
        parent, cur = None, []
        for part in path.split("|"):
            cur.append(part)
            p = "|".join(cur)
            r = self.conn.execute("SELECT id FROM tags WHERE path=?", (p,)).fetchone()
            if r:
                parent = r[0]
                continue
            c = self.conn.execute("INSERT INTO tags(path, parent_id, name, kind) VALUES(?,?,?,?)",
                                  (p, parent, part, tag_kind(p, auto)))
            parent = c.lastrowid
        return parent

    def _apply_doc(self, image_id: int, doc: dict) -> None:
        ops, has_edits, mode, versions = doc_ops_hash(doc)
        rating = doc.get("rating", 0)
        try:
            rating = max(0, min(5, int(rating)))
        except (TypeError, ValueError):
            rating = 0
        flag = doc.get("flag") if doc.get("flag") in ("pick", "reject") else None
        self.conn.execute(
            "UPDATE images SET rating=?, flag=?, label=?, notes=?, caption=?, mode=?, has_edits=?, ops_hash=? WHERE id=?",
            (rating, flag, norm_label(doc.get("label")), doc.get("notes") or "", doc.get("caption") or "", mode,
             1 if has_edits else 0, ops, image_id))
        assign = doc_assignments(doc)
        self.conn.execute("DELETE FROM image_tags WHERE image_id=?", (image_id,))
        for p, a in assign.items():
            tid = self._ensure_tag(p, auto=str(a["source"]).startswith("auto"))
            self.conn.execute("INSERT OR REPLACE INTO image_tags(image_id, tag_id, source, confidence, confirmed) "
                              "VALUES(?,?,?,?,?)", (image_id, tid, a["source"], a.get("confidence"), a.get("confirmed", 1)))
        self.conn.execute("DELETE FROM edit_versions WHERE image_id=?", (image_id,))
        for v in versions:
            self.conn.execute("INSERT INTO edit_versions(image_id, name, ops_hash, n_ops, mode, art, created_at, modified_at,"
                              " is_default) VALUES(?,?,?,?,?,?,?,?,?)",
                              (image_id, v["name"], v["ops_hash"], v["n_ops"], v["mode"], v["art"], v["created"],
                               v["modified"], 1 if v["is_default"] else 0))
        self._fts_put(image_id, assign)

    def _fts_put(self, image_id: int, assign: dict | None = None) -> None:
        r = self.conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        if r is None:
            return
        if assign is None:
            assign = {x["path"]: 1 for x in self.conn.execute(
                "SELECT t.path FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id=?", (image_id,))}
        tags = " ".join(p.replace("|", " ") for p in assign)
        place = " ".join(p.split("|", 1)[1].replace("|", " ") for p in assign if p.lower().startswith("places|"))
        names = " ".join(Path(x).name for x in (r["raw_path"], r["jpg_path"]) if x)
        self.conn.execute("DELETE FROM images_fts WHERE rowid=?", (image_id,))
        self.conn.execute("INSERT INTO images_fts(rowid, filename, tags, notes, caption, place, camera, lens) "
                          "VALUES(?,?,?,?,?,?,?,?)",
                          (image_id, names, tags, r["notes"] or "", r["caption"] or "", place, r["camera"] or "",
                           r["lens"] or ""))

    def _fts_condition(self, text: str) -> tuple[str, list]:
        if self.fts_enabled:
            q = _fts_query(text)
            if not q:
                return "1=1", []
            return "i.id IN (SELECT rowid FROM images_fts WHERE images_fts MATCH ?)", [q]
        toks = re.findall(r"[\w]+", text, flags=re.UNICODE)
        parts = ["instr(lower(coalesce(f.filename,'')||' '||coalesce(f.tags,'')||' '||coalesce(f.notes,'')||' '||"
                 "coalesce(f.caption,'')||' '||coalesce(f.place,'')||' '||coalesce(f.camera,'')||' '||coalesce(f.lens,'')), ?) > 0"
                 for _ in toks]
        return ("i.id IN (SELECT rowid FROM images_fts f WHERE " + " AND ".join(parts) + ")" if parts else "1=1"), \
            [t.lower() for t in toks]

    # -- album helpers used by the rule compiler --------------------------------------------------
    def resolve_album_id(self, name: Any) -> int | None:
        return self.albums.resolve(name)

    def album_membership_sql(self, album_id: int) -> tuple[str, list]:
        return self.albums.membership_sql(album_id)

    # -- querying ---------------------------------------------------------------------------------
    def _where(self, flt: Any) -> tuple[str, list]:
        return compile_rule(filter_to_rule(flt), self)

    def images(self, filter: Any = None, sort: str = "taken_at", order: str = "asc", limit: int | None = None,
               offset: int = 0, with_tags: bool = True) -> list[dict]:
        """Query images. `filter` is a dict of simple keys (rating, flag, label, tags{any,all,none}, date_from,
        date_to, camera, lens, focal_min/max, iso_min/max, aperture_min/max, mode, genre, has_edits, text,
        album, burst, scene, person, place, folder ...), a rule tree, or a DSL string."""
        if isinstance(filter, dict):
            sort = filter.get("sort", sort)
            order = filter.get("order", order)
            limit = filter.get("limit", limit)
            offset = filter.get("offset", offset)
        where, params = self._where(filter)
        col = csql.SORT_COLUMNS.get(str(sort))
        if col is None:
            raise RuleError(f"cannot sort by '{sort}' ({', '.join(sorted(csql.SORT_COLUMNS))})")
        direction = "DESC" if str(order).lower().startswith("d") else "ASC"
        sql = (f"SELECT i.* FROM images i LEFT JOIN analysis a ON a.image_id = i.id WHERE {where} "
               f"ORDER BY {col} {direction}, i.id ASC")
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset or 0)}"
        elif offset:
            sql += f" LIMIT -1 OFFSET {int(offset)}"
        rows = [self._row_dict(r) for r in self.conn.execute(sql, params)]
        if with_tags:
            self._attach_tags(rows)
        return rows

    def count(self, filter: Any = None) -> int:
        where, params = self._where(filter)
        return self.conn.execute(f"SELECT COUNT(*) FROM images i LEFT JOIN analysis a ON a.image_id=i.id WHERE {where}",
                                 params).fetchone()[0]

    def ids(self, filter: Any = None, sort: str = "taken_at", order: str = "asc") -> list[int]:
        return [r["id"] for r in self.images(filter, sort, order, with_tags=False)]

    def _attach_tags(self, rows: list[dict]) -> None:
        by_id = {r["id"]: r for r in rows}
        for r in rows:
            r["tags"] = []
        ids = list(by_id)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for t in self.conn.execute(f"SELECT it.image_id, t.path, it.source, it.confidence, it.confirmed FROM image_tags it "
                                       f"JOIN tags t ON t.id=it.tag_id WHERE it.image_id IN ({q}) ORDER BY t.path", chunk):
                by_id[t["image_id"]]["tags"].append(t["path"])

    def get(self, image: Any) -> dict:
        r = self._row(image)
        d = self._row_dict(r)
        d["tag_details"] = [dict(t) for t in self.conn.execute(
            "SELECT t.path, it.source, it.confidence, it.confirmed FROM image_tags it JOIN tags t ON t.id=it.tag_id "
            "WHERE it.image_id=? ORDER BY t.path", (r["id"],))]
        d["tags"] = [t["path"] for t in d["tag_details"]]
        d["analysis"] = self.get_analysis(r["id"])
        d["versions"] = [dict(v) for v in self.conn.execute("SELECT * FROM edit_versions WHERE image_id=?", (r["id"],))]
        d["albums"] = [x["path"] for x in self.conn.execute(
            "SELECT c.path FROM collection_images ci JOIN collections c ON c.id=ci.collection_id WHERE ci.image_id=?",
            (r["id"],))]
        return d

    def search(self, text: str, filter: Any = None, limit: int = 200) -> list[dict]:
        """Full-text search over filename / tags / notes / caption / place / camera / lens (prefix match, AND)."""
        q = _fts_query(text)
        if not q:
            return []
        where, params = self._where(filter)
        if self.fts_enabled:
            sql = ("SELECT i.* FROM images i JOIN (SELECT rowid AS rid, rank FROM images_fts WHERE images_fts MATCH ?) f "
                   f"ON f.rid = i.id LEFT JOIN analysis a ON a.image_id = i.id WHERE {where} ORDER BY f.rank LIMIT ?")
            rows = self.conn.execute(sql, [q, *params, limit]).fetchall()
        else:
            cond, cp = self._fts_condition(text)
            rows = self.conn.execute(f"SELECT i.* FROM images i LEFT JOIN analysis a ON a.image_id=i.id WHERE {cond} AND "
                                     f"{where} ORDER BY i.taken_ts LIMIT ?", [*cp, *params, limit]).fetchall()
        out = [self._row_dict(r) for r in rows]
        self._attach_tags(out)
        return out

    # -- writing: sidecar + XMP first, DB second ------------------------------------------------------
    def _commit(self, image_id: int, mutate: Callable[[dict], None]) -> bool:
        """Load the sidecar, apply `mutate(doc)`, write sidecar + XMP (only if something changed), mirror to DB."""
        r = self.conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        primary = self.root / r["primary_path"]
        doc = load_doc(primary)
        before = json.dumps(doc, sort_keys=True, default=str)
        mutate(doc)
        sc_p = sidecar_file(primary)
        changed = json.dumps(doc, sort_keys=True, default=str) != before
        if changed:
            save_doc(primary, doc)
        if changed or sc_p.exists():
            self._write_xmps(r, doc)
        xp = primary.with_name(primary.name + ".xmp")
        with self.conn:
            self._apply_doc(image_id, doc)
            self.conn.execute("UPDATE images SET sidecar_mtime=?, xmp_mtime=? WHERE id=?",
                              (sc_p.stat().st_mtime if sc_p.exists() else 0.0, xp.stat().st_mtime if xp.exists() else 0.0,
                               image_id))
        return changed

    def _xmp_state(self, doc: dict) -> dict:
        assign = doc_assignments(doc)
        confirmed = sorted(p for p, a in assign.items() if a["confirmed"])
        flag = doc.get("flag")
        rating = int(doc.get("rating", 0) or 0)
        return {"rating": -1 if flag == "reject" else rating, "label": norm_label(doc.get("label")) or "",
                "keywords": sorted({p.split("|")[-1] for p in confirmed}), "hierarchical": confirmed,
                "place": _place_from_tags(confirmed), "flags": [flag] if flag else []}

    def _write_xmps(self, r, doc: dict) -> None:
        from . import metadata as M

        primary = self.root / r["primary_path"]
        st = self._xmp_state(doc)
        xp = M.xmp_path(primary)
        cur = M.read_xmp(xp) if xp.exists() else {}
        same = (cur.get("rating") == st["rating"] and (cur.get("label") or "") == st["label"]
                and sorted(cur.get("keywords", [])) == st["keywords"] and sorted(cur.get("hierarchical", [])) == st["hierarchical"]
                and set(filter(None, str(cur.get("flags", "")).split(","))) == set(st["flags"])
                and all(cur.get(k) == v for k, v in (("country", st["place"].get("country")),
                                                     ("state", st["place"].get("state")),
                                                     ("city", st["place"].get("city")))))
        if not same or not xp.exists():
            M.write_xmp(primary, rating=st["rating"], label=st["label"], keywords=st["keywords"],
                        hierarchical=st["hierarchical"], place=st["place"], flags=st["flags"], merge=False)
        targets = []
        if r["jpg_path"] and r["jpg_path"] != r["primary_path"]:
            targets.append(self.root / r["jpg_path"])
        elif primary.suffix.lower() in JPEG_EXTS:
            targets.append(primary)
        for jp in targets:
            done = False
            if M.exiftool_available():
                try:
                    done = M.write_jpeg_metadata(jp, rating=st["rating"], keywords=st["keywords"],
                                                 hierarchical=st["hierarchical"])
                except Exception as exc:
                    self.warnings.append(f"in-file metadata for {jp.name} failed: {exc}")
            if not done and jp != primary:
                jx = M.xmp_path(jp)
                M.write_xmp(jp, rating=st["rating"], label=st["label"], keywords=st["keywords"],
                            hierarchical=st["hierarchical"], place=st["place"], flags=st["flags"], merge=False)
                _ = jx

    def _each(self, target: Any, mutate: Callable[[dict], None]) -> dict:
        with self._lock:
            self.warnings = []
            ids = self._ids(target)
            n = 0
            for i in ids:
                n += 1 if self._commit(i, mutate) else 0
            return {"images": len(ids), "changed": n, "warnings": list(self.warnings)}

    def set_rating(self, target: Any, rating: int) -> dict:
        rating = int(rating)
        if not 0 <= rating <= 5:
            raise ValueError("rating must be 0..5 (use set_flag('reject') for rejects)")
        return self._each(target, lambda d: d.__setitem__("rating", rating))

    def set_flag(self, target: Any, flag: str | None, source: str = "user") -> dict:
        """Set pick / reject / none. ``source`` is recorded in the sidecar as ``flag_source`` ("user" for anything a
        person did, "cull" for ``photo cull --apply``) so automatic culling never overwrites a manual decision."""
        f = None if flag is None or str(flag).lower() in ("", "none", "null", "unflag", "clear") else str(flag).lower()
        f = {"picked": "pick", "keep": "pick", "rejected": "reject"}.get(f, f)
        if f not in (None, "pick", "reject"):
            raise ValueError("flag must be pick, reject or none")

        def m(d):
            d["flag"] = f
            if f is None:
                d.pop("flag_source", None)
            else:
                d["flag_source"] = source
        return self._each(target, m)

    def flag_source(self, image: Any) -> str | None:
        """Who set the current flag: "user", "cull", or None when unflagged. A flag without a recorded source (older
        sidecars, flags written by other tools) counts as "user"."""
        i = self.find(image)
        if i is None:
            return None
        r = self.conn.execute("SELECT primary_path, flag FROM images WHERE id=?", (i,)).fetchone()
        if r is None or not r["flag"]:
            return None
        return str(load_doc(self.root / r["primary_path"]).get("flag_source") or "user")

    def set_label(self, target: Any, label: str | None) -> dict:
        lab = norm_label(label)
        return self._each(target, lambda d: d.__setitem__("label", lab))

    def set_notes(self, target: Any, notes: str) -> dict:
        return self._each(target, lambda d: d.__setitem__("notes", notes))

    def set_caption(self, target: Any, caption: str) -> dict:
        def m(d):
            if caption:
                d["caption"] = caption
            else:
                d.pop("caption", None)
        return self._each(target, m)

    def add_tags(self, target: Any, tags: list[str] | str, source: str = "user", confidence: float | None = None,
                 confirmed: bool | None = None) -> dict:
        tl = [norm_tag(t) for t in ([tags] if isinstance(tags, str) else tags)]
        tl = [t for t in tl if t]
        if confirmed is None:
            confirmed = source == "user" or AUTO_CONFIRMED_SOURCES.get(source, False)

        def m(doc):
            assign = doc_assignments(doc)
            for t in tl:
                cur = assign.get(t)
                if source == "user":
                    assign[t] = {"source": "user", "confidence": None, "confirmed": 1}
                elif cur is None or (cur["source"] != "user" and not (cur["confirmed"] and not confirmed)):
                    assign[t] = {"source": source, "confidence": confidence, "confirmed": 1 if confirmed else 0}
            store_assignments(doc, assign)
        return self._each(target, m)

    def remove_tags(self, target: Any, tags: list[str] | str, recursive: bool = False) -> dict:
        tl = [norm_tag(t) for t in ([tags] if isinstance(tags, str) else tags)]

        def m(doc):
            assign = doc_assignments(doc)
            for t in tl:
                for p in list(assign):
                    if p == t or (recursive and p.startswith(t + "|")):
                        del assign[p]
            store_assignments(doc, assign)
        return self._each(target, m)

    def confirm_tags(self, target: Any, tags: list[str] | None = None, confirm: bool = True) -> dict:
        tl = {norm_tag(t) for t in tags} if tags else None

        def m(doc):
            assign = doc_assignments(doc)
            for p, a in assign.items():
                if str(a["source"]).startswith("auto") and (tl is None or any(p == t or p.startswith(t + "|") for t in tl)):
                    a["confirmed"] = 1 if confirm else 0
            store_assignments(doc, assign)
        return self._each(target, m)

    def set_tags(self, image_id: int, assign: dict[str, dict]) -> bool:
        """Replace the whole tag assignment of one image (used by tag tree operations)."""
        return self._commit(image_id, lambda d: store_assignments(d, assign))

    def get_assignments(self, image_id: int) -> dict[str, dict]:
        return {t["path"]: {"source": t["source"], "confidence": t["confidence"], "confirmed": t["confirmed"]}
                for t in self.conn.execute("SELECT t.path, it.source, it.confidence, it.confirmed FROM image_tags it "
                                           "JOIN tags t ON t.id=it.tag_id WHERE it.image_id=?", (image_id,))}

    def rewrite_sidecars(self, ids: list[int] | None = None) -> int:
        """Re-emit XMP (and companion-JPEG metadata) for images from their sidecars (`tags push`)."""
        n = 0
        for i in (ids if ids is not None else self.ids()):
            r = self.conn.execute("SELECT * FROM images WHERE id=?", (i,)).fetchone()
            doc = load_doc(self.root / r["primary_path"])
            self._write_xmps(r, doc)
            n += 1
        return n

    # -- tags -------------------------------------------------------------------------------------
    def list_tags(self, prefix: str | None = None, tree: bool = False, confirmed_only: bool = False) -> list[dict]:
        extra = " AND it.confirmed = 1" if confirmed_only else ""
        rows = self.conn.execute(
            "SELECT t.id, t.path, t.name, t.kind, t.parent_id, "
            f"(SELECT COUNT(*) FROM image_tags it WHERE it.tag_id = t.id{extra}) AS direct FROM tags t ORDER BY t.path").fetchall()
        items = []
        for r in rows:
            if prefix and not (r["path"] == prefix or r["path"].startswith(prefix.rstrip("|") + "|")):
                continue
            total = self.conn.execute(
                "SELECT COUNT(DISTINCT it.image_id) FROM image_tags it JOIN tags t ON t.id=it.tag_id "
                f"WHERE (t.path=? OR substr(t.path,1,?)=?){extra}", (r["path"], len(r["path"]) + 1, r["path"] + "|")).fetchone()[0]
            items.append({"path": r["path"], "name": r["name"], "kind": r["kind"], "count": r["direct"], "total": total})
        if not tree:
            return items
        nodes = {i["path"]: {**i, "children": []} for i in items}
        roots = []
        for p, n in nodes.items():
            parent = p.rsplit("|", 1)[0] if "|" in p else None
            (nodes[parent]["children"] if parent in nodes else roots).append(n)
        return roots

    def prune_tags(self) -> int:
        """Delete tags with no images and no children."""
        n = 0
        while True:
            c = self.conn.execute(
                "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM image_tags) AND id NOT IN "
                "(SELECT parent_id FROM tags WHERE parent_id IS NOT NULL)").rowcount
            n += c
            if not c:
                break
        self.conn.commit()
        return n

    # -- rebuild ----------------------------------------------------------------------------------
    def rebuild(self, keep_analysis: bool = True, hash_files: bool = False) -> dict:
        """Delete the database and rebuild it from sidecars + XMP (+ albums.json). Analysis is carried over."""
        with self._lock:
            saved = []
            if keep_analysis:
                for r in self.conn.execute("SELECT i.primary_path, a.* FROM analysis a JOIN images i ON i.id=a.image_id"):
                    saved.append(dict(r))
            try:
                self.albums.persist()
            except Exception:
                pass
            self.close()
            for suffix in ("", "-wal", "-shm"):
                Path(str(self.db_path) + suffix).unlink(missing_ok=True)
            self._albums = None
            self._open()
            stats = self.scan(hash_files=hash_files)
            restored = 0
            with self.conn:
                for a in saved:
                    i = self.find(a["primary_path"])
                    if i is None:
                        continue
                    a = {k: v for k, v in a.items() if k != "primary_path"}
                    a["image_id"] = i
                    cols = list(a)
                    self.conn.execute(f"INSERT OR REPLACE INTO analysis({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                                      list(a.values()))
                    restored += 1
            stats["analysis_restored"] = restored
            return stats
