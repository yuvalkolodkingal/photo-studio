"""Library endpoints: info, folders, image listing, thumbnails, metadata writes, albums, tags, culling."""
from __future__ import annotations

import asyncio
import functools

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response

from .. import __version__
from . import paths as P
from .api_common import S, jr, need

router = APIRouter(prefix="/api")

LIST_KEYS = ("folder", "recursive", "album", "rating_min", "flag", "label", "tag", "q", "edited", "mode", "date_from", "date_to",
             "sort", "order", "limit", "offset", "camera", "lens", "issue")


@router.get("/info")
def info(request: Request):
    s = S(request)
    from ..pipeline import registry

    return jr({"app": "photo-studio web editor", "version": __version__, "root": str(s.root), "root_name": s.root.name,
               "catalog": s.lib.has_catalog, "catalog_error": s.lib.catalog_error, "export_dir": s.export_dir,
               "n_ops": len(registry.all_ops()), "op_load_errors": dict(registry.LOAD_ERRORS), "ai_cache": True,
               "scanning": s.lib.scanning})


@router.get("/library/folders")
def folders(request: Request):
    return jr({"folders": S(request).lib.folders()})


@router.get("/library/images")
def images(request: Request):
    s = S(request)
    q = {k: v for k, v in request.query_params.items() if k in LIST_KEYS}
    if "recursive" in q:
        q["recursive"] = str(q["recursive"]).lower() in ("1", "true", "yes")
    res = s.lib.list_images(q)
    # watch sidecars of what the user is looking at so external (Claude/CLI) edits show up
    s.events.watch([i["p"] for i in res["items"][:800]], replace=False)
    return jr(res)


@router.post("/library/scan")
def scan(request: Request, body: dict = Body(default={})):
    s = S(request)
    s.lib.start_scan(wait=bool(body.get("wait", False)), prune=True)
    return jr({"scanning": s.lib.scanning, "stats": s.lib.last_scan_stats})


@router.get("/library/stats")
def stats(request: Request):
    return jr(S(request).lib.stats())


@router.get("/image/item")
def item(request: Request, p: str = Query(...)):
    return jr(S(request).lib.get_item(p))


@router.get("/thumb")
async def thumb(request: Request, p: str = Query(...), size: int = Query(320), v: str = Query("")):
    s = S(request)
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(s.renderer.pool, functools.partial(s.renderer.thumb, p, size))
    inm = request.headers.get("if-none-match")
    if inm and inm == r.etag:
        return Response(status_code=304)
    return Response(r.data, media_type=r.media_type, headers={"ETag": r.etag, "Cache-Control": "private, max-age=3600" if v else "no-cache"})


@router.post("/image/meta")
def set_meta(request: Request, body: dict = Body(...)):
    """{paths: [...], rating?: 0-5, flag?: pick|reject|null, label?: red|...|null, add_tags?, remove_tags?, notes?}"""
    s = S(request)
    need(body, "paths")
    kw = {}
    if "rating" in body:
        kw["rating"] = body["rating"]
    if "flag" in body:
        kw["flag"] = body["flag"]
    if "label" in body:
        kw["label"] = body["label"]
    for k in ("add_tags", "remove_tags", "notes"):
        if k in body:
            kw[k] = body[k]
    res = s.lib.set_meta(list(body["paths"]), **kw)
    out = []
    for r in res["updated"]:
        try:
            it = s.lib.get_item(r)
            out.append({"p": r, "rating": it["rating"], "flag": it["flag"], "label": it["label"], "tags": it["tags"],
                        "mtime_ns": s.edits.sidecar_mtime(P.resolve_image(s.root, r))})
            s.events.note_own_write(r, out[-1]["mtime_ns"])
        except Exception:  # noqa: BLE001
            pass
    return jr({"updated": out})


# ------------------------------------------------------------------ albums & tags
@router.get("/albums")
def albums(request: Request):
    s = S(request)
    return jr({"albums": s.lib.albums(), "available": s.lib.has_catalog})


@router.post("/albums")
def album_create(request: Request, body: dict = Body(...)):
    s = S(request)
    need(body, "name")
    a = s.lib.album_create(body["name"], kind=body.get("kind", "static"), rule=body.get("rule"), parent=body.get("parent"),
                           images=body.get("paths"))
    return jr(a)


@router.post("/albums/{album_id}/images")
def album_images(request: Request, album_id: int, body: dict = Body(...)):
    need(body, "paths")
    return jr(S(request).lib.album_images(album_id, list(body["paths"]), body.get("action", "add")))


@router.post("/albums/{album_id}/rename")
def album_rename(request: Request, album_id: int, body: dict = Body(...)):
    need(body, "name")
    return jr(S(request).lib.album_rename(album_id, body["name"]))


@router.delete("/albums/{album_id}")
def album_delete(request: Request, album_id: int):
    return jr(S(request).lib.album_delete(album_id))


@router.get("/tags")
def tags(request: Request):
    return jr({"tags": S(request).lib.tags()})


# ------------------------------------------------------------------ culling
@router.get("/cull/info")
def cull_info(request: Request, p: str = Query(...)):
    """Cached analysis of one image (sharpness, clipping, faces, issues) if the catalog has it."""
    s = S(request)
    P.resolve_image(s.root, p)
    a = s.lib.analysis(p)
    return jr({"p": p, "analysis": a})


@router.post("/cull/run")
def cull_run(request: Request, body: dict = Body(...)):
    """Start a background cull run over ``paths`` (or a ``folder``): analysis + burst grouping + suggested flags."""
    s = S(request)
    rels = body.get("paths")
    if not rels:
        res = s.lib.list_images({"folder": body.get("folder", ""), "recursive": bool(body.get("recursive")),
                                 "album": body.get("album"), "limit": 5000})
        rels = [i["p"] for i in res["items"]]
    rels = [P.to_rel(s.root, P.resolve_image(s.root, r)) for r in rels][:5000]
    if not rels:
        raise ValueError("no images to cull")
    target = body.get("target")
    genre = body.get("genre")

    def run(job):
        from .. import cull

        paths = [str(s.root / r) for r in rels]
        job.total = len(paths)
        res = cull.cull(paths, target=target, genre=genre, analyses=s.analysis_cache, progress=job.progress)
        out = []
        for r, rel in zip(res, rels):
            out.append({"p": rel, "score": r["score"], "flag": r["flag"], "reasons": r.get("reasons", []), "issues": r.get("issues", []),
                        "group": r.get("group"), "burst": r.get("burst"), "group_size": r.get("group_size", 1),
                        "group_rank": r.get("group_rank", 1), "best_of_group": bool(r.get("best_of_group")),
                        "analysis": r.get("analysis", {}), "severity": r.get("severity", {})})
        return out

    job = s.jobs.submit("cull", run)
    return jr(job.to_dict(with_results=False))
