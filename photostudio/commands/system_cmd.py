"""presets / ops / info / status / calibrate commands."""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from ..cli import add_selection_args, resolve_images
from ..util import MissingDependency, cache_home, data_home, have_module, preset_dirs, which


# ------------------------------------------------------------------ presets
def cmd_presets(args):
    from .. import presets

    act = args.pcmd
    if act == "list":
        kinds = [args.kind] if args.kind else ["mode", "art", "export", "video", "picture_style"]
        out = {}
        for k in kinds:
            nk = presets.norm_kind(k)
            out[nk] = [{"name": p["name"], "title": p["title"], "variants": ", ".join(p.get("variants", [])),
                        "description": (p.get("description") or "")[:100], **({"error": p["error"]} if "error" in p else {})}
                       for p in presets.list_presets(nk)]
        return out if not args.kind else out[presets.norm_kind(args.kind)]
    if act == "show":
        d = presets.load_preset(args.kind, args.name)
        d.pop("path", None) if not args.paths else None
        return d
    if act == "validate":
        kinds = [args.kind] if args.kind else ["mode", "art", "export", "video", "picture_style"]
        report, bad = [], 0
        for k in kinds:
            for p in presets.list_presets(k):
                if "error" in p:
                    report.append({"kind": k, "name": p["name"], "problems": [p["error"]]})
                    bad += 1
                    continue
                probs = presets.validate_preset(presets.load_preset(k, p["name"]))
                for v in p.get("variants", []):
                    d = presets.load_preset(k, f"{p['name']}:{v}")
                    probs += [f"[{v}] {x}" for x in presets.validate_preset({**d, "variants": {}}) if x not in probs]
                if probs:
                    bad += 1
                    report.append({"kind": k, "name": p["name"], "problems": probs})
        return {"checked_dirs": [str(d) for d in preset_dirs()], "invalid": bad, "problems": report}
    if act == "save":
        from .. import sidecar
        from ..pipeline import engine

        f = Path(args.source)
        doc = sidecar.load(f)
        stack = engine.stack_for(f, args.version)
        d = presets.preset_from_stack(args.kind, args.name, stack, description=args.description or "")
        path = presets.save_user_preset(args.kind, args.name, d)
        return {"saved": str(path), "n_ops": len(stack), "from": str(f), "version": args.version or doc["active"]}
    raise ValueError(act)


# ------------------------------------------------------------------ ops
def cmd_ops(args):
    from ..pipeline import registry
    from ..pipeline.registry import STAGES

    ops = registry.all_ops()
    if args.ocmd == "list":
        rows = [{"op": s.name, "stage": s.stage, "stage_name": STAGES.get(s.stage), "domain": s.domain,
                 "category": s.category, "raw_only": s.raw_only, "doc": s.doc[:70]} for s in ops.values()
                if not args.category or s.category == args.category]
        rows.sort(key=lambda r: (r["stage"], r["op"]))
        out = {"count": len(rows), "ops": rows}
        if registry.LOAD_ERRORS:
            out["module_load_errors"] = registry.LOAD_ERRORS
        return out
    spec = registry.get(args.name)
    d = spec.describe()
    from ..pipeline.engine import is_spatial

    d["spatial"] = is_spatial(spec)
    d["bakeable_to_lut"] = not d["spatial"]
    return d


# ------------------------------------------------------------------ info
def cmd_info(args):
    from .. import metadata, modes, sidecar
    from ..decode import companion_jpeg, is_raw, preview_display
    from ..pipeline import engine

    out = []
    for f in resolve_images(args.files, args):
        meta = metadata.read_metadata(f)
        meta.pop("xmp", None) if not args.xmp else None
        image = None
        try:
            image = preview_display(f, 384)
        except Exception:  # noqa: BLE001
            pass
        det = modes.detect_mode(meta, image)
        det.pop("features", None) if not args.features else None
        doc = sidecar.load(f)
        row = {"file": str(f), "kind": engine.source_kind(f), "size_bytes": f.stat().st_size, "metadata": meta,
               "detected_mode": det, "sidecar": {"exists": sidecar.exists(f), **sidecar.summary(doc)}}
        if is_raw(f):
            cj = companion_jpeg(f)
            row["companion_jpeg"] = str(cj) if cj else None
        if args.raw_dump and is_raw(f):
            from ..decode import raw_dump
            row["raw_dump"] = raw_dump(f)
        out.append(row)
    return out


