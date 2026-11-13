"""Film stocks, photographer looks and cinema grades (slot B, display-referred), built on `art_grade.grade`.

All parameter tables are *designed* starting points from research_notes/art_modes_styles.md (KQ3-KQ5) except the
`wesanderson` hex palettes, which are the exact values from the R package. Film / camera brand names are trademarks of
their owners; these are generic emulations. Every look exposes `intensity` (scales tint weights and grain)."""
from __future__ import annotations

import cv2
import numpy as np

from ... import color
from ...filters import motion_kernel
from ..registry import REGISTRY, Ctx, op
from . import art_common as ac
from . import effects as fx
from .art_common import F32, finish, luma, px, rng_for, rgb
from .art_grade import (angular_lerp, clahe_luma, grade, hdr_tonemap, sky_mask)

# =========================================================================== film stocks
def _g(amount, size=1.0, chroma=1.0, **kw):
    return dict(amount=amount, size=size, chroma=chroma, **kw)


STOCKS: dict[str, dict] = {
    "portra_160": dict(gain=3.0, black=0.04, sat=0.88, warm=0.12, high_tint=["#F5E9D3", 0.22, 0.85],
                       hsl=[dict(h=115, w=35, shift=-16, sat=0.85)], grain=_g(0.015, 0.8)),
    "portra_400": dict(gain=3.5, black=0.04, sat=0.90, warm=0.15, high_tint=["#F5E9D3", 0.30, 0.85],
                       hsl=[dict(h=115, w=35, shift=-20, sat=0.80)], grain=_g(0.025, 1.0)),
    "portra_800": dict(gain=4.0, black=0.045, sat=0.95, warm=0.15, high_tint=["#F5E9D3", 0.25, 0.85],
                       shadow_tint=["#1E2A1F", 0.06, 0.4], hsl=[dict(h=115, w=35, shift=-18, sat=0.85)], grain=_g(0.045, 1.3)),
    "ektar_100": dict(gain=6.0, black=0.0, sat=1.35, hsl=[dict(h=0, w=22, sat=1.15), dict(h=222, w=32, sat=1.15, lum=0.9)],
                      grain=_g(0.012, 0.7)),
    "kodachrome_64": dict(gain=7.0, black=0.0, white=0.98, sat=1.05, shadow_tint=["#101A33", 0.15, 0.25],
                          hsl=[dict(h=0, w=25, sat=1.2, lum=1.05), dict(h=205, w=35, shift=14, lum=0.92)], grain=_g(0.015, 0.8, 0.0)),
    "gold_200": dict(gain=4.0, black=0.03, sat=1.1, warm=0.4, shadow_tint=["#2A1E12", 0.08, 0.4],
                     hsl=[dict(h=50, w=25, sat=1.2)], grain=_g(0.03, 1.1)),
    "tri_x_400": dict(bw=[0.33, 0.5, 0.17], gain=6.0, black=0.02, grain=_g(0.05, 1.3, 0.0, clump=0.25)),
    "tri_x_pushed": dict(bw=[0.33, 0.5, 0.17], gain=9.0, black=0.0, grain=_g(0.08, 1.5, 0.0, clump=0.4, roughness=0.6)),
    "hp5_plus": dict(bw=[0.3, 0.55, 0.15], gain=4.5, black=0.02, grain=_g(0.045, 1.2, 0.0)),
    "velvia_50": dict(gain=7.5, black=0.0, sat=1.5,
                      hsl=[dict(h=0, w=22, shift=-5), dict(h=125, w=40, sat=1.3), dict(h=222, w=30, sat=1.15, lum=0.92)],
                      grain=_g(0.012, 0.7, 0.0)),
    "provia_100f": dict(gain=5.0, black=0.0, sat=1.15, hsl=[dict(h=222, w=30, shift=-8)], grain=_g(0.012, 0.7, 0.0)),
    "pro_400h": dict(gain=3.5, black=0.05, sat=0.95, shadow_tint=["#1F3A3A", 0.10, 0.4], high_tint=["#EEF3E8", 0.22, 0.8],
                     hsl=[dict(h=125, w=40, shift=12, sat=1.1)], grain=_g(0.03, 1.0)),
    "superia_400": dict(gain=4.5, black=0.02, sat=1.05, mid_tint=["#C060A0", 0.04],
                        hsl=[dict(h=140, w=50, sat=1.2), dict(h=0, w=25, sat=1.1)], grain=_g(0.04, 1.2)),
    "classic_chrome": dict(gain=4.0, black=0.03, sat=0.75, highlight_desat=[0.7, 0.3],
                           hsl=[dict(h=2, w=24, sat=0.88, lum=0.92, shift=4), dict(h=222, w=30, sat=0.6, shift=-14),
                                dict(h=24, w=14, sat=0.88)], grain=_g(0.015, 0.8, 0.0)),
    "cinestill_800t": dict(gain=4.0, black=0.05, sat=1.0, warm=-0.7, shadow_tint=["#2A4A7A", 0.10, 0.4],
                           high_tint=["#F4E7D0", 0.2, 0.8], halation=[0.6, 0.7, 14.0], grain=_g(0.05, 1.3)),
    "polaroid_600": dict(gain=2.5, black=0.12, white=0.92, sat=0.85, shadow_tint=["#2A5A66", 0.15, 0.45],
                         high_tint=["#F3E4C6", 0.15, 0.6], mid_tint=["#B05080", 0.04], soften=0.8,
                         vignette=[-0.3, 0.5, 0.7], grain=_g(0.03, 1.5)),
    "instax": dict(gain=2.8, black=0.09, white=0.94, sat=0.95, shadow_tint=["#2A6A78", 0.12, 0.45],
                   high_tint=["#F6EBD2", 0.12, 0.6], soften=0.6, vignette=[-0.2, 0.5, 0.7], grain=_g(0.015, 1.0)),
    "expired": dict(gain=2.5, black=0.10, sat=0.8, shadow_tint=["#7A3A6A", 0.2, 0.5], high_tint=["#E8D9A8", 0.1, 0.7],
                    grain=_g(0.07, 1.6, 1.0, roughness=0.6)),
    "cross_process": dict(gain=7.0, sat=1.35,
                          curves=dict(r=[(0, 0), (0.25, 0.22), (0.5, 0.52), (0.75, 0.85), (1, 1)],
                                      g=[(0, 0), (0.25, 0.2), (0.5, 0.52), (0.75, 0.8), (1, 1)],
                                      b=[(0, 0.04), (0.25, 0.37), (0.5, 0.5), (0.8, 0.65), (1, 0.85)]),
                          grain=_g(0.04, 1.2)),
}


