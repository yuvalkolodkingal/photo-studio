"""`photo mask preview`, `photo scopes`, `photo stack hdr|focus|astro|pano|trails|long|median`, `photo models list|download`."""
from __future__ import annotations

from pathlib import Path

import numpy as np


# --------------------------------------------------------------------------- shared helpers
def _json_arg(text):
    """Mask spec argument: JSON, @file.json or a bare shorthand (sky, subject ...); the same parser as ``develop --mask``."""
    from ..cli import load_mask_arg

    return load_mask_arg(text)


def _one(item, args=None) -> str:
    from ..cli import resolve_one

    return str(resolve_one(item, args))


def _expand(files, args=None):
    from ..cli import resolve_images

    return [str(p) for p in resolve_images(list(files), args, exts=None)]


def _tonemap_fallback(lin: np.ndarray) -> np.ndarray:
    """Linear Rec.2020 -> display sRGB without the engine: exposure-normalise, sigmoid, matrix, OETF."""
    from .. import color
    from ..pipeline.ops.basic import sigmoid_curve

    y = float(np.median(color.luminance(np.maximum(lin, 0))[::4, ::4]))
    gain = float(np.clip(0.18 / max(y, 1e-4), 0.25, 16.0))
    x = sigmoid_curve(np.maximum(lin * gain, 0), 1.4)
    x = color.apply_matrix(x, color.REC2020_TO_SRGB)
    return color.srgb_oetf(np.clip(x, 0, 1))


def _display_from_linear(lin: np.ndarray, path: str | None = None, kind: str = "raw") -> np.ndarray:
    """Tone-map a stack result for saving: through the engine when possible (consistent look), else a sigmoid."""
    try:
        from .. import decode
        from ..pipeline import engine

        d = decode.Decoded(lin, kind, path or "", {})
        return np.clip(engine.render(d, engine.default_stack(kind)), 0, 1)
    except Exception:  # noqa: BLE001
        return _tonemap_fallback(lin)


def _save(img_display: np.ndarray, out: str, bits: int = 8, quality: int = 92) -> str:
    out_p = Path(out).expanduser()
    out_p.parent.mkdir(parents=True, exist_ok=True)
    try:
        from .. import export

        return str(export.save_array(img_display, out_p, None, quality=quality, bits=bits))
    except Exception:  # noqa: BLE001
        import cv2

        a = np.clip(img_display, 0, 1)
        if bits == 16 and out_p.suffix.lower() in (".tif", ".tiff", ".png"):
            cv2.imwrite(str(out_p), (a[..., ::-1] * 65535 + 0.5).astype(np.uint16))
        else:
            cv2.imwrite(str(out_p), (a[..., ::-1] * 255 + 0.5).astype(np.uint8),
                        [cv2.IMWRITE_JPEG_QUALITY, quality])
        return str(out_p)


def _load_linear(path: str, long_edge: int | None):
    try:
        from ..pipeline import engine

        return engine.load_source(path, long_edge)
    except Exception:  # noqa: BLE001
        from .. import decode
        from ..util import resize_long

        d = decode.decode(path)
        if long_edge and max(d.image.shape[:2]) > long_edge:
            d.image = resize_long(d.image, long_edge)
        return d


# --------------------------------------------------------------------------- mask preview
def cmd_mask_preview(args):
    from ..masks import build_mask, mask_preview
    from ..pipeline.registry import Ctx

    spec = _json_arg(args.spec)
    args.image = _one(args.image, args)
    dec = _load_linear(args.image, args.size)
    lin = np.asarray(dec.image, np.float32)
    ctx = Ctx(domain="linear", kind=dec.kind, meta=dec.meta, path=args.image, source=lin)
    mask = build_mask(spec, lin, ctx)
    overlay = mask_preview(spec, lin, ctx)
    out = args.out or str(Path(args.image).with_suffix("")) + ".mask.png"
    from PIL import Image

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(overlay).save(out)
    res = {"out": out, "size": [int(mask.shape[1]), int(mask.shape[0])], "coverage": round(float(mask.mean()), 4),
           "notes": list(ctx.log)}
    if args.mask_out:
        from ..util import to_uint8

        Image.fromarray(to_uint8(mask)).save(args.mask_out)
        res["mask_out"] = args.mask_out
    return res


