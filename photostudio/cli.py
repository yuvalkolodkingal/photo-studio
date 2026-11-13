"""``photo`` command line: argparse front end with auto-discovered command plugins.

Every module in :mod:`photostudio.commands` may define ``register(subparsers)``; it adds its sub-commands with
``subparsers.add_parser(...)`` and ``parser.set_defaults(func=fn)``. ``fn(args)`` returns a JSON-able object,
printed as pretty text by default or as JSON with the global ``--json`` flag. An exception becomes exit code 1
with ``{"error": ...}``. A broken plugin module is skipped with a warning (stderr), never fatal.

Global flags (accepted before or after the sub-command): ``--json``, ``--quiet``, ``--root DIR`` (library root),
``--debug`` (tracebacks).
"""
from __future__ import annotations

import argparse
import glob
import importlib
import json
import os
import pkgutil
import re
import sys
import traceback
import warnings
from pathlib import Path
from typing import Any

from .util import ALL_MEDIA, MissingDependency, dumps

# colour-science nags about the optional matplotlib on first import; the CLI never plots
warnings.filterwarnings("ignore", message=".*Matplotlib.*related API features.*")


# =============================================================================== helpers shared by commands
def expand_paths(items: list[str] | None, recursive: bool = False, exts: set[str] | None = None,
                 root: str | None = None) -> list[Path]:
    """Expand files, globs and directories into a sorted list of media files (sidecars/xmp excluded).
    A relative item is also tried under ``root``."""
    exts = exts or ALL_MEDIA
    out: list[Path] = []
    for it in items or []:
        cands = [it] + ([os.path.join(root, it)] if root and not os.path.isabs(it) else [])
        found = False
        for c in cands:
            p = Path(c).expanduser()
            if p.is_dir():
                it_ = p.rglob("*") if recursive else p.glob("*")
                out += [f for f in it_ if f.is_file() and f.suffix.lower() in exts]
                found = True
            elif p.exists():
                out.append(p)
                found = True
            else:
                hits = sorted(glob.glob(str(p), recursive=True))
                if hits:
                    out += [Path(h) for h in hits if Path(h).is_file()]
                    found = True
            if found:
                break
        if not found:
            raise FileNotFoundError(f"no such file or pattern: {it}")
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return sorted(uniq, key=lambda p: (str(p.parent), p.name))


