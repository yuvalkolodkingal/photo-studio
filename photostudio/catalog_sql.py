"""SQLite schema of the catalog plus the *safe* rule compiler and query DSL shared by `Catalog.images()`,
smart albums and the CLI.

Rules are JSON (``{"all": [...], "any": [...], "none": [...]}`` groups of ``{"field", "op", "value"}``
conditions, nestable). Fields and operators are whitelisted and every value is bound as a SQL parameter, so a
rule can never inject SQL. The text DSL (``rating>=4 AND tag:people|noa AND taken:[2026-06-01;2026-06-30]
AND camera:"EOS R50" EXCEPT flag:reject``) compiles to the same JSON.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS images(
  id INTEGER PRIMARY KEY, uid TEXT UNIQUE,
  dir TEXT, stem TEXT, primary_path TEXT UNIQUE NOT NULL, raw_path TEXT, jpg_path TEXT, kind TEXT,
  content_hash TEXT, hash_algo TEXT, file_size INTEGER, file_mtime REAL, jpg_size INTEGER, jpg_mtime REAL,
  taken_at TEXT, taken_subsec REAL, taken_ts REAL, imported_at TEXT, added_at TEXT,
  width INTEGER, height INTEGER, orientation INTEGER,
  make TEXT, camera TEXT, lens TEXT, iso REAL, shutter REAL, aperture REAL, focal REAL, focal35 REAL,
  serial TEXT, file_number TEXT, gps_lat REAL, gps_lon REAL, gps_alt REAL,
  rating INTEGER DEFAULT 0, flag TEXT, label TEXT, notes TEXT DEFAULT '', caption TEXT DEFAULT '',
  mode TEXT, genre TEXT, has_edits INTEGER DEFAULT 0, ops_hash TEXT DEFAULT '',
  burst_id INTEGER, scene_id INTEGER, burst_rank INTEGER,
  sidecar_mtime REAL DEFAULT 0, xmp_mtime REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_images_taken ON images(taken_at);
CREATE INDEX IF NOT EXISTS idx_images_dir ON images(dir);
CREATE INDEX IF NOT EXISTS idx_images_rating ON images(rating);
CREATE INDEX IF NOT EXISTS idx_images_camera ON images(camera);
CREATE INDEX IF NOT EXISTS idx_images_hash ON images(content_hash);
CREATE TABLE IF NOT EXISTS analysis(
  image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
  lap_var REAL, tenengrad REAL, sharp_face REAL, sharp_subject REAL, sharp_peak REAL, face_ratio REAL,
  motion_aniso REAL, blur_type TEXT, n_faces INTEGER, eyes_closed INTEGER, ear_min REAL,
  clip_hi REAL, clip_lo REAL, mean_lum REAL, median_lum REAL, dr_p1_p99 REAL, under INTEGER, over INTEGER,
  tilt_deg REAL, noise REAL, aesthetic REAL, phash TEXT, dhash TEXT, clip_emb BLOB,
  issues TEXT DEFAULT '', analyzed_v INTEGER, analyzed_at TEXT, src_mtime REAL, data TEXT);
CREATE TABLE IF NOT EXISTS tags(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, parent_id INTEGER, name TEXT, kind TEXT DEFAULT 'keyword');
CREATE TABLE IF NOT EXISTS image_tags(
  image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  source TEXT DEFAULT 'user', confidence REAL, confirmed INTEGER DEFAULT 1,
  PRIMARY KEY(image_id, tag_id));
CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags(tag_id);
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY, image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  bbox TEXT, emb BLOB, cluster_id INTEGER, person TEXT, ear REAL, sharp REAL, score REAL);
CREATE INDEX IF NOT EXISTS idx_faces_image ON faces(image_id);
CREATE TABLE IF NOT EXISTS collections(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT UNIQUE NOT NULL, parent_id INTEGER,
  kind TEXT NOT NULL DEFAULT 'static', query_json TEXT, cover_image_id INTEGER, sort TEXT DEFAULT 'taken_at',
  description TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS collection_images(
  collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE, pos INTEGER DEFAULT 0,
  PRIMARY KEY(collection_id, image_id));
CREATE TABLE IF NOT EXISTS edit_versions(
  id INTEGER PRIMARY KEY, image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE, name TEXT,
  ops_hash TEXT, n_ops INTEGER, mode TEXT, art TEXT, created_at TEXT, modified_at TEXT, is_default INTEGER);
CREATE TABLE IF NOT EXISTS thumbs(
  image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE, level INTEGER NOT NULL, path TEXT,
  ops_hash TEXT, PRIMARY KEY(image_id, level));
"""

