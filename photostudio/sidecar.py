"""JSON sidecars: ``<image filename>.ps.json`` next to the image (never inside a CR3).

Format (CONTRACT)::

    {schema: 1, source: "IMG_0001.CR3", active: "default",
     versions: {"default": {mode, art, stack: [entries], created, modified}},
     snapshots: [{name, time, version, stack}], rating: 0-5, flag: pick|reject|null, label, tags, notes}

Versions are virtual copies (darktable ``_01`` duplicates); snapshots are the timestamped history. Writes are
atomic (temp file + fsync + ``os.replace``). Unknown top-level keys written by other tools are preserved.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .presets import normalise_entry
from .util import data_home, dumps, params_hash

SCHEMA = 1
SUFFIX = ".ps.json"
MAX_AUTO_SNAPSHOTS = 50


class SidecarError(ValueError):
    pass


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


def sidecar_path(img: str | Path) -> Path:
    p = Path(img)
    if p.name.endswith(SUFFIX):
        return p
    return p.with_name(p.name + SUFFIX)


def new_version_doc(mode: str | None = None, art: str | None = None, stack: list | None = None) -> dict:
    t = now()
    return {"mode": mode, "art": art, "stack": copy.deepcopy(stack or []), "created": t, "modified": t}


def default_doc(img: str | Path) -> dict:
    return {"schema": SCHEMA, "source": Path(img).name, "active": "default",
            "versions": {"default": new_version_doc()}, "snapshots": [], "rating": 0, "flag": None,
            "label": None, "tags": [], "notes": ""}


def _fix(doc: dict, img: str | Path) -> dict:
    """Forward-compatible defaults for documents written by older/foreign tools."""
    base = default_doc(img)
    for k, v in base.items():
        doc.setdefault(k, v)
    if not doc["versions"]:
        doc["versions"] = {"default": new_version_doc()}
    if doc["active"] not in doc["versions"]:
        doc["active"] = next(iter(doc["versions"]))
    for v in doc["versions"].values():
        v.setdefault("mode", None)
        v.setdefault("art", None)
        v.setdefault("stack", [])
        v.setdefault("created", now())
        v.setdefault("modified", v["created"])
    return doc


def exists(img: str | Path) -> bool:
    return sidecar_path(img).exists()


def load(img: str | Path) -> dict:
    """Load the sidecar document; a default in-memory document if none exists yet."""
    sp = sidecar_path(img)
    if not sp.exists():
        return default_doc(sp.name[:-len(SUFFIX)] if sp.name.endswith(SUFFIX) else img)
    try:
        with open(sp, encoding="utf-8") as f:
            doc = json.load(f)
    except json.JSONDecodeError as exc:
        raise SidecarError(f"{sp}: invalid JSON ({exc}). Fix or delete the file; "
                           f"a backup may exist as {sp.name}.bak") from exc
    return _fix(doc, Path(img).name if not str(img).endswith(SUFFIX) else sp.name[:-len(SUFFIX)])


def save(img: str | Path, doc: dict) -> Path:
    """Atomically write the sidecar (keeps one ``.bak`` of the previous version)."""
    sp = sidecar_path(img)
    sp.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(doc, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=sp.name + ".", suffix=".tmp", dir=str(sp.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if sp.exists():
            try:
                os.replace(sp, sp.with_name(sp.name + ".bak"))
            except OSError:
                pass
        os.replace(tmp, sp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sp


# ------------------------------------------------------------------ versions & stacks
def _ver(doc: dict, version: str | None) -> tuple[str, dict]:
    name = version or doc["active"]
    if name not in doc["versions"]:
        raise SidecarError(f"no version '{name}' (have: {', '.join(doc['versions'])})")
    return name, doc["versions"][name]


def get_stack(doc: dict, version: str | None = None) -> list[dict]:
    """Deep copy of a version's stack (default: the active version)."""
    return copy.deepcopy(_ver(doc, version)[1]["stack"])


