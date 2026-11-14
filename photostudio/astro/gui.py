"""`photo astro gui`: a Sequator-style stacking GUI served on 127.0.0.1 (FastAPI + one static page).

Layout follows Sequator: inputs on the left (star images, noise/dark images, flats, base image, output), composition
options on the right (align, freeze ground, reduce distortion, integration, light pollution, star enhancement, HDR,
auto brightness, noise reduction) and the picture in the middle. After a stack finishes, the finishing sliders
re-render a preview from the linear result in about a second; "Save" renders the full-resolution file.
"""
from __future__ import annotations

import io
import os
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import cv2
import numpy as np

STATIC = Path(__file__).resolve().parent / "static"
IMAGE_EXTS = {".cr3", ".cr2", ".nef", ".arw", ".raf", ".rw2", ".orf", ".dng", ".pef", ".srw",
              ".tif", ".tiff", ".jpg", ".jpeg", ".png", ".fit", ".fits", ".fts"}


def allowed_roots() -> list[Path]:
    roots = [Path.home()] + [Path(p) for p in ("/run/media", "/media", "/mnt") if Path(p).exists()]
    extra = os.environ.get("PHOTOSTUDIO_ASTRO_ROOTS")
    if extra:
        roots += [Path(p) for p in extra.split(os.pathsep) if p]
    return [r.resolve() for r in roots]


def check_path(p: str | Path, must_exist: bool = True) -> Path:
    q = Path(p).expanduser().resolve()
    if not any(q == r or r in q.parents for r in allowed_roots()):
        raise PermissionError(f"path outside the allowed folders: {q}")
    if must_exist and not q.exists():
        raise FileNotFoundError(str(q))
    return q


class Job:
    def __init__(self, req: dict):
        self.id = uuid.uuid4().hex[:10]
        self.req = req
        self.state = "queued"          # queued | running | done | error | cancelled
        self.stage, self.frac, self.msg = "", 0.0, ""
        self.log: list[str] = []
        self.cancel = threading.Event()
        self.result = None             # StackResult
        self.small = None              # downscaled linear for fast re-finishing
        self.preview: bytes | None = None
        self.error: str | None = None
        self.started = time.time()
        self.saved: list[str] = []

    def progress(self, stage, frac, msg=""):
        if stage != self.stage:
            self.log.append(f"{time.strftime('%H:%M:%S')}  {stage}: {msg}")
        elif msg and frac >= 1.0:
            self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        self.stage, self.frac, self.msg = stage, float(frac), msg

    def status(self) -> dict:
        info = None
        if self.result is not None:
            i = self.result.info
            info = {k: i.get(k) for k in ("frames_total", "frames_used", "registration", "reference", "nudges_before",
                                          "segments", "dropped", "seconds", "notes", "total_exposure_s",
                                          "stars_per_frame_median", "noise_gain_vs_single_frame", "validation")}
        return {"id": self.id, "state": self.state, "stage": self.stage, "frac": self.frac, "msg": self.msg,
                "log": self.log[-200:], "error": self.error, "info": info, "elapsed": round(time.time() - self.started),
                "saved": self.saved}


def _jpeg(disp: np.ndarray, q: int = 90) -> bytes:
    a = (np.clip(disp, 0, 1) * 255 + 0.5).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", a[..., ::-1], [cv2.IMWRITE_JPEG_QUALITY, q])
    return buf.tobytes()


