"""Library backend for the web UI: the SQLite catalog (``photostudio.catalog``) when it is available, else a plain
folder listing built from the files and their ``.ps.json`` sidecars. Both return the same item dicts, so the
frontend never cares which one it talks to. All methods are thread safe (one lock around the catalog)."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from .. import sidecar
from ..util import IMAGE_EXTS, JPEG_EXTS, RAW_EXTS
from . import paths as P

log = logging.getLogger("photostudio.web")

COLOR_LABELS = ["red", "yellow", "green", "blue", "purple"]
ITEM_KEYS = ("id", "uid", "kind", "taken_at", "width", "height", "camera", "lens", "iso", "shutter", "aperture", "focal",
             "focal35", "rating", "flag", "label", "notes", "mode", "has_edits", "ops_hash", "burst_id", "scene_id",
             "burst_rank", "file_size", "file_mtime", "genre")


class LibraryError(ValueError):
    pass


def _label_norm(label: str | None) -> str | None:
    if label is None or str(label).strip().lower() in ("", "none", "null"):
        return None
    s = str(label).strip()
    return s[:1].upper() + s[1:].lower() if s.lower() in COLOR_LABELS else s


class Library:
    """Facade: ``Library(root)`` picks the catalog when importable and openable."""

    def __init__(self, root: Path, use_catalog: bool = True):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.cat = None
        self.catalog_error: str | None = None
        self.scanning = False
        self._scan_thread: threading.Thread | None = None
        self._scan_done = threading.Event()
        self._folder_scans: dict[str, float] = {}
        self.last_scan_stats: dict = {}
        if use_catalog:
            try:
                from ..catalog import Catalog

                self.cat = Catalog(self.root, create=True)
            except Exception as exc:  # noqa: BLE001
                self.catalog_error = f"{type(exc).__name__}: {exc}"
                log.warning("catalog unavailable, falling back to plain folder listing: %s", exc)
                self.cat = None
        if self.cat is None:
            self._scan_done.set()

    @property
    def has_catalog(self) -> bool:
        return self.cat is not None

    # ------------------------------------------------------------------ scanning
    def start_scan(self, wait: bool = False, paths: list | None = None, prune: bool = True) -> None:
        if self.cat is None:
            return
        if self._scan_thread and self._scan_thread.is_alive():
            if wait:
                self._scan_thread.join()
            return
        self._scan_done.clear()
        self.scanning = True

        def run():
            try:
                with self.lock:
                    self.last_scan_stats = self.cat.scan(paths, prune=prune)
            except Exception as exc:  # noqa: BLE001
                log.warning("catalog scan failed: %s", exc)
                self.last_scan_stats = {"error": str(exc)}
            finally:
                self.scanning = False
                self._scan_done.set()

        self._scan_thread = threading.Thread(target=run, name="ps-scan", daemon=True)
        self._scan_thread.start()
        if wait:
            self._scan_thread.join()

    def ensure_ready(self, timeout: float = 30.0) -> None:
        """First call on an empty catalog waits for the initial scan; later calls return at once."""
        if self.cat is None:
            return
        with self.lock:
            empty = self.cat.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
        if empty and not self._scan_done.is_set():
            self._scan_done.wait(timeout)

    def refresh_folder(self, folder: str, min_interval: float = 5.0, force: bool = False) -> None:
        """Cheap incremental rescan of one folder (new files appear, vanished ones are pruned)."""
        if self.cat is None:
            return
        now = time.time()
        if not force and now - self._folder_scans.get(folder, 0) < min_interval:
            return
        self._folder_scans[folder] = now
        if self.scanning:
            return
        try:
            with self.lock:
                self.cat.scan([folder] if folder else None, prune=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("folder rescan failed: %s", exc)

    def touch(self, rel: str) -> None:
        """Re-read one image's sidecar into the catalog (after the web app or the CLI changed it)."""
        if self.cat is None:
            return
        try:
            with self.lock:
                self.cat.scan([rel], prune=False)
        except Exception as exc:  # noqa: BLE001
            log.debug("touch %s failed: %s", rel, exc)

    # ------------------------------------------------------------------ items
    def _item_from_row(self, r: dict) -> dict:
        d = {k: r.get(k) for k in ITEM_KEYS}
        d["p"] = r["primary_path"]
        d["name"] = r.get("filename") or Path(r["primary_path"]).name
        d["dir"] = r.get("dir") or ""
        d["tags"] = r.get("tags") or []
        d["has_edits"] = bool(r.get("has_edits"))
        d["raw"] = r.get("raw_path")
        d["jpg"] = r.get("jpg_path")
        return d

    def _plain_item(self, rel: str) -> dict:
        full = self.root / rel
        st = full.stat()
        try:
            doc = sidecar.load(full)
        except Exception:  # noqa: BLE001
            doc = sidecar.default_doc(full)
        stack = sidecar.get_stack(doc)
        ext = full.suffix.lower()
        kind = "raw" if ext in RAW_EXTS else "jpeg"
        return {"id": rel, "uid": rel, "p": rel, "name": full.name, "dir": Path(rel).parent.as_posix().replace(".", ""),
                "kind": kind, "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                "rating": int(doc.get("rating") or 0), "flag": doc.get("flag"), "label": doc.get("label"),
                "tags": list(doc.get("tags") or []), "notes": doc.get("notes") or "", "has_edits": bool(stack),
                "mode": doc["versions"][doc["active"]].get("mode"), "ops_hash": sidecar.ops_hash(stack) if stack else "",
                "file_size": st.st_size, "file_mtime": st.st_mtime, "raw": rel if kind == "raw" else None,
                "jpg": rel if kind != "raw" else None, "width": None, "height": None, "camera": None, "lens": None,
                "iso": None, "shutter": None, "aperture": None, "focal": None, "burst_id": None, "scene_id": None,
                "burst_rank": None}

    def _plain_files(self, folder: str, recursive: bool) -> list[str]:
        base = P.safe_dir(self.root, folder)
        out: list[str] = []
        it = os.walk(base) if recursive else [(str(base), [], [e.name for e in os.scandir(base) if e.is_file()])]
        for dp, dns, fns in it:
            if recursive:
                dns[:] = [d for d in sorted(dns) if not d.startswith(".") and d not in P.HIDDEN_DIRS]
            stems_raw = {Path(f).stem.lower() for f in fns if Path(f).suffix.lower() in RAW_EXTS}
            for fn in sorted(fns):
                ext = Path(fn).suffix.lower()
                if fn.startswith(".") or ext not in (RAW_EXTS | IMAGE_EXTS):
                    continue
                if ext in JPEG_EXTS and Path(fn).stem.lower() in stems_raw:
                    continue  # RAW+JPG pair: the raw is the primary
                out.append((Path(dp) / fn).relative_to(self.root).as_posix())
        return out

    def get_item(self, rel: str) -> dict:
        rel = P.to_rel(self.root, P.resolve_image(self.root, rel))
        if self.cat is not None:
            with self.lock:
                try:
                    iid = self.cat.find(rel)
                    if iid is None:
                        self.cat.scan([rel], prune=False)
                        iid = self.cat.find(rel)
                    if iid is not None:
                        row = self.cat.get(iid)
                        item = self._item_from_row(row)
                        item["albums"] = row.get("albums", [])
                        item["tag_details"] = row.get("tag_details", [])
                        item["analysis"] = {k: v for k, v in (row.get("analysis") or {}).items()
                                            if k not in ("clip_emb", "data") and not isinstance(v, (bytes, bytearray))}
                        return item
                except Exception as exc:  # noqa: BLE001
                    log.debug("catalog get failed for %s: %s", rel, exc)
        return self._plain_item(rel)

    # ------------------------------------------------------------------ listing
    def list_images(self, f: dict) -> dict:
        folder = P.clean_rel(f.get("folder") or "")
        recursive = bool(f.get("recursive", False))
        limit = int(f.get("limit") or 2000)
        offset = int(f.get("offset") or 0)
        sort = f.get("sort") or "name"
        order = f.get("order") or "asc"
        if self.cat is not None:
            self.ensure_ready()
            if folder or not f.get("album"):
                self.refresh_folder(folder)
            cf: dict[str, Any] = {}
            if folder and not f.get("album"):
                if recursive:
                    cf["folder"] = folder
                else:
                    cf["folder"] = folder
            if f.get("album"):
                cf["album"] = f["album"]            # "id:N" (what the UI sends), a name or a path: same rules as the CLI
            for k_in, k_out in (("rating_min", "rating_min"), ("flag", "flag"), ("label", "label"), ("mode", "mode"),
                                ("date_from", "date_from"), ("date_to", "date_to"), ("camera", "camera"), ("lens", "lens")):
                if f.get(k_in) not in (None, "", "any"):
                    cf[k_out] = f[k_in]
            if f.get("edited") in (True, False, "true", "false", "1", "0", 1, 0):
                cf["has_edits"] = str(f["edited"]).lower() in ("true", "1")
            if f.get("tag"):
                cf["tags_all"] = [t for t in str(f["tag"]).split(",") if t.strip()]
            if f.get("q"):
                cf["text"] = f["q"]
            if f.get("issue"):
                cf["issue"] = f["issue"]
            with self.lock:
                try:
                    rows = self.cat.images(cf, sort=sort if sort != "date" else "taken_at", order=order, with_tags=True)
                except Exception as exc:  # noqa: BLE001  e.g. bad rule values
                    raise LibraryError(str(exc)) from exc
            if folder and not recursive and not f.get("album"):
                rows = [r for r in rows if (r.get("dir") or "") == folder]
            if folder.split("/")[0] not in P.HIDDEN_DIRS:      # our own export outputs are not part of the library view
                rows = [r for r in rows if (r.get("dir") or "").split("/")[0] not in P.HIDDEN_DIRS]
            total = len(rows)
            items = [self._item_from_row(r) for r in rows[offset:offset + limit]]
            return {"items": items, "total": total, "catalog": True, "scanning": self.scanning}
        # ---- plain fallback
        files = self._plain_files(folder, recursive)
        items = []
        q = (f.get("q") or "").lower()
        for rel in files:
            try:
                it = self._plain_item(rel)
            except OSError:
                continue
            if f.get("rating_min") and it["rating"] < int(f["rating_min"]):
                continue
            if f.get("flag") not in (None, "", "any"):
                want = None if f["flag"] in ("none", "unflagged") else f["flag"]
                if it["flag"] != want:
                    continue
            if f.get("label") not in (None, "", "any") and (it["label"] or "").lower() != str(f["label"]).lower():
                continue
            if f.get("edited") in (True, "true", "1", 1) and not it["has_edits"]:
                continue
            if f.get("edited") in (False, "false", "0", 0) and it["has_edits"]:
                continue
            if f.get("tag") and not all(any(t.lower() in x.lower() for x in it["tags"]) for t in str(f["tag"]).split(",")):
                continue
            if q and q not in it["name"].lower() and not any(q in t.lower() for t in it["tags"]):
                continue
            items.append(it)
        keyf = {"rating": lambda i: i["rating"], "date": lambda i: i["file_mtime"], "taken_at": lambda i: i["file_mtime"]}.get(
            sort, lambda i: i["name"].lower())
        items.sort(key=keyf, reverse=str(order).startswith("d"))
        return {"items": items[offset:offset + limit], "total": len(items), "catalog": False, "scanning": False}

    def folders(self) -> list[dict]:
        return P.walk_dirs(self.root)

    # ------------------------------------------------------------------ metadata writes
    def set_meta(self, rels: list[str], rating: int | None = None, flag: Any = ..., label: Any = ...,
                 add_tags: list[str] | None = None, remove_tags: list[str] | None = None, notes: str | None = None) -> dict:
        rels = [P.to_rel(self.root, P.resolve_image(self.root, r)) for r in rels]
        if rating is not None and not 0 <= int(rating) <= 5:
            raise LibraryError("rating must be 0..5")
        if flag is not ... and flag not in (None, "pick", "reject", "", "none"):
            raise LibraryError("flag must be pick, reject or null")
        if self.cat is not None:
            with self.lock:
                for r in rels:  # make sure the catalog knows the file
                    if self.cat.find(r) is None:
                        self.cat.scan([r], prune=False)
                try:
                    if rating is not None:
                        self.cat.set_rating(rels, int(rating))
                    if flag is not ...:
                        self.cat.set_flag(rels, None if flag in (None, "", "none") else flag)
                    if label is not ...:
                        self.cat.set_label(rels, label)
                    if add_tags:
                        self.cat.add_tags(rels, add_tags)
                    if remove_tags:
                        self.cat.remove_tags(rels, remove_tags)
                    if notes is not None:
                        self.cat.set_notes(rels, notes)
                except (KeyError, ValueError) as exc:
                    raise LibraryError(str(exc)) from exc
        else:
            for r in rels:
                full = self.root / r
                doc = sidecar.load(full)
                if rating is not None:
                    doc["rating"] = int(rating)
                if flag is not ...:
                    doc["flag"] = None if flag in (None, "", "none") else flag
                if label is not ...:
                    doc["label"] = _label_norm(label)
                tags = list(doc.get("tags") or [])
                for t in add_tags or []:
                    if t not in tags:
                        tags.append(t)
                tags = [t for t in tags if t not in (remove_tags or [])]
                doc["tags"] = tags
                if notes is not None:
                    doc["notes"] = notes
                sidecar.save(full, doc)
        return {"updated": rels}

    # ------------------------------------------------------------------ tags & albums
    def tags(self) -> list[dict]:
        if self.cat is None:
            seen: dict[str, int] = {}
            for f in self._plain_files("", True)[:3000]:
                try:
                    for t in sidecar.load(self.root / f).get("tags", []):
                        seen[t] = seen.get(t, 0) + 1
                except Exception:  # noqa: BLE001
                    pass
            return [{"path": k, "name": k.split("|")[-1], "count": v} for k, v in sorted(seen.items())]
        with self.lock:
            return self.cat.list_tags()

    def albums(self) -> list[dict]:
        if self.cat is None:
            return []
        with self.lock:
            return self.cat.albums.list(tree=False)

    def album_create(self, name: str, kind: str = "static", rule: Any = None, parent: Any = None, images: list | None = None) -> dict:
        self._need_catalog("albums")
        with self.lock:
            try:
                return self.cat.albums.create(name, parent=parent, kind=kind, rule=rule, images=images)
            except (KeyError, ValueError) as exc:
                raise LibraryError(str(exc)) from exc

    def album_images(self, album: Any, rels: list[str], action: str) -> dict:
        self._need_catalog("albums")
        rels = [P.to_rel(self.root, P.resolve_image(self.root, r)) for r in rels]
        with self.lock:
            try:
                n = self.cat.albums.add(album, rels) if action == "add" else self.cat.albums.remove(album, rels)
            except (KeyError, ValueError) as exc:
                raise LibraryError(str(exc)) from exc
        return {"changed": n}

    def album_delete(self, album: Any) -> dict:
        self._need_catalog("albums")
        with self.lock:
            try:
                return {"deleted": self.cat.albums.delete(album)}
            except (KeyError, ValueError) as exc:
                raise LibraryError(str(exc)) from exc

    def album_rename(self, album: Any, name: str) -> dict:
        self._need_catalog("albums")
        with self.lock:
            try:
                return self.cat.albums.rename(album, name)
            except (KeyError, ValueError) as exc:
                raise LibraryError(str(exc)) from exc

    def _need_catalog(self, what: str) -> None:
        if self.cat is None:
            raise LibraryError(f"{what} need the catalog (unavailable: {self.catalog_error or 'disabled'})")

    # ------------------------------------------------------------------ cull analysis
    def analysis(self, rel: str) -> dict | None:
        if self.cat is None:
            return None
        with self.lock:
            try:
                iid = self.cat.find(rel)
                a = self.cat.get_analysis(iid, fresh_only=False) if iid is not None else None
            except Exception:  # noqa: BLE001
                return None
        if not a:
            return None
        return {k: v for k, v in a.items() if k not in ("clip_emb", "data") and not isinstance(v, (bytes, bytearray))}

    def stats(self) -> dict:
        if self.cat is None:
            return {"catalog": False}
        with self.lock:
            return {"catalog": True, **self.cat.stats()}

    def close(self) -> None:
        if self.cat is not None:
            with self.lock:
                self.cat.close()
