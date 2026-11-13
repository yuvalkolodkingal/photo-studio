"""Albums: static (explicit, ordered) and smart (rule JSON compiled safely to SQL), hierarchical via a
materialised path (``trips/2026/eilat``), plus album export (folder / symlinks / rendered / zip) and a Pillow
contact sheet. The HTML gallery lives in `gallery.py`.

Albums are virtual: files never move to express membership. Because albums have no sidecar to live in, every
change is mirrored to ``<root>/.photostudio/albums.json`` (members stored by relative path) so a catalog rebuild
restores them, and `catalog export` ships them as plain JSON.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from .catalog_sql import RuleError, compile_rule, parse_query
from .util import dumps


def now_iso() -> str:
    from .catalog import now_iso as n

    return n()


def coerce_rule(rule: Any) -> dict:
    """dict / JSON text / DSL text -> rule tree ({} = match everything)."""
    if rule in (None, "", {}):
        return {}
    if isinstance(rule, str):
        s = rule.strip()
        if s.startswith("{") or s.startswith("["):
            rule = json.loads(s)
        else:
            return parse_query(s)
    if isinstance(rule, list):
        return {"all": rule}
    if not isinstance(rule, dict):
        raise RuleError(f"bad smart-album rule: {rule!r}")
    return rule


class AlbumManager:
    def __init__(self, cat):
        self.cat = cat

    @property
    def conn(self):
        return self.cat.conn

    # ------------------------------------------------------------------------------------ lookup
    def resolve(self, album: Any) -> int | None:
        """Album id for an id (Python ``int``), an explicit ``"id:N"`` string, or a name / ``Parent/Child`` path.
        A bare digit string such as ``"2024"`` is always a *name* (albums are commonly named after years); write
        ``id:2024`` to address an album by its number."""
        if isinstance(album, dict):
            album = album.get("id", album.get("path"))
        if isinstance(album, str) and album.strip().lower().startswith("id:"):
            try:
                album = int(album.strip()[3:])
            except ValueError:
                raise ValueError(f"bad album id: {album!r} (expected id:N)") from None
        if isinstance(album, int) and not isinstance(album, bool):
            r = self.conn.execute("SELECT id FROM collections WHERE id=?", (album,)).fetchone()
            return r[0] if r else None
        s = str(album).strip().strip("/")
        r = self.conn.execute("SELECT id FROM collections WHERE path=?", (s,)).fetchone()
        if r:
            return r[0]
        rows = self.conn.execute("SELECT id FROM collections WHERE lower(path)=lower(?) OR lower(name)=lower(?)",
                                 (s, s)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        if len(rows) > 1:
            raise ValueError(f"album name '{album}' is ambiguous; use the full path")
        return None

    def _row(self, album: Any):
        i = self.resolve(album)
        if i is None:
            raise KeyError(f"no such album: {album}")
        return self.conn.execute("SELECT * FROM collections WHERE id=?", (i,)).fetchone()

    def membership_sql(self, album_id: int, include_children: bool = False) -> tuple[str, list]:
        """Boolean SQL over alias `i`: is the image in this album? Smart albums compile their rule and (as a
        refinement) also intersect with a non-empty static parent or a smart parent."""
        r = self.conn.execute("SELECT * FROM collections WHERE id=?", (album_id,)).fetchone()
        if r is None:
            return "0=1", []
        if r["kind"] == "static":
            parts = [("i.id IN (SELECT image_id FROM collection_images WHERE collection_id=?)", [album_id])]
        else:
            rule = json.loads(r["query_json"] or "{}")
            sql, params = compile_rule(rule, self.cat)
            if r["parent_id"]:
                p = self.conn.execute("SELECT * FROM collections WHERE id=?", (r["parent_id"],)).fetchone()
                inherit = p is not None and (p["kind"] == "smart" or self.conn.execute(
                    "SELECT 1 FROM collection_images WHERE collection_id=? LIMIT 1", (p["id"],)).fetchone())
                if inherit:
                    psql, pp = self.membership_sql(p["id"])
                    sql, params = f"({sql}) AND ({psql})", [*params, *pp]
            parts = [(f"({sql})", params)]
        if include_children:
            for c in self.conn.execute("SELECT id FROM collections WHERE parent_id=?", (album_id,)).fetchall():
                parts.append(self.membership_sql(c["id"], True))
        sql = " OR ".join(f"({s})" for s, _ in parts)
        return f"({sql})", [p for _, ps in parts for p in ps]

    # ------------------------------------------------------------------------------------ CRUD
    def _path_for(self, name: str, parent_id: int | None) -> str:
        if "/" in name or not name.strip():
            raise ValueError("album names must be non-empty and must not contain '/' (use parent= for hierarchy)")
        if parent_id is None:
            return name.strip()
        pp = self.conn.execute("SELECT path FROM collections WHERE id=?", (parent_id,)).fetchone()[0]
        return f"{pp}/{name.strip()}"

    def create(self, name: str, parent: Any = None, kind: str = "static", rule: Any = None, description: str = "",
               sort: str = "taken_at", images: list | None = None, exist_ok: bool = False) -> dict:
        if "/" in name and parent is None:  # 'trips/2026/eilat' creates the chain
            *anc, leaf = [p for p in name.split("/") if p]
            pid = None
            for a in anc:
                pid = (self.resolve(f"{self._path_of(pid)}/{a}" if pid else a) or self.create(a, parent=pid)["id"])
            return self.create(leaf, parent=pid, kind=kind, rule=rule, description=description, sort=sort,
                               images=images, exist_ok=exist_ok)
        kind = kind.lower()
        if kind not in ("static", "smart"):
            raise ValueError("kind must be static or smart")
        pid = self.resolve(parent) if parent is not None else None
        if parent is not None and pid is None:
            raise KeyError(f"no such parent album: {parent}")
        path = self._path_for(name, pid)
        ex = self.conn.execute("SELECT id FROM collections WHERE path=?", (path,)).fetchone()
        if ex:
            if exist_ok:
                if images:
                    self.add(ex[0], images)
                return self.get(ex[0])
            raise ValueError(f"album '{path}' already exists")
        qj = None
        if kind == "smart":
            tree = coerce_rule(rule)
            compile_rule(tree, self.cat)  # validate now, fail early
            qj = json.dumps(tree)
        with self.cat._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO collections(name, path, parent_id, kind, query_json, sort, description, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (name.strip(), path, pid, kind, qj, sort, description, now_iso()))
        aid = cur.lastrowid
        if images:
            self.add(aid, images)
        self.persist()
        return self.get(aid)

    def _path_of(self, album_id: int | None) -> str:
        return self.conn.execute("SELECT path FROM collections WHERE id=?", (album_id,)).fetchone()[0] if album_id else ""

    def create_smart(self, name: str, rule: Any, parent: Any = None, **kw) -> dict:
        return self.create(name, parent=parent, kind="smart", rule=rule, **kw)

    def set_rule(self, album: Any, rule: Any) -> dict:
        r = self._row(album)
        if r["kind"] != "smart":
            raise ValueError("only smart albums have rules")
        tree = coerce_rule(rule)
        compile_rule(tree, self.cat)
        with self.cat._lock, self.conn:
            self.conn.execute("UPDATE collections SET query_json=? WHERE id=?", (json.dumps(tree), r["id"]))
        self.persist()
        return self.get(r["id"])

    def get(self, album: Any) -> dict:
        r = self._row(album)
        d = dict(r)
        d["rule"] = json.loads(d.pop("query_json")) if r["query_json"] else None
        d["count"] = self.count(r["id"])
        d["children"] = [c["path"] for c in self.conn.execute(
            "SELECT path FROM collections WHERE parent_id=? ORDER BY name", (r["id"],))]
        return d

    def count(self, album: Any, include_children: bool = False) -> int:
        i = self.resolve(album)
        sql, params = self.membership_sql(i, include_children)
        return self.conn.execute(f"SELECT COUNT(*) FROM images i LEFT JOIN analysis a ON a.image_id=i.id WHERE {sql}",
                                 params).fetchone()[0]

    def list(self, tree: bool = False, parent: Any = None) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM collections ORDER BY path").fetchall()
        out = []
        for r in rows:
            if parent is not None:
                pp = self._row(parent)["path"]
                if not r["path"].startswith(pp + "/"):
                    continue
            out.append({"id": r["id"], "name": r["name"], "path": r["path"], "kind": r["kind"], "parent_id": r["parent_id"],
                        "count": self.count(r["id"]), "cover_image_id": r["cover_image_id"], "sort": r["sort"],
                        "description": r["description"], "rule": json.loads(r["query_json"]) if r["query_json"] else None})
        if not tree:
            return out
        nodes = {a["id"]: {**a, "children": []} for a in out}
        roots = []
        for a in out:
            (nodes[a["parent_id"]]["children"] if a["parent_id"] in nodes else roots).append(nodes[a["id"]])
        return roots

    def add(self, album: Any, images: Any) -> int:
        r = self._row(album)
        if r["kind"] != "static":
            raise ValueError("cannot add images to a smart album; edit its rule instead")
        ids = self.cat._ids(images)
        n = 0
        with self.cat._lock, self.conn:
            pos = (self.conn.execute("SELECT COALESCE(MAX(pos), -1) FROM collection_images WHERE collection_id=?",
                                     (r["id"],)).fetchone()[0]) + 1
            for i in ids:
                c = self.conn.execute("INSERT OR IGNORE INTO collection_images(collection_id, image_id, pos) VALUES(?,?,?)",
                                      (r["id"], i, pos))
                if c.rowcount:
                    n += 1
                    pos += 1
            if n and r["cover_image_id"] is None:
                self.conn.execute("UPDATE collections SET cover_image_id=? WHERE id=?", (ids[0], r["id"]))
        self.persist()
        return n

    def remove(self, album: Any, images: Any) -> int:
        r = self._row(album)
        if r["kind"] != "static":
            raise ValueError("cannot remove images from a smart album; edit its rule instead")
        ids = self.cat._ids(images)
        with self.cat._lock, self.conn:
            n = sum(self.conn.execute("DELETE FROM collection_images WHERE collection_id=? AND image_id=?",
                                      (r["id"], i)).rowcount for i in ids)
        self.persist()
        return n

    def reorder(self, album: Any, ordered: list) -> int:
        r = self._row(album)
        ids = self.cat._ids(ordered)
        with self.cat._lock, self.conn:
            for pos, i in enumerate(ids):
                self.conn.execute("UPDATE collection_images SET pos=? WHERE collection_id=? AND image_id=?", (pos, r["id"], i))
            self.conn.execute("UPDATE collections SET sort='manual' WHERE id=?", (r["id"],))
        self.persist()
        return len(ids)

    def set_cover(self, album: Any, image: Any) -> None:
        r = self._row(album)
        with self.cat._lock, self.conn:
            self.conn.execute("UPDATE collections SET cover_image_id=? WHERE id=?", (self.cat._ids(image)[0], r["id"]))
        self.persist()

    def set_sort(self, album: Any, sort: str) -> None:
        if sort not in ("taken_at", "rating", "name", "manual", "added"):
            raise ValueError("sort must be taken_at|rating|name|added|manual")
        r = self._row(album)
        with self.cat._lock, self.conn:
            self.conn.execute("UPDATE collections SET sort=? WHERE id=?", (sort, r["id"]))
        self.persist()

    def rename(self, album: Any, new_name: str) -> dict:
        r = self._row(album)
        newpath = self._path_for(new_name, r["parent_id"])
        self._repath(r, newpath, new_name.strip())
        return self.get(r["id"])

    def move(self, album: Any, new_parent: Any = None) -> dict:
        r = self._row(album)
        pid = self.resolve(new_parent) if new_parent is not None else None
        if pid is not None and (pid == r["id"] or self._path_of(pid).startswith(r["path"] + "/")):
            raise ValueError("cannot move an album into itself")
        newpath = self._path_for(r["name"], pid)
        self._repath(r, newpath, r["name"], pid)
        return self.get(r["id"])

    def _repath(self, r, newpath: str, newname: str, parent_id: Any = ...) -> None:
        if self.conn.execute("SELECT 1 FROM collections WHERE path=? AND id<>?", (newpath, r["id"])).fetchone():
            raise ValueError(f"album '{newpath}' already exists")
        old = r["path"]
        with self.cat._lock, self.conn:
            for c in self.conn.execute("SELECT id, path FROM collections WHERE path LIKE ? ESCAPE '\\'",
                                       (old.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%",)).fetchall():
                self.conn.execute("UPDATE collections SET path=? WHERE id=?", (newpath + c["path"][len(old):], c["id"]))
            if parent_id is ...:
                self.conn.execute("UPDATE collections SET path=?, name=? WHERE id=?", (newpath, newname, r["id"]))
            else:
                self.conn.execute("UPDATE collections SET path=?, name=?, parent_id=? WHERE id=?",
                                  (newpath, newname, parent_id, r["id"]))
        self.persist()

    def delete(self, album: Any, recursive: bool = False) -> int:
        r = self._row(album)
        kids = self.conn.execute("SELECT id FROM collections WHERE parent_id=?", (r["id"],)).fetchall()
        if kids and not recursive:
            raise ValueError(f"album '{r['path']}' has {len(kids)} sub-album(s); pass recursive=True to delete them too")
        n = 0
        with self.cat._lock, self.conn:
            for k in kids:
                n += self.delete(k["id"], True)
            self.conn.execute("DELETE FROM collections WHERE id=?", (r["id"],))
        self.persist()
        return n + 1

    # ------------------------------------------------------------------------------------ members
    def images(self, album: Any, include_children: bool = False, sort: str | None = None, order: str = "asc",
               limit: int | None = None, filter: Any = None) -> list[dict]:
        r = self._row(album)
        sort = sort or r["sort"] or "taken_at"
        sql, params = self.membership_sql(r["id"], include_children)
        extra, ep = self.cat._where(filter)
        if sort == "manual":
            q = ("SELECT i.*, ci.pos AS pos FROM images i LEFT JOIN collection_images ci ON ci.image_id=i.id AND "
                 f"ci.collection_id=? LEFT JOIN analysis a ON a.image_id=i.id WHERE {sql} AND {extra} "
                 f"ORDER BY ci.pos IS NULL, ci.pos, i.taken_ts, i.id")
            rows = [self.cat._row_dict(x) for x in self.conn.execute(q + (f" LIMIT {int(limit)}" if limit else ""),
                                                                       [r["id"], *params, *ep])]
            self.cat._attach_tags(rows)
            return rows
        from .catalog_sql import SORT_COLUMNS

        col = SORT_COLUMNS.get(sort)
        if col is None:
            raise RuleError(f"cannot sort by '{sort}'")
        direction = "DESC" if order.lower().startswith("d") else "ASC"
        q = (f"SELECT i.* FROM images i LEFT JOIN analysis a ON a.image_id=i.id WHERE {sql} AND {extra} "
             f"ORDER BY {col} {direction}, i.id")
        rows = [self.cat._row_dict(x) for x in self.conn.execute(q + (f" LIMIT {int(limit)}" if limit else ""), [*params, *ep])]
        self.cat._attach_tags(rows)
        return rows

    # ------------------------------------------------------------------------------------ persistence
    @property
    def json_path(self) -> Path:
        return self.cat.meta_dir / "albums.json"

    def dump(self) -> dict:
        albums = []
        for r in self.conn.execute("SELECT * FROM collections ORDER BY path"):
            members = [x[0] for x in self.conn.execute(
                "SELECT i.primary_path FROM collection_images ci JOIN images i ON i.id=ci.image_id WHERE ci.collection_id=? "
                "ORDER BY ci.pos", (r["id"],))]
            cover = None
            if r["cover_image_id"]:
                c = self.conn.execute("SELECT primary_path FROM images WHERE id=?", (r["cover_image_id"],)).fetchone()
                cover = c[0] if c else None
            albums.append({"name": r["name"], "path": r["path"], "kind": r["kind"],
                           "rule": json.loads(r["query_json"]) if r["query_json"] else None, "sort": r["sort"],
                           "description": r["description"], "cover": cover, "members": members, "created": r["created_at"]})
        return {"version": 1, "albums": albums}

    _hold = 0

    def persist(self) -> None:
        if self._hold:
            return
        try:
            self.cat.meta_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.json_path.with_suffix(".tmp")
            tmp.write_text(dumps(self.dump()), encoding="utf-8")
            os.replace(tmp, self.json_path)
        except OSError:
            pass

    def load(self, data: dict, merge: bool = True) -> dict:
        """Re-create albums from a dump. Existing albums keep their rule but receive missing static members, so loading
        an albums.json before the images are scanned and again afterwards converges. Never writes albums.json
        itself until it is done (a half-loaded state must not overwrite the file)."""
        made = skipped = members = 0
        self._hold += 1
        try:
            for a in sorted(data.get("albums", []), key=lambda x: x["path"].count("/")):
                parts = a["path"].split("/")
                parent = "/".join(parts[:-1]) or None
                existing = self.resolve(a["path"])
                if existing is not None:
                    al = {"id": existing}
                    skipped += 1
                else:
                    try:
                        al = self.create(parts[-1], parent=parent, kind=a.get("kind", "static"), rule=a.get("rule"),
                                         description=a.get("description", ""), sort=a.get("sort", "taken_at"))
                    except Exception:
                        skipped += 1
                        continue
                    made += 1
                if a.get("kind", "static") == "static":
                    ids = [self.cat.find(m) for m in a.get("members", [])]
                    ids = [i for i in ids if i is not None]
                    if ids:
                        members += self.add(al["id"], ids)
                        with self.conn:
                            have = {r[0] for r in self.conn.execute("SELECT image_id FROM collection_images WHERE collection_id=?",
                                                                    (al["id"],))}
                            for pos, i in enumerate(i for i in ids if i in have):
                                self.conn.execute("UPDATE collection_images SET pos=? WHERE collection_id=? AND image_id=?",
                                                  (pos, al["id"], i))
                if a.get("cover"):
                    c = self.cat.find(a["cover"])
                    if c is not None:
                        self.set_cover(al["id"], c)
        finally:
            self._hold -= 1
        # a load before the images exist must not shrink the file: only rewrite once members could be resolved
        if not getattr(self.cat, "_albums_pending", False):
            self.persist()
        return {"albums": made, "skipped": skipped, "members": members}

    def load_from_disk(self) -> dict:
        if not self.json_path.exists():
            return {"albums": 0}
        return self.load(json.loads(self.json_path.read_text(encoding="utf-8")))


# ============================================================================================ export
_SAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)


def safe_name(s: str) -> str:
    return _SAFE.sub("_", s).strip(" .") or "album"


class _Fmt(dict):
    def __missing__(self, k):
        return ""


def export_album(cat, album: Any, dest: str | Path, mode: str = "copy", originals: bool = True, raw: bool = True,
                 jpg: bool = True, sidecars: bool = False, preset: str | None = None,
                 naming: str = "{pos:03d}_{stem}", zip_path: str | Path | None = None, overwrite: bool = False,
                 include_children: bool = False, dry_run: bool = False, images: list[dict] | None = None) -> dict:
    """Export an album to a folder: original files copied (`mode='copy'`) or symlinked (`mode='symlink'`) and/or
    rendered through the export engine (`preset=`), named with `naming` (tokens: pos, stem, date, camera, rating,
    album). Optionally also zip the result. Never modifies the catalog or the originals."""
    mgr = cat.albums
    alb = mgr.get(album)
    imgs = images if images is not None else mgr.images(album, include_children=include_children)
    dest = Path(dest).expanduser()
    if mode not in ("copy", "symlink", "hardlink"):
        raise ValueError("mode must be copy|symlink|hardlink")
    files, rendered, errors = [], [], []
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    for pos, im in enumerate(imgs, start=1):
        fmt = _Fmt(pos=pos, stem=im["stem"], date=(im.get("taken_at") or "")[:10], camera=safe_name(im.get("camera") or ""),
                   rating=im.get("rating", 0), album=safe_name(alb["name"]), name=im["stem"])
        try:
            base = safe_name(naming.format_map(fmt))
        except (ValueError, IndexError, KeyError) as exc:
            raise ValueError(f"bad naming template '{naming}': {exc}") from exc
        if originals:
            srcs = []
            if raw and im.get("raw"):
                srcs.append(Path(im["raw"]))
            if jpg and im.get("jpg") and im["jpg"] != im.get("raw"):
                srcs.append(Path(im["jpg"]))
            for s in srcs:
                t = dest / f"{base}{s.suffix}"
                files.append(str(t))
                if dry_run:
                    continue
                if t.exists() or t.is_symlink():
                    if not overwrite:
                        errors.append(f"exists, skipped: {t.name}")
                        continue
                    t.unlink()
                try:
                    if mode == "symlink":
                        t.symlink_to(s.resolve())
                    elif mode == "hardlink":
                        os.link(s, t)
                    else:
                        shutil.copy2(s, t)
                except OSError as exc:
                    errors.append(f"{s.name}: {exc}")
            if sidecars:
                for suffix in (".ps.json", ".xmp"):
                    sp = Path(str(im["path"]) + suffix)
                    if sp.exists() and not dry_run:
                        shutil.copy2(sp, dest / f"{base}{Path(im['path']).suffix}{suffix}")
        if preset:
            if dry_run:
                rendered.append(str(dest / f"{base}.<{preset}>"))
                continue
            try:
                from .export import export_image  # ENGINE-owned; lazy

                out = export_image(im["path"], str(dest / f"{base}.jpg"), preset=preset)
                rendered.append(str(out))
            except Exception as exc:
                errors.append(f"render {im['stem']}: {type(exc).__name__}: {exc}")
    res = {"album": alb["path"], "images": len(imgs), "dest": str(dest), "files": files, "rendered": rendered,
           "errors": errors, "dry_run": dry_run}
    if zip_path and not dry_run:
        res["zip"] = str(zip_folder(dest, zip_path))
    return res


def zip_folder(folder: str | Path, zip_path: str | Path) -> Path:
    folder, zip_path = Path(folder), Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file() and f != zip_path:
                z.write(f.resolve() if f.is_symlink() else f, f.relative_to(folder).as_posix())
    return zip_path


PAGES_MM = {"a4": (210.0, 297.0), "letter": (215.9, 279.4), "a3": (297.0, 420.0)}


def contact_sheet(cat, source: Any, out: str | Path, cols: int = 5, page: str = "a4", dpi: int = 150,
                  title: str | None = None, landscape: bool = False, thumb_size: int = 480, captions: bool = True,
                  margin_mm: float = 10.0, filter: Any = None) -> dict:
    """Pillow contact sheet(s) of an album (name/id) or an explicit list of image ids/paths. `out` ending in
    .pdf produces one multi-page PDF; .jpg/.png write one file per page (`_p2`, `_p3` ...)."""
    from PIL import Image, ImageDraw, ImageFont

    if isinstance(source, (list, tuple)):
        ids = cat._ids(list(source))
        imgs = [cat._row_dict(cat.conn.execute("SELECT * FROM images WHERE id=?", (i,)).fetchone()) for i in ids]
        title = title or "Contact sheet"
    else:
        imgs = cat.albums.images(source, filter=filter)
        title = title or cat.albums.get(source)["name"]
    if not imgs:
        raise ValueError("nothing to put on the contact sheet")
    pw_mm, ph_mm = PAGES_MM.get(page.lower(), PAGES_MM["a4"])
    if landscape:
        pw_mm, ph_mm = ph_mm, pw_mm
    px = lambda mm: int(round(mm / 25.4 * dpi))  # noqa: E731
    W, H, M = px(pw_mm), px(ph_mm), px(margin_mm)
    try:
        font, small = ImageFont.load_default(size=max(10, dpi // 9)), ImageFont.load_default(size=max(8, dpi // 14))
    except TypeError:
        font = small = ImageFont.load_default()
    head = int(dpi / 3.2) if title else 0
    gap = max(6, dpi // 25)
    cell_w = (W - 2 * M - gap * (cols - 1)) // cols
    cap_h = int(dpi / 9) if captions else 0
    cell_h = int(cell_w * 0.75) + cap_h
    rows = max(1, (H - 2 * M - head + gap) // (cell_h + gap))
    per_page = cols * rows
    pages = []
    for start in range(0, len(imgs), per_page):
        sheet = Image.new("RGB", (W, H), (255, 255, 255))
        d = ImageDraw.Draw(sheet)
        if title:
            d.text((M, M), f"{title}  ({start + 1}-{min(start + per_page, len(imgs))} of {len(imgs)})", fill=(20, 20, 20), font=font)
        for k, im in enumerate(imgs[start:start + per_page]):
            r, c = divmod(k, cols)
            x0, y0 = M + c * (cell_w + gap), M + head + r * (cell_h + gap)
            try:
                with Image.open(cat.thumb(im["id"], thumb_size)) as t:
                    t = t.convert("RGB")
                    t.thumbnail((cell_w, cell_h - cap_h))
                    sheet.paste(t, (x0 + (cell_w - t.width) // 2, y0 + (cell_h - cap_h - t.height) // 2))
            except Exception:
                d.rectangle([x0, y0, x0 + cell_w, y0 + cell_h - cap_h], outline=(200, 0, 0))
            if captions:
                stars = ("*" * int(im.get("rating") or 0)) + ("  X" if im.get("flag") == "reject" else "")
                d.text((x0, y0 + cell_h - cap_h + 1), f"{im['stem']} {stars}".strip(), fill=(60, 60, 60), font=small)
        pages.append(sheet)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    paths = []
    if out.suffix.lower() == ".pdf":
        pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=float(dpi))
        paths.append(str(out))
    else:
        for n, p in enumerate(pages):
            f = out if n == 0 else out.with_name(f"{out.stem}_p{n + 1}{out.suffix}")
            p.save(f, quality=90) if f.suffix.lower() in (".jpg", ".jpeg") else p.save(f)
            paths.append(str(f))
    return {"files": paths, "pages": len(pages), "images": len(imgs), "page": page, "dpi": dpi, "cols": cols, "rows": rows}