def set_stack(doc: dict, stack: list[dict], version: str | None = None, mode: Any = ..., art: Any = ...) -> dict:
    """Replace a version's stack (normalised). ``mode``/``art`` update the recorded preset names if given."""
    name, v = _ver(doc, version)
    v["stack"] = [normalise_entry(e) for e in stack]
    if mode is not ...:
        v["mode"] = mode
    if art is not ...:
        v["art"] = art
    v["modified"] = now()
    return doc


def version_info(doc: dict, version: str | None = None) -> dict:
    name, v = _ver(doc, version)
    return {"name": name, "active": name == doc["active"], "mode": v.get("mode"), "art": v.get("art"),
            "n_ops": len(v["stack"]), "created": v["created"], "modified": v["modified"]}


def list_versions(doc: dict) -> list[dict]:
    return [version_info(doc, n) for n in doc["versions"]]


def new_version(doc: dict, name: str, from_version: str | None = None, switch: bool = True) -> str:
    """Create a virtual copy of ``from_version`` (default: active); optionally make it active."""
    if name in doc["versions"]:
        raise SidecarError(f"version '{name}' already exists")
    _, src = _ver(doc, from_version)
    v = copy.deepcopy(src)
    v["created"] = v["modified"] = now()
    doc["versions"][name] = v
    if switch:
        doc["active"] = name
    return name


def switch_version(doc: dict, name: str) -> dict:
    _ver(doc, name)
    doc["active"] = name
    return doc


def delete_version(doc: dict, name: str) -> dict:
    _ver(doc, name)
    if len(doc["versions"]) == 1:
        raise SidecarError("cannot delete the only version")
    del doc["versions"][name]
    if doc["active"] == name:
        doc["active"] = next(iter(doc["versions"]))
    return doc


def rename_version(doc: dict, old: str, new: str) -> dict:
    _ver(doc, old)
    if new in doc["versions"]:
        raise SidecarError(f"version '{new}' already exists")
    doc["versions"] = {(new if k == old else k): v for k, v in doc["versions"].items()}
    if doc["active"] == old:
        doc["active"] = new
    return doc


# ------------------------------------------------------------------ snapshots (history)
def snapshot(doc: dict, name: str, version: str | None = None) -> dict:
    """Append a named, timestamped copy of a version's stack to the history."""
    vname, v = _ver(doc, version)
    snap = {"name": name, "time": now(), "version": vname, "stack": copy.deepcopy(v["stack"]),
            "mode": v.get("mode"), "art": v.get("art")}
    doc["snapshots"].append(snap)
    autos = [i for i, s in enumerate(doc["snapshots"]) if str(s.get("name", "")).startswith("auto:")]
    for i in reversed(autos[:-MAX_AUTO_SNAPSHOTS]):
        del doc["snapshots"][i]
    return snap


def restore_snapshot(doc: dict, name: str, version: str | None = None) -> dict:
    """Restore the newest snapshot called ``name`` into ``version`` (auto-snapshotting the current state)."""
    matches = [s for s in doc["snapshots"] if s.get("name") == name]
    if not matches:
        raise SidecarError(f"no snapshot '{name}' (have: {', '.join(s.get('name', '?') for s in doc['snapshots'])})")
    snap = matches[-1]
    vname, _ = _ver(doc, version)
    snapshot(doc, f"auto:before-restore-{name}", vname)
    set_stack(doc, snap["stack"], vname, mode=snap.get("mode"), art=snap.get("art"))
    return doc


def list_snapshots(doc: dict) -> list[dict]:
    return [{"name": s.get("name"), "time": s.get("time"), "version": s.get("version"),
             "n_ops": len(s.get("stack", []))} for s in doc["snapshots"]]


# ------------------------------------------------------------------ hashing
def ops_hash(stack_or_doc: list | dict, version: str | None = None) -> str:
    """Stable hash of the *effective* edit (enabled entries only) for thumbnail/preview cache keys."""
    stack = get_stack(stack_or_doc, version) if isinstance(stack_or_doc, dict) else stack_or_doc
    eff = [{k: e.get(k) for k in ("op", "id", "params", "mask", "blend", "opacity", "v")}
           for e in stack if e.get("enabled", True)]
    return params_hash(eff)


