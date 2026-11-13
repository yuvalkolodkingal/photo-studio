# Changelog

## 0.1.0 — 2026-11-13

First release.

- rawpy/LibRaw decode for Canon EOS R50 CR3 (and JPEG), fixed-order scene-referred float32 pipeline, 139 ops
- 12 shoot modes (portrait, animal, landscape, night/astro, macro, street, product, sports, architecture, events, video, auto) with Canon EXIF auto-detection
- 88 art modes (pop art, painterly, photographers, cinema grades, 17 film stocks, historic and lo-fi processes)
- Drawn, parametric, luminosity, colour-range and AI masks (with non-AI fallbacks), retouching, HDR/focus/astro/pano stacking, scopes
- Culling (sharpness, clipping, blink, bursts) that only suggests until `--apply`
- Catalog, hierarchical tags, static and smart albums, events, static HTML galleries
- Verified SD-card import, BLAKE3 manifests, bit-rot detection, rclone/restic backup helpers
- Export presets with ICC profiles; video grading via baked LUTs and ffmpeg
- `photo serve`: local Lightroom-style web editor sharing sidecars with the CLI and Claude

Not yet verified on real R50 CR3 files: black/white levels, Picture Style emulation, mode auto-detect, cull thresholds.
