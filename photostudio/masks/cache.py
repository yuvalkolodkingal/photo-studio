"""Process-level cache for AI masks.

AI masks are expensive (model inference at <= 1024 px plus a guided-filter upsample) but depend only on the unedited
source image, the mask type and the working size. By default they are cached per render context (``ctx.cache``), i.e.
recomputed on every new context. A long-running process such as ``photo serve`` can install a shared cache::

    from photostudio import masks
    masks.enable_cache(max_mb=256)          # bounded LRU keyed by (path, mtime, domain, type/size/model)
    masks.set_cache(get, put)               # or plug in your own: get(ident) -> ndarray|None, put(ident, value)
    masks.disable_cache()

``ident`` is the tuple ``(path, mtime_ns, domain, key)`` where ``key`` starts with ``("ai", <mask type>, (h, w), ...)``.
The context-level cache stays in front of it, so behaviour inside one render is unchanged.
"""
from __future__ import annotations

import collections
import os
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

_lock = threading.Lock()
_get: Callable[[tuple], Any] | None = None
_put: Callable[[tuple, Any], None] | None = None
_lru: LRUMaskCache | None = None


def _nbytes(v: Any) -> int:
    return int(v.nbytes) if isinstance(v, np.ndarray) else 4096


class LRUMaskCache:
    """Thread-safe LRU bounded by total array bytes."""

    def __init__(self, max_mb: float = 256.0):
        self.max_bytes = int(max_mb * 2 ** 20)
        self._d: collections.OrderedDict[tuple, Any] = collections.OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def get(self, ident: tuple) -> Any:
        with self._lock:
            v = self._d.get(ident)
            if v is None:
                self.misses += 1
                return None
            self._d.move_to_end(ident)
            self.hits += 1
            return v

    def put(self, ident: tuple, value: Any) -> None:
        if value is None:
            return
        with self._lock:
            old = self._d.pop(ident, None)
            if old is not None:
                self._bytes -= _nbytes(old)
            self._d[ident] = value
            self._bytes += _nbytes(value)
            while self._bytes > self.max_bytes and len(self._d) > 1:
                _, o = self._d.popitem(last=False)
                self._bytes -= _nbytes(o)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._bytes = 0

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._d), "bytes": self._bytes, "max_bytes": self.max_bytes,
                    "hits": self.hits, "misses": self.misses}

    def __len__(self) -> int:
        return len(self._d)


def set_cache(get: Callable[[tuple], Any] | None, put: Callable[[tuple, Any], None] | None) -> None:
    """Install custom process-level ``get(ident)`` / ``put(ident, value)`` callables (``None, None`` removes them)."""
    global _get, _put, _lru
    with _lock:
        _get, _put = get, put
        _lru = None


def enable_cache(max_mb: float = 256.0) -> LRUMaskCache:
    """Install (or reuse) a bounded LRU as the process-level cache and return it."""
    global _get, _put, _lru
    with _lock:
        if _lru is None:
            _lru = LRUMaskCache(max_mb)
            _get, _put = _lru.get, _lru.put
        else:
            _lru.max_bytes = int(max_mb * 2 ** 20)
        return _lru


def disable_cache() -> None:
    set_cache(None, None)


def clear_cache() -> None:
    if _lru is not None:
        _lru.clear()


def cache_stats() -> dict | None:
    return _lru.stats() if _lru is not None else None


def cache_enabled() -> bool:
    return _get is not None


def identity(ctx: Any, key: tuple) -> tuple | None:
    """Cache identity of a mask for this context, or ``None`` when it has no file behind it (in-memory arrays)."""
    path = getattr(ctx, "path", "") or ""
    if not path:
        return None
    try:
        return (str(path), os.stat(path).st_mtime_ns, getattr(ctx, "domain", ""), key)
    except OSError:
        return None


def process_get(ctx: Any, key: tuple) -> Any:
    g = _get
    if g is None:
        return None
    ident = identity(ctx, key)
    return None if ident is None else g(ident)


def process_put(ctx: Any, key: tuple, value: Any) -> None:
    p = _put
    if p is None or value is None:
        return
    ident = identity(ctx, key)
    if ident is not None:
        p(ident, value)