# ------------------------------------------------------------------ status
PY_MODULES = [("numpy", "core"), ("scipy", "core"), ("cv2", "core (opencv-contrib)"), ("PIL", "core"), ("yaml", "core"),
              ("rawpy", "raw decoding"), ("colour", "colour science"), ("skimage", "core"), ("imagehash", "culling"),
              ("blake3", "hashing"), ("tifffile", "16-bit TIFF export"), ("pillow_heif", "HEIF/AVIF (heif extra)"),
              ("pillow_jxl", "JPEG XL (jxl extra)"), ("lensfunpy", "lens correction (lens extra)"),
              ("exiftool", "PyExifTool (meta extra)"), ("av", "video (video extra)"), ("scenedetect", "video shots"),
              ("onnxruntime", "AI masks (ai extra)"), ("torch", "AI models"), ("mediapipe", "faces"),
              ("rembg", "subject masks"), ("open_clip", "CLIP search"), ("ultralytics", "YOLO"),
              ("insightface", "face clustering"), ("numba", "fast LUTs (fast extra)"), ("bm3d", "BM3D denoise"),
              ("astroalign", "astro stacking"), ("reverse_geocoder", "places"), ("fastapi", "photo serve"),
              ("uvicorn", "photo serve")]
BINARIES = ["exiftool", "ffmpeg", "ffprobe", "rclone", "restic", "nvidia-smi", "hugin_executor", "enblend", "darktable-cli"]


def _gpu() -> dict:
    info: dict = {"nvidia_smi": None, "cuda_torch": None, "onnx_providers": None}
    if which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
                                "--format=csv,noheader"], capture_output=True, text=True, timeout=8)
            info["nvidia_smi"] = r.stdout.strip() or r.stderr.strip()
        except Exception as exc:  # noqa: BLE001
            info["nvidia_smi"] = f"error: {exc}"
    if have_module("torch"):
        try:
            import torch
            info["cuda_torch"] = bool(torch.cuda.is_available())
        except Exception as exc:  # noqa: BLE001
            info["cuda_torch"] = f"error: {exc}"
    if have_module("onnxruntime"):
        try:
            import onnxruntime as ort
            info["onnx_providers"] = ort.get_available_providers()
        except Exception as exc:  # noqa: BLE001
            info["onnx_providers"] = f"error: {exc}"
    return info


def cmd_status(args):
    from .. import presets
    from ..pipeline import engine, registry

    mods = {}
    for name, why in PY_MODULES:
        ok = have_module(name)
        mods[name] = {"ok": ok, "for": why}
    bins = {b: which(b) for b in BINARIES}
    ops = registry.all_ops()
    models_info = None
    try:
        from .. import models

        models_info = models.listing() if hasattr(models, "listing") else None
    except Exception as exc:  # noqa: BLE001
        models_info = {"error": f"{type(exc).__name__}: {exc}"}
    from ..metadata import exiftool_available

    res = {
        "python": sys.version.split()[0], "platform": platform.platform(),
        "photostudio": __import__("photostudio").__version__,
        "modules": mods, "missing_modules": sorted(k for k, v in mods.items() if not v["ok"]),
        "binaries": bins, "exiftool_usable": exiftool_available(), "gpu": _gpu(),
        "ops": {"count": len(ops), "module_load_errors": registry.LOAD_ERRORS},
        "presets": {k: len(presets.list_presets(k)) for k in ("mode", "art", "export", "video", "picture_style")},
        "preset_dirs": [str(d) for d in preset_dirs()],
        "dirs": {"data": str(data_home()), "cache": str(cache_home())},
        "caches": engine.cache_info(),
    }
    if models_info is not None:
        res["models"] = models_info
    res["_text"] = _status_text(res)
    return res


