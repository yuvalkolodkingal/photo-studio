# Shoot-type modes

A **mode** is a preset stack tuned for one kind of photo. It sits between the pipeline defaults and your own edits, so
it gives a sensible starting point that you then refine. There are 10 genre modes plus `auto` (detector) and `video`
(for stills pulled from video). The catalogue at the end is generated from `photostudio/presets/modes/`.

## Contents

- [Using a mode](#using-a-mode)
- [Auto-detection](#auto-detection)
- [Picture Style emulations](#picture-style-emulations)
- [Writing your own mode](#writing-your-own-mode)
- [Mode catalogue](#mode-catalogue)

## Using a mode

```bash
photo develop IMG.CR3 --mode portrait --preview 1600          # explicit
photo develop IMG.CR3 --mode night:astro --preview 1600        # mode:variant
photo develop IMG.CR3 --mode auto --preview 1600               # detect (see below)
photo develop IMG.CR3 --mode landscape --merge --version v2    # layer onto the existing stack in a new version
photo info IMG.CR3                                             # shows the detected mode without changing anything
```

- `--mode` accepts `name` or `name:variant`. Unknown names fail with the list of valid ones, but **an unknown variant
  silently falls back to the base preset** with no warning (`--mode night:nosuchvariant` runs plain `night`). Check the
  variant name against the catalogue below.
- `develop` without `--merge` **replaces** the stack (defaults + mode + art + `--set`), so earlier manual edits are
  dropped from the active version. It saves an `auto:before-develop` snapshot first, so
  `photo versions restore IMG auto:before-develop` brings them back.
- The sidecar stores the chosen `mode` per version, and the web editor and `photo ls` show it.
- Modes mix global colour and tone with **masked entries** (for example the landscape mode adds a masked sky
  gradient). Run `photo edit IMG` with no edits to see every entry, including the masked ones marked with their
  mask type, before you override a global value and wonder why the sky did not move.

## Auto-detection

`--mode auto` runs `modes.detect_mode(meta, image)` and then loads the detected mode with its variant. Trust order
(high to low):

1. Canon `EasyMode` (the SCN dial): landscape, night, portrait, sports, macro, food, HDR modes.
2. Picture Style: Portrait, Landscape and Monochrome map to portrait, landscape, street.
3. Drive and focus: burst plus AI Servo means sports; Servo plus a long lens means animal; macro tags; very long
   exposures at high ISO mean night or astro; face-detect AF or MakerNote face count means portrait or events.
4. Image content (only when the image is passed): dark histogram, detected faces, sky fraction, vertical edges.

The result carries `confidence` (0 to 1), `reasons` and runner-up `candidates`. Below confidence 0.30 the generic
`events` mode is used. Look at `photo info IMG` (or the `auto` block of `photo develop --json`) and read the
reasons: if they are weak, pass `--mode` explicitly. Two Canon MakerNote semantics are unverified on a real R50 file:
whether `EasyMode` is written for every SCN mode, and the unit of the focus-distance tag. The rules tolerate both,
but treat a high-confidence `EasyMode` match on real files as "probably right" until you have seen it work.

The in-camera composite SCN modes (HDR backlight control, handheld night scene) come out of the camera as finished
JPEGs. The detector adds a note saying so; keep edits gentle.

## Picture Style emulations

`presets/picture_styles/` holds approximations of the camera's Picture Styles (a few op entries each: saturation, a
luma curve, sharpen). They are **approximations from Canon's public descriptions**, not Canon's curves. They are
applied through `--art`, using the `style:` prefix (a bare name also works when no art preset has that name):

```bash
photo develop IMG.CR3 --art style:standard --preview 1600
photo develop IMG.CR3 --mode landscape --art style:landscape --merge --version canonlike
```

For a faithful match, fit your own from CR3+JPEG pairs. The fit is saved as a user preset named `fit_<name>`:

```bash
photo calibrate picture-style --dir /path/to/shots --style standard --name mine
photo develop IMG.CR3 --art fit_mine
```

`calibrate` needs real CR3 files that have companion camera JPEGs, so it was not run on synthetic data.

<!-- BEGIN GENERATED: picture-styles -->
| preset | variants | notes |
|---|---|---|
| `faithful` | - | APPROXIMATION of the camera's Picture Style, built from Canon's public descriptions (not Canon's actual curves, which are not published). Sharpness maps at about 0.1 sharpen-amount per Canon step (0-7). For a faithful match run: photo calibrate picture-style - |
| `landscape` | - | APPROXIMATION of the camera's Picture Style, built from Canon's public descriptions (not Canon's actual curves, which are not published). Sharpness maps at about 0.1 sharpen-amount per Canon step (0-7). For a faithful match run: photo calibrate picture-style - |
| `monochrome` | `green`, `orange`, `red`, `sepia`, `yellow` | APPROXIMATION of the camera's Picture Style, built from Canon's public descriptions (not Canon's actual curves, which are not published). Sharpness maps at about 0.1 sharpen-amount per Canon step (0-7). For a faithful match run: photo calibrate picture-style - |
| `neutral` | - | APPROXIMATION of the camera's Picture Style, built from Canon's public descriptions (not Canon's actual curves, which are not published). Sharpness maps at about 0.1 sharpen-amount per Canon step (0-7). For a faithful match run: photo calibrate picture-style - |
| `portrait` | - | APPROXIMATION of the camera's Picture Style, built from Canon's public descriptions (not Canon's actual curves, which are not published). Sharpness maps at about 0.1 sharpen-amount per Canon step (0-7). For a faithful match run: photo calibrate picture-style - |
| `standard` | - | APPROXIMATION of the camera's Picture Style, built from Canon's public descriptions (not Canon's actual curves, which are not published). Sharpness maps at about 0.1 sharpen-amount per Canon step (0-7). For a faithful match run: photo calibrate picture-style - |
<!-- END GENERATED: picture-styles -->

## Writing your own mode

Save the current stack of an image as a user preset, then reuse it by name:

```bash
photo presets save mode my_wedding --from IMG_0042.CR3 --description "soft, warm, skin-safe"
photo develop IMG_0100.CR3 --mode my_wedding
photo presets list modes
photo presets validate modes
```

User presets live in `~/.config/photostudio/presets/modes/<name>.yaml` and override built-ins of the same name.
`photo presets show mode portrait` prints a preset; `--paths` adds the file path. Presets are YAML:
`name, title, description, tags, stack: [entries], variants: {name: {stack: [...]}}`.

## Mode catalogue

<!-- BEGIN GENERATED: mode-catalogue -->
#### `animal` - Animal (wildlife / pets / birds)

The eye and fine texture decide the shot. Texture, not clarity (clarity makes crunchy halos on fur and feathers). Base (wildlife): highlights -25 / whites -10 (white feathers), shadows +18 (backlit fur), contrast +0.05, vibrance +10 and no saturation; local_contrast texture +0.20, clarity +0.03; capture_sharpen 0.8 (default) plus sharpen amount 0.75, radius 0.9, masking 0.5, so the background noise is not sharpened; denoise stays ISO-adaptive and is stronger at high ISO (luma 50 / chroma 70 base, iso_adapt). A second masked texture +0.15 / sharpen pass runs inside the AI animal mask (skipped when the model is missing), and the background gets texture -0.15, clarity -0.05 and exposure -0.15 EV for separation. Variants: pets (softer, warmer: sharpen 0.55, vibrance +8, shadows +22), birds (strongest sharpening 0.9 and texture +0.25; whites -15; blue/green saturation protected by hsl_mixer).

Base stack (8 entries): `highlights_shadows` (highlights=-25, whites=-10, shadows=18); `contrast` (contrast=0.05); `vibrance` (amount=10, protect_skin=0.3); `local_contrast` (texture=0.2, clarity=0.03); `sharpen` (amount=0.75, radius=0.9, masking=0.5, halo=0.5); `sharpen@subject [mask ai_animal]` (amount=0.35, radius=0.8, masking=0.3); `local_contrast@bg [mask ai_animal]` (texture=-0.15, clarity=-0.05); `exposure@bg_ev [mask ai_animal]` (ev=-0.15)

- variant `animal:birds`: `highlights_shadows` (highlights=-30, whites=-15, shadows=15); `local_contrast` (texture=0.25, clarity=0.03); `sharpen` (amount=0.9, radius=0.8, masking=0.55, halo=0.5); `hsl_mixer` (blue_sat=-4, green_sat=-4, green_lum=-3)
- variant `animal:pets`: `highlights_shadows` (highlights=-20, shadows=22); `vibrance` (amount=8); `sharpen` (amount=0.55, radius=0.9, masking=0.5); `local_contrast` (texture=0.15, clarity=0.0)
- variant `animal:wildlife`: 

#### `architecture` - Architecture / real estate

Geometry, dynamic range and neutral whites: bright and airy, verticals corrected. lens_correct (distortion, TCA, vignetting) on, ca_correct auto, perspective auto vertical (strength 0.9, mode crop) and straighten (horizon, max 6 deg); exposure +0.4, highlights -50 (windows), shadows +40, whites +10, blacks +5, contrast -0.02, clarity +0.05, texture +0.05, vibrance +5, saturation -5 (neutral walls), sharpen amount 0.40, radius 1.0, masking 0.2. WB: pick a grey/white wall (white_balance picker) for the dominant light; tungsten lamps need a local WB mask (not set here).

Base stack (11 entries): `lens_correct` (auto=true, distortion=true, tca=true, vignetting=true); `ca_correct` (auto=true); `straighten` (mode=horizon, max_angle=6.0, crop_mode=crop); `perspective` (auto=vertical, strength=0.9, mode=crop); `exposure` (ev=0.4); `highlights_shadows` (highlights=-50, shadows=40, whites=10, blacks=5); `contrast` (contrast=-0.02); `local_contrast` (clarity=0.05, texture=0.05); `vibrance` (amount=5); `saturation` (amount=-5); `sharpen` (amount=0.4, radius=1.0, masking=0.2)


#### `auto` - Auto (detect from EXIF and image content)

Placeholder mode. `photo develop --mode auto` runs `modes.detect_mode(meta, image)` (Canon EasyMode / Picture Style / drive + AI Servo / macro tags / exposure time / faces / sky fraction) and then loads the detected mode, with its variant where one was found, e.g. `night:astro`. When confidence is below the threshold the generic `events` mode is used. This file carries no ops of its own.

Base stack (0 entries): 


#### `events` - Events / family / travel

Batch-consistent, skin-safe default for snapshots and travel (also the fallback for `auto` with low confidence). highlights -20, shadows +15, contrast +0.05, vibrance +10, saturation 0, clarity 0, texture 0, sharpen amount 0.40, radius 1.0, masking 0.5, denoise ISO-adaptive. Exposure/WB matching across a set is a batch step (not in the preset). Variants. family: skin-first (vibrance protect_skin 0.7, texture -0.05, sharpen 0.35, orange saturation -2). travel: landscape-leaning for no-face frames (dehaze +0.05, clarity +0.08, vibrance +12, highlights -30, shadows +20, sharpen 0.45).

Base stack (4 entries): `highlights_shadows` (highlights=-20, shadows=15); `contrast` (contrast=0.05); `vibrance` (amount=10, protect_skin=0.6); `sharpen` (amount=0.4, radius=1.0, masking=0.5)

- variant `events:family`: `vibrance` (amount=8, protect_skin=0.7); `hsl_mixer` (orange_sat=-2); `local_contrast` (texture=-0.05); `sharpen` (amount=0.35)
- variant `events:travel`: `highlights_shadows` (highlights=-30, shadows=20); `dehaze` (strength=0.05); `vibrance` (amount=12); `local_contrast` (clarity=0.08); `sharpen` (amount=0.45)

#### `landscape` - Landscape

Masks do the work: protect the sky, open the land, keep greens believable (Canon Landscape style: sharpness 4, vivid blues and greens, which is rendered here with restraint). highlights -35, shadows +25, whites +8, blacks -8; contrast +0.10; dehaze +0.10 global (more only through the sky mask); local_contrast clarity +0.12 / texture +0.10; vibrance +12; hsl_mixer: greens hue -4 and luminance -6 and saturation -5 (no neon grass), blues saturation -6 and luminance -6 (deeper sky); sharpen amount 0.50, radius 0.9, masking 0.2 (low-ISO base, denoise stays minimal through iso_adapt). Sky: a linear gradient mask over the top 45 % of the frame with exposure -0.4 EV and a gentle dehaze +0.10 (hand-drawn gradient; the ai_sky mask is used by sky_enhance when its model is present). Optional variant `golden_hour` warms with a small split_tone; `overcast` adds contrast and vibrance.

Base stack (9 entries): `highlights_shadows` (highlights=-35, shadows=25, whites=8, blacks=-8); `contrast` (contrast=0.1); `dehaze` (strength=0.1); `vibrance` (amount=12); `hsl_mixer` (green_hue=-4, green_lum=-6, green_sat=-5, blue_sat=-6, blue_lum=-6); `local_contrast` (clarity=0.12, texture=0.1); `sharpen` (amount=0.5, radius=0.9, masking=0.2); `exposure@sky_grad [mask linear]` (ev=-0.4); `dehaze@sky_dehaze [mask linear]` (strength=0.1)

- variant `landscape:golden_hour`: `split_tone` (shadow_hue=230, shadow_sat=0.08, highlight_hue=45, highlight_sat=0.1, balance=0.1); `vibrance` (amount=8)
- variant `landscape:overcast`: `contrast` (contrast=0.15); `vibrance` (amount=18); `highlights_shadows` (highlights=-20, shadows=15)

#### `macro` - Macro

Diffraction and chromatic aberration management with accurate colour. ca_correct auto on (lateral CA) and defringe 40; capture_sharpen 0.9 and a small-radius sharpen (amount 0.70, radius 0.8, masking 0.3) to counter diffraction at f/8+ on APS-C; highlights -25 (petal / wing highlights), shadows +10, contrast +0.05; local_contrast texture +0.25, clarity +0.07; vibrance +5, saturation 0 (flowers saturate easily; the saturation op is Oklch hue-preserving). A masked background pass (ai_subject inverted) takes texture -0.20, clarity -0.10 and -0.2 EV to isolate the subject. No focus-stacking here (separate tool).

Base stack (10 entries): `ca_correct` (auto=true); `defringe` (amount=40); `capture_sharpen` (amount=0.9); `highlights_shadows` (highlights=-25, shadows=10); `contrast` (contrast=0.05); `vibrance` (amount=5); `local_contrast` (texture=0.25, clarity=0.07); `sharpen` (amount=0.7, radius=0.8, masking=0.3); `local_contrast@bg [mask ai_subject]` (texture=-0.2, clarity=-0.1); `exposure@bg_ev [mask ai_subject]` (ev=-0.2)


#### `night` - Night (astro / blue hour / moon)

Generic night base: exposure +0.5 EV lift with highlights -20 / shadows +10 / blacks -6, contrast +0.10, clarity +0.12 (kept under 0.5), vibrance +20, saturation 0, sharpen amount 0.25 with masking 0.8 (stars must not be sharpened), denoise ISO-adaptive and stronger (luma 60 / chroma 80). Heavy lifting (stacking, gradient removal) is an external workflow; this only sets the single-frame starting point. Variants. astro: temperature 5600 K relative to as-shot (a cooler rendering; Lonely Speck starts near 3900 K in Lightroom, whose scale differs), exposure +1.2 EV, contrast +0.20, clarity +0.2, dehaze +0.12, vibrance +30, lower sharpening 0.2, stronger luma noise 70. Tune WB by pushing vibrance and saturation temporarily, then reset (Lonely Speck method). blue_hour: highlights -40, shadows +30, vibrance +15, no dehaze, temperature 5000 K. moon: temperature 5200 K, exposure -0.3 EV, highlights -30, clarity +0.2, texture +0.30 (the disc), sharpen 0.6, denoise light.

Base stack (7 entries): `denoise` (luma=60, chroma=80, iso_adapt=true); `exposure` (ev=0.5); `highlights_shadows` (highlights=-20, shadows=10, blacks=-6); `contrast` (contrast=0.1); `vibrance` (amount=20); `local_contrast` (clarity=0.12); `sharpen` (amount=0.25, radius=1.0, masking=0.8)

- variant `night:astro`: `denoise` (luma=70, chroma=85); `exposure` (ev=1.2); `white_balance` (temperature=5600); `contrast` (contrast=0.2); `dehaze` (strength=0.12); `local_contrast` (clarity=0.2); `vibrance` (amount=30); `sharpen` (amount=0.2, masking=0.85)
- variant `night:blue_hour`: `exposure` (ev=0.2); `white_balance` (temperature=5000); `highlights_shadows` (highlights=-40, shadows=30); `vibrance` (amount=15)
- variant `night:moon`: `denoise` (luma=30, chroma=50); `exposure` (ev=-0.3); `white_balance` (temperature=5200); `highlights_shadows` (highlights=-30); `local_contrast` (clarity=0.2, texture=0.3); `sharpen` (amount=0.6, radius=0.9, masking=0.3)

#### `portrait` - Portrait

Skin-first, soft and conservative (Canon Portrait style: sharpness 2 vs 3, natural skin). exposure ev +0.15 (nudges faces towards L* 65-72; tune per shot); contrast -0.05 (about -10 vs Standard); highlights_shadows highlights -20 (forehead / hair speculars), shadows +10; vibrance +5 (protect_skin 0.7), saturation 0; local_contrast texture -0.10 and clarity 0 (never positive clarity on skin); sharpen amount 0.30, radius 1.0, masking 0.7 so skin is barely sharpened (Canon sharpness 2 ~ 25-35 on a 0-150 scale); hsl_mixer orange saturation -3 and luminance +3 (skin band) and a gentle teeth/eyes pass. skin_smooth runs only inside the AI skin mask at 0.35 (texture kept at 0.9); eye_enhance 0.3 inside the eye mask. Both retouch entries are masked ai_* entries and are skipped (with a note) when the models are not installed. Denoise stays ISO-adaptive (luma 50 / chroma 70 base). All values are starting points, not a finished edit.

Base stack (9 entries): `exposure` (ev=0.15); `highlights_shadows` (highlights=-20, shadows=10); `contrast` (contrast=-0.05); `vibrance` (amount=5, protect_skin=0.7); `hsl_mixer` (orange_sat=-3, orange_lum=3, red_sat=-2); `local_contrast` (texture=-0.1, clarity=0.0); `sharpen` (amount=0.3, radius=1.0, masking=0.7, halo=0.4); `skin_smooth@skin [mask ai_skin]` (amount=0.35, texture=0.9, even_tone=0.25); `eye_enhance@eyes [mask ai_eyes]` (amount=0.3)

- variant `portrait:high_key`: `exposure` (ev=0.45); `highlights_shadows` (highlights=-35, shadows=20, whites=5)
- variant `portrait:low_key`: `exposure` (ev=-0.25); `highlights_shadows` (highlights=-10, shadows=-5, blacks=-8); `contrast` (contrast=0.05)

#### `product` - Product / still life / food

Colour accuracy and clean backgrounds. WB should be set from a grey patch (white_balance picker) per session; this preset leaves WB unchanged. lens_correct vignetting on (before any background whitening), exposure +0.1, highlights -10, shadows +5, contrast 0, local_contrast clarity +0.03 / texture +0.08, vibrance 0 and saturation 0 (profile-accurate), sharpen amount 0.45, radius 0.9, masking 0.3, minimal denoise (ISO-adaptive). Optional backdrop whitening: a masked exposure +0.3 EV and whites +10 on the inverted ai_subject mask (clean white backdrop; target 243-245 sRGB, not clipped). Variant food: exposure +0.3 EV, whites +10, saturation red -5 (Canon Food mode reduces reddish tones under tungsten), temperature 6200 (slightly cooler), vibrance +6, texture +0.12.

Base stack (6 entries): `lens_correct` (vignetting=true); `exposure` (ev=0.1); `highlights_shadows` (highlights=-10, shadows=5); `local_contrast` (clarity=0.03, texture=0.08); `sharpen` (amount=0.45, radius=0.9, masking=0.3); `exposure@backdrop [mask ai_subject]` (ev=0.3)

- variant `product:food`: `exposure` (ev=0.3); `white_balance` (temperature=6200); `highlights_shadows` (whites=10); `hsl_mixer` (red_sat=-5); `vibrance` (amount=6); `local_contrast` (texture=0.12)

#### `sports` - Sports / action

Punchy, cool-neutral render with moderate noise reduction; culling is a separate step. exposure +0.1 (faces under caps / helmets), highlights -20, shadows +20, contrast +0.12, vibrance +10, local_contrast clarity +0.10 / texture +0.10, sharpen amount 0.60, radius 1.0, masking 0.4 (the sharpen halo is limited to 0.4), denoise ISO-adaptive and a little stronger (luma 55 / chroma 70) for ISO 3200-12800. For indoor gyms set a fixed Kelvin for the whole set in white_balance to avoid flicker-induced drift.

Base stack (7 entries): `denoise` (luma=55, chroma=70, iso_adapt=true); `exposure` (ev=0.1); `highlights_shadows` (highlights=-20, shadows=20); `contrast` (contrast=0.12); `vibrance` (amount=10); `local_contrast` (clarity=0.1, texture=0.1); `sharpen` (amount=0.6, radius=1.0, masking=0.4, halo=0.4)


#### `street` - Street / documentary

Black and white by default: monochrome with a neutral panchromatic filter, S-curve with lifted toe, clarity and grain. Colour variant `color` keeps colour with a restrained look. Base: monochrome filter panchromatic (weights ~0.36/0.54/0.10 on linear Rec.2020); contrast +0.25; blacks -12, whites +8; local_contrast clarity +0.20, texture +0.10; dehaze +0.05; tone_curve luma S (toe lifted 0.02); grain amount 0.03, size 1.2, roughness 0.5 (about ISO 800), midtone_bias 0.7; sharpen amount 0.50, radius 1.0, masking 0.25; vignette -0.15. Variant `color`: no monochrome and no grain; contrast +0.10, vibrance +8, highlights -15, shadows +15, clarity +0.10. Verticals are NOT auto-corrected here (add `straighten` / `perspective` to taste).

Base stack (9 entries): `monochrome` (filter=panchromatic); `highlights_shadows` (blacks=-12, whites=8); `contrast` (contrast=0.25); `dehaze` (strength=0.05); `local_contrast` (clarity=0.2, texture=0.1); `sharpen` (amount=0.5, radius=1.0, masking=0.25); `tone_curve` (mode=luma, points=[[0.0, 0.02], [0.25, 0.21],…); `grain` (amount=0.03, size=1.2, roughness=0.5, midtone_bias=0.7); `vignette` (amount=-0.15)

- variant `street:color`: `monochrome`; `grain`; `contrast` (contrast=0.1); `highlights_shadows` (highlights=-15, shadows=15, blacks=0, whites=0); `vibrance` (amount=8); `local_contrast` (clarity=0.1, texture=0.05)

#### `video` - Video frame / log-like grade

For stills pulled from video or log footage: low-contrast, wide-latitude start that is easy to grade further. exposure 0, highlights -15, shadows +10, contrast -0.05, saturation -5, local_contrast clarity 0, sharpen amount 0.2 (video is already sharpened in-camera), vignette off, grain off. A gentle display-referred tone_curve lifts blacks by 0.01 for a cinematic start. There are no raw ops: this mode is meant for JPEG / frame-grab sources.

Base stack (5 entries): `highlights_shadows` (highlights=-15, shadows=10); `contrast` (contrast=-0.05); `saturation` (amount=-5); `sharpen` (amount=0.2, radius=1.0, masking=0.5); `tone_curve` (mode=luma, points=[[0.0, 0.01], [1.0, 1.0]])
<!-- END GENERATED: mode-catalogue -->