FTS_COLS = ("filename", "tags", "notes", "caption", "place", "camera", "lens")
FTS_DDL = ("CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5("
           "filename, tags, notes, caption, place, camera, lens, tokenize='unicode61 remove_diacritics 2')")
FTS_FALLBACK_DDL = ("CREATE TABLE IF NOT EXISTS images_fts(rowid INTEGER PRIMARY KEY, filename, tags, notes, caption,"
                    " place, camera, lens)")

SORT_COLUMNS = {
    "taken_at": "i.taken_ts", "date": "i.taken_ts", "rating": "i.rating", "name": "i.stem COLLATE NOCASE",
    "filename": "i.stem COLLATE NOCASE", "added": "i.added_at", "imported": "i.imported_at", "iso": "i.iso",
    "focal": "i.focal", "aperture": "i.aperture", "camera": "i.camera", "id": "i.id", "path": "i.primary_path",
    "sharpness": "a.lap_var", "aesthetic": "a.aesthetic", "burst": "i.burst_id", "size": "i.file_size",
    "tags": "i.id",
}


class RuleError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------- helpers
def tag_prefix_sql(alias_it: str, alias_t: str, prefix: str, ci: bool = False) -> tuple[str, list]:
    """SQL fragment matching tag `prefix` itself and every descendant (`prefix|...`), no LIKE escaping needed."""
    p = prefix.strip("|")
    if ci:
        return (f"(lower({alias_t}.path) = lower(?) OR substr(lower({alias_t}.path), 1, ?) = lower(?))",
                [p, len(p) + 1, p + "|"])
    return f"({alias_t}.path = ? OR substr({alias_t}.path, 1, ?) = ?)", [p, len(p) + 1, p + "|"]


def _tag_exists(prefix: str, ci: bool = False, confirmed: bool | None = None) -> tuple[str, list]:
    frag, params = tag_prefix_sql("it", "t", prefix, ci)
    extra = "" if confirmed is None else f" AND it.confirmed = {1 if confirmed else 0}"
    return (f"EXISTS (SELECT 1 FROM image_tags it JOIN tags t ON t.id = it.tag_id "
            f"WHERE it.image_id = i.id AND {frag}{extra})", params)


def _parse_dt(v: Any) -> datetime:
    s = str(v).strip().replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    raise RuleError(f"bad date/time '{v}'")


def date_period(v: Any) -> tuple[str, str]:
    """'2026' / '2026-06' / '2026-06-01' / full timestamp -> [start, end) ISO strings."""
    s = str(v).strip().replace("/", "-")
    if re.fullmatch(r"\d{4}", s):
        y = int(s)
        return f"{y:04d}-01-01T00:00:00", f"{y + 1:04d}-01-01T00:00:00"
    if re.fullmatch(r"\d{4}-\d{1,2}", s):
        y, m = (int(x) for x in s.split("-"))
        y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
        return f"{y:04d}-{m:02d}-01T00:00:00", f"{y2:04d}-{m2:02d}-01T00:00:00"
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s):
        d = datetime.strptime(s, "%Y-%m-%d")
        return d.isoformat(), (d + timedelta(days=1)).isoformat()
    d = _parse_dt(s)
    return d.isoformat(), (d + timedelta(seconds=1)).isoformat()


def _num(v: Any) -> float:
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"1/\d+(\.\d+)?", s):  # shutter 1/250
            return 1.0 / float(s[2:])
        s = s.lower().rstrip("mm").lstrip("f/").rstrip("s")
        try:
            return float(s)
        except ValueError as exc:
            raise RuleError(f"bad number '{v}'") from exc
    return float(v)


