"""Develop endpoints: sidecar load/save, history, versions, snapshots, render, scopes, schema, presets, masks, WB."""
from __future__ import annotations

import asyncio
import functools

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response

from .. import presets as presets_mod
from .. import sidecar
from ..pipeline import engine
from . import edits as E
from . import paths as P
from . import presetops
from .api_common import S, jr, need
from .render import Superseded
from .schema import build_schema

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ sidecar / stack
@router.get("/image")
def image(request: Request, p: str = Query(...), version: str | None = Query(None)):
    s = S(request)
    out = s.edits.load(p, version)
    s.events.watch([out["p"]])
    try:
        out["meta"] = _meta_subset(s.edits.metadata(P.resolve_image(s.root, p)))
    except Exception:  # noqa: BLE001
        out["meta"] = {}
    return jr(out)


def _meta_subset(md: dict) -> dict:
    keys = ("make", "model", "lens", "iso", "shutter", "aperture", "focal", "focal35", "exposure_comp", "datetime", "width",
            "height", "gps_lat", "gps_lon", "white_balance", "flash", "picture_style")
    return {k: md[k] for k in keys if md.get(k) is not None}


@router.put("/image/stack")
def put_stack(request: Request, body: dict = Body(...)):
    """{p, stack, version?, label?, mode?, art?, force?, snapshot_first?}  read-modify-write of the sidecar stack."""
    s = S(request)
    need(body, "p", "stack")
    kw = {}
    for k in ("mode", "art"):
        if k in body:
            kw[k] = body[k]
    try:
        res = s.edits.save(body["p"], body["stack"], body.get("version"), body.get("label") or "Edit", force=bool(body.get("force")),
                           snapshot_first=bool(body.get("snapshot_first")), **kw)
    except E.Conflict as exc:
        cur = s.edits.load(body["p"], body.get("version"))
        return jr({"error": str(exc), "conflict": True, "image": cur}, status=409)
    return jr(res)


@router.post("/image/undo")
def undo(request: Request, body: dict = Body(...)):
    need(body, "p")
    return jr(S(request).edits.undo(body["p"], body.get("version")))


@router.post("/image/redo")
def redo(request: Request, body: dict = Body(...)):
    need(body, "p")
    return jr(S(request).edits.redo(body["p"], body.get("version")))


@router.post("/image/history/goto")
def history_goto(request: Request, body: dict = Body(...)):
    need(body, "p", "id")
    return jr(S(request).edits.goto(body["p"], body["id"], body.get("version")))


@router.get("/image/history")
def history(request: Request, p: str = Query(...), version: str | None = Query(None)):
    s = S(request)
    path = P.resolve_image(s.root, p)
    rel = P.to_rel(s.root, path)
    v = version or sidecar.load(path)["active"]
    return jr(s.edits._hist(rel, v).listing())


@router.post("/image/versions")
def versions(request: Request, body: dict = Body(...)):
    """{p, action: create|switch|rename|delete, name, new_name?, from_version?}"""
    need(body, "p", "action")
    return jr(S(request).edits.versions(body["p"], body["action"], body.get("name"), body.get("new_name"), body.get("from_version")))


@router.post("/image/snapshots")
def snapshots(request: Request, body: dict = Body(...)):
    """{p, action: create|restore|delete, name}"""
    need(body, "p", "action", "name")
    return jr(S(request).edits.snapshot(body["p"], body["action"], body["name"], body.get("version")))


@router.post("/image/reset")
def reset(request: Request, body: dict = Body(...)):
    need(body, "p")
    return jr(S(request).edits.reset(body["p"], body.get("version")))


@router.post("/image/copy")
def copy_settings(request: Request, body: dict = Body(...)):
    need(body, "p")
    return jr(S(request).edits.copy_settings(body["p"], body.get("ops"), body.get("version")))


@router.post("/image/paste")
def paste_settings(request: Request, body: dict = Body(...)):
    need(body, "paths")
    return jr(S(request).edits.paste_settings(list(body["paths"]), body.get("mode", "replace"), body.get("ops"), body.get("version")))


