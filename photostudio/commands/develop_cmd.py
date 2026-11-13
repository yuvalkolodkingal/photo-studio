"""develop / edit / render / versions / copy-settings / paste-settings / reset commands."""
from __future__ import annotations

import time
from pathlib import Path

from .. import sidecar
from ..cli import add_selection_args, flatten_sets, load_mask_arg, resolve_images, resolve_one
from ..util import IMAGE_EXTS, RAW_EXTS, cache_home

SPACES = ["srgb", "p3", "adobe"]
PHOTO_EXTS = RAW_EXTS | IMAGE_EXTS


def _images(args) -> list[Path]:
    """Files, folders, globs, library selectors (id:N, @album:X, @last-import ...) and --query, via the shared resolver."""
    return resolve_images(args.files, args, exts=PHOTO_EXTS)


def _checked_edit(args):
    """Parse and validate --set / --mask once, before any sidecar is touched. Returns (assignments, mask)."""
    from ..pipeline.validate import validate_assignments

    sets = flatten_sets(getattr(args, "set", None))
    mask = load_mask_arg(getattr(args, "mask", None))
    if mask and not sets:
        raise ValueError("--mask needs at least one --set (the edited op gets the mask)")
    mask = validate_assignments(sets, mask)
    return sets, mask


# ------------------------------------------------------------------ helpers
def _out_path(src: Path, version: str, args, preview: int | None) -> Path:
    out = getattr(args, "out", None)
    if out:
        o = Path(out).expanduser()
        if o.is_dir() or str(out).endswith("/") or not o.suffix:
            o.mkdir(parents=True, exist_ok=True)
            return o / f"{src.stem}{'' if version == 'default' else '_' + version}.jpg"
        return o
    d = cache_home() / "develop"
    d.mkdir(parents=True, exist_ok=True)
    tag = "" if version == "default" else f"_{version}"
    return d / f"{src.stem}{tag}{'_' + str(preview) + 'px' if preview else ''}.jpg"


def _summarise_stack(stack: list[dict]) -> list[dict]:
    rows = []
    for e in stack:
        p = e.get("params") or {}
        rows.append({"op": e["op"] + (f"@{e['id']}" if e.get("id") else ""), "on": bool(e.get("enabled", True)),
                     "masked": (e["mask"].get("type") if e.get("mask") else None),
                     "params": ", ".join(f"{k}={v}" for k, v in p.items())[:90]})
    return rows


def _ensure_version(doc: dict, version: str | None) -> str:
    name = version or doc["active"]
    if name not in doc["versions"]:
        sidecar.new_version(doc, name, from_version=doc["active"], switch=False)
    return name


def _preview_for_detect(path: Path):
    try:
        from ..decode import preview_display

        return preview_display(path, 512)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ develop