def split_top_level(s: str, sep: str = ",") -> list[str]:
    """Split on ``sep`` outside brackets/braces/quotes (so JSON values with commas survive)."""
    parts, depth, cur, quote = [], 0, [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "[{(":
            depth += 1
            cur.append(ch)
        elif ch in "]})":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def flatten_sets(sets: list[str] | None) -> list[str]:
    """``--set a.b=1,c.d=2 --set e.f=3`` -> ['a.b=1', 'c.d=2', 'e.f=3']."""
    out: list[str] = []
    for s in sets or []:
        out += split_top_level(s)
    return out


def load_json_arg(text: str | None) -> Any:
    """JSON from a literal string or ``@file.json``."""
    if text is None:
        return None
    if text.startswith("@"):
        return json.loads(Path(text[1:]).read_text(encoding="utf-8"))
    return json.loads(text)


# =============================================================================== selectors and mask arguments
def is_selector(item: str, root: str | None = None) -> bool:
    """``@all``, ``@last-import``, ``@album:X``, ``@tag:X``, ``@session:DATE``, ``id:N`` (or a bare number that is not a file)."""
    s = str(item)
    if s.startswith("@") or s.lower().startswith("id:"):
        return True
    if s.isdigit():
        return not (Path(s).exists() or (root is not None and (Path(root) / s).exists()))
    return False


def resolve_images(items: list[str] | None, args: argparse.Namespace | None = None, *, query: str | None = None,
                   exts: set[str] | None = None, recursive: bool = False, label: str = "input files") -> list[Path]:
    """The one resolver behind develop / edit / render / export / video / versions ... as well as the library commands.

    Each item is a file, directory or glob (taken from disk, no catalog needed) or a library selector (``id:N``,
    ``@all``, ``@album:X``, ``@tag:X``, ``@last-import``, ``@session:DATE``; resolved with the library's own
    ``select_ids`` against the catalog at ``--root``). ``query`` is the library filter DSL (``--query "rating>=4"``); with
    explicit items it narrows them, without it it selects. Result: absolute-or-as-given paths, de-duplicated, in order."""
    items = [str(i) for i in (items or [])]
    root = getattr(args, "root", None) if args is not None else None
    query = query if query is not None else (getattr(args, "query", None) if args is not None else None)
    if not items and not query:
        raise FileNotFoundError(f"no {label}: give files, a selector (id:N, @album:NAME, @last-import ...) or --query")
    selectors = [i for i in items if is_selector(i, root)]
    plain = [i for i in items if i not in selectors]
    out: list[Path] = []
    if plain and not query:
        out += expand_paths(plain, recursive=recursive, exts=exts, root=root)
    if selectors or query:
        from .commands import library_cmd as lib

        with lib.open_catalog(args if args is not None else argparse.Namespace(root=root)) as cat:
            ids = lib.select_ids(cat, selectors + (plain if query else []), query, default_all=False)
            for i in ids:
                row = cat.conn.execute("SELECT primary_path FROM images WHERE id=?", (i,)).fetchone()
                if row is None:
                    continue
                p = cat.root / row["primary_path"]
                if p.exists() and (exts is None or p.suffix.lower() in exts):
                    out.append(p)
        if not out and not plain:
            raise FileNotFoundError(f"the selection matched no images ({', '.join(items + ([f'--query {query!r}'] if query else []))})")
    seen: set[Path] = set()
    uniq = [p for p in out if not (p in seen or seen.add(p))]
    if not uniq:
        raise FileNotFoundError(f"no {label}")
    return uniq


def resolve_one(item: str, args: argparse.Namespace | None = None, label: str = "image") -> Path:
    """A single file or a selector that matches exactly one image (video commands, ``versions``, ``--from``)."""
    root = getattr(args, "root", None) if args is not None else None
    if not is_selector(item, root):
        return Path(item).expanduser()
    found = resolve_images([item], args, label=label)
    if len(found) != 1:
        raise ValueError(f"'{item}' matches {len(found)} images; {label} commands need exactly one")
    return found[0]


def add_selection_args(p: argparse.ArgumentParser, *, files_help: str = "files, globs, folders or selectors "
                       "(id:N, @all, @album:NAME, @tag:NAME, @last-import, @session:DATE)") -> None:
    """The positional ``files`` plus ``--query`` used by every command that acts on images."""
    p.add_argument("files", nargs="*", help=files_help)
    p.add_argument("--query", help="library filter, e.g. \"rating>=4 AND tag:people\" (photo ls --help lists the fields)")


def load_mask_arg(text: str | None) -> dict | None:
    """``--mask`` / ``--spec``: a JSON object, ``@file.json``, or a bare shorthand (``sky``, ``subject``, ``background``,
    ``face``, ``skin``, ``eyes``, ``lips``, ``teeth``, ``animal`` or any other type name). Returns the validated,
    normalised spec (``sky`` -> ``{"type": "ai_sky"}``)."""
    if text is None:
        return None
    from .pipeline.validate import EditError, validate_mask_spec

    t = text.strip()
    try:
        if t.startswith("@"):
            path = Path(t[1:]).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"mask file not found: {path}")
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        elif t[:1] in ('{', '[', '"'):
            data = json.loads(t)
        else:
            data = t
    except json.JSONDecodeError as exc:
        raise ValueError(f"mask is not valid JSON ({exc.msg} at column {exc.colno}); quote it in single quotes, e.g. "
                         "--mask '{\"type\":\"ai_sky\"}', or use a shorthand such as sky") from None
    try:
        return validate_mask_spec(data, "--mask")
    except EditError as exc:
        raise ValueError(str(exc)) from None


