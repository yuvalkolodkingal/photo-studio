"""General-purpose effect ops: grain, vignette, halation / bloom / glow (optics, linear slot A), Orton, soft focus,
light leaks, chromatic shift, posterize / threshold / halftone / k-means quantise, gradient maps and duotones,
scanlines, noise, borders, letterbox, tilt-shift, lens blur, XDoG ink layer, paper texture, haze, dust.

Stage / domain: optics (halation, bloom, lens_blur) run in scene-linear at slot A (stage 15); look-style
gradient maps run at slot B (stage 18); the rest are display-referred effects (19) or raster art (20)."""
from __future__ import annotations

import cv2
import numpy as np

from ..registry import Ctx, op
from . import art_common as ac
from .art_common import F32, finish, luma, px, rng_for, rgb

# =========================================================================== grain / noise
@op("grain", 19, "display", category="effects", amount=0.03, size=1.0, roughness=0.4, chroma=0.0,
    midtone_bias=0.6, shadow_bias=0.0, clump=0.0, seed=0)
def grain(img, p, ctx: Ctx):
    """Luminance-dependent film grain (strongest in midtones). amount = sigma in display units, size in px@2000."""
    rng = rng_for(ctx, p, 1)
    size = max(0.3, px(ctx, p["size"], img)) if p["size"] > 0 else 0.0
    out = ac.add_grain(img, rng, float(p["amount"]), size, float(p["chroma"]), float(p["roughness"]),
                       float(p["midtone_bias"]), float(p["shadow_bias"]), float(p["clump"]))
    return finish(out)


@op("noise", 19, "display", category="effects", kind="gaussian", amount=0.05, chroma=0.5, size=0.0, density=0.01,
    seed=0, choices={"kind": ["gaussian", "uniform", "salt_pepper", "banding", "perlin"]})
def noise(img, p, ctx: Ctx):
    """Additive sensor-style noise: gaussian, uniform, salt & pepper, horizontal banding or smooth perlin-like."""
    rng = rng_for(ctx, p, 2)
    h, w = img.shape[:2]
    a, kind = float(p["amount"]), p["kind"]
    size = px(ctx, p["size"], img) if p["size"] else 0.0
    if kind in ("gaussian", "uniform"):
        gen = (lambda: rng.standard_normal((h, w), dtype=F32)) if kind == "gaussian" else (
            lambda: rng.uniform(-1.7, 1.7, (h, w)).astype(F32))
        shared = ac.blur(gen(), size)
        shared /= shared.std() + 1e-8
        per = np.stack([ac.blur(gen(), size) for _ in range(3)], -1)
        per /= per.std() + 1e-8
        c = float(p["chroma"])
        n = np.sqrt(1 - c) * shared[..., None] + np.sqrt(c) * per
        return finish(img + a * n)
    if kind == "salt_pepper":
        r = rng.random((h, w))
        out = img.copy()
        d = float(p["density"])
        out[r < d / 2] = 0.0
        out[(r >= d / 2) & (r < d)] = 1.0
        return finish(out)
    if kind == "banding":
        rows = rng.standard_normal(h).astype(F32)
        rows = ac.blur(rows.reshape(h, 1), max(0.5, size or 1.0)).reshape(h, 1, 1)
        rows /= rows.std() + 1e-8
        return finish(img + a * rows)
    n = ac.noise_unit((h, w), rng, max(2.0, size or px(ctx, 25, img)), 3)
    return finish(img + a * n[..., None])


# =========================================================================== vignette
@op("vignette", 19, "display", category="effects", amount=-0.5, midpoint=0.5, roundness=0.3, feather=0.6,
    highlights=0.2, center_x=0.5, center_y=0.5)
def vignette(img, p, ctx: Ctx):
    """Creative vignette in stops (negative darkens corners). midpoint = where falloff starts, feather = softness,
    roundness 0 follows the frame / 1 circular, highlights protects bright areas."""
    out = ac.apply_vignette(img, float(p["amount"]), float(p["midpoint"]), float(p["roundness"]), float(p["feather"]),
                            float(p["highlights"]), float(p["center_x"]), float(p["center_y"]))
    return finish(out)


# =========================================================================== optics (linear, slot A)
@op("halation", 15, "linear", category="optics", threshold=0.6, knee=0.4, radius=14.0, strength=0.5,
    tint=[1.0, 0.30, 0.08], spread=3.0)
def halation(img, p, ctx: Ctx):
    """Film halation (linear, slot A): threshold highlights, two-scale blur, red-orange tint, add back."""
    r = max(1.0, px(ctx, p["radius"], img))
    g = ac.halation_glow(img, float(p["threshold"]), float(p["knee"]), r, p["tint"], float(p["spread"]))
    return (img + float(p["strength"]) * g).astype(F32)