def _develop_one(f: Path, args, multi: bool, checked) -> dict:
    from .. import metadata
    from ..pipeline import engine

    t0 = time.time()
    kind = engine.source_kind(f)
    doc = sidecar.load(f)
    vname = _ensure_version(doc, args.version)
    meta = metadata.read_metadata(f)
    cur = sidecar.get_stack(doc, vname)
    vrec = doc["versions"][vname]
    mode_arg, art_arg = args.mode, args.art
    info: dict = {"mode": vrec.get("mode"), "art": vrec.get("art")}
    if mode_arg or art_arg or not cur:
        image = _preview_for_detect(f) if (mode_arg or "").split(":")[0] == "auto" else None
        if mode_arg is None and not cur:
            mode_arg = vrec.get("mode")
        if art_arg is None and not cur:
            art_arg = vrec.get("art")
        stack, rinfo = engine.resolve_stack_ex(mode_arg, art_arg, None, kind, meta, image=image,
                                               base=cur if (args.merge and cur) else None)
        if cur and not args.no_save_sidecar:
            sidecar.snapshot(doc, "auto:before-develop", vname)
        info["mode"] = rinfo["mode"] if (args.mode or not cur) else info["mode"]
        info["art"] = rinfo["art"] if (args.art or not cur) else info["art"]
        if rinfo.get("auto"):
            info["auto"] = rinfo["auto"]
    else:
        stack = cur
    sets, mask = checked
    if sets:
        stack = engine.apply_set(stack, sets, mask=mask)
    if not args.no_save_sidecar:
        sidecar.set_stack(doc, stack, vname, mode=info["mode"], art=info["art"])
        sidecar.save(f, doc)
    res: dict = {"file": str(f), "kind": kind, "version": vname, "mode": info["mode"], "art": info["art"],
                 "n_ops": len(stack), "sidecar": None if args.no_save_sidecar else str(sidecar.sidecar_path(f))}
    if info.get("auto"):
        res["auto"] = info["auto"]
    if args.no_render:
        res["seconds"] = round(time.time() - t0, 3)
        return res
    if args.preset:
        from ..export import export_image_ex

        ov = {"stack": stack}
        if args.out:
            o = Path(args.out).expanduser()
            out = o if (not multi and o.suffix) else o
        else:
            out = None
        r = export_image_ex(f, out, args.preset, vname, **ov)
        res.update({k: r[k] for k in ("out", "width", "height", "bytes", "format") if k in r})
        res["warnings"] = r.get("warnings", [])
    else:
        import numpy as np

        from ..export import save_array

        preview = args.preview
        img, ctx = engine.render(f, stack, long_edge=preview, output_space=args.space, return_ctx=True)
        out = _out_path(f, vname, args, preview)
        save_array(np.asarray(img), out, quality=90 if preview else 92, icc=args.space, metadata_from=f,
                   keep_metadata="all")
        res.update({"out": str(out), "width": int(img.shape[1]), "height": int(img.shape[0]),
                    "warnings": [m for m in ctx.log if "warning" in m]})
    res["seconds"] = round(time.time() - t0, 3)
    return res


def cmd_develop(args):
    checked = _checked_edit(args)                       # validate before anything is written
    files = _images(args)
    multi = len(files) > 1
    if multi and args.out and Path(args.out).suffix and not Path(args.out).is_dir():
        raise ValueError("--out must be a directory when developing several files")
    return [_develop_one(f, args, multi, checked) for f in files]


# ------------------------------------------------------------------ edit
def cmd_edit(args):
    from ..pipeline import engine

    from ..pipeline.validate import check_selectors

    sets, mask = _checked_edit(args)
    files = _images(args)
    out = []
    for f in files:
        doc = sidecar.load(f)
        vname = _ensure_version(doc, args.version)
        stack = sidecar.get_stack(doc, vname)
        if not stack:
            from .. import metadata

            stack = engine.resolve_stack(doc["versions"][vname].get("mode"), doc["versions"][vname].get("art"), None,
                                         engine.source_kind(f), metadata.read_metadata(f))
        before = len(stack)
        changed = False
        absent: list[str] = []
        for kind, names in (("remove", args.remove), ("enable", args.enable), ("disable", args.disable)):
            absent += check_selectors(kind, names or [], stack)
        if sets:
            stack = engine.apply_set(stack, sets, mask=mask)
            changed = True
        for name in args.remove or []:
            op_name, _, eid = name.partition("@")
            stack = [e for e in stack if not (e["op"] == op_name and (not eid or e.get("id") == eid))]
            changed = True
        for flag, names in ((True, args.enable), (False, args.disable)):
            for name in names or []:
                op_name, _, eid = name.partition("@")
                for e in stack:
                    if e["op"] == op_name and (not eid or e.get("id") == eid):
                        e["enabled"] = flag
                        changed = True
        if changed and not args.dry_run:
            sidecar.snapshot(doc, "auto:before-edit", vname)
            sidecar.set_stack(doc, stack, vname)
            sidecar.save(f, doc)
        row = {"file": str(f), "version": vname, "changed": changed, "dry_run": bool(args.dry_run),
               "ops_before": before, "ops_after": len(stack), "stack": _summarise_stack(stack)}
        if absent:
            row["not_in_stack"] = absent
        out.append(row)
    return out


