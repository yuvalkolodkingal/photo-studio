"""Painterly / graphic raster ops (slot C): Hertzmann brush strokes (Van Gogh, Monet, pointillist, wash), pointillism,
oil paint, watercolour, pencil, XDoG ink / charcoal / woodcut / pastel, cel toon, low-poly, pixel art, mosaic,
Mondrian, Klimt gold. Recipes: research_notes/art_modes_styles.md KQ2 (Hertzmann 1998, Winnemoeller 2012).

Timing (1600 px long edge, 2 CPU cores): all < 1.5 s except `painterly` / `van_gogh` / `monet` (Hertzmann strokes,
~2-6 s depending on style; they work at <= `work_edge` px and upsample), `klimt` (~2 s) and `mosaic` (SLIC, ~1.5 s)."""
from __future__ import annotations

import cv2
import numpy as np

from ... import color
from ..registry import Ctx, op
from . import art_common as ac
from .art_common import F32, finish, luma, px, rng_for, rgb

# =========================================================================== Hertzmann painterly strokes
STROKE_STYLES = {
    # T = error threshold (0..441 on 8-bit RGB distance), R = brush radii (px at 1000 px width), fc curvature filter,
    # fs blur factor, minlen/maxlen = stroke control points, alpha opacity, fg grid factor, jitter hsv / rgb
    "impressionist": dict(T=100, R=(8, 4, 2), fc=1.0, fs=0.5, minlen=4, maxlen=16, alpha=1.0, fg=1.0, jh=0.0, js=0.0, jv=0.0, jrgb=0.0),
    "expressionist": dict(T=50, R=(8, 4, 2), fc=0.25, fs=0.5, minlen=10, maxlen=16, alpha=0.7, fg=1.0, jh=0.0, js=0.0, jv=0.5, jrgb=0.0),
    "colorist": dict(T=200, R=(8, 4, 2), fc=1.0, fs=0.5, minlen=4, maxlen=16, alpha=0.5, fg=1.0, jh=0.0, js=0.0, jv=0.0, jrgb=0.3),
    "pointillist": dict(T=100, R=(4, 2), fc=1.0, fs=0.5, minlen=0, maxlen=0, alpha=1.0, fg=0.5, jh=0.3, js=0.0, jv=1.0, jrgb=0.0),
}


def _sample(arr, x, y):
    h, w = arr.shape[:2]
    return arr[np.clip((y + 0.5).astype(np.int32), 0, h - 1), np.clip((x + 0.5).astype(np.int32), 0, w - 1)]


def _trace_strokes(ref_b, canvas, gx, gy, seeds_x, seeds_y, R, st, rng):
    """Vectorised makeSplineStroke for all seeds at once. Returns points (n, m, 2) float and valid counts."""
    n = len(seeds_x)
    h, w = ref_b.shape[:2]
    x, y = seeds_x.astype(F32), seeds_y.astype(F32)
    col = _sample(ref_b, x, y)
    X = np.empty((st["maxlen"] + 1, n), F32)
    Y = np.empty_like(X)
    X[0], Y[0] = x, y
    count = np.ones(n, np.int32)
    active = np.ones(n, bool)
    a0 = rng.uniform(0, 2 * np.pi, n).astype(F32)
    lx, ly = np.cos(a0), np.sin(a0)  # initial direction used where there is no gradient
    has_last = np.zeros(n, bool)
    for i in range(1, st["maxlen"] + 1):
        if i > st["minlen"]:
            rc = _sample(ref_b, x, y)
            cd = np.linalg.norm(rc - _sample(canvas, x, y), axis=1)
            sd = np.linalg.norm(rc - col, axis=1)
            active &= ~(cd < sd)
        if not active.any():
            break
        ggx, ggy = _sample(gx, x, y), _sample(gy, x, y)
        gm = np.hypot(ggx, ggy)
        nx_, ny_ = -ggy, ggx
        flat = gm < 1e-3
        nx_ = np.where(flat, lx, nx_ / np.maximum(gm, 1e-6))
        ny_ = np.where(flat, ly, ny_ / np.maximum(gm, 1e-6))
        flip = (nx_ * lx + ny_ * ly) < 0
        nx_, ny_ = np.where(flip, -nx_, nx_), np.where(flip, -ny_, ny_)
        fc = st["fc"]
        dx = fc * nx_ + (1 - fc) * lx
        dy = fc * ny_ + (1 - fc) * ly
        dn = np.maximum(np.hypot(dx, dy), 1e-6)
        dx, dy = dx / dn, dy / dn
        nxp = np.clip(x + R * dx, 0, w - 1)
        nyp = np.clip(y + R * dy, 0, h - 1)
        x = np.where(active, nxp, x)
        y = np.where(active, nyp, y)
        lx, ly = np.where(active, dx, lx), np.where(active, dy, ly)
        X[i], Y[i] = x, y
        count += active.astype(np.int32)
    return X, Y, count, col


def _jitter(col, st, rng):
    n = len(col)
    if st["jrgb"] > 0:
        col = col + st["jrgb"] * (rng.random((n, 3), dtype=F32) - 0.5)
    if st["jh"] or st["js"] or st["jv"]:
        hsv_ = color.rgb_to_hsv(np.clip(col, 0, 1).reshape(n, 1, 3)).reshape(n, 3)
        hsv_[:, 0] = (hsv_[:, 0] + st["jh"] * 360.0 * (rng.random(n, dtype=F32) - 0.5)) % 360.0
        hsv_[:, 1] = np.clip(hsv_[:, 1] + st["js"] * (rng.random(n, dtype=F32) - 0.5), 0, 1)
        hsv_[:, 2] = np.clip(hsv_[:, 2] + st["jv"] * 0.5 * (rng.random(n, dtype=F32) - 0.5), 0, 1)
        col = color.hsv_to_rgb(hsv_.reshape(n, 1, 3)).reshape(n, 3)
    return np.clip(col, 0, 1)


