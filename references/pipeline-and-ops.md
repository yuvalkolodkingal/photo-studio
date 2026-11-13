# Pipeline, stacks and ops

Read this when you need to know what an op does, which parameter to set, or why an edit behaves oddly. The op tables
below are generated from the live registry (`scripts/gen_reference_docs.py`), so names, defaults and choices are exact.
Slider ranges are in the one-line description; `photo ops describe NAME` prints the same data for one op.

Throughout, `photo` means `${CLAUDE_SKILL_DIR}/.venv/bin/photo` (or plain `photo` if it is on PATH).

## Contents

- [How an edit is stored](#how-an-edit-is-stored)
- [The `--set` syntax](#the---set-syntax)
- [Resolution order](#resolution-order)
- [Fixed stage order](#fixed-stage-order)
- [Rules that save debugging time](#rules-that-save-debugging-time)
- [Op catalogue](#op-catalogue)

## How an edit is stored

An edit is a **stack**: an ordered list of entries in the sidecar `<image>.ps.json` next to the file (for
`IMG_0001.CR3` it is `IMG_0001.CR3.ps.json`). Originals are never touched. Each entry:

```json
{"op": "exposure", "id": "sky_grad", "params": {"ev": -0.4}, "enabled": true,
 "mask": {"type": "linear", "p0": [0.5, 0.0], "p1": [0.5, 0.45], "feather": 40},
 "blend": "normal", "opacity": 1.0, "v": 1}
```

- `op`: registry name (`photo ops list`).
- `id`: optional label. Op plus id is the **merge key**. A masked entry without an id is always appended.
- `params`: only the values that differ from the op defaults. Missing keys fall back to defaults at render time.
- `enabled`, `opacity` (0..1), `blend` (22 modes, see [masks-and-retouch.md](masks-and-retouch.md)).
- `mask`: a mask spec or null.

The sidecar also holds `versions` (named virtual copies, each with its own `mode`, `art` and `stack`), `active`
(the version commands act on), `snapshots` (named history points, including automatic `auto:*` ones taken before
destructive changes), plus rating, flag, label, tags and notes. The SQLite catalog is only a rebuildable cache;
the sidecar is the source of truth. A `.bak` of the previous sidecar is kept next to it.

## The `--set` syntax

```
--set op.param=value
--set op@id.param=value          # address (or create) an entry with that id
--set op.nested.key=value        # dotted path into dict params
--set op.enabled=false           # also: opacity, blend
--set 'a.x=1,b.y=2'              # comma separated, or repeat --set
```

Values are parsed as numbers (`+0.3` works), `true`/`false`, JSON (`[..]`, `{..}`, quoted strings), else a bare
string. Quote JSON values for the shell. Examples:

```bash
photo edit IMG.CR3 --set exposure.ev=+0.3 --set white_balance.temperature=5200
photo edit IMG.CR3 --set 'tone_equalizer.bands=[0,0,0,0,0,-0.3,-0.5,-0.7,-0.4]'
photo edit IMG.CR3 --set 'heal.spots=[{"dst":[0.25,0.45],"src":[0.35,0.45],"radius":40}]'
photo edit IMG.CR3 --disable vignette --remove grain --enable dehaze
```

**`--set` is validated before anything is written.** `edit` and `develop` check the op name, the param key (against the
op's defaults, `photo ops describe NAME`), the value kind (number, true/false, JSON list or object), `choices` params such as
`tone_map.curve`, the `--mask` spec, and `heal`/`clone` (`dst`) and `inpaint`/`red_eye` (`center`) spots. A mistake stops the
command with a message that suggests close matches (`op 'exposure' has no param 'evv'. Did you mean 'ev'?`) and leaves the
sidecar untouched. `--remove/--enable/--disable` reject unknown op names the same way and report ops that are valid but not
in the stack as `not_in_stack`. Entries that are already stored and cannot run (an old or hand-edited sidecar) are skipped
at render time with a `warning:` line in the log, so one bad entry never makes an image unrenderable. Still render once after a
hand-typed edit and read the `warnings`.

With `--mask JSON`, the `--set` edits become a **new masked entry** (always appended), so local edits stack:

```bash
photo edit IMG.CR3 --mask '{"type":"linear","p0":[0.5,0.0],"p1":[0.5,0.45],"feather":40}' --set exposure@sky.ev=-0.5
```

## Resolution order

`develop` and `render` resolve the stack as **defaults < mode < art < user edits**:

1. Pipeline defaults for the source kind (`raw` or `jpeg`). A JPEG drops raw-only ops and defaults `tone_map` to
   `none` (soft shoulder only, no second S-curve).
2. Mode preset (`--mode landscape`, `--mode night:astro`, `--mode auto`). See [modes.md](modes.md).
3. Art preset (`--art teal_orange`, `--art warhol_silkscreen:grid_2x2`). See [art-modes.md](art-modes.md).
4. Your `--set` edits.

Merging: an entry updates the first unmasked entry with the same op and id (params shallow-merged, other fields
replaced) or is appended. Consequences you must remember:

- `photo develop IMG --art X` **without `--merge` rebuilds the stack from scratch**, drops earlier manual edits from
  the active version and leaves the previous `mode` label in the sidecar. It saves an `auto:before-develop` snapshot
  first. Add `--merge` to layer a mode or art on the existing stack, and `--version NAME` to keep the original look
  in another virtual copy.
- `photo edit` only changes the sidecar (no render). `photo render` only renders (no sidecar change).
- `photo reset IMG` clears the stack but saves an `auto:` snapshot first, so it is recoverable
  (`photo versions restore IMG auto:...`).

## Fixed stage order

Entries run in a **fixed stage order**, not stack order. Inside one stage, entries run in stack order. The pipeline is
scene-referred: linear Rec.2020 float until the single tone mapper (stage 16), display-referred afterwards.

Three exclusive slots exist so looks do not fight each other:

- **Slot A, stage 15**: optical looks in linear light (bloom, halation, lens blur, spectral mix, background blur).
- **Slot B, stage 18**: display-referred looks and colour (film emulation, split tone, curves, LUTs, named art looks).
- **Slot C, stage 20**: raster art (halftone, painterly, comic, mosaic). These are slow and spatial.

`photo render --upto-stage N` stops after stage N for debugging (the output is not display-ready).

<!-- BEGIN GENERATED: stages -->
| # | stage | domain | note | ops |
|---|---|---|---|---|
| 1 | raw_prepare | mosaic | decode: black/white per CFA channel, hot pixels | - |
| 2 | raw_white_balance | mosaic | decode: as-shot or daylight white balance | - |
| 3 | highlight_reconstruction | mosaic | decode: highlight reconstruction | - |
| 4 | demosaic | mosaic | decode: demosaic (settings live in the `raw` op) | `raw` |
| 5 | denoise | linear |  | `denoise`, `hot_pixels`, `profiled_denoise` |
| 6 | capture_sharpen | linear |  | `capture_sharpen` |
| 7 | lens | linear |  | `ca_correct`, `defringe`, `lens_correct` |
| 8 | retouch | linear |  | `blemish_auto`, `clone`, `heal`, `inpaint`, `liquify`, `red_eye` |
| 9 | camera_matrix | linear | decode: camera matrix to linear Rec.2020 (not an op) | - |
| 10 | exposure_color_calibration | linear |  | `exposure`, `gamut_compress`, `white_balance` |
| 11 | geometry | linear |  | `auto_level`, `crop`, `flip`, `perspective`, `rotate`, `straighten` |
| 12 | tone_equalizer_dehaze | linear |  | `dehaze`, `dodge_burn`, `hdr_tone`, `highlights_shadows`, `scene_curve`, `tone_equalizer` |
| 13 | color_grading | linear |  | `channel_mixer`, `color_balance`, `color_zones`, `contrast`, `hsl_mixer`, `monochrome`, `saturation`, `vibrance` |
| 14 | local_contrast | linear |  | `deconvolve`, `local_contrast`, `sharpen` |
| 15 | optical_looks_slot_A | linear | SLOT A: optical looks in linear light | `background_blur`, `bloom`, `halation`, `lens_blur`, `spectral_mix` |
| 16 | tone_mapper | linear | the single tone mapper | `tone_map` |
| 17 | gamut_map_oetf | display | gamut map + output OETF (implicit, not an op) | - |
| 18 | look_slot_B | display | SLOT B: display-referred looks and colour | `adams_zone`, `aerochrome`, `blade_runner`, `bleach_bypass`, `color_grade`, `crewdson`, `cross_process`, `cyanotype`, `daguerreotype`, `day_for_night`, `duotone`, `eggleston`, `eye_enhance`, `fan_ho`, `film_emulation`, `fincher`, `gradient_map`, `infrared_bw`, `invert`, `leiter`, `levels`, `lik_hdr`, `lith`, `lomo`, `lut3d`, `maier`, `matrix_green`, `mccurry`, `moriyama`, `platinum`, `print_film`, `salgado`, `sepia`, `skin_smooth`, `sky_enhance`, `solarize`, `split_tone`, `sugimoto_sea`, `teal_orange`, `technicolor_3strip`, `teeth_whiten`, `temperature`, `thermal`, `tone_curve`, `wes_anderson`, `wet_plate`, `xray` |
| 19 | effects | display | effects: grain, glow, vignette ... | `chromatic_shift`, `dust_scratches`, `fog_haze`, `glow`, `grain`, `light_leak`, `noise`, `orton`, `soft_focus`, `tilt_shift`, `vignette` |
| 20 | raster_art_slot_C | display | SLOT C: raster art (slow, spatial) | `banksy_stencil`, `border`, `cel_toon`, `fairey_hope`, `gameboy`, `glitch`, `gum_bichromate`, `halftone`, `haring`, `ink_xdog`, `klimt`, `kmeans_quantize`, `letterbox`, `lichtenstein`, `low_poly`, `mondrian`, `monet`, `mosaic`, `oil_paint`, `painterly`, `paper_texture`, `pencil_sketch`, `pixel_art`, `pointillism`, `posterize`, `risograph`, `scanlines`, `threshold`, `van_gogh`, `vhs`, `warhol`, `watercolor`, `xdog` |
| 21 | output | display | output: resize, sharpen | `output_sharpen`, `resize` |
<!-- END GENERATED: stages -->

## Rules that save debugging time

- **Tone mapping happens once.** Do not stack `tone_curve` S-curves on a JPEG that already has a camera curve;
  adjust `contrast` or `highlights_shadows` instead. `tone_map.curve` is `auto` (sigmoid for raw, none for JPEG).
- **Do tonal work before colour.** Exposure, white balance and highlights/shadows first, then `vibrance`,
  `hsl_mixer`, `color_balance`, then grain and vignette last.
- **Prefer `vibrance` over `saturation`** for people and skin; both are -100..100.
- **Pixel sizes are defined at a 2000 px long edge** (feather, brush radius, heal radius, sharpen radius) and scale
  with the render size, so a `--preview 700` looks like the full render.
- **Retouch and geometry coordinates differ.** Entry masks are normalised 0..1 of the *post-geometry* image. Stage 8
  spots (`heal`, `clone`, `inpaint`, `liquify`, `red_eye`) use the *source* frame (before crop/rotate). Details in
  [masks-and-retouch.md](masks-and-retouch.md).
- **Spatial ops cannot be baked into a LUT.** The op table marks them `spatial`. Only colour-only stacks can go
  through `photo lut bake` and the fast video path.
- **Denoise is ISO-adaptive** (`iso_adapt=true`, noise profile fitted per ISO). Raise `denoise.luma` or `chroma`
  for ISO 6400 and above on the R50. The BM3D method is non-commercial licensed.
- **Lens correction needs lensfun data.** The skill ships a database that includes the R50 and RF-S kit lenses. If
  the render log says `camera '...' not in lensfun data`, the correction is skipped and everything else still works.
- **Highlight reconstruction and demosaic** are settings of the `raw` op (`demosaic=dht`, `wb=camera`,
  `highlight=reconstruct`). They only apply to RAW files.

## Op catalogue

Every registered op, grouped by stage. A param shown as `null` means "off or automatic". Choices are listed in parentheses.
Ops tagged `spatial` look at neighbouring pixels; `changes geometry` ops are the crop/rotate family;
`raw only` ops are dropped for JPEG.

<!-- BEGIN GENERATED: ops -->
#### Stage 4: demosaic (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `raw`<br><sub>raw only, spatial</sub> | `demosaic`=dht (linear/vng/ppg/ahd/dcb/dht/aahd), `wb`=camera, `highlight`=reconstruct (clip/reconstruct/ignore/none), `half_size`=false | Decode parameters (demosaic, raw WB, highlight mode); consumed by the decoder, no-op in the pipe. |

#### Stage 5: denoise (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `denoise`<br><sub>spatial</sub> | `luma`=50.0, `chroma`=70.0, `method`=wavelet (wavelet/bilateral/nlm/bm3d), `detail`=20.0, `scales`=5, `iso_adapt`=true, `vst`=true, `a`=null, `b`=null, `profile`=iso (iso/fit), `wb_gain`=1.5, `sigma`=null, `chroma_smooth`=1.0 | Luma/chroma denoise in an orthonormal opponent space after a variance-stabilising transform (profiled Poisson-Gaussian noise): luma/chroma 0..100 (50 = nominal 1 sigma), ISO-adaptive via ctx.meta['ISO']. Methods: wavelet (a-trous + BayesShrink), bilateral, nlm (OpenCV), bm3d (optional). |
| `hot_pixels`<br><sub>spatial</sub> | `threshold`=6.0, `dead`=false, `amount`=1.0 | Remove isolated hot (and optionally dead) pixels: a pixel is replaced by the 3x3 median when it is the strict local extreme and deviates by > threshold sigma (noise-stabilised). RGB fallback of the raw-stage filter. |
| `profiled_denoise`<br><sub>spatial</sub> | `luma`=50.0, `chroma`=70.0, `detail`=20.0, `scales`=5, `iso_adapt`=true, `a`=null, `b`=null, `profile`=iso, `wb_gain`=1.5 | Profiled denoise: variance-stabilised (Poisson-Gaussian a,b per ISO or fitted) wavelet denoise. |

#### Stage 6: capture_sharpen (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `capture_sharpen`<br><sub>spatial</sub> | `amount`=0.8, `radius`=null, `iterations`=8, `threshold`=2.5, `damping`=0.5 | Capture sharpening: Richardson-Lucy with a small Gaussian PSF in linear light, gated by a noise-aware local-contrast mask. radius = native sigma in px (None = auto from lens/diffraction: R50 3.7 um pitch). |

#### Stage 7: lens (any/linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `ca_correct`<br><sub>spatial</sub> | `red`=0.0, `blue`=0.0, `auto`=false, `strength`=1.0, `center`=[0.5, 0.5] | Lateral chromatic aberration: red / blue are radially rescaled to match green. red / blue = relative magnification of that channel (e.g. 0.001 = 0.1 %); auto estimates them from the image and adds the manual values. |
| `defringe`<br><sub>spatial</sub> | `amount`=60.0, `radius`=3.0, `threshold`=0.04, `hues`=["purple", "green"], `mode`=average (average/neutral), `chroma_min`=0.03 | Purple/green fringe removal: chroma of fringe-hued pixels within `radius` px (at 2000) of strong luminance edges is pulled toward the local non-fringe average (or neutral). |
| `lens_correct`<br><sub>spatial</sub> | `auto`=true, `distortion`=true, `tca`=true, `vignetting`=true, `scale`=0.0, `camera`=null, `lens`=null, `focal`=null, `aperture`=null, `distance`=null, `k1`=0.0, `k2`=0.0, `vignette`=0.0, `autoscale`=true, `force`=false | Lens corrections (vignetting in linear light, then distortion + TCA in one remap) from lensfun for the camera/lens in the metadata (bundled Canon RF-S data). Falls back to manual k1/k2/vignette; if lensfunpy or the lens is missing, only a note is logged. Camera JPEGs are skipped (already corrected) unless force. |

#### Stage 8: retouch (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `blemish_auto`<br><sub>spatial</sub> | `sensitivity`=0.5, `max_size`=24.0, `min_size`=3.0, `skin_only`=true, `radius`=3.0, `method`=telea (telea/ns/lama), `grow`=2.0, `feather`=2.0 | Auto-detect small skin blemishes / dust specks and inpaint them. |
| `clone`<br><sub>spatial</sub> | `spots`=[], `mode`=clone (heal/clone), `method`=blur_swap (blur_swap/seamless), `feather`=0.35, `radius`=14.0 | Clone stamp: feathered copy of a source patch (no tonal matching). |
| `heal`<br><sub>spatial</sub> | `spots`=[], `mode`=heal (heal/clone), `method`=blur_swap (blur_swap/seamless), `feather`=0.35, `radius`=14.0 | Spot heal: feathered clone of a source patch with its low frequencies swapped for the destination's. |
| `inpaint`<br><sub>spatial</sub> | `region`=null, `spots`=[], `radius`=3.0, `method`=telea (telea/ns/lama), `grow`=2.0, `feather`=2.0 | Remove objects: fill a mask spec (`region`) or round `spots` [{center, radius}] from the surroundings. |
| `liquify`<br><sub>spatial</sub> | `warps`=[], `radius`=120.0, `strength`=0.5 | Mesh-free liquify: warps [{type: push\|pinch\|bloat\|twirl, center, radius (px@2000), strength, dx, dy}] accumulate an inverse-mapping displacement field (smoothstep falloff) sampled with cv2.remap (cubic). push moves content by (dx, dy) (normalised); pinch contracts (strength>0) / bloat expands towards `center`; twirl rotates by strength*pi. |
| `red_eye`<br><sub>spatial</sub> | `threshold`=1.6, `strength`=1.0, `channel`=auto (auto/red/green), `spots`=[], `auto`=true | Replace red (or green/yellow animal) eye-shine inside the eye region with a dark neutral, keeping specular highlights (pixels where all channels are high are left alone). |

#### Stage 10: exposure_color_calibration (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `exposure` | `ev`=0.0, `black`=0.0, `compensate_bias`=false | Exposure in EV (x 2^ev) on linear data, optional black offset and EXIF bias compensation. |
| `gamut_compress` | `threshold`=0.8 | Compress negative/imaginary colours toward the achromatic axis (ACES RGC style). |
| `white_balance` | `temperature`=6504.0, `tint`=0.0, `method`=cat16 (cat16/bradford/vonkries/multipliers), `auto`=null (None/grayworld/shades_of_gray/white_patch), `picker`=null | Colour calibration: chromatic adaptation (CAT16 default, Bradford) from an assumed scene illuminant (temperature/tint relative to the as-shot neutral, 6504 K = unchanged) or an auto/picked estimate. |

#### Stage 11: geometry (any)

| op | params = default (choices) | what it does |
|---|---|---|
| `auto_level`<br><sub>changes geometry, spatial</sub> | `mode`=horizon (horizon/verticals/auto), `max_angle`=10.0, `amount`=1.0, `angle`=null, `crop_mode`=crop (crop/expand/keep) | Auto-straighten: Hough-line detection of the horizon (landscape) or verticals (architecture), then rotate with auto-crop. angle overrides detection; amount scales the correction (0..1). |
| `crop`<br><sub>changes geometry, spatial</sub> | `rect`=null, `aspect`=null, `anchor`=[0.5, 0.5], `orientation`=auto (auto/landscape/portrait) | Crop to a normalised rect [x, y, w, h] and/or aspect preset (1:1, 4:5, 3:2, 16:9, 9:16, golden ...). With an aspect only, the largest such rectangle is taken around `anchor`. orientation=auto follows the image. |
| `flip`<br><sub>changes geometry, spatial</sub> | `horizontal`=false, `vertical`=false, `turns`=0 | Mirror horizontally / vertically and rotate by quarter turns (counter-clockwise). |
| `perspective`<br><sub>changes geometry, spatial</sub> | `vertical`=0.0, `horizontal`=0.0, `rotate`=0.0, `auto`=none (none/vertical/horizontal/both), `strength`=1.0, `focal`=null, `corners`=null, `mode`=crop (crop/keep), `max_tilt`=30.0, `aspect`=original | Keystone / perspective correction. Manual: vertical / horizontal sliders (-100..100, +vertical widens the top edge, +horizontal widens the right edge, 100 = 30 deg virtual camera tilt) and roll `rotate`; `corners` [[x,y]*4 TL,TR,BR,BL normalised] maps a quadrilateral to the full frame; auto=vertical\|horizontal\|both fits vanishing points of detected lines (strength 0..1 = how much of the full correction to apply). |
| `rotate`<br><sub>changes geometry, spatial</sub> | `angle`=0.0, `mode`=crop (crop/expand/keep), `aspect`=original | Rotate by `angle` degrees (counter-clockwise positive). mode=crop: largest rectangle of the original (or given) aspect inside the rotated image; expand: grow canvas (black corners); keep: same canvas. |
| `straighten`<br><sub>changes geometry, spatial</sub> | `mode`=horizon (horizon/verticals/auto), `max_angle`=10.0, `amount`=1.0, `angle`=null, `crop_mode`=crop (crop/expand/keep) | Alias of auto_level (manual `angle` or Hough horizon detection) with auto-crop. |

#### Stage 12: tone_equalizer_dehaze (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `dodge_burn`<br><sub>spatial</sub> | `strokes`=[], `regions`=[], `radius`=60.0, `hardness`=0.2, `flow`=1.0, `ev`=0.3 | Dodge/burn as masked linear exposure: every stroke {points, radius, hardness, flow, ev} adds ev stops where it paints (positive = dodge, negative = burn); `regions` [{mask: spec, ev}] add EV through any mask spec. |
| `dehaze`<br><sub>spatial</sub> | `strength`=0.0, `distance`=1.0, `patch`=15.0, `t0`=0.2, `refine_radius`=60.0, `airlight`=null | Haze removal by dark channel prior (He et al.) with guided-filter transmission; negative strength adds haze. t0 is the minimum transmission (larger on linear data to avoid noise blow-up); distance limits the reach. |
| `hdr_tone`<br><sub>spatial</sub> | `shadows`=0.0, `highlights`=0.0, `compress`=0.0, `detail`=0.0, `radius`=60.0, `edge_ev`=0.7, `pivot_ev`=0.0 | Durand-Dorsey style local tone mapping in log2 luminance: compress the edge-aware base around grey, lift shadows (EV, up to ~3), pull highlights, and re-inject (optionally boosted) detail. |
| `highlights_shadows`<br><sub>spatial</sub> | `shadows`=0.0, `highlights`=0.0, `whites`=0.0, `blacks`=0.0, `radius`=40.0, `edge_ev`=0.6, `tonal_width`=2.0, `shadow_pivot`=-2.0, `highlight_pivot`=1.0 | Shadows lift / highlights recovery from an edge-aware (guided, log-luminance) base: no halos. Slider convention follows brightness: shadows +100 lifts darks, highlights -100 recovers brights. |
| `scene_curve`<br><sub>spatial</sub> | `points`=[[-8.0, -8.0], [0.0, 0.0], [4.0, 4.0]], `red`=null, `green`=null, `blue`=null, `mode`=luma (luma/rgb), `strength`=1.0 | Tone curve on scene-linear data in log2-EV space (x, y = EV relative to middle grey); applied as a per-pixel gain so hue is preserved in 'luma' mode. Identity continues with slope 1 outside the points. |
| `tone_equalizer`<br><sub>spatial</sub> | `bands`=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0…, `smoothing`=0.25, `norm`=rms (rms/luminance/max/geomean/power), `smoothing_diameter`=5.0, `edge_ev`=0.5, `diffusion`=1, `exposure_comp`=0.0, `contrast_comp`=1.0, `auto_center`=false, `preserve_grey`=false | darktable-style tone equalizer: guided-filter log2-EV luminance mask -> 9-band (-8..0 EV) gain curve -> per-pixel linear gain on all channels (hue preserving). bands are EV gains at -8,-7,...,0 EV. |

#### Stage 13: color_grading (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `channel_mixer` | `matrix`=[[1, 0, 0], [0, 1, 0], [0, 0, 1]], `normalize`=false, `strength`=1.0 | 3x3 channel mixer: out_c = sum_j matrix[c][j] * in_j. normalize=True divides each row by its sum so neutral greys are unchanged. |
| `color_balance` | `shadows`=[0, 0, 0], `midtones`=[0, 0, 0], `highlights`=[0, 0, 0], `global`=[0, 0, 0], `slope`=[1.0, 1.0, 1.0], `offset`=[0.0, 0.0, 0.0], `power`=[1.0, 1.0, 1.0], `power_pivot`=1.0, `contrast`=0.0, `contrast_fulcrum`=0.1845, `hue_shift`=0.0, `chroma`=0.0, `saturation`=0.0, `vibrance`=0.0, `brilliance`=0.0, `brilliance_shadows`=0.0, `brilliance_highlights`=0.0, `protect_skin`=0.5, `mask_falloff`=1.2 | Scene-referred colour balance (darktable colour balance rgb design): 4-way tints (shadows ~ lift, mids ~ gamma, highlights ~ gain, global ~ offset/white balance) as [hue, chroma 0..100, luma stops*100], ASC CDL slope/offset/power, contrast about a grey fulcrum, linear chroma and Oklch saturation / vibrance / brilliance. |
| `color_zones` | `selector`=hue (hue/lightness/chroma), `lightness`=null, `chroma`=null, `hue_shift`=null, `mix`=1.0, `gate`=0.02 | Selective colour by curves (darktable colour zones): the x axis is hue (HSV degrees, periodic), lightness or chroma; curves give lightness delta (-1..1 x 35 %), chroma delta (-1..1 -> x0..x2) and hue delta (degrees), as lists of [x, value]. |
| `hsl_mixer` | `gate`=0.02, `softness`=1.0, `red_hue`=0.0, `red_sat`=0.0, `red_lum`=0.0, `orange_hue`=0.0, `orange_sat`=0.0, `orange_lum`=0.0, `yellow_hue`=0.0, `yellow_sat`=0.0, `yellow_lum`=0.0, `green_hue`=0.0, `green_sat`=0.0, `green_lum`=0.0, `aqua_hue`=0.0, `aqua_sat`=0.0, `aqua_lum`=0.0, `blue_hue`=0.0, `blue_sat`=0.0, `blue_lum`=0.0, `purple_hue`=0.0, `purple_sat`=0.0, `purple_lum`=0.0, `magenta_hue`=0.0, `magenta_sat`=0.0, `magenta_lum`=0.0 | 8-band HSL mixer in Oklch (red, orange, yellow, green, aqua, blue, purple, magenta): <band>_hue (+-100 = +-30 deg), <band>_sat (+-100 = x0..x2 chroma), <band>_lum (+-100 = +-35 % Oklab L). Neutrals are gated. |
| `monochrome` | `filter`=neutral (neutral/red/orange/yellow/green/blue/panchromatic/orthochromatic/infrared), `weights`=null, `filter_strength`=1.0, `amount`=1.0, `tint`=null | Black & white by channel weights on linear data: `weights` [r,g,b] or a named filter (neutral, red, orange, yellow, green, blue, panchromatic, orthochromatic, infrared). Weights are normalised to sum 1 (grey stays grey); filter_strength blends the filter with the neutral luminance weights. |
| `saturation` | `amount`=0.0, `gate`=0.02, `mode`=oklch (oklch/linear) | Saturation (-100..100). 'oklch': perceptual chroma scaling at constant Oklab lightness (neutrals gated); 'linear': CDL-style scaling about the luminance axis. |
| `vibrance` | `amount`=0.0, `protect_skin`=0.5 | Vibrance (-100..100): chroma boost that fades with existing chroma and spares skin hues. |
| `contrast` | `contrast`=0.0, `fulcrum`=0.1845, `hue_preserve`=true | Contrast as a luminance power law around the fulcrum (default 18.45 % grey): Y' = f (Y/f)^(1+c). |

#### Stage 14: local_contrast (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `deconvolve`<br><sub>spatial</sub> | `radius`=1.0, `iterations`=12, `amount`=1.0, `damping`=1.0, `threshold`=2.0 | Richardson-Lucy deconvolution (lens deblur) with Gaussian PSF sigma = radius (px at 2000 px long edge), damped by the sensor noise model so noise is not amplified. |
| `sharpen`<br><sub>spatial</sub> | `amount`=0.5, `radius`=1.0, `threshold`=0.0, `masking`=0.3, `halo`=0.5 | Unsharp mask on perceptual luminance (hue preserving). radius in px at 2000 px long edge; amount 0..3; threshold in encoded units (0.01 ~ low noise); masking limits to midtones; halo clamps overshoot. |
| `local_contrast`<br><sub>spatial</sub> | `amount`=0.0, `clarity`=0.0, `texture`=0.0, `structure`=0.0, `method`=laplacian (laplacian/guided), `sigma_ev`=1.0, `protect_extremes`=0.0, `max_gain_ev`=2.5 | Halo-free local contrast on log2 luminance: local-Laplacian (default) or multiscale guided filter. amount = all scales; clarity = mid/coarse scales; texture = fine scales; structure = very coarse. |

#### Stage 15: optical_looks_slot_A (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `bloom`<br><sub>spatial</sub> | `threshold`=0.7, `knee`=0.5, `radius`=18.0, `strength`=0.25, `tint`=[1.0, 1.0, 1.0], `scales`=4 | Physically-motivated bloom (linear, slot A): multi-scale blurred highlights added back. |
| `halation`<br><sub>spatial</sub> | `threshold`=0.6, `knee`=0.4, `radius`=14.0, `strength`=0.5, `tint`=[1.0, 0.3, 0.08], `spread`=3.0 | Film halation (linear, slot A): threshold highlights, two-scale blur, red-orange tint, add back. |
| `lens_blur`<br><sub>spatial</sub> | `radius`=10.0, `blades`=0, `rotation`=0.0, `highlight_gain`=2.0, `threshold`=0.8, `radial`=0.0, `falloff`=1.5 | Disc / polygon-aperture lens blur in linear light (bokeh balls from highlights). radial>0 blurs mostly toward the corners (Holga-style); highlight_gain brightens clipped highlights so they form discs. |
| `spectral_mix`<br><sub>spatial</sub> | `mode`=mono (mono/matrix), `weights`=[0.0, 0.25, 0.75], `matrix`=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0…, `mix`=1.0 | Spectral channel mixing in scene-linear light (slot A): mono weights (orthochromatic wet plate 0/0.25/0.75, red-filter zone-system 0.7/0.35/-0.05, IR synth 0.65/0.35/0) or an arbitrary 3x3 matrix. |
| `background_blur`<br><sub>spatial</sub> | `radius`=18.0, `source`=subject (subject/depth/mask), `focus`=0.2, `focus_range`=0.12, `falloff`=0.25, `foreground_blur`=true, `levels`=6, `highlight_boost`=0.3, `mask`=null, `edge_grow`=1.0 | Lens blur (disc bokeh) of the background: radius map from the subject mask (source=subject), a depth map (source=depth: pixels away from `focus` +- `focus_range` blur up to `radius`) or an explicit mask spec (source=mask). |

#### Stage 16: tone_mapper (linear)

| op | params = default (choices) | what it does |
|---|---|---|
| `tone_map` | `curve`=auto (auto/sigmoid/filmic/reinhard/aces/none), `contrast`=1.6, `skew`=0.0, `hue_preserve`=1.0, `norm`=max (max/luminance/power/euclidean), `white`=1.0, `black`=0.0, `white_ev`=4.0, `black_ev`=-8.0, `latitude`=0.3, `balance`=0.0, `highlight_desat`=0.6 | Scene->display tone mapper: sigmoid (default for raw), filmic, reinhard, aces; 'none' (default for JPEG: soft shoulder only, never a second S-curve). Chroma-preserving norm with hue_preserve blend. |

#### Stage 18: look_slot_B (display)

| op | params = default (choices) | what it does |
|---|---|---|
| `blade_runner` | `intensity`=1.0, `seed`=0, `variant`=orange (orange/la), `mono_mix`=0.6, `fog`=0.25, `gain`=2.5 | Orange-haze dystopia (Blade Runner 2049-style): near-monochrome orange, fog veil, bloom, heavy contrast; 'la' variant keeps magenta/cyan neon over teal-black shadows. |
| `bleach_bypass` | `intensity`=1.0, `seed`=0, `desat`=0.45, `overlay`=0.5, `grain`=0.03 | Bleach bypass: saturation cut by 35-59% with a luminosity S-curve and an overlaid B&W layer; silvery, gritty highlights. |
| `day_for_night` | `intensity`=1.0, `seed`=0, `ev`=-2.2, `purkinje`=0.6, `saturation`=0.5, `gain`=2.0, `grain`=0.03 | Day-for-night: -2.5 EV in linear light, Purkinje blue-green B&W mix, deep-blue tint, desaturation, bright practicals keep warm colour, blue sky gradient. |
| `fincher` | `intensity`=1.0, `seed`=0, `ev`=-0.7, `saturation`=0.75, `gain`=5.0 | Green-yellow low-key grade (Fincher-style): -0.7 EV, olive mid-tones, teal-black shadows, muted saturation, contrast, fine sharpening and grain. |
| `matrix_green` | `intensity`=1.0, `seed`=0, `saturation`=0.55, `gain`=5.0, `cast`=0.08 | Green-tinted digital-rain grade: desaturated, green cast, deep green shadows and pale green highlights, cool skin, high contrast. |
| `print_film` | `intensity`=1.0, `seed`=0, `gain`=6.0, `saturation`=1.08 | Print-film (2383-style) procedural look: dense cyan-leaning shadows, soft S-curve, warm highlights, slightly reduced blue luminance. For the real LUT download Kodak 2383 / Fuji 3510 LUTs and use lut3d. |
| `teal_orange` | `intensity`=1.0, `seed`=0, `amount`=0.35, `skin_protect`=0.7, `gain`=4.0, `chroma_boost`=1.0, `shadow_hue`=195.0, `highlight_hue`=58.0, `shadow_color`=#1B5E6B, `highlight_color`=#F2A65A | Teal & orange grade: shadows pushed toward teal, highlights toward orange in Oklch (angular hue lerp), skin protected, S-curve contrast. |
| `technicolor_3strip` | `intensity`=1.0, `seed`=0, `saturation`=1.3, `gain`=6.0, `halation`=0.15, `soften`=0.7 | Three-strip Technicolor: dye-separation matrix, hyper-saturated CMY-style primaries, crushed cyan-tinted blacks, S-curve, mild halation and softening. |
| `wes_anderson` | `intensity`=1.0, `seed`=0, `palette`=GrandBudapest2 (Zissou1/GrandBudapest1/GrandBudapest2/Moonrise1/Moonrise2/Moonrise3/Darjeeling1/Darjeeling2/FantasticFox1/Royal1/Royal2/Rushmore1/BottleRocket1/BottleRocket2/Chevalier1/Cavalcanti1/IsleofDogs1/IsleofDogs2/FrenchDispatch/AsteroidCity1/AsteroidCity2/AsteroidCity3/Zissou1Continuous), `k`=6, `blend`=0.45, `flat`=0.0, `soften_edges`=8.0, `gain`=3.0, `black`=0.06, `saturation`=0.9, `warm`=0.2, `grain`=0.015 | Pastel palette remap: k-means clusters mapped to the nearest colour of an exact `wesanderson` palette (MIT-licensed R package hex values), blended 55/45 with the original, lowered contrast, lifted blacks, warm. |
| `color_grade` | `lift`=[0, 0, 0], `gamma`=[0, 0, 0], `gain`=[0, 0, 0], `offset`=[0, 0, 0], `contrast`=0.0, `pivot`=0.435, `saturation`=0.0, `strength`=1.0 | Lift/gamma/gain/offset colour wheels ([hue, chroma 0..100, luma -100..100]) on display data, DaVinci-style: out = (in*gain + lift*(1-in) + offset)^(1/gamma), then S-contrast about `pivot` and Oklch saturation. |
| `invert` | `amount`=1.0 | Colour negative: out = 1 - in (amount blends). |
| `lut3d` | `path`=null, `strength`=1.0, `interp`=tetrahedral (tetrahedral/trilinear) | Apply a .cube (1D/3D) or Hald CLUT PNG/TIFF to display-referred data with tetrahedral interpolation. |
| `split_tone` | `shadow_hue`=220.0, `shadow_sat`=0.0, `highlight_hue`=40.0, `highlight_sat`=0.0, `balance`=0.0, `softness`=0.35, `protect_ends`=0.5, `strength`=1.0 | Split toning in Oklab: tint shadows and highlights (hue = HSV degrees, sat 0..100) with a luminance split controlled by balance (-100..100) and softness; pure black / white are protected. |
| `temperature` | `temperature`=0.0, `tint`=0.0 | Creative warm/cool (+-100, +warm) and tint (+-100, +magenta) shift: chromatic adaptation applied to display values through linear sRGB (about +-60 mired at the extremes). |
| `film_emulation` | `stock`=portra_400 (portra_160/portra_400/portra_800/ektar_100/kodachrome_64/gold_200/tri_x_400/tri_x_pushed/hp5_plus/velvia_50/provia_100f/pro_400h/superia_400/classic_chrome/cinestill_800t/polaroid_600/instax/expired/cross_process), `strength`=1.0, `intensity`=1.0, `saturation`=1.0, `contrast`=1.0, `warmth`=0.0, `grain`=1.0, `halation`=-1.0, `fade`=0.0, `seed`=0 | Parametric film-stock emulation (per-channel curves, colour shifts, saturation, grain, halation, fade). Generic emulation of Kodak / Fujifilm / Ilford / Polaroid / Cinestill looks; brand names belong to their owners. Overrides: saturation/contrast multiply the table, warmth/fade add, grain scales grain, halation>=0 sets glow strength. |
| `duotone` | `shadow`=#1B1464, `highlight`=#FF2D55, `mid`=null, `mid_pos`=0.5, `contrast`=3.0, `space`=oklab (oklab/srgb), `amount`=1.0, `invert`=false | Duotone / tritone gradient map of luminance (piecewise-linear in Oklab by default). |
| `gradient_map` | `stops`=["#000000", "#FFFFFF"], `contrast`=0.0, `space`=oklab (oklab/srgb), `source`=luma (luma/max/oklab_l), `amount`=1.0, `invert`=false, `preserve_luma`=0.0 | Generic gradient map: stops are colours (evenly spaced) or [position, colour] pairs. |
| `adams_zone` | `intensity`=1.0, `seed`=0, `filter`=red (red/orange/yellow/neutral), `sky_burn`=0.35, `local`=0.4, `clahe`=0.4, `gain`=4.5, `black`=0.02, `white`=0.98, `grain`=0.015 | Zone-system B&W (Adams-style): red-filter mix in linear light, full tonal scale with textured Zone I/IX, local contrast, sky burn, fine grain; keeps Zone I texture (no pure black). |
| `crewdson` | `intensity`=1.0, `seed`=0, `fog`=0.1, `saturation`=0.8, `gain`=3.0 | Cinematic twilight tableau (Crewdson-style): blue ambient shadows, warm tungsten practicals, muted greens, lifted blacks, fog and bloom. |
| `eggleston` | `intensity`=1.0, `seed`=0, `saturation`=1.25, `gain`=4.5, `warm`=0.3, `grain`=0.01 | Dye-transfer colour (Eggleston-style): dense saturated reds and greens, warm neutrals, strong tonal density, almost no grain. |
| `fan_ho` | `intensity`=1.0, `seed`=0, `gain`=9.0, `rays`=0.5, `haze`=0.08, `ray_threshold`=0.85, `samples`=36 | Hard-light Hong Kong B&W (Fan Ho-style): deep shadows, S-curve 9, haze and radial light rays from the brightest highlight, warm-neutral tone. |
| `leiter` | `intensity`=1.0, `seed`=0, `haze`=0.04, `grain`=0.03 | Colour street look (Saul Leiter-style): muted overall, warm reds and yellows kept rich, lifted midtones and blacks, green-cyan shadows / amber highlights, soft haze and colour grain. |
| `lik_hdr` | `intensity`=1.0, `seed`=0, `compress`=0.65, `detail`=1.5, `clarity`=0.7, `saturation`=1.4 | Dramatic HDR landscape: base/detail local tone-mapping, strong clarity, vivid saturation with warm sunset hues, deep blues and a vignette. |
| `maier` | `intensity`=1.0, `seed`=0, `gain`=4.0, `grain`=0.025 | Rolleiflex-era street B&W (Maier-style): long smooth mid-tones, moderate contrast, fine grain, mild warm paper tone. |
| `mccurry` | `intensity`=1.0, `seed`=0, `gain`=7.0, `grain`=0.012, `sharpen`=0.6 | Kodachrome-style travel colour: hard S-curve with hard highlight clip, blue-tinted shadows, hot reds, deep true blues, crisp micro-contrast, fine grain. |
| `moriyama` | `intensity`=1.0, `seed`=0, `gain`=10.0, `grain`=0.16, `blur_len`=14.0, `defocus`=3.0, `tilt`=3.0, `dust`=1.0 | Are-bure-boke (Moriyama / Provoke-style): crushed high-contrast B&W, huge clumpy grain, random motion blur and defocus, tilt, vignette and dust. |
| `salgado` | `intensity`=1.0, `seed`=0, `gain`=8.0, `clahe`=0.6, `sky_burn`=0.45, `grain`=0.07 | Dramatic documentary B&W (Salgado-style): high contrast, heavy local contrast (CLAHE), burned skies, edge burn, rich pushed-film grain, warm-neutral print tone. |
| `sugimoto_sea` | `intensity`=1.0, `seed`=0, `water_blur`=0.04, `sky_blur`=1.5, `gain`=3.0, `black`=0.06, `white`=0.92, `horizon`=-1.0 | Long-exposure seascape (Sugimoto-style): horizon split, smoothed water, low-contrast B&W with lifted blacks and capped highlights, warm-neutral tone. |
| `aerochrome` | `style`=aerochrome (aerochrome/channel_swap), `foliage`=0.5, `sky`=0.5, `saturation`=1.3, `grain`=0.03, `intensity`=1.0, `seed`=0 | Infrared false colour: Aerochrome mapping (IR->red, red->green, green->blue; foliage turns pink-red, sky cyan-blue) or the classic R<->B channel swap. |
| `cross_process` | `variant`=e6_in_c41 (e6_in_c41/c41_in_e6), `saturation`=1.35, `gain`=7.0, `grain`=0.04, `intensity`=1.0, `seed`=0 | Cross-processing: per-channel curves (E6 slide in C41 chemistry: yellow-green highlights, cyan-blue shadows, high contrast and saturation). |
| `cyanotype` | `shadow`=#0B2545, `mid`=#1D5B8C, `paper`=#F4F0E6, `mid_pos`=0.55, `gain`=3.0, `paper_texture`=0.05, `blur`=0.6, `brush_border`=0.0, `intensity`=1.0, `seed`=0 | Cyanotype: luminance gradient map Prussian-blue -> paper white (soft printed-on-paper curve), paper tooth, optional brushed border. |
| `daguerreotype` | `mix`=[0.1, 0.4, 0.5], `sharpen`=1.0, `black`=0.12, `tarnish`=0.3, `scratches`=5, `vignette`=-0.7, `mirror`=false, `intensity`=1.0, `seed`=0 | Daguerreotype: blue-weighted mono with extreme detail, mirror-silver tonality, violet/bronze edge tarnish, oval vignette, fine scratches. |
| `infrared_bw` | `foliage`=0.5, `sky`=0.5, `glow`=0.4, `glow_radius`=2.0, `gain`=5.0, `grain`=0.04, `skin_smooth`=0.5, `intensity`=1.0, `seed`=0 | Infrared B&W (Wood effect): synthetic IR luminance (foliage glows white, blue sky goes black), halation-like glow, HIE-like grain, waxy skin. (Linear channel mix computed from the decoded image.) |
| `lith` | `shadow`=#2A1A14, `highlight`=#F2C9A0, `grain`=0.09, `grain_size`=1.6, `warmth`=0.1, `intensity`=1.0, `seed`=0 | Lith print: soft peach highlights, hard cold shadows (steep toe, compressed shoulder), infectious-development grain concentrated in shadows, split tone. |
| `lomo` | `holga`=false, `saturation`=1.3, `gain`=7.0, `vignette`=-3.0, `ca`=0.004, `leak`=0.3, `cross`=0.0, `grain`=0.03, `intensity`=1.0, `seed`=0 | Toy camera (Lomo / Holga look): strong vignette, saturated punchy colour, blue shadows / warm highlights, lateral chromatic aberration, light leaks; holga=True adds corner defocus. |
| `platinum` | `gain`=2.5, `black`=0.08, `white`=0.95, `stops`=["#1C1A17", "#5A4D40", "#EFE9DD"], `grain`=0.012, `paper_texture`=0.04, `intensity`=1.0, `seed`=0 | Platinum/palladium print: very long tonal scale, matte warm-neutral gradient map, fine grain, paper texture. |
| `sepia` | `style`=classic (classic/albumen), `strength`=0.9, `albumen`=["#2B1A0E", "#8B5A2B", "#F2E6C9"], `gain`=3.5, `black`=0.08, `vignette`=-0.3, `grain`=0.012, `intensity`=1.0, `seed`=0 | Sepia: the classic W3C sepia matrix, or an albumen-print gradient map with yellowed highlights, lifted blacks, vignette, paper grain. |
| `solarize` | `threshold`=0.62, `mix`=0.85, `mackie`=0.3, `gain`=4.0, `mode`=rgb (rgb/luma/bw), `intensity`=1.0 | Solarisation / Sabattier: fold the tone curve above a threshold (v -> 2t - v), Mackie-line edge halos, then S-curve. |
| `thermal` | `ramp`=ironbow (ironbow/white_hot/black_hot/rainbow), `smooth`=2.5, `res`=320, `legend`=true, `crosshair`=false, `gain`=1.5 | Thermal camera false colour: smoothed lightness at low sensor resolution mapped through an ironbow (or white/black-hot, rainbow) ramp, with an optional temperature bar and crosshair. |
| `wet_plate` | `spectral_mix`=true, `weights`=[0.0, 0.25, 0.75], `gain`=7.0, `black`=0.05, `white`=0.85, `tone`=classic (classic/ruby_gold), `pour_marks`=0.2, `fog`=0.12, `defocus`=3.0, `vignette`=-1.4, `dust`=1.0, `intensity`=1.0, `seed`=0 | Wet-plate collodion tintype / ambrotype: blue-only spectral sensitivity (reds go dark, blue eyes light), short tonal scale, japanned-plate blacks, pour marks, chemical edge fog, edge defocus, dust. spectral_mix=False if a slot-A `spectral_mix` precedes it. |
| `xray` | `mode`=xray (xray/negative/negative_bw), `gain`=4.0, `glow`=0.8, `vignette`=-0.5, `stops`=["#0A1A2A", "#6FA3C7", "#FFFFFF"] | X-ray (blue-white negative with edge glow) or plain colour / B&W negative. |
| `eye_enhance`<br><sub>spatial</sub> | `amount`=0.5, `contrast`=0.35, `saturation`=0.25, `sclera`=0.25, `sharpen`=0.5 | Eye pop: local contrast + sharpening and saturation on the iris/eye region, brighten and de-redden the sclera. |
| `skin_smooth`<br><sub>spatial</sub> | `amount`=0.5, `radius`=6.0, `texture`=0.9, `smooth_eps`=0.03, `use_ai_mask`=true, `even_tone`=0.3 | Frequency separation limited to the AI skin mask: the low layer is edge-aware smoothed (guided filter), the high layer (pores, texture) is kept at `texture` strength, so skin looks smooth but not plastic. |
| `sky_enhance`<br><sub>spatial</sub> | `darken`=0.18, `saturation`=0.3, `clouds`=0.35, `gradient`=0.6, `cloud_radius`=18.0 | Inside the AI sky mask: graduated darkening (stronger toward the top), blue saturation, and cloud clarity (local contrast on lightness). |
| `teeth_whiten`<br><sub>spatial</sub> | `amount`=0.5, `lift`=0.04, `hue_range`=[55.0, 80.0, 110.0, 135.0] | Inside the teeth mask: reduce chroma of the yellow band (Oklch hue ~60-130 deg) and lift lightness. |
| `levels` | `black`=0.0, `white`=1.0, `gamma`=1.0, `out_black`=0.0, `out_white`=1.0, `auto`=false, `auto_clip`=0.001, `per_channel`=false | Levels: input black/white points, midtone gamma (>1 brightens), output black/white; optional auto-levels. |
| `tone_curve` | `points`=[[0.0, 0.0], [1.0, 1.0]], `red`=null, `green`=null, `blue`=null, `mode`=rgb (rgb/luma/film/oklab), `strength`=1.0 | Monotone cubic (PCHIP) tone curve on display values: rgb, luma (ratio-preserving), film-like or Oklab L. |

#### Stage 19: effects (display)

| op | params = default (choices) | what it does |
|---|---|---|
| `chromatic_shift`<br><sub>spatial</sub> | `mode`=lateral (lateral/offset), `amount`=0.004, `red_dx`=2.0, `red_dy`=0.0, `blue_dx`=-2.0, `blue_dy`=0.0 | RGB misregistration: lateral CA (R scaled up / B down about the centre) or per-channel pixel offsets. |
| `dust_scratches`<br><sub>spatial</sub> | `scratches`=6, `dust`=120, `hairs`=2, `strength`=0.5, `light`=true, `dark_fraction`=0.3, `seed`=0 | Film / plate damage: long vertical scratches, dust specks and a few hairs (white by default, some dark). |
| `fog_haze`<br><sub>spatial</sub> | `amount`=0.2, `color`=#DDE3E8, `shadow_lift`=0.5, `gradient`=0.0 | Atmospheric haze: veil of `color`, stronger in the shadows (lifts blacks); gradient>0 thickens toward the bottom. |
| `glow`<br><sub>spatial</sub> | `threshold`=0.6, `radius`=24.0, `strength`=0.5, `tint`=[1.0, 1.0, 1.0], `softness`=0.5 | Display-referred glow / pro-mist: blurred highlights screened over the image. |
| `grain`<br><sub>spatial</sub> | `amount`=0.03, `size`=1.0, `roughness`=0.4, `chroma`=0.0, `midtone_bias`=0.6, `shadow_bias`=0.0, `clump`=0.0, `seed`=0 | Luminance-dependent film grain (strongest in midtones). amount = sigma in display units, size in px@2000. |
| `light_leak`<br><sub>spatial</sub> | `count`=3, `strength`=0.45, `size`=0.2, `style`=blob (blob/streak/burn), `colors`=["#FFB347", "#FF5E3A", "#FFD27A"], `edge_bias`=0.8, `seed`=0 | Light leaks: large warm anisotropic gaussian blobs (or streaks / edge burns) screened onto the frame. |
| `noise`<br><sub>spatial</sub> | `kind`=gaussian (gaussian/uniform/salt_pepper/banding/perlin), `amount`=0.05, `chroma`=0.5, `size`=0.0, `density`=0.01, `seed`=0 | Additive sensor-style noise: gaussian, uniform, salt & pepper, horizontal banding or smooth perlin-like. |
| `orton`<br><sub>spatial</sub> | `amount`=0.35, `radius`=28.0, `brightness`=0.0, `contrast`=1.0, `protect_highlights`=0.6 | Orton effect: (screen + multiply) duplicate blurred and blended in; highlight protection avoids dark halos. |
| `soft_focus`<br><sub>spatial</sub> | `amount`=0.5, `radius`=10.0, `halo`=0.3, `contrast_drop`=0.1 | Soft-focus lens: sharp core kept, blurred copy mixed in, highlights bleed (halo), slight contrast drop. |
| `tilt_shift`<br><sub>spatial</sub> | `focus_y`=0.5, `focus_height`=0.18, `angle`=0.0, `blur`=14.0, `falloff`=0.35, `saturation`=1.2, `contrast`=1.1 | Miniature-model look: blur grows with distance from a (rotatable) in-focus band; extra saturation / contrast. |
| `vignette`<br><sub>spatial</sub> | `amount`=-0.5, `midpoint`=0.5, `roundness`=0.3, `feather`=0.6, `highlights`=0.2, `center_x`=0.5, `center_y`=0.5 | Creative vignette in stops (negative darkens corners). midpoint = where falloff starts, feather = softness, roundness 0 follows the frame / 1 circular, highlights protects bright areas. |

#### Stage 20: raster_art_slot_C (display)

| op | params = default (choices) | what it does |
|---|---|---|
| `banksy_stencil`<br><sub>spatial</sub> | `levels`=2, `threshold`=-1.0, `blur`=1.0, `clean`=3.0, `min_area`=0.0003, `overspray`=9.0, `overspray_strength`=0.3, `wall`=#CFC9BC, `ink`=#121212, `grey`=#6E6E6E, `wall_texture`=0.4, `noise`=0.03, `accent`=false, `accent_sat`=1.3, `accent_min_area`=0.02, `seed`=0 | Stencil-graffiti: Otsu / fixed threshold (2 or 3 tones), stencil bridges cleaned, spray overspray halo, concrete-wall texture, optional single saturated accent colour. |
| `border`<br><sub>spatial</sub> | `style`=solid (solid/polaroid/instax/rough/rounded/double), `width`=60.0, `color`=#F6F1E4, `fit`=shrink (shrink/overlay), `seed`=0, `inner_color`=#1A1A1A | Frame / border (size-preserving: the picture is shrunk into the frame or painted over its edge). |
| `cel_toon`<br><sub>spatial</sub> | `levels`=5, `soft`=0.03, `saturation`=1.2, `edge_block`=9, `edge_c`=2.0, `edge_method`=adaptive (adaptive/xdog), `ink`=#1A1A1A, `passes`=3, `seed`=0 | Cel / toon shading: half-res bilateral x3, lightness quantised to a few soft steps, saturated colour, ink outlines. |
| `fairey_hope`<br><sub>spatial</sub> | `thresholds`=[0.25, 0.5, 0.75], `palette`=["#0B3A5D", "#D32A27", "#7098C6", "#F2E…, `split`=diagonal (none/diagonal/hue), `split_angle`=-30.0, `split_pos`=0.5, `equalize`=true, `caption_band`=false, `caption_text`=, `smooth`=1.0 | Four-tone stencil poster: navy / red / light blue / cream. Optional split swaps the two middle colours on one side of a diagonal (or on warm hues) for the two-colour-face look. Flat fills, no texture. |
| `gameboy`<br><sub>spatial</sub> | `cells`=128, `gain`=5.0, `dither`=bayer4 (none/bayer2/bayer4), `palette`=["#0F380F", "#306230", "#8BAC0F", "#9BB…, `frame`=false | Game Boy Camera: ~128-column downscale, B&W with contrast gain, 4x4 (or 2x2) Bayer ordered dither to the 4-shade DMG green ramp, integer upscale. |
| `glitch`<br><sub>spatial</sub> | `bands`=12, `shift`=0.06, `channel_shift`=8.0, `jpeg_quality`=12, `corrupt`=8, `sort_bands`=2, `scan`=0.15, `noise_bands`=2, `seed`=0 | Glitch / datamosh: row-band displacement, RGB channel misalignment, low-quality JPEG with macroblock corruption, pixel sorting in some bands, scanline dimming and saturated noise bands. |
| `halftone`<br><sub>spatial</sub> | `mode`=bw (bw/color/cmyk), `pitch`=12.0, `angle`=45.0, `gain`=1.0, `ink`=#14110F, `paper`=#F6F1E4, `cmyk_angles`=[15.0, 75.0, 0.0, 45.0], `inks`=["#00A3E0", "#E6007E", "#FFE600", "#141…, `aa`=1.0 | Ben-Day / halftone dot screens: bw (one rotated screen), cmyk (four screens at 15/75/0/45) or color (RGB-complement CMY). Dot size follows the tone (variable-size AM halftone). |
| `haring`<br><sub>spatial</sub> | `k`=4, `palette`=["#E6332A", "#FFD500", "#2C79C5", "#3CB…, `outline`=9.0, `background`=#FFFFFF, `radiating`=true, `n_marks`=10, `mark_len`=0.04, `smooth`=2, `seed`=0 | Bold-outline flat colour (Haring-style): 3-4 flat clusters snapped to a primary palette, background cluster white, thick uniform black outline, short radiating motion lines from strong corners. |
| `ink_xdog`<br><sub>spatial</sub> | `style`=ink (ink/pencil/charcoal/woodcut/pastel), `sigma`=0.0, `sharpness`=0.0, `epsilon`=0.0, `phi`=0.0, `flow`=-1.0, `shade`=-1.0, `colour`=0.0, `ink`=, `paper`=, `seed`=0 | Line art / drawing by XDoG (Winnemoeller 2012) with the paper's named looks: ink, pencil, charcoal, woodcut, pastel. Params left at their 0/-1/'' defaults take the style table values; flow>0 smooths along the structure tensor. |
| `klimt`<br><sub>spatial</sub> | `segments`=260, `foreground_saturation`=0.9, `outline`=#3B2A12, `outline_width`=1.2, `gold`=["#8C6A1E", "#D4AF37", "#F4D160"], `motif_colors`=["#3B2A12", "#F4D160"], `sparkle`=0.25, `seed`=0 | Gold-leaf ornament (Klimt-style): saliency + GrabCut foreground stays photographic, background flattened with SLIC into gold tiles carrying spiral / circle / square motifs, metallic noise and specular glints. |
| `kmeans_quantize`<br><sub>spatial</sub> | `k`=6, `space`=oklab (oklab/srgb), `prefilter`=0.0, `palette`=[], `assign`=rank (rank/nearest), `smooth`=0.0, `seed`=0, `sample`=20000, `spatial`=0.0 | k-means colour quantisation (cv2.kmeans on an Oklab pixel sample). Flat cluster colours, or each cluster mapped onto a palette (rank = by lightness order, nearest = closest colour). |
| `letterbox`<br><sub>spatial</sub> | `aspect`=2.39, `color`=#000000, `feather`=0.0, `offset`=0.0 | Cinema letterbox / pillarbox bars painted over the frame for the target aspect (size-preserving). |
| `lichtenstein`<br><sub>spatial</sub> | `k`=5, `pitch`=14.0, `dot_radius`=0.32, `outline`=5.0, `outline_method`=canny (canny/adaptive), `dots_on`=["cream", "blue"], `angle`=0.0, `highlight`=0.95, `smooth`=2, `seed`=0 | Comic-print pop (Lichtenstein-style): bilateral + k-means -> primary-colour fills, uniform Ben-Day dots in the mid-tone fills, thick black outlines, white highlight slashes. |
| `low_poly`<br><sub>spatial</sub> | `points`=1500, `edge_points`=0.7, `seam`=0.15, `seam_color`=#111111, `saturation`=1.1, `seed`=0 | Low-poly / cubist triangulation: points sampled along Canny edges + uniform + border -> Delaunay -> each triangle filled with its mean colour; faint seams. |
| `mondrian`<br><sub>spatial</sub> | `palette`=["#F5F3EE", "#E52521", "#1A4C9C", "#FAD…, `min_cell`=0.06, `max_depth`=5, `variance`=0.06, `line`=14.0, `white_bias`=3.0, `black_cells`=0.0, `seed`=0 | Mondrian grid: image-adaptive quadtree (splits snap to the strongest edge near the middle), leaves coloured with the nearest of white / red / blue / yellow (white weighted), thick black dividing lines. |
| `monet`<br><sub>spatial</sub> | `style`=impressionist (impressionist/colorist/expressionist), `brush_scale`=1.0, `radii`=null, `threshold`=0.0, `alpha`=0.0, `init`=mean (mean/paper/white), `paper`=#F1E8D6, `impasto`=0.0, `swirl`=0.0, `work_edge`=1600, `max_strokes`=60000, `seed`=0 | Impressionist (Monet-style): lifted blacks, violet shadows, cream highlights, soft broken-colour dabs. |
| `mosaic`<br><sub>spatial</sub> | `shape`=slic (slic/grid), `tiles`=2500, `compactness`=30.0, `grout`=#2E2A26, `grout_width`=1.5, `tile_noise`=0.08, `bevel`=0.1, `palette_colors`=0, `seed`=0 | Mosaic of tesserae: SLIC superpixels (or a square grid) filled with mean colour, grout lines, per-tile lighting variation and a subtle bevel. |
| `oil_paint`<br><sub>spatial</sub> | `radius`=5.0, `levels`=24, `detail`=0.4, `canvas`=0.06, `saturation`=1.1, `seed`=0 | Oil painting: neighbourhood-mode (xphoto oilPainting algorithm) filter, brush-edge detail boost and a canvas weave. |
| `painterly`<br><sub>spatial</sub> | `style`=impressionist (impressionist/expressionist/colorist/pointillist), `brush_scale`=1.0, `radii`=null, `threshold`=0.0, `alpha`=0.0, `init`=mean (mean/paper/white), `paper`=#F1E8D6, `impasto`=0.0, `swirl`=0.0, `work_edge`=1600, `max_strokes`=60000, `seed`=0 | Hertzmann (1998) layered curved brush strokes with the published style tables (impressionist, expressionist, colorist wash, pointillist). Slow op (2-6 s at 1600 px); works at <= work_edge px. |
| `pencil_sketch`<br><sub>spatial</sub> | `method`=dodge (dodge/opencv), `sigma`=10.0, `colour`=0.0, `contrast`=1.2, `paper`=#F7F4EC, `graphite`=#2B2B2E, `hatch`=0.25, `grain`=0.04, `seed`=0 | Pencil sketch: colour-dodge of the grey against its blurred negative (fine graphite -> soft charcoal with sigma), XDoG hatching for shading, graphite grain on paper; colour>0 adds coloured-pencil tint. |
| `pixel_art`<br><sub>spatial</sub> | `cells`=128, `colors`=24, `saturation`=1.15, `dither`=none (none/bayer), `dither_amount`=0.5, `outline`=0.0, `seed`=0 | Pixel art: area-downscale to `cells` columns, k-means palette (Oklab) with optional ordered dither, optional dark outline, integer nearest-neighbour upscale. |
| `pointillism`<br><sub>spatial</sub> | `density`=0.015, `radius`=7.0, `canvas`=#F1E8D6, `hue_jitter`=0.02, `sat_jitter`=0.12, `val_jitter`=0.14, `complement`=0.12, `seed`=0 | Pointillism (Seurat-style): jittered-grid dots sampled from the image, HSV colour jitter, a fraction of dots shifted to the complementary hue (optical mixing), on a cream canvas. |
| `posterize`<br><sub>spatial</sub> | `levels`=4, `mode`=luma (luma/rgb/oklab), `palette`=[], `smooth`=0.0, `quantile_mix`=0.5 | Posterise to N levels (luma: flat tone bands, rgb: per-channel, oklab: lightness only). With a palette (dark->light) each band becomes a flat colour. quantile_mix blends equal-width and equal-population bands. |
| `risograph`<br><sub>spatial</sub> | `inks`=["#FF48B0", "#0078BF"], `paper`=#F3EFE4, `misregister`=4.0, `dot`=0.8, `spread`=0.6, `screen`=4.5, `grain_mix`=0.35, `opacity`=0.92, `grain`=0.03, `contrast`=1.1, `seed`=0 | Risograph: image split into 2-3 spot-ink densities (least squares in optical-density space), each stochastically screened, misregistered per layer, overprinted multiplicatively on paper, with ink spread and paper grain. |
| `scanlines`<br><sub>spatial</sub> | `period`=4.0, `strength`=0.25, `thickness`=0.5, `vertical`=false, `triad`=0.0, `softness`=0.6, `phase`=0.0 | CRT scanlines (and optional RGB aperture-grille triad). |
| `threshold`<br><sub>spatial</sub> | `level`=0.5, `otsu`=false, `softness`=0.0, `blur`=1.0, `fg`=#101010, `bg`=#F4F1EA, `invert`=false | Two-tone threshold (fixed level or Otsu) with optional soft edge; colours fg (dark) / bg (light). |
| `van_gogh`<br><sub>spatial</sub> | `style`=expressionist (expressionist/impressionist/colorist), `brush_scale`=1.0, `radii`=null, `threshold`=0.0, `alpha`=0.0, `init`=mean (mean/paper/white), `paper`=#F1E8D6, `impasto`=0.6, `swirl`=0.5, `work_edge`=1600, `max_strokes`=60000, `seed`=0 | Post-impressionist (Van Gogh-style): saturated blue/yellow push, expressionist directional strokes, impasto emboss and a LIC swirl texture in calm regions. Slow op (see painterly). |
| `vhs`<br><sub>spatial</sub> | `lines`=480, `chroma_bleed`=4.0, `saturation`=1.15, `gain`=4.0, `black`=0.06, `noise`=0.05, `tracking`=1.0, `comb`=1.0, `barrel`=0.04, `seed`=0 | VHS: 480-line resample, soft luma, 4x downsampled chroma with Cr/Cb shifted (colour bleed), crushed contrast, line noise, a tracking bar, interlace comb on edges and slight barrel distortion. |
| `warhol`<br><sub>spatial</sub> | `levels`=4, `mode`=luma (luma/kmeans), `palette`=p1, `grid`=1x1 (1x1/2x2/3x3/1x2/2x1/1x3/3x1), `ink_threshold`=0.28, `ink_offset`=[4.0, 3.0], `texture`=0.02, `kmeans_k`=5, `smooth`=60.0, `sigma_r`=0.4, `gutter`=0.012, `seed`=0 | Silkscreen-pop (Warhol-style): edge-preserving smooth, 3-5 flat tone bands -> flat palette fills, dark off-register ink layer; grid variants give each panel its own palette. |
| `watercolor`<br><sub>spatial</sub> | `levels`=7, `wet_edge`=0.35, `bleed`=6.0, `granulation`=0.12, `paper`=0.06, `saturation`=0.85, `lift`=0.1, `smooth`=60.0, `seed`=0 | Watercolour: edge-preserving wash, soft-quantised lightness, wet-edge darkening, noise-displaced colour bleed, pigment granulation, paper tooth, high-key lift. |
| `xdog`<br><sub>spatial</sub> | `sigma`=2.4, `k`=1.6, `sharpness`=20.0, `epsilon`=0.75, `phi`=1.0, `mode`=overlay (overlay/replace), `ink`=#101010, `paper`=#FFFFFF, `opacity`=1.0, `flow`=0.0 | XDoG ink layer (Winnemoeller 2012): S = (1+p)G_s - pG_ks, soft threshold tanh(phi(S-eps)). overlay multiplies the ink lines onto the image; replace outputs ink on paper. sigma in px@2000, phi on the 0..1 scale. |
| `paper_texture`<br><sub>spatial</sub> | `strength`=0.08, `scale`=1.0, `fibres`=0.3, `relief`=0.5, `tint`=#FFFFFF, `tint_amount`=0.0, `mode`=multiply (multiply/overlay), `seed`=0 | Procedural paper: tooth + mottling + fibres with embossed relief, multiplied (or overlaid) onto the image. |
| `gum_bichromate`<br><sub>spatial</sub> | `pigments`=["#1F6F8B", "#A3325F", "#E0B23A"], `misregister`=5.0, `saturation`=0.8, `gain`=2.5, `black`=0.1, `paper`=#F4EFE4, `paper_texture`=0.1, `brush_border`=40.0, `seed`=0 | Gum bichromate: three pigment layers (cyan / magenta / yellow separations), each smoothed, randomly misregistered, brush-edged, printed multiplicatively on heavily textured paper with lifted blacks. |

#### Stage 21: output (display)

| op | params = default (choices) | what it does |
|---|---|---|
| `output_sharpen`<br><sub>spatial</sub> | `medium`=screen (screen/web/print), `amount`=null, `radius`=null, `ppi`=null, `threshold`=0.0 | Post-resize sharpening on the final pixels: screen/web 0.5 px, print scales with ppi (0.8 px at 300). amount/radius override the medium defaults. Radius is in output pixels (no preview scaling). |
| `resize`<br><sub>changes geometry, spatial</sub> | `long_edge`=null, `short_edge`=null, `width`=null, `height`=null, `megapixels`=null, `scale`=null, `method`=auto (auto/lanczos/area/cubic/linear), `no_upscale`=false | Resize by long edge, short edge, width, height, megapixels or scale. method auto = area (down) / Lanczos (up). |
<!-- END GENERATED: ops -->