@op("film_emulation", 18, "display", category="film", stock="portra_400", strength=1.0, intensity=1.0, saturation=1.0,
    contrast=1.0, warmth=0.0, grain=1.0, halation=-1.0, fade=0.0, seed=0, choices={"stock": list(STOCKS)})
def film_emulation(img, p, ctx: Ctx):
    """Parametric film-stock emulation (per-channel curves, colour shifts, saturation, grain, halation, fade). Generic
    emulation of Kodak / Fujifilm / Ilford / Polaroid / Cinestill looks; brand names belong to their owners.
    Overrides: saturation/contrast multiply the table, warmth/fade add, grain scales grain, halation>=0 sets glow strength."""
    rng = rng_for(ctx, p, 51)
    s = {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v)) for k, v in STOCKS[p["stock"]].items()}
    if s.get("sat") is not None:
        s["sat"] = s["sat"] * float(p["saturation"])
    if s.get("gain"):
        s["gain"] = s["gain"] * float(p["contrast"])
    s["warm"] = s.get("warm", 0.0) + float(p["warmth"])
    s["black"] = s.get("black", 0.0) + float(p["fade"])
    if p["halation"] >= 0:
        base = s.get("halation") or [0.0, 0.7, 14.0]
        s["halation"] = [float(p["halation"]), base[1], base[2]]
    if p["stock"] == "expired":
        s["shadow_tint"] = [str(rng.choice(["#7A3A6A", "#4A6A3A"])), 0.2, 0.5]
    out = grade(img, s, rng, ctx, float(p["intensity"]), float(p["grain"]))
    a = float(p["strength"])
    return finish(img * (1 - a) + out * a if a < 1 else out)


# =========================================================================== photographer looks
def _photo_op(name, doc, **defaults):
    def deco(fn):
        op(name, 18, "display", category="photographer", intensity=1.0, seed=0, **defaults)(fn)
        fn.__doc__ = doc
        REGISTRY[name].doc = doc
        return fn
    return deco


ZONE_FILTERS = {"red": [0.70, 0.35, -0.05], "orange": [0.55, 0.45, 0.0], "yellow": [0.45, 0.50, 0.05], "neutral": [0.2126, 0.7152, 0.0722]}


@_photo_op("adams_zone", "Zone-system B&W (Adams-style): red-filter mix in linear light, full tonal scale with textured Zone I/IX, "
           "local contrast, sky burn, fine grain; keeps Zone I texture (no pure black).", filter="red", sky_burn=0.35,
           local=0.4, clahe=0.4, gain=4.5, black=0.02, white=0.98, grain=0.015, choices={"filter": list(ZONE_FILTERS)})
