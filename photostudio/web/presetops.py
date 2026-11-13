"""Applying / listing presets for the web UI (thin layer over photostudio.presets and engine.resolve_stack_ex)."""
from __future__ import annotations

import copy
from pathlib import Path

from .. import presets as presets_mod
from ..pipeline import engine
from ..presets import PresetError, merge_stack, normalise_entry

KINDS = ("mode", "art", "picture_style", "export")
GEOMETRY_OPS = {"crop", "rotate", "straighten", "auto_level", "perspective", "flip", "resize"}
RETOUCH_OPS = {"heal", "clone", "inpaint", "liquify", "red_eye", "dodge_burn", "blemish_auto"}


def is_user_preset(kind: str, path: str | None) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(presets_mod.user_preset_dir(kind).resolve())
    except Exception:  # noqa: BLE001
        return False


def list_kind(kind: str) -> list[dict]:
    kind = presets_mod.norm_kind(kind)
    out = []
    for p in presets_mod.list_presets(kind):
        p = dict(p)
        p["user"] = is_user_preset(kind, p.get("path"))
        p.pop("path", None)
        out.append(p)
    return out


def list_user() -> list[dict]:
    out = []
    for k in ("mode", "art", "picture_style"):
        out += [p for p in list_kind(k) if p["user"]]
    return out


def apply(kind: str, name: str, current: list[dict] | None, source_kind: str, meta: dict | None = None,
          variant: str | None = None, replace: bool = False, image=None) -> tuple[list[dict], dict]:
    """New stack after applying a preset on top of ``current`` (or replacing everything with defaults + preset).
    Returns ``(stack, {mode, art, info})``; the caller stores mode/art in the sidecar version."""
    kind = presets_mod.norm_kind(kind)
    cur = [normalise_entry(e) for e in (current or [])]
    base = None if (replace or not cur) else cur
    full = name if not variant else f"{name}:{variant}"
    if kind == "mode":
        stack, info = engine.resolve_stack_ex(mode=full, art=None, user=None, kind=source_kind, meta=meta, image=image, base=base)
        return stack, {"mode": info.get("mode") or full, "art": ..., "info": info}
    if kind in ("art", "picture_style"):
        pre = presets_mod.load_preset(kind, name, variant)
        if base is None:
            stack, _ = engine.resolve_stack_ex(None, None, pre["stack"], source_kind, meta)
        else:
            stack = merge_stack(base, pre["stack"])
        if source_kind != "raw":
            from ..pipeline import registry
            reg = registry.all_ops()
            stack = [e for e in stack if not (e["op"] in reg and reg[e["op"]].raw_only)]
        return stack, {"mode": ..., "art": name if kind == "art" else f"style:{name}", "info": {"title": pre.get("title")}}
    raise PresetError(f"cannot apply preset kind '{kind}' to an image")


def remove(kind: str, name: str, current: list[dict], variant: str | None = None) -> list[dict]:
    """Drop the unmasked entries of ``current`` whose ops are named by the preset (undo of 'apply')."""
    pre = presets_mod.load_preset(kind, name, variant)
    ops = {e["op"] for e in pre["stack"]} - {"raw", "exposure", "white_balance", "tone_map"}
    return [e for e in current if not (e["op"] in ops and not e.get("mask"))]


def stack_for_user_preset(stack: list[dict], scope: str = "look") -> list[dict]:
    """Which entries a 'save current as preset' keeps. scope: look (no geometry / retouch / raw decoder entries),
    all (everything but retouch spots), tone_color (only colour/tone categories)."""
    from ..pipeline import registry

    reg = registry.all_ops()
    out = []
    for e in copy.deepcopy(stack):
        op = e["op"]
        if op == "raw":
            continue
        if scope == "look" and (op in GEOMETRY_OPS or op in RETOUCH_OPS or (e.get("mask") is not None and False)):
            continue
        if scope == "all" and op in RETOUCH_OPS:
            continue
        if scope == "tone_color" and (op not in reg or reg[op].category not in ("tone", "color", "core", "look", "film")):
            continue
        out.append(e)
    return out