def cmd_mask_types(args):
    from ..masks import BLEND_MODES, MASK_TYPES

    return {"mask_types": MASK_TYPES, "blend_modes": BLEND_MODES}


# --------------------------------------------------------------------------- scopes
def cmd_scopes(args):
    from .. import scopes

    args.image = _one(args.image, args)
    dec = _load_linear(args.image, args.size)
    disp = _display_from_linear(np.asarray(dec.image, np.float32), args.image, dec.kind)
    out_dir = args.out or str(Path(args.image).with_suffix("")) + "_scopes"
    res = scopes.scope_pack(disp, out_dir, "display", prefix="")
    hist = scopes.histogram(disp, "display")
    res["clip_high_pct"] = {k: round(v, 3) for k, v in hist["clip_high_pct"].items()}
    res["clip_low_pct"] = {k: round(v, 3) for k, v in hist["clip_low_pct"].items()}
    res["median_ev"] = round(hist["ev"]["median_ev"], 2)
    return res


# --------------------------------------------------------------------------- stack
def cmd_stack(args):
    from .. import multiframe as mf

    files = _expand(args.files, args)
    kind = args.kind
    info: dict = {}
    if kind == "hdr":
        exp = [float(x) for x in args.exposures.split(",")] if args.exposures else None
        res, info = mf.hdr_merge(files, method=args.method or "linear", align=args.align or "mtb", exposures=exp,
                                 deghost=args.deghost, return_info=True)
    elif kind == "focus":
        res, info = mf.focus_stack(files, align=args.align or "ecc_affine", return_info=True)
        info.pop("depth_index", None)
    elif kind == "astro":
        sky = None
        if args.freeze_ground:
            sky = "auto" if args.freeze_ground == "auto" else _json_arg(args.freeze_ground)
        res, info = mf.astro_stack(files, combine=args.method or "kappa_sigma", kappa=args.kappa,
                                   darks=_expand(args.darks, args) if args.darks else None,
                                   flats=_expand(args.flats, args) if args.flats else None,
                                   bias=_expand(args.bias, args) if args.bias else None, sky_mask=sky,
                                   keep_fraction=args.keep, return_info=True)
    elif kind == "pano":
        res, info = mf.panorama(files, mode=args.method or "panorama", return_info=True)
    elif kind == "trails":
        res, info = mf.star_trails(files, mode=args.method or "lighten", fade=args.fade, return_info=True)
    elif kind == "long":
        res, info = mf.long_exposure(files, align=args.align or "none", return_info=True)
    elif kind == "median":
        res, info = mf.median_stack(files, align=args.align or "none", return_info=True)
    else:
        raise ValueError(kind)
    out = args.out or str(Path(files[0]).with_name(f"{Path(files[0]).stem}_{kind}.tif"))
    bits = 16 if Path(out).suffix.lower() in (".tif", ".tiff") else 8
    saved = _save(_display_from_linear(res, files[0]), out, bits=bits)
    ret = {"kind": kind, "frames": len(files), "out": saved, "size": [int(res.shape[1]), int(res.shape[0])]}
    if args.linear_out:
        np.save(args.linear_out, res)
        ret["linear_out"] = args.linear_out
    ret.update({k: v for k, v in info.items() if k in ("notes", "used_frames", "stars_reference", "relative_gains", "reference")})
    return ret


# --------------------------------------------------------------------------- models
def cmd_models_list(args):
    from .. import models

    rows = []
    for name in sorted(models.REGISTRY):
        m = models.REGISTRY[name]
        a = models.available(name)
        rows.append({"name": name, "installed": a["installed"], "deps_ok": a["deps_ok"], "size_mb": m.size_mb,
                     "licence": m.licence, "purpose": m.purpose, "path": a["path"]})
    return rows


