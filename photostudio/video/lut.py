"""3D LUT baking for video: a Rec.709-encoded identity lattice is decoded to linear light, pushed through the
colour-only ops of a stack in the pipeline's fixed stage order, re-encoded, and written as a ``.cube``.

Transfer handling (``transfer=``): ``srgb`` (default: identical to how the photo pipeline treats JPEGs, so a
look developed on a still looks the same on video), ``bt709`` (ITU-R BT.709 inverse OETF) or ``gamma24``
(BT.1886 display gamma).  Rec.709 primaries equal sRGB primaries, so only the transfer function differs.
An empty stack yields an identity cube (no shoulder or clipping is added: video white stays at 235).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .. import color
from ..pipeline.ops.basic import output_transform
from ..pipeline.registry import Ctx
from . import stack as vstack
from .ff import VideoError

TRANSFERS = ("srgb", "bt709", "gamma24")


# ----------------------------------------------------------------------------- transfer functions
def decode_transfer(x: np.ndarray, transfer: str) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if transfer == "srgb":
        return color.srgb_eotf(x)
    if transfer == "gamma24":
        return np.power(np.clip(x, 0, None), 2.4).astype(np.float32)
    if transfer == "bt709":
        a = np.abs(x)
        y = np.where(a < 0.081, a / 4.5, np.power((a + 0.099) / 1.099, 1 / 0.45))
        return (np.sign(x) * y).astype(np.float32)
    raise ValueError(f"unknown transfer '{transfer}' (use one of {TRANSFERS})")


def encode_transfer(x: np.ndarray, transfer: str) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if transfer == "srgb":
        return color.srgb_oetf(x)
    if transfer == "gamma24":
        return np.power(np.clip(x, 0, None), 1 / 2.4).astype(np.float32)
    if transfer == "bt709":
        a = np.abs(x)
        y = np.where(a < 0.018, 4.5 * a, 1.099 * np.power(np.maximum(a, 1e-12), 0.45) - 0.099)
        return (np.sign(x) * y).astype(np.float32)
    raise ValueError(f"unknown transfer '{transfer}' (use one of {TRANSFERS})")


# ----------------------------------------------------------------------------- LUT container + IO
@dataclass
class LUT3D:
    """``table[r, g, b] -> (r, g, b)`` float32, shape (N, N, N, 3), values nominally 0..1."""

    table: np.ndarray
    title: str = "photostudio"
    meta: dict = field(default_factory=dict)

    @property
    def size(self) -> int:
        return int(self.table.shape[0])

    def flat(self) -> np.ndarray:
        """(N^3, 3) in .cube order: red index fastest."""
        return np.ascontiguousarray(self.table.transpose(2, 1, 0, 3)).reshape(-1, 3)

    def max_error_vs_identity(self) -> float:
        return float(np.abs(self.table - identity_table(self.size)).max())

    def blend_identity(self, strength: float) -> "LUT3D":
        """strength 0 = identity, 1 = this LUT (linear mix in LUT output space)."""
        if abs(strength - 1.0) < 1e-9:
            return self
        ident = identity_table(self.size)
        return LUT3D((ident + (self.table - ident) * float(strength)).astype(np.float32), self.title, dict(self.meta))

    def apply(self, img: np.ndarray) -> np.ndarray:
        return apply_table(self.table, img)


def identity_table(n: int) -> np.ndarray:
    g = np.linspace(0.0, 1.0, int(n), dtype=np.float32)
    rr, gg, bb = np.meshgrid(g, g, g, indexing="ij")
    return np.stack([rr, gg, bb], -1)


def apply_table(table: np.ndarray, img: np.ndarray) -> np.ndarray:
    """Trilinear lookup of RGB ``img`` (...,3) in a ``[r,g,b]`` table."""
    n = table.shape[0]
    x = np.clip(img, 0, 1) * (n - 1)
    i0 = np.minimum(np.floor(x).astype(np.int32), n - 2)
    f = (x - i0).astype(np.float32)
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    out = 0.0
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = (fr if dr else 1 - fr) * (fg if dg else 1 - fg) * (fb if db else 1 - fb)
                out = out + w * table[r0 + dr, g0 + dg, b0 + db]
    return np.asarray(out, dtype=np.float32)


def write_cube(path: str | Path, lut: LUT3D | np.ndarray, title: str | None = None, size_check: bool = True) -> Path:
    """Write an Adobe/IRIDAS .cube (red fastest).  Accepts a LUT3D or an ``[r,g,b]`` array."""
    if not isinstance(lut, LUT3D):
        lut = LUT3D(np.asarray(lut, np.float32))
    n = lut.size
    if size_check and not 2 <= n <= 256:
        raise VideoError(f"bad LUT size {n}")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    head = [f'TITLE "{(title or lut.title).replace(chr(34), "")}"', f"LUT_3D_SIZE {n}",
            "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    flat = lut.flat()
    body = np.char.mod("%.6f", np.clip(flat, -0.5, 1.5))
    lines = head + [" ".join(row) for row in body]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def read_cube(path: str | Path) -> LUT3D:
    n = None
    title = Path(path).stem
    dmin, dmax = np.zeros(3), np.ones(3)
    rows: list[list[float]] = []
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key = line.split()[0].upper()
        if key == "TITLE":
            title = line.split(None, 1)[1].strip().strip('"') if len(line.split(None, 1)) > 1 else title
        elif key == "LUT_3D_SIZE":
            n = int(line.split()[1])
        elif key == "LUT_1D_SIZE":
            raise VideoError("1D .cube LUTs are not supported (use a 3D LUT)")
        elif key == "DOMAIN_MIN":
            dmin = np.array([float(v) for v in line.split()[1:4]])
        elif key == "DOMAIN_MAX":
            dmax = np.array([float(v) for v in line.split()[1:4]])
        elif key[0] in "-+0123456789.":
            rows.append([float(v) for v in line.split()[:3]])
    if n is None or len(rows) != n ** 3:
        raise VideoError(f"{path}: malformed .cube (size {n}, {len(rows)} rows)")
    arr = np.asarray(rows, np.float32).reshape(n, n, n, 3)  # [b][g][r]
    if not (np.allclose(dmin, 0) and np.allclose(dmax, 1)):
        arr = ((arr - dmin) / np.maximum(dmax - dmin, 1e-9)).astype(np.float32)
    return LUT3D(np.ascontiguousarray(arr.transpose(2, 1, 0, 3)), title)


def write_cube_preferring_core(path: str | Path, lut: LUT3D, title: str | None = None) -> Path:
    """Use the core ops' ``write_cube`` when present (and verified by read-back); else our own writer."""
    for modname in ("photostudio.pipeline.ops.color_ops", "photostudio.pipeline.engine"):
        try:
            fn = importlib.import_module(modname).write_cube
            # core convention (engine.write_cube): array indexed [b][g][r], red fastest when flattened
            fn(path, np.ascontiguousarray(lut.table.transpose(2, 1, 0, 3)), title or lut.title)
            back = read_cube(path)
            if back.size == lut.size and np.abs(back.table - lut.table).max() < 2e-6:
                return Path(path)
        except Exception:
            continue
    return write_cube(path, lut, title)


