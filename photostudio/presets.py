"""Preset loader: YAML presets for modes, art looks, export, video and Picture Style emulations.

Layout (searched over ``util.preset_dirs()``, later directories override earlier ones by name)::

    <presets>/modes/<name>.yaml      kind: mode
    <presets>/art/<name>.yaml        kind: art
    <presets>/export/<name>.yaml     kind: export
    <presets>/video/<name>.yaml      kind: video
    <presets>/picture_styles/<name>.yaml   kind: picture_style

A preset is ``{name, kind, title, description, tags, stack: [entries], variants: {v: {stack: [...]}}, options}``.
Variants are addressed as ``name:variant`` (``night:astro``); loading a variant returns the preset with the
variant stack merged on top of the base stack.

Stack merge rule (also used by ``engine.resolve_stack``): the merge key of an entry is its op name plus the
optional ``id``. An entry with a ``mask`` and no ``id`` is always appended; any other entry updates the first
existing unmasked entry with the same key (params shallow-merged, other fields replaced) or is appended.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from .util import preset_dirs

KIND_DIRS = {
    "mode": "modes", "art": "art", "export": "export", "video": "video", "picture_style": "picture_styles",
}
_ALIASES = {"modes": "mode", "look": "art", "looks": "art", "arts": "art", "exports": "export",
            "videos": "video", "picture_styles": "picture_style", "picture-style": "picture_style",
            "picture-styles": "picture_style", "styles": "picture_style", "style": "picture_style"}
ENTRY_KEYS = {"op", "params", "enabled", "mask", "blend", "opacity", "v", "id", "label"}

try:  # C loader is ~10x faster; fall back to the pure-python one
    _Loader = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader


class PresetError(ValueError):
    pass


def norm_kind(kind: str) -> str:
    k = (kind or "").strip().lower().replace(" ", "_")
    k = _ALIASES.get(k, k)
    if k not in KIND_DIRS:
        raise PresetError(f"unknown preset kind '{kind}'. Known: {', '.join(KIND_DIRS)}")
    return k


# ------------------------------------------------------------------ stack helpers
def normalise_entry(e: dict) -> dict:
    """Fill defaults of a stack entry (copy)."""
    if not isinstance(e, dict) or "op" not in e:
        raise PresetError(f"stack entry needs an 'op' key: {e!r}")
    out = copy.deepcopy(e)
    out.setdefault("params", {})
    out.setdefault("enabled", True)
    out.setdefault("mask", None)
    out.setdefault("blend", "normal")
    out.setdefault("opacity", 1.0)
    out.setdefault("v", 1)
    if out["params"] is None:
        out["params"] = {}
    return out


def entry_key(e: dict) -> tuple[str, Any]:
    return (e["op"], e.get("id"))


def merge_stack(base: list[dict], layer: list[dict] | None) -> list[dict]:
    """Merge ``layer`` on top of ``base`` following the contract rules; returns a new list."""
    out = [copy.deepcopy(e) for e in base]
    for raw in layer or []:
        e = copy.deepcopy(raw)
        if "op" not in e:
            raise PresetError(f"stack entry needs an 'op' key: {raw!r}")
        has_mask = bool(e.get("mask"))
        if has_mask and e.get("id") is None:
            out.append(normalise_entry(e))
            continue
        key = entry_key(e)
        for existing in out:
            if entry_key(existing) == key and not (existing.get("mask") and existing.get("id") is None):
                params = dict(existing.get("params") or {})
                params.update(e.get("params") or {})
                existing["params"] = params
                for k, v in e.items():
                    if k not in ("params", "op"):
                        existing[k] = copy.deepcopy(v)
                break
        else:
            out.append(normalise_entry(e))
    return out


# ------------------------------------------------------------------ discovery
def _kind_paths(kind: str) -> dict[str, Path]:
    """name -> file path, later preset dirs overriding earlier ones."""
    sub = KIND_DIRS[kind]
    found: dict[str, Path] = {}
    for root in preset_dirs():
        d = root / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")):
            found[f.stem] = f
    return found


_FILE_CACHE: dict[str, tuple[float, dict]] = {}


def _read(path: Path) -> dict:
    key = str(path)
    mtime = path.stat().st_mtime
    hit = _FILE_CACHE.get(key)
    if hit and hit[0] == mtime:
        return copy.deepcopy(hit[1])
    with open(path, encoding="utf-8") as f:
        data = yaml.load(f, Loader=_Loader) or {}
    if not isinstance(data, dict):
        raise PresetError(f"{path}: preset must be a mapping")
    _FILE_CACHE[key] = (mtime, data)
    return copy.deepcopy(data)


def _finish(data: dict, kind: str, path: Path) -> dict:
    data.setdefault("name", path.stem)
    data.setdefault("kind", kind)
    data.setdefault("title", str(data["name"]).replace("_", " ").title())
    data.setdefault("description", "")
    data.setdefault("tags", [])
    data.setdefault("stack", [])
    data.setdefault("options", {})
    data["stack"] = [normalise_entry(e) for e in (data.get("stack") or [])]
    variants = data.get("variants") or {}
    for vn, v in list(variants.items()):
        v = dict(v or {})
        v["stack"] = [normalise_entry(e) for e in (v.get("stack") or [])]
        variants[vn] = v
    data["variants"] = variants
    data["path"] = str(path)
    return data


def list_presets(kind: str) -> list[dict]:
    """Summaries of every preset of a kind: name, title, description, tags, variants, path."""
    kind = norm_kind(kind)
    out = []
    for name, path in sorted(_kind_paths(kind).items()):
        try:
            d = _finish(_read(path), kind, path)
        except Exception as exc:  # noqa: BLE001 - one broken YAML must not hide the others
            out.append({"name": name, "kind": kind, "title": name, "description": "", "tags": [],
                        "variants": [], "path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        out.append({"name": d["name"], "kind": d["kind"], "title": d["title"], "description": d["description"],
                    "tags": d["tags"], "variants": sorted(d["variants"]), "path": d["path"],
                    "n_ops": len(d["stack"])})
    return out


def split_variant(name: str) -> tuple[str, str | None]:
    for sep in (":", "/"):
        if sep in name:
            base, var = name.split(sep, 1)
            return base, var or None
    return name, None


def load_preset(kind: str, name: str, variant: str | None = None) -> dict:
    """Load a preset by name (``name`` or ``name:variant``). With a variant, ``stack`` is base+variant merged."""
    kind = norm_kind(kind)
    base, var = split_variant(name)
    variant = variant or var
    paths = _kind_paths(kind)
    if base not in paths:
        raise PresetError(f"no {kind} preset named '{base}'. Available: {', '.join(sorted(paths)) or '(none)'}")
    d = _finish(_read(paths[base]), kind, paths[base])
    if variant:
        if variant not in d["variants"]:
            raise PresetError(f"preset '{base}' has no variant '{variant}'. "
                              f"Variants: {', '.join(sorted(d['variants'])) or '(none)'}")
        d["stack"] = merge_stack(d["stack"], d["variants"][variant]["stack"])
        vopts = d["variants"][variant].get("options") or {}
        d["options"] = {**d["options"], **vopts}
        d["variant"] = variant
        d["title"] = f"{d['title']} ({variant})"
    return d


def preset_exists(kind: str, name: str) -> bool:
    try:
        load_preset(kind, name)
        return True
    except PresetError:
        return False


# ------------------------------------------------------------------ validation
def validate_preset(data: dict, check_ops: bool = True) -> list[str]:
    """Return a list of problems (empty = valid). ``check_ops`` verifies op names against the registry."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["preset must be a mapping"]
    for k in ("name", "kind"):
        if not data.get(k):
            problems.append(f"missing required key '{k}'")
    kind = data.get("kind")
    if kind and kind not in KIND_DIRS:
        problems.append(f"unknown kind '{kind}'")
    known = None
    if check_ops:
        try:
            from .pipeline.registry import all_ops
            known_ops = all_ops()
            known = set(known_ops)
        except Exception:  # noqa: BLE001
            known = None
            known_ops = {}
    else:
        known_ops = {}
    try:
        from .masks import MASK_TYPES as _mask_types
        mask_types = set(_mask_types)
    except Exception:  # noqa: BLE001
        mask_types = None

    def check_stack(stack, where):
        if not isinstance(stack, list):
            problems.append(f"{where}: stack must be a list")
            return
        for i, e in enumerate(stack):
            w = f"{where}[{i}]"
            if not isinstance(e, dict) or "op" not in e:
                problems.append(f"{w}: entry needs 'op'")
                continue
            extra = set(e) - ENTRY_KEYS
            if extra:
                problems.append(f"{w} ({e['op']}): unknown keys {sorted(extra)}")
            if not isinstance(e.get("params", {}) or {}, dict):
                problems.append(f"{w} ({e['op']}): params must be a mapping")
            if known is not None and e["op"] not in known:
                problems.append(f"{w}: unknown op '{e['op']}'")
            spec = known_ops.get(e["op"])
            if spec is not None and spec.defaults and isinstance(e.get("params"), dict):
                bad = sorted(set(e["params"]) - set(spec.defaults))
                if bad:
                    problems.append(f"{w} ({e['op']}): unknown params {bad}")
                for k, allowed in (getattr(spec, "choices", None) or {}).items():
                    val = e["params"].get(k)
                    if k in e["params"] and allowed and val not in allowed:
                        problems.append(f"{w} ({e['op']}): param {k}={val!r} not in {list(allowed)}")
            m = e.get("mask")
            if m is not None and (not isinstance(m, dict) or "type" not in m):
                problems.append(f"{w} ({e['op']}): mask needs a 'type'")
            elif m is not None and mask_types is not None and m["type"] not in mask_types:
                problems.append(f"{w} ({e['op']}): unknown mask type '{m['type']}'")
            op_ = e.get("opacity", 1.0)
            if not isinstance(op_, (int, float)) or not 0.0 <= float(op_) <= 1.0:
                problems.append(f"{w} ({e['op']}): opacity must be 0..1")

    if kind != "export":
        check_stack(data.get("stack", []), "stack")
    for vn, v in (data.get("variants") or {}).items():
        if not isinstance(v, dict):
            problems.append(f"variants.{vn}: must be a mapping")
            continue
        check_stack(v.get("stack", []), f"variants.{vn}.stack")
    if kind == "export":
        opts = data.get("options")
        if not isinstance(opts, dict) or "format" not in opts:
            problems.append("export preset needs options.format")
    return problems


# ------------------------------------------------------------------ user presets
def user_preset_dir(kind: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "photostudio" / "presets"
    return base / KIND_DIRS[norm_kind(kind)]


def save_user_preset(kind: str, name: str, data: dict, overwrite: bool = True) -> Path:
    """Write a preset into the user preset directory (highest priority after the package presets)."""
    kind = norm_kind(kind)
    data = copy.deepcopy(data)
    data["name"] = name
    data["kind"] = kind
    data.pop("path", None)
    problems = [p for p in validate_preset(data, check_ops=False) if "unknown op" not in p]
    if problems:
        raise PresetError("; ".join(problems))
    d = user_preset_dir(kind)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.yaml"
    if path.exists() and not overwrite:
        raise PresetError(f"{path} exists")
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=None)
    os.replace(tmp, path)
    _FILE_CACHE.pop(str(path), None)
    return path


def preset_from_stack(kind: str, name: str, stack: list[dict], title: str = "", description: str = "",
                      tags: list[str] | None = None) -> dict:
    return {"name": name, "kind": norm_kind(kind), "title": title or name.replace("_", " ").title(),
            "description": description, "tags": tags or [], "stack": [normalise_entry(e) for e in stack]}
