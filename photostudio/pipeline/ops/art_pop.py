"""Pop-art raster ops (slot C): silkscreen pop, comic Ben-Day, four-tone stencil, graffiti stencil, bold-outline flat colour.
Recipes: research_notes/art_modes_styles.md KQ1 (palettes are 'designed' starting points, not measured values)."""
from __future__ import annotations

import cv2
import numpy as np

from ..registry import Ctx, op
from . import art_common as ac
from .art_common import F32, finish, luma, px, rng_for, rgb

# --------------------------------------------------------------------------- palettes (dark -> light, designed)
WARHOL_PALETTES = {
    "p1": ["#2E0E36", "#D81B7A", "#F7B7A3", "#F9E04B"],   # hot pink
    "p2": ["#1B2A49", "#2BB5B0", "#F28C28", "#FDE74C"],   # turquoise
    "p3": ["#3A1F0B", "#E4232A", "#F6A5B4", "#FFF3B0"],   # orange
    "p4": ["#1F1B2E", "#7B5AA6", "#8FD3C7", "#F5F0D6"],   # lilac
    "p5": ["#0F2F1F", "#1FA05A", "#F2E24B", "#FFF6E0"],
    "p6": ["#101840", "#2B63D9", "#FF8FB8", "#FFF0C8"],
    "p7": ["#1A0A0A", "#C8102E", "#F9B233", "#F6EBDC"],
    "p8": ["#2A0A3A", "#8E2DC9", "#B6E23A", "#FFF8D6"],
    "p9": ["#05222A", "#0AA5C8", "#FF7A1A", "#FFF1B8"],
}
PALETTE_ORDER = list(WARHOL_PALETTES)
COMIC = {"red": "#E3191B", "yellow": "#F8D21C", "blue": "#1D4FA1", "white": "#FFFFFF", "cream": "#FFF3D6"}
COMIC_DOTS = {"cream": ("#FFF3D6", "#E3191B"), "blue": ("#FFFFFF", "#1D4FA1"), "red": ("#FFFFFF", "#E3191B"),
              "yellow": ("#FFF6B0", "#F8D21C")}
HOPE = ["#0B3A5D", "#D32A27", "#7098C6", "#F2E9D0"]
HARING = ["#E6332A", "#FFD500", "#2C79C5", "#3CB44B", "#FFFFFF"]


def _shift(layer: np.ndarray, dx: float, dy: float, fill) -> np.ndarray:
    h, w = layer.shape[:2]
    m = np.array([[1, 0, dx], [0, 1, dy]], F32)
    return cv2.warpAffine(layer, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=fill)


def _levels_by_quantile(l: np.ndarray, n: int, mix: float = 0.6) -> np.ndarray:
    edges = np.linspace(0, 1, n + 1)[1:-1]
    qe = np.quantile(l[::2, ::2], np.linspace(0, 1, n + 1)[1:-1])
    return np.searchsorted(edges * (1 - mix) + qe * mix, l)


def _warhol_panel(img: np.ndarray, ctx, palette: np.ndarray, p: dict, rng) -> np.ndarray:
    flat = ac.edge_preserve(img, ctx, float(p["smooth"]), float(p["sigma_r"]))
    l = luma(flat)
    n = len(palette)
    if p["mode"] == "kmeans":
        centers, labels = ac.kmeans_cluster(ac.oklab(flat), int(p["kmeans_k"]), int(p["seed"]) + 11)
        k = len(centers)
        order = np.argsort(centers[:, 0])
        rank = np.empty(k, int)
        rank[order] = np.arange(k)
        idx_map = np.round(rank / max(k - 1, 1) * (n - 1)).astype(int)
        idx = idx_map[labels]
    else:
        idx = _levels_by_quantile(l, n)
    out = palette[idx]
    ink = (l < float(p["ink_threshold"])).astype(np.uint8)
    ink = ac.dilate(ink, px(ctx, 1.0, img))
    ink = ac.blur(ink.astype(F32), max(0.6, px(ctx, 1.0, img)))
    dx, dy = px(ctx, p["ink_offset"][0], img), px(ctx, p["ink_offset"][1], img)
    ink = _shift(ink, dx, dy, 0.0)
    if p["texture"] > 0:
        out = out * (1.0 - float(p["texture"]) * rng.random(l.shape, dtype=F32))[..., None]
    k_col = rgb("#111111")
    return out * (1 - ink[..., None]) + k_col * ink[..., None]


@op("warhol", 20, "display", category="art", levels=4, mode="luma", palette="p1", grid="1x1", ink_threshold=0.28,
    ink_offset=[4.0, 3.0], texture=0.02, kmeans_k=5, smooth=60.0, sigma_r=0.4, gutter=0.012, seed=0,
    choices={"mode": ["luma", "kmeans"], "grid": ["1x1", "2x2", "3x3", "1x2", "2x1", "1x3", "3x1"]})