def thumbnail(path: Path, size: int = 900) -> bytes:
    """Fast preview: the embedded JPEG of a raw file when there is one, else a quick decode and auto stretch."""
    if path.suffix.lower() in {".cr3", ".cr2", ".nef", ".arw", ".raf", ".rw2", ".orf", ".dng", ".pef", ".srw"}:
        try:
            import rawpy

            with rawpy.imread(str(path)) as r:
                t = r.extract_thumb()
            if t.format == rawpy.ThumbFormat.JPEG:
                from PIL import Image

                im = Image.open(io.BytesIO(t.data))
                im.thumbnail((size, size))
                b = io.BytesIO()
                im.convert("RGB").save(b, "JPEG", quality=85)
                return b.getvalue()
        except Exception:  # noqa: BLE001
            pass
    from .engine import load_any

    img, _ = load_any(path, draft=True)
    h, w = img.shape[:2]
    s = size / max(h, w)
    img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    L = img.mean(2)
    lo, hi = np.percentile(L, 5), np.percentile(L, 99.8)
    x = np.clip((img - lo) / max(hi - lo, 1e-9), 0, 1)
    return _jpeg(np.arcsinh(30 * x) / np.arcsinh(30))


def _finish_opts(d: dict, job: "Job | None" = None):
    from .post import FinishOptions

    f = FinishOptions()
    if job is not None and all(Path(p).suffix.lower() in {".fit", ".fits", ".fts"} for p in job.req["lights"]):
        f.to_srgb = False          # FITS from Siril / DSS is camera RGB, not linear Rec.2020
    for k, v in (d or {}).items():
        if hasattr(f, k):
            setattr(f, k, type(getattr(f, k))(v))
    return f


