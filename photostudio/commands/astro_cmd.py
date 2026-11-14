"""`photo astro stack | finish | gui`: Sequator-style night-sky stacking from the command line and a browser GUI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


# --------------------------------------------------------------------------- linear I/O
def save_linear(img: np.ndarray, path: str) -> str:
    """float32 linear result: .tif/.tiff (32-bit float TIFF), .npy, or .fit/.fits (needs astropy)."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower()
    if ext == ".npy":
        np.save(p, img.astype(np.float32))
    elif ext in (".fit", ".fits", ".fts"):
        from astropy.io import fits

        fits.writeto(p, np.moveaxis(img[::-1], -1, 0).astype(np.float32), overwrite=True)   # FITS: bottom-up, CHW
    else:
        import tifffile

        tifffile.imwrite(p, img.astype(np.float32), photometric="rgb", compression="zlib")
    return str(p)


def load_linear(path: str) -> np.ndarray:
    p = Path(path).expanduser()
    ext = p.suffix.lower()
    if ext == ".npy":
        return np.load(p).astype(np.float32)
    if ext in (".fit", ".fits", ".fts"):
        from ..astro.engine import load_any

        return load_any(p)[0]
    import tifffile

    a = tifffile.imread(p)
    if a.dtype == np.uint16:
        a = a.astype(np.float32) / 65535
    elif a.dtype == np.uint8:
        a = a.astype(np.float32) / 255
    return a.astype(np.float32)


def _save_display(disp: np.ndarray, out: str) -> str:
    from .masks_cmd import _save

    bits = 16 if Path(out).suffix.lower() in (".tif", ".tiff", ".png") else 8
    return _save(disp, out, bits=bits, quality=95)


def _progress(args):
    if args.quiet or args.json:
        return None
    last = {"stage": None, "pct": -1}

    def p(stage, frac, msg=""):
        pct = int(frac * 100)
        new = stage != last["stage"] or msg.split(":")[0] != last.get("label")
        if new or pct >= last["pct"] + 5 or stage == "done":
            print(f"[{stage:>11}] {pct:3d}%  {msg}", file=sys.stderr, flush=True)
            last.update(stage=stage, pct=pct, label=msg.split(":")[0])
    return p


def finish_options(args):
    from ..astro.post import FinishOptions

    return FinishOptions(light_pollution=not args.no_light_pollution, colour=not args.no_colour, stretch=args.stretch,
                         black=args.black, hdr=args.hdr, enhance_stars=args.enhance_stars, denoise=args.denoise,
                         saturation=args.saturation, target_bg=args.brightness, to_srgb=not args.camera_rgb)