_NUM_OPS = {"=": "=", "==": "=", "!=": "!=", "<>": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}

# field -> (kind, column). kind drives value handling in `_compile_condition`.
FIELDS: dict[str, tuple[str, str]] = {
    "rating": ("num", "i.rating"), "iso": ("num", "i.iso"), "focal": ("num", "i.focal"),
    "focal35": ("num", "i.focal35"), "aperture": ("num", "i.aperture"), "shutter": ("num", "i.shutter"),
    "width": ("num", "i.width"), "height": ("num", "i.height"), "burst": ("num", "i.burst_id"),
    "scene": ("num", "i.scene_id"), "sharpness": ("num", "a.lap_var"), "aesthetic": ("num", "a.aesthetic"),
    "faces": ("num", "a.n_faces"), "size": ("num", "i.file_size"), "id": ("num", "i.id"),
    "date": ("date", "i.taken_at"), "camera": ("text", "i.camera"), "lens": ("text", "i.lens"),
    "filename": ("text", "i.stem"), "folder": ("prefix", "i.dir"),
    "flag": ("flag", "i.flag"), "label": ("label", "i.label"), "tag": ("tag", ""), "person": ("person", ""),
    "place": ("place", ""), "text": ("text_search", ""), "mode": ("mode", ""), "has_edits": ("bool", "i.has_edits"),
    "album": ("album", ""), "issue": ("issue", ""), "kind": ("text", "i.kind"), "analyzed": ("analyzed", ""),
    "confirmed_tag": ("tag", ""),
}
ALIASES = {
    "stars": "rating", "taken": "date", "when": "date", "year": "date", "model": "camera", "fnumber": "aperture",
    "f": "aperture", "keyword": "tag", "keywords": "tag", "people": "person", "location": "place",
    "q": "text", "search": "text", "genre": "mode", "edited": "has_edits", "collection": "album", "dir": "folder",
    "name": "filename", "type": "kind", "pick": "flag", "blur": "sharpness", "color": "label", "colour": "label",
    "group": "scene",
}
ISSUES = {"blurry", "soft", "eyes_closed", "closed_eyes", "overexposed", "underexposed", "tilted", "noisy",
          "clipped", "motion_blur", "defocus", "duplicate"}


def canonical_field(name: str) -> str:
    n = name.strip().lower().replace("-", "_")
    n = ALIASES.get(n, n)
    if n not in FIELDS:
        raise RuleError(f"unknown field '{name}'. Known: {', '.join(sorted(set(FIELDS) | set(ALIASES)))}")
    return n