def adams_zone(img, p, ctx):
    rng = rng_for(ctx, p, 52)
    sm = sky_mask(img, px(ctx, 20, img))
    s = dict(bw=ZONE_FILTERS[p["filter"]], bw_linear=True, gain=p["gain"], black=p["black"], white=p["white"],
             local=[40.0, float(p["local"])], grain=dict(amount=p["grain"], size=0.6, chroma=0.0))
    out = grade(img, s, rng, ctx, p["intensity"])
    out = clahe_luma(out, 2.0, 8, float(p["clahe"]) * p["intensity"])
    out = out * (1 - float(p["sky_burn"]) * sm)[..., None]
    return finish(out)


@_photo_op("sugimoto_sea", "Long-exposure seascape (Sugimoto-style): horizon split, smoothed water, low-contrast B&W with lifted "
           "blacks and capped highlights, warm-neutral tone.", water_blur=0.04, sky_blur=1.5, gain=3.0, black=0.06, white=0.92,
           horizon=-1.0)
def sugimoto_sea(img, p, ctx):
    rng = rng_for(ctx, p, 53)
    h, w = img.shape[:2]
    g = luma(img)
    if p["horizon"] >= 0:
        hy = int(float(p["horizon"]) * h)
    else:
        prof = ac.blur(g.mean(1).reshape(-1, 1), max(1.0, h / 200.0)).ravel()
        d = np.abs(np.diff(prof))
        lo, hi = int(h * 0.2), int(h * 0.8)
        hy = lo + int(np.argmax(d[lo:hi]))
    L = max(3, int(float(p["water_blur"]) * w))
    below = cv2.filter2D(g, -1, motion_kernel(L, 0.0), borderType=cv2.BORDER_REFLECT)
    below = ac.blur(below, px(ctx, 3, img))
    above = ac.blur(g, px(ctx, p["sky_blur"], img))
    t = ac.smoothstep(hy - max(2, h * 0.01), hy + max(2, h * 0.01), np.arange(h, dtype=F32))[:, None]
    base = above * (1 - t) + below * t
    s = dict(gain=p["gain"], black=p["black"], white=p["white"], toning=["#15130F", "#F0EDE8", 0.6], grain=dict(amount=0.006, size=0.8, chroma=0.0))
    return finish(grade(ac.gray3(base), s, rng, ctx, p["intensity"]))


@_photo_op("leiter", "Colour street look (Saul Leiter-style): muted overall, warm reds and yellows kept rich, lifted midtones and blacks, "
           "green-cyan shadows / amber highlights, soft haze and colour grain.", haze=0.04, grain=0.03)
def leiter(img, p, ctx):
    rng = rng_for(ctx, p, 54)
    s = dict(sat=0.7, hsl=[dict(h=0, w=22, sat=1.57, lum=0.9, shift=8), dict(h=40, w=22, sat=1.4)],
             curves=dict(l=[(0, 0.06), (0.25, 0.30), (0.5, 0.58), (0.75, 0.78), (1, 0.96)]),
             shadow_tint=["#2E5F5A", 0.12, 0.45], high_tint=["#E8B860", 0.10, 0.6], local=[30.0, 0.15], soften=0.6,
             fog=[float(p["haze"]), "#E8DFD0"], grain=dict(amount=p["grain"], size=1.0, chroma=1.0))
    return finish(grade(img, s, rng, ctx, p["intensity"]))


@_photo_op("eggleston", "Dye-transfer colour (Eggleston-style): dense saturated reds and greens, warm neutrals, strong tonal density, "
           "almost no grain.", saturation=1.25, gain=4.5, warm=0.3, grain=0.01)
def eggleston(img, p, ctx):
    rng = rng_for(ctx, p, 55)
    s = dict(sat=p["saturation"], hsl=[dict(h=0, w=20, sat=1.15, lum=0.85), dict(h=222, w=30, sat=0.9)], gain=p["gain"], warm=p["warm"],
             grain=dict(amount=p["grain"], size=0.8, chroma=0.5))
    return finish(grade(img, s, rng, ctx, p["intensity"]))


@_photo_op("mccurry", "Kodachrome-style travel colour: hard S-curve with hard highlight clip, blue-tinted shadows, hot reds, deep "
           "true blues, crisp micro-contrast, fine grain.", gain=7.0, grain=0.012, sharpen=0.6)
def mccurry(img, p, ctx):
    rng = rng_for(ctx, p, 56)
    s = dict(sat=1.1, hsl=[dict(h=0, w=25, sat=1.2, lum=1.05), dict(h=215, w=35, shift=10, lum=0.85)], gain=p["gain"], knee=[0.9, 0.98],
             shadow_tint=["#101A33", 0.15, 0.25], sharpen=[1.0, p["sharpen"]], grain=dict(amount=p["grain"], size=0.8, chroma=0.0))
    return finish(grade(img, s, rng, ctx, p["intensity"]))


