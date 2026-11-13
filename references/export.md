# Export

`photo export` develops each image with its stored edit (the active version, or `--version`), resizes, applies output
sharpening, optional border and watermark, then encodes with an embedded ICC profile and the metadata policy of the
preset. Originals and sidecars are never changed. Files go to `<image folder>/exports/` unless you pass `--out`.

## Contents

- [Quick use](#quick-use)
- [Presets](#presets)
- [Flags](#flags)
- [Naming templates](#naming-templates)
- [Metadata and privacy](#metadata-and-privacy)
- [Colour, proofing and print](#colour-proofing-and-print)
- [Checking the result](#checking-the-result)
- [Very detailed images and JPEG](#very-detailed-images-and-jpeg)

## Quick use

```bash
photo export IMG_0001.CR3 IMG_0002.CR3 --preset web --out /tmp/web
photo export 2026/2026-06-14_wedding/*.CR3 --preset instagram_portrait --out ~/Pictures/ig \
  --name '{date:%Y%m%d}_{seq:03d}_{stem}' --jobs 4
photo export IMG_0001.CR3 --preset print_tiff16 --out /tmp/print                      # 16-bit TIFF, Adobe RGB
photo export IMG_0001.CR3 --format webp --quality 80 --long-edge 1200 --icc srgb --out /tmp/x   # no preset
photo export a.CR3 b.CR3 --preset web --version bw --out /tmp/bw            # a virtual copy
photo export a.CR3 b.CR3 --preset web --contact-sheet /tmp/web/sheet.jpg --columns 4 --title "Wedding picks"
```

`export` takes paths, globs, folders and the library selectors (`id:N`, `@all`, `@album:NAME`, `@tag:NAME`, `@last-import`,
`@session:DATE`, and `--query "rating>=4"`); see [library.md](library.md#selecting-images). `photo album export ALBUM DEST --preset web`
also works for a whole album. The JSON result lists, for every file, `out`,
`format`, `width`, `height`, `bytes`, `icc`, `bits`, `warnings`, and `failed` counts at the top.

## Presets

<!-- BEGIN GENERATED: export-presets -->
| preset | what it produces | options |
|---|---|---|
| `archive_full` | Full size 16-bit TIFF in Display P3, no resizing and no output sharpening, all metadata kept. Use as a rendered master. | format=tiff, bits=16, icc=p3, compression=zlib, metadata=all, name={stem}_archive |
| `email_small` | Long edge 1024, JPEG q80, 4:2:0, sRGB, no metadata (no GPS, no serial numbers). | format=jpeg, quality=80, subsampling=4:2:0, progressive=true, optimize=true, icc=srgb, long_edge=1024, sharpen.target=screen, metadata=none, name={stem}_small |
| `instagram_portrait` | 1080x1350 canvas (4:5), image fitted inside with white padding (use fit cover to crop instead). JPEG q90 sRGB, screen sharpening. | format=jpeg, quality=90, subsampling=4:4:4, progressive=true, icc=srgb, width=1080, height=1350, fit=pad, pad_color=#ffffff, sharpen.target=screen, metadata=copyright, name={stem}_ig45 |
| `instagram_square` | 1080x1080 canvas, image fitted inside with white padding (use fit cover to crop). JPEG q90 sRGB. | format=jpeg, quality=90, subsampling=4:4:4, progressive=true, icc=srgb, width=1080, height=1080, fit=pad, pad_color=#ffffff, sharpen.target=screen, metadata=copyright, name={stem}_ig11 |
| `print_tiff16` | Full resolution 16-bit TIFF in Adobe RGB with the ICC profile embedded, 300 dpi, light print sharpening for matte paper, all metadata kept. | format=tiff, bits=16, icc=adobe, dpi=300, compression=zlib, sharpen.target=print_matte, metadata=all, name={stem}_print |
| `story_9x16` | 1080x1920 canvas (9:16), image fitted inside with black padding. JPEG q90 sRGB. | format=jpeg, quality=90, subsampling=4:4:4, icc=srgb, width=1080, height=1920, fit=pad, pad_color=#000000, sharpen.target=screen, metadata=copyright, name={stem}_story |
| `wallpaper_4k` | 3840x2160 (16:9) centre crop (fit cover), no upscaling beyond source, JPEG q92 sRGB. | format=jpeg, quality=92, subsampling=4:4:4, icc=srgb, width=3840, height=2160, fit=cover, sharpen.target=screen, metadata=none, name={stem}_4k |
| `web` | JPEG q88, 4:4:4, progressive, long edge 2048, screen output sharpening, sRGB ICC. Strips GPS and serial numbers (keeps copyright). | format=jpeg, quality=88, subsampling=4:4:4, progressive=true, optimize=true, icc=srgb, long_edge=2048, sharpen.target=screen, metadata=copyright, name={stem}_web |
<!-- END GENERATED: export-presets -->

Preset options are the same keys the flags set. Show one with `photo presets show export web`. Your own presets go in
`~/.config/photostudio/presets/export/<name>.yaml` (copy a shipped one and edit). Override a single option for one run
with `--set key=value` (comma separated), for example `--set long_edge=1600,quality=85`.

R50 reference: the sensor is 6000 x 4000 (24 MP), so `web` (2048 long edge) is about one third size, and `print_tiff16`
is full size, so a 300 dpi print is about 20 x 13.3 inches.

## Flags

```text
photo export FILES... [--preset P] [--out DIR] [--name TEMPLATE] [--version V] [--jobs N] [--skip-existing]
  encode:    [--format jpeg|png|tiff|webp|avif|heif|jxl] [--quality 1-100] [--bits 8|16] [--dpi N]
  size:      [--long-edge PX] [--fit contain|pad|cover|exact]
  colour:    [--icc srgb|p3|adobe|none] [--gamut-warning] [--proof PAPER.icc]
  metadata:  [--metadata all|copyright|none] [--copyright TEXT] [--artist TEXT]
  finishing: [--watermark TEXT] [--border SIZE] [--border-color #RRGGBB]
  extras:    [--contact-sheet PATH] [--columns N] [--title TEXT] [--set key=value,...]
```

- `--jobs` is the number of worker processes (default up to 4). Slow raster art looks use a lot of memory per worker
  on 24 MP files; use `--jobs 1` or `2` for them on a 16 GB machine.
- `--border SIZE` is a fraction of the long edge, or pixels when 1 or more. It adds a frame around the picture.
- `--watermark TEXT` renders a text watermark (bottom right, 60 percent opacity). Image watermarks and more control
  (position, opacity, size, margin) are options of a preset: `watermark: {image: logo.png, position: bottom-left, opacity: 0.5}`.
- `--skip-existing` skips files whose output already exists, useful for resuming a batch.
- `--fit pad` letterboxes to an exact size with `pad_color`; `cover` crops to fill; `exact` stretches (avoid).
- Formats `avif` and `heif` need the `heif` extra, `jxl` needs the `jxl` extra. Check `photo status`.

## Naming templates

`--name` (or `name:` in a preset) is a Python format string. Tokens: `{stem}`, `{name}` (with extension), `{date}`
(use `{date:%Y%m%d}`), `{time}` (HHMMSS), `{seq}` (use `{seq:03d}`, counts 1, 2, 3 in input order), `{album_pos}`,
`{mode}`, `{art}`, `{preset}`, `{version}`, `{rating}`, `{camera}`, `{lens}`, `{iso}`, `{ext}`. An unknown token is an error that lists
the valid ones. Characters illegal in file names are replaced by `_`.

## Metadata and privacy

- `metadata: all` keeps EXIF (including GPS and serial numbers). `copyright` keeps only copyright and artist.
  `none` strips everything. `web` and the Instagram and story presets use `copyright`; `email_small` and `wallpaper_4k` use `none`, so GPS
  positions and camera serial numbers are not published. Check `photo presets show export NAME` before sending images to anyone.
- `print_tiff16` and `archive_full` keep all metadata. Do not send those files to the web without re-exporting.
- `--copyright "(c) 2026 Name" --artist "Name"` stamp ownership. Take the name from the user, never invent one.

## Colour, proofing and print

- Output profiles (sRGB, Display P3, Adobe RGB) are generated and embedded. Use sRGB for web and social, P3 or Adobe
  RGB for 16-bit masters and print.
- `--proof PAPER.icc` writes a soft-proof JPEG next to each export using the paper or printer ICC profile you supply
  (from your print lab). `--gamut-warning` marks colours outside the output gamut. No ICC profiles for papers are
  shipped; if the file is missing the command stops with `PyCMSError: cannot open profile file`.
- Print sharpening depends on the target (`print_matte`, `print_glossy`); screen output uses a light sharpen.
- Export 16-bit TIFF or PNG for further editing elsewhere; 8-bit JPEG is final delivery.

## Checking the result

After exporting a new look or preset, Read one output JPEG (or the sheet from `--contact-sheet`) and confirm: crop and
orientation, no banding in skies, watermark legible but quiet, file size sensible (`bytes` in the JSON). For a batch,
check the first, middle and last image.

## Very detailed images and JPEG

Progressive, optimised JPEG encoding of extremely detailed pictures (the `halftone_cmyk` look, white noise) used to fail with
`OSError: broken data stream when writing image file`. The writer now enlarges Pillow's encoder buffer for the duration of
the write, and if the encoder still fails it rewrites the file as a baseline JPEG and adds a `warnings` entry
("saved as baseline instead"). No flags are needed. `progressive`, `optimize` and `subsampling` remain ordinary export
options (`--set 'progressive=false'`) if you want a different file, and PNG, WebP and TIFF are always available.