def hertzmann(ref: np.ndarray, st: dict, rng, scale: float, canvas0: np.ndarray | None = None, max_strokes: int = 60000):
    """Hertzmann (1998) layered curved brush strokes. ref float32 display RGB. Returns the painted canvas."""
    h, w = ref.shape[:2]
    canvas = canvas0.copy() if canvas0 is not None else np.ones_like(ref) * 0.95
    first = True
    for R0 in sorted(st["R"], reverse=True):
        R = max(1.0, R0 * scale)
        ref_b = ac.blur(ref, st["fs"] * R)
        diff = np.sqrt(((canvas - ref_b) ** 2).sum(2)) * 255.0
        if first and canvas0 is not None:
            diff = diff + 1e4   # unpainted canvas: the first (largest) layer covers everything
        first = False
        grid = max(1, int(round(st["fg"] * R)))
        gh, gw = h // grid, w // grid
        if gh < 1 or gw < 1:
            continue
        dc = diff[:gh * grid, :gw * grid].reshape(gh, grid, gw, grid).transpose(0, 2, 1, 3).reshape(gh, gw, grid * grid)
        area = dc.mean(2)
        am = dc.argmax(2)
        ys, xs = np.nonzero(area > st["T"])
        if len(ys) == 0:
            continue
        sy = ys * grid + am[ys, xs] // grid
        sx = xs * grid + am[ys, xs] % grid
        order = rng.permutation(len(sy))[:max_strokes]
        sx, sy = sx[order], sy[order]
        Lg = luma(ref_b)
        gx = cv2.Sobel(Lg, cv2.CV_32F, 1, 0, ksize=3) / 8.0
        gy = cv2.Sobel(Lg, cv2.CV_32F, 0, 1, ksize=3) / 8.0
        if st["maxlen"] == 0:
            X, Y, cnt = np.stack([sx, sx])[:1], np.stack([sy, sy])[:1], np.ones(len(sx), np.int32)
            col = _sample(ref_b, sx.astype(F32), sy.astype(F32))
        else:
            X, Y, cnt, col = _trace_strokes(ref_b, canvas, gx, gy, sx, sy, R, st, rng)
        col = _jitter(col, st, rng)
        layer = ac.to_u8(canvas)
        col8 = (col * 255 + 0.5).astype(np.uint8)
        thick = max(1, int(round(2 * R)))
        for i in range(len(sx)):
            c = (int(col8[i, 0]), int(col8[i, 1]), int(col8[i, 2]))
            m = int(cnt[i])
            if m <= 1:
                cv2.circle(layer, (int(X[0, i]), int(Y[0, i])), max(1, int(round(R))), c, -1, cv2.LINE_AA)
            else:
                pts = np.stack([X[:m, i], Y[:m, i]], 1).astype(np.int32).reshape(-1, 1, 2)
                cv2.polylines(layer, [pts], False, c, thick, cv2.LINE_AA)
        a = st["alpha"]
        canvas = canvas * (1 - a) + ac.from_u8(layer) * a
    return canvas.astype(F32)


def _emboss(canvas: np.ndarray, strength: float) -> np.ndarray:
    """Impasto relief: directional emboss of the painted lightness (stroke boundaries read as paint ridges)."""
    k = np.array([[-2, -1, 0], [-1, 1, 1], [0, 1, 2]], F32)
    e = cv2.filter2D(ac.blur(luma(canvas), 0.7), -1, k)
    return 1.0 + strength * np.clip(e / (e.std() + 1e-6), -3, 3) * 0.12


def _swirl_texture(img, ctx, rng, amount):
    """LIC brush-flow texture (Van Gogh recipe B): structure-tensor flow + curl perturbation in flat regions."""
    L = ac.blur(luma(img), 1.0)
    tx, ty, coh = ac.structure_flow(L, max(2.0, px(ctx, 10, img)))
    h, w = L.shape
    flatw = (1 - ac.smoothstep(0.05, 0.4, coh))
    ang = ac.noise_unit((h, w), rng, px(ctx, 60, img), 2) * 1.1
    cx, cy = np.cos(ang), np.sin(ang)
    tx2 = tx * (1 - flatw) + cx * flatw
    ty2 = ty * (1 - flatw) + cy * flatw
    nrm = np.maximum(np.hypot(tx2, ty2), 1e-6)
    noise = rng.random((h, w), dtype=F32)
    t = ac.lic(noise, tx2 / nrm, ty2 / nrm, max(6, int(px(ctx, 24, img))))
    t = (t - t.mean()) / (t.std() + 1e-6)
    return 1.0 + amount * 0.18 * t


def _paint(img, p, ctx, style_name, pre=None):
    rng = rng_for(ctx, p, 31)
    big = max(img.shape[:2])
    work, s = ac.work_scale(img, int(p["work_edge"]))
    if pre is not None:
        work = pre(work)
    st = dict(STROKE_STYLES[style_name])
    if p.get("radii"):
        st["R"] = tuple(p["radii"])
    if p.get("threshold"):
        st["T"] = float(p["threshold"])
    if p.get("alpha"):
        st["alpha"] = float(p["alpha"])
    scale = float(p["brush_scale"]) * max(work.shape[:2]) / 1000.0
    if p["init"] == "paper":
        init = np.ones_like(work) * ac.rgb(p["paper"])
    elif p["init"] == "white":
        init = np.ones_like(work)
    else:  # mean colour: unpainted gaps blend in
        init = np.ones_like(work) * work.reshape(-1, 3).mean(0)
    canvas = hertzmann(work, st, rng, scale, init, max_strokes=int(p["max_strokes"]))
    if p["impasto"] > 0:
        canvas = canvas * _emboss(canvas, float(p["impasto"]))[..., None]
    if p["swirl"] > 0:
        canvas = canvas * _swirl_texture(work, ctx, rng, float(p["swirl"]))[..., None]
    return ac.upscale_like(np.clip(canvas, 0, 1), img.shape)


_PAINT_DEFAULTS = dict(brush_scale=1.0, radii=None, threshold=0.0, alpha=0.0, init="mean", paper="#F1E8D6", impasto=0.0,
                       swirl=0.0, work_edge=1600, max_strokes=60000, seed=0)


