"""Application state shared by all API modules (one instance per server)."""
from __future__ import annotations

import logging
from pathlib import Path

from . import edits as E
from .jobs import Jobs
from .library import Library
from .render import Renderer

log = logging.getLogger("photostudio.web")


class AppState:
    def __init__(self, root: str | Path, use_catalog: bool = True, workers: int = 2, export_dir: str = "exports",
                 background_scan: bool = True):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"library root is not a folder: {self.root}")
        self.export_dir = export_dir
        self.events = E.EventLog()
        self.lib = Library(self.root, use_catalog=use_catalog)
        self.edits = E.EditService(self.root, self.events, on_saved=self._saved)
        self.renderer = Renderer(self.root, self.edits, workers=workers)
        self.jobs = Jobs(workers=1)
        self.analysis_cache: dict = {}
        self.events.start(self.root)
        if background_scan and self.lib.has_catalog:
            self.lib.start_scan()

    def _saved(self, rel: str) -> None:
        """After every sidecar write by the web app: refresh the catalog row (has_edits / ops hash)."""
        try:
            self.lib.touch(rel)
        except Exception as exc:  # noqa: BLE001
            log.debug("catalog refresh failed for %s: %s", rel, exc)
        self.events.emit({"type": "saved", "p": rel})

    def close(self) -> None:
        self.events.stop()
        self.renderer.shutdown()
        self.jobs.shutdown()
        self.lib.close()