@op("bloom", 15, "linear", category="optics", threshold=0.7, knee=0.5, radius=18.0, strength=0.25, tint=[1.0, 1.0, 1.0],
    scales=4)
def bloom(img, p, ctx: Ctx):
    """Physically-motivated bloom (linear, slot A): multi-scale blurred highlights added back."""
    L = ac.lum_lin(img)
    m = ac.highlight_mask_lin(L, float(p["threshold"]), float(p["knee"]))
    src = img * (m / np.maximum(L, 1e-4))[..., None]
    r0 = max(1.0, px(ctx, p["radius"], img))
    acc, wsum = np.zeros_like(img), 0.0
    for i in range(int(p["scales"])):
        wgt = 0.6 ** i
        acc += wgt * ac.blur(src, r0 * (2 ** i) * 0.5)
        wsum += wgt
    return (img + float(p["strength"]) * acc / wsum * np.asarray(p["tint"], F32)).astype(F32)


def _disc_kernel(r: float, blades: int = 0, rotation: float = 0.0) -> np.ndarray:
    R = max(1, int(np.ceil(r)))
    ss = 4
    n = (2 * R + 1) * ss
    k = np.zeros((n, n), np.uint8)
    c = n // 2
    if blades >= 3:
        ang = np.radians(rotation) + np.arange(blades) * 2 * np.pi / blades
        pts = np.stack([c + r * ss * np.cos(ang), c + r * ss * np.sin(ang)], 1).astype(np.int32)
        cv2.fillConvexPoly(k, pts, 255)
    else:
        cv2.circle(k, (c, c), int(round(r * ss)), 255, -1)
    k = cv2.resize(k.astype(F32), (2 * R + 1, 2 * R + 1), interpolation=cv2.INTER_AREA)
    return k / max(k.sum(), 1e-6)


@op("lens_blur", 15, "linear", category="optics", radius=10.0, blades=0, rotation=0.0, highlight_gain=2.0,
    threshold=0.8, radial=0.0, falloff=1.5)
def lens_blur(img, p, ctx: Ctx):
    """Disc / polygon-aperture lens blur in linear light (bokeh balls from highlights). radial>0 blurs mostly
    toward the corners (Holga-style); highlight_gain brightens clipped highlights so they form discs."""
    r = px(ctx, p["radius"], img)
    if r < 0.6:
        return img
    k = _disc_kernel(r, int(p["blades"]), float(p["rotation"]))
    L = ac.lum_lin(img)
    wgt = 1.0 + float(p["highlight_gain"]) * ac.smoothstep(float(p["threshold"]), float(p["threshold"]) + 0.5, L)
    num = cv2.filter2D(img * wgt[..., None], -1, k, borderType=cv2.BORDER_REFLECT)
    den = cv2.filter2D(wgt, -1, k, borderType=cv2.BORDER_REFLECT)
    out = (num / np.maximum(den, 1e-6)[..., None]).astype(F32)
    rad = float(p["radial"])
    if rad > 0:
        m = ac.vignette_mask(*img.shape[:2], 0.15, 0.5, 1.0)
        m = np.power(m, float(p["falloff"]))[..., None] * rad + (1 - rad)
        out = img * (1 - m) + out * m
    return out.astype(F32)


@op("glow", 19, "display", category="effects", threshold=0.6, radius=24.0, strength=0.5, tint=[1.0, 1.0, 1.0], softness=0.5)
def glow(img, p, ctx: Ctx):
    """Display-referred glow / pro-mist: blurred highlights screened over the image."""
    L = luma(img)
    m = ac.smoothstep(float(p["threshold"]) - float(p["softness"]) * 0.3, 1.0, L)
    src = img * m[..., None]
    g = 0.6 * ac.blur(src, max(1.0, px(ctx, p["radius"], img))) + 0.4 * ac.blur(src, max(1.0, px(ctx, p["radius"] * 2.5, img)))
    return finish(ac.screen(img, np.clip(g * np.asarray(p["tint"], F32) * float(p["strength"]), 0, 1)))


@op("orton", 19, "display", category="effects", amount=0.35, radius=28.0, brightness=0.0, contrast=1.0, protect_highlights=0.6)
def orton(img, p, ctx: Ctx):
    """Orton effect: (screen + multiply) duplicate blurred and blended in; highlight protection avoids dark halos."""
    over = ac.screen(img, img)
    under = img * img
    merged = 0.5 * (over + under)
    if p["brightness"]:
        merged = np.clip(merged * (2.0 ** (float(p["brightness"]) / 2.2)), 0, 1)
    if p["contrast"] != 1.0:
        merged = np.clip((merged - 0.5) * float(p["contrast"]) + 0.5, 0, 1)
    layer = ac.blur(merged, max(1.0, px(ctx, p["radius"], img)))
    prot = ac.smoothstep(0.65, 0.95, ac.blur(luma(img), max(1.0, px(ctx, p["radius"] * 0.5, img))))
    a = float(p["amount"]) * (1.0 - float(p["protect_highlights"]) * prot)[..., None]
    return finish(img * (1 - a) + layer * a)