@router.post("/image/sync")
def sync_settings(request: Request, body: dict = Body(...)):
    need(body, "src", "paths")
    return jr(S(request).edits.sync_settings(body["src"], list(body["paths"]), body.get("ops"), body.get("mode", "replace"),
                                             body.get("version")))


@router.post("/watch")
def watch(request: Request, body: dict = Body(...)):
    s = S(request)
    rels = [P.to_rel(s.root, P.resolve_image(s.root, r)) for r in body.get("paths", [])[:2000]]
    s.events.watch(rels, replace=bool(body.get("replace")))
    return jr({"watching": len(rels)})


@router.get("/changes")
async def changes(request: Request, since: int = Query(0), wait: float = Query(0.0)):
    """Long-poll event log: sidecars changed on disk (external=true when Claude/CLI wrote them), saves, job events."""
    s = S(request)
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, s.events.since, since, min(max(wait, 0.0), 25.0))
    return jr(res)


# ------------------------------------------------------------------ render
def _geo(v):
    """nogeo: false | true (all geometry ops off) | ["crop", ...] (only those off, e.g. while the crop tool is open)."""
    if isinstance(v, (list, tuple)):
        return frozenset(str(x) for x in v) or False
    return bool(v)


def _int(v, default, lo, hi):
    try:
        return int(min(max(int(v), lo), hi))
    except (TypeError, ValueError):
        return default


def _send(r, status: int = 200) -> Response:
    if r.headers.get("status") == 304:
        return Response(status_code=304, headers={"ETag": r.etag})
    h = {k: v for k, v in r.headers.items() if k != "status"}
    if r.etag:
        h["ETag"] = r.etag
    h["Cache-Control"] = "no-store" if not r.etag else "no-cache"
    h["Access-Control-Expose-Headers"] = "X-Image-Size, X-Full-Size, X-Render-Ms, X-Warnings, X-Draft, X-Region, X-Mask-Notes, X-Seq"
    return Response(r.data, media_type=r.media_type, headers=h, status_code=status)


@router.post("/render")
async def render(request: Request, body: dict = Body(...)):
    """{p, stack?, long_edge?, quality?, before?, nogeo?, overlay?: clipping|false_color, session?, seq?, draft?}
    Latest-wins per session: a request whose ``seq`` is older than a newer one for the same session returns 204."""
    s = S(request)
    need(body, "p")
    sess, seq = s.renderer.register_seq(body.get("session"), body.get("seq"))
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        s.renderer.render_preview, body["p"], body.get("stack"), long_edge=_int(body.get("long_edge"), 1600, 32, 8000),
        quality=_int(body.get("quality"), 85, 30, 98), before=bool(body.get("before")), nogeo=_geo(body.get("nogeo")),
        overlay=body.get("overlay"), sid=sess.sid, seq=seq, version=body.get("version"), draft=bool(body.get("draft")),
        if_none_match=request.headers.get("if-none-match"))
    try:
        r = await loop.run_in_executor(s.renderer.pool, fn)
    except Superseded:
        return Response(status_code=204, headers={"X-Superseded": "1", "X-Seq": str(seq)})
    resp = _send(r)
    resp.headers["X-Seq"] = str(seq)
    return resp


@router.get("/render")
async def render_get(request: Request, p: str = Query(...), long_edge: int = Query(1600), version: str | None = Query(None),
                     before: bool = Query(False), quality: int = Query(85)):
    """Render with the stack stored in the sidecar (ETag/304 aware)."""
    s = S(request)
    fn = functools.partial(s.renderer.render_preview, p, None, long_edge=_int(long_edge, 1600, 32, 8000), quality=_int(quality, 85, 30, 98),
                           before=before, version=version, if_none_match=request.headers.get("if-none-match"))
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(s.renderer.pool, fn)
    return _send(r)