@op("painterly", 20, "display", category="art", style="impressionist",
    choices={"style": list(STROKE_STYLES), "init": ["mean", "paper", "white"]}, **_PAINT_DEFAULTS)
def painterly(img, p, ctx: Ctx):
    """Hertzmann (1998) layered curved brush strokes with the published style tables (impressionist, expressionist,
    colorist wash, pointillist). Slow op (2-6 s at 1600 px); works at <= work_edge px."""
    return finish(_paint(img, p, ctx, p["style"]))


def _van_gogh_grade(img):
    out = ac.saturate(img, 1.35)
    hh = ac.hsv(out)
    blue = ac.ang_weight(hh[..., 0], 225, 40) * ac.smoothstep(0.2, 0.5, hh[..., 1])
    yel = ac.ang_weight(hh[..., 0], 52, 14, 10) * ac.smoothstep(0.45, 0.8, hh[..., 1])
    out = ac.tint_toward(out, "#2A4D9B", 0.25, blue)
    out = ac.tint_toward(out, "#F2C541", 0.2, yel)
    return np.clip(out, 0, 1)


def _monet_grade(img):
    out = img * (1 - 0.08) + 0.08
    out = ac.saturate(out, 1.1)
    l = luma(out)
    out = ac.tint_toward(out, "#5B6BB0", 0.15, 1 - ac.smoothstep(0.2, 0.3, l) * 0 - ac.smoothstep(0.0, 0.3, l))
    out = ac.tint_toward(out, "#F6E7C1", 0.10, ac.smoothstep(0.7, 0.9, l))
    return np.clip(out, 0, 1)


@op("van_gogh", 20, "display", category="art", style="expressionist", **{**_PAINT_DEFAULTS, "impasto": 0.6, "swirl": 0.5},
    choices={"style": ["expressionist", "impressionist", "colorist"], "init": ["mean", "paper", "white"]})
def van_gogh(img, p, ctx: Ctx):
    """Post-impressionist (Van Gogh-style): saturated blue/yellow push, expressionist directional strokes, impasto
    emboss and a LIC swirl texture in calm regions. Slow op (see painterly)."""
    return finish(_paint(img, p, ctx, p["style"], pre=_van_gogh_grade))


@op("monet", 20, "display", category="art", style="impressionist", **{**_PAINT_DEFAULTS, "impasto": 0.0},
    choices={"style": ["impressionist", "colorist", "expressionist"], "init": ["mean", "paper", "white"]})
def monet(img, p, ctx: Ctx):
    """Impressionist (Monet-style): lifted blacks, violet shadows, cream highlights, soft broken-colour dabs."""
    return finish(_paint(img, p, ctx, p["style"], pre=_monet_grade))


@op("pointillism", 20, "display", category="art", density=0.015, radius=7.0, canvas="#F1E8D6", hue_jitter=0.02,
    sat_jitter=0.12, val_jitter=0.14, complement=0.12, seed=0)
def pointillism(img, p, ctx: Ctx):
    """Pointillism (Seurat-style): jittered-grid dots sampled from the image, HSV colour jitter, a fraction of dots
    shifted to the complementary hue (optical mixing), on a cream canvas."""
    rng = rng_for(ctx, p, 32)
    h, w = img.shape[:2]
    r = max(1.5, px(ctx, p["radius"], img))
    spacing = max(2.0, r * 1.05)
    gy, gx = np.mgrid[spacing / 2:h:spacing, spacing / 2:w:spacing]
    pts_y = (gy + rng.uniform(-0.45, 0.45, gy.shape) * spacing).ravel()
    pts_x = (gx + rng.uniform(-0.45, 0.45, gx.shape) * spacing).ravel()
    keep = rng.random(len(pts_x)) < min(1.0, float(p["density"]) * 60 + 0.1)
    pts_x, pts_y = pts_x[keep], pts_y[keep]
    ref = ac.blur(img, r * 0.6)
    col = _sample(ref, pts_x.astype(F32), pts_y.astype(F32))
    n = len(col)
    hh = color.rgb_to_hsv(col.reshape(n, 1, 3)).reshape(n, 3)
    comp = rng.random(n) < float(p["complement"])
    hh[:, 0] = (hh[:, 0] + comp * 180.0 + float(p["hue_jitter"]) * 360.0 * (rng.random(n) - 0.5) * 2) % 360.0
    hh[:, 1] = np.clip(hh[:, 1] + float(p["sat_jitter"]) * (rng.random(n) - 0.5) * 2, 0, 1)
    hh[:, 2] = np.clip(hh[:, 2] + float(p["val_jitter"]) * (rng.random(n) - 0.5) * 2, 0, 1)
    col = color.hsv_to_rgb(hh.reshape(n, 1, 3)).reshape(n, 3)
    canvas = ac.to_u8(np.ones_like(img) * ac.rgb(p["canvas"]))
    col8 = (col * 255 + 0.5).astype(np.uint8)
    rad = r * rng.uniform(0.8, 1.2, n)
    for i in rng.permutation(n):
        cv2.circle(canvas, (int(pts_x[i]), int(pts_y[i])), max(1, int(round(rad[i]))), (int(col8[i, 0]), int(col8[i, 1]), int(col8[i, 2])), -1, cv2.LINE_AA)
    return finish(ac.from_u8(canvas))


# =========================================================================== oil paint
def oil_filter(img: np.ndarray, radius: int, levels: int = 24) -> np.ndarray:
    """Oil-paint filter (OpenCV xphoto algorithm, vectorised): per pixel the most frequent intensity bin in the
    (2r+1)^2 neighbourhood wins and the mean colour of that bin is output."""
    radius = max(1, int(radius))
    I = np.clip((luma(img) * (levels - 1) + 0.5).astype(np.int32), 0, levels - 1)
    best = np.zeros(img.shape[:2], F32)
    acc = np.zeros_like(img)
    kk = (2 * radius + 1, 2 * radius + 1)
    for b in range(levels):
        m = (I == b).astype(F32)
        if not m.any():
            continue
        cnt = cv2.boxFilter(m, -1, kk, borderType=cv2.BORDER_REFLECT)
        s = cv2.boxFilter(img * m[..., None], -1, kk, borderType=cv2.BORDER_REFLECT)
        upd = cnt > best
        best = np.where(upd, cnt, best)
        acc = np.where(upd[..., None], s, acc)
    return (acc / np.maximum(best, 1e-6)[..., None]).astype(F32)


