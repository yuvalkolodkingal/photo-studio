"""export / contact-sheet / lut bake commands."""
from __future__ import annotations

import time
from pathlib import Path

from ..cli import add_selection_args, flatten_sets, load_mask_arg, resolve_images, resolve_one
from ..util import IMAGE_EXTS, RAW_EXTS


def _coerce(v: str):
    from ..util import parse_value

    return parse_value(v)


def cmd_export(args):
    from .. import export as ex

    files = resolve_images(args.files, args, exts=RAW_EXTS | IMAGE_EXTS)
    overrides: dict = {}
    for kv in flatten_sets(args.set):
        if "=" not in kv:
            raise ValueError(f"--set expects key=value for export options, got '{kv}'")
        k, v = kv.split("=", 1)
        overrides[k.strip()] = _coerce(v.strip())
    for name in ("quality", "long_edge", "bits", "dpi", "icc", "metadata", "fit", "copyright", "artist"):
        v = getattr(args, name, None)
        if v is not None:
            overrides[name] = v
    if args.format:
        overrides["format"] = args.format
    if args.watermark:
        overrides["watermark"] = {"text": args.watermark}
    if args.border is not None:
        overrides["border"] = {"size": args.border, "color": args.border_color}
    if args.skip_existing:
        overrides["skip_existing"] = True
    out_dir = args.out
    if out_dir and len(files) > 1 and Path(out_dir).suffix and not Path(out_dir).is_dir():
        raise ValueError("--out must be a directory when exporting several files")
    t0 = time.time()
    results = ex.export_batch([str(f) for f in files], preset=args.preset, out_dir=out_dir, version=args.version,
                              jobs=args.jobs, name=args.name, **overrides)
    ok = [r for r in results if r.get("ok")]
    out = {"preset": args.preset, "count": len(results), "ok": len(ok), "failed": len(results) - len(ok),
           "seconds": round(time.time() - t0, 2), "results": results}
    if args.proof:
        from PIL import Image

        from ..export import soft_proof
        from ..util import imread_display, to_uint8

        proofs = []
        for r in ok:
            img = imread_display(r["out"])
            p = soft_proof(img, args.proof, r.get("icc", "srgb"), gamut_warning=args.gamut_warning)
            pp = Path(r["out"]).with_name(Path(r["out"]).stem + "_proof.jpg")
            Image.fromarray(to_uint8(p)).save(pp, quality=90)
            proofs.append(str(pp))
        out["proofs"] = proofs
    if args.contact_sheet and ok:
        sheet = ex.build_contact_sheet([r["out"] for r in ok], args.contact_sheet, columns=args.columns,
                                       title=args.title)
        out["contact_sheet"] = str(sheet)
    if out["failed"]:
        out["errors"] = [{"src": r["src"], "error": r.get("error")} for r in results if not r.get("ok")]
    return out


def cmd_contact_sheet(args):
    from .. import export as ex

    files = resolve_images(args.files, args, exts=RAW_EXTS | IMAGE_EXTS)
    out = ex.build_contact_sheet([str(f) for f in files], args.out, columns=args.columns, cell=args.cell,
                                 captions=not args.no_captions, title=args.title, page=args.page)
    return {"out": str(out), "images": len(files)}


def cmd_lut_bake(args):
    import numpy as np

    from .. import metadata, sidecar
    from ..pipeline import engine

    if args.file:
        f = resolve_one(args.file, args)
        kind = engine.source_kind(f)
        meta = metadata.read_metadata(f)
        stack = engine.stack_for(f, args.version) if not (args.mode or args.art) else \
            engine.resolve_stack(args.mode, args.art, sidecar.get_stack(sidecar.load(f), args.version) or None, kind, meta)
    else:
        kind, meta = "jpeg", {}
        stack = engine.resolve_stack(args.mode, args.art, None, "jpeg", meta)
    sets = flatten_sets(args.set)
    if sets:
        from ..pipeline.validate import validate_assignments

        mask = validate_assignments(sets, load_mask_arg(args.mask))
        stack = engine.apply_set(stack, sets, mask=mask)
    skipped: list[str] = []
    lut = engine.bake_lut(stack, args.size, kind="jpeg" if kind != "raw" else "jpeg", meta=meta, skipped=skipped)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    engine.write_cube(out, lut, title=out.stem)
    ident = np.linspace(0, 1, args.size, dtype=np.float32)
    dev = float(np.abs(lut[..., 1] - ident[None, :, None]).mean())
    return {"out": str(out), "size": args.size, "skipped_spatial_ops": skipped, "mean_green_shift": round(dev, 4),
            "ffmpeg": f"ffmpeg -i in.mp4 -vf lut3d='{out}' out.mp4"}


