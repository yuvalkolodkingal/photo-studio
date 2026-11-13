# Troubleshooting

## Contents

- [First checks](#first-checks)
- [Command-line quirks](#command-line-quirks)
- [Editing and rendering](#editing-and-rendering)
- [Output and export](#output-and-export)
- [Library, import and backup](#library-import-and-backup)
- [Video](#video)
- [Web editor](#web-editor)
- [Missing tools and models](#missing-tools-and-models)
- [Known limits](#known-limits)

## First checks

```bash
photo status            # python modules, binaries, GPU, op and preset counts, library catalog
photo status --json
photo --debug COMMAND ...    # show the Python traceback instead of a one-line error
```

`photo` not found: use `${CLAUDE_SKILL_DIR}/.venv/bin/photo`, or re-run the installer (see
[install-models-licences.md](install-models-licences.md)). Every command prints one JSON object with `--json`; failures
carry an `error` field and a non-zero exit code. Read the message before retrying: most are a wrong flag or a missing
optional package that the message names.

`AttributeError: module 'photostudio' has no attribute '__version__'` from `photo status`: a stale
`photostudio.egg-info` folder. Delete it and run `.venv/bin/pip install -e .` again, or re-run `install.sh`.

## Command-line quirks

- **Global flags work anywhere.** `--json`, `--quiet`/`-q`, `--root DIR` and `--debug` are accepted before the command, after
  it, and after nested sub-commands (`photo mask preview IMG --spec sky --json`, `photo models list --json`).
- **A value that starts with a dash** is fine when it is a number or a comma list of numbers (`--loudnorm -14,-1,11`,
  `--lens-k1 -0.05`). Any other value that begins with `-` still needs the `=` form (`--text=-draft-`).
- **Selectors work everywhere an image is expected.** `id:42`, `@all`, `@last-import`, `@album:NAME`, `@session:2026-05-01`,
  `@tag:NAME` and `--query "rating>=4"` are accepted by `develop`, `edit`, `render`, `export`, `reset`, `copy-settings`,
  `paste-settings`, `info`, `contact-sheet`, `stack`, `video` inputs and every library command (`ls` and `search` also take them
  through `--sel`). Commands that act on one image (`versions`, `lut bake --file`) need a selector that matches exactly one
  image. A selector that matches nothing is an error. Details: [library.md](library.md#selecting-images).
- **Quote JSON.** Wrap mask and spot JSON in single quotes; the shell eats unquoted braces and double quotes.
- **`--mask` shorthands.** `--mask sky` (also `subject`, `background`, `face`, `skin`, `eyes`, `lips`, `teeth`, `animal`),
  `--mask '{"type":"ai_sky"}'` and `--mask @file.json` all work in `edit`, `develop` and `mask preview --spec`
  (see [masks-and-retouch.md](masks-and-retouch.md)).
- **`--set` takes `op[@id].param=value`**, several separated by commas; values are JSON (`true`, `0.5`, `[1,2]`, `"text"`).
  A value that contains a comma, such as a list, must be valid JSON inside the string (`--set 'crop.rect=[0.1,0.1,0.9,0.9]'`
  is safe because the parser splits only at top level). Names and values are checked before the sidecar is written.

## Editing and rendering

| symptom | cause and fix |
|---|---|
| a `warning: op 'X' skipped ...` line in the render log | the stored entry is invalid (old or hand-edited sidecar): it is skipped and the rest renders. Fix it with a corrected `--set`, `photo edit IMG --remove OP[@ID]`, or `photo versions restore IMG NAME` (an `auto:*` snapshot, see `photo versions snapshots IMG`) |
| `unknown op` / `has no param` / `is not valid` from `edit` or `develop` | the write-time check caught a typo and wrote nothing; the message suggests the close match. List names with `photo ops list` and `photo ops describe NAME` |
| my edit does nothing | the entry is disabled or a later entry in the same stage overrides it; `photo edit IMG` with no edit flags prints the stack (or read `IMG.ps.json`); look at `enabled`, order and `id` |
| edit changed the wrong layer | merge key is op plus `id`. Without `@id` the global entry is edited; masked entries need `@id` to be addressed again |
| result looks flat or too contrasty | the tone map (stage 16) is the one place contrast lives; adjust `tone_map` and `filmic`/curve params, not many stacked contrast ops |
| mode is wrong | `photo info IMG` shows the detected mode and confidence; force it with `photo develop IMG --mode landscape`. Confidence under 0.30 falls back to `events` |
| AI mask is rough | model not installed, the fallback masker ran (stated in the render log). `photo models download NAME`, then re-render |
| heal or clone lands in the wrong place | stage-8 spots use the source frame, entry masks are post-geometry; radius is px@2000 for heal/clone and a width fraction for inpaint |
| art look too strong or slow | lower the look strength with `--merge --set LOOKOP.opacity=0.6` (see [art-modes.md](art-modes.md)), preview at `--preview 900`, and check the timing column of the catalogue |
| `develop` writes next to my files unexpectedly | `develop` writes to `~/.cache/photostudio/develop/` unless `--out` is given; pass `--out` explicitly |

## Output and export

- **A `warnings` entry "saved as baseline instead"**: a very detailed result (such as `halftone_cmyk`) could not be written as
  a progressive, optimised JPEG even with the enlarged encoder buffer, so it was written as a baseline JPEG. Nothing to fix;
  see [export.md](export.md#very-detailed-images-and-jpeg).
- **`PyCMSError: cannot open profile file`**: an export preset references an ICC profile that is not on disk; use `--icc srgb` (or `p3`,
  `adobe`, `none`), or place the file where the preset points.
- **Unknown naming token**: the error lists the valid ones (`{stem}`, `{date:%Y%m%d}`, `{seq:03d}`, `{mode}`, ...; see [export.md](export.md)).
- **Proof or soft-proof error**: `--proof` needs an ICC profile name or path that exists; list export presets with `photo presets list export`.
- **Files overwritten?** Export never touches originals. It overwrites an existing output of the same name unless `--skip-existing` is given; use `--out` and a `--name` template to keep runs apart.
- **Colours differ in the browser**: export `web` presets as sRGB; 16-bit TIFF presets keep Rec.2020 or ProPhoto per the preset.

## Library, import and backup

- **Nothing shows in `ls` after copying files in by hand.** Run `photo --root LIB scan` (the sidecars are the source of truth,
  the SQLite catalog is a cache and can be rebuilt any time: `photo --root LIB catalog rebuild`).
- **Catalog out of date or locked**: stop other `photo` or `photo serve` processes on the same root, then `scan`.
- **Import skipped files**: they were duplicates (same BLAKE3 hash already in the library). The result lists them in `counts.duplicates`.
- **Card not safe to format**: only trust `safe_to_format: true` with zero errors; format in the camera.
- **`cull --apply` left some images alone**: flags you set by hand are kept (`kept_user_flags` in the result). Use
  `--overwrite` to replace them; undo an apply with `photo flag none SELECTOR`. Culling never deletes; `--move-rejects` moves
  files to `_rejects/` under the library, and the catalog does not index `_rejects/`, `exports/` or hidden folders.
- **Backup refuses or prints a plan only**: `--plan` is a dry run; for rclone and restic destinations the binary must be on PATH
  (`photo status`). Never claim off-site safety unless `status` shows `ever_ok: True` and a recent `last_run` for an `offsite` destination.
- **Tag or search finds nothing**: try `photo ls --count --query "..."` with a simpler query and add terms one at a time; field names and
  aliases are listed in [library.md](library.md).

## Video

- **ffmpeg missing or without a codec** (x265, libfdk, NVENC): `photo status` shows ffmpeg and ffprobe. On Fedora the full build is
  RPM Fusion's. Set `PHOTOSTUDIO_FFMPEG=/path/to/ffmpeg` to use another binary.
- **Slow**: `--frames python` (frame-by-frame in Python, needed for raster art looks) is much slower than the baked-LUT ffmpeg
  path. Try a colour-only look first, or a short test clip. `--dry-run` prints the exact ffmpeg commands without running them.
- **Wrong shot assignment**: shot numbers in `video shots --assign N:mode=X|art=Y` are the numbers shown in the list and start
  at 1; run `video shots INPUT` without `--save` first to see them. An unknown number is an error that names the valid range.
- **Output location**: next to the input unless `--out` is given.
- **NVENC and HDR PQ clips** were not tested here (no GPU in the build environment).

## Web editor

- **Port busy**: `photo serve` picks the next free port and prints the URL; use `--strict-port` to fail instead.
- **Page loads but is empty**: wrong root. Pass the library folder explicitly (`photo serve ~/Pictures --open`) or `--root`.
- **`ModuleNotFoundError: fastapi`**: install the extra, `.venv/bin/pip install -e ".[serve]"` from the skill folder.
- **Cannot reach it from another machine**: by design it binds to 127.0.0.1 only and checks the Host header. Use an SSH tunnel
  (`ssh -L 8765:127.0.0.1:8765 host`); do not expose it.
- **Save fails with 409**: the sidecar changed on disk (CLI or Claude edited it). Reload the image in the UI, then redo the edit;
  the UI also shows an external-change notice by itself.
- **Claude's CLI edit does not appear**: the page long-polls `/api/changes`; wait a second or press reload. The CLI edit
  is in the sidecar either way. See [web-editor.md](web-editor.md).

## Missing tools and models

| missing | effect | what to tell the user |
|---|---|---|
| `exiftool` | fewer maker-note fields, weaker mode detection, no XMP-embedded writes by exiftool | `sudo dnf install perl-Image-ExifTool` |
| `ffmpeg` | video commands stop | RPM Fusion ffmpeg (see install doc) |
| `rclone` / `restic` | those backup destination types fail, local works | `sudo dnf install rclone restic` |
| `onnxruntime`, `rembg`, `mediapipe`, `torch` | AI masks fall back to heuristics; `tag auto --tier clip`, `analyze --aesthetic` stop with a named missing package | install the `ai` extra and `photo models download NAME` |
| `ultralytics` | animal mask unavailable | `yolo` extra (AGPL) |
| `insightface` | `tag people` unavailable | `faces` extra and manual weights (non-commercial) |
| `bm3d` | the `bm3d` denoise method unavailable, default denoiser works | `denoise` extra (non-commercial) |
| `hugin`, `darktable` | not driven by this skill | optional, for full-resolution panoramas / reference only |

Claude never runs `sudo`; print the command and let the user run it. Models are never downloaded without an explicit request.

## Known limits

- Real Canon R50 CR3 decoding, EasyMode and maker-note semantics, `calibrate picture-style`, real rclone and restic transfers,
  NVENC, HDR PQ video, real panoramas, real astro stacks, browser clicking, the optional AI models and InsightFace people tagging
  were **not verified** when this documentation was written (synthetic data only). If one fails, say so plainly, show the error, and
  suggest the manual route.
- The panorama stitcher is preview-grade (3000 px) and needs textured, overlapping frames.
- Cull thresholds are calibrated on synthetic bursts; on real files treat suggestions as a starting point and look at the contact sheet.
- Sharpness scoring uses a 1024 px preview, so fine focus errors on a high-resolution subject can pass.
- Only 8-bit sRGB, Display P3 and the built-in 16-bit TIFF profiles are exercised in export tests; wide-gamut screens are a manual check.
