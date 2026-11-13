"""Photographic processes and lo-fi / digital artefact ops: cyanotype, sepia / albumen, platinum, lith, daguerreotype,
wet-plate tintype, infrared (B&W + Aerochrome false colour), solarisation, toy camera (Lomo / Holga), cross-process,
risograph, gum bichromate, glitch, VHS, Game Boy camera, thermal, X-ray / negative, plus the linear slot-A `spectral_mix`.
Recipes: research_notes/art_modes_styles.md KQ6 (palettes are designed starting points)."""
from __future__ import annotations

import cv2
import numpy as np

from ... import color
from ..registry import REGISTRY, Ctx, op
from . import art_common as ac
from . import effects as fx
from .art_common import F32, finish, luma, px, rng_for, rgb
from .art_film import STOCKS
from .art_grade import grade, skin_mask

# =========================================================================== slot A: spectral channel mix (linear)
@op("spectral_mix", 15, "linear", category="optics", mode="mono", weights=[0.0, 0.25, 0.75],
    matrix=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], mix=1.0, choices={"mode": ["mono", "matrix"]})
def spectral_mix(img, p, ctx: Ctx):
    """Spectral channel mixing in scene-linear light (slot A): mono weights (orthochromatic wet plate 0/0.25/0.75, red-filter
    zone-system 0.7/0.35/-0.05, IR synth 0.65/0.35/0) or an arbitrary 3x3 matrix."""
    if p["mode"] == "mono":
        w = np.asarray(p["weights"], F32)
        g = np.maximum(img[..., 0] * w[0] + img[..., 1] * w[1] + img[..., 2] * w[2], 0)
        out = np.repeat(g[..., None], 3, axis=2)
    else:
        out = color.apply_matrix(img, np.asarray(p["matrix"], F32))
    a = float(p["mix"])
    return (img * (1 - a) + out * a).astype(F32) if a < 1 else out.astype(F32)


# =========================================================================== toned monochrome processes
def _mask_border(h, w, rng, width, softness=0.5):
    """Brush-edged coating border mask (1 inside, 0 outside) with a noisy edge."""
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    d = np.minimum(np.minimum(xx, w - 1 - xx), np.minimum(yy, h - 1 - yy))
    n = ac.noise_unit((h, w), rng, max(2.0, width * 0.2), 3)
    return ac.smoothstep(0.0, max(width * softness, 1.0), d + n * width * 0.35 - width * 0.2)


@op("cyanotype", 18, "display", category="process", shadow="#0B2545", mid="#1D5B8C", paper="#F4F0E6", mid_pos=0.55, gain=3.0,
    paper_texture=0.05, blur=0.6, brush_border=0.0, intensity=1.0, seed=0)
def cyanotype(img, p, ctx: Ctx):
    """Cyanotype: luminance gradient map Prussian-blue -> paper white (soft printed-on-paper curve), paper tooth, optional brushed border."""
    rng = rng_for(ctx, p, 81)
    h, w = img.shape[:2]
    l = ac.sgain(np.clip(ac.blur(luma(img), px(ctx, p["blur"], img)), 0, 1), float(p["gain"]))
    lut = ac.make_lut([(0.0, p["shadow"]), (float(p["mid_pos"]), p["mid"]), (1.0, p["paper"])])
    out = ac.apply_lut(l, lut)
    if p["paper_texture"] > 0:
        out = out * ac.paper_multiplier(h, w, rng, float(p["paper_texture"]), max(0.5, max(h, w) / 2000.0), 0.3)[..., None]
    if p["brush_border"] > 0:
        m = _mask_border(h, w, rng, px(ctx, p["brush_border"], img))[..., None]
        out = rgb(p["paper"]) * (1 - m) + out * m
    a = float(p["intensity"])
    return finish(img * (1 - a) + out * a if a < 1 else out)


SEPIA_M = np.array([[0.393, 0.769, 0.189], [0.349, 0.686, 0.168], [0.272, 0.534, 0.131]], F32)


@op("sepia", 18, "display", category="process", style="classic", strength=0.9, albumen=["#2B1A0E", "#8B5A2B", "#F2E6C9"], gain=3.5,
    black=0.08, vignette=-0.3, grain=0.012, intensity=1.0, seed=0, choices={"style": ["classic", "albumen"]})
def sepia(img, p, ctx: Ctx):
    """Sepia: the classic W3C sepia matrix, or an albumen-print gradient map with yellowed highlights, lifted blacks, vignette, paper grain."""
    rng = rng_for(ctx, p, 82)
    if p["style"] == "classic":
        out = np.clip(color.apply_matrix(img, SEPIA_M), 0, 1)
        out = img * (1 - float(p["strength"])) + out * float(p["strength"])
        return finish(out)
    l = ac.tone(np.clip(luma(img), 0, 1), float(p["gain"]), float(p["black"]), 1.0)
    out = ac.apply_lut(l, ac.make_lut(p["albumen"]))
    out = ac.apply_vignette(out, float(p["vignette"]), 0.45, 0.2, 0.7)
    out = ac.add_grain(out, rng, float(p["grain"]) * p["intensity"], max(0.4, px(ctx, 0.8, img)), 0.0)
    return finish(out)