def _run(job: Job, workers: int):
    from . import engine, post

    r = job.req
    job.state = "running"
    try:
        o = r.get("options", {})
        fg = None
        if o.get("freeze") == "auto":
            fg = "auto"
        opts = engine.StackOptions(
            align=o.get("align", "auto"), reject=o.get("reject", "sigma"), kappa=float(o.get("kappa", 2.5)),
            keep=float(o.get("keep", 1.0)), group=int(o.get("group", 8)),
            reference=r["lights"].index(r["base"]) if r.get("base") in r["lights"] else None,
            lens=bool(o.get("lens")), darks=r.get("darks", []), flats=r.get("flats", []), bias=r.get("bias", []),
            freeze_ground=fg, horizon=float(o["horizon"]) if o.get("freeze") == "horizon" else None,
            halves=True, jobs=int(o.get("jobs") or workers or 0), mem_gb=float(o.get("mem_gb") or 0),
            draft=bool(o.get("draft")), workdir=o.get("workdir") or None)
        res = engine.stack(r["lights"], opts, progress=job.progress, cancel=job.cancel)
        job.result = res
        job.progress("finish", 0.5, "validating faint stars in both half stacks")
        if res.half_a is not None:
            v = post.validate_sources(res.image, res.half_a, res.half_b)
            res.info["validation"] = {k: v[k] for k in ("candidates", "confirmed", "half_test_false_alarm_rate")}
        h, w = res.image.shape[:2]
        s = 1600 / max(h, w)
        job.small = cv2.resize(res.image, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        disp, _ = post.finish(job.small, _finish_opts(r.get("finish"), job))
        job.preview = _jpeg(disp)
        if r.get("out"):
            job.progress("save", 0.0, "rendering full resolution")
            _save(job, r["out"], r.get("finish"))
        job.state = "done"
        job.progress("done", 1.0, f"{res.info['frames_used']} of {res.info['frames_total']} frames stacked")
    except engine.Cancelled:
        job.state = "cancelled"
        job.progress("cancelled", 0.0, "cancelled")
    except Exception as exc:  # noqa: BLE001
        job.state, job.error = "error", f"{type(exc).__name__}: {exc}"
        job.log.append("ERROR " + job.error)


def _save(job: Job, out: str, finish: dict | None) -> list[str]:
    from ..commands.astro_cmd import _save_display, save_linear
    from . import post

    outp = check_path(out, must_exist=False)
    disp, _ = post.finish(job.result.image, _finish_opts(finish, job))
    saved = [_save_display(disp, str(outp)), save_linear(job.result.image, str(outp.with_suffix("")) + "_linear.tif")]
    job.saved += saved
    return saved


def create_app(workers: int = 0):
    from fastapi import Body, FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, Response

    from ..web.server import _host_ok

    app = FastAPI(title="photo-studio astro", docs_url=None, redoc_url=None, openapi_url=None)
    jobs: dict[str, Job] = {}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not _host_ok(request.headers.get("host")):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            if origin.lower().split("://", 1)[-1] != (request.headers.get("host") or "").lower():
                return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        try:
            return await call_next(request)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except FileNotFoundError as exc:
            return JSONResponse({"error": f"not found: {exc}"}, status_code=404)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "astro.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/roots")
    def roots():
        return {"roots": [str(r) for r in allowed_roots()], "home": str(Path.home())}

    @app.get("/api/browse")
    def browse(path: str = ""):
        p = check_path(path or Path.home())
        if p.is_file():
            p = p.parent
        dirs, files = [], []
        for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if c.name.startswith("."):
                continue
            if c.is_dir():
                dirs.append(c.name)
            elif c.suffix.lower() in IMAGE_EXTS and c.exists():
                files.append(c.name)
        parent = str(p.parent) if any(p.parent == r or r in p.parent.parents for r in allowed_roots()) else None
        return {"path": str(p), "parent": parent, "dirs": dirs, "files": files}

    @app.get("/api/thumb")
    def thumb(path: str, size: int = 900):
        return Response(thumbnail(check_path(path), min(max(size, 64), 2400)), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/stack")
    def start(req: dict = Body(...)):
        if any(j.state == "running" for j in jobs.values()):
            return JSONResponse({"error": "a stack is already running"}, status_code=409)
        lights = [str(check_path(p)) for p in req.get("lights", [])]
        if len(lights) < 2:
            return JSONResponse({"error": "add at least two star images"}, status_code=400)
        req = {**req, "lights": lights,
               "darks": [str(check_path(p)) for p in req.get("darks", [])],
               "flats": [str(check_path(p)) for p in req.get("flats", [])],
               "bias": [str(check_path(p)) for p in req.get("bias", [])]}
        if req.get("base"):
            req["base"] = str(check_path(req["base"]))
        if req.get("out"):
            check_path(req["out"], must_exist=False)
        job = Job(req)
        jobs[job.id] = job
        threading.Thread(target=_run, args=(job, workers), daemon=True).start()
        return {"job": job.id}

    def get(jid: str) -> Job:
        if jid not in jobs:
            raise FileNotFoundError(f"job {jid}")
        return jobs[jid]

    @app.get("/api/job/{jid}")
    def status(jid: str):
        return get(jid).status()

    @app.post("/api/job/{jid}/cancel")
    def cancel(jid: str):
        get(jid).cancel.set()
        return {"ok": True}

    @app.get("/api/job/{jid}/preview.jpg")
    def preview(jid: str):
        j = get(jid)
        if j.preview is None:
            raise FileNotFoundError("no preview yet")
        return Response(j.preview, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/job/{jid}/refinish")
    def refinish(jid: str, finish: dict = Body(...)):
        from . import post

        j = get(jid)
        if j.small is None:
            return JSONResponse({"error": "stack not finished"}, status_code=409)
        disp, info = post.finish(j.small, _finish_opts(finish, j))
        j.preview = _jpeg(disp)
        return {"ok": True, "finish": info}

    @app.post("/api/job/{jid}/save")
    def save(jid: str, req: dict = Body(...)):
        j = get(jid)
        if j.result is None:
            return JSONResponse({"error": "stack not finished"}, status_code=409)
        return {"saved": _save(j, req["out"], req.get("finish"))}

    return app


def serve(port: int = 8766, open_browser: bool = False, workers: int = 0) -> None:
    import uvicorn

    from ..web.server import find_free_port

    port = find_free_port(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"photo-studio astro stacker: {url}  -- Ctrl+C to stop", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(workers), host="127.0.0.1", port=port, log_level="warning", access_log=False)
