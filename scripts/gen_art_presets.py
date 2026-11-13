#!/usr/bin/env python
"""Regenerate photostudio/presets/art/*.yaml from the compact table below (single source of truth for the art presets).

    .venv/bin/python scripts/gen_art_presets.py

Preset schema (docs/architecture.md): name, kind: art, title, description, tags, stack [{op, params}], variants {name: {stack}}, options.
UI titles for living / rights-enforcing artists and trademarked film names are deliberately generic (see spec licensing note);
descriptions name the inspiration. Variant stacks merge into the base stack by op name."""
from __future__ import annotations

from pathlib import Path

import yaml

OUT = Path(__file__).resolve().parents[1] / "photostudio" / "presets" / "art"
TM = "Generic emulation; film and brand names are trademarks of their owners."
PRESETS: list[dict] = []


def E(op, **params):
    return {"op": op, "params": params}


def P(name, title, desc, tags, stack, variants=None, slot="B", note=None, **opts):
    d = {"name": name, "kind": "art", "title": title, "description": desc, "tags": tags, "stack": stack}
    if variants:
        d["variants"] = {k: {"stack": v} for k, v in variants.items()}
    d["options"] = {"slot": slot, **({"note": note} if note else {}), **opts}
    PRESETS.append(d)


# ----------------------------------------------------------------------------- pop
P("warhol_silkscreen", "Silkscreen pop",
  "Warhol-style silkscreen: edge-preserving flatten, 4 flat tone bands mapped to a saturated designed palette, dark ink layer printed slightly off-register. Variants tile 2x2 / 3x3 panels with a different palette each.",
  ["pop", "raster"], [E("warhol", levels=4, palette="p1", mode="luma")],
  {"grid_2x2": [E("warhol", grid="2x2")], "grid_3x3": [E("warhol", grid="3x3")], "turquoise": [E("warhol", palette="p2")],
   "kmeans_zones": [E("warhol", mode="kmeans", kmeans_k=5, palette="p3")]}, slot="C",
  note="Look is generic 'silkscreen pop'; do not reproduce specific Warhol works.")
P("lichtenstein_comic", "Comic print (Ben-Day)",
  "Lichtenstein-style comic print: k-means flat primaries (red / yellow / blue / cream), uniform Ben-Day dots (equal size, regular grid) in skin and sky areas, thick black outlines, white highlight slashes.",
  ["pop", "raster"], [E("lichtenstein", pitch=14, outline=5)], {"dots_45": [E("lichtenstein", angle=45.0)], "fine_dots": [E("lichtenstein", pitch=9, dot_radius=0.3)]}, slot="C")
P("fairey_four_tone", "Four-tone stencil poster",
  "Fairey 'Hope'-style stencil poster: bilateral + CLAHE luminance posterised to navy / red / light blue / cream with a diagonal split that swaps the middle colours. Flat, no texture. Fine on your own photos; do not reproduce the Obama poster.",
  ["pop", "raster"], [E("fairey_hope", split="diagonal")], {"no_split": [E("fairey_hope", split="none")], "caption_band": [E("fairey_hope", caption_band=True)]}, slot="C")
P("stencil_graffiti", "Stencil graffiti",
  "Banksy-style spray stencil: Otsu threshold to black / wall, bridges cleaned, soft overspray halo, concrete-wall texture. Variants: a single saturated accent colour, or three tones.",
  ["pop", "raster"], [E("banksy_stencil")], {"accent": [E("banksy_stencil", accent=True)], "three_tone": [E("banksy_stencil", levels=3)]}, slot="C")
P("bold_outline_flat", "Bold-outline flat colour",
  "Haring-style bold outlines: three or four flat saturated primaries, white background cluster, thick uniform black outline and short radiating motion lines from strong corners.",
  ["pop", "raster"], [E("haring", outline=9)], {"no_marks": [E("haring", radiating=False)]}, slot="C")

# ----------------------------------------------------------------------------- painterly
P("van_gogh_swirl", "Post-impressionist strokes",
  "Van Gogh-style: saturated blue / yellow push, Hertzmann expressionist directional strokes (T=50, R=8/4/2, curvature 0.25), impasto relief and a line-integral-convolution swirl in calm regions, plus a touch of canvas.",
  ["painterly", "raster", "slow"], [E("van_gogh"), E("paper_texture", strength=0.04, fibres=0.0, mode="overlay")], {"heavy_swirl": [E("van_gogh", swirl=1.0)]}, slot="C",
  note="Slow: 3-8 s at 1600 px (brush strokes).")