def _status_text(r: dict) -> str:
    ok = [k for k, v in r["modules"].items() if v["ok"]]
    miss = [f"{k} ({r['modules'][k]['for']})" for k in r["missing_modules"]]
    bins = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in r["binaries"].items())
    lines = [f"photostudio {r['photostudio']}  python {r['python']}  {r['platform']}",
             f"modules ok ({len(ok)}): {', '.join(ok)}",
             f"modules missing ({len(miss)}): {', '.join(miss) or '-'}",
             f"binaries: {bins}  exiftool usable: {r['exiftool_usable']}",
             f"gpu: {r['gpu']}",
             f"ops: {r['ops']['count']}  load errors: {r['ops']['module_load_errors'] or '-'}",
             "presets: " + ", ".join(f"{k}={v}" for k, v in r["presets"].items()),
             f"data: {r['dirs']['data']}  cache: {r['dirs']['cache']}"]
    m = r.get("models")
    if isinstance(m, dict) and "error" in m:
        lines.append(f"models: {m['error']}")
    elif m:
        lines.append(f"models: {len(m)} entries (photo models list)")
    return "\n".join(lines)


# ------------------------------------------------------------------ calibrate
def cmd_calibrate(args):
    from .. import picture_style, presets

    if args.ccmd != "picture-style":
        raise ValueError(args.ccmd)
    pairs = []
    if args.dir:
        pairs += picture_style.find_pairs(args.dir, args.style, args.limit)
    for spec in args.pair or []:
        raw, _, jpg = spec.partition(",")
        if not jpg:
            from ..decode import companion_jpeg
            cj = companion_jpeg(raw)
            if cj is None:
                raise FileNotFoundError(f"no companion JPEG for {raw}")
            jpg = str(cj)
        pairs.append((Path(raw), Path(jpg)))
    if not pairs:
        raise ValueError("no CR3+JPEG pairs: use --dir DIR (raws with companion JPEGs) or --pair RAW[,JPG]")
    r = picture_style.fit_picture_style(pairs, name=args.name)
    out = {"pairs_used": r["pairs_used"], "pairs_given": r["pairs_given"], "residual": r["residual"],
           "matrix": r["matrix"], "preset": r["preset"]["name"]}
    if not args.no_save:
        out["saved"] = str(presets.save_user_preset("picture_style", r["preset"]["name"], r["preset"]))
        out["use"] = f"photo develop IMG.CR3 --art {r['preset']['name']}   # or --set via the stack"
    return out


def register(sub) -> None:
    p = sub.add_parser("presets", help="list|show|validate|save presets (modes, art, export, video, picture_style)")
    ps = p.add_subparsers(dest="pcmd", required=True)
    q = ps.add_parser("list")
    q.add_argument("kind", nargs="?", help="modes | art | export | video | picture-styles (default: all)")
    q = ps.add_parser("show")
    q.add_argument("kind")
    q.add_argument("name")
    q.add_argument("--paths", action="store_true")
    q = ps.add_parser("validate")
    q.add_argument("kind", nargs="?")
    q = ps.add_parser("save", help="save an image's current stack as a user preset")
    q.add_argument("kind", choices=["mode", "art", "modes", "looks"])
    q.add_argument("name")
    q.add_argument("--from", dest="source", required=True, metavar="IMAGE")
    q.add_argument("--version")
    q.add_argument("--description")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("ops", help="list|describe pipeline ops")
    os_ = p.add_subparsers(dest="ocmd", required=True)
    q = os_.add_parser("list")
    q.add_argument("--category")
    q = os_.add_parser("describe")
    q.add_argument("name")
    p.set_defaults(func=cmd_ops)

    p = sub.add_parser("info", help="metadata, detected shoot-type mode and sidecar summary of images")
    add_selection_args(p)
    p.add_argument("--xmp", action="store_true")
    p.add_argument("--features", action="store_true", help="include image content features used for detection")
    p.add_argument("--raw-dump", action="store_true", help="LibRaw golden dump (black/white levels, matrices)")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("status", help="dependency / binary / GPU / model / preset availability report")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("calibrate", help="fit camera renderings from your own CR3+JPEG pairs")
    cs = p.add_subparsers(dest="ccmd", required=True)
    q = cs.add_parser("picture-style", help="fit a tone curve + 3x3 matrix emulating the camera Picture Style")
    q.add_argument("--dir", help="directory with CR3 files that have companion JPEGs")
    q.add_argument("--pair", action="append", metavar="RAW[,JPG]")
    q.add_argument("--style", help="only use pairs shot with this Picture Style (standard, portrait, ...)")
    q.add_argument("--name", default="standard")
    q.add_argument("--limit", type=int, default=120)
    q.add_argument("--no-save", action="store_true")
    p.set_defaults(func=cmd_calibrate)


__all__ = ["register", "MissingDependency"]