def _as_list(v: Any) -> list:
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def _compile_condition(c: dict, cat) -> tuple[str, list]:
    if "field" not in c:
        raise RuleError(f"condition needs a 'field': {c}")
    f = canonical_field(str(c["field"]))
    kind, col = FIELDS[f]
    op = str(c.get("op", "=")).strip().lower()
    if op in (":", "default", "is", "eq"):
        op = "="
    val = c.get("value")
    if f == "date" and str(c["field"]).lower() == "year" and val is not None and re.fullmatch(r"\d+", str(val)):
        val = str(val)

    # ---- numbers
    if kind == "num":
        if op in ("between", "range", "in") or (isinstance(val, (list, tuple)) and len(val) == 2):
            lo, hi = _as_list(val)[:2]
            parts, params = [], []
            if lo not in (None, ""):
                parts.append(f"{col} >= ?")
                params.append(_num(lo))
            if hi not in (None, ""):
                parts.append(f"{col} <= ?")
                params.append(_num(hi))
            return ("(" + " AND ".join(parts) + ")" if parts else "1=1"), params
        if op not in _NUM_OPS:
            raise RuleError(f"operator '{op}' is not valid for numeric field '{f}'")
        if val in (None, "") or str(val).lower() in ("none", "null"):
            return (f"{col} IS NULL" if _NUM_OPS[op] == "=" else f"{col} IS NOT NULL"), []
        return f"{col} {_NUM_OPS[op]} ?", [_num(val)]

    # ---- dates
    if kind == "date":
        if op in ("between", "range") or (isinstance(val, (list, tuple)) and len(val) == 2):
            lo, hi = _as_list(val)[:2]
            parts, params = [], []
            if lo not in (None, ""):
                parts.append(f"{col} >= ?")
                params.append(date_period(lo)[0])
            if hi not in (None, ""):
                parts.append(f"{col} < ?")
                params.append(date_period(hi)[1])
            return ("(" + " AND ".join(parts) + ")" if parts else "1=1"), params
        if val in (None, ""):
            raise RuleError("date condition needs a value")
        s, e = date_period(val)
        if op == "=":
            return f"({col} >= ? AND {col} < ?)", [s, e]
        if op == "!=":
            return f"NOT ({col} >= ? AND {col} < ?)", [s, e]
        if op == ">=":
            return f"{col} >= ?", [s]
        if op == ">":
            return f"{col} >= ?", [e]
        if op == "<":
            return f"{col} < ?", [s]
        if op == "<=":
            return f"{col} < ?", [e]
        raise RuleError(f"operator '{op}' is not valid for dates")

    # ---- plain text (case-insensitive substring unless '=')
    if kind == "text":
        vals = [str(v) for v in _as_list(val)]
        parts, params = [], []
        for v in vals:
            if op in ("=", "is"):
                parts.append(f"lower(coalesce({col}, '')) = lower(?)")
            elif op == "!=":
                parts.append(f"lower(coalesce({col}, '')) <> lower(?)")
            elif op in ("contains", "like"):
                parts.append(f"instr(lower(coalesce({col}, '')), lower(?)) > 0")
            elif op == "startswith":
                parts.append(f"substr(lower(coalesce({col}, '')), 1, ?) = lower(?)")
                params.append(len(v))
            else:
                raise RuleError(f"operator '{op}' is not valid for text field '{f}'")
            params.append(v)
        j = " AND " if op == "!=" else " OR "
        # '=' on camera/lens means "contains" for convenience (user types 'EOS R50' not 'Canon EOS R50')
        if f in ("camera", "lens", "filename") and op == "=":
            parts = [f"instr(lower(coalesce({col}, '')), lower(?)) > 0" for _ in vals]
            params = list(vals)
        return "(" + j.join(parts) + ")", params

    if kind == "prefix":
        v = str(val).strip("/")
        return "(i.dir = ? OR substr(i.dir, 1, ?) = ?)", [v, len(v) + 1, v + "/"]

    if kind == "flag":
        v = str(val).lower() if val is not None else "none"
        neg = op == "!="
        if v in ISSUES:  # `flag:closed_eyes` style analysis flags
            return _compile_condition({"field": "issue", "op": "!=" if neg else "=", "value": v}, cat)
        if v in ("none", "unflagged", "null", ""):
            sql = "i.flag IS NULL"
        elif v in ("flagged", "any"):
            sql = "i.flag IS NOT NULL"
        elif v in ("pick", "picked", "keep"):
            sql = "i.flag = 'pick'"
        elif v in ("reject", "rejected"):
            sql = "i.flag = 'reject'"
        else:
            raise RuleError(f"unknown flag '{val}' (pick|reject|none|flagged)")
        return (f"NOT ({sql})" if neg else sql), []

    if kind == "label":
        v = None if val is None else str(val)
        neg = op == "!="
        if v is None or v.lower() in ("none", ""):
            sql, p = "(i.label IS NULL OR i.label = '')", []
        else:
            sql, p = "lower(i.label) = lower(?)", [v]
        return (f"NOT {sql}" if neg else sql), p

    if kind in ("tag", "person", "place"):
        vals = [str(v) for v in _as_list(val)]
        parts, params = [], []
        for v in vals:
            if kind == "person":
                v = v if v.lower().startswith("people|") else f"people|{v}"
                s, p = _tag_exists(v, ci=True)
            elif kind == "place":
                if v.lower().startswith("places|"):
                    s, p = _tag_exists(v, ci=True)
                else:  # a bare place name matches any component under places|
                    s = ("EXISTS (SELECT 1 FROM image_tags it JOIN tags t ON t.id = it.tag_id WHERE it.image_id = i.id "
                         "AND substr(t.path, 1, 7) = 'places|' AND instr(lower(t.path), lower(?)) > 0)")
                    p = [v]
            else:
                s, p = _tag_exists(v, ci=True, confirmed=True if str(c["field"]).lower() == "confirmed_tag" else None)
            parts.append(s)
            params += p
        sql = "(" + " OR ".join(parts) + ")"
        return (f"NOT {sql}" if op == "!=" else sql), params

    if kind == "text_search":
        return cat._fts_condition(str(val))

    if kind == "mode":
        v = str(val).lower()
        sql = "(lower(coalesce(i.mode, '')) = ? OR lower(coalesce(i.genre, '')) = ?)"
        return (f"NOT {sql}" if op == "!=" else sql), [v, v]

    if kind == "bool":
        b = str(val).lower() not in ("0", "false", "no", "off", "none") if val is not None else True
        return f"{col} = {1 if b else 0}", []

    if kind == "album":
        out_sql = []
        for v in _as_list(val):
            aid = cat.resolve_album_id(v)
            if aid is None:
                raise RuleError(f"unknown album '{v}'")
            out_sql.append(cat.album_membership_sql(aid))
        sql = "(" + " OR ".join(s for s, _ in out_sql) + ")"
        params = [p for _, ps in out_sql for p in ps]
        return (f"NOT {sql}" if op == "!=" else sql), params

    if kind == "analyzed":
        b = str(val).lower() not in ("0", "false", "no", "off") if val is not None else True
        return ("a.image_id IS NOT NULL" if b else "a.image_id IS NULL"), []

    if kind == "issue":
        v = str(val).lower().replace("-", "_").replace(" ", "_")
        v = {"closed_eyes": "eyes_closed", "soft": "blurry"}.get(v, v)
        if v not in ISSUES:
            raise RuleError(f"unknown issue '{val}' ({', '.join(sorted(ISSUES))})")
        sql = "instr(',' || coalesce(a.issues, '') || ',', ?) > 0"
        return (f"NOT {sql}" if op == "!=" else sql), [f",{v},"]
    raise RuleError(f"unsupported field kind for '{f}'")