# =============================================================================== text rendering
def _fmt_scalar(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    if v is None:
        return "-"
    return str(v)


def format_text(obj: Any, indent: int = 0) -> str:
    """Compact human-readable rendering of a JSON-able result."""
    pad = "  " * indent
    if isinstance(obj, dict):
        if "_text" in obj and isinstance(obj["_text"], str):
            return obj["_text"]
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(format_text(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_fmt_scalar(v) if not isinstance(v, (dict, list)) else ('{}' if isinstance(v, dict) else '[]')}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj) and all(len(x) <= 8 for x in obj):
            keys = []
            for x in obj:
                for k in x:
                    if k not in keys:
                        keys.append(k)
            if all(not isinstance(v, (dict, list)) for x in obj for v in x.values()):
                rows = [[_fmt_scalar(x.get(k)) for k in keys] for x in obj]
                widths = [max(len(k), *(len(r[i]) for r in rows)) for i, k in enumerate(keys)]
                head = pad + "  ".join(k.ljust(widths[i]) for i, k in enumerate(keys))
                body = [pad + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in rows]
                return "\n".join([head] + body)
        lines = []
        for x in obj:
            if isinstance(x, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(format_text(x, indent + 1))
            else:
                lines.append(f"{pad}- {_fmt_scalar(x)}")
        return "\n".join(lines)
    return pad + _fmt_scalar(obj)


def _strip_text(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_text(v) for k, v in obj.items() if k != "_text"}
    if isinstance(obj, list):
        return [_strip_text(x) for x in obj]
    return obj


# =============================================================================== parser
class _SubparserProxy:
    """Gives plugin modules a ``subparsers`` whose parsers also accept the global flags."""

    def __init__(self, sub, common):
        self._sub, self._common = sub, common

    def add_parser(self, name, **kw):
        kw["parents"] = list(kw.get("parents", [])) + [self._common]
        kw.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        return self._sub.add_parser(name, **kw)

    def __getattr__(self, item):
        return getattr(self._sub, item)


def add_common_flags(p: argparse.ArgumentParser, defaults: bool = False) -> argparse.ArgumentParser:
    """Add the global flags (--json, --quiet/-q, --root, --debug) to ``p``, skipping any it already has.
    ``defaults=False`` gives them SUPPRESS defaults so a flag given before the sub-command is not overwritten."""
    d = (lambda v: v) if defaults else (lambda v: argparse.SUPPRESS)
    specs = [(("--json",), dict(action="store_true", default=d(False), help="emit machine-readable JSON")),
             (("--quiet", "-q"), dict(action="store_true", default=d(False), help="no progress/warnings on stderr")),
             (("--root",), dict(default=d(None), help="photo library root (default: current directory)")),
             (("--debug",), dict(action="store_true", default=d(False), help="show tracebacks"))]
    for names, kw in specs:
        if not any(n in p._option_string_actions for n in names):
            p.add_argument(*names, **kw)
    return p


def _common_parser(defaults: bool) -> argparse.ArgumentParser:
    """Parent parser carrying the global flags; use it as ``parents=[_common_parser(False)]`` for any sub-parser."""
    return add_common_flags(argparse.ArgumentParser(add_help=False), defaults)


def _propagate_common_flags(parser: argparse.ArgumentParser) -> None:
    """Give every nested sub-command parser (``mask preview``, ``models list``, ``tag add`` ...) the global flags, so they
    are accepted anywhere on the command line no matter how a plugin built its parsers."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                add_common_flags(child, defaults=False)
                _propagate_common_flags(child)


def build_parser() -> tuple[argparse.ArgumentParser, list[str]]:
    parser = argparse.ArgumentParser(
        prog="photo", parents=[_common_parser(True)], formatter_class=argparse.RawDescriptionHelpFormatter,
        description="photo-studio: scene-referred RAW/JPEG develop, edit, export and library tools "
                    "(Canon R50 CR3+JPEG). Every command supports --json.")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    proxy = _SubparserProxy(sub, _common_parser(False))
    warnings: list[str] = []
    from . import commands

    for mod in sorted(pkgutil.iter_modules(commands.__path__), key=lambda m: m.name):
        full = f"{commands.__name__}.{mod.name}"
        try:
            m = importlib.import_module(full)
            reg = getattr(m, "register", None)
            if reg is None:
                continue
            reg(proxy)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not take the CLI down
            warnings.append(f"command module '{mod.name}' failed to load: {type(exc).__name__}: {exc}")
    _propagate_common_flags(parser)
    return parser, warnings


def emit(result: Any, args: argparse.Namespace) -> None:
    if getattr(args, "json", False):
        print(dumps(_strip_text(result)))
    elif result is not None:
        print(format_text(result))


_NEG_LIST = re.compile(r"^-\d+(\.\d+)?(,-?\d+(\.\d+)?)+$")


def fix_argv(argv: list[str]) -> list[str]:
    """argparse reads ``--loudnorm -14,-1,11`` as a flag (``-14,...`` is not a plain negative number). A comma list that
    starts with a minus sign after a ``--option`` is merged into ``--option=VALUE`` so it parses as the value."""
    out: list[str] = []
    for tok in argv:
        if out and _NEG_LIST.match(tok) and out[-1].startswith("--") and "=" not in out[-1]:
            out[-1] = f"{out[-1]}={tok}"
        else:
            out.append(tok)
    return out


def main(argv: list[str] | None = None) -> int:
    parser, load_warnings = build_parser()
    args = parser.parse_args(fix_argv(list(sys.argv[1:] if argv is None else argv)))
    quiet = getattr(args, "quiet", False)
    if load_warnings and not quiet:
        for w in load_warnings:
            print(f"warning: {w}", file=sys.stderr)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    try:
        result = func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "debug", False) or os.environ.get("PHOTOSTUDIO_DEBUG"):
            traceback.print_exc()
        msg = f"{type(exc).__name__}: {exc}" if not isinstance(exc, (MissingDependency, FileNotFoundError, ValueError)) else str(exc)
        if getattr(args, "json", False):
            print(dumps({"error": msg, "type": type(exc).__name__}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1
    emit(result, args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