def register(sub) -> None:
    p = sub.add_parser("export", help="develop + resize + sharpen + encode with ICC (export presets)",
                       description="photo export <files...> --preset web|instagram_portrait|instagram_square|"
                                   "story_9x16|print_tiff16|archive_full|wallpaper_4k|email_small "
                                   "[--out DIR] [--name '{date:%%Y%%m%%d}_{seq:03d}_{stem}'] [--jobs N]")
    add_selection_args(p)
    p.add_argument("--preset", default="web")
    p.add_argument("--out", help="output directory (default: <image dir>/exports)")
    p.add_argument("--name", help="naming template: {stem} {date:%%Y%%m%%d} {seq:04d} {mode} {art} {preset} ...")
    p.add_argument("--version")
    p.add_argument("--jobs", type=int, help="parallel worker processes (default: up to 4)")
    p.add_argument("--set", action="append", help="export option override key=value (long_edge=1600, quality=85, ...)")
    p.add_argument("--format")
    p.add_argument("--quality", type=int)
    p.add_argument("--long-edge", dest="long_edge", type=int)
    p.add_argument("--bits", type=int, choices=[8, 16])
    p.add_argument("--dpi", type=int)
    p.add_argument("--icc", choices=["srgb", "p3", "adobe", "none"])
    p.add_argument("--metadata", choices=["all", "copyright", "none"])
    p.add_argument("--fit", choices=["contain", "pad", "cover", "exact"])
    p.add_argument("--copyright")
    p.add_argument("--artist")
    p.add_argument("--watermark", help="text watermark")
    p.add_argument("--border", type=float, help="border size (fraction of the long edge, or px if >= 1)")
    p.add_argument("--border-color", default="#ffffff")
    p.add_argument("--skip-existing", action="store_true")
    p.add_argument("--proof", help="paper/printer ICC profile: also write a soft-proof JPEG next to each export")
    p.add_argument("--gamut-warning", action="store_true")
    p.add_argument("--contact-sheet", help="also write a contact sheet of the exports to this path")
    p.add_argument("--columns", type=int, default=5)
    p.add_argument("--title")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("contact-sheet", help="contact sheet (JPEG/PNG/PDF) from previews")
    add_selection_args(p)
    p.add_argument("--out", required=True)
    p.add_argument("--columns", type=int, default=5)
    p.add_argument("--cell", type=int, default=360)
    p.add_argument("--title")
    p.add_argument("--page", choices=["A4", "letter", "A3"], help="render at 300 dpi sized to this page")
    p.add_argument("--no-captions", action="store_true")
    p.set_defaults(func=cmd_contact_sheet)

    lut = sub.add_parser("lut", help="bake a look into a 3D LUT: photo lut bake ...")
    ls = lut.add_subparsers(dest="lcmd", required=True)
    b = ls.add_parser("bake", help="render an identity lattice through the colour-only ops of a stack -> .cube")
    b.add_argument("--file", help="take the stack from this image's sidecar (a path or id:N)")
    b.add_argument("--version")
    b.add_argument("--mode")
    b.add_argument("--art")
    b.add_argument("--set", action="append")
    b.add_argument("--mask")
    b.add_argument("--size", type=int, default=33, choices=[17, 33, 65])
    b.add_argument("--out", required=True)
    b.set_defaults(func=cmd_lut_bake)