P("monet_impressionist", "Impressionist dabs",
  "Monet-style: lifted blacks, violet shadows, cream highlights and Hertzmann impressionist strokes (T=100, R=8/4/2) for soft broken colour.",
  ["painterly", "raster", "slow"], [E("monet")], {"wash": [E("monet", style="colorist")]}, slot="C", note="Slow: ~2 s at 1600 px.")
P("seurat_pointillism", "Pointillism",
  "Seurat-style pointillism: jittered-grid dots coloured from the image with HSV jitter, 12% complementary-hue dots for optical mixing, on a cream canvas.",
  ["painterly", "raster"], [E("pointillism")], {"hertzmann": [E("painterly", style="pointillist", init="paper", paper="#F1E8D6")], "fine": [E("pointillism", radius=4.5)]}, slot="C")
P("expressionist_paint", "Expressionist paint",
  "Hertzmann expressionist style (T=50, curvature filter 0.25, alpha 0.7, value jitter) for bold directional strokes in the original colours.",
  ["painterly", "raster", "slow"], [E("painterly", style="expressionist")], slot="C", note="Slow: ~3 s at 1600 px.")
P("colorist_wash", "Colourist wash",
  "Hertzmann colorist-wash style (T=200, alpha 0.5, RGB jitter 0.3): translucent overlapping strokes with colour jitter.",
  ["painterly", "raster", "slow"], [E("painterly", style="colorist")], slot="C")
P("oil_painting", "Oil painting",
  "Oil-paint filter: neighbourhood-mode (OpenCV xphoto oilPainting algorithm) on 24 intensity bins, brush-edge detail boost and a canvas weave.",
  ["painterly", "raster"], [E("oil_paint")], {"thick": [E("oil_paint", radius=8, levels=16)]}, slot="C")
P("watercolor_wash", "Watercolour",
  "Watercolour: edge-preserving wash, soft-quantised lightness, wet-edge pigment pooling, noise-displaced bleed, granulation and paper tooth in a high-key palette.",
  ["painterly", "raster"], [E("watercolor")], {"loose": [E("watercolor", bleed=12, levels=5)]}, slot="C")
P("pencil_sketch", "Pencil sketch",
  "Graphite sketch: colour-dodge of grey against its blurred negative, XDoG hatching and graphite grain on paper. Variants: coloured pencil, OpenCV pencilSketch.",
  ["painterly", "raster"], [E("pencil_sketch")], {"coloured_pencil": [E("pencil_sketch", colour=0.6)], "opencv": [E("pencil_sketch", method="opencv", colour=0.5)], "soft_charcoal": [E("pencil_sketch", sigma=25.0)]}, slot="C")
P("ink_line_art", "Ink line art (XDoG)",
  "Pen-and-ink by XDoG (Winnemoeller 2012, k=1.6, p~20, hard tanh threshold): black lines and hatching on white.",
  ["painterly", "raster"], [E("ink_xdog", style="ink")], {"pencil": [E("ink_xdog", style="pencil")]}, slot="C")
P("charcoal_drawing", "Charcoal drawing",
  "Charcoal by flow-guided XDoG (wide edge sigma, p=70, eps=0.80) with tonal shading on warm paper.",
  ["painterly", "raster"], [E("ink_xdog", style="charcoal")], slot="C")
P("woodcut_print", "Woodcut print",
  "Woodcut: flow-guided XDoG with p~100, eps 0.73 and a hard threshold - bold black and cream carved lines.",
  ["painterly", "raster"], [E("ink_xdog", style="woodcut")], slot="C")
P("pastel_chalk", "Soft pastel",
  "Pastel / chalk: XDoG tonal strokes (eps=1.0, all tones) along the flow field, coloured by the photo on toned paper.",
  ["painterly", "raster"], [E("ink_xdog", style="pastel", colour=0.85)], slot="C")
P("cel_toon", "Cel / toon shading",
  "Cartoon cel shading: half-res bilateral x3, lightness quantised to five soft steps, saturated colour and adaptive-threshold ink outlines.",
  ["painterly", "raster"], [E("cel_toon")], {"xdog_lines": [E("cel_toon", edge_method="xdog")], "flat": [E("cel_toon", levels=3)]}, slot="C")
