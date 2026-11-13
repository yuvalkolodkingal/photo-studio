---
name: photo-studio
description: Develops, edits, culls, organises, exports and serves photos and video on Linux with the photo-studio CLI (rawpy/LibRaw; a Darktable/Lightroom replacement built for Canon EOS R50 CR3+JPEG). Use whenever the user wants to import a card, cull or rate a shoot, auto-edit or retouch RAW/JPEG files, apply film, cinematic, artist-style or art looks (painterly, comic, halftone, sketch, pixel art), mask sky/subject/skin, batch-match a set, stack HDR/focus/astro/panorama, export for web, print or Instagram, grade video, manage albums/tags/search, back up a library, or open the browser editor (`photo serve`). Also use for "make this photo look better", "pick my best shots", "darktable alternative", and for installing or troubleshooting photo-studio. Edits are non-destructive sidecars; originals are never modified or deleted.
---

# photo-studio

A scene-referred RAW/JPEG photo studio driven from the `photo` CLI, plus a local browser editor. Everything is
non-destructive: edits live in `<image>.ps.json` sidecars next to the files. You are the editor: render, look at the
JPEG with the Read tool, judge it, adjust, render again.

## Setup (check first, every session)

Run the binary as `${CLAUDE_SKILL_DIR}/.venv/bin/photo`. Examples below abbreviate it to `photo`: either write the
full path or start each Bash call with `export PATH="${CLAUDE_SKILL_DIR}/.venv/bin:$PATH"` (shell state does not
persist between calls). If the venv is missing, `command -v photo` may still find a linked copy.

```bash
${CLAUDE_SKILL_DIR}/.venv/bin/photo status          # modules, binaries, GPU, op/preset counts; add --json for scripts
```

Not installed (no `.venv`, or `photo: command not found`)? Tell the user the one-liner, or run it for them (it needs
network, never uses sudo, and is safe to re-run to update):

```bash
curl -fsSL https://raw.githubusercontent.com/yuvalkolodkingal/photo-studio/main/install.sh | bash
```

Manual alternative (Python 3.11 or newer; on Fedora `sudo dnf install python3.12`, which you print and the user runs):

```bash
git clone https://github.com/yuvalkolodkingal/photo-studio ~/.claude/skills/photo-studio && cd ~/.claude/skills/photo-studio
python3.12 -m venv .venv && .venv/bin/pip install -e ".[serve,meta,lens,video,heif,astro,geo]"
```

Installer flags: `--extras LIST`, `--dir DIR`, `--no-link`, `--uninstall`. Missing system tools (exiftool, ffmpeg from
RPM Fusion, rclone, restic) are reported by `photo status`; print the dnf line from
[references/install-models-licences.md](references/install-models-licences.md) and let the user run it.
Optional AI models are never downloaded unless the user asks (`photo models list`, `photo models download NAME`).

Global flags (`--json`, `--quiet`, `--root LIB`, `--debug`) go right after `photo`; `mask` and `models` subcommands
reject them after the subcommand. `--json` on any command gives machine-readable output. When something errors,
read the message, then [references/troubleshooting.md](references/troubleshooting.md).

## Mental model (15 lines)

- A **stack** is an ordered list of entries `{op, params, enabled, mask, blend, opacity, id}` stored per image **version**
  (virtual copy) in the sidecar. 139 ops, one fixed 21-stage pipeline: linear light until a single tone map (stage 16),
  display-referred looks and effects after it. Entries run in stage order, then stack order; you cannot reorder stages.
- A **mode** (12: ten genres such as portrait, landscape, night and street, plus `auto` and `video`) is the starting stack. An **art/look
  preset** (88: film stocks, cinema grades, photographer styles, painterly, raster art) layers on top. Resolution order:
  defaults, mode, art, then your `--set` edits.
- `--set op[@id].param=value` (comma separated, JSON values) edits the stack. Op names, param keys, value kinds, choices,
  mask specs and heal/clone spots are **validated before the sidecar is written**; a typo fails with a "Did you mean ...?"
  message and writes nothing. Check names with `photo ops describe NAME`. If a stored entry still cannot run, render skips it
  and logs a `warning:` line instead of failing.
