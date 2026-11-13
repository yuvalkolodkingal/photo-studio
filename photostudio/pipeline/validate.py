"""Write-time validation of hand-typed edits (``photo edit/develop --set ... --mask ...``).

A typo stored in a sidecar would otherwise only show up at render time. These checks run *before* the sidecar is
written and fail with a one-line message that names the problem and suggests close matches (difflib):

* op names against the registry (``expsure`` -> ``exposure``),
* parameter keys against the op's defaults (``exposure.ev2`` -> ``ev``), plus the documented extra keys in
  :data:`EXTRA_PARAM_KEYS`,
* value kinds against the default (a number where the op expects a number, a list where it expects a list),
* ``choices`` (``tone_map.curve=sigmoyd``),
* mask specs (type, ``combine`` children, numeric common keys) and heal/clone/inpaint/red_eye ``spots``.

The engine independently skips an entry it cannot run (with a warning in ``ctx.log``) so an old or hand-edited sidecar
never makes an image permanently unrenderable.
"""
from __future__ import annotations

import difflib
from typing import Any

from . import registry

# Keys an op reads although they are not in its defaults (none today; add here when an op documents one).
EXTRA_PARAM_KEYS: dict[str, set[str]] = {}
ENTRY_KEYS = ("enabled", "opacity", "blend")
COMBINE_MODES = ("union", "intersect", "subtract", "exclude")
SPOT_OPS = {"heal": ("dst", True), "clone": ("dst", True), "inpaint": ("center", False), "red_eye": ("center", False)}
NUMERIC_MASK_KEYS = ("feather", "opacity", "guided_radius", "guided_eps", "blur", "contrast", "threshold")


class EditError(ValueError):
    """A rejected edit; the message is meant to be shown to the user as is."""


def _close(word: str, options, n: int = 3) -> list[str]:
    return difflib.get_close_matches(str(word), [str(o) for o in options], n=n, cutoff=0.6)


def _suggest(word: str, options, hint: str = "") -> str:
    m = _close(word, options)
    s = f" Did you mean {' or '.join(repr(x) for x in m)}?" if m else ""
    return s + (f" {hint}" if hint else "")


def op_spec(name: str):
    registry._ensure_loaded()
    spec = registry.REGISTRY.get(name)
    if spec is None:
        raise EditError(f"unknown op '{name}'." + _suggest(name, registry.REGISTRY, "(photo ops list shows all ops)"))
    return spec


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_kind(op: str, key: str, default: Any, value: Any) -> None:
    if default is None or value is None:
        return
    want = None
    if isinstance(default, bool):
        ok, want = isinstance(value, bool) or value in (0, 1), "true/false"
    elif _is_num(default):
        ok, want = _is_num(value), "a number"
    elif isinstance(default, list):
        ok, want = isinstance(value, list), "a JSON list"
    elif isinstance(default, dict):
        ok, want = isinstance(value, dict), "a JSON object"
    else:
        return
    if not ok:
        raise EditError(f"{op}.{key} expects {want}, got {value!r}")


def _point(v: Any, what: str) -> None:
    if not (isinstance(v, (list, tuple)) and len(v) == 2 and all(_is_num(x) for x in v)):
        raise EditError(f"{what} must be [x, y] with numbers, got {v!r}")


def validate_spots(op: str, spots: Any) -> None:
    """``spots`` of heal / clone (``dst``, optional ``src``) and inpaint / red_eye (``center``)."""
    if op not in SPOT_OPS:
        return
    key, _ = SPOT_OPS[op]
    if not isinstance(spots, list):
        raise EditError(f"{op}.spots must be a JSON list of objects such as [{{\"{key}\":[0.4,0.5],\"radius\":30}}]")
    for i, sp in enumerate(spots):
        if not isinstance(sp, dict):
            raise EditError(f"{op}.spots[{i}] must be an object with '{key}', got {sp!r}")
        if key not in sp:
            alt = "center" if key == "dst" else "dst"
            hint = f" ('{alt}' belongs to {'inpaint/red_eye' if key == 'dst' else 'heal/clone'})" if alt in sp else ""
            raise EditError(f"{op}.spots[{i}] needs '{key}': [x, y] (0..1 of the image){hint}")
        _point(sp[key], f"{op}.spots[{i}].{key}")
        if "src" in sp and sp["src"] is not None:
            _point(sp["src"], f"{op}.spots[{i}].src")
        for k in ("radius", "radius_px", "feather", "opacity"):
            if k in sp and not _is_num(sp[k]):
                raise EditError(f"{op}.spots[{i}].{k} must be a number, got {sp[k]!r}")


