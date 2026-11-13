"""Library commands: import, scan, ls/search, rate/flag/label, tag *, events, album *, analyze, cull, group, thumbs,
catalog *.  Each handler returns a JSON-able object (printed as text, or JSON with --json).

The library root is the global ``--root DIR`` (else $PHOTOSTUDIO_ROOT, else the nearest parent of the current directory
that holds a ``.photostudio`` folder, else the current directory).

Selections (any command that acts on images): file paths / directories / globs, ``id:N`` (or plain ``N``),
``@all``, ``@last-import``, ``@album:NAME``, ``@session:YYYY-MM-DD`` (taken that day), ``@tag:a|b`` and ``--query "rating>=4 AND tag:people"``
(the filter DSL; `photo ls --help` lists the fields). With no selection, commands that make sense for a whole library
act on everything.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from ..cli import load_json_arg
from ..util import to_jsonable

# ================================================================================================ helpers
def find_root(args) -> Path:
    r = getattr(args, "root", None) or os.environ.get("PHOTOSTUDIO_ROOT")
    if r:
        return Path(r).expanduser().resolve()
    cur = Path.cwd().resolve()
    for p in (cur, *cur.parents):
        if (p / ".photostudio" / "catalog.sqlite").exists():
            return p
    return cur


def open_catalog(args, create: bool = True):
    from ..catalog import Catalog

    return Catalog(find_root(args), create=create)


def _progress(args):
    if getattr(args, "quiet", False) or getattr(args, "json", False) or not sys.stderr.isatty():
        return None

    def cb(done, total, *_):
        print(f"\r{done}/{total}", end="", file=sys.stderr, flush=True)
        if done >= total:
            print(file=sys.stderr)

    return cb


def _rule_arg(text: str | None):
    if text is None:
        return None
    t = text.strip()
    if t.startswith("{") or t.startswith("[") or t.startswith("@"):
        return load_json_arg(t)
    return t


def _quote(v: str) -> str:
    return f'"{v}"' if any(c in v for c in " \t()") else v


def filter_from_args(args) -> str | None:
    """Turn the common filter flags into one DSL string (ANDed with --query)."""
    parts = []
    if getattr(args, "query", None):
        parts.append(f"({args.query})")
    if getattr(args, "rating", None) is not None:
        parts.append(f"rating>={args.rating}")
    if getattr(args, "flag", None):
        parts.append(f"flag:{args.flag}")
    if getattr(args, "label", None):
        parts.append(f"label:{_quote(args.label)}")
    for t in getattr(args, "tag", None) or []:
        parts.append(f"tag:{_quote(t)}")
    if getattr(args, "camera", None):
        parts.append(f"camera:{_quote(args.camera)}")
    if getattr(args, "album", None):
        parts.append(f"album:{_quote(args.album)}")
    since, until = getattr(args, "since", None), getattr(args, "until", None)
    if since or until:
        parts.append(f"taken:[{since or '0000'};{until or '9999'}]")
    return " AND ".join(parts) if parts else None


def select_ids(cat, items: list[str] | None, query: str | None = None, default_all: bool = True,
               scan_unknown: bool = True) -> list[int]:
    """Resolve a selection to catalog ids (in order, de-duplicated)."""
    out: list[int] = []
    seen: set[int] = set()
    if scan_unknown and cat.count() == 0:       # first use of a folder: index it instead of reporting "0 images"
        cat.scan()

    def add(ids):
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append(i)

    for it in items or []:
        s = str(it)
        if s == "@all":
            add(cat.ids())
        elif s == "@last-import":
            add(cat.last_import_ids())
        elif s.startswith("@album:"):
            add([r["id"] for r in cat.albums.images(s[7:])])
        elif s.startswith("@session:"):
            add(cat.ids(f"taken:{s[9:]}"))
        elif s.startswith("@tag:"):
            add(cat.ids(f"tag:{_quote(s[5:])}"))
        elif s.startswith("id:") or s.isdigit():
            i = int(s[3:] if s.startswith("id:") else s)
            if cat.find(i) is None:
                raise KeyError(f"no image with id {i}")
            add([i])
        else:
            p = Path(s).expanduser()
            ids: list[int] = []
            cands = [p] if p.is_absolute() else [Path.cwd() / p, cat.root / p]
            hit = next((c for c in cands if c.exists()), None)
            if hit is not None and hit.is_dir():
                rel = cat.rel(hit)
                ids = cat.ids(f"folder:{_quote(rel)}" if rel not in (".", "") else None)
                if not ids and scan_unknown:
                    cat.scan([hit])
                    ids = cat.ids(f"folder:{_quote(rel)}" if rel not in (".", "") else None)
            elif hit is not None:
                i = cat.find(hit)
                if i is None and scan_unknown:
                    cat.scan([hit])
                    i = cat.find(hit)
                if i is None:
                    raise KeyError(f"not in catalog (unsupported file?): {s}")
                ids = [i]
            else:
                import glob

                for g in sorted(glob.glob(str(p if p.is_absolute() else Path.cwd() / p), recursive=True)):
                    i = cat.find(g)
                    if i is not None:
                        ids.append(i)
                if not ids:
                    raise FileNotFoundError(f"no such file, id or pattern: {s}")
            add(ids)
    if query:
        qids = cat.ids(query)
        if items:
            keep = set(qids)
            out = [i for i in out if i in keep]
            seen = set(out)
        else:
            add(qids)
    elif not items and default_all:
        add(cat.ids())
    return out


def _need_selection(items, query):
    if not items and not query:
        raise ValueError("give at least one image (path, id, @last-import, @album:NAME ...) or --query")


def brief(r: dict, tags: bool = True) -> dict:
    d = {"id": r["id"], "file": r["primary_path"], "rating": r.get("rating"), "flag": r.get("flag"), "label": r.get("label"),
         "taken": (r.get("taken_at") or "")[:19].replace("T", " ") or None}
    if tags:
        t = r.get("tags") or []
        d["tags"] = ", ".join(t[:6]) + (f" (+{len(t) - 6})" if len(t) > 6 else "")
    return d


def _split_list(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").replace(";", ",").split(",") if x.strip()]


def _tags_arg(v: str) -> list[str]:
    # tags may contain commas only inside quotes; plain comma/semicolon separated lists are the norm
    return [t.strip().strip('"') for t in v.replace(";", ",").split(",") if t.strip()]


# ================================================================================================ import / scan
def cmd_import(args):
    from ..importer import import_media

    root = find_root(args)
    dest = Path(args.dest).expanduser().resolve() if args.dest else root
    cat = None
    if not args.dry_run:
        from ..catalog import Catalog

        cat = Catalog(dest)
    try:
        rep = import_media(args.source, dest, dest2=args.dest2, event=args.event, rename=args.rename,
                           delete_after_verify=args.delete_after_verify, dry_run=args.dry_run,
                           sidecars=not args.no_sidecars, include_video=not args.no_video, catalog=cat,
                           progress=_progress(args))
    finally:
        if cat is not None:
            cat.close()
    if not args.full:
        rep = {k: v for k, v in rep.items() if k not in ("files",)}
    return rep


def cmd_scan(args):
    with open_catalog(args) as cat:
        paths = [Path(p).expanduser() for p in args.paths] or None
        res = cat.scan(paths, hash_files=args.hash, prune=not args.no_prune, progress=_progress(args))
        res["total"] = cat.count()
        if cat.warnings:
            res["warnings"] = cat.warnings[:20]
        return res


# ================================================================================================ query
def cmd_ls(args):
    with open_catalog(args) as cat:
        flt = filter_from_args(args)
        text = getattr(args, "text", None)
        sel = list(getattr(args, "sel", None) or []) + (["@last-import"] if getattr(args, "last_import", False) else [])
        if text and args.command == "ls":                 # `photo ls @last-import` / `photo ls id:7`: a selector, not a word
            from ..cli import is_selector

            if is_selector(text):
                sel.append(text)
                text = None
        if sel:                                    # restrict to an explicit selection (paths, @album:NAME, @last-import ...)
            from ..catalog_sql import parse_query

            ids = select_ids(cat, sel, None, default_all=False)
            flt = {"all": [{"__ids": ids}] + ([parse_query(flt)] if flt else [])}
        if getattr(args, "count", False):
            n = len(cat.search(text, flt, limit=10 ** 6)) if text else cat.count(flt)
            return {"count": n}
        if text:
            rows = cat.search(text, flt, limit=args.limit or 200)
        else:
            rows = cat.images(flt, sort=args.sort, order=args.order, limit=args.limit, offset=args.offset)
        if args.full:
            return to_jsonable(rows)
        return [brief(r) for r in rows]


def cmd_stats(args):
    with open_catalog(args) as cat:
        return cat.stats()


def _each(args, fn):
    with open_catalog(args) as cat:
        ids = select_ids(cat, args.images, args.query, default_all=False)
        _need_selection(args.images, args.query)
        if not ids:
            return {"changed": 0, "images": 0}
        res = fn(cat, ids)
        return {"images": len(ids), **(res if isinstance(res, dict) else {})}


def cmd_rate(args):
    return _each(args, lambda c, ids: c.set_rating(ids, args.rating))


def cmd_flag(args):
    return _each(args, lambda c, ids: c.set_flag(ids, args.flag))


def cmd_label(args):
    return _each(args, lambda c, ids: c.set_label(ids, args.label))


# ================================================================================================ tags
def cmd_tag_add(args):
    return _each(args, lambda c, ids: c.add_tags(ids, _tags_arg(args.tags), source="user"))


def cmd_tag_remove(args):
    return _each(args, lambda c, ids: c.remove_tags(ids, _tags_arg(args.tags), recursive=args.recursive))


def cmd_tag_confirm(args):
    return _each(args, lambda c, ids: c.confirm_tags(ids, _tags_arg(args.tags) if args.tags else None, confirm=not args.reject))


def cmd_tag_list(args):
    from .. import tags as T

    with open_catalog(args) as cat:
        if args.text:
            return {"_text": T.tree_text(cat, args.prefix, counts=True)}
        return cat.list_tags(args.prefix, tree=args.tree, confirmed_only=args.confirmed)


def cmd_tag_rename(args):
    from .. import tags as T

    with open_catalog(args) as cat:
        return T.rename_tag(cat, args.old, args.new, merge=args.merge)


def cmd_tag_move(args):
    from .. import tags as T

    with open_catalog(args) as cat:
        return T.move_tag(cat, args.src, None if args.parent in ("", "-", "root") else args.parent, merge=args.merge)


def cmd_tag_merge(args):
    from .. import tags as T

    with open_catalog(args) as cat:
        return T.merge_tags(cat, args.src, args.dst)


def cmd_tag_delete(args):
    from .. import tags as T

    with open_catalog(args) as cat:
        return T.delete_tag(cat, args.tag, recursive=not args.keep_children)


def cmd_tag_auto(args):
    from .. import tags as T

    tiers = _split_list(args.tier) or ["metadata"]
    out: dict[str, Any] = {}
    with open_catalog(args) as cat:
        ids = select_ids(cat, args.images, args.query)
        prog = _progress(args)
        for t in tiers:
            if t in ("metadata", "meta", "all"):
                out["metadata"] = T.auto_tag_metadata(cat, ids, progress=prog)
            if t in ("clip", "all"):
                out["clip"] = _optional(lambda: T.auto_tag_clip(cat, ids, min_prob=args.min_prob, confirm_above=args.confirm_above,
                                                                progress=prog), t, args.tier == "all")
            if t in ("ram", "all"):
                out["ram"] = _optional(lambda: T.auto_tag_ram(cat, ids, min_rating=args.min_rating, confirm_above=args.confirm_above,
                                                              progress=prog), t, args.tier == "all")
            if t in ("caption", "all"):
                out["caption"] = _optional(lambda: T.caption_images(cat, ids, min_rating=args.min_rating or 4, progress=prog), t,
                                           args.tier == "all")
            if t in ("places", "all"):
                out["places"] = _optional(lambda: T.place_tags(cat, ids, progress=prog), t, args.tier == "all")
            if t not in ("metadata", "meta", "clip", "ram", "caption", "places", "all"):
                raise ValueError(f"unknown tier '{t}' (metadata, clip, ram, caption, places, all)")
        out["images"] = len(ids)
    return out


def _optional(fn, name, soft):
    from ..util import MissingDependency

    try:
        return fn()
    except MissingDependency as exc:
        if not soft:
            raise
        return {"skipped": str(exc)}


def cmd_tag_people(args):
    from .. import tags as T

    with open_catalog(args) as cat:
        out: dict[str, Any] = {}
        if args.detect:
            ids = select_ids(cat, args.images, args.query)
            out["detect"] = T.detect_faces_catalog(cat, ids, progress=_progress(args))
        if args.cluster or args.detect:
            out["clusters"] = T.cluster_faces(cat, eps=args.eps, min_samples=args.min_samples)
        if args.name:
            cid, name = args.name
            out["named"] = T.name_cluster(cat, int(cid), name)
        if args.match:
            out["match"] = T.match_faces(cat)
        if not out:
            out["people"] = T.people_summary(cat)
        return out


def cmd_tag_places(args):
    from .. import tags as T

    with open_catalog(args) as cat:
        return T.place_tags(cat, select_ids(cat, args.images, args.query), progress=_progress(args))


def cmd_events(args):
    from .. import tags as T

    with open_catalog(args) as cat:
        ids = select_ids(cat, args.images, args.query)
        evs = T.suggest_events(cat, ids, gap_hours=args.gap_hours, merge_km=args.merge_km)
        names = {}
        for spec in args.name or []:
            if "=" not in spec:
                raise ValueError("--name expects N=Event name (N = event number from the listing)")
            n, nm = spec.split("=", 1)
            names[int(n)] = nm.strip()
        res: dict[str, Any] = {"events": [{k: v for k, v in e.items() if k not in ("ids", "image_ids")} | {"n_images": len(e.get("image_ids", e.get("ids", [])))}
                                          if not args.full else e for e in evs]}
        if args.apply:
            res["applied"] = T.apply_events(cat, evs, names=names, albums=not args.no_albums, tags=not args.no_tags)
        return res


# ================================================================================================ albums
def cmd_album_create(args):
    with open_catalog(args) as cat:
        rule = _rule_arg(args.rule)
        if rule is not None and (args.images or args.query):
            raise ValueError("a smart album is defined by its rule: give either --rule or images, not both")
        if rule is not None:
            r = cat.albums.create_smart(args.name, rule, parent=args.parent, description=args.description or "")
        else:
            r = cat.albums.create(args.name, parent=args.parent, description=args.description or "", exist_ok=args.exist_ok)
        n = 0
        if args.images or args.query:
            n = cat.albums.add(r["id"], select_ids(cat, args.images, args.query, default_all=False))
        cat.albums.persist()
        return {"album": r, "added": n}


def cmd_album_smart(args):
    args.images, args.query = [], None
    if not args.rule:
        raise ValueError("--rule is required (JSON or the DSL, e.g. \"rating>=4 AND tag:people\")")
    return cmd_album_create(args)


def cmd_album_add(args):
    with open_catalog(args) as cat:
        ids = select_ids(cat, args.images, args.query, default_all=False)
        _need_selection(args.images, args.query)
        n = cat.albums.add(args.album, ids)
        cat.albums.persist()
        return {"album": args.album, "added": n}


def cmd_album_remove(args):
    with open_catalog(args) as cat:
        ids = select_ids(cat, args.images, args.query, default_all=False, scan_unknown=False)
        _need_selection(args.images, args.query)
        n = cat.albums.remove(args.album, ids)
        cat.albums.persist()
        return {"album": args.album, "removed": n}


def cmd_album_list(args):
    with open_catalog(args) as cat:
        return cat.albums.list(tree=args.tree)


def cmd_album_show(args):
    with open_catalog(args) as cat:
        rows = cat.albums.images(args.album, include_children=args.children, limit=args.limit)
        return {"album": cat.albums.get(args.album), "images": [brief(r) for r in rows]}


def cmd_album_delete(args):
    with open_catalog(args) as cat:
        n = cat.albums.delete(args.album, recursive=args.recursive)
        cat.albums.persist()
        return {"deleted": n}


def cmd_album_export(args):
    from ..albums import export_album

    with open_catalog(args) as cat:
        return export_album(cat, args.album, args.dest, mode=args.mode, originals=not args.no_originals, raw=not args.no_raw,
                            jpg=not args.no_jpg, sidecars=args.sidecars, preset=args.preset, naming=args.naming,
                            zip_path=args.zip, overwrite=args.overwrite, include_children=args.children, dry_run=args.dry_run)


def cmd_album_gallery(args):
    from ..gallery import build_gallery

    with open_catalog(args) as cat:
        return build_gallery(cat, args.album, args.out, title=args.title, description=args.description or "",
                             thumb_size=args.thumb_size, web_size=args.web_size, show_tags=not args.no_tags,
                             include_originals=args.originals, show_filenames=args.filenames)


def cmd_album_sheet(args):
    from ..albums import contact_sheet

    with open_catalog(args) as cat:
        src: Any = args.album
        if args.album.startswith("@") or args.album in ("", "-"):
            src = select_ids(cat, [args.album])
        return contact_sheet(cat, src, args.out, cols=args.cols, page=args.page, dpi=args.dpi, title=args.title,
                             landscape=args.landscape, captions=not args.no_captions)


# ================================================================================================ analysis
def cmd_analyze(args):
    from .. import cull as C

    with open_catalog(args) as cat:
        ids = select_ids(cat, args.images, args.query)
        res = C.analyze_catalog(cat, ids, force=args.force, aesthetic=args.aesthetic, progress=_progress(args))
        if args.show:
            res["analysis"] = [{"id": i, **{k: v for k, v in (cat.get_analysis(i) or {}).items() if k != "clip_emb"}} for i in ids[:args.show]]
        return res


def _cull_row(r: dict, full: bool) -> dict:
    if full:
        return {k: v for k, v in r.items() if k != "analysis"}
    return {"id": r.get("id"), "name": r.get("name"), "score": round(r.get("score") or 0, 3), "flag": r.get("flag"),
            "group": r.get("group"), "reasons": "; ".join(r.get("reasons") or [])}


def cmd_cull(args):
    from .. import cull as C

    with open_catalog(args) as cat:
        ids = select_ids(cat, args.images, args.query)
        results = C.cull_catalog(cat, ids, target=args.target, genre=args.genre, aesthetic=args.aesthetic, progress=_progress(args))
        out: dict[str, Any] = {"summary": C.summarize(results), "results": [_cull_row(r, args.full) for r in results]}
        if args.apply:
            out["applied"] = C.apply_cull(cat, results, flags=True, rate=args.rate, label_maybe=args.label_maybe,
                                          clear_maybe=args.clear_maybe, move_rejects=args.move_rejects,
                                          rejects_dir=args.rejects_dir, dry_run=args.dry_run, overwrite=args.overwrite)
        else:
            out["note"] = "suggestions only: nothing was written. Re-run with --apply to set pick/reject flags (files are never deleted)."
        return out


def cmd_group(args):
    from .. import cull as C

    with open_catalog(args) as cat:
        ids = select_ids(cat, args.images, args.query)
        res = C.group_catalog(cat, ids, store=not args.dry_run)
        return res if args.full else {k: v for k, v in res.items() if k not in ("members",)}


def cmd_thumbs(args):
    with open_catalog(args) as cat:
        out: dict[str, Any] = {}
        if args.prune:
            out["pruned"] = cat.prune_thumbs()
        ids = select_ids(cat, args.images, args.query)
        sizes = tuple(int(s) for s in _split_list(args.sizes)) or (160, 480)
        out.update(cat.thumbs(ids, sizes=sizes, progress=_progress(args), force=args.force))
        return out


# ================================================================================================ catalog
def cmd_catalog_export(args):
    from .. import backup

    with open_catalog(args) as cat:
        return backup.export_catalog(cat, args.out, formats=tuple(_split_list(args.formats)) or ("json", "csv", "tags", "albums"),
                                     xmp=args.xmp, sqlite_copy=args.sqlite)


def cmd_catalog_import(args):
    from .. import backup

    with open_catalog(args) as cat:
        return backup.import_catalog(cat, args.src)


def cmd_catalog_rebuild(args):
    with open_catalog(args) as cat:
        return cat.rebuild(keep_analysis=not args.drop_analysis, hash_files=args.hash)


def cmd_catalog_rewrite(args):
    with open_catalog(args) as cat:
        return {"rewritten": cat.rewrite_sidecars(select_ids(cat, args.images, args.query) if (args.images or args.query) else None)}


# ================================================================================================ parser
def _sel(p, optional: bool = True, query: bool = True):
    p.add_argument("images", nargs="*" if optional else "+", help="paths, globs, id:N, @all, @last-import, @album:NAME, @session:DATE")
    if query:
        p.add_argument("--query", "-Q", help="filter DSL, e.g. \"rating>=4 AND tag:people|noa EXCEPT flag:reject\"")


def _filters(p):
    p.add_argument("--query", "-Q", help="filter DSL (see below)")
    p.add_argument("--rating", type=int, help="minimum rating")
    p.add_argument("--flag", choices=["pick", "reject", "none"])
    p.add_argument("--label")
    p.add_argument("--tag", action="append", help="tag path (prefix match: people matches people|noa); repeatable (AND)")
    p.add_argument("--camera")
    p.add_argument("--album")
    p.add_argument("--since", help="taken on/after (YYYY[-MM[-DD]])")
    p.add_argument("--until", help="taken on/before")


def register(subparsers) -> None:
    try:
        from ..cli import _common_parser

        common = _common_parser(False)
    except Exception:  # noqa: BLE001
        common = None

    def sub(group, name, fn, help_, **kw):
        kw.setdefault("parents", [common] if common else [])
        p = group.add_parser(name, help=help_, description=help_, **kw)
        p.set_defaults(func=fn)
        return p

    P = subparsers.add_parser

    p = P("import", help="copy a card/folder into the library (dated folders, verified, optional second copy)")
    p.add_argument("source", help="card mount, DCIM folder or any folder")
    p.add_argument("--dest", help="library root to import into (default: --root / cwd)")
    p.add_argument("--dest2", help="second destination, written from the verified first copy")
    p.add_argument("--event", help="event name appended to the dated folder (2026/2026-06-14_wedding)")
    p.add_argument("--rename", help="rename pattern, e.g. {date}_{time}_{orig}")
    p.add_argument("--delete-after-verify", action="store_true", help="remove each source file only after every copy verified")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-sidecars", action="store_true", help="do not create .ps.json stubs")
    p.add_argument("--no-video", action="store_true")
    p.add_argument("--full", action="store_true", help="include the per-file list")
    p.set_defaults(func=cmd_import)

    p = P("scan", help="index the library (incremental; reconciles .ps.json and .xmp)")
    p.add_argument("paths", nargs="*")
    p.add_argument("--hash", action="store_true", help="also hash file contents")
    p.add_argument("--no-prune", action="store_true", help="keep catalog rows for files that vanished")
    p.set_defaults(func=cmd_scan)

    for name, h in (("ls", "list images"), ("search", "full-text search (names, tags, notes, captions, place, camera)")):
        p = P(name, help=h, description=h + "\n\nFilter DSL: rating>=4  flag:pick  label:red  tag:people|noa  camera:R50  iso>3200  "
                                              "taken:[2026-06-01;2026-06-30]  id:42  album:Best  issue:blurry  person:Noa  place:Lisbon  "
                                              "text:sunset  AND OR EXCEPT NOT ( )")
        if name == "search":
            p.add_argument("text")
        else:
            p.add_argument("text", nargs="?", help="optional full-text search, or a selector (@last-import, @album:NAME, id:N)")
        _filters(p)
        p.add_argument("--sel", "-s", action="append", help="restrict to a selection: path, id:N, @album:NAME, @session:DATE (repeatable)")
        p.add_argument("--last-import", action="store_true", help="only the images of the most recent import")
        p.add_argument("--sort", default="taken_at")
        p.add_argument("--order", default="asc", choices=["asc", "desc"])
        p.add_argument("--limit", type=int)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--count", action="store_true")
        p.add_argument("--full", action="store_true", help="all fields")
        p.set_defaults(func=cmd_ls)

    p = P("stats", help="catalog statistics")
    p.set_defaults(func=cmd_stats)

    p = P("rate", help="set star rating 0-5 (sidecar + XMP)")
    p.add_argument("rating", type=int, choices=range(0, 6), metavar="0-5")
    _sel(p)
    p.set_defaults(func=cmd_rate)
    p = P("flag", help="pick / reject / none")
    p.add_argument("flag", choices=["pick", "reject", "none"])
    _sel(p)
    p.set_defaults(func=cmd_flag)
    p = P("label", help="colour label (red yellow green blue purple, or none)")
    p.add_argument("label")
    _sel(p)
    p.set_defaults(func=cmd_label)

    # ---- tag
    t = P("tag", help="hierarchical tags: add remove list rename move merge delete confirm auto people places")
    tg = t.add_subparsers(dest="tcmd", required=True, metavar="<action>")
    q = sub(tg, "add", cmd_tag_add, "add tags (a|b|c paths; comma separated)")
    q.add_argument("tags")
    _sel(q)
    q = sub(tg, "remove", cmd_tag_remove, "remove tags from images")
    q.add_argument("tags")
    q.add_argument("--recursive", action="store_true", help="also remove descendants")
    _sel(q)
    q = sub(tg, "confirm", cmd_tag_confirm, "confirm (or with --reject, drop) auto-suggested tags")
    q.add_argument("--tags")
    q.add_argument("--reject", action="store_true")
    _sel(q)
    q = sub(tg, "list", cmd_tag_list, "list the tag tree with counts")
    q.add_argument("--prefix")
    q.add_argument("--tree", action="store_true")
    q.add_argument("--text", action="store_true", help="indented text tree")
    q.add_argument("--confirmed", action="store_true")
    q = sub(tg, "rename", cmd_tag_rename, "rename a tag (and its descendants) everywhere")
    q.add_argument("old")
    q.add_argument("new", help="new leaf name")
    q.add_argument("--merge", action="store_true")
    q = sub(tg, "move", cmd_tag_move, "move a tag under another parent ('-' = top level)")
    q.add_argument("src")
    q.add_argument("parent")
    q.add_argument("--merge", action="store_true")
    q = sub(tg, "merge", cmd_tag_merge, "merge one tag into another")
    q.add_argument("src")
    q.add_argument("dst")
    q = sub(tg, "delete", cmd_tag_delete, "delete a tag (and descendants) from all images")
    q.add_argument("tag")
    q.add_argument("--keep-children", action="store_true")
    q = sub(tg, "auto", cmd_tag_auto, "auto-tag: metadata (always), clip, ram, caption, places (optional models), or all")
    q.add_argument("--tier", default="metadata", help="comma list: metadata,clip,ram,caption,places,all")
    q.add_argument("--min-prob", type=float)
    q.add_argument("--min-rating", type=int, default=0)
    q.add_argument("--confirm-above", type=float, help="auto-confirm tags with confidence above this")
    _sel(q)
    q = sub(tg, "people", cmd_tag_people, "faces: --detect, --cluster, --name CLUSTER NAME, --match (default: list people)")
    q.add_argument("--detect", action="store_true")
    q.add_argument("--cluster", action="store_true")
    q.add_argument("--name", nargs=2, metavar=("CLUSTER_ID", "NAME"))
    q.add_argument("--match", action="store_true")
    q.add_argument("--eps", type=float, default=0.65)
    q.add_argument("--min-samples", type=int, default=3)
    _sel(q)
    q = sub(tg, "places", cmd_tag_places, "tag GPS images places|country|region|city (offline)")
    _sel(q)

    p = P("events", help="split the timeline into events (time gaps + GPS); --apply creates tags and albums")
    p.add_argument("--gap-hours", type=float, help="fixed gap instead of adaptive")
    p.add_argument("--merge-km", type=float, default=1.0)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--name", action="append", help="N=Name for event number N (repeatable)")
    p.add_argument("--no-albums", action="store_true")
    p.add_argument("--no-tags", action="store_true")
    p.add_argument("--full", action="store_true")
    _sel(p)
    p.set_defaults(func=cmd_events)

    # ---- album
    a = P("album", help="albums: create add remove list show smart delete export gallery contact-sheet")
    ag = a.add_subparsers(dest="acmd", required=True, metavar="<action>")
    q = sub(ag, "create", cmd_album_create, "create a static album (name 'a/b' makes a hierarchy); --rule makes it smart")
    q.add_argument("name")
    q.add_argument("--parent")
    q.add_argument("--description")
    q.add_argument("--rule", help="JSON/@file or DSL: makes the album smart")
    q.add_argument("--exist-ok", action="store_true")
    _sel(q)
    q = sub(ag, "smart", cmd_album_smart, "create a smart album from a rule")
    q.add_argument("name")
    q.add_argument("--rule", required=True)
    q.add_argument("--parent")
    q.add_argument("--description")
    q.add_argument("--exist-ok", action="store_true")
    q = sub(ag, "add", cmd_album_add, "add images to a static album")
    q.add_argument("album")
    _sel(q)
    q = sub(ag, "remove", cmd_album_remove, "remove images from a static album")
    q.add_argument("album")
    _sel(q)
    q = sub(ag, "list", cmd_album_list, "list albums")
    q.add_argument("--tree", action="store_true")
    q = sub(ag, "show", cmd_album_show, "show an album's images")
    q.add_argument("album")
    q.add_argument("--children", action="store_true")
    q.add_argument("--limit", type=int)
    q = sub(ag, "delete", cmd_album_delete, "delete an album (never the images)")
    q.add_argument("album")
    q.add_argument("--recursive", action="store_true")
    q = sub(ag, "export", cmd_album_export, "export an album to a folder (originals and/or rendered preset), optional zip")
    q.add_argument("album")
    q.add_argument("dest")
    q.add_argument("--mode", choices=["copy", "symlink", "hardlink"], default="copy")
    q.add_argument("--preset", help="render through the export engine instead of / besides copying originals")
    q.add_argument("--no-originals", action="store_true")
    q.add_argument("--no-raw", action="store_true")
    q.add_argument("--no-jpg", action="store_true")
    q.add_argument("--sidecars", action="store_true")
    q.add_argument("--naming", default="{pos:03d}_{stem}")
    q.add_argument("--zip", help="also write a .zip")
    q.add_argument("--overwrite", action="store_true")
    q.add_argument("--children", action="store_true")
    q.add_argument("--dry-run", action="store_true")
    q = sub(ag, "gallery", cmd_album_gallery, "build a static HTML gallery (grid + lightbox, no server)")
    q.add_argument("album")
    q.add_argument("out")
    q.add_argument("--title")
    q.add_argument("--description")
    q.add_argument("--thumb-size", type=int, default=480)
    q.add_argument("--web-size", type=int, default=2048)
    q.add_argument("--no-tags", action="store_true")
    q.add_argument("--originals", action="store_true", help="also copy the originals and link them")
    q.add_argument("--filenames", action="store_true")
    q = sub(ag, "contact-sheet", cmd_album_sheet, "contact sheet (PDF or JPG pages) of an album or @selection")
    q.add_argument("album")
    q.add_argument("out")
    q.add_argument("--cols", type=int, default=5)
    q.add_argument("--page", default="a4")
    q.add_argument("--dpi", type=int, default=150)
    q.add_argument("--title")
    q.add_argument("--landscape", action="store_true")
    q.add_argument("--no-captions", action="store_true")

    # ---- analysis
    p = P("analyze", help="technical analysis: sharpness, exposure, eyes, tilt, noise (cached in the catalog)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--aesthetic", action="store_true", help="also LAION aesthetic score (optional model)")
    p.add_argument("--show", type=int, default=0, help="include the analysis of the first N images")
    _sel(p)
    p.set_defaults(func=cmd_analyze)

    p = P("cull", help="rank and suggest pick/maybe/reject with reasons (never deletes; --apply writes flags)")
    p.add_argument("--target", type=float, help="keep about this fraction (0.2 or 20 = 20%%)")
    p.add_argument("--genre", help="portrait, landscape, wildlife, street, event, macro, astro ... (default: auto)")
    p.add_argument("--aesthetic", action="store_true")
    p.add_argument("--apply", action="store_true", help="write the suggested flags (sidecar + XMP); flags you set by hand are kept")
    p.add_argument("--overwrite", action="store_true", help="with --apply: also replace flags that were set by hand")
    p.add_argument("--rate", action="store_true", help="with --apply: also give unrated images 1-5 stars from the score")
    p.add_argument("--label-maybe", help="with --apply: colour label for maybes")
    p.add_argument("--clear-maybe", action="store_true")
    p.add_argument("--move-rejects", action="store_true", help="with --apply: move rejects to _rejects/ (not deleted)")
    p.add_argument("--rejects-dir", default="_rejects")
    p.add_argument("--dry-run", action="store_true", help="with --apply: show what would change")
    p.add_argument("--full", action="store_true")
    _sel(p)
    p.set_defaults(func=cmd_cull)

    p = P("group", help="group bursts / near-duplicates (stored as burst and scene ids)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--full", action="store_true")
    _sel(p)
    p.set_defaults(func=cmd_group)

    p = P("thumbs", help="(re)build cached thumbnails (keyed by the edit recipe hash)")
    p.add_argument("--sizes", default="160,480")
    p.add_argument("--force", action="store_true")
    p.add_argument("--prune", action="store_true", help="delete thumbnails of recipes that no longer exist")
    _sel(p)
    p.set_defaults(func=cmd_thumbs)

    # ---- catalog
    c = P("catalog", help="catalog maintenance: export import rebuild rewrite-sidecars")
    cg = c.add_subparsers(dest="ccmd2", required=True, metavar="<action>")
    q = sub(cg, "export", cmd_catalog_export, "dump ratings/tags/albums to open formats (json, csv, tags, albums)")
    q.add_argument("out")
    q.add_argument("--formats", default="json,csv,tags,albums")
    q.add_argument("--xmp", action="store_true", help="also write a .xmp next to each image in the dump")
    q.add_argument("--sqlite", action="store_true", help="also copy the SQLite file")
    q = sub(cg, "import", cmd_catalog_import, "re-apply a catalog export (matched by relative path)")
    q.add_argument("src")
    q = sub(cg, "rebuild", cmd_catalog_rebuild, "drop and rebuild the database from sidecars + XMP")
    q.add_argument("--drop-analysis", action="store_true")
    q.add_argument("--hash", action="store_true")
    q = sub(cg, "rewrite-sidecars", cmd_catalog_rewrite, "re-write sidecar + XMP from the catalog")
    _sel(q)