@op("oil_paint", 20, "display", category="art", radius=5.0, levels=24, detail=0.4, canvas=0.06, saturation=1.1, seed=0)
def oil_paint(img, p, ctx: Ctx):
    """Oil painting: neighbourhood-mode (xphoto oilPainting algorithm) filter, brush-edge detail boost and a canvas weave."""
    r = max(1, int(round(px(ctx, p["radius"], img))))
    out = oil_filter(img, r, int(p["levels"]))
    if p["detail"] > 0:
        out = ac.unsharp(out, max(0.8, r * 0.35), float(p["detail"]))
    out = ac.saturate(out, float(p["saturation"]))
    if p["canvas"] > 0:
        h, w = out.shape[:2]
        rng = rng_for(ctx, p, 33)
        yy, xx = np.mgrid[0:h, 0:w].astype(F32)
        per = max(2.5, px(ctx, 5.0, img))
        weave = 0.5 * (np.sin(2 * np.pi * xx / per) * np.sin(2 * np.pi * yy / per) + 1)
        weave += 0.3 * ac.noise_unit((h, w), rng, 0.8)
        out = out * (1 + float(p["canvas"]) * (weave - 0.5))[..., None]
    return finish(out)


# =========================================================================== watercolour
@op("watercolor", 20, "display", category="art", levels=7, wet_edge=0.35, bleed=6.0, granulation=0.12, paper=0.06,
    saturation=0.85, lift=0.1, smooth=60.0, seed=0)