@_photo_op("salgado", "Dramatic documentary B&W (Salgado-style): high contrast, heavy local contrast (CLAHE), burned skies, edge burn, "
           "rich pushed-film grain, warm-neutral print tone.", gain=8.0, clahe=0.6, sky_burn=0.45, grain=0.07)
def salgado(img, p, ctx):
    rng = rng_for(ctx, p, 57)
    sm = sky_mask(img, px(ctx, 20, img))
    s = dict(bw=[0.45, 0.45, 0.10], gain=p["gain"], black=0.0, vignette=[-0.35, 0.45, 0.7], toning=["#14110F", "#F2EDE3", 0.5],
             grain=dict(amount=p["grain"], size=1.4, chroma=0.0, clump=0.3, roughness=0.5))
    out = grade(img, s, rng, ctx, p["intensity"])
    out = out * (1 - float(p["sky_burn"]) * sm)[..., None]
    out = clahe_luma(out, 3.0, 8, float(p["clahe"]) * p["intensity"])
    return finish(out)


@_photo_op("fan_ho", "Hard-light Hong Kong B&W (Fan Ho-style): deep shadows, S-curve 9, haze and radial light rays from the brightest "
           "highlight, warm-neutral tone.", gain=9.0, rays=0.5, haze=0.08, ray_threshold=0.85, samples=36)