@op("platinum", 18, "display", category="process", gain=2.5, black=0.08, white=0.95, stops=["#1C1A17", "#5A4D40", "#EFE9DD"],
    grain=0.012, paper_texture=0.04, intensity=1.0, seed=0)
def platinum(img, p, ctx: Ctx):
    """Platinum/palladium print: very long tonal scale, matte warm-neutral gradient map, fine grain, paper texture."""
    rng = rng_for(ctx, p, 83)
    h, w = img.shape[:2]
    l = ac.tone(np.clip(luma(img), 0, 1), float(p["gain"]), float(p["black"]), float(p["white"]))
    out = ac.apply_lut(np.clip((l - p["black"]) / max(p["white"] - p["black"], 1e-3), 0, 1), ac.make_lut(p["stops"]))
    out = ac.add_grain(out, rng, float(p["grain"]) * p["intensity"], max(0.4, px(ctx, 0.8, img)), 0.0)
    out = out * ac.paper_multiplier(h, w, rng, float(p["paper_texture"]), max(0.5, max(h, w) / 2000.0), 0.35)[..., None]
    return finish(out)


@op("lith", 18, "display", category="process", shadow="#2A1A14", highlight="#F2C9A0", grain=0.09, grain_size=1.6, warmth=0.1,
    intensity=1.0, seed=0)
def lith(img, p, ctx: Ctx):
    """Lith print: soft peach highlights, hard cold shadows (steep toe, compressed shoulder), infectious-development grain concentrated in shadows, split tone."""
    rng = rng_for(ctx, p, 84)
    l = np.clip(luma(img), 0, 1)
    l = ac.apply_curve(l, ac.curve_lut([(0, 0.0), (0.1, 0.02), (0.25, 0.13), (0.4, 0.42), (0.5, 0.55), (0.7, 0.72), (0.85, 0.84), (1, 0.92)]))
    out = ac.gray3(l)
    out = ac.add_grain(out, rng, float(p["grain"]) * p["intensity"], max(0.5, px(ctx, p["grain_size"], img)), 0.0, 0.6, 0.0, 1.0)
    lut = ac.make_lut([(0.0, p["shadow"]), (0.5, ac.rgb("#8A6A58")), (1.0, p["highlight"])])
    toned = ac.apply_lut(np.clip(luma(out), 0, 1), lut)
    out = out * (1 - 0.8 * p["intensity"]) + toned * 0.8 * p["intensity"]
    return finish(ac.warm(out, float(p["warmth"])))


@op("daguerreotype", 18, "display", category="process", mix=[0.1, 0.4, 0.5], sharpen=1.0, black=0.12, tarnish=0.3, scratches=5,
    vignette=-0.7, mirror=False, intensity=1.0, seed=0)
def daguerreotype(img, p, ctx: Ctx):
    """Daguerreotype: blue-weighted mono with extreme detail, mirror-silver tonality, violet/bronze edge tarnish, oval vignette, fine scratches."""
    rng = rng_for(ctx, p, 85)
    h, w = img.shape[:2]
    x = img[:, ::-1] if p["mirror"] else img
    g = np.clip(ac.bw_mix(x, p["mix"]), 0, 1)
    g = np.clip(g + float(p["sharpen"]) * (g - ac.blur(g, max(0.6, px(ctx, 1.4, img)))), 0, 1)
    g = float(p["black"]) + (1 - float(p["black"])) * ac.sgain(g, 2.5)
    lut = ac.make_lut([(0.0, "#2E3238"), (0.5, "#8E939A"), (0.95, "#D9DCE0"), (1.0, "#E8DFCB")])
    out = ac.apply_lut(np.clip(g, 0, 1), lut)
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    r = np.hypot((xx / w - 0.5) / 0.5, (yy / h - 0.5) / 0.5) / np.sqrt(2.0)
    n = ac.noise_unit((h, w), rng, px(ctx, 40, img), 2)
    tw = ac.smoothstep(0.62, 0.98, r + 0.05 * n) * float(p["tarnish"]) * p["intensity"]
    tcol = rgb("#5A4A7A")[None, None] * (1 - ac.smoothstep(-1, 1, n))[..., None] + rgb("#B08A50")[None, None] * ac.smoothstep(-1, 1, n)[..., None]
    out = out * (1 - tw[..., None]) + tcol * tw[..., None]
    out = ac.apply_vignette(out, float(p["vignette"]), 0.35, 1.0, 0.8)
    if p["scratches"] > 0:
        out = fx.dust_scratches(out, REGISTRY["dust_scratches"].resolve_params({"scratches": int(p["scratches"]), "dust": 40, "hairs": 1, "strength": 0.4, "seed": int(p["seed"])}), ctx)
    return finish(out)