def watercolor(img, p, ctx: Ctx):
    """Watercolour: edge-preserving wash, soft-quantised lightness, wet-edge darkening, noise-displaced colour bleed,
    pigment granulation, paper tooth, high-key lift."""
    rng = rng_for(ctx, p, 34)
    h, w = img.shape[:2]
    wash = ac.edge_preserve(img, ctx, float(p["smooth"]), 0.4)
    lab = ac.oklab(wash)
    n = int(p["levels"])
    Lq = np.round(lab[..., 0] * n) / n
    Lq = ac.blur(Lq, px(ctx, 1.5, img))
    edges = cv2.morphologyEx(Lq, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    edges = ac.blur(edges, px(ctx, 2.0, img))
    edges = np.clip(edges / (edges.max() + 1e-6) * 3.0, 0, 1)
    lab[..., 0] = Lq
    out = ac.from_oklab(lab)
    out = out * (1 - float(p["wet_edge"]) * edges)[..., None]
    amp = px(ctx, p["bleed"], img)
    if amp > 0.3:
        yy, xx = np.mgrid[0:h, 0:w].astype(F32)
        dx = ac.noise_unit((h, w), rng, px(ctx, 18, img), 2) * amp
        dy = ac.noise_unit((h, w), rng, px(ctx, 18, img), 2) * amp
        out = cv2.remap(out, xx + dx, yy + dy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        lab2 = ac.oklab(out)   # slight chroma-only offset from the lightness (pigment drifting off the sizing)
        ab = cv2.remap(lab2[..., 1:], xx + dx * 0.5 + amp * 0.3, yy + dy * 0.5 - amp * 0.2, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        lab2[..., 1:] = ab
        out = ac.from_oklab(lab2)
    gran = ac.noise_unit((h, w), rng, max(0.6, px(ctx, 1.2, img)))
    l = luma(out)
    out = out * (1 - float(p["granulation"]) * np.clip(0.5 + 0.5 * gran, 0, 1) * (0.4 + 0.6 * (1 - l)))[..., None]
    out = ac.saturate(out, float(p["saturation"]))
    out = out * (1 - float(p["lift"])) + float(p["lift"])
    out = out * ac.paper_multiplier(h, w, rng, float(p["paper"]), max(0.5, max(h, w) / 2000.0), 0.2)[..., None]
    return finish(out)


# =========================================================================== pencil / ink / charcoal / woodcut / pastel
@op("pencil_sketch", 20, "display", category="art", method="dodge", sigma=10.0, colour=0.0, contrast=1.2, paper="#F7F4EC",
    graphite="#2B2B2E", hatch=0.25, grain=0.04, seed=0, choices={"method": ["dodge", "opencv"]})
def pencil_sketch(img, p, ctx: Ctx):
    """Pencil sketch: colour-dodge of the grey against its blurred negative (fine graphite -> soft charcoal with sigma),
    XDoG hatching for shading, graphite grain on paper; colour>0 adds coloured-pencil tint."""
    rng = rng_for(ctx, p, 35)
    h, w = img.shape[:2]
    g = luma(img)
    if p["method"] == "opencv":
        u8 = ac.to_u8(img)
        s = float(np.clip(60 * max(h, w) / 2000.0, 3, 200))
        gray, colr = cv2.pencilSketch(u8, sigma_s=s, sigma_r=0.07, shade_factor=0.02)
        sk = gray.astype(F32) / 255.0
        col_sk = colr.astype(F32) / 255.0
    else:
        inv = 1.0 - g
        b = ac.blur(inv, max(1.0, px(ctx, p["sigma"], img)))
        sk = np.clip(g / np.maximum(1.0 - b, 0.02), 0, 1)
        col_sk = None
    sk = ac.sgain(np.clip(sk, 0, 1), 3.0 * float(p["contrast"]))
    if p["hatch"] > 0:
        T = ac.xdog_map(g, max(0.6, px(ctx, 1.4, img)), 1.6, 20.0, 0.78, 1.0)
        sk = sk * (1 - float(p["hatch"]) * (1 - T))
    paper, graph = rgb(p["paper"]), rgb(p["graphite"])
    out = graph * (1 - sk[..., None]) + paper * sk[..., None]
    if p["colour"] > 0:
        tint = ac.saturate(img, 1.4)
        out = out * (1 - p["colour"]) + out * (0.55 + 0.45 * tint) * p["colour"] if col_sk is None else out * (1 - p["colour"]) + col_sk * p["colour"]
    if p["grain"] > 0:
        pap = ac.paper_multiplier(h, w, rng, float(p["grain"]), max(0.5, max(h, w) / 2000.0), 0.15)
        out = out * pap[..., None]
    return finish(out)


XDOG_STYLES = {
    # sigma px@2000, k, p, eps, phi (0..1 scale), flow length px@2000, shade (keep tone), colour mix
    "ink":      dict(sigma=2.4, k=1.6, sharp=20.0, eps=0.75, phi=8.0, flow=0, shade=0.0, paper="#FFFFFF", ink="#0F0F12"),
    "pencil":   dict(sigma=1.6, k=1.6, sharp=22.0, eps=0.80, phi=1.0, flow=0, shade=0.25, paper="#F7F4EC", ink="#2B2B2E"),
    "charcoal": dict(sigma=5.0, k=1.6, sharp=70.0, eps=0.80, phi=1.0, flow=18, shade=0.35, paper="#EDE6D8", ink="#18171A"),
    "woodcut":  dict(sigma=3.2, k=1.6, sharp=100.0, eps=0.73, phi=8.3, flow=14, shade=0.0, paper="#F1E5CC", ink="#1E1410"),
    "pastel":   dict(sigma=4.0, k=1.6, sharp=40.0, eps=1.0, phi=1.0, flow=18, shade=0.0, paper="#E9E1D2", ink="#2A2530"),
}


@op("ink_xdog", 20, "display", category="art", style="ink", sigma=0.0, sharpness=0.0, epsilon=0.0, phi=0.0, flow=-1.0,
    shade=-1.0, colour=0.0, ink="", paper="", seed=0, choices={"style": list(XDOG_STYLES)})
def ink_xdog(img, p, ctx: Ctx):
    """Line art / drawing by XDoG (Winnemoeller 2012) with the paper's named looks: ink, pencil, charcoal, woodcut, pastel.
    Params left at their 0/-1/'' defaults take the style table values; flow>0 smooths along the structure tensor."""
    rng = rng_for(ctx, p, 36)
    st = XDOG_STYLES[p["style"]]
    sig = float(p["sigma"] or st["sigma"])
    sharp = float(p["sharpness"] or st["sharp"])
    eps = float(p["epsilon"] or st["eps"])
    phi = float(p["phi"] or st["phi"])
    flow_len = st["flow"] if p["flow"] < 0 else p["flow"]
    shade = st["shade"] if p["shade"] < 0 else p["shade"]
    L = luma(img)
    flow = None
    fl = 0
    if flow_len > 0:
        tx, ty, _ = ac.structure_flow(ac.blur(L, 1.0), max(2.0, px(ctx, 8, img)))
        flow, fl = (tx, ty), max(3, int(px(ctx, flow_len, img)))
    T = ac.xdog_map(L, max(0.6, px(ctx, sig, img)), st["k"], sharp, eps, phi, flow, fl)
    ink, paper = rgb(p["ink"] or st["ink"]), rgb(p["paper"] or st["paper"])
    tone = T
    if shade > 0:
        tone = T * (1 - shade * (1 - ac.blur(L, px(ctx, 3, img))))
    out = ink * (1 - tone[..., None]) + paper * tone[..., None]
    if p["colour"] > 0:
        out = out * (1 - p["colour"]) + out * (0.4 + 0.6 * np.clip(img * 1.1, 0, 1)) * p["colour"]
    h, w = L.shape
    grain = ac.paper_multiplier(h, w, rng, 0.04, max(0.5, max(h, w) / 2000.0), 0.2)
    return finish(out * grain[..., None])


# =========================================================================== cel / toon
@op("cel_toon", 20, "display", category="art", levels=5, soft=0.03, saturation=1.2, edge_block=9, edge_c=2.0,
    edge_method="adaptive", ink="#1A1A1A", passes=3, seed=0, choices={"edge_method": ["adaptive", "xdog"]})
def cel_toon(img, p, ctx: Ctx):
    """Cel / toon shading: half-res bilateral x3, lightness quantised to a few soft steps, saturated colour, ink outlines."""
    h, w = img.shape[:2]
    half = ac.resize_to(img, max(2, w // 2), max(2, h // 2))
    sm = ac.bilateral(half, ctx, 9, 0.3, 20.0, passes=int(p["passes"]))
    sm = ac.upscale_like(sm, img.shape)
    lab = ac.oklab(sm)
    n = int(p["levels"])
    v = lab[..., 0] * n
    f = np.floor(v)
    wd = float(p["soft"]) * n
    q = (f + ac.smoothstep(0.5 - wd, 0.5 + wd, v - f) * 0 + ac.smoothstep(1.0 - wd, 1.0 + wd, v - f)) / n
    lab[..., 0] = np.clip(q + 0.5 / n, 0, 1)
    lab[..., 1:] *= float(p["saturation"])
    out = ac.from_oklab(lab)
    g = ac.to_u8(ac.blur(luma(img), 1.0))
    if p["edge_method"] == "adaptive":
        e = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, ac.odd(px(ctx, p["edge_block"] * 1.4, img), 3), float(p["edge_c"]))
        e = (e > 0).astype(np.uint8)
        e = _clean(e, (px(ctx, 3, img)) ** 2)
        em = ac.blur(ac.dilate(e, px(ctx, 0.8, img)).astype(F32), 0.5)
    else:
        em = 1 - ac.xdog_map(luma(img), max(0.6, px(ctx, 2.0, img)), 1.6, 20, 0.75, 10.0)
    return finish(out * (1 - em[..., None]) + rgb(p["ink"]) * em[..., None])


def _clean(mask, min_area):
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros(n, np.uint8)
    keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= min_area).astype(np.uint8)
    return keep[lab]


# =========================================================================== low-poly / pixel / mosaic / mondrian
@op("low_poly", 20, "display", category="art", points=1500, edge_points=0.7, seam=0.15, seam_color="#111111",
    saturation=1.1, seed=0)
def low_poly(img, p, ctx: Ctx):
    """Low-poly / cubist triangulation: points sampled along Canny edges + uniform + border -> Delaunay -> each triangle
    filled with its mean colour; faint seams."""
    from scipy.spatial import Delaunay

    rng = rng_for(ctx, p, 37)
    h, w = img.shape[:2]
    sm = ac.blur(img, max(0.8, px(ctx, 1.2, img)))
    g = ac.to_u8(luma(sm))
    edges = cv2.Canny(g, 50, 150)
    ey, ex = np.nonzero(edges)
    n = int(p["points"])
    ne = min(int(n * float(p["edge_points"])), len(ex))
    sel = rng.choice(len(ex), ne, replace=False) if ne > 0 else np.array([], int)
    pts = [np.stack([ex[sel], ey[sel]], 1)]
    nr = n - ne
    pts.append(np.stack([rng.uniform(0, w - 1, nr), rng.uniform(0, h - 1, nr)], 1))
    pts.append(np.array([[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1]]))
    nb = 10
    for t in np.linspace(0, 1, nb, endpoint=False)[1:]:
        pts += [np.array([[t * (w - 1), 0], [t * (w - 1), h - 1], [0, t * (h - 1)], [w - 1, t * (h - 1)]])]
    P = np.unique(np.round(np.concatenate(pts, 0)).astype(np.int32), axis=0)
    tri = Delaunay(P.astype(np.float64))
    lab = np.zeros((h, w), np.int32)
    for i, s in enumerate(tri.simplices):
        cv2.fillConvexPoly(lab, P[s].astype(np.int32), int(i + 1))
    k = len(tri.simplices) + 1
    means, cnt = ac.label_means(sm, lab, k)
    out = means[lab]
    out = np.where((lab == 0)[..., None], sm, out)
    out = ac.saturate(out, float(p["saturation"]))
    if p["seam"] > 0:
        seam = np.zeros((h, w), bool)
        seam[:, 1:] |= lab[:, 1:] != lab[:, :-1]
        seam[1:, :] |= lab[1:, :] != lab[:-1, :]
        sm_ = ac.blur(seam.astype(F32), 0.5)[..., None] * float(p["seam"])
        out = out * (1 - sm_) + rgb(p["seam_color"]) * sm_
    return finish(out)


def _cells_up(small: np.ndarray, cell: int, shape) -> np.ndarray:
    big = np.repeat(np.repeat(small, cell, axis=0), cell, axis=1)
    return big[:shape[0], :shape[1]]


@op("pixel_art", 20, "display", category="art", cells=128, colors=24, saturation=1.15, dither="none", dither_amount=0.5,
    outline=0.0, seed=0, choices={"dither": ["none", "bayer"]})
def pixel_art(img, p, ctx: Ctx):
    """Pixel art: area-downscale to `cells` columns, k-means palette (Oklab) with optional ordered dither, optional dark
    outline, integer nearest-neighbour upscale."""
    h, w = img.shape[:2]
    cell = max(1, int(round(w / float(p["cells"]))))
    sw, sh = -(-w // cell), -(-h // cell)
    small = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA)
    small = ac.saturate(small, float(p["saturation"]))
    lab = ac.oklab(np.clip(small, 0, 1))
    centers, _ = ac.kmeans_cluster(lab, int(p["colors"]), int(p["seed"]) + 7, sample=20000)
    if p["dither"] == "bayer":
        b = np.tile(ac.bayer(4), (sh // 4 + 1, sw // 4 + 1))[:sh, :sw]
        lab2 = lab.copy()
        lab2[..., 0] += (b - 0.5) * 0.12 * float(p["dither_amount"])
    else:
        lab2 = lab
    d = (lab2.reshape(-1, 3) ** 2).sum(1, keepdims=True) - 2 * lab2.reshape(-1, 3) @ centers.T + (centers ** 2).sum(1)[None]
    idx = np.argmin(d, 1).reshape(sh, sw)
    q = ac.from_oklab(centers.reshape(1, -1, 3))[0][idx]
    if p["outline"] > 0:
        e = cv2.Canny(ac.to_u8(luma(small)), 50, 140)
        q = q * (1 - float(p["outline"]) * (e > 0)[..., None] * 0.8)
    return finish(_cells_up(q, cell, (h, w)))


@op("mosaic", 20, "display", category="art", shape="slic", tiles=2500, compactness=30.0, grout="#2E2A26", grout_width=1.5,
    tile_noise=0.08, bevel=0.1, palette_colors=0, seed=0, choices={"shape": ["slic", "grid"]})
def mosaic(img, p, ctx: Ctx):
    """Mosaic of tesserae: SLIC superpixels (or a square grid) filled with mean colour, grout lines, per-tile lighting
    variation and a subtle bevel."""
    rng = rng_for(ctx, p, 38)
    h, w = img.shape[:2]
    gw_ = max(0.6, px(ctx, p["grout_width"], img))
    if p["shape"] == "grid":
        n = int(p["tiles"])
        cell = max(3, int(round(np.sqrt(h * w / n))))
        sw, sh = -(-w // cell), -(-h // cell)
        small = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA)
        lab_small = np.arange(sw * sh, dtype=np.int32).reshape(sh, sw)
        lab = _cells_up(lab_small[..., None].astype(F32), cell, (h, w))[..., 0].astype(np.int32)
        means = small.reshape(-1, 3)
    else:
        from skimage.segmentation import slic

        work, s = ac.work_scale(img, 900)
        seg = slic(work, n_segments=int(p["tiles"] * s * s), compactness=float(p["compactness"]),
                   sigma=1.0, start_label=0, channel_axis=-1, convert2lab=True)
        lab = cv2.resize(seg.astype(np.int32), (w, h), interpolation=cv2.INTER_NEAREST) if s < 1 else seg
        k = int(lab.max()) + 1
        means, _ = ac.label_means(img, lab, k)
    k = len(means)
    if p["palette_colors"] > 0:
        c, _ = ac.kmeans_cluster(ac.oklab(means.reshape(1, k, 3)), int(p["palette_colors"]), int(p["seed"]) + 9, sample=5000)
        means = ac.from_oklab(c.reshape(1, -1, 3))[0][ac.nearest_palette(means.reshape(1, k, 3), ac.from_oklab(c.reshape(1, -1, 3))[0])[0]]
    light = 1.0 + float(p["tile_noise"]) * rng.standard_normal(k).astype(F32)
    out = np.clip(means * light[:, None], 0, 1)[lab]
    b = np.zeros((h, w), bool)
    b[:, 1:] |= lab[:, 1:] != lab[:, :-1]
    b[1:, :] |= lab[1:, :] != lab[:-1, :]
    bm = ac.dilate(b.astype(np.uint8), gw_ * 0.5).astype(F32)
    if p["bevel"] > 0:
        dist = cv2.distanceTransform(1 - ac.dilate(b.astype(np.uint8), gw_ * 0.5), cv2.DIST_L2, 3)
        rim = np.clip(1.0 - dist / max(gw_ * 2.0, 1.0), 0, 1)
        out = out * (1 + float(p["bevel"]) * rim[..., None] * 0.6)
    bm = ac.blur(bm, 0.5)[..., None]
    return finish(out * (1 - bm) + rgb(p["grout"]) * bm)


MONDRIAN = ["#F5F3EE", "#E52521", "#1A4C9C", "#FAD201", "#000000"]


@op("mondrian", 20, "display", category="art", palette=MONDRIAN, min_cell=0.06, max_depth=5, variance=0.06, line=14.0,
    white_bias=3.0, black_cells=0.0, seed=0)
def mondrian(img, p, ctx: Ctx):
    """Mondrian grid: image-adaptive quadtree (splits snap to the strongest edge near the middle), leaves coloured with
    the nearest of white / red / blue / yellow (white weighted), thick black dividing lines."""
    rng = rng_for(ctx, p, 39)
    h, w = img.shape[:2]
    work, s = ac.work_scale(img, 256)
    wh, ww = work.shape[:2]
    lab = ac.oklab(work)
    L = lab[..., 0]
    gx = np.abs(cv2.Sobel(L, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(L, cv2.CV_32F, 0, 1, ksize=3))
    pl = ac.pal(p["palette"])
    pl_lab = ac.oklab(pl.reshape(1, -1, 3))[0]
    min_w = max(4, int(float(p["min_cell"]) * ww))
    leaves = []

    def split(x0, y0, x1, y1, depth):
        rw, rh = x1 - x0, y1 - y0
        reg = L[y0:y1, x0:x1]
        if depth >= int(p["max_depth"]) or (rw < 2 * min_w and rh < 2 * min_w) or reg.std() < float(p["variance"]) * 0.5 * (1 + 0.0 * depth):
            leaves.append((x0, y0, x1, y1))
            return
        vertical = rw >= rh
        if vertical and rw < 2 * min_w:
            vertical = False
        if (not vertical) and rh < 2 * min_w:
            vertical = True
        if vertical:
            prof = gx[y0:y1, x0:x1].mean(0)
            lo, hi = int(rw * 0.3), int(rw * 0.7)
            lo, hi = max(lo, min_w), min(hi, rw - min_w)
            if hi <= lo:
                leaves.append((x0, y0, x1, y1)); return
            c = x0 + lo + int(np.argmax(prof[lo:hi]))
            split(x0, y0, c, y1, depth + 1); split(c, y0, x1, y1, depth + 1)
        else:
            prof = gy[y0:y1, x0:x1].mean(1)
            lo, hi = int(rh * 0.3), int(rh * 0.7)
            lo, hi = max(lo, min_w), min(hi, rh - min_w)
            if hi <= lo:
                leaves.append((x0, y0, x1, y1)); return
            c = y0 + lo + int(np.argmax(prof[lo:hi]))
            split(x0, y0, x1, c, depth + 1); split(x0, c, x1, y1, depth + 1)

    split(0, 0, ww, wh, 0)
    out = np.zeros((h, w, 3), F32) + rgb("#000000")
    t = max(2, int(round(px(ctx, p["line"], img))))
    sx, sy = w / ww, h / wh
    wbias = float(p["white_bias"])
    for (x0, y0, x1, y1) in leaves:
        mean = lab[y0:y1, x0:x1].reshape(-1, 3).mean(0)
        d = np.linalg.norm(pl_lab - mean, axis=1)
        d[0] = d[0] / wbias
        if float(p["black_cells"]) <= 0:
            d[4] = 1e9
        ci = int(np.argmin(d))
        X0, X1 = int(round(x0 * sx)), int(round(x1 * sx))
        Y0, Y1 = int(round(y0 * sy)), int(round(y1 * sy))
        a0 = t // 2 if X0 > 0 else 0
        a1 = t - t // 2 if X1 < w else 0
        b0 = t // 2 if Y0 > 0 else 0
        b1 = t - t // 2 if Y1 < h else 0
        out[Y0 + b0:Y1 - b1, X0 + a0:X1 - a1] = pl[ci]
    return finish(out)


# =========================================================================== Klimt gold
def _saliency(img: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(np.clip(ac.blur(img, 2.0), 0, 1), cv2.COLOR_RGB2Lab)
    mean = lab.reshape(-1, 3).mean(0)
    s = np.linalg.norm(lab - mean, axis=2)
    h, w = s.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    centre = np.exp(-(((xx / w - 0.5) / 0.35) ** 2 + ((yy / h - 0.5) / 0.35) ** 2))
    s = s * (0.4 + 0.6 * centre)
    return s / (s.max() + 1e-6)


def _foreground(img: np.ndarray, iters: int = 3) -> np.ndarray:
    small, sc = ac.work_scale(img, 400)
    sal = _saliency(small)
    thr = cv2.threshold(ac.to_u8(sal), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0] / 255.0
    mask = np.full(sal.shape, cv2.GC_PR_BGD, np.uint8)
    mask[sal > thr * 0.7] = cv2.GC_PR_FGD
    mask[sal > thr * 1.6] = cv2.GC_FGD
    b = max(2, sal.shape[1] // 40)
    mask[:b], mask[-b:], mask[:, :b], mask[:, -b:] = cv2.GC_BGD, cv2.GC_BGD, cv2.GC_BGD, cv2.GC_BGD
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.setRNGSeed(5)
        cv2.grabCut(ac.to_u8(small)[..., ::-1].copy(), mask, None, bgd, fgd, iters, cv2.GC_INIT_WITH_MASK)
        fg = ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).astype(F32)
    except cv2.error:
        fg = (sal > thr).astype(F32)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, ac.ellipse(3))
    return ac.upscale_like(ac.blur(fg, 1.5), img.shape, cv2.INTER_LINEAR)


def _ornament(canvas, cx, cy, r, kind, cols, rng, th):
    c0, c1 = tuple(int(255 * v) for v in cols[0]), tuple(int(255 * v) for v in cols[1])
    cx, cy, r = int(cx), int(cy), max(3, int(r))
    if kind == 0:   # spiral
        t = np.linspace(0, 6 * np.pi, 90)
        rr = r * t / (6 * np.pi)
        pts = np.stack([cx + rr * np.cos(t), cy + rr * np.sin(t)], 1).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(canvas, [pts], False, c0, th, cv2.LINE_AA)
    elif kind == 1:  # concentric circles
        for k in range(1, 4):
            cv2.circle(canvas, (cx, cy), max(1, r * k // 3), c0 if k % 2 else c1, th, cv2.LINE_AA)
    elif kind == 2:  # nested squares
        for k in range(1, 4):
            q = r * k // 3
            cv2.rectangle(canvas, (cx - q, cy - q), (cx + q, cy + q), c0 if k % 2 else c1, th, cv2.LINE_AA)
    elif kind == 3:  # dots
        for dx in (-r // 2, 0, r // 2):
            for dy in (-r // 2, 0, r // 2):
                cv2.circle(canvas, (cx + dx, cy + dy), max(1, r // 7), c1, -1, cv2.LINE_AA)
    else:            # eye / ellipse
        cv2.ellipse(canvas, (cx, cy), (r, max(2, r // 2)), float(rng.integers(0, 180)), 0, 360, c0, th, cv2.LINE_AA)
        cv2.circle(canvas, (cx, cy), max(1, r // 4), c1, -1, cv2.LINE_AA)


@op("klimt", 20, "display", category="art", segments=260, foreground_saturation=0.9, outline="#3B2A12", outline_width=1.2,
    gold=["#8C6A1E", "#D4AF37", "#F4D160"], motif_colors=["#3B2A12", "#F4D160"], sparkle=0.25, seed=0)
def klimt(img, p, ctx: Ctx):
    """Gold-leaf ornament (Klimt-style): saliency + GrabCut foreground stays photographic, background flattened with
    SLIC into gold tiles carrying spiral / circle / square motifs, metallic noise and specular glints."""
    from skimage.segmentation import slic

    rng = rng_for(ctx, p, 40)
    h, w = img.shape[:2]
    fg = _foreground(img)
    work, s = ac.work_scale(img, 700)
    seg = slic(work, n_segments=int(p["segments"]), compactness=18, sigma=1.0, start_label=0, channel_axis=-1, convert2lab=True)
    k = int(seg.max()) + 1
    means, cnt = ac.label_means(work, seg, k)
    lum = luma(means.reshape(1, k, 3))[0]
    golds = ac.pal(p["gold"])
    lvl = np.clip(np.floor(np.argsort(np.argsort(lum)) / k * len(golds)).astype(int), 0, len(golds) - 1)
    base_small = golds[lvl][seg]
    ys, xs = np.mgrid[0:work.shape[0], 0:work.shape[1]]
    cxs = np.bincount(seg.ravel(), weights=xs.ravel(), minlength=k) / np.maximum(cnt, 1)
    cys = np.bincount(seg.ravel(), weights=ys.ravel(), minlength=k) / np.maximum(cnt, 1)
    canvas = ac.to_u8(base_small)
    th = max(1, int(round(px(ctx, 2.0, img) * s)))
    mcols = ac.pal(p["motif_colors"])
    for i in range(k):
        if cnt[i] < 20:
            continue
        _ornament(canvas, cxs[i], cys[i], np.sqrt(cnt[i] / np.pi) * 0.85, i % 5, mcols, rng, th)
    bg = ac.upscale_like(ac.from_u8(canvas), img.shape, cv2.INTER_LINEAR)
    n = ac.noise_unit((h, w), rng, 0.8)
    metal = 1 + 0.08 * n + 0.04 * ac.noise_unit((h, w), rng, px(ctx, 12, img))
    bg = bg * metal[..., None]
    bg = ac.screen(bg, ((n > 2.0).astype(F32) * float(p["sparkle"]))[..., None] * np.array([1.0, 0.95, 0.7], F32))
    fgimg = ac.saturate(img, float(p["foreground_saturation"]))
    edge = np.clip(np.abs(cv2.Sobel(fg, cv2.CV_32F, 1, 0)) + np.abs(cv2.Sobel(fg, cv2.CV_32F, 0, 1)), 0, 1)
    edge = ac.blur(ac.dilate((edge > 0.15).astype(np.uint8), px(ctx, p["outline_width"], img) * 0.5).astype(F32), 0.6)
    out = fgimg * fg[..., None] + bg * (1 - fg[..., None])
    out = out * (1 - edge[..., None] * 0.85) + rgb(p["outline"]) * edge[..., None] * 0.85
    return finish(out)


ac.finalize_docs(__name__)
