"""Export endpoints (background jobs with polled progress) and job polling."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import FileResponse

from . import edits as E
from . import paths as P
from .api_common import S, jr, need

router = APIRouter(prefix="/api")

ALLOWED_OVERRIDES = {"format", "quality", "long_edge", "short_edge", "width", "height", "megapixels", "icc", "bits", "dpi", "metadata",
                     "name", "sharpen", "fit", "copyright", "artist", "watermark", "border", "skip_existing", "subsampling",
                     "progressive", "lossless", "optimize", "dither", "compression", "no_upscale"}


@router.post("/export")
def export(request: Request, body: dict = Body(...)):
    """{paths: [...], preset?: web, overrides?: {format, quality, long_edge, ...}, version?, stack? (single image only),
    out_dir?: folder under the library root, default 'exports'} -> job (poll GET /api/jobs/{id})."""
    s = S(request)
    need(body, "paths")
    rels = [P.to_rel(s.root, P.resolve_image(s.root, r)) for r in body["paths"]]
    if not rels:
        raise ValueError("no images selected")
    preset = body.get("preset") or "web"
    ov = {k: v for k, v in (body.get("overrides") or {}).items() if k in ALLOWED_OVERRIDES}
    bad = sorted(set(body.get("overrides") or {}) - ALLOWED_OVERRIDES)
    if bad:
        raise ValueError(f"override(s) not allowed: {', '.join(bad)}")
    stack = None
    if body.get("stack") is not None:
        if len(rels) != 1:
            raise ValueError("a stack override only works with a single image")
        stack = E.clean_stack(body["stack"])
        ov["stack"] = stack
    out_rel = P.clean_rel(body.get("out_dir") or s.export_dir) or s.export_dir
    out_dir = P.safe_dir(s.root, out_rel, create=True)
    version = body.get("version")

    def run(job):
        from ..export import export_batch

        job.total = len(rels)
        res = export_batch([str(s.root / r) for r in rels], preset=preset, out_dir=str(out_dir), version=version, jobs=1,
                           progress=job.progress, **ov)
        rows = []
        for rel, r in zip(rels, res):
            row = {"src": rel, "ok": bool(r.get("ok")), "error": r.get("error"), "warnings": r.get("warnings", [])}
            if r.get("ok") and r.get("out"):
                try:
                    row["out"] = Path(r["out"]).resolve().relative_to(s.root).as_posix()
                except ValueError:
                    row["out"] = r["out"]
                for k in ("width", "height", "bytes", "format", "seconds"):
                    row[k] = r.get(k)
            rows.append(row)
        return {"files": rows, "out_dir": out_rel, "ok": sum(1 for r in rows if r["ok"]), "failed": sum(1 for r in rows if not r["ok"])}

    job = s.jobs.submit("export", run)
    return jr(job.to_dict(with_results=False))


@router.get("/jobs/{job_id}")
def job(request: Request, job_id: str):
    j = S(request).jobs.get(job_id)
    if j is None:
        raise FileNotFoundError(f"no such job {job_id}")
    return jr(j.to_dict())


@router.post("/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: str):
    j = S(request).jobs.get(job_id)
    if j is None:
        raise FileNotFoundError(f"no such job {job_id}")
    j.cancel.set()
    return jr({"cancelled": True})


@router.get("/file")
def file(request: Request, p: str = Query(...)):
    """Download a file produced under the export folder (never anything else)."""
    s = S(request)
    full = P.resolve_under(s.root, p)
    exp = (s.root / s.export_dir).resolve()
    if not full.is_file() or not full.is_relative_to(exp):
        raise P.PathError("only files inside the export folder can be downloaded")
    return FileResponse(full, filename=full.name)
