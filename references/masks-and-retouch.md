# Masks, local edits and retouching

Every stack entry can carry a **mask** so an edit applies to part of the picture. This file has the mask spec, copy-ready
JSON for each type, the workflow for checking a mask, blend modes, and the retouch ops (heal, clone, inpaint, liquify,
red-eye, dodge and burn). All JSON below was run through `photo mask preview` or `photo edit` on synthetic images.

## Contents

- [Applying a mask to an edit](#applying-a-mask-to-an-edit)
- [Coordinates and units](#coordinates-and-units)
- [Common spec fields](#common-spec-fields)
- [Mask types with examples](#mask-types-with-examples)
- [AI masks and their fallbacks](#ai-masks-and-their-fallbacks)
- [Combining masks](#combining-masks)
- [Check a mask before you rely on it](#check-a-mask-before-you-rely-on-it)
- [Blend modes and opacity](#blend-modes-and-opacity)
- [Retouch ops](#retouch-ops)
- [Gotchas](#gotchas)

## Applying a mask to an edit

```bash
# one command: the --set edits become a new masked entry (always appended, so masked edits stack)
photo edit IMG.CR3 \
  --mask '{"type":"linear","p0":[0.5,0.0],"p1":[0.5,0.45],"feather":40}' \
  --set exposure@sky.ev=-0.5 --set dehaze@sky.strength=0.15

# a mask from a file, and AI masks (JSON, @file.json or a bare shorthand: sky, subject, background, face, skin, eyes, lips, teeth, animal)
photo edit IMG.CR3 --mask @sky_mask.json --set saturation@sky.amount=10
photo edit IMG.CR3 --mask '{"type":"ai_sky"}' --set 'color_balance@sky.shadows=[220,15,0]'
photo edit IMG.CR3 --mask sky --set saturation@sky.amount=8          # same as {"type":"ai_sky"}; `mask preview --spec sky` too
```

- The `@sky` id after the op name labels the entry so you can change or remove it later. To **change** the value,
  repeat the `--set` **without** `--mask`: `photo edit IMG.CR3 --set exposure@sky.ev=-0.7`. Passing `--mask` again
  always **appends a new entry**, even with the same id, so you would get two entries stacked.
- `--remove exposure@sky` removes every entry with that id; `--disable exposure@sky` switches it off.
- Put an id on every masked entry, or you cannot address it afterwards (an unmasked `--set exposure.ev=...` skips
  masked entries that have no id, which is what you want, but then they are only editable by id).
- In a hand-written sidecar or preset, the entry is `{"op":"exposure","id":"sky","params":{"ev":-0.5},"mask":{...}}`.
- `--mask` works with `develop` too (applied to that command's `--set` edits). Both commands and `mask preview --spec` share one
  parser, so the shorthands, `@file.json` and the error messages (unknown type, bad JSON) are the same everywhere.

## Coordinates and units

- Points are **normalised 0..1** of the image: `[x, y]`, x to the right, y downward, `[0,0]` top-left.
- Entry masks are in the **post-geometry** frame, that is, after crop, rotate and perspective. If you crop later,
  the mask moves with the picture's frame, not with the content. Place masks after the crop is final.
- Pixel sizes (`feather`, brush `radius`, `blur`, `guided_radius`) are defined at a **2000 px long edge** and scale
  with the render size.
- Stage 8 retouch spots (`heal`, `clone`, `inpaint`, `liquify`, `red_eye`) use **source-frame** coordinates, before
  any crop or rotation. Their own units are listed under [Retouch ops](#retouch-ops).

## Common spec fields

| field | meaning |
|---|---|
| `type` | one of the types below (aliases such as `sky`, `subject`, `face`, `gradient`, `ellipse` are accepted) |
| `feather` | softness in px@2000. Linear, radial, polygon and brush have intrinsic feathering; for other types it is a Gaussian |
| `invert` | `true` flips the mask |
| `opacity` | 0..1 multiplier of the finished mask |
| `refine` | `{guided_radius, guided_eps, blur, contrast, threshold, threshold_softness}`: snap edges to the image (guided filter), blur px@2000, contrast -1..1, hard threshold |

Order of operations: raw mask, feather, refine, invert, opacity.

<!-- BEGIN GENERATED: blend-modes -->
Mask types: `linear`, `radial`, `polygon`, `brush`, `parametric`, `luminosity`, `color_range`, `ai_subject`, `ai_background`, `ai_sky`, `ai_face`, `ai_eyes`, `ai_lips`, `ai_teeth`, `ai_skin`, `ai_animal`, `depth`, `combine`, `all`, `none`.

| blend mode | formula / note |
|---|---|
| `normal` | replace (alpha-weighted 'over') |
| `multiply` | a*b (linear domain: a*b/fulcrum) |
| `screen` | 1-(1-a)(1-b); display only |
| `overlay` | contrast, a decides; display only |
| `soft_light` | gentle contrast (W3C); display only |
| `hard_light` | contrast, b decides; display only |
| `vivid_light` | colour dodge/burn by b; display only |
| `linear_light` | a+2b-1; display only |
| `pin_light` | lighten/darken by 2b; display only |
| `exclusion` | a+b-2ab; display only |
| `darken` | min(a,b) |
| `lighten` | max(a,b) |
| `add` | a+b |
| `subtract` | max(a-b,0) |
| `difference` | \|a-b\| |
| `divide` | a/b (linear domain: a*fulcrum/b) |
| `average` | (a+b)/2 |
| `geometric_mean` | sqrt(a*b) |
| `luminosity` | lightness of b, chroma+hue of a (Oklch); alias lightness |
| `chroma` | chroma of b, lightness+hue of a |
| `hue` | hue of b, lightness+chroma of a |
| `color` | hue+chroma of b, lightness of a |
<!-- END GENERATED: blend-modes -->

## Mask types with examples

Linear gradient, 1 at `p0` falling to 0 at `p1` (graduated filter for skies):

```json
{"type":"linear","p0":[0.5,0.0],"p1":[0.5,0.45],"feather":40}
```
Optional `curvature` -1..1 (bends the iso-lines) and `curve` `smooth|linear`. With no `p1`, `angle` (degrees, 0 = +x)
makes a half-plane through `p0`.

Radial or elliptical (vignette, spotlight, face brighten). `radii` are fractions of width and height; a scalar means a
circle of that fraction of the short side:

```json
{"type":"radial","center":[0.62,0.45],"radii":[0.25,0.18],"rotation":20,"feather":120}
```
`feather_mode`: `out` (default, feather falls off outside the shape), `center`, `in`. Use `"invert": true` to affect
everything except the ellipse.

Polygon or bezier path (building, shoreline):

```json
{"type":"polygon","points":[[0.1,0.9],[0.3,0.55],[0.7,0.5],[0.95,0.9]],"smooth":true,"feather":25}
```
Points can be `[x,y]` or `{"x":..,"y":..}`. `closed` defaults to true; an open path draws a stroke of `width` px.

Brush strokes (paint by hand, erase with `"erase": true`):

```json
{"type":"brush","radius":60,"hardness":0.4,"strokes":[{"points":[[0.2,0.5],[0.35,0.55],[0.5,0.6]]},
                                                     {"points":[[0.35,0.55]],"radius":20,"erase":true}]}
```

Luminosity (Kuyper). `mode` is `lights|darks|midtones`, `level` 1..5 narrows it (L to the power n):

```json
{"type":"luminosity","mode":"lights","level":2,"feather":20}
{"type":"luminosity","range":[0.05,0.35],"softness":0.1}
```
The second form is a plain lightness band. `ev_offset` shifts the lightness before masking.

Colour range, from picked points or colours. Distance is Oklab x100 (`tolerance` inside, `falloff` soft edge):

```json
{"type":"color_range","point":[0.3,0.6],"tolerance":12,"falloff":14,"lightness_weight":0.3}
{"type":"color_range","color":"#3a7d2c","tolerance":10,"falloff":10}
```
`points`/`colors` take lists, `metric: "de2000"` is also available. `lightness_weight` 0 selects by hue and chroma only.

Parametric (darktable-style trapezoids; each channel `[outer_lo, inner_lo, inner_hi, outer_hi]`). Channels: `L C h`
(Oklch lightness, chroma, hue degrees), `R G B`, `Y`, `ev`:

```json
{"type":"parametric","channels":{"L":[0.1,0.25,0.6,0.8],"C":[0.02,0.06,0.4,0.4],"h":{"range":[80,110,160,190],"smooth":true}}}
```

Depth band (0 = near, 1 = far; uses a depth model or a heuristic):

```json
{"type":"depth","near":0.55,"far":1.0,"falloff":0.1,"feather":30}
```

`{"type":"all"}` selects everything and `{"type":"none"}` nothing, useful inside `combine`.

## AI masks and their fallbacks

| type | purpose | model used | fallback when the model is missing |
|---|---|---|---|
| `ai_subject` / `ai_background` | person, object or animal vs background | IS-Net, BiRefNet or U2-Net (ONNX) | GrabCut seeded by centre-weighted saliency |
| `ai_sky` | sky | SegFormer ADE20K | top-connected bright or blue low-texture region |
| `ai_face`, `ai_skin`, `ai_eyes`, `ai_lips`, `ai_teeth` | face parts | MediaPipe Face Landmarker | Haar cascade or skin-colour blob with canonical geometry |
| `ai_animal` | animals | YOLO11-seg (AGPL, personal use) | subject mask |
| `depth` | distance band | Depth Anything V2 Small | defocus plus vertical-position heuristic |

Models are never downloaded automatically. Install what you need with `photo models download isnet-general-use`
(see [install-models-licences.md](install-models-licences.md)). The fallbacks are crude: **always preview an AI mask**.
Options: `"model": "u2net"` on `ai_subject`, `"faces": [0]` to pick faces, `"part"` on eye masks, `"classes"` on
`ai_animal`. The mask preview and the render `log` say which backend ran (`ai_subject: no matte model installed ...
using GrabCut + saliency fallback`, `depth: WARNING no depth model installed`). A depth mask from the heuristic is
not real depth; do not trust it for subtle work.

Add `"refine": {"guided_radius": 6, "blur": 1.5}` to snap an AI mask to real edges.

## Combining masks

`combine` takes `masks` and a `mode`: `union`, `intersect`, `subtract` (first minus the rest), `exclude` (XOR);
`"soft": true` uses probabilistic maths instead of max and min.

```json
{"type":"combine","mode":"subtract","masks":[{"type":"ai_sky","refine":{"guided_radius":8}},
                                              {"type":"polygon","points":[[0.0,0.0],[0.3,0.0],[0.0,0.3]]}]}
{"type":"combine","mode":"intersect","masks":[{"type":"luminosity","mode":"darks","level":2},
                                               {"type":"radial","center":[0.5,0.5],"radii":[0.4,0.4],"feather":200}]}
```

Typical recipes: sky without the trees: `combine subtract` of `ai_sky` and a `luminosity darks` mask; subject glow:
`ai_subject` with `refine`; a darkened vignette that spares the face: radial `invert` combined with `subtract`
`ai_face`.

## Check a mask before you rely on it

```bash
photo mask preview IMG.CR3 --spec '{"type":"ai_sky"}' --out /tmp/mask.png --size 900
photo mask preview IMG.CR3 --spec @sky_mask.json --out /tmp/mask.png --mask-out /tmp/mask_raw.png
photo mask types            # lists mask types and blend modes
photo --json mask preview IMG.CR3 --spec @sky_mask.json --out /tmp/mask.png   # machine-readable result
```

`mask` and `models` subcommands do not accept `--json` or `--root` after the subcommand name; put those global flags
before it (`photo --json mask preview ...`).

Then **Read `/tmp/mask.png`**: the picture is shown with the mask in solid red (100 percent) and a thin yellow contour
at 50 percent. The JSON result has `coverage` (fraction of the image selected, 0..1) and `notes` (which model or fallback
ran). Check the edges, then check that `coverage` is plausible (a sky mask at 0.02 or 0.95 is wrong).

`mask preview` works on the **uncropped** source image. If the edit stack has a crop, rotate or perspective,
render a JPEG first (`photo render IMG --out /tmp/framed.jpg`) and preview the mask on that JPEG, because entry masks are
post-geometry. Coordinates picked on the framed JPEG then match the entry masks.

## Blend modes and opacity

Each entry has `blend` (default `normal`) and `opacity`. Set them like params:
`--set exposure@sky.blend=multiply --set exposure@sky.opacity=0.7`. Display-only modes (`screen`, `overlay`,
`soft_light`, `hard_light`, `vivid_light`, `linear_light`, `pin_light`, `exclusion`) fall back to `normal` when the entry
sits in the linear domain (stages 5 to 16). Put creative blend-mode layers in stage 18 or later (looks, effects),
for example a `gradient_map` with `blend: soft_light`. `luminosity`, `chroma`, `hue` and `color` blend in Oklch.

## Retouch ops

All stage 8 ops work in source coordinates (before crop and rotate).

**heal / clone** (`spots` list). `dst` is the spot, `src` is where to sample from (optional: omit for an automatic
source). `radius` in px@2000, `feather` 0..1 of the radius.

```bash
photo edit IMG.CR3 --set 'heal.spots=[{"dst":[0.25,0.45],"src":[0.35,0.45],"radius":40}]'
photo edit IMG.CR3 --set 'clone.spots=[{"dst":[0.80,0.30],"src":[0.70,0.30],"radius":30,"feather":0.5}]'
photo edit IMG.CR3 --set 'heal.spots=[{"dst":[0.25,0.45],"radius":40},{"dst":[0.6,0.2],"src":[0.55,0.2],"radius":25}]'
```
`heal` matches the tone of the destination (low frequencies swapped), `clone` copies as is. Replacing `heal.spots`
replaces the whole list, so include the earlier spots when you add a new one.

**inpaint** removes objects by filling from the surroundings. Either a round `spots` list (`center`, `radius` as a
**fraction of image width**, or `radius_px`) or any mask spec in `region`:

```bash
photo edit IMG.CR3 --set 'inpaint.region={"type":"radial","center":[0.58,0.55],"radii":[0.03,0.045]}'
photo edit IMG.CR3 --set 'inpaint.spots=[{"center":[0.58,0.55],"radius":0.03}]'
photo edit IMG.CR3 --set 'inpaint.region={"type":"polygon","points":[[0.5,0.5],[0.56,0.5],[0.56,0.62],[0.5,0.62]]}'
```
`method` is `telea` (default), `ns` or `lama` (needs an optional model). `grow` expands the hole in px. Inpainting
suits small and medium objects on simple backgrounds (wires, a bin, a stray person against sky or grass). It
smears across complex textures; for large or structured areas, say so and offer a manual clone, or accept a
softer result. Always view the render at 100 percent around the area.

**blemish_auto** finds small skin blemishes and dust and inpaints them: `sensitivity` 0..1, `max_size` and `min_size`
px, `skin_only` true by default (it uses the skin mask). Start with `--set blemish_auto.sensitivity=0.5`
and look; raise gently.

**red_eye** (`auto` true uses the eye mask; or give `spots` `[{"center":[x,y],"radius":0.01}]`, radius a fraction of
width). `channel` `auto|red|green` handles animal eye-shine. Specular highlights are left alone.

**liquify** `warps` list of `{type: push|pinch|bloat|twirl, center, radius (px@2000), strength, dx, dy}`:

```bash
photo edit IMG.CR3 --set 'liquify.warps=[{"type":"bloat","center":[0.5,0.4],"radius":120,"strength":0.15}]'
```
Keep strength small (0.05 to 0.3) and warp only where the content is simple; this is a geometry warp, not an AI
retouch. For people, resist reshaping faces and bodies unless asked.

**dodge_burn** (stage 12, linear). `strokes` of `{points, radius, hardness, flow, ev}` (positive `ev` dodges,
negative burns) and `regions` `[{mask: spec, ev}]`:

```bash
photo edit IMG.CR3 --set 'dodge_burn.regions=[{"mask":{"type":"radial","center":[0.5,0.4],"radii":[0.2,0.25],"feather":200},"ev":0.35}]'
photo edit IMG.CR3 --set 'dodge_burn.strokes=[{"points":[[0.2,0.5],[0.4,0.5]],"radius":50,"ev":-0.3}]'
```

Skin and face helpers run in stage 18 and use AI masks internally: `skin_smooth` (frequency separation inside the skin
mask, pores kept at `texture`), `eye_enhance`, `teeth_whiten`, `sky_enhance` (needs a sky mask), `background_blur`
(subject or depth mask). Keep `amount` modest; if the render log says the fallback masker ran, check the result.

## Gotchas

- `--set` stores whatever you write. A typo in `heal.spots` or a bad mask spec breaks every later render of that image
  until you correct it with another `--set` or `photo versions restore`. Render once after typing JSON by hand.
- Coordinates are **normalised**, not pixels. `[400, 300]` is a typo that lands far outside the image.
- `heal` and `clone` radius is px@2000 (40 is a decent default for a spot on a 24 MP frame), `inpaint` spot radius is a
  fraction of width (0.03 is a 3 percent disc). They differ on purpose; do not mix them up.
- `mask preview` ignores crop and rotate (see above).
- Masks with `ai_*` types need the optional models for good results; without them the fallbacks are rough.
- Entry masks with `feather` in px@2000 are cheap; `guided_radius` refinement and AI types are heavier. Preview at
  `--preview 900` while iterating.