# ----------------------------------------------------------------------------- applying a stack
def _blend(base: np.ndarray, edited: np.ndarray, entry: dict, ctx: Ctx, domain: str) -> np.ndarray:
    op_ = float(entry.get("opacity", 1.0))
    mask = entry.get("mask")
    if mask:
        try:
            masks = importlib.import_module("photostudio.masks")
            m = masks.build_mask(mask, base, ctx)
            return masks.blend(base, edited, m, entry.get("blend", "normal"), op_, domain)
        except Exception as exc:
            ctx.note(f"mask on '{entry.get('op')}' ignored: {exc}")
    if op_ < 1.0:
        return (base + (edited - base) * op_).astype(np.float32)
    return edited


def _native_lut3d(img: np.ndarray, p: dict) -> np.ndarray:
    f = p.get("file")
    if not f:
        return img
    lut = read_cube(f)
    return (img + (lut.apply(np.clip(img, 0, 1)) - img) * float(p.get("strength", 1.0))).astype(np.float32)


def run_entries(img_enc: np.ndarray, stack: list[dict], *, transfer: str = "srgb", seed: int = 0, frame: int = 0,
                reseed: tuple[str, ...] = (), only_color: bool = True, notes: list[str] | None = None,
                reseed_grain: bool = False) -> np.ndarray:
    """Run ``stack`` on video-encoded RGB (float, 0..1, any leading shape ``(...,3)``) and return video-encoded RGB.

    Order follows the registry stages: linear ops (<= stage 16; tone_map only when explicitly set to a real
    curve, because video is already display-referred), then the Rec.2020 -> Rec.709 output transform, then the
    display-domain ops.  ``only_color`` skips anything that is not a per-pixel colour op (LUT baking).
    """
    vstack.ensure_ops()
    shape = img_enc.shape
    x = img_enc.reshape(-1, 3)[None, :, :] if img_enc.ndim != 3 else img_enc
    notes = notes if notes is not None else []
    ctx = Ctx(domain="linear", kind="jpeg", seed=seed, output_space="srgb")
    ctx.source = None

    entries: list[tuple[int, int, dict, Any, dict]] = []
    for i, e in enumerate(stack):
        if not e.get("enabled", True):
            continue
        name = e.get("op")
        spec = vstack.spec_for(name)
        if spec is None:
            if name == "lut3d":
                entries.append((19, i, e, None, dict(e.get("params") or {})))
            else:
                notes.append(f"unknown op '{name}' skipped")
            continue
        if spec.raw_only or name == "raw":
            continue
        if spec.changes_geometry or name in vstack.GEOMETRY_NAMES:
            notes.append(f"geometry op '{name}' skipped (use ffmpeg crop/scale)")
            continue
        try:
            params = spec.resolve_params(e.get("params"), e.get("v"))
        except ValueError as exc:
            raise VideoError(str(exc)) from exc
        if only_color:
            kind, why = vstack.classify_entry(e)
            if kind != "color":
                if kind not in ("spatial",):
                    notes.append(f"'{name}' not baked into the LUT: {why}")
                continue
        entries.append((spec.stage, i, e, spec, params))
    entries.sort(key=lambda t: (t[0], t[1]))

    lin709 = decode_transfer(x, transfer)
    cur = color.apply_matrix(lin709, color.SRGB_TO_REC2020)
    in_display = False

    def to_display(lin2020: np.ndarray) -> np.ndarray:
        return np.clip(output_transform(lin2020, ctx), 0.0, 1.0)

    for stage, _i, e, spec, params in entries:
        if spec is None:  # native lut3d entry (display domain)
            dom = "display"
        else:
            dom = vstack.entry_domain(spec)
            if spec.name == "tone_map" and params.get("curve", "auto") in ("auto", "none"):
                continue  # video is display-referred: never a second tone curve / shoulder
        if dom == "linear" and in_display:
            notes.append(f"linear op '{e.get('op')}' after the display stage skipped")
            continue
        if dom == "display" and not in_display:
            cur = to_display(cur)
            in_display = True
            ctx.domain = "display"
        ctx.seed = seed + (frame if (reseed_grain and e.get("op") in reseed) else 0)
        base = cur
        if spec is None:
            edited = _native_lut3d(cur.copy(), params)
        else:
            edited = np.asarray(spec.fn(cur.copy(), params, ctx), dtype=np.float32)
        cur = _blend(base, edited, e, ctx, dom)
        if in_display:
            cur = np.clip(cur, 0.0, 1.0)
    if not in_display:
        cur = to_display(cur)
    # display (sRGB-encoded) -> requested video transfer
    if transfer != "srgb":
        cur = encode_transfer(color.srgb_eotf(cur), transfer)
    return np.clip(cur, 0.0, 1.0).reshape(shape).astype(np.float32)