def cmd_models_download(args):
    from .. import models

    names = list(models.REGISTRY) if args.names == ["all"] else args.names
    done = {}
    for n in names:
        try:
            p = models.download(n, force=args.force)
            done[n] = str(p)
        except Exception as exc:  # noqa: BLE001
            done[n] = f"ERROR: {exc}"
    return done


# --------------------------------------------------------------------------- registration
def register(subparsers) -> None:
    from ..cli import add_selection_args

    p = subparsers.add_parser("mask", help="mask tools: preview a mask spec as a red overlay")
    sp = p.add_subparsers(dest="mask_cmd", required=True)
    q = sp.add_parser("preview", help="photo mask preview IMG --spec '<json|type>' --out x.png")
    q.add_argument("image")
    q.add_argument("--spec", required=True, help="mask spec JSON, @file.json, or a shorthand (sky, subject, background, face, "
                                                 "skin, eyes, lips, teeth, animal)")
    q.add_argument("--out")
    q.add_argument("--mask-out", help="also write the raw 8-bit mask PNG")
    q.add_argument("--size", type=int, default=1600, help="long edge for the preview decode (default 1600)")
    q.set_defaults(func=cmd_mask_preview)
    q = sp.add_parser("types", help="list mask types and blend modes")
    q.set_defaults(func=cmd_mask_types)

    p = subparsers.add_parser("scopes", help="histogram / waveform / parade / vectorscope / false colour / clipping PNGs")
    p.add_argument("image")
    p.add_argument("--out", help="output directory")
    p.add_argument("--size", type=int, default=1200, help="long edge used for the scope computation")
    p.set_defaults(func=cmd_scopes)

    p = subparsers.add_parser("stack", help="multi-frame stacking: hdr | focus | astro | pano | trails | long | median")
    p.add_argument("kind", choices=["hdr", "focus", "astro", "pano", "trails", "long", "median"])
    add_selection_args(p, files_help="frames: files, globs, folders or selectors (id:N, @album:NAME, @last-import ...)")
    p.add_argument("--out", help="output image (.tif = 16-bit, .jpg/.png = 8-bit)")
    p.add_argument("--linear-out", help="also save the float32 linear result as .npy")
    p.add_argument("--method", help="hdr: linear|mertens|debevec; astro: kappa_sigma|mean|median|max; pano: panorama|scans; "
                                    "trails: lighten|comet")
    p.add_argument("--align", help="none|mtb|ecc|ecc_affine|ecc_homography|feature")
    p.add_argument("--exposures", help="hdr: comma separated relative exposure gains (default from EXIF)")
    p.add_argument("--deghost", action="store_true", help="hdr: suppress moving objects")
    p.add_argument("--kappa", type=float, default=2.5, help="astro: kappa-sigma threshold")
    p.add_argument("--darks", nargs="+")
    p.add_argument("--flats", nargs="+")
    p.add_argument("--bias", nargs="+")
    p.add_argument("--keep", type=float, default=1.0, help="astro: fraction of frames to keep (best star counts)")
    p.add_argument("--freeze-ground", nargs="?", const="auto",
                   help="astro: keep the foreground unaligned ('auto' = AI sky mask, or a mask spec JSON)")
    p.add_argument("--fade", type=float, default=0.0, help="trails comet mode: decay per frame")
    p.set_defaults(func=cmd_stack)

    p = subparsers.add_parser("models", help="optional AI models: list | download")
    sp = p.add_subparsers(dest="models_cmd", required=True)
    q = sp.add_parser("list", help="show registered models and their install state")
    q.set_defaults(func=cmd_models_list)
    q = sp.add_parser("download", help="download models into ~/.cache/photostudio/models (explicit network use)")
    q.add_argument("names", nargs="+", help="model names or 'all'")
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=cmd_models_download)
