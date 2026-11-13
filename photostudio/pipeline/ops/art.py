"""Art ops (raster / stylisation / film / process looks): this module only wires the family modules together so that
`from photostudio.pipeline.ops import art` registers every art op.

  art_pop      warhol, lichtenstein, fairey_hope, banksy_stencil, haring
  art_paint    painterly, van_gogh, monet, pointillism, oil_paint, watercolor, pencil_sketch, ink_xdog, cel_toon, low_poly,
               pixel_art, mosaic, mondrian, klimt
  art_film     film_emulation, photographer looks (adams_zone ... lik_hdr), cinema grades (teal_orange ... print_film)
  art_process  spectral_mix, cyanotype, sepia, platinum, lith, daguerreotype, wet_plate, infrared_bw, aerochrome, solarize,
               cross_process, lomo, risograph, gum_bichromate, glitch, vhs, gameboy, thermal, xray
Shared helpers: art_common (primitives), art_grade (table-driven grade engine). General effects live in effects.py."""
from __future__ import annotations

from . import effects  # noqa: F401  (grain, vignette, halation, ...)
from . import art_pop, art_paint, art_film, art_process  # noqa: F401

ART_OP_MODULES = (art_pop, art_paint, art_film, art_process)