- **Masks** are JSON specs attached to an entry (`--mask`), coordinates normalised 0..1, pixel sizes in px at 2000 px long edge.
- Sidecars are the source of truth, the SQLite catalog under `<root>/.photostudio/` is a rebuildable cache, XMP is
  written beside files. The CLI, Claude and the web editor all share the same sidecars.
- `develop` writes the stack and renders; `edit` changes the stack only; `render` renders the stored stack.
  `develop` without `--merge` replaces the active stack (an `auto:before-develop` snapshot is saved first).
- Every command that acts on images takes the same selectors as the library commands: paths, globs, folders, `id:N`, `@all`,
  `@last-import`, `@album:NAME`, `@session:YYYY-MM-DD`, `@tag:a|b`, and `--query "rating>=4"` (develop, edit, render,
  export, reset, copy/paste-settings, info, contact-sheet, stack, `video` inputs; `versions` and `lut bake --file` need
  exactly one image). Global flags (`--json`, `--quiet`, `--root`, `--debug`) work anywhere on the line.
- Preview while iterating (`--preview 900`), render full size only at the end.

## Decision guide

| the user wants | do this | details |
|---|---|---|
| bring in a card or folder | `photo --root LIB import SRC --dry-run`, then for real; verify, never format the card yourself | [import-backup.md](references/import-backup.md) |
| find the best shots | `analyze`, `group`, `cull` (suggest), review, `cull --apply --query "flag:none"` | [culling.md](references/culling.md) |
| "make it look good" | auto-edit critique loop below | [modes.md](references/modes.md), [pipeline-and-ops.md](references/pipeline-and-ops.md) |
| a specific look (film, comic, noir) | `develop --art NAME --merge --preview 900` | [art-modes.md](references/art-modes.md) |
| fix only the sky, face, one corner | `edit --mask JSON --set op@id.param=v` | [masks-and-retouch.md](references/masks-and-retouch.md) |
| remove a blemish, dust, a person | `heal`, `clone`, `inpaint` spots | [masks-and-retouch.md](references/masks-and-retouch.md) |
| same look across a set | `copy-settings` and `paste-settings`, or one `develop --art` over many files | [library.md](references/library.md) |
| web, print, Instagram files | `photo export --preset web\|print_tiff16\|instagram_portrait ...` | [export.md](references/export.md) |
| tags, albums, search, ratings | `tag`, `album`, `search`, `ls`, `rate`, `flag` | [library.md](references/library.md) |
| backup | `backup status`, `backup run --dry-run`, then `backup run` | [import-backup.md](references/import-backup.md) |
| HDR, focus stack, astro, trails, pano | `photo stack KIND FILES --out X.tif` | [multiframe.md](references/multiframe.md) |
| video grade, stabilise, stills, timelapse | `photo video ...` | [video.md](references/video.md) |
| hand-editing in a browser | `photo serve LIB --open` | [web-editor.md](references/web-editor.md) |
| list ops, params, stages | `photo ops list`, `photo ops describe NAME` | [pipeline-and-ops.md](references/pipeline-and-ops.md) |
| a new mode or preset of their own | `photo presets save ...` | [modes.md](references/modes.md) |
| install, models, licences, GPU | | [install-models-licences.md](references/install-models-licences.md) |

## Recipe: import with verification

```bash
photo --root ~/Pictures/Library import /run/media/$USER/EOS_DIGITAL --dry-run                  # plan only
photo --root ~/Pictures/Library import /run/media/$USER/EOS_DIGITAL --event "wedding" --dest2 /mnt/backup/Library
photo --root ~/Pictures/Library ls --last-import --count
```

Files land in dated folders, are hashed (BLAKE3) and re-read after copying; RAW+JPEG pairs stay together; duplicates are
skipped. Say the card is safe to format **only if** the JSON says `safe_to_format: true` with zero errors, and tell
the user to format it in the camera. Never use `--delete-after-verify` unless asked. Then `analyze`, `cull`, tag
(`photo tag auto --tier metadata @last-import`), and run `backup run`.

## Recipe: cull (suggest, then apply, never delete)