def compile_rule(rule: Any, cat) -> tuple[str, list]:
    """Compile a rule tree (dict / list of conditions) to a SQL boolean expression over alias `i` (+ `a`)."""
    if rule in (None, {}, []):
        return "1=1", []
    if isinstance(rule, list):
        rule = {"all": rule}
    if not isinstance(rule, dict):
        raise RuleError(f"rule must be a dict or list, got {type(rule).__name__}")
    if "field" in rule:
        return _compile_condition(rule, cat)
    if "conditions" in rule:  # {"match": "all|any|none", "conditions": [...]}
        rule = {str(rule.get("match", "all")): rule["conditions"]}
    if "__ids" in rule:
        ids = [int(x) for x in rule["__ids"]]
        return ("i.id IN (%s)" % ",".join("?" * len(ids)) if ids else "0=1"), ids
    if "__paths" in rule:
        rels = [cat.rel(p) for p in rule["__paths"]]
        if not rels:
            return "0=1", []
        q = ",".join("?" * len(rels))
        return f"(i.primary_path IN ({q}) OR i.raw_path IN ({q}) OR i.jpg_path IN ({q}))", rels * 3
    unknown = set(rule) - {"all", "any", "none", "match", "conditions", "name", "description"}
    if unknown:
        raise RuleError(f"unknown rule keys {sorted(unknown)}; expected all / any / none")
    parts, params = [], []
    for key in ("all", "any", "none"):
        items = rule.get(key)
        if not items:
            continue
        subs = [compile_rule(x, cat) for x in _as_list(items)]
        if key == "all":
            parts.append("(" + " AND ".join(s for s, _ in subs) + ")")
        elif key == "any":
            parts.append("(" + " OR ".join(s for s, _ in subs) + ")")
        else:
            parts.append("NOT COALESCE(" + " OR ".join(s for s, _ in subs) + ", 0)")   # NULL (unset column) must not drop rows
        for _, p in subs:
            params += p
    return (" AND ".join(parts) if parts else "1=1"), params


