"""Masks: spec -> float32 HxW in 0..1, blend modes, previews.

    from photostudio.masks import build_mask, blend, BLEND_MODES, mask_preview

Sub-modules: drawn (linear/radial/polygon/brush), parametric (trapezoid, luminosity, colour range, depth band),
ai (subject/sky/face parts/animal/depth with fallbacks), cache (optional process-level AI mask cache), blend, build (dispatch, combine, refine, preview).
"""
from .ai import depth_map, detect_faces, saliency_map, upsample_guided
from .blend import BLEND_MODES, blend, blend_pixels, canonical_mode, effective_mode
from .build import build_mask, combine_masks, mask_preview, refine_mask
from .cache import cache_enabled, cache_stats, clear_cache, disable_cache, enable_cache, set_cache
from .common import MASK_TYPES, TYPE_ALIASES, normalise_spec
from .drawn import brush_mask, brush_stroke_alpha, linear_mask, polygon_mask, radial_mask
from .parametric import color_range_mask, depth_range_mask, luminosity_mask, parametric_mask, trapezoid

__all__ = [
    "build_mask", "blend", "BLEND_MODES", "mask_preview", "combine_masks", "refine_mask", "normalise_spec",
    "MASK_TYPES", "TYPE_ALIASES", "blend_pixels", "canonical_mode", "effective_mode", "depth_map", "detect_faces",
    "saliency_map", "upsample_guided", "brush_mask", "brush_stroke_alpha", "linear_mask", "polygon_mask",
    "set_cache", "enable_cache", "disable_cache", "clear_cache", "cache_stats", "cache_enabled",
    "radial_mask", "color_range_mask", "depth_range_mask", "luminosity_mask", "parametric_mask", "trapezoid",
]