@op("wet_plate", 18, "display", category="process", spectral_mix=True, weights=[0.0, 0.25, 0.75], gain=7.0, black=0.05, white=0.85,
    tone="classic", pour_marks=0.2, fog=0.12, defocus=3.0, vignette=-1.4, dust=1.0, intensity=1.0, seed=0,
    choices={"tone": ["classic", "ruby_gold"]})
def wet_plate(img, p, ctx: Ctx):
    """Wet-plate collodion tintype / ambrotype: blue-only spectral sensitivity (reds go dark, blue eyes light), short tonal scale,
    japanned-plate blacks, pour marks, chemical edge fog, edge defocus, dust. spectral_mix=False if a slot-A `spectral_mix` precedes it."""
    rng = rng_for(ctx, p, 86)
    h, w = img.shape[:2]
    if p["spectral_mix"]:
        wgt = np.asarray(p["weights"], F32)
        g = ac.enc(np.maximum(ac.lin(img)[..., 0] * wgt[0] + ac.lin(img)[..., 1] * wgt[1] + ac.lin(img)[..., 2] * wgt[2], 0))
    else:
        g = luma(img)
    g = np.clip(g, 0, 1)
    g = ac.tone(g, float(p["gain"]), float(p["black"]), float(p["white"]))
    stops = (["#1A1410", "#C9B998"] if p["tone"] == "classic" else ["#4A1A1E", "#D4B36A"])
    out = ac.apply_lut(np.clip((g - p["black"]) / max(p["white"] - p["black"], 1e-3), 0, 1), ac.make_lut(stops))
    out = out * (p["white"] - p["black"]) + p["black"] * 0.5 * np.ones(3, F32)
    # pour marks: soft crescents in the corners, slightly blue
    marks = np.zeros((h, w), np.uint8)
    for cx, cy in ((0.0, 0.0), (1.0, 1.0), (1.0, 0.0)):
        if rng.random() < 0.75:
            c = (int(cx * w + rng.uniform(-0.03, 0.03) * w), int(cy * h + rng.uniform(-0.03, 0.03) * h))
            cv2.ellipse(marks, c, (int(rng.uniform(0.18, 0.3) * w), int(rng.uniform(0.15, 0.3) * h)), float(rng.uniform(0, 180)), 0, 360, 255, max(2, int(0.02 * w)), cv2.LINE_AA)
    mk = ac.blur(marks.astype(F32) / 255.0, px(ctx, 14, img))
    out = out + (float(p["pour_marks"]) * mk * p["intensity"])[..., None] * np.array([0.8, 0.9, 1.0], F32)
    n = ac.noise_unit((h, w), rng, px(ctx, 30, img), 3)
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    edge = ac.smoothstep(0.55, 1.0, np.hypot((xx / w - 0.5) / 0.5, (yy / h - 0.5) / 0.5) / 1.2)
    fog = ac.smoothstep(0.7, 1.4, n) * edge * float(p["fog"]) * p["intensity"]
    out = out + fog[..., None] * np.array([0.9, 0.95, 1.0], F32)
    if p["defocus"] > 0:
        rm = ac.smoothstep(0.3, 1.0, edge)[..., None]
        out = out * (1 - rm) + ac.blur(out, px(ctx, p["defocus"], img)) * rm
    out = ac.apply_vignette(out, float(p["vignette"]), 0.3, 0.6, 0.8)
    if p["dust"] > 0:
        out = fx.dust_scratches(out, REGISTRY["dust_scratches"].resolve_params({"strength": 0.5 * p["dust"], "scratches": 4, "dust": 160, "hairs": 3, "seed": int(p["seed"])}), ctx)
    return finish(out)


# =========================================================================== infrared
def ir_synth(img: np.ndarray, foliage: float = 0.5, sky: float = 0.5) -> np.ndarray:
    """Synthetic infrared luminance (linear): 0.65R + 0.35G, foliage bright (Wood effect), blue sky dark. Returns linear IR."""
    li = ac.lin(img)
    ir = 0.65 * li[..., 0] + 0.35 * li[..., 1]
    hh = ac.hsv(img)
    fm = ac.ang_weight(hh[..., 0], 115.0, 45.0, 15.0) * ac.smoothstep(0.15, 0.35, hh[..., 1])
    ir = ir + foliage * fm * li[..., 1]
    sm = ac.ang_weight(hh[..., 0], 220.0, 30.0, 15.0) * ac.smoothstep(0.08, 0.2, hh[..., 1]) * ac.smoothstep(0.3, 0.5, luma(img))
    ir = ir - sky * sm * ir
    return np.maximum(ir, 0)


@op("infrared_bw", 18, "display", category="process", foliage=0.5, sky=0.5, glow=0.4, glow_radius=2.0, gain=5.0, grain=0.04,
    skin_smooth=0.5, intensity=1.0, seed=0)
