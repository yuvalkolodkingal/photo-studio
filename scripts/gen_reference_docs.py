#!/usr/bin/env python3
"""Regenerate the catalogue blocks inside references/*.md from the live op registry, presets and model registry.

The reference files are hand-written Markdown with generated regions delimited by

    <!-- BEGIN GENERATED: name -->
    ...
    <!-- END GENERATED: name -->

This script only rewrites those regions, so prose and tables never drift apart: after adding an op, preset or model
run it again and commit the diff.

    python scripts/gen_reference_docs.py            # rewrite the generated blocks
    python scripts/gen_reference_docs.py --check    # exit 1 if any block is out of date (CI / pre-commit)
    python scripts/gen_reference_docs.py --measure  # also re-time every art preset on a 1600 px synthetic JPEG
                                                    #   (about 2 minutes) and store references/.art_timings.json

Run it with the skill's venv: ${CLAUDE_SKILL_DIR}/.venv/bin/python scripts/gen_reference_docs.py
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFS = ROOT / "references"
TIMINGS = REFS / ".art_timings.json"

CATEGORY_ORDER = [
    ("pop", "Pop art and graphic prints"),
    ("painterly", "Painting, drawing and engraving"),
    ("photographer", "Photographer-inspired looks"),
    ("cinema", "Cinema grades"),
    ("film", "Film stock emulations"),
    ("process", "Historic and alternative processes, false colour"),
    ("other", "Lo-fi, optical and finishing effects"),
]


# ---------------------------------------------------------------------------------------------- helpers
def esc(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def short(v, n: int = 70) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 1] + "…"


def collapse(doc: str) -> str:
    return re.sub(r"\s+", " ", doc or "").strip()


def stack_summary(stack: list[dict], limit: int = 10) -> str:
    parts = []
    for e in stack[:limit]:
        p = e.get("params") or {}
        pp = ", ".join(f"{k}={short(v, 28)}" for k, v in list(p.items())[:5])
        name = e["op"] + (f"@{e['id']}" if e.get("id") else "")
        mask = e.get("mask")
        if mask:
            name += f" [mask {mask.get('type')}]"
        parts.append(f"`{name}`" + (f" ({pp})" if pp else ""))
    if len(stack) > limit:
        parts.append(f"… +{len(stack) - limit} more")
    return "; ".join(parts)


# ---------------------------------------------------------------------------------------------- blocks
def block_stages() -> str:
    from photostudio.pipeline.registry import STAGES, all_ops

    ops = all_ops()
    by_stage: dict[int, list[str]] = {}
    for s in ops.values():
        by_stage.setdefault(s.stage, []).append(s.name)
    notes = {
        1: "decode: black/white per CFA channel, hot pixels", 2: "decode: as-shot or daylight white balance",
        3: "decode: highlight reconstruction", 4: "decode: demosaic (settings live in the `raw` op)",
        9: "decode: camera matrix to linear Rec.2020 (not an op)",
        15: "SLOT A: optical looks in linear light", 16: "the single tone mapper",
        17: "gamut map + output OETF (implicit, not an op)", 18: "SLOT B: display-referred looks and colour",
        19: "effects: grain, glow, vignette ...", 20: "SLOT C: raster art (slow, spatial)", 21: "output: resize, sharpen",
    }
    rows = ["| # | stage | domain | note | ops |", "|---|---|---|---|---|"]
    for n, name in STAGES.items():
        dom = "mosaic" if n <= 4 else ("linear" if n <= 16 else "display")
        if n in (7, 11):
            dom = "linear"
        names = ", ".join(f"`{o}`" for o in sorted(by_stage.get(n, []))) or "-"
        rows.append(f"| {n} | {name} | {dom} | {esc(notes.get(n, ''))} | {names} |")
    return "\n".join(rows)


def block_ops() -> str:
    from photostudio.pipeline.registry import STAGES, all_ops
    from photostudio.pipeline.engine import is_spatial

    ops = all_ops()
    out: list[str] = []
    for stage in sorted({s.stage for s in ops.values()}):
        specs = sorted((s for s in ops.values() if s.stage == stage), key=lambda s: (s.category, s.name))
        dom = {s.domain for s in specs}
        out.append(f"#### Stage {stage}: {STAGES[stage]} ({'/'.join(sorted(dom))})\n")
        out.append("| op | params = default (choices) | what it does |")
        out.append("|---|---|---|")
        for s in specs:
            plist = []
            for k, v in s.defaults.items():
                ch = s.choices.get(k)
                plist.append(f"`{k}`={esc(short(v, 40))}" + (f" ({esc('/'.join(map(str, ch)))})" if ch else ""))
            flags = []
            if s.raw_only:
                flags.append("raw only")
            if s.changes_geometry:
                flags.append("changes geometry")
            try:
                if is_spatial(s):
                    flags.append("spatial")
            except Exception:  # noqa: BLE001
                pass
            doc = collapse(s.fn.__doc__ or s.doc)
            head = f"`{s.name}`" + (f"<br><sub>{esc(', '.join(flags))}</sub>" if flags else "")
            out.append(f"| {head} | {', '.join(plist) or '-'} | {esc(doc)} |")
        out.append("")
    return "\n".join(out).rstrip()


def _category(tags: list[str]) -> str:
    for key, _ in CATEGORY_ORDER[:-1]:
        if key in tags:
            return key
    return "other"


def block_art() -> str:
    from photostudio import presets
    from photostudio.pipeline.registry import all_ops

    ops = all_ops()
    timings = json.loads(TIMINGS.read_text()) if TIMINGS.exists() else {}
    cats: dict[str, list[dict]] = {k: [] for k, _ in CATEGORY_ORDER}
    for p in presets.list_presets("art"):
        if "error" in p:
            continue
        d = presets.load_preset("art", p["name"])
        cats[_category(d.get("tags", []))].append(d)
    out = [f"{sum(len(v) for v in cats.values())} art presets. Time = measured seconds for one render of a 1600 px "
           "JPEG on the build machine's CPU (`--measure`; relative speed matters more than the number). "
           "`slow` = tagged slow by the preset author.\n"]
    for key, title in CATEGORY_ORDER:
        rows = cats[key]
        if not rows:
            continue
        out.append(f"#### {title}\n")
        out.append("| preset | variants (`--art name:variant`) | stack | time | notes |")
        out.append("|---|---|---|---|---|")
        for d in sorted(rows, key=lambda x: x["name"]):
            variants = ", ".join(f"`{v}`" for v in sorted(d["variants"])) or "-"
            t = timings.get(d["name"])
            tcol = f"{t:.1f} s" if t is not None else "n/a"
            if "slow" in d.get("tags", []):
                tcol += " (slow)"
            slot = (d.get("options") or {}).get("slot")
            note = esc((d.get("options") or {}).get("note", ""))
            desc = esc(d["description"])
            if note and note in desc:
                note = ""
            cell = f"**{esc(d['title'])}**. {desc}" + (f" _{note}_" if note else "")
            stack = stack_summary(d["stack"], 4) + (f" [slot {slot}]" if slot else "")
            out.append(f"| `{d['name']}` | {variants} | {esc(stack)} | {tcol} | {cell} |")
        out.append("")
    del ops
    return "\n".join(out).rstrip()


def block_modes() -> str:
    from photostudio import presets

    out: list[str] = []
    for p in presets.list_presets("mode"):
        if "error" in p:
            continue
        d = presets.load_preset("mode", p["name"])
        out.append(f"#### `{d['name']}` - {d['title']}\n")
        out.append(collapse(d["description"]) + "\n")
        out.append(f"Base stack ({len(d['stack'])} entries): {stack_summary(d['stack'], 14)}\n")
        for vn in sorted(d["variants"]):
            v = d["variants"][vn]
            out.append(f"- variant `{d['name']}:{vn}`: {stack_summary(v['stack'], 10)}")
        out.append("")
    return "\n".join(out).rstrip()


def _flat(opts: dict, prefix: str = "") -> list[str]:
    rows = []
    for k, v in opts.items():
        if isinstance(v, dict):
            rows += _flat(v, f"{prefix}{k}.")
        else:
            rows.append(f"{prefix}{k}={short(v, 30)}")
    return rows


def block_export() -> str:
    from photostudio import presets

    out = ["| preset | what it produces | options |", "|---|---|---|"]
    for p in presets.list_presets("export"):
        d = presets.load_preset("export", p["name"])
        out.append(f"| `{d['name']}` | {esc(collapse(d['description']))} | {esc(', '.join(_flat(d['options'])))} |")
    return "\n".join(out)


def block_video() -> str:
    from photostudio import presets

    out = ["| preset | what it produces | variants |", "|---|---|---|"]
    for p in presets.list_presets("video"):
        d = presets.load_preset("video", p["name"])
        v = ", ".join(f"`{x}`" for x in sorted(d["variants"])) or "-"
        out.append(f"| `{d['name']}` | **{esc(d['title'])}**. {esc(collapse(d['description']))} | {v} |")
    return "\n".join(out)


def block_picture_styles() -> str:
    from photostudio import presets

    out = ["| preset | variants | notes |", "|---|---|---|"]
    for p in presets.list_presets("picture_style"):
        d = presets.load_preset("picture_style", p["name"])
        v = ", ".join(f"`{x}`" for x in sorted(d["variants"])) or "-"
        out.append(f"| `{d['name']}` | {v} | {esc(collapse(d['description'])[:260])} |")
    return "\n".join(out)


def block_models() -> str:
    from photostudio import models

    out = ["| name | used by | licence | size (MB) | needs python modules |", "|---|---|---|---|---|"]
    for n in sorted(models.REGISTRY):
        m = models.REGISTRY[n]
        bundled = "" if m.redistributable else " (do not redistribute)"
        out.append(f"| `{n}` | {esc(m.purpose)} | {esc(m.licence)}{bundled} | {m.size_mb:g} | "
                   f"{', '.join(m.requires) or '-'} |")
    return "\n".join(out)


def block_extras() -> str:
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        import tomli as tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    proj = data["project"]
    out = ["| extra | packages | notes |", "|---|---|---|"]
    notes = {
        "meta": "PyExifTool wrapper; needs the exiftool binary (`perl-Image-ExifTool`)",
        "lens": "lensfunpy; the skill ships a patched lensfun DB with the R50 and RF-S kit lenses",
        "video": "PyAV + PySceneDetect (shot detection, frame access); ffmpeg binary still required",
        "heif": "HEIF/AVIF export", "jxl": "JPEG XL export", "astro": "astroalign for star registration",
        "geo": "offline reverse geocoding for places tags", "fast": "numba (fast LUTs) + xxhash",
        "denoise": "BM3D: non-commercial licence, personal use only",
        "ai": "onnxruntime, rembg, mediapipe, open_clip, transformers; model weights separate (`photo models download`)",
        "yolo": "Ultralytics YOLO: AGPL-3.0, personal use OK, do not redistribute combined works",
        "faces": "InsightFace; weights are non-commercial research only (manual download)",
        "serve": "fastapi + uvicorn for the browser editor (`photo serve`); not part of `all`, the installer adds it by default",
        "dev": "pytest, ruff", "all": "meta, lens, video, heif, astro, geo, fast (does NOT include serve, jxl, denoise, ai, yolo, faces)",
    }
    for name, pk in proj.get("optional-dependencies", {}).items():
        out.append(f"| `{name}` | {esc(', '.join(pk))} | {esc(notes.get(name, ''))} |")
    return "\n".join(out)


def block_blend() -> str:
    from photostudio.masks import BLEND_MODES, MASK_TYPES

    out = ["Mask types: " + ", ".join(f"`{t}`" for t in MASK_TYPES) + ".\n",
           "| blend mode | formula / note |", "|---|---|"]
    for k, v in BLEND_MODES.items():
        out.append(f"| `{k}` | {esc(v)} |")
    return "\n".join(out)


BLOCKS = {
    "stages": block_stages, "ops": block_ops, "art-catalogue": block_art, "mode-catalogue": block_modes,
    "export-presets": block_export, "video-presets": block_video, "picture-styles": block_picture_styles,
    "models-table": block_models, "extras-table": block_extras, "blend-modes": block_blend,
}
PATTERN = re.compile(r"(<!-- BEGIN GENERATED: (?P<name>[\w-]+) -->\n)(?P<body>.*?)(\n?<!-- END GENERATED: (?P=name) -->)", re.S)


def measure() -> dict:
    import numpy as np

    from photostudio import presets
    from photostudio.pipeline import engine
    from photostudio.testing import make_scene, write_jpeg

    with tempfile.TemporaryDirectory() as td:
        src = write_jpeg(Path(td) / "t.jpg", make_scene(1600, 1067, 3))
        engine.render(src, engine.resolve_stack(None, None, None, "jpeg"))
        out = {}
        for p in presets.list_presets("art"):
            if "error" in p:
                continue
            stack = engine.resolve_stack(None, p["name"], None, "jpeg")
            t = []
            for _ in range(2):
                t0 = time.time()
                np.asarray(engine.render(src, stack))
                t.append(time.time() - t0)
            out[p["name"]] = round(statistics.median(t) if len(t) > 2 else min(t), 2)
            print(f"{p['name']:28s} {out[p['name']]:6.2f}s", file=sys.stderr)
    TIMINGS.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 if a generated block is stale")
    ap.add_argument("--measure", action="store_true", help="re-time all art presets first")
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    if args.measure:
        measure()
    stale, seen = [], set()
    for md in sorted(REFS.glob("*.md")):
        text = md.read_text(encoding="utf-8")

        def sub(m):
            name = m.group("name")
            if name not in BLOCKS:
                raise SystemExit(f"{md.name}: unknown generated block '{name}' (known: {', '.join(BLOCKS)})")
            seen.add(name)
            body = BLOCKS[name]()
            return m.group(1) + body + "\n<!-- END GENERATED: " + name + " -->"

        new = PATTERN.sub(sub, text)
        if new != text:
            stale.append(md.name)
            if not args.check:
                md.write_text(new, encoding="utf-8")
    missing = sorted(set(BLOCKS) - seen)
    if missing:
        print(f"warning: no file contains generated block(s): {', '.join(missing)}", file=sys.stderr)
    if args.check:
        if stale:
            print("stale generated blocks in: " + ", ".join(stale) + " (run scripts/gen_reference_docs.py)")
            return 1
        print("references are up to date")
        return 0
    print("updated: " + (", ".join(stale) if stale else "nothing (already current)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
