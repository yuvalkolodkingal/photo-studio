"""Local web editor for photo-studio (``photo serve``): a Lightroom-like Library + Develop UI over the same
``<image>.ps.json`` sidecars the CLI uses. FastAPI backend, vanilla-JS frontend, fully offline, 127.0.0.1 only."""
from __future__ import annotations


def create_app(*a, **kw):
    from .server import create_app as _c

    return _c(*a, **kw)


def serve(*a, **kw):
    from .server import serve as _s

    return _s(*a, **kw)