def infrared_bw(img, p, ctx: Ctx):
    """Infrared B&W (Wood effect): synthetic IR luminance (foliage glows white, blue sky goes black), halation-like glow,
    HIE-like grain, waxy skin. (Linear channel mix computed from the decoded image.)"""
    rng = rng_for(ctx, p, 87)
    h, w = img.shape[:2]
    g = np.clip(ac.enc(ir_synth(img, p["foliage"], p["sky"])), 0, 1)
    if p["skin_smooth"] > 0:
        sm = skin_mask(img) * float(p["skin_smooth"])
        b = cv2.bilateralFilter(np.ascontiguousarray(g), 9, 0.15, max(2.0, px(ctx, 6, img)))
        g = g * (1 - sm) + b * sm
    glow = ac.blur(np.clip(g - 0.7, 0, 1), px(ctx, p["glow_radius"] * 10, img))
    g = ac.screen(g, np.clip(glow * float(p["glow"]) * 2.0, 0, 1))
    g = ac.sgain(np.clip(g, 0, 1), float(p["gain"]))
    out = ac.add_grain(ac.gray3(g), rng, float(p["grain"]) * p["intensity"], max(0.5, px(ctx, 1.3, img)), 0.0, 0.5)
    return finish(out)


@op("aerochrome", 18, "display", category="process", style="aerochrome", foliage=0.5, sky=0.5, saturation=1.3, grain=0.03,
    intensity=1.0, seed=0, choices={"style": ["aerochrome", "channel_swap"]})
def aerochrome(img, p, ctx: Ctx):
    """Infrared false colour: Aerochrome mapping (IR->red, red->green, green->blue; foliage turns pink-red, sky cyan-blue) or the classic R<->B channel swap."""
    rng = rng_for(ctx, p, 88)
    if p["style"] == "channel_swap":
        out = img[..., ::-1].copy()
    else:
        ir = np.clip(ac.enc(ir_synth(img, p["foliage"], p["sky"])), 0, 1)
        out = np.stack([ir, img[..., 0], img[..., 1]], -1)
    out = ac.saturate(out, float(p["saturation"]))
    out = ac.add_grain(out, rng, float(p["grain"]) * p["intensity"], max(0.5, px(ctx, 1.0, img)), 0.5)
    return finish(out)


# =========================================================================== solarisation / cross-process / toy camera
@op("solarize", 18, "display", category="process", threshold=0.62, mix=0.85, mackie=0.3, gain=4.0, mode="rgb", intensity=1.0,
    choices={"mode": ["rgb", "luma", "bw"]})
