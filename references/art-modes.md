# Art modes (looks, film emulations, painterly and graphic effects)

An **art preset** is a named creative look: film stock, cinema grade, historic process, comic print, painting. It is
applied with `--art NAME[:variant]` and layered on top of the mode. 88 presets ship; the catalogue at the end is
generated from the YAML files and includes measured render times.

## Contents

- [How to apply an art look](#how-to-apply-an-art-look)
- [Choosing a look](#choosing-a-look)
- [Slots and what can be combined](#slots-and-what-can-be-combined)
- [Controlling strength](#controlling-strength)
- [Speed](#speed)
- [Licence and naming notes](#licence-and-naming-notes)
- [Art catalogue](#art-catalogue)

## How to apply an art look

```bash
# Look at it first, small and fast. Never touches the sidecar's existing stack when --merge --version is used.
photo develop IMG.CR3 --art portra_400 --merge --version film --preview 900
photo develop IMG.CR3 --art lichtenstein_comic:dots_45 --merge --version lichtenstein --preview 700
photo develop IMG.CR3 --art warhol_silkscreen:grid_2x2 --merge --version pop --preview 900
```

Then **read the produced JPEG** (the `out` path in the `--json` result) and judge it; see the critique loop in
SKILL.md.

Rules that matter:

- **Use `--merge --version NAME`** to try a look without losing the normal edit. Without `--merge`, `develop`
  rebuilds the stack (an `auto:before-develop` snapshot is saved) and the sidecar keeps the old `mode` label.
- `--version NAME` does not switch the active version. The `default` version stays what other commands act on, and
  export uses the active version unless you pass `--version NAME` to `photo export` or `photo render`.
- `NAME:variant` picks a variant, for example `portra_400:pushed`, `halftone_cmyk:newsprint`. **An unknown variant
  silently falls back to the base look**, so copy variant names from the catalogue.
- Art presets are YAML in `photostudio/presets/art/`. Your own go to `~/.config/photostudio/presets/art/`
  (`photo presets save art NAME --from IMG`). Picture Style approximations can be used as `--art style:standard`
  (see [modes.md](modes.md)).
- `photo presets list art` and `photo presets show art NAME` print what is installed.
- To use a look on many images, develop one, then `photo copy-settings` and `photo paste-settings`
  (see [library.md](library.md)), or `photo develop a.CR3 b.CR3 c.CR3 --art X`.

## Choosing a look

Match the look to the picture, not the other way round.

| goal | start with | why |
|---|---|---|
| natural portrait, warm skin | `portra_400`, `portra_160`, `pro_400h` | low contrast, soft shoulder, skin-safe colour |
| punchy landscape | `velvia_50`, `provia_100f`, `ektar_100` | saturated, higher contrast; check skies for clipping |
| nostalgic colour | `kodachrome_64`, `gold_200`, `superia_400`, `classic_chrome` | warm or green cast with grain |
| night and neon | `cinestill_800t`, `blade_runner_haze`, `halation_glow` | halation on highlights; needs real highlights to bloom |
| classic black and white | `tri_x_400`, `hp5_plus`, `adams_zone_bw`, `salgado_drama` | contrast and grain differ, compare two |
| street, documentary | `leiter_street_colour`, `moriyama_are_bure_boke`, `maier_rolleiflex` | photographer-inspired generic looks |
| cinema grade | `teal_orange`, `fincher_low_key`, `print_film_2383`, `bleach_bypass` | grade-style, usually colour-only |
| graphic poster | `warhol_silkscreen`, `lichtenstein_comic`, `halftone_cmyk`, `risograph`, `bold_outline_flat` | needs a simple, strong subject |
| painting, drawing | `oil_painting`, `watercolor_wash`, `pencil_sketch`, `charcoal_drawing`, `van_gogh_swirl` | detail is lost by design; use a sharp, well-exposed original |
| historic process | `cyanotype`, `platinum_palladium`, `tintype_wet_plate`, `daguerreotype` | strong tonal remapping |
| false colour, science | `infrared_bw`, `aerochrome`, `thermal_camera`, `xray` | foliage and heat mapped to new colours |
| retro, glitch | `vhs_tape`, `crt_scanlines`, `gameboy_camera`, `pixel_art`, `glitch` | deliberately degraded |

Guidance for taste: one idea per image. Film looks reward gentle exposure and soft light, graphic and painterly
looks reward clean shapes and few small details, and heavy looks on a noisy high-ISO frame emphasise the noise.

## Slots and what can be combined

Looks live in three stage slots (see [pipeline-and-ops.md](pipeline-and-ops.md)):

- **Slot A (stage 15, linear)**: optical effects before the tone mapper (only `halation_glow` and half of
  `tintype_wet_plate` use it; the slot letter of each preset is shown in the catalogue "stack" column).
- **Slot B (stage 18 and 19, display)**: colour and tone looks, effects and almost all film and cinema presets.
  Colour-only and fast. Colour-only stacks are the only kind that can be baked into a 3D LUT (`photo lut bake`,
  `photo video grade`).
- **Slot C (stage 20, display)**: raster art such as halftone, painterly, mosaic, comic. Slow and spatial; it cannot
  be baked into a LUT.

Combine at most one look per slot. A slot-B grade plus a slot-C raster effect is fine (the grade runs first, then
the effect). Two looks in the same slot fight: if both use the same op the later one replaces its params, otherwise
both run and the result is muddy. Build the combination in a new version so the grade stays available:

```bash
photo develop IMG.CR3 --mode portrait --art portra_400 --version base --preview 900
photo versions new IMG.CR3 base_halftone --from base --no-switch
photo develop IMG.CR3 --art halftone_cmyk:big_dots --merge --version base_halftone --preview 900
```

(`versions new` makes the new version active unless you pass `--no-switch`; the active version is what `export`,
`render` and the web editor use when no `--version` is given.)

## Controlling strength

Almost every look is one op with a strength-like parameter. Find it in the catalogue ("stack" column) and tweak it:

```bash
photo develop IMG.CR3 --art teal_orange --merge --set teal_orange.amount=0.3 --preview 900
photo develop IMG.CR3 --art portra_400 --merge --set film_emulation.opacity=0.6 --preview 900   # entry opacity blends with the look-less image
photo develop IMG.CR3 --art portra_400 --merge --set film_emulation.grain=0.5 --set film_emulation.halation=0 --preview 900
```

- `opacity` (0..1) works on every entry and blends the entry's result with its input.
- The `strength` or `amount` param, where the op has one, scales the look itself (`film_emulation.strength`,
  `teal_orange.amount`, `color_grade.strength`).
- Many presets ship `subtle` or `strong` variants; use those before hand-tuning.
- Apply a look only to part of the picture by adding `--mask` (see [masks-and-retouch.md](masks-and-retouch.md)).

Film stock names accepted by `film_emulation.stock`: portra_160, portra_400, portra_800, ektar_100, kodachrome_64,
gold_200, tri_x_400, tri_x_pushed, hp5_plus, velvia_50, provia_100f, pro_400h, superia_400, classic_chrome,
cinestill_800t, polaroid_600, instax, expired, cross_process. The 88 presets wrap these and other ops with tuned
defaults; use `film_emulation` directly only for something none of the presets cover.

## Speed

Timings in the catalogue are for one render of a 1600 px long-edge JPEG on the build machine's CPU. Colour looks
are well under a second. The slowest are the stroke-based painters (`van_gogh_swirl`, `expressionist_paint`,
`colorist_wash`, `monet_impressionist`) and a few texture synthesisers (`gum_bichromate`, `klimt_gold`,
`halftone_cmyk`, `oil_painting`, `watercolor_wash`). Rules of thumb:

- Always develop with `--preview 700` to `--preview 1200` while iterating. Pixel-based parameters scale with the
  render size, so the preview matches the full render.
- A full-size 24 MP R50 render is about 14 times the pixels of the 1600 px timing, so budget minutes, not seconds,
  for the slow looks. Export a few finished frames, do not batch-render a slow look over hundreds of files.
- Looks tagged `slow` in the catalogue also take noticeable time on video through `--frames python`
  (see [video.md](video.md)); colour-only looks use the fast LUT path.

Re-measure with `python scripts/gen_reference_docs.py --measure` (about 2 minutes) after a hardware or code change.

## Licence and naming notes

- Film names (Portra, Kodachrome, Velvia, Tri-X and so on) are used as generic descriptive labels for an
  *emulation*; the brands belong to their owners and nothing from the manufacturers is bundled.
- Photographer-inspired and artist-inspired presets are generic stylistic emulations, not copies of specific works.
  Some preset notes say so explicitly (for example Warhol and the Fairey poster style). Fine for your own photos.
  Do not recreate a specific copyrighted artwork or someone's trademarked image.
- The BM3D denoiser (`denoise.method=bm3d`) is non-commercial; the default wavelet denoiser has no such limit.

## Art catalogue

<!-- BEGIN GENERATED: art-catalogue -->
88 art presets. Time = measured seconds for one render of a 1600 px JPEG on the build machine's CPU (`--measure`; relative speed matters more than the number). `slow` = tagged slow by the preset author.

#### Pop art and graphic prints

| preset | variants (`--art name:variant`) | stack | time | notes |
|---|---|---|---|---|
| `bold_outline_flat` | `no_marks` | `haring` (outline=9) [slot C] | 0.4 s | **Bold-outline flat colour**. Haring-style bold outlines: three or four flat saturated primaries, white background cluster, thick uniform black outline and short radiating motion lines from strong corners. |
| `fairey_four_tone` | `caption_band`, `no_split` | `fairey_hope` (split=diagonal) [slot C] | 0.2 s | **Four-tone stencil poster**. Fairey 'Hope'-style stencil poster: bilateral + CLAHE luminance posterised to navy / red / light blue / cream with a diagonal split that swaps the middle colours. Flat, no texture. Fine on your own photos; do not reproduce the Obama poster. |
| `halftone_cmyk` | `big_dots`, `newsprint` | `halftone` (mode=cmyk, pitch=12) [slot C] | 1.5 s | **CMYK halftone print**. Four rotated dot screens (15/75/0/45 deg) in CMYK inks on paper. Variants: single-screen B&W newsprint, large pitch. |
| `lichtenstein_comic` | `dots_45`, `fine_dots` | `lichtenstein` (pitch=14, outline=5) [slot C] | 0.6 s | **Comic print (Ben-Day)**. Lichtenstein-style comic print: k-means flat primaries (red / yellow / blue / cream), uniform Ben-Day dots (equal size, regular grid) in skin and sky areas, thick black outlines, white highlight slashes. |
| `stencil_graffiti` | `accent`, `three_tone` | `banksy_stencil` [slot C] | 0.5 s | **Stencil graffiti**. Banksy-style spray stencil: Otsu threshold to black / wall, bridges cleaned, soft overspray halo, concrete-wall texture. Variants: a single saturated accent colour, or three tones. |
| `warhol_silkscreen` | `grid_2x2`, `grid_3x3`, `kmeans_zones`, `turquoise` | `warhol` (levels=4, palette=p1, mode=luma) [slot C] | 0.4 s | **Silkscreen pop**. Warhol-style silkscreen: edge-preserving flatten, 4 flat tone bands mapped to a saturated designed palette, dark ink layer printed slightly off-register. Variants tile 2x2 / 3x3 panels with a different palette each. _Look is generic 'silkscreen pop'; do not reproduce specific Warhol works._ |

#### Painting, drawing and engraving

| preset | variants (`--art name:variant`) | stack | time | notes |
|---|---|---|---|---|
| `cel_toon` | `flat`, `xdog_lines` | `cel_toon` [slot C] | 0.3 s | **Cel / toon shading**. Cartoon cel shading: half-res bilateral x3, lightness quantised to five soft steps, saturated colour and adaptive-threshold ink outlines. |
| `charcoal_drawing` | - | `ink_xdog` (style=charcoal) [slot C] | 0.8 s | **Charcoal drawing**. Charcoal by flow-guided XDoG (wide edge sigma, p=70, eps=0.80) with tonal shading on warm paper. |
| `colorist_wash` | - | `painterly` (style=colorist) [slot C] | 1.0 s (slow) | **Colourist wash**. Hertzmann colorist-wash style (T=200, alpha 0.5, RGB jitter 0.3): translucent overlapping strokes with colour jitter. |
| `expressionist_paint` | - | `painterly` (style=expressionist) [slot C] | 2.6 s (slow) | **Expressionist paint**. Hertzmann expressionist style (T=50, curvature filter 0.25, alpha 0.7, value jitter) for bold directional strokes in the original colours. _Slow: ~3 s at 1600 px._ |
| `ink_line_art` | `pencil` | `ink_xdog` (style=ink) [slot C] | 0.3 s | **Ink line art (XDoG)**. Pen-and-ink by XDoG (Winnemoeller 2012, k=1.6, p~20, hard tanh threshold): black lines and hatching on white. |
| `klimt_gold` | - | `klimt` [slot C] | 1.5 s | **Gold-leaf ornament**. Klimt-style: the salient subject stays photographic, the background becomes gold-leaf SLIC tiles with spiral, circle and square motifs, metallic noise and glints. _Segmentation is saliency + GrabCut (heuristic); ~2 s at 1600 px._ |
| `low_poly` | `coarse`, `fine` | `low_poly` [slot C] | 0.2 s | **Low-poly facets**. Low-poly / cubist: Canny-weighted point cloud (1500 points) -> Delaunay triangulation -> mean colour per facet with faint seams. |
| `mondrian_grid` | `busy` | `mondrian` (white_bias=1.6) [slot C] | 0.1 s | **Mondrian grid**. Mondrian-style: image-adaptive quadtree snapped to strong edges, leaves coloured white / red / blue / yellow (white-biased), thick black dividing lines. |
| `monet_impressionist` | `wash` | `monet` [slot C] | 1.3 s (slow) | **Impressionist dabs**. Monet-style: lifted blacks, violet shadows, cream highlights and Hertzmann impressionist strokes (T=100, R=8/4/2) for soft broken colour. _Slow: ~2 s at 1600 px._ |
| `mosaic_tesserae` | `grid` | `mosaic` [slot C] | 0.6 s | **Mosaic**. Mosaic of tesserae: SLIC superpixels filled with mean colour, dark grout, per-tile light variation and bevel. Grid variant for square tiles. |
| `oil_painting` | `thick` | `oil_paint` [slot C] | 1.0 s | **Oil painting**. Oil-paint filter: neighbourhood-mode (OpenCV xphoto oilPainting algorithm) on 24 intensity bins, brush-edge detail boost and a canvas weave. |
| `orton_glow` | `subtle` | `orton` [slot B] | 0.2 s | **Orton glow**. Orton effect: screen+multiply duplicate blurred in at 35%, with highlight protection against dark halos. |
| `pastel_chalk` | - | `ink_xdog` (style=pastel, colour=0.85) [slot C] | 0.8 s | **Soft pastel**. Pastel / chalk: XDoG tonal strokes (eps=1.0, all tones) along the flow field, coloured by the photo on toned paper. |
| `pencil_sketch` | `coloured_pencil`, `opencv`, `soft_charcoal` | `pencil_sketch` [slot C] | 0.3 s | **Pencil sketch**. Graphite sketch: colour-dodge of grey against its blurred negative, XDoG hatching and graphite grain on paper. Variants: coloured pencil, OpenCV pencilSketch. |
| `pixel_art` | `chunky`, `dithered` | `pixel_art` [slot C] | 0.1 s | **Pixel art**. Pixel art: area downscale to 128 columns, 24-colour Oklab k-means palette, integer nearest-neighbour upscale. Variants: 64 cells, 16 colours with Bayer dither. |
| `seurat_pointillism` | `fine`, `hertzmann` | `pointillism` [slot C] | 0.4 s | **Pointillism**. Seurat-style pointillism: jittered-grid dots coloured from the image with HSV jitter, 12% complementary-hue dots for optical mixing, on a cream canvas. |
| `van_gogh_swirl` | `heavy_swirl` | `van_gogh`; `paper_texture` (strength=0.04, fibres=0.0, mode=overlay) [slot C] | 3.6 s (slow) | **Post-impressionist strokes**. Van Gogh-style: saturated blue / yellow push, Hertzmann expressionist directional strokes (T=50, R=8/4/2, curvature 0.25), impasto relief and a line-integral-convolution swirl in calm regions, plus a touch of canvas. _Slow: 3-8 s at 1600 px (brush strokes)._ |
| `watercolor_wash` | `loose` | `watercolor` [slot C] | 1.0 s | **Watercolour**. Watercolour: edge-preserving wash, soft-quantised lightness, wet-edge pigment pooling, noise-displaced bleed, granulation and paper tooth in a high-key palette. |
| `woodcut_print` | - | `ink_xdog` (style=woodcut) [slot C] | 0.7 s | **Woodcut print**. Woodcut: flow-guided XDoG with p~100, eps 0.73 and a hard threshold - bold black and cream carved lines. |

#### Photographer-inspired looks

| preset | variants (`--art name:variant`) | stack | time | notes |
|---|---|---|---|---|
| `adams_zone_bw` | `neutral`, `orange_filter` | `adams_zone` [slot B] | 0.4 s | **Zone-system B&W**. Ansel Adams-style: red-filter channel mix in linear light, full zone scale with textured Zone I and IX (no pure black), CLAHE and large-radius local contrast, burned blue skies, fine grain, no vignette. |
| `crewdson_twilight` | - | `crewdson` [slot B] | 0.6 s | **Cinematic twilight tableau**. Gregory Crewdson-inspired: blue ambient shadows vs warm tungsten practicals, lifted blacks, fog, bloom, muted greens. |
| `eggleston_dye_transfer` | - | `eggleston` [slot B] | 0.7 s | **Dye-transfer colour**. William Eggleston-inspired dye-transfer colour: dense saturated reds and greens, warm yellow-ish neutrals, strong tonal density, very fine grain. |
| `fan_ho_light` | - | `fan_ho` [slot B] | 0.5 s | **Hard light and haze**. Fan Ho-inspired: S-gain 9 with deep shadows, haze and radial light rays from the brightest highlight, warm-neutral B&W. |
| `leiter_street_colour` | - | `leiter` [slot B] | 0.8 s | **Layered street colour**. Saul Leiter-inspired: muted global saturation with warm reds and yellows kept rich (reds toward orange), lifted midtones and blacks, green-cyan shadows, amber highlights, soft haze, colour grain. |
| `lik_hdr_landscape` | - | `lik_hdr` [slot B] | 0.6 s | **HDR landscape**. Dramatic HDR landscape: base/detail local tone-mapping in log luminance, strong clarity, vivid saturation with warm sunset hues, deep blues, vignette. |
| `maier_rolleiflex` | `square` | `maier` [slot B] | 0.2 s | **Rolleiflex street B&W**. Vivian Maier-inspired: long smooth mid-tones, moderate contrast, fine grain, warm paper highlights and soft near-black shadows (square crop optional via letterbox). |
| `mccurry_kodachrome` | - | `mccurry` [slot B] | 0.4 s | **Kodachrome-style travel**. Steve McCurry-inspired travel colour: S-curve with hard highlight clip, blue-tinted shadows, hot reds, deep true blues, crisp micro-contrast, fine mono grain. |
| `moriyama_are_bure_boke` | `calmer` | `moriyama` [slot B] | 0.5 s | **Are-bure-boke (grain, blur, defocus)**. Daido Moriyama / Provoke-inspired: crushed high-contrast B&W, huge clumpy grain, random regional motion blur and defocus, tilt, vignette, dust and scratches. |
| `salgado_drama` | - | `salgado` [slot B] | 0.5 s | **Dramatic documentary B&W**. Sebastiao Salgado-inspired: mix 0.45/0.45/0.10, heavy CLAHE local contrast, S-gain 8, burned skies, edge burn, rich pushed-film grain, warm-neutral print tone. |
| `sugimoto_seascape` | - | `sugimoto_sea` [slot B] | 0.4 s | **Long-exposure seascape**. Hiroshi Sugimoto-style minimalism: horizon split, motion-smoothed water below and soft sky above, low-contrast B&W with lifted blacks and capped highlights, warm-neutral tone, no visible grain. |

#### Cinema grades

| preset | variants (`--art name:variant`) | stack | time | notes |
|---|---|---|---|---|
| `blade_runner_haze` | `la` | `blade_runner` [slot B] | 0.3 s | **Orange haze dystopia**. Blade Runner 2049-style: near-monochrome orange, fog veil, bloom, heavy contrast. The 'la' variant keeps neon over teal-black shadows. |
| `bleach_bypass` | `strong` | `bleach_bypass` [slot B] | 0.4 s | **Bleach bypass**. Bleach bypass: saturation -45% (range 35-59%), luminosity S-curve, B&W overlay, silvery desaturated highlights, grain. |
| `cinestill_800t` | - | `halation` (threshold=0.6, knee=0.4, radius=14, strength=0.9); `film_emulation` (stock=cinestill_800t, halation=0.0) [slot B] | 0.5 s | **800T tungsten (emulation)**. Tungsten-balanced cinema negative with strong red-orange halation around highlights (linear slot-A halation), cool blue daylight cast. Generic emulation; film and brand names are trademarks of their owners. |
| `day_for_night` | - | `day_for_night` [slot B] | 0.5 s | **Day for night**. Day-for-night: -2.2 EV in linear light, Purkinje blue-green mix, deep-blue tint, desaturation; bright practicals keep warm colour. |
| `fincher_low_key` | - | `fincher` [slot B] | 0.4 s | **Green-yellow low key**. Fincher-style: -0.7 EV, olive mid-tones, teal-black shadows, muted saturation 0.75, contrast 5, fine sharpening and grain. |
| `halation_glow` | - | `halation` (threshold=0.5, radius=22, strength=1.4); `bloom` (strength=0.25) [slot A] | 0.3 s | **Highlight halation**. Linear-light halation: highlights above 0.6 bleed a red-orange glow (slot A) with a soft bloom, as in cinema negative stocks. |
| `letterbox_scope` | - | `vignette` (amount=-0.4); `grain` (amount=0.015); `letterbox` (aspect=2.39) [slot C] | 0.3 s | **Cinemascope letterbox**. 2.39:1 letterbox bars over the frame with mild grain and vignette. |
| `matrix_green` | - | `matrix_green` [slot B] | 0.4 s | **Digital-rain green**. Matrix-style: desaturated with a green cast, deep green shadows, pale green highlights, cool skin, contrast gain 5. |
| `print_film_2383` | - | `print_film` [slot B] | 0.7 s | **Print-film look (procedural)**. Kodak 2383-style procedural print emulation: cyan-leaning dense shadows, soft S-curve, warm highlights. For the real LUT, download the free LUTs and apply with lut3d. _Generic emulation; film and brand names are trademarks of their owners._ |
| `teal_orange` | `strong`, `subtle` | `teal_orange` (amount=0.5) [slot B] | 0.6 s | **Teal and orange**. Blockbuster grade: shadows pushed toward teal, highlights toward orange (Oklch hue lerp), chroma boost, skin protected, S-gain 4. |
| `technicolor_3strip` | - | `technicolor_3strip` [slot B] | 0.5 s | **Three-strip Technicolor**. Technicolor 3-strip: dye-separation matrix, saturation x1.3, crushed cyan-tinted blacks, S-gain 6, mild halation and softening. |
| `wes_anderson_pastel` | `asteroid_city`, `darjeeling`, `fantastic_fox`, `grand_budapest`, `isle_of_dogs`, `moonrise`, `royal`, `zissou` | `wes_anderson` (palette=GrandBudapest2) [slot B] | 0.8 s | **Pastel palette remap**. Wes Anderson-style: k-means clusters remapped to an exact `wesanderson` palette (GrandBudapest2 pink/lavender by default), blended 55/45 with the original, lowered contrast, lifted blacks, warm. Variants swap the palette. |

#### Film stock emulations

| preset | variants (`--art name:variant`) | stack | time | notes |
|---|---|---|---|---|
| `classic_chrome` | - | `film_emulation` (stock=classic_chrome) [slot B] | 0.4 s | **Classic Chrome (simulation)**. Muted documentary slide simulation: sat 0.75, brick reds, cyan-grey blues, desaturated highlights, fine mono grain. Generic emulation; film and brand names are trademarks of their owners. |
| `cross_process` | `c41_in_e6` | `cross_process` [slot B] | 0.5 s | **Cross-process**. E6 slide in C41 chemistry: per-channel curves give yellow-green highlights and cyan-blue shadows with high contrast and saturation. Variant: C41 in E6. |
| `ektar_100` | - | `film_emulation` (stock=ektar_100) [slot B] | 0.6 s | **Ektar 100 (emulation)**. Vivid fine-grain negative: S-gain 6, saturation x1.35, strong reds and deep blues, very fine grain. Generic emulation; film and brand names are trademarks of their owners. |
| `expired_film` | - | `film_emulation` (stock=expired); `light_leak` (strength=0.35, count=2) [slot B] | 0.6 s | **Expired film**. Expired film: lifted fog blacks, random magenta or green shadow cast, low contrast, heavy coloured grain, light leaks. Generic emulation; film and brand names are trademarks of their owners. |
| `film_grain_400` | - | `grain` (amount=0.04, size=1.2); `vignette` (amount=-0.3) [slot B] | 0.2 s | **Film grain 400**. Luminance-dependent mono grain (sigma 0.04, 1.2 px, midtone-weighted) with a gentle vignette. |
| `gold_200` | - | `film_emulation` (stock=gold_200) [slot B] | 0.5 s | **Gold 200 (emulation)**. Consumer warm negative: +400K warmth, rich yellows, sat 1.1, warm shadows, grain 0.03. Generic emulation; film and brand names are trademarks of their owners. |
| `hp5_plus` | - | `film_emulation` (stock=hp5_plus) [slot B] | 0.3 s | **HP5 Plus (emulation)**. Flexible B&W film: mix 0.3/0.55/0.15, gentler gain 4.5 than Tri-X, moderate grain. Generic emulation; film and brand names are trademarks of their owners. |
| `kodachrome_64` | - | `film_emulation` (stock=kodachrome_64) [slot B] | 0.4 s | **Kodachrome 64 (emulation)**. Slide-film classic: hot reds, deep blues, hard highlights, dense blue-leaning shadows, mono grain. Generic emulation; film and brand names are trademarks of their owners. |
| `light_leak_film` | - | `light_leak` (strength=0.5, count=3); `grain` (amount=0.03, chroma=0.8) [slot B] | 0.6 s | **Light leaks**. Warm film light leaks screened over the frame with film grain. |
| `polaroid_600` | `instax` | `film_emulation` (stock=polaroid_600); `border` (style=polaroid, width=40, color=#F4EFE4) [slot B] | 0.6 s | **Instant film 600 (emulation)**. Instant-film look: low contrast, lifted blacks, cyan shadows / warm highlights, magenta mid-tone, soft focus, vignette, white frame. Variant: cleaner cyan 'instax'. Generic emulation; film and brand names are trademarks of their owners. |
| `portra_160` | - | `film_emulation` (stock=portra_160) [slot B] | 0.6 s | **Portra 160 (emulation)**. Soft, fine-grained portrait negative: gentle S-curve, restrained saturation 0.88, cream highlight roll-off, olive greens. Generic emulation; film and brand names are trademarks of their owners. |
| `portra_400` | `pushed` | `film_emulation` (stock=portra_400) [slot B] | 0.6 s | **Portra 400 (emulation)**. Warm, low-contrast portrait negative: S-gain 3.5, lifted blacks 0.04, cream shoulder, sat 0.9, olive foliage, soft grain. Generic emulation; film and brand names are trademarks of their owners. |
| `portra_800` | - | `film_emulation` (stock=portra_800) [slot B] | 0.6 s | **Portra 800 (emulation)**. Higher-ISO portrait negative: gain 4, visible grain (0.045), slightly green shadows. Generic emulation; film and brand names are trademarks of their owners. |
| `pro_400h` | - | `film_emulation` (stock=pro_400h) [slot B] | 0.7 s | **Pro 400H (emulation)**. Pastel mint negative: lifted blacks, minty shadows, greens toward cyan, airy pastel highlights. Generic emulation; film and brand names are trademarks of their owners. |
| `provia_100f` | - | `film_emulation` (stock=provia_100f) [slot B] | 0.4 s | **Provia 100F (emulation)**. Neutral slide film: S-gain 5, sat 1.15, slightly cyan blues, very fine grain. Generic emulation; film and brand names are trademarks of their owners. |
| `superia_400` | - | `film_emulation` (stock=superia_400) [slot B] | 0.6 s | **Superia 400 (emulation)**. Everyday consumer negative: gain 4.5, punchy greens and cyans, warm reds, slightly magenta mid-tones. Generic emulation; film and brand names are trademarks of their owners. |
| `tri_x_400` | `pushed` | `film_emulation` (stock=tri_x_400) [slot B] | 0.3 s | **Tri-X 400 (emulation)**. Classic B&W reportage film: mix 0.33/0.5/0.17, S-gain 6, strong grain. Variant: pushed (gain 9, heavy grain). Generic emulation; film and brand names are trademarks of their owners. |
| `velvia_50` | - | `film_emulation` (stock=velvia_50) [slot B] | 0.4 s | **Velvia 50 (emulation)**. Super-saturated slide film: S-gain 7.5, sat x1.5, magenta-leaning reds, rich greens, deep blues. Generic emulation; film and brand names are trademarks of their owners. |

#### Historic and alternative processes, false colour

| preset | variants (`--art name:variant`) | stack | time | notes |
|---|---|---|---|---|
| `aerochrome` | `channel_swap` | `aerochrome` [slot B] | 0.5 s | **Infrared false colour**. Aerochrome-style: IR -> red, red -> green, green -> blue; foliage turns pink-red, skies cyan-blue. Variant: plain R<->B channel swap. |
| `cyanotype` | `brushed_border` | `cyanotype` [slot B] | 0.3 s | **Cyanotype**. Prussian-blue print on paper: luminance gradient map #0B2545 -> #1D5B8C -> paper white, soft curve, paper tooth. Variant adds a brushed coating border. |
| `daguerreotype` | - | `daguerreotype` [slot B] | 0.5 s | **Daguerreotype**. Mirror-silver plate: blue-weighted mono, extreme detail, low Dmax, violet/bronze edge tarnish, oval vignette, scratches. |
| `duotone_pop` | `selenium`, `warm_tritone` | `duotone` [slot B] | 0.2 s | **Duotone**. Gradient-map duotone (#1B1464 -> #FF2D55) in Oklab with contrast gain 3. Variants: selenium tritone, warm sepia tritone. |
| `gum_bichromate` | - | `gum_bichromate` [slot C] | 1.5 s | **Gum bichromate**. Three misregistered pigment layers on heavy paper with brush-edged borders, lifted blacks and muted saturation. |
| `infrared_bw` | - | `infrared_bw` [slot B] | 0.4 s | **Infrared B&W**. Wood-effect infrared: foliage glows, blue sky goes black, halation-like glow, waxy skin, HIE-like grain. |
| `lith_print` | - | `lith` [slot B] | 0.3 s | **Lith print**. Lith print: peach soft highlights, hard cold shadows, grain concentrated in the shadows, split tone. |
| `lomo_toy_camera` | `holga` | `lomo` [slot B] | 0.7 s | **Toy camera**. Lomo / Holga-style: strong vignette, saturated punchy colour, blue shadows / warm highlights, chromatic aberration, light leaks. Variant: Holga corner defocus with cross-process. |
| `negative` | `bw` | `xray` (mode=negative) [slot B] | 0.1 s | **Colour negative**. Inverted colour image. Variant: B&W negative. |
| `platinum_palladium` | - | `platinum` [slot B] | 0.3 s | **Platinum / palladium print**. Very long tonal scale, matte warm-neutral gradient #1C1A17 -> #5A4D40 -> #EFE9DD, fine grain, paper texture. |
| `risograph` | `blue_yellow`, `green_pink`, `teal_red` | `risograph` [slot C] | 0.7 s | **Risograph**. Two-ink Riso print: optical-density least-squares separation, AM dot screens + grain, per-layer misregistration, multiply overprint on cream paper. Variants: teal/red, blue/yellow. |
| `sepia_albumen` | `classic_matrix` | `sepia` (style=albumen) [slot B] | 0.3 s | **Sepia / albumen print**. Albumen print: gradient map #2B1A0E -> #8B5A2B -> #F2E6C9, lifted blacks, yellowed highlights, vignette and fine paper grain. Variant: classic sepia matrix. |
| `solarization` | `bw`, `luma` | `solarize` [slot B] | 0.2 s | **Solarisation (Sabattier)**. Sabattier effect: tone curve folded above the threshold with Mackie-line edge halos. Variants: luminance-only or B&W. |
| `thermal_camera` | `rainbow`, `white_hot` | `thermal` [slot B] | 0.2 s | **Thermal camera**. Thermal imager false colour: smoothed lightness at 320 px through an ironbow ramp with temperature bar. Variants: white-hot, rainbow. |
| `tintype_wet_plate` | `ruby_gold` | `spectral_mix` (mode=mono, weights=[0.0, 0.25, 0.75]); `wet_plate` (spectral_mix=false) [slot A+B] | 0.5 s | **Wet-plate tintype**. Collodion tintype: blue-only spectral sensitivity (0/0.25/0.75 mix in linear slot A) - reds dark, eyes light - short tonal scale, pour marks, chemical edge fog, edge defocus, dust. Variant: ruby / gold toning. |
| `xray` | - | `xray` [slot B] | 0.2 s | **X-ray**. X-ray: B&W negative through a blue-white gradient with edge glow and mild vignette. |

#### Lo-fi, optical and finishing effects

| preset | variants (`--art name:variant`) | stack | time | notes |
|---|---|---|---|---|
| `crt_scanlines` | - | `chromatic_shift` (mode=offset, red_dx=1.5, blue_dx=-1.5); `glow` (strength=0.35); `scanlines` (period=4, strength=0.3, triad=0.12) [slot C] | 0.2 s | **CRT scanlines**. CRT monitor: scanlines plus RGB aperture-grille triad, glow and slight chromatic shift. |
| `gameboy_camera` | `no_dither` | `gameboy` [slot C] | 0.1 s | **Handheld pixel camera**. Game Boy Camera: 128-column 4-shade green ramp with 4x4 Bayer dither, integer upscale. |
| `glitch` | `mild` | `glitch` [slot C] | 0.1 s | **Glitch**. Digital glitch: displaced row bands, RGB misalignment, JPEG crush with macroblock corruption, pixel sorting, scanlines, neon noise bands. |
| `soft_focus_dream` | - | `soft_focus` [slot B] | 0.2 s | **Soft focus**. Soft-focus lens: sharp core with blurred copy mixed in, highlight bleed and slight contrast drop. |
| `tilt_shift_miniature` | - | `tilt_shift` [slot B] | 0.5 s | **Tilt-shift miniature**. Miniature-model look: blur grows away from a horizontal in-focus band, boosted saturation and contrast. |
| `vhs_tape` | - | `vhs` [slot C] | 0.2 s | **VHS tape**. Consumer VHS: 480-line resample, bled chroma, crushed contrast, line noise, tracking bar, interlace comb, barrel distortion. |
<!-- END GENERATED: art-catalogue -->