P("low_poly", "Low-poly facets",
  "Low-poly / cubist: Canny-weighted point cloud (1500 points) -> Delaunay triangulation -> mean colour per facet with faint seams.",
  ["painterly", "raster"], [E("low_poly")], {"coarse": [E("low_poly", points=600)], "fine": [E("low_poly", points=4500)]}, slot="C")
P("pixel_art", "Pixel art",
  "Pixel art: area downscale to 128 columns, 24-colour Oklab k-means palette, integer nearest-neighbour upscale. Variants: 64 cells, 16 colours with Bayer dither.",
  ["painterly", "retro", "raster"], [E("pixel_art")], {"chunky": [E("pixel_art", cells=64, colors=16)], "dithered": [E("pixel_art", colors=16, dither="bayer")]}, slot="C")
P("mosaic_tesserae", "Mosaic",
  "Mosaic of tesserae: SLIC superpixels filled with mean colour, dark grout, per-tile light variation and bevel. Grid variant for square tiles.",
  ["painterly", "raster"], [E("mosaic")], {"grid": [E("mosaic", shape="grid", tiles=3000)]}, slot="C")
P("mondrian_grid", "Mondrian grid",
  "Mondrian-style: image-adaptive quadtree snapped to strong edges, leaves coloured white / red / blue / yellow (white-biased), thick black dividing lines.",
  ["painterly", "raster"], [E("mondrian", white_bias=1.6)], {"busy": [E("mondrian", max_depth=6, variance=0.04)]}, slot="C")
P("klimt_gold", "Gold-leaf ornament",
  "Klimt-style: the salient subject stays photographic, the background becomes gold-leaf SLIC tiles with spiral, circle and square motifs, metallic noise and glints.",
  ["painterly", "raster"], [E("klimt")], slot="C", note="Segmentation is saliency + GrabCut (heuristic); ~2 s at 1600 px.")

# ----------------------------------------------------------------------------- photographers
P("adams_zone_bw", "Zone-system B&W",
  "Ansel Adams-style: red-filter channel mix in linear light, full zone scale with textured Zone I and IX (no pure black), CLAHE and large-radius local contrast, burned blue skies, fine grain, no vignette.",
  ["photographer", "bw"], [E("adams_zone")], {"orange_filter": [E("adams_zone", filter="orange")], "neutral": [E("adams_zone", filter="neutral", sky_burn=0.0)]})
P("sugimoto_seascape", "Long-exposure seascape",
  "Hiroshi Sugimoto-style minimalism: horizon split, motion-smoothed water below and soft sky above, low-contrast B&W with lifted blacks and capped highlights, warm-neutral tone, no visible grain.",
  ["photographer", "bw"], [E("sugimoto_sea")])
P("leiter_street_colour", "Layered street colour",
  "Saul Leiter-inspired: muted global saturation with warm reds and yellows kept rich (reds toward orange), lifted midtones and blacks, green-cyan shadows, amber highlights, soft haze, colour grain.",
  ["photographer", "color"], [E("leiter")])
P("eggleston_dye_transfer", "Dye-transfer colour",
  "William Eggleston-inspired dye-transfer colour: dense saturated reds and greens, warm yellow-ish neutrals, strong tonal density, very fine grain.",
  ["photographer", "color"], [E("eggleston")])
P("mccurry_kodachrome", "Kodachrome-style travel",
  "Steve McCurry-inspired travel colour: S-curve with hard highlight clip, blue-tinted shadows, hot reds, deep true blues, crisp micro-contrast, fine mono grain.",
  ["photographer", "color", "film"], [E("mccurry")])
P("salgado_drama", "Dramatic documentary B&W",
  "Sebastiao Salgado-inspired: mix 0.45/0.45/0.10, heavy CLAHE local contrast, S-gain 8, burned skies, edge burn, rich pushed-film grain, warm-neutral print tone.",
  ["photographer", "bw"], [E("salgado")])
P("fan_ho_light", "Hard light and haze",
  "Fan Ho-inspired: S-gain 9 with deep shadows, haze and radial light rays from the brightest highlight, warm-neutral B&W.",
  ["photographer", "bw"], [E("fan_ho")])
P("moriyama_are_bure_boke", "Are-bure-boke (grain, blur, defocus)",
  "Daido Moriyama / Provoke-inspired: crushed high-contrast B&W, huge clumpy grain, random regional motion blur and defocus, tilt, vignette, dust and scratches.",
  ["photographer", "bw"], [E("moriyama")], {"calmer": [E("moriyama", grain=0.1, blur_len=8.0, dust=0.3)]})