```bash
photo analyze @last-import && photo group @last-import
photo cull @last-import --target 0.2            # SUGGEST: score, pick/maybe/reject and reasons; writes nothing
photo cull @last-import --full | head -80       # read the reasons; sample picks AND rejects before trusting them
photo contact-sheet IMG_0012.JPG IMG_0013.JPG --out /tmp/sheet.jpg --columns 4        # look at borderline frames
photo cull @last-import --apply                       # APPLY; frames the user flagged by hand are kept
```

`--apply` never replaces a flag the user set by hand (`flag_source` in the sidecar; the result reports `kept_user_flags`);
add `--overwrite` only when the user asks for that. `--move-rejects` moves rejected files to
`_rejects/`; nothing is ever deleted. Thresholds are untested on real R50 files: present results as suggestions, show
what you would reject, and let the user decide. Details and flags: [references/culling.md](references/culling.md).

## Recipe: auto-edit with a critique loop

The loop is: render small, look, judge, change one or two things, render again. Stop after 3 to 5 rounds.

```bash
photo info IMG.CR3                                         # detected mode and confidence, sidecar summary
photo develop IMG.CR3 --mode auto --preview 1600 --out /tmp/e1.jpg     # detect mode, write stack, render
```

1. **Look** at `/tmp/e1.jpg` with the Read tool. Judge: exposure, white balance, highlights and shadows, colour, noise,
   sharpness, crop and horizon, any distraction. Compare with the original camera JPEG (Read it too).
2. **Measure** when unsure: `photo scopes /tmp/e1.jpg --out /tmp/sc` writes histogram, waveform, parade, vectorscope,
   false-colour and clipping PNGs and prints `clip_high_pct`, `clip_low_pct` and `median_ev`. Read the PNGs you need.
3. **Adjust** with `edit` (stack only), then **render** again:

```bash
photo edit IMG.CR3 --set 'exposure.ev=+0.3,highlights_shadows.shadows=15,white_balance.temperature=5400'
photo render IMG.CR3 --preview 1600 --out /tmp/e2.jpg
```

4. Wrong mode? `photo develop IMG.CR3 --mode landscape --preview 1600 --out /tmp/e1.jpg` (replaces the stack, snapshot
   kept; `photo versions restore IMG.CR3 auto:before-develop` brings the old one back). Try a variant (`night:astro`,
   `portrait:low_key`) before hand-tuning.
5. **Finish**: `photo export IMG.CR3 --preset web --out /tmp/final`, or full-size `photo render IMG.CR3 --out /tmp/full.jpg`.

Mode confidence under 0.30 falls back to `events`, a safe general default. Say which mode you used and why. Keep a
named restore point before big changes: `photo versions snapshot IMG.CR3 before_look`. Use a second version for
alternatives: `photo versions new IMG.CR3 bw --from default`.

## Recipe: masks and local edits

```bash
# graduated darkening of the sky (normalised coords, feather in px at 2000 px long edge)
photo edit IMG.CR3 --mask '{"type":"linear","p0":[0.5,0.0],"p1":[0.5,0.45],"feather":40}' --set exposure@sky.ev=-0.5
# AI sky mask (falls back to a rough heuristic if the model is not installed)
photo edit IMG.CR3 --mask '{"type":"ai_sky"}' --set 'saturation@sky.amount=10'
# radial: brighten a face area; luminosity: only the highlights
photo edit IMG.CR3 --mask '{"type":"radial","center":[0.62,0.45],"radii":[0.2,0.28],"feather":120}' --set exposure@face.ev=0.3
photo edit IMG.CR3 --mask '{"type":"luminosity","mode":"lights","level":2,"feather":20}' --set 'tone_curve@hl.points=[[0,0],[0.8,0.78],[1,0.95]]'
# check the mask before committing to it (red overlay)
photo mask preview IMG.CR3 --spec '{"type":"ai_sky"}' --out /tmp/mask.png --size 900
# change the value later WITHOUT --mask (passing --mask again appends a second entry)
photo edit IMG.CR3 --set exposure@sky.ev=-0.7
photo edit IMG.CR3 --disable exposure@sky           # or --remove exposure@sky
```