# --------------------------------------------------------------------------- commands
def cmd_stack(args):
    from ..astro import engine, post
    from .masks_cmd import _expand, _json_arg

    files = _expand(args.files, args)
    fg = None
    if args.freeze_ground:
        fg = "auto" if args.freeze_ground == "auto" else _json_arg(args.freeze_ground)
    opts = engine.StackOptions(
        align=args.align, reject=args.reject, kappa=args.kappa, keep=args.keep, group=args.group,
        reference=(args.reference - 1) if args.reference else None, lens=args.lens,
        darks=_expand(args.darks, args) if args.darks else (), flats=_expand(args.flats, args) if args.flats else (),
        bias=_expand(args.bias, args) if args.bias else (), freeze_ground=fg, horizon=args.horizon,
        halves=not args.no_halves, nudge_px=args.nudge_px, crop=not args.no_crop, jobs=args.jobs, mem_gb=args.mem_gb,
        cache=args.cache, workdir=args.workdir, draft=args.draft, homography=not args.no_homography,
        weights=args.weights, min_cover=args.min_cover)
    res = engine.stack(files, opts, progress=_progress(args))
    out = args.out or str(Path(files[0]).with_name(f"{Path(files[0]).stem}_astro.tif"))
    ret = {"out": None, "frames": len(files)}
    stem = str(Path(out).with_suffix(""))
    if args.linear_out or not args.no_linear:
        ret["linear"] = save_linear(res.image, args.linear_out or stem + "_linear.tif")
        if res.half_a is not None and args.save_halves:
            ret["half_a"] = save_linear(res.half_a, stem + "_halfA.tif")
            ret["half_b"] = save_linear(res.half_b, stem + "_halfB.tif")
    sky = None
    if opts.horizon or fg is not None:
        sky = None   # finishing works on the cropped frame; the gradient model is robust to the ground already
    disp, finfo = post.finish(res.image, finish_options(args), sky)
    ret["out"] = _save_display(disp, out)
    if args.validate and res.half_a is not None:
        v = post.validate_sources(res.image, res.half_a, res.half_b)
        ret["validation"] = {k: v[k] for k in ("candidates", "confirmed", "half_test_false_alarm_rate")}
        res.info["validated_sources"] = v["sources"]
    ret.update({k: res.info[k] for k in ("frames_used", "frames_total", "registration", "reference", "nudges_before",
                                          "total_exposure_s", "seconds", "notes")})
    ret["reference"] += 1
    ret["nudges_before"] = [i + 1 for i in ret["nudges_before"]]
    ret["dropped"] = len(res.info["dropped"])
    ret["finish"] = finfo
    report = args.report or stem + "_report.json"
    Path(report).write_text(json.dumps({**res.info, "finish": finfo, "outputs": ret}, indent=1, default=float))
    ret["report"] = report
    return ret


def cmd_finish(args):
    from ..astro import post

    img = load_linear(args.linear)
    disp, info = post.finish(img, finish_options(args))
    out = args.out or str(Path(args.linear).with_name(Path(args.linear).stem + "_final.tif"))
    res = {"out": _save_display(disp, out), "finish": info}
    if args.half_a and args.half_b:
        v = post.validate_sources(img, load_linear(args.half_a), load_linear(args.half_b))
        res["validation"] = {k: v[k] for k in ("candidates", "confirmed", "half_test_false_alarm_rate")}
    return res


def cmd_gui(args):
    from ..astro import gui

    gui.serve(port=args.port, open_browser=args.open, workers=args.jobs)
    return None


# --------------------------------------------------------------------------- registration
def _finish_args(p):
    g = p.add_argument_group("finishing")
    g.add_argument("--no-light-pollution", action="store_true", help="skip sky-glow / gradient removal")
    g.add_argument("--no-colour", action="store_true", help="skip star colour calibration")
    g.add_argument("--stretch", type=float, default=0.0, help="asinh strength (0 = auto brightness, 100 gentle .. 3000 strong)")
    g.add_argument("--brightness", type=float, default=0.10, help="auto brightness: target sky level 0..1 (default 0.10)")
    g.add_argument("--black", type=float, default=0.0, help="black point in sky sigmas (0 natural, 1.5+ inky)")
    g.add_argument("--hdr", action="store_true", help="protect bright star cores and highlights")
    g.add_argument("--enhance-stars", type=float, default=0.3, help="0..1 (default 0.3)")
    g.add_argument("--denoise", type=float, default=0.5, help="star-protected noise reduction 0..1 (default 0.5)")
    g.add_argument("--saturation", type=float, default=1.25)
    g.add_argument("--camera-rgb", action="store_true", help="input is camera RGB (e.g. Siril FITS), not linear Rec.2020")


