# photo-studio build contract (read fully before writing code)

Several agents build this skill in parallel. Each owns specific files. Do NOT edit files owned by
others or the foundation files (ask via your final report instead; tiny bug fixes in foundation
files are allowed if you describe them in your report). Spec: `/home/claude/reports/Libraw photo
editing skill spec.md`; detailed formulas/palettes/APIs in
`/home/claude/research_notes/Libraw photo editing skill spec/*.md`.

Target: Canon EOS R50 CR3 + JPEG, Fedora Linux, RTX 4050 6 GB / 16 GB RAM, Python >= 3.11, personal use.
Venv: `/home/claude/photo-studio/.venv` (core deps, pytest, fastapi, uvicorn, httpx already
installed; you may `pip install` extras you need, but everything heavy/optional must be lazily
imported with `photostudio.util.lazy_import` and fail with a clear message). No real CR3 exists:
test with `photostudio.testing` (`make_scene`, `write_jpeg`, `make_fake_raw`, `FakeRawPy`).

## Foundation (already written — read it, reuse it)
`photostudio/util.py, color.py, filters.py, decode.py, metadata.py, testing.py,
pipeline/registry.py, pipeline/ops/basic.py`.
- Op = `@op(name, stage, domain, **defaults)` function `fn(img, p, ctx) -> img` on float32 HxWx3.
  `domain='linear'` = scene-referred linear Rec.2020 (unbounded); `'display'` = encoded output
  (0..1 sRGB by default). Stage numbers fix execution order (see `STAGES`); slots A=15 (optical,
  linear), B=18 (looks, display), C=20 (raster art, display). Tone mapper is stage 16.
- Pixel-size params are given at a 2000 px long edge; scale with `ctx.px(value, img)`.
- Ops must be deterministic (use `ctx.seed`), vectorised, and fast enough for a 1600 px preview.

## Shared data formats
Stack entry (JSON/YAML):
`{op: str, params: {..}, enabled: true, mask: <mask spec>|null, blend: "normal", opacity: 1.0, v: 1}`
The same op may appear multiple times (e.g. masked exposure instances); order within one stage
follows stack order.

Mask spec (recursive dict): `{type: linear|radial|polygon|brush|parametric|luminosity|color_range|
ai_subject|ai_background|ai_sky|ai_face|ai_eyes|ai_lips|ai_teeth|ai_skin|ai_animal|depth|combine,
...params, feather: px@2000, invert: false, opacity: 1.0, refine: {guided_radius, guided_eps, blur,
contrast}}`. All coordinates are normalised 0..1 of the (post-geometry) image. `combine` has
`mode: union|intersect|subtract|exclude` and `masks: [spec, ...]`.

Sidecar: `<image filename>.ps.json` next to the image (e.g. `IMG_0001.CR3.ps.json`):
`{schema: 1, source: "IMG_0001.CR3", active: "default", versions: {"default": {mode: str|null,
art: str|null, stack: [entries], created, modified}}, snapshots: [{name, time, version, stack}],
rating: 0-5, flag: "pick"|"reject"|null, label: str|null, tags: ["a|b|c", ...], notes: ""}`.

Preset YAML (in `photostudio/presets/{modes,art,export,video}/<name>.yaml`):
`{name, kind: mode|art|export|video, title, description, tags: [..], stack: [entries],
variants: {name: {stack: [...]}} (optional), options: {...}}`. Merge order when resolving:
pipeline defaults < mode < art < user edits. Merge key = op name (+ optional `id` field on an
entry to allow multiple instances; entries with a mask and no id are appended, not merged).

## Public Python APIs (signatures are binding; owners implement, others call)
- `photostudio.pipeline.engine` (owner: ENGINE)
  - `load_source(path, long_edge=None) -> decode.Decoded` (LRU-cached decode; `long_edge` = preview size)
  - `default_stack(kind) -> list[entry]`
  - `resolve_stack(mode=None, art=None, user=None, kind="raw", meta=None) -> list[entry]`
  - `render(path_or_decoded, stack, long_edge=None, output_space="srgb", upto_stage=None, return_ctx=False) -> np.ndarray float32 display 0..1`
  - `render_file(path, version=None, long_edge=None) -> np.ndarray` (uses the sidecar)
  - `apply_stack_to_array(img_linear, stack, ctx) -> np.ndarray`
- `photostudio.presets` (ENGINE): `list_presets(kind) -> list[dict]`, `load_preset(kind, name) -> dict`
- `photostudio.modes` (ENGINE): `detect_mode(meta: dict, image: np.ndarray|None=None) -> {mode, confidence, reasons}`
- `photostudio.sidecar` (ENGINE): `sidecar_path(img)`, `load(img) -> dict` (creates default doc in memory if absent),
  `save(img, doc)`, `get_stack(doc, version=None)`, `set_stack(doc, stack, version=None)`,
  `new_version(doc, name, from_version=None)`, `snapshot(doc, name)`
- `photostudio.export` (ENGINE): `export_image(path, out_path=None, preset="web", version=None, **overrides) -> Path`,
  `save_array(img_display, out_path, fmt=None, quality=92, icc="srgb", bits=8, metadata_from=None, keep_metadata="all") -> Path`
- `photostudio.masks` (owner: MASKS): `build_mask(spec, img, ctx) -> float32 HxW 0..1`,
  `blend(base, edited, mask, mode="normal", opacity=1.0, domain="linear") -> img`,
  `BLEND_MODES`, `mask_preview(spec, img, ctx) -> uint8 overlay`
- `photostudio.scopes` (MASKS): `histogram(img) -> dict`, `render_histogram/png_waveform/png_vectorscope(img) -> uint8 array`, `clipping_overlay(img)`
- `photostudio.catalog` (owner: LIBRARY): `Catalog(root)` with `scan()`, `images(filter: dict) -> list[dict]`,
  `get(image_id|path)`, `set_rating/flag/label`, `add_tags/remove_tags`, `search(text)`, and albums API;
  DB lives at `<root>/.photostudio/catalog.sqlite` and is a rebuildable cache of sidecars + XMP.
- `photostudio.cull` (LIBRARY): `analyze(path) -> dict` (sharpness, clipping, blink, etc.), `cull(paths, target=None) -> list[dict]`

## CLI plugin pattern
`photostudio/cli.py` (ENGINE) builds an argparse parser and auto-discovers every module in the
package `photostudio/commands/` (pkgutil), calling its `register(subparsers)`.
Each command function receives `args` and returns a JSON-able object; cli.py prints it (pretty
text by default, JSON with the global `--json` flag) and maps exceptions to exit code 1 with
`{"error": ...}`. Each agent adds its own `photostudio/commands/<area>.py` — never edit another
agent's command module. `photostudio/commands/__init__.py` is ENGINE's (empty).

## Tests
Each agent writes `tests/test_<area>*.py` (pytest, synthetic data only, fast: whole file < 60 s)
and runs ONLY its own test files until green (`.venv/bin/python -m pytest tests/test_<area>*.py -q`).
Cross-module imports that don't exist yet must be guarded with `pytest.importorskip`.
`tests/conftest.py` is ENGINE's.

## Quality bar
Real algorithms from the research notes, not placeholders. If something is genuinely optional
(needs model weights/GPU), implement the integration with lazy import plus a sensible non-AI
fallback where possible, and list it as "optional" in your final report. Keep modules cohesive
(< ~900 lines each; split if larger). No network access at runtime except explicit
`photo models download`. Never write into CR3 files. Do not git commit or push.
Final report (short): files written, what's real vs optional/stubbed, test results, open issues
for other owners.