Always give a masked entry an `@id`. Mask types: linear, radial, polygon, brush, luminosity, color_range, parametric,
ai_sky, ai_subject, ai_background, ai_face, ai_skin and more, plus `combine`, and the spec fields `invert` and `refine`.
`--mask` (and `mask preview --spec`) takes JSON, `@file.json`, or a bare shorthand: `sky`, `subject`, `background`, `face`,
`skin`, `eyes`, `lips`, `teeth`, `animal`. Retouch (stage 8, source-frame coordinates):

```bash
photo edit IMG.CR3 --set 'heal.spots=[{"dst":[0.25,0.45],"src":[0.35,0.45],"radius":40}]'      # radius px at 2000
photo edit IMG.CR3 --set 'inpaint.spots=[{"center":[0.58,0.55],"radius":0.03}]'                 # radius = fraction of width
```

All mask types, blend modes and retouch ops: [references/masks-and-retouch.md](references/masks-and-retouch.md).

## Recipe: art modes and variants

```bash
photo presets list art                                           # name, title, variants, description
photo develop IMG.CR3 --art portra_400 --merge --version film --preview 900 --out /tmp/film.jpg
photo develop IMG.CR3 --art lichtenstein_comic:dots_45 --merge --version comic --preview 700 --out /tmp/comic.jpg
photo develop IMG.CR3 --art portra_400 --merge --set film_emulation.opacity=0.6 --preview 900 --out /tmp/film60.jpg
```

Use `--merge --version NAME` so the base edit stays untouched and the look is a separate virtual copy. Variants are
`name:variant`. Strength: `LOOKOP.opacity` (0..1 works on every entry) or the op's own `amount`. Raster art looks
(slot C: comic, halftone, painterly, mosaic) are slow at full size (minutes for 24 MP): always preview at 700 to 900 px
first. Pick the look by intent from the catalogue and timing columns in
[references/art-modes.md](references/art-modes.md). Canon-like renderings: `--art style:standard`
(see [references/modes.md](references/modes.md)).

## Recipe: batch consistency

```bash
photo develop IMG_0042.CR3 --mode portrait --preview 1600 --out /tmp/ref.jpg       # perfect one reference frame
photo copy-settings IMG_0042.CR3 --ops exposure,white_balance,hsl_mixer
photo paste-settings IMG_0043.CR3 IMG_0044.CR3 --mode merge --ops hsl_mixer
photo paste-settings IMG_0043.CR3 --from IMG_0042.CR3 --mode merge                 # straight from an image
photo contact-sheet /tmp/set/*.jpg --out /tmp/set_sheet.jpg --columns 5            # judge the whole set at once
```

Scene-dependent ops (`exposure`, `white_balance`) differ per frame: paste the look (`hsl_mixer`, `color_balance`,
`tone_curve`, art), then fix exposure and WB per frame. Compare frames with `photo scopes` or `photo analyze`.
To feed library selectors into `develop`, convert them to paths ([references/library.md](references/library.md)).

## Recipe: export presets

```bash
photo presets list export          # web, instagram_portrait, instagram_square, story_9x16, print_tiff16, archive_full, wallpaper_4k, email_small
photo export IMG_0001.CR3 IMG_0002.CR3 --preset web --out /tmp/web
photo export 2026/*/*.CR3 --preset instagram_portrait --out ~/Pictures/ig --name '{date:%Y%m%d}_{seq:03d}_{stem}' --jobs 4
photo export IMG.CR3 --preset print_tiff16 --out /tmp/print --version bw
```

Export renders the active version (or `--version`), resizes, output-sharpens, embeds the ICC profile and strips GPS
and serial numbers for `web` and `email_small`. Open one result and check it before the batch. Options and naming
tokens: [references/export.md](references/export.md).

## Recipe: albums, tags, search, ratings

```bash
photo --root LIB scan                                        # index new files (import does it for you)
photo ls --query "rating>=4 AND flag:pick" --sort taken_at --order desc --limit 50
photo search sunset --count
photo rate 4 id:12 id:15 && photo flag pick @album:Best && photo label red id:12
photo tag add "people|noa,events|birthday" id:1 id:5
photo tag auto --tier metadata @last-import                  # always works; clip/ram/caption tiers need optional models
photo album create "Trip 2026" @last-import
photo album smart "Best of" --rule "rating>=4 AND tag:people"
photo album export "Trip 2026" /tmp/trip --preset web --zip /tmp/trip.zip
photo album gallery "Trip 2026" /tmp/gallery --title "Trip"  # static HTML, open index.html
photo events @last-import --apply --name 1="Lisbon trip"
```