# ----------------------------------------------------------------------------- baking
def _lattice(n: int) -> np.ndarray:
    """(N*N, N, 3) image whose pixels enumerate the lattice; element [b*N+g, r] = (r,g,b)/(N-1)."""
    g = np.linspace(0.0, 1.0, n, dtype=np.float32)
    bb, gg, rr = np.meshgrid(g, g, g, indexing="ij")
    return np.stack([rr, gg, bb], -1).reshape(n * n, n, 3)


def bake_lut(stack: list[dict], size: int = 33, *, transfer: str = "srgb", backend: str = "auto",
             intensity: float = 1.0, title: str = "photostudio", seed: int = 0) -> LUT3D:
    """Bake the colour-only part of ``stack`` into a 3D LUT.

    backend: ``auto``/``own`` (default: run the lattice through the registry ops here, exact identity for an
    empty stack) or ``engine`` (``photostudio.pipeline.engine.bake_lut`` with ``shoulder=False`` — sRGB only; white stays 1.0 like the own backend).
    """
    if size not in (17, 33, 65) and not 2 <= size <= 129:
        raise VideoError(f"LUT size {size} not supported (use 33 or 65)")
    if transfer not in TRANSFERS:
        raise VideoError(f"unknown transfer '{transfer}'")
    notes: list[str] = []
    skipped: list[str] = []
    part = vstack.partition_stack(stack)
    used = "own"
    table = None
    if backend == "engine":
        if transfer != "srgb":
            raise VideoError("engine backend only supports transfer=srgb")
        try:
            eng = importlib.import_module("photostudio.pipeline.engine")
            arr = np.asarray(eng.bake_lut(part["color"], size, "jpeg", None, skipped, shoulder=False), np.float32)
            table = np.ascontiguousarray(arr.transpose(2, 1, 0, 3))     # [b][g][r] -> [r][g][b]
            used = "engine"
        except Exception as exc:
            notes.append(f"engine.bake_lut unavailable ({exc}); used the built-in baker")
    if table is None:
        lat = _lattice(size)
        out = run_entries(lat, part["color"], transfer=transfer, seed=seed, notes=notes, only_color=True)
        table = np.ascontiguousarray(out.reshape(size, size, size, 3).transpose(2, 1, 0, 3))  # [b][g][r]->[r][g][b]
    lut = LUT3D(table.astype(np.float32), title, {
        "backend": used, "transfer": transfer, "size": size,
        "baked": [e["op"] for e in part["color"]],
        "spatial": [e["op"] for e in part["spatial"]],
        "unsupported": part["unsupported"] + part["geometry"], "notes": notes + [f"skipped {s}" for s in skipped],
    })
    if intensity != 1.0:
        lut = lut.blend_identity(intensity)
    return lut


def lut_signature(stack: list[dict], size: int, transfer: str, intensity: float) -> str:
    from ..util import params_hash

    return params_hash({"s": stack, "n": size, "t": transfer, "i": intensity})
