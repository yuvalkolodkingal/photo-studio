"""Path confinement: every file the web API touches must resolve to a location under the library ROOT."""
from __future__ import annotations

import os
from pathlib import Path

from ..util import ALL_MEDIA, IMAGE_EXTS, RAW_EXTS

SIDECAR_SUFFIXES = (".ps.json", ".ps.json.bak", ".xmp")
HIDDEN_DIRS = {".photostudio", "exports", "__pycache__", "_rejects"}


class PathError(ValueError):
    """The requested path is outside the library root or otherwise not allowed (HTTP 400/403)."""


def clean_rel(rel: str | None) -> str:
    """Normalise a client supplied relative path; reject absolute paths, drive letters, NULs and '..'."""
    if rel is None:
        raise PathError("missing path")
    s = str(rel).replace("\\", "/")
    if "\x00" in s:
        raise PathError("invalid path")
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        raise PathError("absolute paths are not allowed")
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathError("path traversal is not allowed")
    return "/".join(parts)


def resolve_under(root: Path, rel: str | None, must_exist: bool = True) -> Path:
    """Absolute path of ``rel`` inside ``root`` (symlinks resolved, must stay inside)."""
    r = clean_rel(rel)
    root = root.resolve()
    full = (root / r).resolve()
    try:
        full.relative_to(root)
    except ValueError as exc:
        raise PathError("path escapes the library root") from exc
    if must_exist and not full.exists():
        raise FileNotFoundError(f"no such file: {r}")
    return full


def resolve_image(root: Path, rel: str | None) -> Path:
    """Like :func:`resolve_under` but the target must be an existing supported image file."""
    full = resolve_under(root, rel)
    if not full.is_file():
        raise FileNotFoundError(f"not a file: {rel}")
    if full.suffix.lower() not in (RAW_EXTS | IMAGE_EXTS):
        raise PathError(f"unsupported file type: {full.suffix}")
    if any(part in (".photostudio",) for part in full.relative_to(root.resolve()).parts):
        raise PathError("internal folder")
    return full


def to_rel(root: Path, p: str | Path) -> str:
    return Path(p).resolve().relative_to(root.resolve()).as_posix()


def safe_dir(root: Path, rel: str | None, create: bool = False) -> Path:
    """A directory under root (``''`` = root itself)."""
    full = resolve_under(root, rel or "", must_exist=not create)
    if create:
        full.mkdir(parents=True, exist_ok=True)
    if not full.is_dir():
        raise FileNotFoundError(f"not a folder: {rel}")
    return full


def is_media(name: str) -> bool:
    return Path(name).suffix.lower() in ALL_MEDIA and not name.startswith(".")


def walk_dirs(root: Path, max_depth: int = 8) -> list[dict]:
    """Folder tree (rel path, name, image count of the folder itself) for the library sidebar."""
    out: list[dict] = []
    root = root.resolve()

    def rec(d: Path, depth: int):
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            return
        n = sum(1 for e in entries if e.is_file() and is_media(e.name) and Path(e.name).suffix.lower() in (RAW_EXTS | IMAGE_EXTS))
        rel = d.relative_to(root).as_posix() if d != root else ""
        out.append({"path": rel, "name": d.name if rel else (root.name or "/"), "count": n, "depth": depth})
        if depth >= max_depth:
            return
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in HIDDEN_DIRS:
                rec(Path(e.path), depth + 1)

    rec(root, 0)
    return out