Filter DSL fields, tag management, smart albums and the people/places tools: [references/library.md](references/library.md).
`tag people` uses face recognition with a non-commercial model: only run it if the user asks.

## Recipe: backup

```bash
photo --root LIB backup init                      # once: writes a commented LIB/.photostudio/backup.yaml to edit
photo --root LIB backup status                    # copies, media, off-site, freshness, warnings
photo --root LIB backup run --dry-run             # what would be copied, needs no binaries
photo --root LIB backup run                       # all destinations in LIB/.photostudio/backup.yaml, verified
photo --root LIB backup verify                    # sampled bit-rot check against the manifest
```

The user edits the destinations (local, rclone, restic) in `backup.yaml`; `run` stops with an error until one is configured. Do not run against an
unmounted or unconfigured destination. Do not claim an off-site copy exists unless `status` shows an `offsite` destination
that has succeeded recently. 3-2-1 explained in [references/import-backup.md](references/import-backup.md).

## Recipe: stacking

```bash
photo stack hdr br0.CR3 br1.CR3 br2.CR3 --out /tmp/hdr.tif
photo stack focus f1.CR3 f2.CR3 f3.CR3 --out /tmp/focus.tif --align ecc_affine
photo stack astro sky_*.CR3 --out /tmp/sky.tif --keep 0.8 --darks dark1.CR3 dark2.CR3
photo stack trails night_*.CR3 --out /tmp/trails.jpg --method lighten
photo develop /tmp/hdr.tif --mode landscape --preview 1600 --out /tmp/hdr_prev.jpg     # the result is an ordinary image
```

Other kinds: `pano` (preview-grade OpenCV stitcher, needs 30 percent overlap and texture; for full-resolution panoramas
use Hugin), `long`, `median`. Details: [references/multiframe.md](references/multiframe.md).

## Recipe: video

```bash
photo video probe clip.MP4
photo video grade clip.MP4 --art teal_orange --preset preview_fast --start 10 --duration 8 -o /tmp/test.mp4   # short test first
photo video grade clip.MP4 --art teal_orange --preset youtube_1080p -o /tmp/clip_look.mp4
photo video shots clip.MP4 --assign 1:art=teal_orange --assign 2:mode=night --save     # per-shot looks; N is the shot number as listed, starting at 1
photo video stills clip.MP4 --best 1.0 --count 6 --format tif --bits 16 -o /tmp/stills # then develop them as photos
```

Use `--dry-run` to print the ffmpeg command. Raster art on video needs `--frames python` and is very slow. `--loudnorm I,TP,LRA`
takes values such as `-14,-1,11`. ffmpeg is required (`photo status`). Output defaults to next to the input.
See [references/video.md](references/video.md).

## Recipe: hand off to the web editor

```bash
photo serve ~/Pictures/Library --open            # 127.0.0.1:8765 (next free port if busy), Library and Develop views
```

The editor reads and writes the same sidecars, versions and snapshots as the CLI. Edits Claude makes with the CLI while
the page is open show up there (external-change events); a save from the page that lost a race is refused with a 409. Start it in the background, give the user the printed URL, and keep editing the same files if asked. It
binds to localhost only; never expose it. More in [references/web-editor.md](references/web-editor.md).

## Editing judgement

Work like a good retoucher: fix problems first, add taste second, stop early.

1. **Order of fixes.** Crop and straighten (`auto_level`, `crop`), exposure, white balance, highlights and shadows, then
   colour, then local contrast and sharpening, then creative looks. Do geometry before masks (masks follow the cropped frame).
2. **Small moves.** Exposure within about +/-1 EV, `highlights_shadows` and `local_contrast` amounts rarely above 30,
   `vibrance` before `saturation`, `denoise`/`sharpen` defaults first. If two changes fight, undo one.
3. **Contrast lives in the tone map** (stage 16). Stacking `contrast`, `tone_curve` and `local_contrast` gives crunchy,
   haloed results. Change one lever and re-render.