# ------------------------------------------------------------------ copy / paste
def _select(stack: list[dict], ops: list[str] | None) -> list[dict]:
    if not ops:
        return copy.deepcopy(stack)
    want = set(ops)
    return [copy.deepcopy(e) for e in stack if e["op"] in want or e.get("id") in want]


def clipboard_path() -> Path:
    return data_home() / "clipboard.json"


def copy_settings(src_img: str | Path, ops: list[str] | None = None, version: str | None = None) -> dict:
    """Copy (a subset of) the stack of ``src_img`` to the shared clipboard file."""
    doc = load(src_img)
    vname, v = _ver(doc, version)
    clip = {"source": Path(src_img).name, "time": now(), "version": vname, "mode": v.get("mode"),
            "art": v.get("art"), "stack": _select(v["stack"], ops)}
    tmp = clipboard_path().with_suffix(".tmp")
    tmp.write_text(dumps(clip), encoding="utf-8")
    os.replace(tmp, clipboard_path())
    return clip


def read_clipboard() -> dict:
    p = clipboard_path()
    if not p.exists():
        raise SidecarError("clipboard is empty: run `photo copy-settings <image>` first")
    return json.loads(p.read_text(encoding="utf-8"))


def paste_stack(doc: dict, stack: list[dict], version: str | None = None, mode: str = "replace",
                preset_names: tuple | None = None) -> dict:
    """Apply a copied stack to a document. mode: replace (whole stack), merge (per-op merge), append."""
    from .presets import merge_stack

    cur = get_stack(doc, version)
    snapshot(doc, "auto:before-paste", version)
    if mode == "replace":
        new = copy.deepcopy(stack)
    elif mode == "merge":
        new = merge_stack(cur, stack)
    elif mode == "append":
        new = cur + copy.deepcopy(stack)
    else:
        raise SidecarError(f"unknown paste mode '{mode}' (replace|merge|append)")
    kw = {}
    if preset_names and mode == "replace":
        kw = {"mode": preset_names[0], "art": preset_names[1]}
    return set_stack(doc, new, version, **kw)


def paste_settings(dst_img: str | Path, version: str | None = None, mode: str = "replace",
                   ops: list[str] | None = None) -> dict:
    """Paste the clipboard onto ``dst_img`` (writes the sidecar)."""
    clip = read_clipboard()
    stack = _select(clip["stack"], ops)
    doc = load(dst_img)
    paste_stack(doc, stack, version, mode, (clip.get("mode"), clip.get("art")))
    save(dst_img, doc)
    return doc


def copy_between(src_img: str | Path, dst_imgs: list[str | Path], ops: list[str] | None = None,
                 version: str | None = None, mode: str = "replace", dst_version: str | None = None) -> list[Path]:
    """Copy edits from one image to many without touching the clipboard."""
    sdoc = load(src_img)
    vname, v = _ver(sdoc, version)
    stack = _select(v["stack"], ops)
    out = []
    for d in dst_imgs:
        doc = load(d)
        paste_stack(doc, stack, dst_version, mode, (v.get("mode"), v.get("art")))
        out.append(save(d, doc))
    return out


def reset(doc: dict, version: str | None = None) -> dict:
    """Clear a version's stack (keeps an auto snapshot so it can be restored)."""
    vname, v = _ver(doc, version)
    if v["stack"]:
        snapshot(doc, "auto:before-reset", vname)
    set_stack(doc, [], vname, mode=None, art=None)
    return doc


def summary(doc: dict) -> dict:
    """Short JSON-able overview used by ``photo info``."""
    return {"active": doc["active"], "versions": list_versions(doc), "snapshots": len(doc["snapshots"]),
            "rating": doc.get("rating", 0), "flag": doc.get("flag"), "label": doc.get("label"),
            "tags": doc.get("tags", []), "n_ops_active": len(get_stack(doc))}
