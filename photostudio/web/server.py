"""FastAPI application factory + launcher for the local web editor (``photo serve``)."""
from __future__ import annotations

import contextlib
import logging
import socket
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from . import edits as E
from . import paths as P
from .library import LibraryError
from .render import RenderError
from .state import AppState

log = logging.getLogger("photostudio.web")
STATIC = Path(__file__).resolve().parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _host_ok(host_header: str | None) -> bool:
    if not host_header:
        return True
    h = host_header.strip().lower()
    if h.startswith("["):
        name = h.split("]")[0] + "]"
    else:
        name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return name in LOOPBACK or name == "testserver"


def create_app(root: str | Path, use_catalog: bool = True, workers: int = 2, export_dir: str = "exports",
               background_scan: bool = True) -> FastAPI:
    from fastapi.exceptions import RequestValidationError

    from . import api_develop, api_export, api_library
    from .schema import build_schema

    # Load the op registry once, on this thread, before any request thread can touch it: pipeline.registry sets its
    # "loaded" flag before the op modules finish importing, so a concurrent first call can see a half-filled registry.
    build_schema()

    state = AppState(root, use_catalog=use_catalog, workers=workers, export_dir=export_dir, background_scan=background_scan)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        state.close()

    app = FastAPI(title="photo-studio web editor", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.ps = state

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Local-only app: refuse foreign Host headers (DNS rebinding) and cross-origin state-changing requests (CSRF)."""
        if not _host_ok(request.headers.get("host")):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            host = (request.headers.get("host") or "").lower()
            if origin.lower().split("://", 1)[-1] != host:
                return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    def bad(status):
        def h(request: Request, exc: Exception):
            return JSONResponse({"error": str(exc), "type": type(exc).__name__}, status_code=status)
        return h

    app.add_exception_handler(P.PathError, bad(403))
    app.add_exception_handler(FileNotFoundError, bad(404))
    app.add_exception_handler(E.EditError, bad(422))
    app.add_exception_handler(LibraryError, bad(422))
    app.add_exception_handler(RenderError, bad(422))
    app.add_exception_handler(ValueError, bad(422))
    app.add_exception_handler(KeyError, bad(404))
    from ..presets import PresetError
    from ..sidecar import SidecarError

    app.add_exception_handler(PresetError, bad(422))
    app.add_exception_handler(SidecarError, bad(422))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": "invalid request: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:4])},
                            status_code=422)

    @app.exception_handler(Exception)
    async def _any(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    app.include_router(api_library.router)
    app.include_router(api_develop.router)
    app.include_router(api_export.router)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    class NoCacheStatic(StaticFiles):
        async def get_response(self, path, scope):
            r = await super().get_response(path, scope)
            r.headers["Cache-Control"] = "no-cache"
            return r

    app.mount("/static", NoCacheStatic(directory=STATIC), name="static")

    return app


def find_free_port(preferred: int, tries: int = 20) -> int:
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"no free port in {preferred}..{preferred + tries - 1}")


def serve(root: str | Path, port: int = 8765, open_browser: bool = False, use_catalog: bool = True, auto_port: bool = True,
          log_level: str = "warning") -> None:
    """Run the server on 127.0.0.1 (never any other interface) until interrupted."""
    import uvicorn

    root = Path(root).expanduser().resolve()
    if auto_port:
        port = find_free_port(port)
    app = create_app(root, use_catalog=use_catalog)
    url = f"http://127.0.0.1:{port}/"
    print(f"photo-studio web editor: {url}  (library: {root})  -- Ctrl+C to stop", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level=log_level, access_log=False)
