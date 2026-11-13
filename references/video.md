# Video

Video mode is for the R50's clips (8-bit H.264, and 10-bit HDR PQ HEVC if used). **ffmpeg does the encoding.** Colour
looks from the photo pipeline are baked into a `.cube` 3D LUT and applied with ffmpeg's `lut3d`, so a look graded
on a still can be applied to a clip. Raster art effects need the slow numpy path. Set `PHOTOSTUDIO_FFMPEG=/path/to/ffmpeg`
to choose a binary; `photo status` shows whether ffmpeg, ffprobe and the Python `video` extra (`av`, `scenedetect`)
are present.

## Contents

- [Commands](#commands)
- [Grading a clip with a look](#grading-a-clip-with-a-look)
- [Per-shot grading](#per-shot-grading)
- [Clean-up, stabilise, speed, reframe](#clean-up-stabilise-speed-reframe)
- [Export presets](#export-presets)
- [Stills and time-lapse](#stills-and-time-lapse)
- [Raster art on video](#raster-art-on-video)
- [Rules for Claude](#rules-for-claude)
- [Verified and unverified](#verified-and-unverified)

## Commands

```text
photo video probe INPUT [--caps]            codec, fps, bit depth, colour tags, HDR flags, audio; --caps lists ffmpeg features
photo video grade INPUT [look] [finish] [encode]   apply a look/LUT and finishing, encode (the main command)
photo video export INPUT ...                same options as grade; use with a preset for the final encode
photo video stabilize INPUT                 vid.stab two-pass stabilisation (deshake fallback)
photo video denoise INPUT [--level light|medium|strong|chroma|temporal|nlmeans|0..1]
photo video shots INPUT                     detect shots, assign looks, analyse per-shot auto match, --save writes <video>.ps.json
photo video lut [--mode M] [--art A] [--stack FILE] [--from-image IMG] -o look.cube [INPUT]   bake a .cube; with INPUT shows the ffmpeg command
photo video preview INPUT --kind video|gif|webp|contact     quick graded preview or contact sheet
photo video stills INPUT --at T [T..] | --every S | --count N | --representative N | --per-shot [--best [WINDOW]] [--format png|jpg|tif] [--bits 8|16]
photo video timelapse INPUTS... -o out.mp4 [--fps N] [--deflicker N] [--kenburns [ZOOM]] [--develop] [--trails [DECAY]] [--query Q]
photo video startrails INPUTS... -o trails.jpg [--decay D] [--develop] [--query Q]
```

Inputs of `timelapse` and `startrails` are files, folders, globs or library selectors (`@album:Trip`, `@session:2026-06-14`,
`@last-import`, `id:N`, `--query "..."`); `video lut --from-image` takes a path or `id:N`. Videos themselves are not catalogued, so
give a video as a path.

It is `stabilize` (z), not `stabilise`. By default the output is written **next to the input** as
`<input>_graded.mp4`, `<input>_stab.mp4` and so on, never over the original. Pass `-o` for a path, and `--no-overwrite` to
refuse replacing an existing file. Every command has `--dry-run`, which prints the exact ffmpeg commands as JSON and
does nothing; use it first on any new combination.

## Grading a clip with a look

```bash
photo video probe clip.MP4
photo video grade clip.MP4 --mode video --art teal_orange --preset youtube_1080p -o /tmp/clip_look.mp4
photo video grade clip.MP4 --from-image IMG_0042.CR3 --intensity 0.7 --preset preview_fast -o /tmp/clip_match.mp4
photo video grade clip.MP4 --art portra_400 --set exposure.ev=0.3 --start 10 --duration 8 --preset preview_fast -o /tmp/test.mp4
photo video grade clip.MP4 --lut my_look.cube --intensity 0.8 --dry-run
photo video lut --art teal_orange -o /tmp/teal.cube                       # reuse the LUT elsewhere
photo video preview clip.MP4 --kind contact --art portra_400 -o /tmp/sheet.jpg   # frames, graded, on one sheet
```

- `--mode`, `--art`, `--stack FILE`, `--from-image IMG` (reuse the colour stack from a developed photo's sidecar),
  `--set op.param=value` and `--lut EXTERNAL.cube` layer like photo edits. `--intensity 0..1` blends the look with identity.
- Only **colour** ops can go into a LUT. Spatial ops (grain, blur, sharpen, halation, art effects) are skipped or
  need `--frames python`. Native ffmpeg alternatives for the finishing touches: `--grain`, `--vignette`, `--sharpen`,
  `--denoise`, `--deband`, `--letterbox 2.39`.
- Default transfer for decoding is `--transfer srgb` (same as photos); use `bt709` or `gamma24` for log-free Rec.709
  footage if the look seems too contrasty.
- Work on a short test first: `--start 10 --duration 5 --preset preview_fast`, extract a still (`video stills --at 2`)
  and Read it, then run the full encode.

## Per-shot grading

Cuts in a clip often need different exposure or white balance. Detect shots, then bake one LUT per shot:

```bash
photo video shots clip.MP4                                      # list shots (PySceneDetect, or ffmpeg fallback)
photo video shots clip.MP4 --assign 1:art=teal_orange --assign 2:mode=night --save   # N = the shot number in the list, starting at 1; looks go into clip.MP4.ps.json
photo video shots clip.MP4 --analyze both --save                # compute per-shot exposure and WB match values
photo video grade clip.MP4 --art portra_400 --per-shot --auto both --auto-reference median --max-ev 1.0 --preset youtube_1080p -o /tmp/out.mp4
```

`--auto exposure|wb|both` matches each shot to the median (or `first`, `index:N` with N the shot number, 1 = first shot) with at most `--max-ev` of change,
`--smooth` splits long shots and smooths the values over time. Shot lists are stored in `<video>.ps.json`, like photo
sidecars.

## Clean-up, stabilise, speed, reframe

```bash
photo video stabilize clip.MP4 --shakiness 6 --smoothing 20 --optzoom 2 -o /tmp/clip_stab.mp4   # vid.stab, tripod: --tripod
photo video denoise clip.MP4 --level medium -o /tmp/clip_dn.mp4
photo video grade clip.MP4 --slowmo 4 --preset youtube_1080p            # conform 100/120p footage to quarter speed
photo video grade clip.MP4 --reframe 9:16 --reframe-mode blur --preset reels_9x16_1080x1920   # vertical with blurred sides
photo video grade clip.MP4 --speed 2 --blend 3 --fps 30                  # fast-forward with motion blur
photo video grade clip.MP4 --stabilize 5 --denoise light --sharpen 0.3 --loudnorm -14,-1,11 --audio loudnorm
```

`--stabilize` needs ffmpeg built with `vidstab`; otherwise it falls back to `deshake`. Stabilisation crops or zooms
the frame (`--optzoom`, `--crop`).

## Export presets

<!-- BEGIN GENERATED: video-presets -->
| preset | what it produces | variants |
|---|---|---|
| `archive_prores` | **Archive intermediate - ProRes 422 HQ (10-bit)**. prores_ks 422 HQ in a MOV with uncompressed PCM audio and Apple-style vendor tag. Because the R50 records 8-bit 4:2:0 H.264, this protects against generation loss while editing; it does not add source quality. Large files. | `4444`, `lt`, `proxy` |
| `dnxhr` | **DNxHR HQX intermediate (10-bit 4:2:2)**. Avid DNxHR HQX in a MOV (10-bit 4:2:2, resolution independent) with PCM audio, for editors that prefer DNx over ProRes. Variants: hq (8-bit), sq, lb for lighter proxies. | `hq`, `lb`, `sq` |
| `gif` | **Animated GIF (480 px, 12 fps, palette-optimised)**. Two-stage palette GIF (palettegen stats_mode=diff + paletteuse bayer dither) - the quality way to make a GIF with ffmpeg. Use the `webp` variant for a much smaller animated WebP. Audio is dropped. | `small`, `webp` |
| `hevc_10bit` | **HEVC 10-bit master (libx265, CRF 20)**. Compact high-quality master: libx265 10-bit 4:2:0 (hvc1 tag for Apple players), CRF 20, BT.709 tags, audio passed through. The `hdr10` variant preserves an HDR-PQ clip (R50 HDR PQ mode: BT.2020 / SMPTE 2084 tags, mastering display and content-light metadata) instead of tone-mapping it to SDR. | `hdr10` |
| `preview_fast` | **Fast preview (540p H.264, hardware encoder when available)**. Small, quick preview render for checking a grade: max 540 px high, x264 veryfast CRF 28 (NVENC when a GPU is detected), low-bitrate AAC. Never upscales. | - |
| `reels_9x16_1080x1920` | **Reels / TikTok / Shorts 9:16 (1080x1920, 30 fps)**. Vertical 1080x1920 H.264 + AAC for Instagram Reels, TikTok and YouTube Shorts. Landscape footage is reframed with a blurred, darkened copy filling the frame (the `crop` variant centre-crops instead, `pad` letterboxes with black). 30 fps, CRF 20 under an 8 Mbps cap; keep clips under 3 minutes for Reels. | `crop`, `pad` |
| `youtube_1080p` | **YouTube 1080p (H.264 High, BT.709, fast-start)**. YouTube's recommended upload encode: MP4 with the moov atom first, H.264 High profile, CABAC, 2 B-frames, closed GOP of half the frame rate, 4:2:0, BT.709 tags, AAC-LC 48 kHz. CRF 18 (visually lossless at YouTube's 8 Mbps reference for 24-30 fps / 12 Mbps for 48-60 fps). Never upscales; audio is passed through when it is AAC. | `hevc`, `hq` |
| `youtube_4k` | **YouTube 4K / 2160p (H.264 High, or HEVC 10-bit)**. YouTube's 2160p upload guidance (35-45 Mbps for 24-30 fps, 53-68 Mbps for 48-60 fps): H.264 High, CRF 20 with a maxrate cap, closed GOP of half the frame rate, 2 B-frames, BT.709 tags, AAC-LC 48 kHz. The `hevc` variant is smaller and keeps gradients cleaner from 8-bit sources (10-bit encode). | `hevc` |
<!-- END GENERATED: video-presets -->

- `--preset NAME[:variant]`. `--hw auto` (default) uses NVENC on the RTX 4050 only if it really works, otherwise software
  encoding; force with `--hw on|off` or `--encoder libx265`.
- Audio: `--audio auto` copies when compatible, `aac`, `pcm`, `loudnorm` (EBU R128; `--loudnorm I,TP,LRA`, for example `--loudnorm -14,-1,11`; `=` is optional), or `none`.
- HDR PQ or HLG clips: `--hdr auto` (default), `tonemap` (to SDR, algorithm `--tonemap hable|mobius|reinhard`) or `passthrough`.
  Check `video probe` for `is_hdr_pq`, and Read a still after tone-mapping.

## Stills and time-lapse

```bash
photo video stills clip.MP4 --best 1.0 --count 6 --format tif --bits 16 -o /tmp/stills     # sharpest frame near 6 even moments
photo video stills clip.MP4 --per-shot --format png -o /tmp/shotstills
photo video timelapse "2026/2026-06-14_tl/*.CR3" --develop --mode landscape --deflicker 10 --fps 24 -o /tmp/tl.mp4
photo video timelapse /tmp/frames --kenburns 0.2 --blend 2 -o /tmp/tl_zoom.mp4
photo video startrails night_*.CR3 --develop --decay 0.98 -o /tmp/trails.jpg                # comet-style trails
```

Stills come out as PNG16, TIFF or JPEG that the photo pipeline can develop (`photo develop still.png --mode video`).
Time-lapses from RAW need `--develop` (rendered through the photo pipeline first, so it is slow for hundreds of frames;
use `--long-edge 1920`).

## Raster art on video

`--frames python` pipes every frame through numpy so slot-C art looks (halftone, painterly, comic, VHS) work on video.
It is **slow**: seconds per frame for the slow looks. Always combine it with `--max-width 640` and `--duration 3` for a
test, and warn the user about the time before running a full clip. `--reseed-grain` gives new grain per frame, `--seed N`
a fixed grain pattern. Colour-only looks do not need it; they use the LUT path.

## Rules for Claude

- Probe first, dry-run second, short test third, full encode last. Say which of these you did.
- Never overwrite the original clip; the defaults write next to it with a suffix, and `-o` should point to a new file.
- Report the ffmpeg binary in use (`photo status`) and any `--hw` fallback.
- Do not promise 8K, 120p or HDR handling that was not checked on the user's real clips.

## Verified and unverified

Verified on synthetic 640x360 H.264 clips: probe, grade (dry run and real encode with a LUT look), `--per-shot --auto
both` (dry run), shots, lut, stills, preview contact sheet, `--frames python` on a short test. **Not verified:** real R50
files (4K, 10-bit HEVC HDR PQ, 120p), NVENC encodes, vidstab (depends on the ffmpeg build; check `photo video probe
INPUT --caps`), loudnorm results, Fedora's ffmpeg-free build (RPM Fusion ffmpeg is needed for some codecs).
