# Multi-frame stacking

`photo stack KIND FILES... --out RESULT` combines several frames into one image: HDR brackets, focus stacks, astro
sequences, star trails, long-exposure simulation, noise-reduction medians, and panoramas. Frames can be RAW, JPEG or TIFF.
It works on linear light internally and writes the finished image: `.tif` is 16-bit, `.jpg` and `.png` are 8-bit.
`--linear-out result.npy` also saves the float32 linear data.

## Contents

- [Common usage](#common-usage)
- [hdr](#hdr)
- [focus](#focus)
- [astro](#astro)
- [trails](#trails)
- [long and median](#long-and-median)
- [pano](#pano)
- [After stacking](#after-stacking)
- [Limits](#limits)

## Common usage

```bash
photo stack hdr br0.CR3 br1.CR3 br2.CR3 --out hdr.tif
photo stack focus f1.CR3 f2.CR3 f3.CR3 f4.CR3 --out focus.tif --align ecc_affine
photo stack astro 2026/2026-06-14_sky/IMG_*.CR3 --out sky.tif --keep 0.8 --darks dark1.CR3 dark2.CR3
photo stack trails night_*.CR3 --out trails.jpg --method lighten
```

Flags (shared parser; each mode uses its own subset): `--method`, `--align none|mtb|ecc|ecc_affine|ecc_homography|feature`,
`--exposures` (hdr), `--deghost` (hdr), `--kappa`, `--keep`, `--darks/--flats/--bias` (astro), `--freeze-ground [auto|SPEC]`
(astro), `--fade` (trails comet). Defaults are sensible; start with just `--out`.

Group the frames first. Use `photo group` (see [culling.md](culling.md)) to find the burst, and do not cull the
frames of a bracket or a focus series.

## hdr

Merges an exposure bracket (use 3 to 5 frames, shot on a tripod or braced, with a fixed aperture).

- `--method linear` (default): radiance merge with inverse-variance weights; the right choice for RAW. Relative
  exposures come from EXIF shutter, ISO and aperture; override with `--exposures 0.25,1,4`.
- `--method mertens`: exposure fusion, no calibration needed, a flatter "natural HDR" look; works well for JPEG brackets.
- `--method debevec`: calibrated merge for JPEG brackets (needs `--exposures`).
- `--align mtb` (default) corrects small hand-held shifts; `--deghost` suppresses moving objects (clouds, people).
- The result is a rendered image with the default pipeline's tone mapping. Check highlights and shadows by reading it,
  then grade it with `photo develop result.tif --mode landscape`.

## focus

Laplacian-pyramid fusion: the sharpest frame wins at every scale. Aligns with ECC affine to absorb focus breathing.
Shoot a stepped focus series with a fixed aperture; give the frames in order. Check edges of the subject and out-of-focus
transitions for halos and ghosting.

## astro

Star-aligned stack of a night-sky sequence. Alignment uses star triangles (astroalign, the `astro` extra) and falls back to
star-pattern voting then ECC. Options:

- `--method kappa_sigma` (default) | `mean` | `median` | `max`, `--kappa 2.5`.
- `--keep 0.8`: drop the 20 percent of frames with the fewest detected stars (clouds, bad tracking).
- `--darks`, `--flats`, `--bias`: calibration frames (lists of files).
- `--freeze-ground auto`: keep the foreground sharp by aligning only the sky; `auto` uses the AI sky mask (or a heuristic
  without the model), or pass a mask spec JSON.

Needs at least a few frames with visible stars. Typical R50 recipe: 10 to 30 frames of 10 to 15 s at ISO 1600 to
3200, wide lens, f/2.8 to f/4 (the RF-S 18-45 kit lens is slow and gives few stars; say so).

## trails

`--method lighten` blends the maximum of every frame (star trails). `--method comet --fade 0.02` decays old positions so
trails have tails. Frames are not aligned. Use many short frames, so gaps between them are small.

## long and median

- `photo stack long FILES --out x.jpg`: averages frames to simulate a long exposure (smooth water, clouds, remove
  crowds with enough frames). Use a tripod.
- `photo stack median FILES --out x.jpg`: median combine, removes passing objects and reduces noise.
- `--method mean|median|kappa_sigma|max` and `--align` apply to the generic stackers.

## pano

OpenCV stitcher (`--method panorama` default, `--method scans` for flat subjects). **Limits:** it works on
8-bit copies reduced to at most 3000 px on the long side, so the result is a lower-resolution preview-grade panorama. It
needs overlapping frames (30 percent or more) with texture; on feature-poor synthetic or sky-only frames it fails with
`stitching failed: need more images (ERR_NEED_MORE_IMGS)`. For a full-resolution panorama, export 16-bit TIFFs and use Hugin
(`hugin_executor`, `enblend`), if installed (`photo status` shows it); this skill does not drive Hugin.
Panorama was not verified on real overlapping photos.

## After stacking

The saved image is already tone-mapped with the default pipeline. Continue as for any image:

```bash
photo develop hdr.tif --mode landscape --preview 1600       # TIFF is read as an ordinary image
photo versions snapshot hdr.tif "stack_base"
```

Then follow the critique loop in SKILL.md. Move the stacked file into the library (`photo scan`) so it gets a sidecar
and shows in the catalog. Keep the source frames; stacking never touches them.

## Limits

- Memory: median and kappa-sigma combines run in row bands over a temporary file, so 24 MP x 30 frames fits in about 7 GB
  of RAM on the 16 GB machine. Close other heavy programs for large astro stacks.
- Verified on synthetic data: hdr (mertens and linear), focus, trails, long, median, astro (alignment fell back to
  star-pattern voting on synthetic stars). Not verified on real astro or real bracketed R50 frames.
- Hand-held brackets need `--align`, and moving subjects need `--deghost`; the results are not magic.
- Highlights that clip in every frame of an HDR stay clipped.