def validate_mask_spec(spec: Any, where: str = "mask") -> dict:
    """Check a mask spec (dict or bare type); returns the normalised dict. Raises :class:`EditError`."""
    from ..masks.common import MASK_TYPES, TYPE_ALIASES, normalise_spec

    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, dict):
        raise EditError(f"{where} must be a JSON object such as {{\"type\":\"ai_sky\"}} or a bare type name, got {spec!r}")
    if "type" not in spec:
        raise EditError(f"{where} needs a 'type' ({', '.join(MASK_TYPES)})")
    s = normalise_spec(spec)
    t = s["type"]
    if t not in MASK_TYPES:
        raise EditError(f"unknown mask type '{spec.get('type')}'." +
                        _suggest(str(spec.get("type")), list(MASK_TYPES) + list(TYPE_ALIASES), f"(mask types: {', '.join(MASK_TYPES)})"))
    for k in NUMERIC_MASK_KEYS:
        if k in s and s[k] is not None and not _is_num(s[k]):
            raise EditError(f"{where}.{k} must be a number, got {s[k]!r}")
    if "invert" in s and not isinstance(s["invert"], (bool, int)):
        raise EditError(f"{where}.invert must be true or false")
    if s.get("refine") is not None and not isinstance(s["refine"], dict):
        raise EditError(f"{where}.refine must be an object (guided_radius, guided_eps, blur, contrast)")
    if t == "combine":
        kids = s.get("masks")
        if not isinstance(kids, list) or not kids:
            raise EditError(f"{where} of type 'combine' needs a non-empty 'masks' list")
        if s.get("mode", "union") not in COMBINE_MODES:
            raise EditError(f"{where}.mode must be one of {', '.join(COMBINE_MODES)}, got {s.get('mode')!r}")
        s["masks"] = [validate_mask_spec(k, f"{where}.masks[{i}]") for i, k in enumerate(kids)]
    if t == "polygon":
        pts = s.get("points") or s.get("nodes")
        if pts is not None and (not isinstance(pts, list) or len(pts) < 3 or not all(
                isinstance(p, (list, tuple)) and len(p) >= 2 and _is_num(p[0]) and _is_num(p[1]) for p in pts)):
            raise EditError(f"{where}.points must be a list of at least three [x, y] points")
    return s


def validate_param(op: str, path: list[str], value: Any, eid: str | None = None) -> None:
    """Validate one ``op[@id].path=value`` assignment (``path`` is the dotted param path split into parts)."""
    spec = op_spec(op)
    head = path[0]
    if len(path) == 1 and head in ENTRY_KEYS:
        if head == "opacity" and not _is_num(value):
            raise EditError(f"{op}.opacity must be a number 0..1, got {value!r}")
        if head == "enabled" and not isinstance(value, (bool, int)):
            raise EditError(f"{op}.enabled must be true or false, got {value!r}")
        if head == "blend":
            from ..masks import BLEND_MODES

            if str(value) not in BLEND_MODES and str(value).lower() not in BLEND_MODES:
                raise EditError(f"{op}.blend={value!r} is not a blend mode." +
                                _suggest(str(value), BLEND_MODES, f"(modes: {', '.join(BLEND_MODES)})"))
        return
    allowed = set(spec.defaults) | EXTRA_PARAM_KEYS.get(op, set())
    if head not in allowed:
        shown = ", ".join(sorted(allowed)[:16]) + (" ..." if len(allowed) > 16 else "")
        raise EditError(f"op '{op}' has no param '{head}'." + _suggest(head, list(allowed) + list(ENTRY_KEYS),
                                                                       f"Params: {shown or '(none)'}; try: photo ops describe {op}"))
    if len(path) == 1:
        _check_kind(op, head, spec.defaults.get(head), value)
        allowed_choices = spec.choices.get(head)
        if allowed_choices and value not in allowed_choices:
            raise EditError(f"{op}.{head}={value!r} is not valid." +
                            _suggest(str(value), allowed_choices, f"Choices: {', '.join(map(str, allowed_choices))}"))
        if head == "spots":
            validate_spots(op, value)
        if head in ("region", "mask") and value is not None and op in ("inpaint",):
            validate_mask_spec(value, f"{op}.{head}")


def validate_assignments(assignments: list[str], mask: Any = None) -> Any:
    """Validate every ``--set`` assignment (and the ``--mask`` spec, returned normalised). Raises EditError."""
    from .engine import EngineError, parse_assignment

    if mask is not None:
        mask = validate_mask_spec(mask, "--mask")
    for a in assignments:
        try:
            op, eid, path, value = parse_assignment(a)
        except EngineError as exc:
            raise EditError(str(exc)) from exc
        validate_param(op, path, value, eid)
    return mask


def validate_entry(entry: dict, strict_keys: bool = True) -> list[str]:
    """Problems with one stack entry (used to check whole stacks, e.g. web/API payloads); [] when fine."""
    probs: list[str] = []
    try:
        spec = op_spec(entry.get("op"))
    except EditError as exc:
        return [str(exc)]
    for k, v in (entry.get("params") or {}).items():
        try:
            validate_param(spec.name, [k], v)
        except EditError as exc:
            if strict_keys or "no param" not in str(exc):
                probs.append(str(exc))
    if entry.get("mask"):
        try:
            validate_mask_spec(entry["mask"], f"{spec.name} mask")
        except EditError as exc:
            probs.append(str(exc))
    return probs


def check_selectors(kind: str, names: list[str], stack: list[dict]) -> list[str]:
    """``--remove/--enable/--disable OP[@ID]``: an op name that does not exist (typo) is an error with suggestions;
    a valid op that simply is not in this stack is returned (the caller reports it) because removing it is a no-op."""
    absent: list[str] = []
    have = [e["op"] + (f"@{e['id']}" if e.get("id") else "") for e in stack]
    for name in names:
        op, _, eid = name.partition("@")
        op_spec(op)                                     # unknown op -> EditError with suggestions
        if not any(e["op"] == op and (not eid or e.get("id") == eid) for e in stack):
            if eid and any(e["op"] == op for e in stack):
                raise EditError(f"--{kind} '{name}': op '{op}' is in the stack but no entry has id '{eid}'." +
                                _suggest(name, have, f"Entries: {', '.join(have)}"))
            absent.append(name)
    return absent