@op("soft_focus", 19, "display", category="effects", amount=0.5, radius=10.0, halo=0.3, contrast_drop=0.1)
def soft_focus(img, p, ctx: Ctx):
    """Soft-focus lens: sharp core kept, blurred copy mixed in, highlights bleed (halo), slight contrast drop."""
    b = ac.blur(img, max(1.0, px(ctx, p["radius"], img)))
    a = float(p["amount"])
    out = img * (1 - a * 0.7) + b * a * 0.7
    L = luma(img)
    halo = ac.blur(img * ac.smoothstep(0.55, 1.0, L)[..., None], max(1.0, px(ctx, p["radius"] * 1.8, img)))
    out = ac.screen(out, halo * float(p["halo"]) * a * 2)
    cd = float(p["contrast_drop"]) * a
    out = out * (1 - cd) + 0.5 * cd + (out - 0.5) * 0.0
    return finish(out)


@op("light_leak", 19, "display", category="effects", count=3, strength=0.45, size=0.2, style="blob",
    colors=["#FFB347", "#FF5E3A", "#FFD27A"], edge_bias=0.8, seed=0, choices={"style": ["blob", "streak", "burn"]})
def light_leak(img, p, ctx: Ctx):
    """Light leaks: large warm anisotropic gaussian blobs (or streaks / edge burns) screened onto the frame."""
    rng = rng_for(ctx, p, 3)
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    cols = ac.pal(p["colors"])
    leak = np.zeros((h, w, 3), F32)
    style = p["style"]
    for i in range(int(p["count"])):
        side = rng.integers(0, 4)
        t = rng.uniform(0.1, 0.9)
        eb = float(p["edge_bias"])
        cx, cy = {0: (-0.02, t), 1: (1.02, t), 2: (t, -0.02), 3: (t, 1.02)}[int(side)]
        cx = cx * eb + rng.uniform(0.2, 0.8) * (1 - eb)
        cy = cy * eb + rng.uniform(0.2, 0.8) * (1 - eb)
        s = float(p["size"]) * w * rng.uniform(0.7, 1.3)
        ang = rng.uniform(0, np.pi)
        dx, dy = xx - cx * w, yy - cy * h
        u = dx * np.cos(ang) + dy * np.sin(ang)
        v = -dx * np.sin(ang) + dy * np.cos(ang)
        if style == "streak":
            g = np.exp(-(u / (s * 2.6)) ** 2 - (v / (s * 0.25)) ** 2)
        elif style == "burn":
            g = np.exp(-(np.hypot(dx, dy) / (s * 1.4)) ** 2)
        else:
            g = np.exp(-(u / (s * 1.4)) ** 2 - (v / (s * 0.8)) ** 2)
        c = cols[i % len(cols)]
        leak += g[..., None].astype(F32) * c * rng.uniform(0.6, 1.0)
    return finish(ac.screen(img, np.clip(leak * float(p["strength"]), 0, 1)))


# =========================================================================== optical misregistration
@op("chromatic_shift", 19, "display", category="effects", mode="lateral", amount=0.004, red_dx=2.0, red_dy=0.0,
    blue_dx=-2.0, blue_dy=0.0, choices={"mode": ["lateral", "offset"]})
