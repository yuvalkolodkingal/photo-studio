#!/usr/bin/env python
"""Render every art preset on the synthetic test scene (and optionally a user image) into one labelled PNG.

    .venv/bin/python scripts/make_art_contact_sheet.py [--image PATH] [--out docs/art_modes_contact_sheet.png]
        [--cell 320] [--cols 6] [--variants] [--only name,name] [--fallback]

Rendering prefers ``photostudio.pipeline.engine.apply_stack_to_array`` / ``render`` (the real stage order, tone map and output
transform); if the engine cannot be imported or fails for a preset, a minimal internal runner (`run_stack_fallback`) is used
(stage order, a soft-shoulder tone map and the sRGB OETF). `run_stack_fallback` is importable by tests."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from photostudio import color, testing  # noqa: E402

TONEMAP_STAGE = 16


def _registry():
    from photostudio.pipeline import registry
    registry._ensure_loaded()
    return registry


def run_stack_fallback(display: np.ndarray, stack: list[dict], seed: int = 0) -> np.ndarray:
    """Minimal renderer used when the engine is unavailable: linearise (sRGB -> Rec.2020), run linear ops (stage < 16),
    soft-shoulder tone map, OETF back to display, then display ops in (stage, stack order)."""
    from photostudio.pipeline.registry import Ctx
    reg = _registry().REGISTRY
    ctx = Ctx(domain="linear", kind="image", seed=seed)
    steps = []
    for i, e in enumerate(stack):
        if not e.get("enabled", True):
            continue
        spec = reg.get(e["op"])
        if spec is None:
            continue
        steps.append((spec.stage, i, spec, spec.resolve_params(e.get("params"), e.get("v"))))
    steps.sort(key=lambda s: (s[0], s[1]))
    lin = color.apply_matrix(color.srgb_eotf(np.clip(display, 0, 1)), color.SRGB_TO_REC2020).astype(np.float32)
    img = lin
    in_linear = True
    ctx.domain = "linear"
    for stage, _, spec, params in steps:
        if in_linear and stage >= TONEMAP_STAGE:
            from photostudio.pipeline.ops.basic import soft_shoulder
            rgb = color.rec2020_to_output(np.maximum(img, 0), "srgb")
            img = color.srgb_oetf(np.clip(soft_shoulder(np.clip(rgb, 0, None)), 0, 1)).astype(np.float32)
            in_linear = False
            ctx.domain = "display"
        if not in_linear and spec.domain == "linear":
            continue
        img = spec.fn(np.array(img, np.float32, copy=True), params, ctx)
    if in_linear:
        from photostudio.pipeline.ops.basic import soft_shoulder
        rgb = color.rec2020_to_output(np.maximum(img, 0), "srgb")
        img = color.srgb_oetf(np.clip(soft_shoulder(np.clip(rgb, 0, None)), 0, 1))
    return np.clip(img, 0, 1).astype(np.float32)


def render_stack(display: np.ndarray, stack: list[dict], seed: int = 0, prefer_engine: bool = True) -> tuple[np.ndarray, str]:
    """Render ``stack`` on a display-referred image. Returns (image, backend) with backend 'engine' or 'fallback'."""
    if prefer_engine:
        try:
            from photostudio.pipeline import engine
            from photostudio.pipeline.registry import Ctx
            lin = color.apply_matrix(color.srgb_eotf(np.clip(display, 0, 1)), color.SRGB_TO_REC2020).astype(np.float32)
            ctx = Ctx(kind="image", seed=seed)
            out = engine.apply_stack_to_array(lin, stack, ctx)
            return np.clip(out, 0, 1).astype(np.float32), "engine"
        except Exception as exc:  # noqa: BLE001 - engine missing / incomplete: fall through
            print(f"  (engine unavailable: {type(exc).__name__}: {exc}; using fallback)", file=sys.stderr)
    return run_stack_fallback(display, stack, seed), "fallback"


def collect_presets(include_variants: bool = False, only: set[str] | None = None) -> list[tuple[str, dict]]:
    """[(label, preset dict with merged stack)] for every art preset (variants optional)."""
    import yaml
    out = []
    for f in sorted((ROOT / "photostudio" / "presets" / "art").glob("*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8"))
        if only and d["name"] not in only:
            continue
        out.append((d["name"], d))
        if include_variants:
            for vn, v in (d.get("variants") or {}).items():
                merged = {op["op"]: dict(op) for op in d["stack"]}
                order = [op["op"] for op in d["stack"]]
                for e in v.get("stack", []):
                    if e["op"] in merged:
                        m = dict(merged[e["op"]])
                        m["params"] = {**(m.get("params") or {}), **(e.get("params") or {})}
                        merged[e["op"]] = m
                    else:
                        merged[e["op"]] = dict(e)
                        order.append(e["op"])
                out.append((f"{d['name']}:{vn}", {**d, "stack": [merged[o] for o in order]}))
    return out


def _fit(img: np.ndarray, cell: int, ch: int) -> np.ndarray:
    h, w = img.shape[:2]
    s = min(cell / w, ch / h)
    return cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)


def build_sheet(items: list[tuple[str, np.ndarray]], cell: int, cols: int) -> np.ndarray:
    from PIL import Image, ImageDraw, ImageFont
    pad, lab = 8, 22
    rows = (len(items) + cols - 1) // cols
    W, H = cols * (cell + pad) + pad, rows * (cell * 2 // 3 + lab + pad) + pad
    sheet = Image.new("RGB", (W, H), (24, 24, 26))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 12)
    except OSError:
        font = ImageFont.load_default()
    ch = cell * 2 // 3
    for i, (label, img) in enumerate(items):
        r, c = divmod(i, cols)
        x, y = pad + c * (cell + pad), pad + r * (ch + lab + pad)
        t = Image.fromarray((np.clip(_fit(img, cell, ch), 0, 1) * 255 + 0.5).astype(np.uint8))
        sheet.paste(t, (x + (cell - t.width) // 2, y + (ch - t.height) // 2))
        draw.text((x + 2, y + ch + 4), label, fill=(225, 225, 225), font=font)
    return np.asarray(sheet)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--image", help="optional extra image (jpg/png); rendered as a second sheet <out>_image.png")
    ap.add_argument("--out", default=str(ROOT / "docs" / "art_modes_contact_sheet.png"))
    ap.add_argument("--cell", type=int, default=320)
    ap.add_argument("--cols", type=int, default=6)
    ap.add_argument("--variants", action="store_true", help="also render every variant")
    ap.add_argument("--only", default="", help="comma separated preset names")
    ap.add_argument("--fallback", action="store_true", help="force the internal fallback runner")
    ap.add_argument("--scene-width", type=int, default=960)
    a = ap.parse_args(argv)

    only = {s for s in a.only.split(",") if s} or None
    presets = collect_presets(a.variants, only)
    scene = testing.make_scene(a.scene_width, a.scene_width * 2 // 3)
    sources = [("scene", scene)]
    if a.image:
        im = cv2.imread(a.image, cv2.IMREAD_COLOR)
        if im is None:
            print(f"cannot read {a.image}", file=sys.stderr)
            return 2
        im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        s = a.scene_width / max(im.shape[:2])
        sources.append(("image", cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else im))
    outp = Path(a.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    for sname, src in sources:
        items = [("original", src)]
        t_all = time.time()
        for label, d in presets:
            t0 = time.time()
            try:
                img, backend = render_stack(src, d["stack"], seed=0, prefer_engine=not a.fallback)
            except Exception as exc:  # noqa: BLE001
                print(f"FAILED {label}: {type(exc).__name__}: {exc}", file=sys.stderr)
                img = np.zeros_like(src)
                backend = "error"
            print(f"{label:34s} {backend:8s} {time.time() - t0:5.2f}s", flush=True)
            items.append((label, img))
        sheet = build_sheet(items, a.cell, a.cols)
        path = outp if sname == "scene" else outp.with_name(outp.stem + "_image" + outp.suffix)
        Image.fromarray(sheet).save(path)
        print(f"wrote {path} ({len(items) - 1} presets, {time.time() - t_all:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