def solarize(img, p, ctx: Ctx):
    """Solarisation / Sabattier: fold the tone curve above a threshold (v -> 2t - v), Mackie-line edge halos, then S-curve."""
    t, m = float(p["threshold"]), float(p["mix"]) * float(p["intensity"])

    def fold(v):
        f = np.where(v < t, v, 2 * t - v)
        return v * (1 - m) + f * m

    if p["mode"] == "rgb":
        out = fold(np.clip(img, 0, 1))
        edge_src = luma(img)
    elif p["mode"] == "bw":
        g = luma(img)
        out = ac.gray3(fold(g))
        edge_src = g
    else:
        l = luma(img)
        l2 = fold(l)
        out = np.clip(img * ((l2 + 0.02) / (l + 0.02))[..., None], 0, 1)
        edge_src = l
    if p["mackie"] > 0:
        b = ac.blur(edge_src, max(1.0, px(ctx, 2.0, img)))
        gx, gy = cv2.Sobel(b, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(b, cv2.CV_32F, 0, 1, ksize=3)
        e = np.clip(np.hypot(gx, gy) * 6.0, 0, 1)
        out = out + (float(p["mackie"]) * e)[..., None]
    out = ac.sgain(np.clip(out, 0, 1), float(p["gain"]))
    return finish(out)


@op("cross_process", 18, "display", category="process", variant="e6_in_c41", saturation=1.35, gain=7.0, grain=0.04,
    intensity=1.0, seed=0, choices={"variant": ["e6_in_c41", "c41_in_e6"]})
def cross_process(img, p, ctx: Ctx):
    """Cross-processing: per-channel curves (E6 slide in C41 chemistry: yellow-green highlights, cyan-blue shadows, high contrast and saturation)."""
    rng = rng_for(ctx, p, 89)
    if p["variant"] == "e6_in_c41":
        curves = STOCKS["cross_process"]["curves"]
    else:
        curves = dict(r=[(0, 0.06), (0.3, 0.28), (0.6, 0.62), (1, 0.97)], g=[(0, 0), (0.3, 0.32), (0.7, 0.78), (1, 1)],
                      b=[(0, 0.0), (0.3, 0.24), (0.6, 0.55), (1, 0.92)])
    s = dict(sat=p["saturation"], gain=float(p["gain"]) * (1.0 if p["variant"] == "e6_in_c41" else 0.5), curves=curves,
             grain=dict(amount=p["grain"], size=1.2, chroma=1.0))
    return finish(grade(img, s, rng, ctx, p["intensity"]))


@op("lomo", 18, "display", category="process", holga=False, saturation=1.3, gain=7.0, vignette=-3.0, ca=0.004, leak=0.3, cross=0.0,
    grain=0.03, intensity=1.0, seed=0)
def lomo(img, p, ctx: Ctx):
    """Toy camera (Lomo / Holga look): strong vignette, saturated punchy colour, blue shadows / warm highlights, lateral chromatic
    aberration, light leaks; holga=True adds corner defocus."""
    rng = rng_for(ctx, p, 90)
    I = float(p["intensity"])
    x = img
    if p["cross"] > 0:
        c = STOCKS["cross_process"]["curves"]
        cp = np.stack([ac.apply_curve(x[..., i], ac.curve_lut(c["rgb"[i]])) for i in range(3)], -1)
        x = x * (1 - p["cross"]) + cp * p["cross"]
    s = dict(sat=p["saturation"], gain=p["gain"], shadow_tint=["#1B2A4A", 0.12, 0.4], high_tint=["#F6D08A", 0.10, 0.6])
    x = grade(x, s, rng, ctx, I)
    x = fx.chromatic_shift(x, REGISTRY["chromatic_shift"].resolve_params({"mode": "lateral", "amount": float(p["ca"])}), ctx)
    if p["holga"]:
        m = ac.vignette_mask(*x.shape[:2], 0.1, 0.6, 0.9)[..., None] ** 1.3
        x = x * (1 - m) + ac.blur(x, px(ctx, 5, img)) * m
    x = ac.apply_vignette(x, float(p["vignette"]) * (1.3 if p["holga"] else 1.0) * I, 0.25, 0.5, 0.9)
    if p["leak"] > 0:
        x = fx.light_leak(x, REGISTRY["light_leak"].resolve_params({"strength": float(p["leak"]) * I, "count": 2, "seed": int(p["seed"])}), ctx)
    x = ac.add_grain(x, rng, float(p["grain"]) * I, max(0.4, px(ctx, 1.1, img)), 0.5)
    return finish(x)


# =========================================================================== ink-on-paper processes



@op("risograph", 20, "display", category="art", inks=["#FF48B0", "#0078BF"], paper="#F3EFE4", misregister=4.0, dot=0.8, spread=0.6, screen=4.5, grain_mix=0.35,
    opacity=0.92, grain=0.03, contrast=1.1, seed=0)
def risograph(img, p, ctx: Ctx):
    """Risograph: image split into 2-3 spot-ink densities (least squares in optical-density space), each stochastically screened,
    misregistered per layer, overprinted multiplicatively on paper, with ink spread and paper grain."""
    rng = rng_for(ctx, p, 91)
    h, w = img.shape[:2]
    paper = rgb(p["paper"])
    pl = ac.lin(paper)
    inks = ac.pal(p["inks"])
    rel = np.clip(ac.lin(inks) / pl, 0.03, 1.0)                    # reflectance of each ink relative to paper
    M = -np.log(rel).T                                             # (3, k) optical densities
    src = np.clip((img - 0.5) * float(p["contrast"]) + 0.5, 0, 1)
    tgt = np.clip(ac.lin(src) / pl, 0.03, 1.0)
    od = -np.log(tgt).reshape(-1, 3)
    dens = np.clip(od @ np.linalg.pinv(M).T, 0, 1).reshape(h, w, len(inks)).astype(F32)
    out = np.ones((h, w, 3), F32) * pl
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    pitch = max(2.0, px(ctx, p["screen"], img))
    for i in range(len(inks)):
        th = np.radians([15.0, 75.0, 45.0][i % 3])
        uu = (xx * np.cos(th) + yy * np.sin(th)) / pitch
        vv = (-xx * np.sin(th) + yy * np.cos(th)) / pitch
        screen = 0.5 + 0.25 * (np.cos(2 * np.pi * uu) + np.cos(2 * np.pi * vv))   # AM dot screen thresholds 0..1
        u = ac.noise_unit((h, w), rng, px(ctx, p["dot"], img))
        u = 0.5 * (1 + _erf(u / np.sqrt(2)))                       # uniform grain thresholds from gaussian noise
        thr = (1 - float(p["grain_mix"])) * screen + float(p["grain_mix"]) * u
        cov = (dens[..., i] > thr).astype(F32)
        dx, dy = rng.uniform(-1, 1, 2) * px(ctx, p["misregister"], img)
        cov = cv2.warpAffine(cov, np.array([[1, 0, dx], [0, 1, dy]], F32), (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        cov = ac.blur(cov, px(ctx, p["spread"], img)) * float(p["opacity"])
        out = out * (1 - cov[..., None] * (1 - rel[i]))
    res = ac.enc(out)
    res = res * ac.paper_multiplier(h, w, rng, float(p["grain"]), max(0.5, max(h, w) / 2000.0), 0.2)[..., None]
    return finish(res)


def _erf(x):
    from scipy.special import erf

    return erf(x).astype(F32)


@op("gum_bichromate", 20, "display", category="process", pigments=["#1F6F8B", "#A3325F", "#E0B23A"], misregister=5.0, saturation=0.8,
    gain=2.5, black=0.1, paper="#F4EFE4", paper_texture=0.1, brush_border=40.0, seed=0)
def gum_bichromate(img, p, ctx: Ctx):
    """Gum bichromate: three pigment layers (cyan / magenta / yellow separations), each smoothed, randomly misregistered, brush-edged,
    printed multiplicatively on heavily textured paper with lifted blacks."""
    rng = rng_for(ctx, p, 92)
    h, w = img.shape[:2]
    x = ac.saturate(np.clip(img, 0, 1), float(p["saturation"]) + 0.2)
    sep = 1.0 - x
    pig = ac.pal(p["pigments"])
    paper = rgb(p["paper"])
    out = np.ones((h, w, 3), F32) * paper
    for i in range(3):
        layer = ac.edge_preserve(ac.gray3(sep[..., i]), ctx, 40, 0.5)[..., 0]
        layer = ac.tone(np.clip(layer, 0, 1), float(p["gain"]), float(p["black"]) * 0.0, 1.0) * (1 - float(p["black"])) + 0.0
        dx, dy = rng.uniform(-1, 1, 2) * px(ctx, p["misregister"], img)
        layer = cv2.warpAffine(layer.astype(F32), np.array([[1, 0, dx], [0, 1, dy]], F32), (w, h), borderMode=cv2.BORDER_CONSTANT)
        if p["brush_border"] > 0:
            layer = layer * _mask_border(h, w, rng, px(ctx, p["brush_border"], img))
        out = out * (1 - layer[..., None] * (1 - pig[i]))
    out = out * ac.paper_multiplier(h, w, rng, float(p["paper_texture"]), max(0.6, max(h, w) / 1500.0), 0.4)[..., None]
    return finish(out)


# =========================================================================== digital artefacts
@op("glitch", 20, "display", category="art", bands=12, shift=0.06, channel_shift=8.0, jpeg_quality=12, corrupt=8, sort_bands=2,
    scan=0.15, noise_bands=2, seed=0)
def glitch(img, p, ctx: Ctx):
    """Glitch / datamosh: row-band displacement, RGB channel misalignment, low-quality JPEG with macroblock corruption, pixel sorting in
    some bands, scanline dimming and saturated noise bands."""
    rng = rng_for(ctx, p, 93)
    h, w = img.shape[:2]
    out = img.copy()
    for _ in range(int(p["bands"])):
        bh = max(2, int(rng.uniform(0.02, 0.08) * h))
        y0 = int(rng.integers(0, max(1, h - bh)))
        sh = int(rng.uniform(-1, 1) * float(p["shift"]) * w)
        out[y0:y0 + bh] = np.roll(out[y0:y0 + bh], sh, axis=1)
        if rng.random() < 0.6:
            ch = int(rng.choice([0, 2]))
            out[y0:y0 + bh, :, ch] = np.roll(out[y0:y0 + bh, :, ch], int(rng.uniform(-1, 1) * px(ctx, p["channel_shift"], img) * 2), axis=1)
    for _ in range(int(p["sort_bands"])):
        bh = max(2, int(rng.uniform(0.02, 0.06) * h))
        y0 = int(rng.integers(0, max(1, h - bh)))
        x0 = int(rng.integers(0, w // 2))
        x1 = int(min(w, x0 + rng.uniform(0.2, 0.6) * w))
        band = out[y0:y0 + bh, x0:x1]
        idx = np.argsort(luma(band), axis=1)
        out[y0:y0 + bh, x0:x1] = np.take_along_axis(band, idx[..., None].repeat(3, -1), 1)
    # low-quality JPEG recompression (clean decode), then macroblock-style corruption: smeared / chroma-shifted 8x8-aligned spans
    bgr = ac.to_u8(out)[..., ::-1]
    ok, buf = cv2.imencode(".jpg", np.ascontiguousarray(bgr), [cv2.IMWRITE_JPEG_QUALITY, int(p["jpeg_quality"])])
    if ok:
        dec = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if dec is not None and dec.shape[:2] == (h, w):
            out = dec[..., ::-1].astype(F32) / 255.0
    for _ in range(int(p["corrupt"])):
        bh = 8 * int(rng.integers(1, 5))
        y0 = 8 * int(rng.integers(0, max(1, (h - bh) // 8)))
        x0 = 8 * int(rng.integers(0, max(1, w // 16)))
        x1 = min(w, x0 + 8 * int(rng.integers(w // 64, max(w // 64 + 1, w // 8))))
        kind = int(rng.integers(0, 3))
        blk = out[y0:y0 + bh, x0:x1]
        if kind == 0:      # smear: repeat the first column of blocks
            out[y0:y0 + bh, x0:x1] = blk[:, :1]
        elif kind == 1:    # copy from a different row band
            ys = int(rng.integers(0, max(1, h - bh)))
            out[y0:y0 + bh, x0:x1] = out[ys:ys + bh, x0:x1]
        else:              # chroma inversion
            yc = cv2.cvtColor(np.ascontiguousarray(blk), cv2.COLOR_RGB2YCrCb)
            yc[..., 1:] = 1.0 - yc[..., 1:]
            out[y0:y0 + bh, x0:x1] = cv2.cvtColor(yc, cv2.COLOR_YCrCb2RGB)
    if p["scan"] > 0:
        m = np.ones(h, F32)
        m[::2] = 1 - float(p["scan"])
        out = out * m[:, None, None]
    for _ in range(int(p["noise_bands"])):
        bh = max(1, int(rng.uniform(0.005, 0.02) * h))
        y0 = int(rng.integers(0, max(1, h - bh)))
        col = rgb(str(rng.choice(["#00FFFF", "#FF00FF"])))
        out[y0:y0 + bh] = out[y0:y0 + bh] * 0.5 + col * 0.5 * (0.6 + 0.4 * rng.random((bh, w, 1), dtype=F32))
    return finish(out)


@op("vhs", 20, "display", category="art", lines=480, chroma_bleed=4.0, saturation=1.15, gain=4.0, black=0.06, noise=0.05, tracking=1.0,
    comb=1.0, barrel=0.04, seed=0)
def vhs(img, p, ctx: Ctx):
    """VHS: 480-line resample, soft luma, 4x downsampled chroma with Cr/Cb shifted (colour bleed), crushed contrast, line noise,
    a tracking bar, interlace comb on edges and slight barrel distortion."""
    rng = rng_for(ctx, p, 94)
    h, w = img.shape[:2]
    lines = int(max(96, round(float(p["lines"]) / 1080.0 * h)))
    sm = cv2.resize(img, (w, lines), interpolation=cv2.INTER_AREA)
    ycc = cv2.cvtColor(np.clip(sm, 0, 1), cv2.COLOR_RGB2YCrCb)
    sx = w / 640.0
    ycc[..., 0] = ac.blur(ycc[..., 0], 1.2 * sx)
    cs = max(1, int(float(p["chroma_bleed"])))
    for ch, sgn in ((1, 1), (2, -1)):
        c = cv2.resize(ycc[..., ch], (max(2, w // cs), lines), interpolation=cv2.INTER_AREA)
        c = cv2.resize(c, (w, lines), interpolation=cv2.INTER_LINEAR)
        ycc[..., ch] = np.roll(c, int(sgn * 3 * sx), axis=1)
    sm = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)
    out = cv2.resize(sm, (w, h), interpolation=cv2.INTER_LINEAR)
    out = ac.saturate(out, float(p["saturation"]))
    out = ac.tone(np.clip(out, 0, 1), float(p["gain"]), float(p["black"]), 1.0)
    rows = (1.0 + 0.3 * (rng.random(h) < 0.015) * rng.random(h)).astype(F32)
    out = out * rows[:, None, None]
    if p["comb"] > 0:
        e = np.clip(np.abs(cv2.Sobel(luma(out), cv2.CV_32F, 1, 0, ksize=3)) * 4, 0, 1)
        shifted = np.roll(out, max(1, int(2 * sx)), axis=1)
        odd = (np.arange(h) % 2 == 1)[:, None, None]
        out = np.where(odd, out * (1 - e[..., None] * p["comb"]) + shifted * e[..., None] * p["comb"], out)
    if p["tracking"] > 0:
        bh = max(2, int(rng.uniform(0.03, 0.06) * h))
        y0 = int(rng.uniform(0.55, 0.9) * h)
        y1 = min(h, y0 + bh)
        band = np.roll(out[y0:y1], int(rng.uniform(-0.03, 0.03) * w), axis=1)
        out[y0:y1] = np.clip(band * 1.25 + 0.05 + 0.08 * rng.standard_normal((y1 - y0, w, 1), dtype=F32) * p["tracking"], 0, 1)
    if p["barrel"] > 0:
        yy, xx = np.mgrid[0:h, 0:w].astype(F32)
        u, v = (xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2)
        r2 = u * u + v * v
        k = 1 + float(p["barrel"]) * r2
        out = cv2.remap(out, (u * k * w / 2 + w / 2).astype(F32), (v * k * h / 2 + h / 2).astype(F32), cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    out = out + float(p["noise"]) * 0.5 * rng.standard_normal((h, w, 1), dtype=F32)
    return finish(out)


GB_PALETTE = ["#0F380F", "#306230", "#8BAC0F", "#9BBC0F"]


@op("gameboy", 20, "display", category="art", cells=128, gain=5.0, dither="bayer4", palette=GB_PALETTE, frame=False,
    choices={"dither": ["none", "bayer2", "bayer4"]})
def gameboy(img, p, ctx: Ctx):
    """Game Boy Camera: ~128-column downscale, B&W with contrast gain, 4x4 (or 2x2) Bayer ordered dither to the 4-shade DMG green ramp, integer upscale."""
    h, w = img.shape[:2]
    cell = max(1, int(round(w / float(p["cells"]))))
    sw, sh = -(-w // cell), -(-h // cell)
    g = cv2.resize(luma(img), (sw, sh), interpolation=cv2.INTER_AREA)
    g = ac.sgain(np.clip(g, 0, 1), float(p["gain"]))
    pl = ac.pal(p["palette"])
    n = {"none": 0, "bayer2": 2, "bayer4": 4}[p["dither"]]
    idx = ac.ordered_dither_levels(g, len(pl), n) if n else np.clip(np.round(g * (len(pl) - 1)), 0, len(pl) - 1).astype(np.int32)
    small = pl[idx]
    if p["frame"]:
        small[0, :], small[-1, :], small[:, 0], small[:, -1] = pl[0], pl[0], pl[0], pl[0]
    big = np.repeat(np.repeat(small, cell, 0), cell, 1)[:h, :w]
    return finish(big)


IRONBOW = [(0.0, "#000000"), (0.17, "#1E0040"), (0.34, "#7A0C7A"), (0.5, "#D11A4A"), (0.67, "#FF7A00"), (0.84, "#FFD800"), (1.0, "#FFFFFF")]
RAMPS = {"ironbow": IRONBOW, "white_hot": [(0.0, "#000000"), (1.0, "#FFFFFF")], "black_hot": [(0.0, "#FFFFFF"), (1.0, "#000000")],
         "rainbow": [(0.0, "#00007F"), (0.25, "#007FFF"), (0.5, "#7FFF7F"), (0.75, "#FF7F00"), (1.0, "#7F0000")]}


@op("thermal", 18, "display", category="process", ramp="ironbow", smooth=2.5, res=320, legend=True, crosshair=False, gain=1.5,
    choices={"ramp": list(RAMPS)})
def thermal(img, p, ctx: Ctx):
    """Thermal camera false colour: smoothed lightness at low sensor resolution mapped through an ironbow (or white/black-hot, rainbow)
    ramp, with an optional temperature bar and crosshair."""
    h, w = img.shape[:2]
    l = np.clip(luma(ac.blur(img, px(ctx, p["smooth"], img))), 0, 1)
    rw = int(min(w, p["res"])) if p["res"] else w
    rh = max(2, int(round(rw * h / w)))
    l = cv2.resize(cv2.resize(l, (rw, rh), interpolation=cv2.INTER_AREA), (w, h), interpolation=cv2.INTER_CUBIC)
    l = np.clip(ac.sgain(np.clip(l, 0, 1), float(p["gain"]) * 2), 0, 1)
    lut = ac.make_lut(RAMPS[p["ramp"]])
    out = ac.apply_lut(l, lut)
    if p["legend"]:
        bw_ = max(4, int(0.03 * w))
        x0 = w - bw_ - max(4, int(0.02 * w))
        y0, y1 = int(0.1 * h), int(0.9 * h)
        bar = lut[np.linspace(1023, 0, y1 - y0).astype(int)]
        out[y0:y1, x0:x0 + bw_] = bar[:, None, :]
        cv2.rectangle(out, (x0 - 1, y0 - 1), (x0 + bw_, y1), (1.0, 1.0, 1.0), 1, cv2.LINE_AA)
    if p["crosshair"]:
        cx, cy, r = w // 2, h // 2, max(6, int(0.03 * w))
        for c in ((cx - r, cy, cx + r, cy), (cx, cy - r, cx, cy + r)):
            cv2.line(out, (c[0], c[1]), (c[2], c[3]), (1.0, 1.0, 1.0), 1, cv2.LINE_AA)
    return finish(out)


@op("xray", 18, "display", category="process", mode="xray", gain=4.0, glow=0.8, vignette=-0.5, stops=["#0A1A2A", "#6FA3C7", "#FFFFFF"],
    choices={"mode": ["xray", "negative", "negative_bw"]})
def xray(img, p, ctx: Ctx):
    """X-ray (blue-white negative with edge glow) or plain colour / B&W negative."""
    if p["mode"] == "negative":
        return finish(1.0 - np.clip(img, 0, 1))
    n = 1.0 - luma(img)
    if p["mode"] == "negative_bw":
        return finish(ac.gray3(n))
    n = ac.sgain(np.clip(n, 0, 1), float(p["gain"]))
    n = n + float(p["glow"]) * (n - ac.blur(n, px(ctx, 6, img)))
    n = ac.blur(n, px(ctx, 1.0, img))
    out = ac.apply_lut(np.clip(n, 0, 1), ac.make_lut(p["stops"]))
    out = ac.apply_vignette(out, float(p["vignette"]), 0.4, 0.5, 0.8)
    return finish(out)


ac.finalize_docs(__name__)