4. **Protect skin and skies.** Portraits: lower `clarity`/`texture`, keep skin hue natural (modes already do). Skies:
   graduated or AI-sky mask, not a global darkening. Check clipping with `photo scopes` (clip_high_pct near 0 for skies).
5. **High ISO (R50 above ISO 3200)**: let `denoise` work, avoid strong sharpening and clarity; judge at 100 percent by
   rendering a crop, not at preview size.
6. **Black and white**: `street` mode is B&W by default (`street:color` for colour); otherwise use `--art` B&W looks or
   `monochrome`.
7. **Looks are not fixes.** Fix exposure and WB first, then apply `--art`. Keep strength moderate (opacity 0.4 to 0.7)
   unless the user wants the full effect.
8. **Be honest.** Say what you changed and what you could not judge (screen colour, print). Offer a version with less.
9. **Respect "no".** If the user reverts or says it is too much, restore the snapshot instead of arguing.

## Canon EOS R50 notes and honest limits

- 24 MP APS-C (6000 x 4000), CR3 (optionally with a JPEG), 8-bit H.264 and HDR PQ HEVC video, no Canon Log. Mode
  auto-detection reads the Canon MakerNote (EasyMode, Picture Style, drive and AF mode) first, then image content;
  a camera JPEG is a different, simpler path than a RAW.
- Lens correction ships a patched lensfun database with the R50 and RF-S kit lenses (the `lens` extra).
- **Verified on synthetic data only:** the whole CLI, library, import/backup (local), export, stacking, video and the web
  API. **Not verified:** real R50 CR3 decode and maker-note semantics, `calibrate picture-style`, real rclone/restic
  transfers, NVENC, HDR PQ video, real panoramas and astro stacks, browser clicking, optional AI models, InsightFace.
  If a command fails on a real file, say so and show the error.
- AI features (`ai_*` masks, CLIP tags, aesthetic score, people) need optional packages and weights; without them masks
  fall back to rough heuristics and the render log says so. Nothing is downloaded without being asked.
- Licences: no weights or third-party data are bundled; InsightFace weights are non-commercial, YOLO is AGPL-3.0, BM3D
  is non-commercial, SegFormer is research/personal. Personal use is assumed; see
  [references/install-models-licences.md](references/install-models-licences.md).
- It is not a DAM replacement for a 100k-photo catalog and does not drive Hugin or darktable.

## Safety rules

- Never modify or delete originals. Never format a card. Never run `sudo`. Never download models unasked.
- Prefer `--dry-run` and `--preview` first; write exports to a new folder with `--out`.
- Do not run destructive library commands (`album delete`, `tag delete`, `reset`, `versions delete`) without the user asking;
  `reset` keeps an auto snapshot, `versions restore` can undo most edits.
- Keep the user's pictures, paths and tags local; do not upload them anywhere.

## References (read on demand)

| file | when |
|---|---|
| [references/pipeline-and-ops.md](references/pipeline-and-ops.md) | stack format, `--set` syntax, stages, the full ops catalogue |
| [references/modes.md](references/modes.md) | 12 modes, auto-detection, picture styles, writing your own mode |
| [references/art-modes.md](references/art-modes.md) | the 88 looks with timings, variants, strength, speed |
| [references/masks-and-retouch.md](references/masks-and-retouch.md) | mask types and JSON, blend modes, heal/clone/inpaint/liquify, dodge and burn |
| [references/culling.md](references/culling.md) | scoring, flags, `--apply` rules, grouping |
| [references/library.md](references/library.md) | selectors, DSL, tags, albums, events, versions, catalog |
| [references/import-backup.md](references/import-backup.md) | import, verification, 3-2-1 backup, restore tests |
| [references/export.md](references/export.md) | presets, naming, metadata, known JPEG failure |
| [references/video.md](references/video.md) | grade, shots, stabilise, stills, timelapse, presets |
| [references/multiframe.md](references/multiframe.md) | HDR, focus, astro, trails, pano, long, median |
| [references/web-editor.md](references/web-editor.md) | `photo serve`, shared sidecars, conflicts |
| [references/install-models-licences.md](references/install-models-licences.md) | install, extras, models, GPU, licences, env vars |
| [references/troubleshooting.md](references/troubleshooting.md) | errors, quirks, missing tools, known limits |