P("maier_rolleiflex", "Rolleiflex street B&W",
  "Vivian Maier-inspired: long smooth mid-tones, moderate contrast, fine grain, warm paper highlights and soft near-black shadows (square crop optional via letterbox).",
  ["photographer", "bw"], [E("maier")], {"square": [E("letterbox", aspect=1.0, color="#000000")]})
P("crewdson_twilight", "Cinematic twilight tableau",
  "Gregory Crewdson-inspired: blue ambient shadows vs warm tungsten practicals, lifted blacks, fog, bloom, muted greens.",
  ["photographer", "cinema", "color"], [E("crewdson")])
P("lik_hdr_landscape", "HDR landscape",
  "Dramatic HDR landscape: base/detail local tone-mapping in log luminance, strong clarity, vivid saturation with warm sunset hues, deep blues, vignette.",
  ["photographer", "color"], [E("lik_hdr")])

# ----------------------------------------------------------------------------- cinema
P("teal_orange", "Teal and orange",
  "Blockbuster grade: shadows pushed toward teal, highlights toward orange (Oklch hue lerp), chroma boost, skin protected, S-gain 4.", ["cinema", "color"], [E("teal_orange", amount=0.5)],
  {"strong": [E("teal_orange", amount=0.7)], "subtle": [E("teal_orange", amount=0.3)]})
_wes = {"grand_budapest": "GrandBudapest2", "moonrise": "Moonrise3", "darjeeling": "Darjeeling1", "royal": "Royal1", "isle_of_dogs": "IsleofDogs2",
        "asteroid_city": "AsteroidCity1", "zissou": "Zissou1", "fantastic_fox": "FantasticFox1"}
P("wes_anderson_pastel", "Pastel palette remap",
  "Wes Anderson-style: k-means clusters remapped to an exact `wesanderson` palette (GrandBudapest2 pink/lavender by default), blended 55/45 with the original, lowered contrast, lifted blacks, warm. Variants swap the palette.",
  ["cinema", "color"], [E("wes_anderson", palette="GrandBudapest2")], {k: [E("wes_anderson", palette=v)] for k, v in _wes.items()})
P("blade_runner_haze", "Orange haze dystopia",
  "Blade Runner 2049-style: near-monochrome orange, fog veil, bloom, heavy contrast. The 'la' variant keeps neon over teal-black shadows.", ["cinema", "color"], [E("blade_runner")], {"la": [E("blade_runner", variant="la")]})
P("matrix_green", "Digital-rain green", "Matrix-style: desaturated with a green cast, deep green shadows, pale green highlights, cool skin, contrast gain 5.", ["cinema", "color"], [E("matrix_green")])
P("bleach_bypass", "Bleach bypass", "Bleach bypass: saturation -45% (range 35-59%), luminosity S-curve, B&W overlay, silvery desaturated highlights, grain.", ["cinema", "color"], [E("bleach_bypass")],
  {"strong": [E("bleach_bypass", desat=0.59, overlay=0.7)]})
P("day_for_night", "Day for night", "Day-for-night: -2.2 EV in linear light, Purkinje blue-green mix, deep-blue tint, desaturation; bright practicals keep warm colour.", ["cinema", "color"], [E("day_for_night")])
P("technicolor_3strip", "Three-strip Technicolor", "Technicolor 3-strip: dye-separation matrix, saturation x1.3, crushed cyan-tinted blacks, S-gain 6, mild halation and softening.", ["cinema", "color", "retro"], [E("technicolor_3strip")])
P("fincher_low_key", "Green-yellow low key", "Fincher-style: -0.7 EV, olive mid-tones, teal-black shadows, muted saturation 0.75, contrast 5, fine sharpening and grain.", ["cinema", "color"], [E("fincher")])
P("print_film_2383", "Print-film look (procedural)", "Kodak 2383-style procedural print emulation: cyan-leaning dense shadows, soft S-curve, warm highlights. For the real LUT, download the free LUTs and apply with lut3d.", ["cinema", "film", "color"], [E("print_film")], note=TM)

# ----------------------------------------------------------------------------- film stocks
def film(name, title, desc, stock, tags=("film", "color"), variants=None, extra=None, pre=None, **kw):
    stack = list(pre or []) + [E("film_emulation", stock=stock, **kw)] + list(extra or [])
    P(name, title, desc + " " + TM, list(tags), stack, variants, note=TM)