def register(subparsers) -> None:
    from ..cli import add_selection_args

    p = subparsers.add_parser("astro", help="night-sky stacking (Sequator-style): stack | finish | gui")
    sp = p.add_subparsers(dest="astro_cmd", required=True)

    q = sp.add_parser("stack", help="register + integrate night-sky frames, then finish (light pollution, colour, stretch)")
    add_selection_args(q, files_help="light frames: files, globs, folders or selectors")
    q.add_argument("--out", help="finished image (.tif = 16-bit, .jpg); default <first>_astro.tif")
    q.add_argument("--linear-out", help="linear float32 result (.tif, .fits, .npy); default <out>_linear.tif")
    q.add_argument("--no-linear", action="store_true", help="do not write the linear result")
    q.add_argument("--save-halves", action="store_true", help="also write the two independent half stacks")
    q.add_argument("--report", help="JSON report path (default <out>_report.json)")
    g = q.add_argument_group("calibration")
    g.add_argument("--darks", nargs="+", help="dark frames (Sequator 'noise images')")
    g.add_argument("--flats", nargs="+")
    g.add_argument("--bias", nargs="+")
    g.add_argument("--lens", action="store_true", help="lensfun distortion + vignetting correction ('reduce distortion')")
    g = q.add_argument_group("registration")
    g.add_argument("--align", default="auto", choices=["auto", "stars", "ecc", "scene", "none"],
                   help="auto picks stars (rich fields) or ecc mini-stacks (city skies with few stars); scene aligns on the "
                        "landscape (city / foreground noise stacks, works across zoom and reframing)")
    g.add_argument("--reference", type=int, help="base frame number (1-based); default: middle of the longest steady run")
    g.add_argument("--group", type=int, default=8, help="ecc: frames per mini-stack (default 8)")
    g.add_argument("--nudge-px", type=float, default=8.0, help="minimum jump (px) counted as a camera nudge; the real threshold "
                   "adapts to the sequence jitter (default 8)")
    g.add_argument("--no-homography", action="store_true", help="similarity/euclidean only")
    g.add_argument("--freeze-ground", nargs="?", const="auto",
                   help="keep the foreground sharp: 'auto' (AI sky mask) or a mask spec JSON")
    g.add_argument("--horizon", type=float, help="bottom fraction of the frame that is ground (simple freeze ground)")
    g = q.add_argument_group("integration")
    g.add_argument("--reject", default="sigma", choices=["sigma", "winsor", "mean", "max"],
                   help="sigma (kappa-sigma clip), winsor (winsorized), mean, max (lighten / trails)")
    g.add_argument("--kappa", type=float, default=2.5)
    g.add_argument("--weights", default="noise", choices=["noise", "equal"])
    g.add_argument("--keep", type=float, default=1.0, help="fraction of steady frames to keep (best first)")
    g.add_argument("--no-halves", action="store_true", help="skip the split-half stacks (saves RAM)")
    g.add_argument("--validate", action="store_true", help="count faint sources confirmed in both half stacks")
    g.add_argument("--no-crop", action="store_true")
    g.add_argument("--min-cover", type=float, default=0.9, help="crop to where this fraction of frames contributes")
    g = q.add_argument_group("performance")
    g.add_argument("--jobs", type=int, default=0, help="worker processes (0 = cores-2, limited by --mem-gb)")
    g.add_argument("--mem-gb", type=float, default=0.0, help="RAM budget (default 60%% of available)")
    g.add_argument("--cache", default="auto", choices=["auto", "on", "off"], help="float16 warped-frame disk cache")
    g.add_argument("--workdir", help="scratch directory (needs ~N x 150 MB with the cache)")
    g.add_argument("--draft", action="store_true", help="half-size raw decode: fast preview stack")
    _finish_args(q)
    q.set_defaults(func=cmd_stack)

    q = sp.add_parser("finish", help="finish a linear stack (light pollution, colour, stretch, stars, noise)")
    q.add_argument("linear", help="linear float image (.tif, .fits, .npy)")
    q.add_argument("--out")
    q.add_argument("--half-a", help="half stack A (for split-half validation)")
    q.add_argument("--half-b")
    _finish_args(q)
    q.set_defaults(func=cmd_finish)

    q = sp.add_parser("gui", help="Sequator-style stacking GUI in the browser (localhost only)")
    q.add_argument("--port", type=int, default=8766)
    q.add_argument("--open", action="store_true", help="open the browser")
    q.add_argument("--jobs", type=int, default=0)
    q.set_defaults(func=cmd_gui)