@router.post("/render/region")
async def render_region(request: Request, body: dict = Body(...)):
    """{p, stack?, rect: [x0,y0,x1,y1] normalised (post-geometry), max_px?, nogeo?} -> 1:1 JPEG crop of the full-res render."""
    s = S(request)
    need(body, "p", "rect")
    fn = functools.partial(s.renderer.render_region, body["p"], body.get("stack"), body["rect"], max_px=_int(body.get("max_px"), 2600, 64, 6000),
                           nogeo=_geo(body.get("nogeo")), version=body.get("version"))
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(s.renderer.pool, fn)
    return _send(r)


# ------------------------------------------------------------------ scopes
@router.get("/scopes/histogram")
def histogram(request: Request, session: str = Query("default"), bins: int = Query(128)):
    return jr(S(request).renderer.histogram(session, bins=bins if bins in (64, 128, 256) else 128))


@router.get("/scopes/{kind}.png")
def scope_png(request: Request, kind: str, session: str = Query("default"), w: int = Query(512), h: int = Query(256)):
    data = S(request).renderer.scope_png(session, kind, _int(w, 512, 64, 1600), _int(h, 256, 64, 1200))
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ schema
@router.get("/schema")
def schema(request: Request):
    sch = build_schema()
    tag = f'W/"schema-{len(sch["ops"])}"'
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304)
    return jr(sch, headers={"ETag": tag, "Cache-Control": "no-cache"})


# ------------------------------------------------------------------ presets
@router.get("/presets")
def presets_list(kind: str = Query("art")):
    if kind == "user":
        return jr({"kind": "user", "presets": presetops.list_user()})
    return jr({"kind": kind, "presets": presetops.list_kind(kind)})


@router.get("/presets/{kind}/{name}")
def preset_get(kind: str, name: str, variant: str | None = Query(None)):
    pre = presets_mod.load_preset(kind, name, variant)
    pre.pop("path", None)
    return jr(pre)


@router.post("/presets/apply")
def preset_apply(request: Request, body: dict = Body(...)):
    """{p, kind: mode|art|picture_style, name, variant?, stack?, replace?} -> {stack, mode, art}. Does not save."""
    s = S(request)
    need(body, "p", "kind", "name")
    path = P.resolve_image(s.root, body["p"])
    kind = engine.source_kind(path)
    cur = body.get("stack")
    if cur is None:
        cur = sidecar.get_stack(sidecar.load(path))
    else:
        cur = E.clean_stack(cur)
    image_arr = None
    if str(body["name"]).split(":")[0] == "auto":
        from ..decode import preview_display

        image_arr = preview_display(path, 512)
    stack, info = presetops.apply(body["kind"], body["name"], cur, kind, s.edits.metadata(path), body.get("variant"),
                                  bool(body.get("replace")), image=image_arr)
    return jr({"stack": stack, "mode": None if info["mode"] is ... else info["mode"], "art": None if info["art"] is ... else info["art"],
               "set_mode": info["mode"] is not ..., "set_art": info["art"] is not ..., "info": info.get("info")})


@router.post("/presets/remove")
def preset_remove(request: Request, body: dict = Body(...)):
    need(body, "kind", "name", "stack")
    return jr({"stack": presetops.remove(body["kind"], body["name"], E.clean_stack(body["stack"]), body.get("variant"))})


@router.post("/presets/user")
def preset_save_user(request: Request, body: dict = Body(...)):
    """Save the current stack as a user preset: {name, stack, kind?: art|mode, scope?: look|all|tone_color, title?, description?}"""
    need(body, "name", "stack")
    name = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(body["name"]).strip().lower().replace(" ", "_"))[:60]
    if not name:
        raise ValueError("preset name is empty")
    kind = body.get("kind", "art")
    stack = presetops.stack_for_user_preset(E.clean_stack(body["stack"]), body.get("scope", "look"))
    if not stack:
        raise ValueError("nothing to save (the stack has no entries in this scope)")
    pre = presets_mod.preset_from_stack(kind, name, stack, title=body.get("title") or str(body["name"]),
                                        description=body.get("description", "Saved from the web editor"), tags=["user"])
    path = presets_mod.save_user_preset(kind, name, pre, overwrite=bool(body.get("overwrite", True)))
    return jr({"name": name, "kind": kind, "path": path.name, "n_ops": len(stack)})