film("portra_160", "Portra 160 (emulation)", "Soft, fine-grained portrait negative: gentle S-curve, restrained saturation 0.88, cream highlight roll-off, olive greens.", "portra_160")
film("portra_400", "Portra 400 (emulation)", "Warm, low-contrast portrait negative: S-gain 3.5, lifted blacks 0.04, cream shoulder, sat 0.9, olive foliage, soft grain.", "portra_400",
     variants={"pushed": [E("film_emulation", stock="portra_800")]})
film("portra_800", "Portra 800 (emulation)", "Higher-ISO portrait negative: gain 4, visible grain (0.045), slightly green shadows.", "portra_800")
film("ektar_100", "Ektar 100 (emulation)", "Vivid fine-grain negative: S-gain 6, saturation x1.35, strong reds and deep blues, very fine grain.", "ektar_100")
film("kodachrome_64", "Kodachrome 64 (emulation)", "Slide-film classic: hot reds, deep blues, hard highlights, dense blue-leaning shadows, mono grain.", "kodachrome_64", tags=("film", "color", "retro"))
film("gold_200", "Gold 200 (emulation)", "Consumer warm negative: +400K warmth, rich yellows, sat 1.1, warm shadows, grain 0.03.", "gold_200", tags=("film", "color", "retro"))
film("tri_x_400", "Tri-X 400 (emulation)", "Classic B&W reportage film: mix 0.33/0.5/0.17, S-gain 6, strong grain. Variant: pushed (gain 9, heavy grain).", "tri_x_400", tags=("film", "bw"),
     variants={"pushed": [E("film_emulation", stock="tri_x_pushed")]})
film("hp5_plus", "HP5 Plus (emulation)", "Flexible B&W film: mix 0.3/0.55/0.15, gentler gain 4.5 than Tri-X, moderate grain.", "hp5_plus", tags=("film", "bw"))
film("velvia_50", "Velvia 50 (emulation)", "Super-saturated slide film: S-gain 7.5, sat x1.5, magenta-leaning reds, rich greens, deep blues.", "velvia_50")
film("provia_100f", "Provia 100F (emulation)", "Neutral slide film: S-gain 5, sat 1.15, slightly cyan blues, very fine grain.", "provia_100f")
film("pro_400h", "Pro 400H (emulation)", "Pastel mint negative: lifted blacks, minty shadows, greens toward cyan, airy pastel highlights.", "pro_400h")
film("superia_400", "Superia 400 (emulation)", "Everyday consumer negative: gain 4.5, punchy greens and cyans, warm reds, slightly magenta mid-tones.", "superia_400", tags=("film", "color", "retro"))
film("classic_chrome", "Classic Chrome (simulation)", "Muted documentary slide simulation: sat 0.75, brick reds, cyan-grey blues, desaturated highlights, fine mono grain.", "classic_chrome")
film("cinestill_800t", "800T tungsten (emulation)", "Tungsten-balanced cinema negative with strong red-orange halation around highlights (linear slot-A halation), cool blue daylight cast.", "cinestill_800t",
     pre=[E("halation", threshold=0.6, knee=0.4, radius=14, strength=0.9)], halation=0.0, tags=("film", "color", "cinema"))
film("polaroid_600", "Instant film 600 (emulation)", "Instant-film look: low contrast, lifted blacks, cyan shadows / warm highlights, magenta mid-tone, soft focus, vignette, white frame. Variant: cleaner cyan 'instax'.", "polaroid_600",
     tags=("film", "color", "retro"), extra=[E("border", style="polaroid", width=40, color="#F4EFE4")], variants={"instax": [E("film_emulation", stock="instax"), E("border", style="instax", width=34, color="#F8F5EE")]})
film("expired_film", "Expired film", "Expired film: lifted fog blacks, random magenta or green shadow cast, low contrast, heavy coloured grain, light leaks.", "expired", tags=("film", "color", "retro"),
     extra=[E("light_leak", strength=0.35, count=2)])
P("cross_process", "Cross-process", "E6 slide in C41 chemistry: per-channel curves give yellow-green highlights and cyan-blue shadows with high contrast and saturation. Variant: C41 in E6.",
  ["film", "process", "color", "retro"], [E("cross_process")], {"c41_in_e6": [E("cross_process", variant="c41_in_e6")]})