def chromatic_shift(img, p, ctx: Ctx):
    """RGB misregistration: lateral CA (R scaled up / B down about the centre) or per-channel pixel offsets."""
    h, w = img.shape[:2]
    out = img.copy()
    if p["mode"] == "lateral":
        a = float(p["amount"])
        c = ((w - 1) / 2.0, (h - 1) / 2.0)
        for ch, s in ((0, 1.0 + a), (2, 1.0 - a)):
            m = np.array([[s, 0, c[0] * (1 - s)], [0, s, c[1] * (1 - s)]], F32)
            out[..., ch] = cv2.warpAffine(img[..., ch], m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    else:
        for ch, (dx, dy) in ((0, (p["red_dx"], p["red_dy"])), (2, (p["blue_dx"], p["blue_dy"]))):
            m = np.array([[1, 0, px(ctx, dx, img)], [0, 1, px(ctx, dy, img)]], F32)
            out[..., ch] = cv2.warpAffine(img[..., ch], m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return finish(out)


# =========================================================================== quantise / threshold / halftone
@op("posterize", 20, "display", category="art", levels=4, mode="luma", palette=[], smooth=0.0, quantile_mix=0.5,
    choices={"mode": ["luma", "rgb", "oklab"]})
def posterize(img, p, ctx: Ctx):
    """Posterise to N levels (luma: flat tone bands, rgb: per-channel, oklab: lightness only). With a palette
    (dark->light) each band becomes a flat colour. quantile_mix blends equal-width and equal-population bands."""
    n = int(p["levels"]) if not p["palette"] else len(p["palette"])
    n = max(2, n)
    l = luma(img)
    if p["mode"] == "rgb" and not p["palette"]:
        q = np.floor(np.clip(img, 0, 0.9999) * n) / (n - 1)
        return finish(q)
    if p["mode"] == "oklab" and not p["palette"]:
        lab = ac.oklab(img)
        L = lab[..., 0]
        q = np.minimum(np.floor(np.clip(L, 0, 0.9999) * n), n - 1) / (n - 1)
        lab[..., 0] = q
        return finish(ac.from_oklab(lab))
    qm = float(p["quantile_mix"])
    edges = np.linspace(0, 1, n + 1)[1:-1]
    if qm > 0:
        qe = np.quantile(l[::2, ::2], np.linspace(0, 1, n + 1)[1:-1])
        edges = edges * (1 - qm) + qe * qm
    if p["smooth"] > 0:
        l = ac.blur(l, px(ctx, p["smooth"], img))
    idx = np.searchsorted(edges, l)
    if p["palette"]:
        return finish(ac.pal(p["palette"])[idx])
    q = idx.astype(F32) / (n - 1)
    ratio = (q + 0.02) / (l + 0.02)
    return finish(img * ratio[..., None] if p["mode"] == "luma" else q[..., None] * np.ones(3, F32))


@op("threshold", 20, "display", category="art", level=0.5, otsu=False, softness=0.0, blur=1.0, fg="#101010", bg="#F4F1EA",
    invert=False)
def threshold(img, p, ctx: Ctx):
    """Two-tone threshold (fixed level or Otsu) with optional soft edge; colours fg (dark) / bg (light)."""
    l = ac.blur(luma(img), px(ctx, p["blur"], img))
    lvl = float(p["level"])
    if p["otsu"]:
        lvl = cv2.threshold(ac.to_u8(l), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0] / 255.0
    s = float(p["softness"])
    t = ac.smoothstep(lvl - s, lvl + s, l) if s > 0 else (l >= lvl).astype(F32)
    if p["invert"]:
        t = 1 - t
    return finish(rgb(p["fg"]) * (1 - t[..., None]) + rgb(p["bg"]) * t[..., None])


def _halftone_layer(cov: np.ndarray, pitch: float, angle: float, gain: float, aa: float, constant_r: float | None = None):
    """Ink coverage (HxW 0..1) of a rotated dot screen whose dot size follows `cov` (tone coverage 0..1)."""
    h, w = cov.shape
    th = np.radians(angle)
    c, s = np.cos(th), np.sin(th)
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    u = (xx * c + yy * s) / pitch
    v = (-xx * s + yy * c) / pitch
    cu0 = np.floor(u - 0.5) + 0.5
    cv0 = np.floor(v - 0.5) + 0.5
    smooth = cv2.GaussianBlur(cov, (0, 0), max(0.7, pitch * 0.45))
    out = np.zeros((h, w), F32)
    for du in (0.0, 1.0):
        for dv in (0.0, 1.0):
            cu, cvv = cu0 + du, cv0 + dv
            xc = (cu * c - cvv * s) * pitch
            yc = (cu * s + cvv * c) * pitch
            t = cv2.remap(smooth, xc.astype(F32), yc.astype(F32), cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            t = np.clip(t * gain, 0, 1)
            if constant_r is None:
                r = pitch * np.sqrt(t / np.pi) * (1.0 + 0.25 * ac.smoothstep(0.5, 1.0, t))
                r = np.where(t < 0.015, 0.0, r)
            else:
                r = np.where(t > 0.5, constant_r * pitch, 0.0)
            d = np.hypot(u - cu, v - cvv) * pitch
            out = np.maximum(out, np.clip((r - d) / max(aa, 0.5) + 0.5, 0, 1))
    return out


@op("halftone", 20, "display", category="art", mode="bw", pitch=12.0, angle=45.0, gain=1.0, ink="#14110F", paper="#F6F1E4",
    cmyk_angles=[15.0, 75.0, 0.0, 45.0], inks=["#00A3E0", "#E6007E", "#FFE600", "#14110F"], aa=1.0,
    choices={"mode": ["bw", "color", "cmyk"]})
def halftone(img, p, ctx: Ctx):
    """Ben-Day / halftone dot screens: bw (one rotated screen), cmyk (four screens at 15/75/0/45) or color
    (RGB-complement CMY). Dot size follows the tone (variable-size AM halftone)."""
    pitch = max(2.0, px(ctx, p["pitch"], img))
    aa = max(0.8, px(ctx, p["aa"], img))
    paper = rgb(p["paper"])
    mode = p["mode"]
    if mode == "bw":
        cov = np.clip(1.0 - luma(img), 0, 1) ** 1.0
        layer = _halftone_layer(cov, pitch, float(p["angle"]), float(p["gain"]), aa)[..., None]
        return finish(paper * (1 - layer) + rgb(p["ink"]) * layer)
    c_, m_, y_ = 1 - img[..., 0], 1 - img[..., 1], 1 - img[..., 2]
    k_ = np.minimum(np.minimum(c_, m_), y_)
    inv = np.maximum(1 - k_, 1e-3)
    chans = [(c_ - k_) / inv, (m_ - k_) / inv, (y_ - k_) / inv, k_ * 0.9]
    if mode == "color":
        chans = chans[:3]
    out = np.broadcast_to(paper, img.shape).copy()
    inks = ac.pal(p["inks"])
    for i, cov in enumerate(chans):
        lay = _halftone_layer(np.clip(cov, 0, 1).astype(F32), pitch, float(p["cmyk_angles"][i]), float(p["gain"]), aa)
        out = out * (1 - lay[..., None] * (1 - inks[i]))
    return finish(out)


@op("duotone", 18, "display", category="look", shadow="#1B1464", highlight="#FF2D55", mid=None, mid_pos=0.5, contrast=3.0,
    space="oklab", amount=1.0, invert=False, choices={"space": ["oklab", "srgb"]})
def duotone(img, p, ctx: Ctx):
    """Duotone / tritone gradient map of luminance (piecewise-linear in Oklab by default)."""
    stops = [(0.0, p["shadow"])] + ([(float(p["mid_pos"]), p["mid"])] if p["mid"] else []) + [(1.0, p["highlight"])]
    l = luma(img)
    if p["invert"]:
        l = 1 - l
    if p["contrast"]:
        l = ac.sgain(l, float(p["contrast"]))
    out = ac.apply_lut(l, ac.make_lut(stops, space=p["space"]))
    a = float(p["amount"])
    return finish(img * (1 - a) + out * a if a < 1 else out)


@op("gradient_map", 18, "display", category="look", stops=["#000000", "#FFFFFF"], contrast=0.0, space="oklab", source="luma",
    amount=1.0, invert=False, preserve_luma=0.0, choices={"space": ["oklab", "srgb"], "source": ["luma", "max", "oklab_l"]})
def gradient_map(img, p, ctx: Ctx):
    """Generic gradient map: stops are colours (evenly spaced) or [position, colour] pairs."""
    stops = [tuple(s) if isinstance(s, (list, tuple)) else s for s in p["stops"]]
    src = p["source"]
    t = luma(img) if src == "luma" else (img.max(2) if src == "max" else np.clip(ac.oklab(img)[..., 0], 0, 1))
    if p["invert"]:
        t = 1 - t
    if p["contrast"]:
        t = ac.sgain(t, float(p["contrast"]))
    out = ac.apply_lut(t, ac.make_lut(stops, space=p["space"]))
    if p["preserve_luma"]:
        out = out * (1 - p["preserve_luma"]) + out * ((luma(img) + 1e-3) / (luma(out) + 1e-3))[..., None] * p["preserve_luma"]
    a = float(p["amount"])
    return finish(img * (1 - a) + out * a if a < 1 else out)


@op("kmeans_quantize", 20, "display", category="art", k=6, space="oklab", prefilter=0.0, palette=[], assign="rank",
    smooth=0.0, seed=0, sample=20000, spatial=0.0, choices={"space": ["oklab", "srgb"], "assign": ["rank", "nearest"]})
def kmeans_quantize(img, p, ctx: Ctx):
    """k-means colour quantisation (cv2.kmeans on an Oklab pixel sample). Flat cluster colours, or each cluster
    mapped onto a palette (rank = by lightness order, nearest = closest colour)."""
    src = ac.edge_preserve(img, ctx, 40, float(p["prefilter"])) if p["prefilter"] > 0 else img
    feat = ac.oklab(src) if p["space"] == "oklab" else src
    k = int(p["k"])
    if p["spatial"] > 0:
        h, w = img.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w].astype(F32)
        feat = np.concatenate([feat, (xx / w * p["spatial"])[..., None], (yy / h * p["spatial"])[..., None]], -1)
    centers, labels = ac.kmeans_cluster(feat, k, int(p["seed"]) + int(getattr(ctx, "seed", 0)), int(p["sample"]))
    k = len(centers)
    if p["smooth"] > 0:
        sig = px(ctx, p["smooth"], img)
        oh = np.stack([ac.blur((labels == i).astype(F32), sig) for i in range(k)], -1)
        labels = np.argmax(oh, -1).astype(np.int32)
    means, _ = ac.label_means(img, labels, k)
    if not p["palette"]:
        return finish(means[labels])
    pl = ac.pal(p["palette"])
    lum_c = luma(means.reshape(1, k, 3))[0]
    if p["assign"] == "rank":
        rank = np.empty(k, int)
        rank[np.argsort(lum_c)] = np.arange(k)
        mapping = pl[np.round(rank / max(k - 1, 1) * (len(pl) - 1)).astype(int)]
    else:
        mapping = pl[ac.nearest_palette(means.reshape(1, k, 3), pl)[0]]
    return finish(mapping[labels])


# =========================================================================== line / ink layers
@op("xdog", 20, "display", category="art", sigma=2.4, k=1.6, sharpness=20.0, epsilon=0.75, phi=1.0, mode="overlay",
    ink="#101010", paper="#FFFFFF", opacity=1.0, flow=0.0, choices={"mode": ["overlay", "replace"]})
def xdog(img, p, ctx: Ctx):
    """XDoG ink layer (Winnemoeller 2012): S = (1+p)G_s - pG_ks, soft threshold tanh(phi(S-eps)). overlay multiplies
    the ink lines onto the image; replace outputs ink on paper. sigma in px@2000, phi on the 0..1 scale."""
    L = luma(img)
    sig = max(0.5, px(ctx, p["sigma"], img))
    flow = None
    fl = 0
    if p["flow"] > 0:
        tx, ty, _ = ac.structure_flow(ac.blur(L, 1.0), max(2.0, px(ctx, 6, img)))
        flow, fl = (tx, ty), max(2, int(px(ctx, p["flow"], img)))
    T = ac.xdog_map(L, sig, float(p["k"]), float(p["sharpness"]), float(p["epsilon"]), float(p["phi"]), flow, fl)
    a = float(p["opacity"])
    ink, paper = rgb(p["ink"]), rgb(p["paper"])
    if p["mode"] == "overlay":
        line = ink + (1 - ink) * T[..., None]          # ink where T=0, white where T=1
        out = img * (1 - a) + img * line * a
        return finish(out)
    return finish(ink * (1 - T[..., None]) + paper * T[..., None])


@op("paper_texture", 20, "display", category="effects", strength=0.08, scale=1.0, fibres=0.3, relief=0.5, tint="#FFFFFF",
    tint_amount=0.0, mode="multiply", seed=0, choices={"mode": ["multiply", "overlay"]})
def paper_texture(img, p, ctx: Ctx):
    """Procedural paper: tooth + mottling + fibres with embossed relief, multiplied (or overlaid) onto the image."""
    rng = rng_for(ctx, p, 4)
    h, w = img.shape[:2]
    sc = max(0.4, float(p["scale"]) * max(h, w) / 2000.0)
    m = ac.paper_multiplier(h, w, rng, float(p["strength"]), sc, float(p["fibres"]), float(p["relief"]))
    if p["mode"] == "overlay":
        out = img + (m - 1.0)[..., None]
    else:
        out = img * m[..., None]
    if p["tint_amount"] > 0:
        out = out * (1 - p["tint_amount"] + p["tint_amount"] * rgb(p["tint"]))
    return finish(out)


# =========================================================================== video-ish / frame
@op("scanlines", 20, "display", category="art", period=4.0, strength=0.25, thickness=0.5, vertical=False, triad=0.0,
    softness=0.6, phase=0.0)
def scanlines(img, p, ctx: Ctx):
    """CRT scanlines (and optional RGB aperture-grille triad)."""
    h, w = img.shape[:2]
    per = max(2.0, px(ctx, p["period"], img))
    n = w if p["vertical"] else h
    pos = (np.arange(n, dtype=F32) + float(p["phase"])) / per
    ph = pos - np.floor(pos)
    d = np.abs(ph - 0.5) * 2.0
    dark = ac.smoothstep(1.0 - float(p["thickness"]) - float(p["softness"]) * 0.5, 1.0 - float(p["thickness"]) + float(p["softness"]) * 0.5, d)
    m = 1.0 - float(p["strength"]) * dark
    m = m[None, :, None] if p["vertical"] else m[:, None, None]
    out = img * m
    if p["triad"] > 0:
        x = (np.arange(w) % 3)
        mask = np.ones((1, w, 3), F32) * (1 - p["triad"])
        for c in range(3):
            mask[0, x == c, c] = 1.0
        out = out * mask
    return finish(out)


@op("border", 20, "display", category="art", style="solid", width=60.0, color="#F6F1E4", fit="shrink", seed=0,
    inner_color="#1A1A1A", choices={"style": ["solid", "polaroid", "instax", "rough", "rounded", "double"],
                                    "fit": ["shrink", "overlay"]})
def border(img, p, ctx: Ctx):
    """Frame / border (size-preserving: the picture is shrunk into the frame or painted over its edge)."""
    h, w = img.shape[:2]
    bw_ = px(ctx, p["width"], img)
    style = p["style"]
    col = rgb(p["color"])
    top = left = right = bottom = bw_
    if style == "polaroid":
        bottom = bw_ * 3.2
    elif style == "instax":
        left, right, top, bottom = bw_ * 1.2, bw_ * 1.2, bw_ * 1.2, bw_ * 3.6
    top, left, right, bottom = [int(round(v)) for v in (top, left, right, bottom)]
    canvas = np.empty_like(img)
    canvas[:] = col
    rng = rng_for(ctx, p, 5)
    iw, ih = max(2, w - left - right), max(2, h - top - bottom)
    if p["fit"] == "shrink":
        inner = cv2.resize(img, (iw, ih), interpolation=cv2.INTER_AREA)
    else:
        inner = img[top:top + ih, left:left + iw]
    if style == "rounded":
        r = int(min(iw, ih) * 0.06)
        m = np.zeros((ih, iw), np.uint8)
        cv2.rectangle(m, (r, 0), (iw - r - 1, ih - 1), 255, -1)
        cv2.rectangle(m, (0, r), (iw - 1, ih - r - 1), 255, -1)
        for cx, cy in ((r, r), (iw - r - 1, r), (r, ih - r - 1), (iw - r - 1, ih - r - 1)):
            cv2.circle(m, (cx, cy), r, 255, -1)
        mm = ac.blur(m.astype(F32) / 255.0, 0.8)[..., None]
        base = canvas[top:top + ih, left:left + iw]
        canvas[top:top + ih, left:left + iw] = inner * mm + base * (1 - mm)
    elif style == "rough":
        mask = np.zeros((h, w), F32)
        mask[top:top + ih, left:left + iw] = 1.0
        n = ac.noise_unit((h, w), rng, max(1.5, bw_ * 0.15), 3)
        dist = cv2.distanceTransform((mask > 0.5).astype(np.uint8), cv2.DIST_L2, 3)
        m = ac.smoothstep(0.0, 1.0, dist / max(bw_ * 0.5, 1.0) + n * 0.35 - 0.1)
        canvas = canvas * (1 - m[..., None]) + np.pad(inner, ((top, h - top - ih), (left, w - left - iw), (0, 0)), mode="edge") * m[..., None]
    else:
        canvas[top:top + ih, left:left + iw] = inner
    if style == "double":
        t = max(1, int(bw_ * 0.06))
        g = int(bw_ * 0.35)
        cv2.rectangle(canvas, (left - g, top - g), (left + iw + g - 1, top + ih + g - 1), tuple(float(c) for c in rgb(p["inner_color"])), t, cv2.LINE_AA)
    return finish(canvas)


@op("letterbox", 20, "display", category="art", aspect=2.39, color="#000000", feather=0.0, offset=0.0)
def letterbox(img, p, ctx: Ctx):
    """Cinema letterbox / pillarbox bars painted over the frame for the target aspect (size-preserving)."""
    h, w = img.shape[:2]
    target = float(p["aspect"])
    cur = w / h
    m = np.ones((h, w), F32)
    if target > cur:   # bars top/bottom
        vis = w / target
        y0 = (h - vis) / 2.0 + float(p["offset"]) * h
        yy = np.arange(h, dtype=F32)[:, None]
        f = max(0.5, px(ctx, p["feather"], img))
        m = np.clip(np.minimum(yy - y0, (y0 + vis) - yy) / f + 0.5, 0, 1) * np.ones((1, w), F32)
    elif target < cur:
        vis = h * target
        x0 = (w - vis) / 2.0 + float(p["offset"]) * w
        xx = np.arange(w, dtype=F32)[None, :]
        f = max(0.5, px(ctx, p["feather"], img))
        m = np.clip(np.minimum(xx - x0, (x0 + vis) - xx) / f + 0.5, 0, 1) * np.ones((h, 1), F32)
    return finish(img * m[..., None] + rgb(p["color"]) * (1 - m[..., None]))


# =========================================================================== blur-based looks
@op("tilt_shift", 19, "display", category="effects", focus_y=0.5, focus_height=0.18, angle=0.0, blur=14.0, falloff=0.35,
    saturation=1.2, contrast=1.1)
def tilt_shift(img, p, ctx: Ctx):
    """Miniature-model look: blur grows with distance from a (rotatable) in-focus band; extra saturation / contrast."""
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    a = np.radians(float(p["angle"]))
    d = ((yy / h - float(p["focus_y"])) * np.cos(a) - (xx / w - 0.5) * np.sin(a) * w / h)
    dist = np.maximum(np.abs(d) - float(p["focus_height"]) / 2.0, 0.0) / max(float(p["falloff"]), 1e-3)
    t = np.clip(dist, 0, 1)
    smax = max(0.8, px(ctx, p["blur"], img))
    levels = [img] + [ac.blur(img, smax * f) for f in (0.33, 0.66, 1.0)]
    pos = t * 3.0
    i0 = np.clip(np.floor(pos).astype(int), 0, 2)
    fr = (pos - i0)[..., None]
    stack = np.stack(levels, 0)
    a0 = np.take_along_axis(stack, i0[None, ..., None].repeat(3, -1), 0)[0]
    a1 = np.take_along_axis(stack, (i0 + 1)[None, ..., None].repeat(3, -1), 0)[0]
    out = a0 * (1 - fr) + a1 * fr
    out = ac.saturate(out, float(p["saturation"]))
    out = (out - 0.5) * float(p["contrast"]) + 0.5
    return finish(out)


@op("fog_haze", 19, "display", category="effects", amount=0.2, color="#DDE3E8", shadow_lift=0.5, gradient=0.0)
def fog_haze(img, p, ctx: Ctx):
    """Atmospheric haze: veil of `color`, stronger in the shadows (lifts blacks); gradient>0 thickens toward the bottom."""
    h, w = img.shape[:2]
    l = luma(img)
    f = float(p["amount"]) * ((1 - float(p["shadow_lift"])) + float(p["shadow_lift"]) * (1 - l))
    if p["gradient"]:
        ramp = np.linspace(0, 1, h, dtype=F32)[:, None]
        g = float(p["gradient"])
        f = f * (1 + g * (2 * ramp - 1))
    f = np.clip(f, 0, 1)[..., None]
    return finish(img * (1 - f) + rgb(p["color"]) * f)


@op("dust_scratches", 19, "display", category="effects", scratches=6, dust=120, hairs=2, strength=0.5, light=True,
    dark_fraction=0.3, seed=0)
def dust_scratches(img, p, ctx: Ctx):
    """Film / plate damage: long vertical scratches, dust specks and a few hairs (white by default, some dark)."""
    rng = rng_for(ctx, p, 6)
    h, w = img.shape[:2]
    sc = max(0.4, max(h, w) / 2000.0)
    layer = np.zeros((h, w), np.float32)
    dark = np.zeros((h, w), np.float32)
    for _ in range(int(p["scratches"])):
        x = float(rng.uniform(0, w))
        y0 = float(rng.uniform(-0.1, 0.7) * h)
        y1 = y0 + float(rng.uniform(0.15, 0.7) * h)
        pts = np.stack([x + np.cumsum(rng.normal(0, 0.6 * sc, 12)) , np.linspace(y0, y1, 12)], 1).astype(np.int32)
        tgt = dark if rng.random() < p["dark_fraction"] else layer
        cv2.polylines(tgt, [pts.reshape(-1, 1, 2)], False, float(rng.uniform(0.4, 1.0)), 1, cv2.LINE_AA)
    for _ in range(int(p["dust"])):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = max(1, int(rng.uniform(0.6, 2.8) * sc))
        tgt = dark if rng.random() < p["dark_fraction"] else layer
        cv2.circle(tgt, (x, y), r, float(rng.uniform(0.4, 1.0)), -1, cv2.LINE_AA)
    for _ in range(int(p["hairs"])):
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        pts = [(x, y)]
        a = rng.uniform(0, 2 * np.pi)
        for _ in range(14):
            a += rng.normal(0, 0.5)
            x += np.cos(a) * 12 * sc
            y += np.sin(a) * 12 * sc
            pts.append((x, y))
        cv2.polylines(dark, [np.array(pts, np.int32).reshape(-1, 1, 2)], False, 0.9, 1, cv2.LINE_AA)
    layer = ac.blur(layer, 0.5 * sc)
    dark = ac.blur(dark, 0.5 * sc)
    s = float(p["strength"])
    out = img + (layer * s)[..., None] * (1.0 if p["light"] else 0.0)
    out = out * (1 - (dark * s * 0.8)[..., None])
    return finish(out)


ac.finalize_docs(__name__)