# ------------------------------------------------------------------ render
def cmd_render(args):
    import numpy as np

    from ..export import save_array
    from ..pipeline import engine

    res = []
    for f in _images(args):
        t0 = time.time()
        doc = sidecar.load(f)
        vname = args.version or doc["active"]
        stack = engine.stack_for(f, vname)
        img, ctx = engine.render(f, stack, long_edge=args.preview, output_space=args.space,
                                 upto_stage=args.upto_stage, return_ctx=True)
        out = _out_path(f, vname, args, args.preview)
        save_array(np.asarray(img), out, quality=90, icc=args.space, metadata_from=f, keep_metadata="all")
        res.append({"file": str(f), "version": vname, "out": str(out), "width": int(img.shape[1]),
                    "height": int(img.shape[0]), "log": ctx.log, "seconds": round(time.time() - t0, 3)})
    return res


# ------------------------------------------------------------------ versions
def cmd_versions(args):
    f = resolve_one(args.file, args)
    doc = sidecar.load(f)
    act = args.vcmd
    msg = None
    if act == "list":
        return {"file": str(f), "active": doc["active"], "versions": sidecar.list_versions(doc)}
    if act == "snapshots":
        return {"file": str(f), "snapshots": sidecar.list_snapshots(doc)}
    if act == "new":
        sidecar.new_version(doc, args.name, from_version=args.from_version, switch=not args.no_switch)
        msg = f"created version '{args.name}'"
    elif act == "switch":
        sidecar.switch_version(doc, args.name)
        msg = f"active version is now '{args.name}'"
    elif act == "delete":
        sidecar.delete_version(doc, args.name)
        msg = f"deleted version '{args.name}'"
    elif act == "rename":
        sidecar.rename_version(doc, args.name, args.new_name)
        msg = f"renamed '{args.name}' to '{args.new_name}'"
    elif act == "snapshot":
        sidecar.snapshot(doc, args.name, args.version)
        msg = f"snapshot '{args.name}' saved"
    elif act == "restore":
        sidecar.restore_snapshot(doc, args.name, args.version)
        msg = f"restored snapshot '{args.name}'"
    sidecar.save(f, doc)
    return {"file": str(f), "message": msg, "active": doc["active"], "versions": sidecar.list_versions(doc)}


# ------------------------------------------------------------------ copy / paste / reset
def cmd_copy(args):
    ops = [o for o in (args.ops or "").split(",") if o]
    clip = sidecar.copy_settings(resolve_one(args.file, args), ops or None, args.version)
    return {"copied_from": clip["source"], "version": clip["version"], "n_ops": len(clip["stack"]),
            "ops": [e["op"] for e in clip["stack"]]}


def cmd_paste(args):
    ops = [o for o in (args.ops or "").split(",") if o]
    files = _images(args)
    if args.src:
        outs = sidecar.copy_between(resolve_one(args.src, args), files, ops or None, args.src_version, args.mode, args.version)
        return {"pasted_from": args.src, "to": [str(p) for p in outs]}
    res = []
    for f in files:
        doc = sidecar.paste_settings(f, args.version, args.mode, ops or None)
        res.append(str(sidecar.sidecar_path(f)))
        del doc
    return {"pasted_to": res, "mode": args.mode}


def cmd_reset(args):
    out = []
    for f in _images(args):
        doc = sidecar.load(f)
        sidecar.reset(doc, args.version)
        sidecar.save(f, doc)
        out.append(str(f))
    return {"reset": out, "note": "previous stack kept as snapshot 'auto:before-reset' (photo versions restore)"}