# ----------------------------------------------------------------------------- processes / lo-fi
P("cyanotype", "Cyanotype", "Prussian-blue print on paper: luminance gradient map #0B2545 -> #1D5B8C -> paper white, soft curve, paper tooth. Variant adds a brushed coating border.", ["process", "retro"], [E("cyanotype")],
  {"brushed_border": [E("cyanotype", brush_border=50)]})
P("sepia_albumen", "Sepia / albumen print", "Albumen print: gradient map #2B1A0E -> #8B5A2B -> #F2E6C9, lifted blacks, yellowed highlights, vignette and fine paper grain. Variant: classic sepia matrix.", ["process", "retro"], [E("sepia", style="albumen")],
  {"classic_matrix": [E("sepia", style="classic")]})
P("daguerreotype", "Daguerreotype", "Mirror-silver plate: blue-weighted mono, extreme detail, low Dmax, violet/bronze edge tarnish, oval vignette, scratches.", ["process", "retro"], [E("daguerreotype")])
P("tintype_wet_plate", "Wet-plate tintype", "Collodion tintype: blue-only spectral sensitivity (0/0.25/0.75 mix in linear slot A) - reds dark, eyes light - short tonal scale, pour marks, chemical edge fog, edge defocus, dust. Variant: ruby / gold toning.",
  ["process", "retro", "bw"], [E("spectral_mix", mode="mono", weights=[0.0, 0.25, 0.75]), E("wet_plate", spectral_mix=False)],
  {"ruby_gold": [E("wet_plate", tone="ruby_gold", spectral_mix=False)]}, slot="A+B")
P("platinum_palladium", "Platinum / palladium print", "Very long tonal scale, matte warm-neutral gradient #1C1A17 -> #5A4D40 -> #EFE9DD, fine grain, paper texture.", ["process", "bw", "retro"], [E("platinum")])
P("lith_print", "Lith print", "Lith print: peach soft highlights, hard cold shadows, grain concentrated in the shadows, split tone.", ["process", "bw"], [E("lith")])
P("infrared_bw", "Infrared B&W", "Wood-effect infrared: foliage glows, blue sky goes black, halation-like glow, waxy skin, HIE-like grain.", ["process", "bw"], [E("infrared_bw")])
P("aerochrome", "Infrared false colour", "Aerochrome-style: IR -> red, red -> green, green -> blue; foliage turns pink-red, skies cyan-blue. Variant: plain R<->B channel swap.", ["process", "color"], [E("aerochrome")],
  {"channel_swap": [E("aerochrome", style="channel_swap")]})
P("solarization", "Solarisation (Sabattier)", "Sabattier effect: tone curve folded above the threshold with Mackie-line edge halos. Variants: luminance-only or B&W.", ["process", "retro"], [E("solarize")],
  {"luma": [E("solarize", mode="luma")], "bw": [E("solarize", mode="bw")]})
P("lomo_toy_camera", "Toy camera", "Lomo / Holga-style: strong vignette, saturated punchy colour, blue shadows / warm highlights, chromatic aberration, light leaks. Variant: Holga corner defocus with cross-process.", ["process", "retro", "color"], [E("lomo")],
  {"holga": [E("lomo", holga=True, cross=0.5)]})
P("gum_bichromate", "Gum bichromate", "Three misregistered pigment layers on heavy paper with brush-edged borders, lifted blacks and muted saturation.", ["process", "retro"], [E("gum_bichromate")], slot="C")
P("risograph", "Risograph", "Two-ink Riso print: optical-density least-squares separation, AM dot screens + grain, per-layer misregistration, multiply overprint on cream paper. Variants: teal/red, blue/yellow.", ["process", "retro", "raster"],
  [E("risograph")], {"teal_red": [E("risograph", inks=["#00838A", "#F15060"])], "blue_yellow": [E("risograph", inks=["#0078BF", "#FFE800"])], "green_pink": [E("risograph", inks=["#00A95C", "#FF48B0"])]}, slot="C")