@router.delete("/presets/user/{kind}/{name}")
def preset_delete_user(kind: str, name: str):
    d = presets_mod.user_preset_dir(kind)
    f = d / f"{name}.yaml"
    if not f.is_file() or f.resolve().parent != d.resolve():
        raise FileNotFoundError(f"no user preset '{name}'")
    f.unlink()
    return jr({"deleted": name})


@router.get("/preset_thumb")
async def preset_thumb(request: Request, p: str = Query(...), kind: str = Query(...), name: str = Query(...),
                       variant: str | None = Query(None), size: int = Query(256)):
    """Image rendered with a preset applied on top of the current sidecar stack (hover previews, cached on disk)."""
    s = S(request)
    path = P.resolve_image(s.root, p)
    cur = sidecar.get_stack(sidecar.load(path))
    stack, _ = presetops.apply(kind, name, cur, engine.source_kind(path), s.edits.metadata(path), variant, False)
    pre_mtime = ""
    try:
        pre_mtime = str(presets_mod.load_preset(kind, name, variant).get("path"))
    except Exception:  # noqa: BLE001
        pass
    tag = f"{kind}:{name}:{variant}:{pre_mtime}"
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(s.renderer.pool, functools.partial(s.renderer.preset_thumb, p, stack, tag, _int(size, 256, 64, 1024)))
    return Response(r.data, media_type="image/jpeg", headers={"ETag": r.etag, "Cache-Control": "private, max-age=3600"})


@router.post("/mode/detect")
def mode_detect(request: Request, body: dict = Body(...)):
    """Auto-detect the shoot mode from EXIF/MakerNote and the image: {mode, confidence, reasons, variant, candidates}."""
    from .. import modes
    from ..decode import preview_display

    s = S(request)
    need(body, "p")
    path = P.resolve_image(s.root, body["p"])
    img = None
    try:
        img = preview_display(path, 512)
    except Exception:  # noqa: BLE001
        pass
    det = modes.detect_mode(s.edits.metadata(path), img)
    det["preset"] = modes.mode_preset_name(det) if det.get("mode") else None
    det.pop("features", None)
    return jr(det)


# ------------------------------------------------------------------ masks / WB
@router.post("/mask/preview")
async def mask_preview(request: Request, body: dict = Body(...)):
    """{p, mask: spec, stack?, long_edge?, nogeo?} -> JPEG with the mask as red overlay."""
    s = S(request)
    need(body, "p", "mask")
    fn = functools.partial(s.renderer.mask_preview, body["p"], body.get("stack"), body["mask"], long_edge=_int(body.get("long_edge"), 1024, 64, 3000),
                           nogeo=_geo(body.get("nogeo")))
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(s.renderer.pool, fn)
    return _send(r)


@router.post("/mask/ai")
async def mask_ai(request: Request, body: dict = Body(...)):
    """{p, type: ai_subject|ai_background|ai_sky|ai_face|ai_eyes|ai_lips|ai_teeth|ai_skin|ai_animal, stack?} -> coverage, bbox,
    notes (which backend ran). Warms the server-side AI mask cache so later renders with this mask are instant."""
    s = S(request)
    need(body, "p", "type")
    spec = {"type": body["type"], **{k: v for k, v in (body.get("params") or {}).items()}}
    fn = functools.partial(s.renderer.mask_ai, body["p"], spec, body.get("stack"), _int(body.get("long_edge"), 1024, 64, 3000))
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(s.renderer.pool, fn)
    res["spec"] = spec
    return jr(res)


@router.post("/wb/pick")
def wb_pick(request: Request, body: dict = Body(...)):
    """{p, x, y, radius?} (normalised, on the un-cropped image) -> {temperature, tint} that neutralise the sampled grey."""
    s = S(request)
    need(body, "p", "x", "y")
    return jr(s.renderer.wb_pick(body["p"], float(body["x"]), float(body["y"]), float(body.get("radius", 0.01))))