def warhol(img, p, ctx: Ctx):
    """Silkscreen-pop (Warhol-style): edge-preserving smooth, 3-5 flat tone bands -> flat palette fills, dark
    off-register ink layer; grid variants give each panel its own palette."""
    rng = rng_for(ctx, p, 21)
    h, w = img.shape[:2]
    pal_param = p["palette"]
    nx, ny = (int(v) for v in p["grid"].split("x"))
    if isinstance(pal_param, str):
        start = PALETTE_ORDER.index(pal_param) if pal_param in PALETTE_ORDER else 0
        pals = [ac.pal(WARHOL_PALETTES[PALETTE_ORDER[(start + i) % len(PALETTE_ORDER)]]) for i in range(nx * ny)]
    else:
        pals = [ac.pal(pal_param)] * (nx * ny)
    if nx == 1 and ny == 1:
        return finish(_warhol_panel(img, ctx, pals[0], p, rng))
    g = int(round(float(p["gutter"]) * w))
    pw, ph = (w - g * (nx - 1)) // nx, (h - g * (ny - 1)) // ny
    out = np.ones_like(img)
    small = cv2.resize(img, (pw, ph), interpolation=cv2.INTER_AREA)
    sub_ctx = Ctx(seed=ctx.seed)
    for j in range(ny):
        for i in range(nx):
            q = dict(p)
            # keep ink offset proportional to the panel size relative to the full image
            panel = _warhol_panel(small, ctx, pals[j * nx + i], q, rng)
            x0, y0 = i * (pw + g), j * (ph + g)
            out[y0:y0 + ph, x0:x0 + pw] = panel
    return finish(out)


# --------------------------------------------------------------------------- comic
def dot_grid(h: int, w: int, pitch: float, radius_frac: float, angle: float, aa: float = 1.0) -> np.ndarray:
    """Uniform Ben-Day dots (equal size on a regular grid): coverage 0..1."""
    th = np.radians(angle)
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    u = (xx * np.cos(th) + yy * np.sin(th)) / pitch
    v = (-xx * np.sin(th) + yy * np.cos(th)) / pitch
    du, dv = u - (np.floor(u) + 0.5), v - (np.floor(v) + 0.5)
    d = np.hypot(du, dv) * pitch
    return np.clip((radius_frac * pitch - d) / max(aa, 0.5) + 0.5, 0, 1).astype(F32)


def _clean_lines(mask: np.ndarray, min_area: float) -> np.ndarray:
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros(n, np.uint8)
    keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= min_area).astype(np.uint8)
    return keep[lab]


@op("lichtenstein", 20, "display", category="art", k=5, pitch=14.0, dot_radius=0.32, outline=5.0, outline_method="canny",
    dots_on=["cream", "blue"], angle=0.0, highlight=0.95, smooth=2, seed=0, choices={"outline_method": ["canny", "adaptive"]})
def lichtenstein(img, p, ctx: Ctx):
    """Comic-print pop (Lichtenstein-style): bilateral + k-means -> primary-colour fills, uniform Ben-Day dots in the
    mid-tone fills, thick black outlines, white highlight slashes."""
    h, w = img.shape[:2]
    sm = ac.bilateral(img, ctx, 9, 0.24, 9.0, passes=int(p["smooth"]))
    centers, labels = ac.kmeans_cluster(ac.oklab(sm), int(p["k"]), int(p["seed"]) + 3)
    k = len(centers)
    means, _ = ac.label_means(sm, labels, k)
    names = list(COMIC)
    pl = ac.pal([COMIC[n] for n in names])
    near = ac.nearest_palette(means.reshape(1, k, 3), pl, (0.6, 1.4, 1.4))[0]
    flat = pl[near][labels]
    nm = near[labels]
    pitch = max(3.0, px(ctx, p["pitch"], img))
    dots = dot_grid(h, w, pitch, float(p["dot_radius"]), float(p["angle"]), px(ctx, 1.0, img, 0.8))
    out = flat.copy()
    for nme in p["dots_on"]:
        if nme not in COMIC_DOTS or nme not in names:
            continue
        bg, ink = (rgb(c) for c in COMIC_DOTS[nme])
        sel = (nm == names.index(nme))[..., None]
        layer = bg * (1 - dots[..., None]) + ink * dots[..., None]
        out = np.where(sel, layer, out)
    gray = ac.to_u8(ac.blur(luma(sm), 1.2))
    if p["outline_method"] == "canny":
        edges = cv2.Canny(gray, 60, 160)
    else:
        edges = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, ac.odd(px(ctx, 11, img), 5), 4)
    t = max(1.0, px(ctx, p["outline"], img))
    edges = ac.dilate((edges > 0).astype(np.uint8), t * 0.5)
    edges = _clean_lines(edges, (t * 3) ** 2)
    hi = (luma(sm) > float(p["highlight"])).astype(F32)
    out = out * (1 - hi[..., None]) + hi[..., None]
    e = ac.blur(edges.astype(F32), 0.6)[..., None]
    return finish(out * (1 - e))