P("glitch", "Glitch", "Digital glitch: displaced row bands, RGB misalignment, JPEG crush with macroblock corruption, pixel sorting, scanlines, neon noise bands.", ["retro", "raster"], [E("glitch")], {"mild": [E("glitch", bands=5, corrupt=3, sort_bands=0)]}, slot="C")
P("vhs_tape", "VHS tape", "Consumer VHS: 480-line resample, bled chroma, crushed contrast, line noise, tracking bar, interlace comb, barrel distortion.", ["retro", "raster"], [E("vhs")], slot="C")
P("gameboy_camera", "Handheld pixel camera", "Game Boy Camera: 128-column 4-shade green ramp with 4x4 Bayer dither, integer upscale.", ["retro", "raster"], [E("gameboy")], {"no_dither": [E("gameboy", dither="none")]}, slot="C")
P("thermal_camera", "Thermal camera", "Thermal imager false colour: smoothed lightness at 320 px through an ironbow ramp with temperature bar. Variants: white-hot, rainbow.", ["process", "retro"], [E("thermal")],
  {"white_hot": [E("thermal", ramp="white_hot", legend=False)], "rainbow": [E("thermal", ramp="rainbow")]})
P("xray", "X-ray", "X-ray: B&W negative through a blue-white gradient with edge glow and mild vignette.", ["process"], [E("xray")])
P("negative", "Colour negative", "Inverted colour image. Variant: B&W negative.", ["process"], [E("xray", mode="negative")], {"bw": [E("xray", mode="negative_bw")]})

# ----------------------------------------------------------------------------- effect-based looks
P("halftone_cmyk", "CMYK halftone print", "Four rotated dot screens (15/75/0/45 deg) in CMYK inks on paper. Variants: single-screen B&W newsprint, large pitch.", ["pop", "retro", "raster"], [E("halftone", mode="cmyk", pitch=12)],
  {"newsprint": [E("halftone", mode="bw", pitch=10)], "big_dots": [E("halftone", mode="cmyk", pitch=22)]}, slot="C")
P("duotone_pop", "Duotone", "Gradient-map duotone (#1B1464 -> #FF2D55) in Oklab with contrast gain 3. Variants: selenium tritone, warm sepia tritone.", ["process", "retro", "color"], [E("duotone")],
  {"selenium": [E("duotone", shadow="#1A1A24", mid="#B8B4C8", highlight="#FFFFFF")], "warm_tritone": [E("duotone", shadow="#2B1A0E", mid="#8B5A2B", highlight="#F2E6C9")]})
P("orton_glow", "Orton glow", "Orton effect: screen+multiply duplicate blurred in at 35%, with highlight protection against dark halos.", ["painterly", "color"], [E("orton")], {"subtle": [E("orton", amount=0.15)]})
P("soft_focus_dream", "Soft focus", "Soft-focus lens: sharp core with blurred copy mixed in, highlight bleed and slight contrast drop.", ["retro", "color"], [E("soft_focus")])
P("tilt_shift_miniature", "Tilt-shift miniature", "Miniature-model look: blur grows away from a horizontal in-focus band, boosted saturation and contrast.", ["color"], [E("tilt_shift")])
P("crt_scanlines", "CRT scanlines", "CRT monitor: scanlines plus RGB aperture-grille triad, glow and slight chromatic shift.", ["retro", "raster"],
  [E("chromatic_shift", mode="offset", red_dx=1.5, blue_dx=-1.5), E("glow", strength=0.35), E("scanlines", period=4, strength=0.3, triad=0.12)], slot="C")
P("letterbox_scope", "Cinemascope letterbox", "2.39:1 letterbox bars over the frame with mild grain and vignette.", ["cinema"], [E("vignette", amount=-0.4), E("grain", amount=0.015), E("letterbox", aspect=2.39)], slot="C")
P("light_leak_film", "Light leaks", "Warm film light leaks screened over the frame with film grain.", ["film", "retro"], [E("light_leak", strength=0.5, count=3), E("grain", amount=0.03, chroma=0.8)])
P("halation_glow", "Highlight halation", "Linear-light halation: highlights above 0.6 bleed a red-orange glow (slot A) with a soft bloom, as in cinema negative stocks.", ["film", "cinema"],
  [E("halation", threshold=0.5, radius=22, strength=1.4), E("bloom", strength=0.25)], slot="A")
P("film_grain_400", "Film grain 400", "Luminance-dependent mono grain (sigma 0.04, 1.2 px, midtone-weighted) with a gentle vignette.", ["film", "retro"], [E("grain", amount=0.04, size=1.2), E("vignette", amount=-0.3)])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.yaml"):
        old.unlink()
    names = set()
    for d in PRESETS:
        assert d["name"] not in names, d["name"]
        names.add(d["name"])
        (OUT / f"{d['name']}.yaml").write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    print(f"wrote {len(PRESETS)} presets to {OUT}")


if __name__ == "__main__":
    main()
