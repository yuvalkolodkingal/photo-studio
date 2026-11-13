"""``photo serve [ROOT] [--port 8765] [--open]``: the local Lightroom-like web editor (127.0.0.1 only)."""
from __future__ import annotations

import os
from pathlib import Path


def _root(args) -> Path:
    r = getattr(args, "root_dir", None) or getattr(args, "root", None) or os.environ.get("PHOTOSTUDIO_ROOT")
    if r:
        return Path(r).expanduser().resolve()
    cur = Path.cwd().resolve()
    for p in (cur, *cur.parents):
        if (p / ".photostudio" / "catalog.sqlite").exists():
            return p
    return cur


def cmd_serve(args):
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        from ..util import MissingDependency

        raise MissingDependency("`photo serve` needs fastapi and uvicorn: pip install 'photostudio[serve]'") from exc
    from ..web import serve

    root = _root(args)
    if not root.is_dir():
        raise FileNotFoundError(f"library root is not a folder: {root}")
    serve(root, port=args.port, open_browser=args.open, use_catalog=not args.no_catalog, auto_port=not args.strict_port,
          log_level="info" if getattr(args, "debug", False) else "warning")
    return None


def register(sub) -> None:
    p = sub.add_parser(
        "serve", help="local web editor (Library + Develop) on 127.0.0.1",
        description="photo serve [ROOT] [--port 8765] [--open]\n\n"
                    "Starts a Lightroom-like editor in your browser for the library under ROOT (default: --root, "
                    "$PHOTOSTUDIO_ROOT, the nearest catalog folder, else the current directory). It reads and writes the same "
                    "<image>.ps.json sidecars as the CLI, so manual tweaks and Claude's CLI edits interoperate; if Claude edits "
                    "a sidecar while the UI is open, the UI reloads it. Binds to 127.0.0.1 only; works fully offline.")
    p.add_argument("root_dir", nargs="?", metavar="ROOT", help="library folder (default: --root / current directory)")
    p.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765; next free port is used if busy)")
    p.add_argument("--open", action="store_true", help="open the editor in the default browser")
    p.add_argument("--no-catalog", action="store_true", help="plain folder listing instead of the SQLite catalog")
    p.add_argument("--strict-port", action="store_true", help="fail instead of picking another port if --port is busy")
    p.set_defaults(func=cmd_serve)