# ------------------------------------------------------------------ registration
def register(sub) -> None:
    p = sub.add_parser("develop", help="develop images: resolve defaults+mode+art+--set, save sidecar, render",
                       description="photo develop <files...> [--mode portrait|night:astro|auto] [--art NAME] "
                                   "[--set exposure.ev=+0.3,white_balance.temperature=5200] [--mask JSON] "
                                   "[--version V] [--out PATH] [--preset EXPORT] [--preview N] [--no-save-sidecar]")
    add_selection_args(p)
    p.add_argument("--mode", help="shoot-type mode (portrait, animal, landscape, night, macro, street, product, "
                                  "sports, architecture, events, video, or auto); 'name:variant' for variants")
    p.add_argument("--art", help="art/look preset name")
    p.add_argument("--set", action="append", help="op.param=value (repeatable or comma separated); "
                                                  "op@id.param addresses an entry id; names are checked first")
    p.add_argument("--mask", help="mask for the --set edits (a new masked entry): JSON, @file.json, or a shorthand "
                                  "(sky, subject, background, face, skin, eyes, lips, teeth, animal)")
    p.add_argument("--version", help="sidecar version (virtual copy) to edit; created if missing")
    p.add_argument("--out", help="output file or directory (default: ~/.cache/photostudio/develop)")
    p.add_argument("--preset", help="export preset to render with (web, instagram_portrait, print_tiff16, ...)")
    p.add_argument("--preview", type=int, help="render a preview with this long edge (fast, cached)")
    p.add_argument("--space", choices=SPACES, default="srgb", help="output colour space for non-preset renders")
    p.add_argument("--merge", action="store_true", help="apply --mode/--art on top of the existing stack")
    p.add_argument("--no-save-sidecar", action="store_true", help="do not write the sidecar")
    p.add_argument("--no-render", action="store_true", help="update the sidecar only")
    p.set_defaults(func=cmd_develop)

    p = sub.add_parser("edit", help="modify the sidecar stack only (no render); no edits = show the stack")
    add_selection_args(p)
    p.add_argument("--set", action="append")
    p.add_argument("--mask")
    p.add_argument("--version")
    p.add_argument("--remove", action="append", metavar="OP[@ID]")
    p.add_argument("--enable", action="append", metavar="OP[@ID]")
    p.add_argument("--disable", action="append", metavar="OP[@ID]")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("render", help="render the stored edit (no sidecar changes)")
    add_selection_args(p)
    p.add_argument("--version")
    p.add_argument("--out")
    p.add_argument("--preview", type=int)
    p.add_argument("--upto-stage", type=int, help="stop after this pipeline stage (debug)")
    p.add_argument("--space", choices=SPACES, default="srgb")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("versions", help="virtual copies and history snapshots: list|new|switch|delete|rename|"
                                        "snapshot|snapshots|restore")
    vs = p.add_subparsers(dest="vcmd", required=True)
    for name, extra in (("list", []), ("snapshots", []), ("new", ["name"]), ("switch", ["name"]),
                        ("delete", ["name"]), ("rename", ["name", "new_name"]), ("snapshot", ["name"]),
                        ("restore", ["name"])):
        q = vs.add_parser(name)
        q.add_argument("file")
        for e in extra:
            q.add_argument(e)
        if name == "new":
            q.add_argument("--from", dest="from_version")
            q.add_argument("--no-switch", action="store_true")
        if name in ("snapshot", "restore"):
            q.add_argument("--version")
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("copy-settings", help="copy a stack (or some ops) to the settings clipboard")
    p.add_argument("file")
    p.add_argument("--ops", help="comma separated op names/ids to copy (default: all)")
    p.add_argument("--version")
    p.set_defaults(func=cmd_copy)

    p = sub.add_parser("paste-settings", help="paste the clipboard (or --from IMAGE) onto images")
    add_selection_args(p)
    p.add_argument("--from", dest="src", help="copy straight from this image instead of the clipboard")
    p.add_argument("--src-version", help="version of --from to copy")
    p.add_argument("--mode", choices=["replace", "merge", "append"], default="replace")
    p.add_argument("--ops")
    p.add_argument("--version", help="destination version")
    p.set_defaults(func=cmd_paste)

    p = sub.add_parser("reset", help="clear the edit stack of images (kept as an auto snapshot)")
    add_selection_args(p)
    p.add_argument("--version")
    p.set_defaults(func=cmd_reset)