# ---------------------------------------------------------------------------------------------- filter dicts
def filter_to_rule(flt: Any) -> Any:
    """Convert a user-facing filter (dict of simple keys, rule tree, DSL string, None) to a rule tree."""
    if flt in (None, {}, ""):
        return {}
    if isinstance(flt, str):
        return parse_query(flt)
    if isinstance(flt, list):
        return {"all": flt}
    if not isinstance(flt, dict):
        raise RuleError(f"bad filter {flt!r}")
    if set(flt) & {"all", "any", "none", "conditions", "field"}:
        return flt
    conds: list[dict] = []

    def add(field, op, value):
        conds.append({"field": field, "op": op, "value": value})

    def rng(field, v):
        if isinstance(v, dict):
            lo, hi = v.get("min"), v.get("max")
        elif isinstance(v, (list, tuple)):
            lo, hi = v[0], v[1]
        else:
            add(field, ">=", v)
            return
        if lo not in (None, ""):
            add(field, ">=", lo)
        if hi not in (None, ""):
            add(field, "<=", hi)

    for k, v in flt.items():
        if v is None or v == "" or k in ("sort", "order", "limit", "offset"):
            continue
        if k == "rating":
            rng("rating", v)
        elif k == "rating_min":
            add("rating", ">=", v)
        elif k == "rating_max":
            add("rating", "<=", v)
        elif k == "rating_eq":
            add("rating", "=", v)
        elif k in ("flag", "label", "camera", "lens", "mode", "genre", "folder", "dir", "kind", "filename"):
            add(k, "=", v) if k not in ("folder", "dir", "filename") else add(k, "=", v)
        elif k == "tags":
            if isinstance(v, dict):
                for key, op in (("all", "all"), ("any", "any"), ("none", "none")):
                    t = v.get(key)
                    if t:
                        conds.append({op: [{"field": "tag", "op": "=", "value": x} for x in _as_list(t)]})
            else:
                for x in _as_list(v):  # a plain list means ALL of them
                    add("tag", "=", x)
        elif k in ("tags_all", "tag_all"):
            for x in _as_list(v):
                add("tag", "=", x)
        elif k in ("tags_any", "tag", "tag_any"):
            conds.append({"any": [{"field": "tag", "op": "=", "value": x} for x in _as_list(v)]})
        elif k in ("tags_none", "tag_none", "no_tags"):
            conds.append({"none": [{"field": "tag", "op": "=", "value": x} for x in _as_list(v)]})
        elif k in ("date", "taken"):
            rng("date", v) if isinstance(v, (dict, list, tuple)) else add("date", "=", v)
        elif k == "date_from":
            add("date", ">=", v)
        elif k == "date_to":
            add("date", "<=", v)
        elif k in ("focal", "iso", "aperture", "shutter"):
            rng(k, v)
        elif k in ("focal_min", "iso_min", "aperture_min", "shutter_min"):
            add(k.split("_")[0], ">=", v)
        elif k in ("focal_max", "iso_max", "aperture_max", "shutter_max"):
            add(k.split("_")[0], "<=", v)
        elif k in ("has_edits", "edited"):
            add("has_edits", "=", v)
        elif k in ("text", "q", "search"):
            add("text", "=", v)
        elif k in ("album", "collection"):
            add("album", "=", v)
        elif k in ("burst", "scene", "person", "place", "issue", "analyzed", "sharpness", "aesthetic"):
            add(k, "=", v) if k not in ("sharpness", "aesthetic") else rng(k, v)
        elif k == "ids":
            conds.append({"__ids": [int(x) for x in _as_list(v)]})
        elif k == "paths":
            conds.append({"__paths": [str(x) for x in _as_list(v)]})
        else:
            raise RuleError(f"unknown filter key '{k}'")
    return {"all": conds}