def fan_ho(img, p, ctx):
    rng = rng_for(ctx, p, 58)
    h, w = img.shape[:2]
    s = dict(bw=[0.3, 0.59, 0.11], gain=p["gain"], black=0.0, toning=["#14110F", "#F1ECE2", 0.45], grain=dict(amount=0.025, size=1.0, chroma=0.0))
    g = grade(img, s, rng, ctx, p["intensity"])
    L = luma(g)
    cy, cx = (float(v) for v in np.unravel_index(np.argmax(ac.blur(L, px(ctx, 30, img))), L.shape))
    t0 = float(p["ray_threshold"])
    m = np.clip((L - t0) / max(1e-3, 1 - t0), 0, 1)
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    acc = np.zeros_like(m)
    n = int(p["samples"])
    for i in range(1, n + 1):
        sc = 1.0 - i * (0.5 / n)
        acc += (0.97 ** i) * cv2.remap(m, cx + (xx - cx) * sc, cy + (yy - cy) * sc, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    acc = acc / (acc.max() + 1e-6)
    out = ac.screen(L, np.clip(acc * float(p["rays"]) * p["intensity"], 0, 1)) + float(p["haze"]) * p["intensity"] * (1 - L)
    return finish(ac.gray3(np.clip(out, 0, 1)) * (g / (L[..., None] + 1e-3)).clip(0.5, 1.5))


@_photo_op("moriyama", "Are-bure-boke (Moriyama / Provoke-style): crushed high-contrast B&W, huge clumpy grain, random motion blur and "
           "defocus, tilt, vignette and dust.", gain=10.0, grain=0.16, blur_len=14.0, defocus=3.0, tilt=3.0, dust=1.0)
def moriyama(img, p, ctx):
    rng = rng_for(ctx, p, 59)
    h, w = img.shape[:2]
    g = ac.gray3(np.clip(ac.bw_mix(img, [0.3, 0.59, 0.11]), 0, 1))
    L = px(ctx, rng.uniform(0.6, 1.4) * float(p["blur_len"]), img)
    ang = float(rng.uniform(0, 180))
    mb = cv2.filter2D(g, -1, motion_kernel(max(3, L), ang), borderType=cv2.BORDER_REFLECT)
    m1 = ac.smoothstep(-0.2, 0.6, ac.noise_unit((h, w), rng, px(ctx, 150, img)))[..., None]
    g = g * (1 - m1 * p["intensity"]) + mb * m1 * p["intensity"]
    m2 = ac.smoothstep(0.2, 1.0, ac.noise_unit((h, w), rng, px(ctx, 220, img)))[..., None]
    g = g * (1 - m2 * 0.7 * p["intensity"]) + ac.blur(g, px(ctx, p["defocus"], img)) * m2 * 0.7 * p["intensity"]
    tilt = float(rng.uniform(-1, 1)) * float(p["tilt"])
    if abs(tilt) > 0.05:
        M = cv2.getRotationMatrix2D(((w - 1) / 2, (h - 1) / 2), tilt, 1.04)
        g = cv2.warpAffine(g, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    l = np.clip((g[..., 0] - 0.08) / 0.84, 0, 1)
    s = dict(gain=float(p["gain"]), vignette=[-0.5, 0.4, 0.7],
             grain=dict(amount=p["grain"], size=2.2, chroma=0.0, clump=1.0, roughness=0.6))
    out = grade(ac.gray3(l), s, rng, ctx, p["intensity"])
    if p["dust"] > 0:
        out = fx.dust_scratches(out, REGISTRY["dust_scratches"].resolve_params({"strength": 0.5 * p["dust"], "scratches": 5, "dust": 140, "hairs": 3}), ctx)
    return finish(out)


@_photo_op("maier", "Rolleiflex-era street B&W (Maier-style): long smooth mid-tones, moderate contrast, fine grain, mild warm paper tone.",
           gain=4.0, grain=0.025)
def maier(img, p, ctx):
    rng = rng_for(ctx, p, 60)
    s = dict(bw=[0.3, 0.59, 0.11], gain=p["gain"], black=0.03, toning=["#15110F", "#F3EEE4", 0.5], grain=dict(amount=p["grain"], size=1.0, chroma=0.0))
    return finish(grade(img, s, rng, ctx, p["intensity"]))


@_photo_op("crewdson", "Cinematic twilight tableau (Crewdson-style): blue ambient shadows, warm tungsten practicals, muted greens, "
           "lifted blacks, fog and bloom.", fog=0.10, saturation=0.8, gain=3.0)
def crewdson(img, p, ctx):
    rng = rng_for(ctx, p, 61)
    hh = ac.hsv(img)
    prac = ac.smoothstep(0.7, 1.0, luma(img)) * ac.smoothstep(0.2, 0.5, hh[..., 1])
    s = dict(sat=p["saturation"], hsl=[dict(h=115, w=40, sat=0.7)], gain=p["gain"], black=0.07,
             shadow_tint=["#1F3A5F", 0.25, 0.5], fog=[float(p["fog"]), "#9DB0C8"], bloom=[0.8, 30.0, 0.15],
             grain=dict(amount=0.015, size=1.0, chroma=0.5))
    out = grade(img, s, rng, ctx, p["intensity"])
    out = ac.tint_toward(out, "#F2B36B", 0.2 * p["intensity"], prac)
    return finish(out)


@_photo_op("lik_hdr", "Dramatic HDR landscape: base/detail local tone-mapping, strong clarity, vivid saturation with warm sunset hues, "
           "deep blues and a vignette.", compress=0.65, detail=1.5, clarity=0.7, saturation=1.4)
def lik_hdr(img, p, ctx):
    rng = rng_for(ctx, p, 62)
    x = hdr_tonemap(img, float(p["compress"]), float(p["detail"]), px(ctx, 25, img))
    hh = ac.hsv(x)
    blue = ac.ang_weight(hh[..., 0], 225, 35) * ac.smoothstep(0.2, 0.5, hh[..., 1])
    x = ac.tint_toward(x, "#1B4F9C", 0.15 * p["intensity"], blue)
    s = dict(sat=p["saturation"], hsl=[dict(h=35, w=18, sat=1.2)], local=[25.0, float(p["clarity"])], vignette=[-0.4, 0.45, 0.7],
             grain=dict(amount=0.008, size=0.8, chroma=0.0))
    return finish(grade(x, s, rng, ctx, p["intensity"]))


# =========================================================================== cinema looks
def _cin_op(name, doc, **defaults):
    def deco(fn):
        op(name, 18, "display", category="cinema", intensity=1.0, seed=0, **defaults)(fn)
        REGISTRY[name].doc = doc
        fn.__doc__ = doc
        return fn
    return deco


@_cin_op("teal_orange", "Teal & orange grade: shadows pushed toward teal, highlights toward orange in Oklch (angular hue lerp), "
         "skin protected, S-curve contrast.", amount=0.35, skin_protect=0.7, gain=4.0, chroma_boost=1.0,
         shadow_hue=195.0, highlight_hue=58.0, shadow_color="#1B5E6B", highlight_color="#F2A65A")
def teal_orange(img, p, ctx):
    rng = rng_for(ctx, p, 63)
    x = np.clip(img, 0, 1)
    L = luma(x)
    ws, wh = ac.smoothstep(0.55, 0.0, L), ac.smoothstep(0.45, 1.0, L)
    hh = ac.hsv(x)
    skin = ac.ang_weight(hh[..., 0], 25.0, 15.0, 12.0) * ac.smoothstep(0.15, 0.3, hh[..., 1]) * (1 - ac.smoothstep(0.55, 0.7, hh[..., 1]))
    prot = 1 - float(p["skin_protect"]) * skin
    lch = color.oklab_to_oklch(ac.oklab(x))
    a = float(p["amount"]) * float(p["intensity"])
    H = lch[..., 2]
    H = angular_lerp(H, float(p["shadow_hue"]), np.clip(ws * a * prot, 0, 1))
    H = angular_lerp(H, float(p["highlight_hue"]), np.clip(wh * a * prot, 0, 1))
    lch[..., 2] = H
    lch[..., 1] *= 1 + float(p["chroma_boost"]) * (0.25 * ws + 0.15 * wh) * float(p["intensity"])
    lab = color.oklch_to_oklab(lch)
    # near-neutral pixels have no hue to rotate: also pull chroma toward the teal / orange target
    tab_s = ac.oklab(rgb(p["shadow_color"]).reshape(1, 1, 3))[0, 0, 1:]
    tab_h = ac.oklab(rgb(p["highlight_color"]).reshape(1, 1, 3))[0, 0, 1:]
    ts = np.clip(ws * a * prot * 1.1, 0, 1)[..., None]
    th = np.clip(wh * a * prot * 1.1, 0, 1)[..., None]
    lab[..., 1:] = lab[..., 1:] * (1 - ts) + tab_s * ts
    lab[..., 1:] = lab[..., 1:] * (1 - th) + tab_h * th
    out = np.clip(ac.from_oklab(lab), 0, 1)
    s = dict(gain=p["gain"], black=0.01)
    return finish(grade(out, s, rng, ctx, p["intensity"]))


WES = {
    "Zissou1": ["#3B9AB2", "#78B7C5", "#EBCC2A", "#E1AF00", "#F21A00"],
    "GrandBudapest1": ["#F1BB7B", "#FD6467", "#5B1A18", "#D67236"],
    "GrandBudapest2": ["#E6A0C4", "#C6CDF7", "#D8A499", "#7294D4"],
    "Moonrise1": ["#F3DF6C", "#CEAB07", "#D5D5D3", "#24281A"],
    "Moonrise2": ["#798E87", "#C27D38", "#CCC591", "#29211F"],
    "Moonrise3": ["#85D4E3", "#F4B5BD", "#9C964A", "#CDC08C", "#FAD77B"],
    "Darjeeling1": ["#FF0000", "#00A08A", "#F2AD00", "#F98400", "#5BBCD6"],
    "Darjeeling2": ["#ECCBAE", "#046C9A", "#D69C4E", "#ABDDDE", "#000000"],
    "FantasticFox1": ["#DD8D29", "#E2D200", "#46ACC8", "#E58601", "#B40F20"],
    "Royal1": ["#899DA4", "#C93312", "#FAEFD1", "#DC863B"],
    "Royal2": ["#9A8822", "#F5CDB4", "#F8AFA8", "#FDDDA0", "#74A089"],
    "Rushmore1": ["#E1BD6D", "#EABE94", "#0B775E", "#35274A", "#F2300F"],
    "BottleRocket1": ["#A42820", "#5F5647", "#9B110E", "#3F5151", "#4E2A1E", "#550307", "#0C1707"],
    "BottleRocket2": ["#FAD510", "#CB2314", "#273046", "#354823", "#1E1E1E"],
    "Chevalier1": ["#446455", "#FDD262", "#D3DDDC", "#C7B19C"],
    "Cavalcanti1": ["#D8B70A", "#02401B", "#A2A475", "#81A88D", "#972D15"],
    "IsleofDogs1": ["#9986A5", "#79402E", "#CCBA72", "#0F0D0E", "#D9D0D3", "#8D8680"],
    "IsleofDogs2": ["#EAD3BF", "#AA9486", "#B6854D", "#39312F", "#1C1718"],
    "FrenchDispatch": ["#90D4CC", "#BD3027", "#B0AFA2", "#7FC0C6", "#9D9C85"],
    "AsteroidCity1": ["#0A9F9D", "#CEB175", "#E54E21", "#6C8645", "#C18748"],
    "AsteroidCity2": ["#C52E19", "#AC9765", "#54D8B1", "#b67c3b", "#175149", "#AF4E24"],
    "AsteroidCity3": ["#FBA72A", "#D3D4D8", "#CB7A5C", "#5785C1"],
    "Zissou1Continuous": ["#3A9AB2", "#6FB2C1", "#91BAB6", "#A5C2A3", "#BDC881", "#DCCB4E", "#E3B710", "#E79805", "#EC7A05", "#EF5703", "#F11B00"],
}


@_cin_op("wes_anderson", "Pastel palette remap: k-means clusters mapped to the nearest colour of an exact `wesanderson` palette "
         "(MIT-licensed R package hex values), blended 55/45 with the original, lowered contrast, lifted blacks, warm.",
         palette="GrandBudapest2", k=6, blend=0.45, flat=0.0, soften_edges=8.0, gain=3.0, black=0.06, saturation=0.9, warm=0.2,
         grain=0.015, choices={"palette": list(WES)})
def wes_anderson(img, p, ctx):
    rng = rng_for(ctx, p, 64)
    pl = ac.pal(WES[p["palette"]])
    centers, labels = ac.kmeans_cluster(ac.oklab(img), int(p["k"]), int(p["seed"]) + 13)
    k = len(centers)
    means, _ = ac.label_means(img, labels, k)
    target = pl[ac.nearest_palette(means.reshape(1, k, 3), pl)[0]]
    flat = target[labels]
    shift = ac.blur((target - means)[labels], px(ctx, p["soften_edges"], img))
    shifted = np.clip(img + shift, 0, 1)
    fl = float(p["flat"])
    remap = shifted * (1 - fl) + flat * fl
    b = float(p["blend"]) * float(p["intensity"])
    out = img * (1 - b) + remap * b
    s = dict(gain=p["gain"], black=p["black"], sat=p["saturation"], warm=p["warm"], grain=dict(amount=p["grain"], size=1.0, chroma=0.3))
    return finish(grade(out, s, rng, ctx, p["intensity"]) if p["gain"] or p["grain"] else out)


@_cin_op("blade_runner", "Orange-haze dystopia (Blade Runner 2049-style): near-monochrome orange, fog veil, bloom, heavy contrast; "
         "'la' variant keeps magenta/cyan neon over teal-black shadows.", variant="orange", mono_mix=0.6, fog=0.25, gain=2.5,
         choices={"variant": ["orange", "la"]})
def blade_runner(img, p, ctx):
    rng = rng_for(ctx, p, 65)
    x = np.clip(img, 0, 1)
    I = float(p["intensity"])
    l = luma(x)[..., None]
    if p["variant"] == "orange":
        mono = l * rgb("#E07A1F") * 1.6
        x = x * (1 - p["mono_mix"] * I) + np.clip(mono, 0, 1) * p["mono_mix"] * I
        f = (float(p["fog"]) + 0.25 * (1 - l)) * I
        x = x * (1 - f) + rgb("#D97B2B") * f
        s = dict(gain=p["gain"], bloom=[0.75, 40.0, 0.2], grain=dict(amount=0.02, size=1.0, chroma=0.0))
    else:
        hh = ac.hsv(x)
        neon = ac.smoothstep(0.45, 0.7, hh[..., 1])[..., None]
        base = ac.tint_toward(ac.saturate(x, 0.5), "#0E2F3F", 0.35 * I, 1.0 - ac.smoothstep(0.0, 0.5, luma(x)))
        x = base * (1 - neon) + ac.saturate(x, 1.2) * neon
        s = dict(gain=p["gain"], fog=[0.12, "#2A5A66"], bloom=[0.75, 40.0, 0.25], grain=dict(amount=0.02, size=1.0, chroma=0.0))
    return finish(grade(np.clip(x, 0, 1), s, rng, ctx, I))


@_cin_op("matrix_green", "Green-tinted digital-rain grade: desaturated, green cast, deep green shadows and pale green highlights, "
         "cool skin, high contrast.", saturation=0.55, gain=5.0, cast=0.08)
def matrix_green(img, p, ctx):
    rng = rng_for(ctx, p, 66)
    x = np.clip(img, 0, 1)
    I = float(p["intensity"])
    x = ac.saturate(x, float(p["saturation"]))
    l = luma(x)[..., None]
    x = x * (np.array([0.9, 1.0, 0.85], F32) * I + (1 - I)) + l * float(p["cast"]) * I * np.array([0, 1, 0], F32)
    s = dict(gain=p["gain"], shadow_tint=["#0B2A1A", 0.20, 0.5], high_tint=["#D8F3C8", 0.15, 0.65],
             hsl=[dict(h=10, w=25, sat=0.9)], grain=dict(amount=0.02, size=1.0, chroma=0.0))
    return finish(grade(np.clip(x, 0, 1), s, rng, ctx, I))


@_cin_op("bleach_bypass", "Bleach bypass: saturation cut by 35-59% with a luminosity S-curve and an overlaid B&W layer; silvery, gritty highlights.",
         desat=0.45, overlay=0.5, grain=0.03)
def bleach_bypass(img, p, ctx):
    rng = rng_for(ctx, p, 67)
    x = np.clip(img, 0, 1)
    I = float(p["intensity"])
    base = ac.saturate(x, 1.0 - float(p["desat"]) * I)
    base = np.stack([ac.apply_curve(base[..., i], ac.curve_lut([(0, 0), (0.25, 0.18), (0.5, 0.5), (0.75, 0.82), (1, 1)])) for i in range(3)], -1)
    g = ac.gray3(luma(x))
    ov = np.where(base < 0.5, 2 * base * g, 1 - 2 * (1 - base) * (1 - g))
    out = base * (1 - float(p["overlay"]) * I) + ov * float(p["overlay"]) * I
    s = dict(highlight_desat=[0.8, 0.3], grain=dict(amount=p["grain"], size=1.0, chroma=0.0))
    return finish(grade(np.clip(out, 0, 1), s, rng, ctx, I))


@_cin_op("day_for_night", "Day-for-night: -2.5 EV in linear light, Purkinje blue-green B&W mix, deep-blue tint, desaturation, "
         "bright practicals keep warm colour, blue sky gradient.", ev=-2.2, purkinje=0.6, saturation=0.5, gain=2.0, grain=0.03)
def day_for_night(img, p, ctx):
    rng = rng_for(ctx, p, 68)
    x = np.clip(img, 0, 1)
    I = float(p["intensity"])
    prac = ac.smoothstep(0.85, 0.98, luma(x))
    dark = ac.enc(ac.lin(x) * 2.0 ** (float(p["ev"]) * I))
    g = ac.gray3(np.clip(ac.bw_mix(dark, [0.1, 0.6, 0.3]), 0, 1))
    out = dark * (1 - float(p["purkinje"]) * I) + g * float(p["purkinje"]) * I
    h = out.shape[0]
    sky = (np.linspace(1, 0, h, dtype=F32) ** 2)[:, None, None] * rgb("#2A4A8A") * 0.06 * I
    out = out + sky
    out = ac.tint_toward(out, "#1B2A4A", 0.35 * I)
    out = ac.saturate(out, 1 - (1 - float(p["saturation"])) * I)
    warm_src = ac.enc(ac.lin(x) * 2.0 ** 0.5)
    out = out * (1 - prac[..., None]) + np.clip(warm_src, 0, 1) * prac[..., None]
    s = dict(gain=p["gain"], grain=dict(amount=p["grain"], size=1.0, chroma=0.0))
    return finish(grade(np.clip(out, 0, 1), s, rng, ctx, I))


@_cin_op("technicolor_3strip", "Three-strip Technicolor: dye-separation matrix, hyper-saturated CMY-style primaries, crushed cyan-tinted "
         "blacks, S-curve, mild halation and softening.", saturation=1.3, gain=6.0, halation=0.15, soften=0.7)
def technicolor_3strip(img, p, ctx):
    rng = rng_for(ctx, p, 69)
    s = dict(matrix=[[1.3, -0.15, -0.15], [-0.15, 1.3, -0.15], [-0.15, -0.15, 1.3]], sat=p["saturation"], gain=p["gain"], black=0.0,
             shadow_tint=["#0B2B33", 0.15, 0.25], halation=[p["halation"], 0.7, 12.0], soften=p["soften"],
             grain=dict(amount=0.01, size=0.8, chroma=0.0))
    return finish(grade(img, s, rng, ctx, p["intensity"]))


@_cin_op("fincher", "Green-yellow low-key grade (Fincher-style): -0.7 EV, olive mid-tones, teal-black shadows, muted saturation, "
         "contrast, fine sharpening and grain.", ev=-0.7, saturation=0.75, gain=5.0)
def fincher(img, p, ctx):
    rng = rng_for(ctx, p, 70)
    s = dict(ev=p["ev"], sat=p["saturation"], gain=p["gain"], mid_tint=["#8A8F3A", 0.15], shadow_tint=["#0F1A1F", 0.20, 0.4],
             high_tint=["#E5E0B0", 0.10, 0.65], sharpen=[0.8, 0.4], grain=dict(amount=0.02, size=1.0, chroma=0.0))
    return finish(grade(img, s, rng, ctx, p["intensity"]))


@_cin_op("print_film", "Print-film (2383-style) procedural look: dense cyan-leaning shadows, soft S-curve, warm highlights, slightly "
         "reduced blue luminance. For the real LUT download Kodak 2383 / Fuji 3510 LUTs and use lut3d.", gain=6.0, saturation=1.08)
def print_film(img, p, ctx):
    rng = rng_for(ctx, p, 71)
    s = dict(gain=p["gain"], black=0.02, sat=p["saturation"], hsl=[dict(h=225, w=40, lum=0.9)],
             shadow_tint=["#0A1C24", 0.15, 0.35], high_tint=["#F4EAD5", 0.08, 0.7], grain=dict(amount=0.012, size=0.9, chroma=0.5))
    return finish(grade(img, s, rng, ctx, p["intensity"]))


ac.finalize_docs(__name__)