# --------------------------------------------------------------------------- four-tone stencil
@op("fairey_hope", 20, "display", category="art", thresholds=[0.25, 0.5, 0.75], palette=HOPE, split="diagonal",
    split_angle=-30.0, split_pos=0.5, equalize=True, caption_band=False, caption_text="", smooth=1.0,
    choices={"split": ["none", "diagonal", "hue"]})
def fairey_hope(img, p, ctx: Ctx):
    """Four-tone stencil poster: navy / red / light blue / cream. Optional split swaps the two middle colours on one
    side of a diagonal (or on warm hues) for the two-colour-face look. Flat fills, no texture."""
    h, w = img.shape[:2]
    pl = ac.pal(p["palette"])
    g = ac.bilateral(luma(img)[..., None].repeat(3, -1), ctx, 9, 0.2, 9.0)[..., 0]
    if p["equalize"]:
        cl = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        g = cl.apply(ac.to_u8(g)).astype(F32) / 255.0
    g = ac.blur(g, px(ctx, p["smooth"], img))
    idx = np.searchsorted(np.asarray(p["thresholds"], F32), g)
    pl_b = pl.copy()
    pl_b[[1, 2]] = pl[[2, 1]]
    if p["split"] == "none":
        side = np.zeros((h, w), bool)
    elif p["split"] == "hue":
        hh = ac.hsv(img)
        side = (ac.ang_weight(hh[..., 0], 20, 40) * np.clip(hh[..., 1] / 0.2, 0, 1)) > 0.4
    else:
        a = np.radians(float(p["split_angle"]))
        yy, xx = np.mgrid[0:h, 0:w].astype(F32)
        d = (xx / w - 0.5) * np.sin(a) * w / h + (yy / h - 0.5) * np.cos(a)
        side = d > (float(p["split_pos"]) - 0.5)
    out = np.where(side[..., None], pl_b[idx], pl[idx])
    if p["caption_band"]:
        bh = int(h * 0.13)
        out[h - bh:] = pl[0]
        if p["caption_text"]:
            sc = h / 900.0
            (tw, th), _ = cv2.getTextSize(p["caption_text"], cv2.FONT_HERSHEY_DUPLEX, 1.6 * sc, max(2, int(3 * sc)))
            cv2.putText(out, p["caption_text"], ((w - tw) // 2, h - bh // 2 + th // 2), cv2.FONT_HERSHEY_DUPLEX, 1.6 * sc,
                        tuple(float(c) for c in pl[3]), max(2, int(3 * sc)), cv2.LINE_AA)
    return finish(out)


# --------------------------------------------------------------------------- stencil graffiti
@op("banksy_stencil", 20, "display", category="art", levels=2, threshold=-1.0, blur=1.0, clean=3.0, min_area=0.0003,
    overspray=9.0, overspray_strength=0.3, wall="#CFC9BC", ink="#121212", grey="#6E6E6E", wall_texture=0.4,
    noise=0.03, accent=False, accent_sat=1.3, accent_min_area=0.02, seed=0)
def banksy_stencil(img, p, ctx: Ctx):
    """Stencil-graffiti: Otsu / fixed threshold (2 or 3 tones), stencil bridges cleaned, spray overspray halo,
    concrete-wall texture, optional single saturated accent colour."""
    rng = rng_for(ctx, p, 22)
    h, w = img.shape[:2]
    g = ac.blur(luma(img), px(ctx, p["blur"], img))
    g8 = ac.to_u8(g)
    t = (cv2.threshold(g8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0] / 255.0) if p["threshold"] < 0 else float(p["threshold"])
    black = (g < (t * 0.7 if p["levels"] >= 3 else t)).astype(np.uint8)
    greyc = ((g >= t * 0.7) & (g < t * 1.25)).astype(np.uint8) if p["levels"] >= 3 else np.zeros_like(black)
    cl = ac.ellipse(px(ctx, p["clean"], img) * 0.5)
    ma = max(4.0, float(p["min_area"]) * h * w)
    for m in (black, greyc):
        m[:] = cv2.morphologyEx(cv2.morphologyEx(m, cv2.MORPH_OPEN, cl), cv2.MORPH_CLOSE, cl)
        m[:] = _clean_lines(m, ma)
        m[:] = 1 - _clean_lines(1 - m, ma)   # fill tiny holes
    wall = rgb(p["wall"]) * np.ones((h, w, 3), F32)
    tex = ac.noise_unit((h, w), rng, max(1.0, px(ctx, 6, img)), 3)
    fine = ac.noise_unit((h, w), rng, 0.8)
    wall = wall * (1 - float(p["wall_texture"]) * 0.12 * (0.7 * tex + 0.5 * fine))[..., None]
    out = wall.copy()
    bm = black.astype(F32)
    gm = greyc.astype(F32)
    if p["overspray"] > 0:
        halo = ac.blur(np.maximum(bm, gm * 0.6), px(ctx, p["overspray"], img))
        out = out * (1 - float(p["overspray_strength"]) * halo * (1 - bm))[..., None]
    out = out * (1 - gm[..., None]) + rgb(p["grey"]) * gm[..., None]
    out = out * (1 - bm[..., None]) + rgb(p["ink"]) * bm[..., None]
    if p["accent"]:
        hh = ac.hsv(ac.blur(img, 1.5))
        m = ((hh[..., 1] > 0.5) & (hh[..., 2] > 0.3)).astype(np.uint8)
        m = ac.dilate(erode_open(m, 2), 2)
        m = _clean_lines(m, float(p["accent_min_area"]) * h * w)
        acc = np.clip(ac.saturate(img, float(p["accent_sat"])), 0, 1)
        mm = (m.astype(F32) * (1 - bm))[..., None]
        out = out * (1 - mm) + acc * mm
    out = out + float(p["noise"]) * rng.standard_normal((h, w, 1), dtype=F32)
    return finish(out)


def erode_open(m, r):
    return cv2.morphologyEx(m, cv2.MORPH_OPEN, ac.ellipse(r))


# --------------------------------------------------------------------------- bold outline / flat colour
@op("haring", 20, "display", category="art", k=4, palette=HARING, outline=9.0, background="#FFFFFF", radiating=True,
    n_marks=10, mark_len=0.04, smooth=2, seed=0)
def haring(img, p, ctx: Ctx):
    """Bold-outline flat colour (Haring-style): 3-4 flat clusters snapped to a primary palette, background cluster
    white, thick uniform black outline, short radiating motion lines from strong corners."""
    rng = rng_for(ctx, p, 23)
    h, w = img.shape[:2]
    sm = ac.bilateral(img, ctx, 9, 0.24, 9.0, passes=int(p["smooth"]))
    centers, labels = ac.kmeans_cluster(ac.oklab(sm), int(p["k"]), int(p["seed"]) + 5)
    k = len(centers)
    means, cnt = ac.label_means(sm, labels, k)
    pl = ac.pal(p["palette"])
    # background cluster = the one touching most of the image border
    border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    bg_c = int(np.bincount(border, minlength=k).argmax())
    near = ac.nearest_palette(means.reshape(1, k, 3), pl, (0.5, 1.5, 1.5))[0]
    cols = pl[near]
    cols[bg_c] = rgb(p["background"])
    out = cols[labels]
    gray = ac.to_u8(ac.blur(luma(sm), 1.2))
    edges = cv2.Canny(gray, 60, 160)
    # cluster boundaries are outlines too
    lab_edge = (cv2.morphologyEx(labels.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0).astype(np.uint8)
    t = max(1.0, px(ctx, p["outline"], img))
    line = ac.dilate(((edges > 0) | (lab_edge > 0)).astype(np.uint8), t * 0.5)
    line = _clean_lines(line, (t * 3) ** 2)
    lm = ac.blur(line.astype(F32), 0.6)
    if p["radiating"]:
        dark = np.zeros((h, w), F32)
        corners = cv2.goodFeaturesToTrack(gray, int(p["n_marks"]), 0.05, max(10, int(w * 0.06)))
        if corners is not None:
            cen = np.array([w / 2.0, h / 2.0])
            L = float(p["mark_len"]) * w
            for c in corners.reshape(-1, 2):
                dvec = c - cen
                n = np.linalg.norm(dvec) + 1e-6
                dvec = dvec / n
                for a in (-0.28, 0.0, 0.28):
                    ca, sa = np.cos(a), np.sin(a)
                    dd = np.array([dvec[0] * ca - dvec[1] * sa, dvec[0] * sa + dvec[1] * ca])
                    s0 = c + dd * t * 1.6
                    s1 = s0 + dd * L * rng.uniform(0.7, 1.2)
                    cv2.line(dark, (int(s0[0]), int(s0[1])), (int(s1[0]), int(s1[1])), 1.0, max(1, int(t * 0.5)), cv2.LINE_AA)
        lm = np.maximum(lm, dark)
    return finish(out * (1 - lm[..., None]))


ac.finalize_docs(__name__)