# ---------------------------------------------------------------------------------------------- DSL
_WORD_END = " \t\r\n()"


def _tokenize(text: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
        elif ch in "()":
            toks.append((ch, ch))
            i += 1
        else:
            j, buf = i, []
            while j < n and text[j] not in _WORD_END:
                c = text[j]
                if c in "\"'":
                    k = text.find(c, j + 1)
                    if k < 0:
                        raise RuleError(f"unterminated quote in query: {text!r}")
                    buf.append(text[j:k + 1])
                    j = k + 1
                elif c == "[":
                    k = text.find("]", j + 1)
                    if k < 0:
                        raise RuleError(f"unterminated [range] in query: {text!r}")
                    buf.append(text[j:k + 1])
                    j = k + 1
                else:
                    buf.append(c)
                    j += 1
            w = "".join(buf)
            up = w.upper()
            toks.append((up, w) if up in ("AND", "OR", "NOT", "EXCEPT") else ("WORD", w))
            i = j
    return toks


_COND_RE = re.compile(r"^(?P<neg>-?)(?P<field>[A-Za-z_]+)(?P<op>>=|<=|!=|<>|==|=|>|<|:)(?P<val>.*)$", re.S)


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _word_to_cond(word: str) -> dict:
    m = _COND_RE.match(word)
    if not m:
        return {"field": "text", "op": "=", "value": _unquote(word)}
    field = canonical_field(m["field"])
    op, val = m["op"], m["val"].strip()
    # `rating:>=4` / `iso:<=800`
    om = re.match(r"^(>=|<=|!=|>|<|=)(.*)$", val)
    if op == ":" and om:
        op, val = om[1], om[2].strip()
    rm = re.fullmatch(r"\[([^;\]]*);([^;\]]*)\]", val)
    if rm:
        cond = {"field": field, "op": "between", "value": [rm[1].strip() or None, rm[2].strip() or None]}
    else:
        v = _unquote(val)
        if field == "date" and m["field"].lower() == "year" and re.fullmatch(r"\d{4}", v):
            pass
        cond = {"field": field, "op": "=" if op in (":", "==") else ("!=" if op == "<>" else op), "value": v}
        if field == "tag" and "," in v and not val.startswith(("'", '"')):
            cond["value"] = [x.strip() for x in v.split(",") if x.strip()]
    if m["neg"]:
        return {"none": [cond]}
    return cond


def parse_query(text: str) -> dict:
    """Mini DSL -> rule tree.  expr := and (OR and)* ; and := unary ((AND|EXCEPT|<implicit>) unary)* ."""
    toks = _tokenize(text)
    pos = 0

    def peek():
        return toks[pos][0] if pos < len(toks) else None

    def parse_or():
        nonlocal pos
        items = [parse_and()]
        while peek() == "OR":
            pos += 1
            items.append(parse_and())
        return items[0] if len(items) == 1 else {"any": items}

    def parse_and():
        nonlocal pos
        items = [parse_unary()]
        while peek() in ("AND", "EXCEPT", "WORD", "(", "NOT"):
            if peek() in ("AND", "EXCEPT"):
                exc = peek() == "EXCEPT"
                pos += 1
                nxt = parse_unary()
                items.append({"none": [nxt]} if exc else nxt)
            else:  # implicit AND
                items.append(parse_unary())
        return items[0] if len(items) == 1 else {"all": items}

    def parse_unary():
        nonlocal pos
        k = peek()
        if k == "NOT":
            pos += 1
            return {"none": [parse_unary()]}
        if k == "(":
            pos += 1
            e = parse_or()
            if peek() != ")":
                raise RuleError(f"missing ')' in query: {text!r}")
            pos += 1
            return e
        if k == "WORD":
            w = toks[pos][1]
            pos += 1
            return _word_to_cond(w)
        raise RuleError(f"unexpected {'end of query' if k is None else repr(k)} in query: {text!r}")

    if not toks:
        return {}
    tree = parse_or()
    if pos != len(toks):
        raise RuleError(f"unexpected {toks[pos][1]!r} in query: {text!r}")
    return tree
