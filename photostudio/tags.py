"""Tags: one hierarchical tree (`a|b|c`), tree operations, tiered auto-tagging, people, places and events.

Storage: sidecar `tags` (confirmed) + `auto_tags` (provenance), XMP `dc:subject` (leaf names) +
`lr:hierarchicalSubject` (full paths); unconfirmed auto tags stay in the sidecar/DB only, so XMP does not fill with
noise. Auto-tag tiers (all optional except the first, which is pure metadata):

  1. metadata-derived (always available): camera, lens, focal class, time of day, season, year/month, ISO class,
     orientation, genre (`modes.detect_mode` if present, else a conservative EXIF heuristic)
  2. CLIP zero-shot against `data/tag_vocab.yaml` (needs open_clip + weights; reuses the stored embedding)
  3. RAM++ tags and Florence-2 captions (optional, keepers only)
  4. people: InsightFace embeddings clustered with a pure scipy DBSCAN, named once by the user
  5. places: offline reverse_geocoder -> `places|CC|admin1|city`
  6. events: time-gap splitting (max(2 h, 10x the day's median gap)), merged by GPS/6 h
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .catalog import doc_assignments, norm_tag, store_assignments
from .util import lazy_import, package_dir

# ============================================================================================== tree ops
def split_tag(path: str) -> list[str]:
    return norm_tag(path).split("|") if norm_tag(path) else []


def flat_keywords(paths: list[str]) -> list[str]:
    return sorted({split_tag(p)[-1] for p in paths if split_tag(p)})


def _strength(a: dict) -> tuple:
    return (a.get("confirmed", 0), a.get("source") == "user", a.get("confidence") or 0.0)


def _remap(cat, old: str, fn: Callable[[str], str]) -> dict:
    """Rewrite every tag under `old` (itself + descendants) with `fn`, in sidecars, XMP and the DB."""
    old = norm_tag(old)
    rows = cat.conn.execute("SELECT path FROM tags WHERE path=? OR substr(path,1,?)=?", (old, len(old) + 1, old + "|")).fetchall()
    if not rows:
        raise KeyError(f"no such tag: {old}")
    affected = [r[0] for r in rows]
    ids = [r[0] for r in cat.conn.execute(
        "SELECT DISTINCT it.image_id FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE t.path=? OR substr(t.path,1,?)=?",
        (old, len(old) + 1, old + "|"))]
    with cat._lock:
        for i in ids:
            cur = cat.get_assignments(i)
            new: dict[str, dict] = {}
            for p, a in cur.items():
                np_ = fn(p) if (p == old or p.startswith(old + "|")) else p
                if np_ not in new or _strength(a) > _strength(new[np_]):
                    new[np_] = a
            cat.set_tags(i, new)
        with cat.conn:
            for p in affected:  # tags without images keep their place in the tree too
                cat._ensure_tag(fn(p))
            for p in sorted(affected, key=len, reverse=True):
                if fn(p) != p:
                    cat.conn.execute("DELETE FROM tags WHERE path=?", (p,))
    return {"tags": len(affected), "images": len(ids)}


def rename_tag(cat, old: str, new_name: str, merge: bool = False) -> dict:
    """Rename the last component of `old` (a|b|c -> a|b|new). Refuses to overwrite an existing tag unless merge."""
    parts = split_tag(old)
    if "|" in new_name:
        raise ValueError("new name must be a single component; use move_tag to change the parent")
    new = "|".join(parts[:-1] + [new_name.strip()])
    return _relocate(cat, old, new, merge)


def move_tag(cat, src: str, new_parent: str | None, merge: bool = False) -> dict:
    """Move a tag (and its subtree) under a new parent ('' or None = top level)."""
    leaf = split_tag(src)[-1]
    new = "|".join(split_tag(new_parent or "") + [leaf])
    if new == norm_tag(src) or new.startswith(norm_tag(src) + "|"):
        raise ValueError("cannot move a tag into itself")
    return _relocate(cat, src, new, merge)


def merge_tags(cat, src: str, dst: str) -> dict:
    """Merge `src` (and its descendants) into `dst`: images tagged src become tagged dst, children re-parent."""
    return _relocate(cat, src, norm_tag(dst), True)


def _relocate(cat, old: str, new: str, merge: bool) -> dict:
    old, new = norm_tag(old), norm_tag(new)
    if old == new:
        return {"tags": 0, "images": 0}
    if cat.conn.execute("SELECT 1 FROM tags WHERE path=?", (new,)).fetchone() and not merge:
        raise ValueError(f"tag '{new}' already exists; use merge to combine them")
    return _remap(cat, old, lambda p: new + p[len(old):])


def delete_tag(cat, tag: str, recursive: bool = True) -> dict:
    tag = norm_tag(tag)
    rows = cat.conn.execute("SELECT id FROM tags WHERE path=? OR substr(path,1,?)=?", (tag, len(tag) + 1, tag + "|")).fetchall()
    if not rows:
        raise KeyError(f"no such tag: {tag}")
    if not recursive and len(rows) > 1:
        raise ValueError(f"tag '{tag}' has children; pass recursive=True")
    res = cat.remove_tags([r["id"] for r in cat.images({"tag": tag}, with_tags=False)], [tag], recursive=True)
    with cat.conn:
        cat.conn.execute("DELETE FROM tags WHERE path=? OR substr(path,1,?)=?", (tag, len(tag) + 1, tag + "|"))
    return {"tags": len(rows), **res}


def create_tag(cat, path: str) -> int:
    path = norm_tag(path)
    if not path:
        raise ValueError("empty tag")
    with cat._lock, cat.conn:
        return cat._ensure_tag(path)


def tree_text(cat, prefix: str | None = None, counts: bool = False) -> str:
    """Tag tree as indented text (tab per level; digiKam / controlled-vocabulary friendly)."""
    lines = []
    for t in cat.list_tags(prefix):
        depth = t["path"].count("|")
        lines.append("\t" * depth + t["name"] + (f"  ({t['total']})" if counts else ""))
    return "\n".join(lines) + ("\n" if lines else "")


def import_tree_text(cat, text: str) -> int:
    """Create (empty) tags from tab- or 2-space-indented text; returns how many tags exist afterwards from it."""
    stack: list[str] = []
    n = 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        ind = len(raw) - len(raw.lstrip("\t "))
        depth = raw[:ind].count("\t") + raw[:ind].count(" ") // 2
        name = raw.strip().split("  (")[0].strip()
        stack = stack[:depth] + [name]
        create_tag(cat, "|".join(stack))
        n += 1
    return n


def push_tags(cat, ids: list[int] | None = None) -> int:
    """Re-emit XMP / companion-JPEG metadata from the sidecars (DB -> files)."""
    return cat.rewrite_sidecars(ids)


def pull_tags(cat, ids: list[int] | None = None) -> dict:
    """Read tags / rating / label back from standard XMP (files -> sidecar + DB), e.g. after editing in digiKam."""
    from .metadata import read_xmp

    n = changed = 0
    for i in (ids if ids is not None else cat.ids()):
        r = cat.conn.execute("SELECT primary_path FROM images WHERE id=?", (i,)).fetchone()
        xp = cat.root / (r[0] + ".xmp")
        x = read_xmp(xp)
        if not x:
            continue
        n += 1
        tl = x.get("hierarchical") or x.get("keywords")
        if tl is not None:
            cur = cat.get_assignments(i)
            new = {p: a for p, a in cur.items() if a["source"] != "user" and not a["confirmed"]}
            for t in tl:
                new[norm_tag(t)] = {"source": "user", "confidence": None, "confirmed": 1}
            changed += int(cat.set_tags(i, new))
        if isinstance(x.get("rating"), int) and x["rating"] >= 0:
            cat.set_rating(i, x["rating"])
        if x.get("label"):
            cat.set_label(i, x["label"])
    return {"read": n, "changed": changed}


# ============================================================================================== replace auto tags
def replace_auto(cat, image_id: int, source: str, new: dict[str, tuple[float | None, bool]]) -> bool:
    """Make the set of `source` tags on an image equal to `new` {path: (confidence, confirmed)}; user tags and
    other sources untouched. No file is written when nothing changes."""
    def m(doc):
        assign = doc_assignments(doc)
        for p in [p for p, a in assign.items() if a["source"] == source]:
            del assign[p]
        for p, (conf, confirmed) in new.items():
            p = norm_tag(p)
            if p and p not in assign:  # never override a user/other-source assignment
                assign[p] = {"source": source, "confidence": conf, "confirmed": 1 if confirmed else 0}
        store_assignments(doc, assign)
    return cat._commit(image_id, m)


# ============================================================================================== tier 1: metadata
TIME_OF_DAY = [(5, "night"), (7, "dawn"), (11, "morning"), (15, "midday"), (18, "afternoon"), (21, "dusk"), (24, "night")]
CROP_FACTORS = {"r50": 1.6, "r10": 1.6, "r7": 1.6, "r100": 1.6, "m50": 1.6, "m6": 1.6, "m200": 1.6, "7d": 1.6, "80d": 1.6,
                "90d": 1.6, "rebel": 1.6, "xt": 1.5, "x-t": 1.5, "a6": 1.5, "z50": 1.5, "d7": 1.5, "d5": 1.5}


def focal35_of(row: dict) -> float | None:
    if row.get("focal35"):
        return float(row["focal35"])
    f = row.get("focal")
    if not f:
        return None
    cam = (row.get("camera") or "").lower()
    crop = next((v for k, v in CROP_FACTORS.items() if k in cam.replace(" ", "")), 1.0)
    return float(f) * crop


def heuristic_genre(row: dict, md: dict | None = None) -> tuple[str | None, float]:
    """Conservative EXIF-only genre guess (used when modes.detect_mode is unavailable). Returns (genre, confidence)."""
    md = md or {}
    f35 = focal35_of(row) or 0
    ap = row.get("aperture") or 0
    sh = row.get("shutter") or 0
    iso = row.get("iso") or 0
    hour = int((row.get("taken_at") or "T12")[11:13] or 12) if row.get("taken_at") else 12
    night = hour >= 21 or hour < 5
    if md.get("macro_mode") in (1, 2):
        return "macro", 0.6
    if night and sh >= 10 and iso >= 800:
        return "astro", 0.5
    if night and (iso >= 1600 or sh >= 1.0):
        return "night", 0.4
    if f35 >= 200 and sh and sh <= 1 / 800:
        return "wildlife", 0.35
    if sh and sh <= 1 / 1600 and f35 >= 100:
        return "sports", 0.3
    if f35 and 70 <= f35 <= 150 and ap and ap <= 2.8:
        return "portrait", 0.35
    if f35 and f35 <= 36 and ap and ap >= 7.1:
        return "landscape", 0.35
    return None, 0.0


def metadata_tags(row: dict, md: dict | None = None, use_modes: bool = True) -> dict[str, float]:
    """Deterministic facts from EXIF/DB columns -> {tag: confidence}."""
    t: dict[str, float] = {}
    if row.get("camera"):
        t[f"camera|{row['camera'].replace('|', '/')}"] = 1.0
    if row.get("lens"):
        t[f"lens|{str(row['lens']).replace('|', '/')}"] = 1.0
    f35 = focal35_of(row)
    if f35:
        t["focal|" + ("wide" if f35 <= 35 else "normal" if f35 <= 85 else "tele")] = 1.0
    iso = row.get("iso")
    if iso:
        t["iso|" + ("low" if iso <= 400 else "medium" if iso <= 1600 else "high" if iso <= 6400 else "very-high")] = 1.0
    w, h = row.get("width"), row.get("height")
    if w and h:
        if row.get("orientation") in (5, 6, 7, 8):
            w, h = h, w
        ratio = w / h
        t["orientation|" + ("square" if 0.97 <= ratio <= 1.03 else "landscape" if ratio > 1 else "portrait")] = 1.0
    ta = row.get("taken_at")
    if ta:
        try:
            d = datetime.fromisoformat(ta)
            t[f"date|{d.year}"] = 1.0
            t[f"date|{d.year}|{d.month:02d}"] = 1.0
            hour = d.hour
            t["time|" + next(n for lim, n in TIME_OF_DAY if hour < lim)] = 1.0
            south = (row.get("gps_lat") or 0) < 0
            seasons = ["winter", "spring", "summer", "autumn"]
            idx = {12: 0, 1: 0, 2: 0, 3: 1, 4: 1, 5: 1, 6: 2, 7: 2, 8: 2, 9: 3, 10: 3, 11: 3}[d.month]
            t["season|" + seasons[(idx + (2 if south else 0)) % 4]] = 1.0
        except ValueError:
            pass
    g, conf = None, 0.0
    if use_modes and md:
        try:
            from .modes import detect_mode

            r = detect_mode(md)
            g, conf = r.get("mode"), float(r.get("confidence") or 0.5)
        except Exception:
            g = None
    if g is None:
        g, conf = heuristic_genre(row, md)
    if g:
        t[f"genre|{g}"] = conf
    return t


def auto_tag_metadata(cat, ids: list[int] | None = None, use_modes: bool = True,
                      progress: Callable[[int, int], None] | None = None) -> dict:
    """Tier 1. Replaces all `auto:meta` tags on the images and fills the catalog `genre` column."""
    from .metadata import read_metadata

    ids = ids if ids is not None else cat.ids()
    genres, written = {}, 0
    for k, i in enumerate(ids):
        row = cat.get(i)
        md = None
        if use_modes:
            try:
                md = read_metadata(row["raw"] or row["path"])
            except Exception:
                md = None
        tags = metadata_tags(row, md, use_modes)
        for p, c in tags.items():
            if p.startswith("genre|"):
                genres[i] = p.split("|", 1)[1]
        written += int(replace_auto(cat, i, "auto:meta", {p: (c, False) for p, c in tags.items()}))
        if progress:
            progress(k + 1, len(ids))
    cat.set_genre(genres)
    return {"images": len(ids), "changed": written, "genres": len(genres)}


# ============================================================================================== tier 2: CLIP zero-shot
def load_vocab(path: str | Path | None = None) -> dict:
    import os

    import yaml

    p = Path(path or os.environ.get("PHOTOSTUDIO_TAG_VOCAB") or package_dir() / "data" / "tag_vocab.yaml")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def vocab_terms(vocab: dict) -> list[tuple[str, list[str], str]]:
    out = []
    tmpl = vocab.get("template", "a photo of {}")
    for g, terms in vocab["groups"].items():
        for tag, phrases in terms.items():
            ps = phrases if isinstance(phrases, list) else [phrases]
            out.append((norm_tag(tag), [tmpl.format(p) for p in ps], g))
    return out


def text_embeddings(vocab: dict, clip=None) -> tuple[list[str], np.ndarray, list[str]]:
    """Averaged, L2-normalised text embedding per tag (cached on disk keyed by vocabulary hash)."""
    from .util import cache_home, params_hash

    terms = vocab_terms(vocab)
    key = params_hash([[t, p] for t, p, _ in terms])
    cp = cache_home() / f"tagvocab_{key}.npy"
    tags, groups = [t for t, _, _ in terms], [g for _, _, g in terms]
    if cp.exists():
        return tags, np.load(cp), groups
    if clip is None:
        from . import library_ai

        clip = library_ai.clip_model()
    mats = []
    for _, phrases, _ in terms:
        e = clip.encode_text(phrases).mean(0)
        mats.append(e / max(float(np.linalg.norm(e)), 1e-9))
    m = np.stack(mats).astype(np.float32)
    np.save(cp, m)
    return tags, m, groups


def zero_shot(img_emb: np.ndarray, tags: list[str], text_emb: np.ndarray, groups: list[str], *, exclusive=(),
              logit_scale: float = 100.0, min_prob: float = 0.10, max_tags: int = 6) -> list[tuple[str, float]]:
    """CLIP zero-shot: softmax over all vocabulary terms (logit scale 100); keep terms with probability >=
    min_prob (confidence = that probability, i.e. the margin over every other label), at most one tag per
    exclusive group, at most `max_tags` overall. Pure numpy so it is testable without a model."""
    e = np.asarray(img_emb, np.float32)
    e = e / max(float(np.linalg.norm(e)), 1e-9)
    logits = logit_scale * (text_emb @ e)
    p = np.exp(logits - logits.max())
    p /= p.sum()
    out, used = [], set()
    for k in np.argsort(-p):
        if p[k] < min_prob or len(out) >= max_tags:
            break
        g = groups[k]
        if g in exclusive and g in used:
            continue
        used.add(g)
        out.append((tags[k], float(p[k])))
    return out


def auto_tag_clip(cat, ids: list[int] | None = None, vocab_path: str | None = None, min_prob: float | None = None,
                  confirm_above: float | None = None, progress=None, batch: int = 8) -> dict:
    """Tier 2 (optional): tag from the stored CLIP embedding (computed on the preview if missing)."""
    from . import library_ai
    from .decode import preview_display

    clip = library_ai.clip_model()
    vocab = load_vocab(vocab_path)
    tags, tm, groups = text_embeddings(vocab, clip)
    ids = ids if ids is not None else cat.ids()
    have = cat.get_embeddings(ids)
    todo = [i for i in ids if i not in have]
    for s in range(0, len(todo), batch):
        chunk = todo[s:s + batch]
        imgs = [preview_display(cat.get(i)["path"], 512) for i in chunk]
        for i, e in zip(chunk, clip.encode_images(imgs)):
            cat.set_embedding(i, e)
            have[i] = e.astype(np.float16).astype(np.float32)
    changed = 0
    for k, i in enumerate(ids):
        res = zero_shot(have[i], tags, tm, groups, exclusive=set(vocab.get("exclusive", [])),
                        min_prob=min_prob if min_prob is not None else float(vocab.get("min_prob", 0.1)),
                        max_tags=int(vocab.get("max_tags", 6)))
        new = {t: (p, confirm_above is not None and p >= confirm_above) for t, p in res}
        changed += int(replace_auto(cat, i, "auto:clip", new))
        if progress:
            progress(k + 1, len(ids))
    return {"images": len(ids), "embedded": len(todo), "changed": changed}


# ============================================================================================== tier 3: RAM++ / captions
def auto_tag_ram(cat, ids: list[int] | None = None, min_rating: int = 0, confirm_above: float | None = None,
                 progress=None) -> dict:
    from . import library_ai
    from .decode import preview_display

    ids = ids if ids is not None else cat.ids({"rating_min": min_rating} if min_rating else None)
    changed = 0
    for k, i in enumerate(ids):
        tl = library_ai.ram_tags(preview_display(cat.get(i)["path"], 512))
        new = {f"ram|{t.replace('|', '/')}": (s, confirm_above is not None and s >= confirm_above) for t, s in tl}
        changed += int(replace_auto(cat, i, "auto:ram", new))
        if progress:
            progress(k + 1, len(ids))
    return {"images": len(ids), "changed": changed}


def caption_images(cat, ids: list[int] | None = None, min_rating: int = 4, overwrite: bool = False, progress=None) -> dict:
    """Florence-2 `<DETAILED_CAPTION>` for good images (default 4-5 stars); stored in the sidecar and FTS."""
    from . import library_ai
    from .decode import preview_display

    ids = ids if ids is not None else cat.ids({"rating_min": min_rating})
    done = 0
    for k, i in enumerate(ids):
        r = cat.get(i)
        if r["caption"] and not overwrite:
            continue
        cap = library_ai.florence_caption(preview_display(r["path"], 768))
        cat.set_caption(i, cap)
        done += 1
        if progress:
            progress(k + 1, len(ids))
    return {"images": len(ids), "captioned": done}


# ============================================================================================== people
def dbscan(emb: np.ndarray, eps: float = 0.65, min_samples: int = 3) -> np.ndarray:
    """DBSCAN on unit vectors with Euclidean eps (0.6-0.7 is the usual range for 512-d face embeddings; tune on
    your own faces). Pure scipy (cKDTree), no scikit-learn. Returns labels, -1 = noise."""
    from scipy.spatial import cKDTree

    n = len(emb)
    if n == 0:
        return np.zeros(0, int)
    tree = cKDTree(emb)
    nbrs = tree.query_ball_point(emb, eps)
    labels = np.full(n, -1, int)
    core = np.array([len(x) >= min_samples for x in nbrs])
    cid = 0
    for i in range(n):
        if labels[i] != -1 or not core[i]:
            continue
        labels[i] = cid
        stack = [i]
        while stack:
            j = stack.pop()
            if not core[j]:
                continue
            for k in nbrs[j]:
                if labels[k] == -1:
                    labels[k] = cid
                    stack.append(k)
        cid += 1
    return labels


def detect_faces_catalog(cat, ids: list[int] | None = None, force: bool = False, progress=None) -> dict:
    """InsightFace detection + embeddings for images (lazy; MissingDependency if not installed). Images with no
    face get a placeholder row so they are not re-processed."""
    from . import library_ai
    from .decode import preview_display

    ids = ids if ids is not None else cat.ids()
    n_faces = done = 0
    for k, i in enumerate(ids):
        have = cat.conn.execute("SELECT COUNT(*) FROM faces WHERE image_id=?", (i,)).fetchone()[0]
        if have and not force:
            continue
        faces = library_ai.insightface_faces(preview_display(cat.get(i)["path"], 1600))
        with cat._lock, cat.conn:
            cat.conn.execute("DELETE FROM faces WHERE image_id=?", (i,))
            if not faces:
                cat.conn.execute("INSERT INTO faces(image_id, bbox, emb, score) VALUES(?, '[]', NULL, -1)", (i,))
            for f in faces:
                cat.conn.execute("INSERT INTO faces(image_id, bbox, emb, score) VALUES(?,?,?,?)",
                                 (i, json.dumps(f["bbox"]), np.asarray(f["emb"], np.float16).tobytes(), f["score"]))
        n_faces += len(faces)
        done += 1
        if progress:
            progress(k + 1, len(ids))
    return {"images": done, "faces": n_faces}


def _load_faces(cat) -> tuple[list[dict], np.ndarray]:
    rows = [dict(r) for r in cat.conn.execute("SELECT id, image_id, emb, cluster_id, person FROM faces WHERE emb IS NOT NULL")]
    emb = np.stack([np.frombuffer(r["emb"], np.float16).astype(np.float32) for r in rows]) if rows else np.zeros((0, 512), np.float32)
    if len(emb):
        emb /= np.maximum(np.linalg.norm(emb, axis=1, keepdims=True), 1e-9)
    return rows, emb


def cluster_faces(cat, eps: float = 0.65, min_samples: int = 3, samples: int = 4) -> dict:
    """Cluster all stored face embeddings. Clusters containing already-named faces adopt that name (the naming
    workflow therefore only needs doing once per person). Returns clusters with sample image ids to show the user."""
    rows, emb = _load_faces(cat)
    labels = dbscan(emb, eps, min_samples)
    out = []
    with cat._lock, cat.conn:
        for r, lab in zip(rows, labels):
            cat.conn.execute("UPDATE faces SET cluster_id=? WHERE id=?", (int(lab) if lab >= 0 else None, r["id"]))
        for c in sorted(set(int(x) for x in labels if x >= 0)):
            idx = [k for k, l in enumerate(labels) if l == c]
            names = [rows[k]["person"] for k in idx if rows[k]["person"]]
            person = max(set(names), key=names.count) if names else None
            if person:
                for k in idx:
                    if not rows[k]["person"]:
                        cat.conn.execute("UPDATE faces SET person=? WHERE id=?", (person, rows[k]["id"]))
            out.append({"cluster": c, "faces": len(idx), "images": len({rows[k]["image_id"] for k in idx}),
                        "person": person, "sample_images": sorted({rows[k]["image_id"] for k in idx})[:samples]})
    return {"faces": len(rows), "clusters": out, "noise": int((labels < 0).sum())}


def name_cluster(cat, cluster_id: int, name: str, confirmed: bool = True) -> dict:
    """Name a face cluster: tags every image that contains one of its faces with `people|<name>`."""
    name = name.strip()
    if not name or "|" in name:
        raise ValueError("person name must be non-empty and contain no '|'")
    tag = f"people|{name}"
    rows = cat.conn.execute("SELECT id, image_id FROM faces WHERE cluster_id=?", (cluster_id,)).fetchall()
    if not rows:
        raise KeyError(f"no faces in cluster {cluster_id}")
    with cat._lock, cat.conn:
        cat.conn.execute("UPDATE faces SET person=? WHERE cluster_id=?", (tag, cluster_id))
    ids = sorted({r["image_id"] for r in rows})
    for i in ids:
        cat.add_tags(i, [tag], source="auto:face", confidence=1.0, confirmed=confirmed)
    return {"person": tag, "faces": len(rows), "images": len(ids)}


def match_faces(cat, match: float = 0.55) -> dict:
    """Assign still-unnamed faces to the nearest named person centroid within `match` (Euclid on unit vectors)."""
    rows, emb = _load_faces(cat)
    named: dict[str, list[int]] = {}
    for k, r in enumerate(rows):
        if r["person"]:
            named.setdefault(r["person"], []).append(k)
    if not named:
        return {"assigned": 0}
    cents = {p: (lambda v: v / max(float(np.linalg.norm(v)), 1e-9))(emb[ix].mean(0)) for p, ix in named.items()}
    assigned = 0
    by_image: dict[int, set[str]] = {}
    with cat._lock, cat.conn:
        for k, r in enumerate(rows):
            if r["person"]:
                continue
            best = min(cents, key=lambda p: float(np.linalg.norm(emb[k] - cents[p])))
            if float(np.linalg.norm(emb[k] - cents[best])) <= match:
                cat.conn.execute("UPDATE faces SET person=? WHERE id=?", (best, r["id"]))
                by_image.setdefault(r["image_id"], set()).add(best)
                assigned += 1
    for i, ps in by_image.items():
        cat.add_tags(i, sorted(ps), source="auto:face", confidence=0.8, confirmed=False)
    return {"assigned": assigned, "images": len(by_image)}


def people_summary(cat) -> list[dict]:
    return [dict(r) for r in cat.conn.execute(
        "SELECT person, COUNT(*) AS faces, COUNT(DISTINCT image_id) AS images FROM faces WHERE person IS NOT NULL "
        "GROUP BY person ORDER BY images DESC")]


# ============================================================================================== places
def reverse_geocode(points: list[tuple[float, float]], cache: dict | None = None) -> list[dict]:
    """[(lat, lon)] -> [{cc, admin1, name}] with offline reverse_geocoder (GeoNames cities1000, 2016 data); results
    are cached per coordinate rounded to 3 decimals (~100 m)."""
    cache = cache if cache is not None else {}
    keys = [(round(a, 3), round(b, 3)) for a, b in points]
    todo = sorted({k for k in keys if k not in cache})
    if todo:
        import contextlib
        import sys

        rg = lazy_import("reverse_geocoder", "place tags")
        with contextlib.redirect_stdout(sys.stderr):  # the package prints a loading banner
            found = rg.search(todo, mode=1)
        for k, r in zip(todo, found):
            cache[k] = {"cc": r.get("cc", ""), "admin1": r.get("admin1", ""), "name": r.get("name", "")}
    return [cache[k] for k in keys]


def _clean(s: str) -> str:
    return str(s).replace("|", "/").strip()


def place_tags(cat, ids: list[int] | None = None, progress=None) -> dict:
    """Tag GPS-tagged images `places|CC|admin1|city` (source auto:geo, confirmed: factual, fills IPTC location)."""
    ids = ids if ids is not None else cat.ids()
    rows = [cat.conn.execute("SELECT id, gps_lat, gps_lon FROM images WHERE id=?", (i,)).fetchone() for i in ids]
    rows = [r for r in rows if r and r["gps_lat"] is not None and r["gps_lon"] is not None]
    if not rows:
        return {"images": 0, "tagged": 0, "without_gps": len(ids)}
    geo = reverse_geocode([(r["gps_lat"], r["gps_lon"]) for r in rows])
    tagged = 0
    for k, (r, g) in enumerate(zip(rows, geo)):
        parts = [_clean(x) for x in (g["cc"], g["admin1"], g["name"]) if _clean(x)]
        new = {"|".join(["places", *parts]): (1.0, True)} if parts else {}
        tagged += int(replace_auto(cat, r["id"], "auto:geo", new))
        if progress:
            progress(k + 1, len(rows))
    return {"images": len(rows), "changed": tagged, "without_gps": len(ids) - len(rows)}


# ============================================================================================== events
def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def detect_events(items: list[dict], gap_hours: float | None = None, min_gap_hours: float = 2.0, k: float = 10.0,
                  merge_km: float = 1.0, merge_hours: float = 6.0, split_km: float = 25.0, split_min: float = 15.0) -> list[dict]:
    """items: [{id, ts, lat?, lon?, place?}]. Split where the gap exceeds max(min_gap_hours, k x the median gap of
    that day) (or a fixed `gap_hours`); then merge neighbouring events that share a GPS cluster (< merge_km) and are
    < merge_hours apart. A geotagged jump of more than `split_km` after a pause of `split_min` minutes also starts a new
    event (a different city is a different event even 80 minutes later). A heuristic (no sourced thresholds): the simpler
    3 h rule is `gap_hours=3`."""
    its = sorted((x for x in items if x.get("ts") is not None), key=lambda x: x["ts"])
    if not its:
        return []
    gaps = [its[i + 1]["ts"] - its[i]["ts"] for i in range(len(its) - 1)]
    day_of = [datetime.fromtimestamp(its[i]["ts"]).date() for i in range(len(its) - 1)]
    by_day: dict[Any, list[float]] = {}
    for g, d in zip(gaps, day_of):
        by_day.setdefault(d, []).append(g)
    gmed = statistics.median(gaps) if gaps else 0.0
    events: list[list[dict]] = [[its[0]]]
    for i, g in enumerate(gaps):
        if gap_hours is not None:
            thr = gap_hours * 3600.0
        else:
            dg = by_day[day_of[i]]
            med = statistics.median(dg) if len(dg) >= 3 else gmed
            thr = max(min_gap_hours * 3600.0, k * med)
        a, b = its[i], its[i + 1]
        jumped = (gap_hours is None and g > split_min * 60.0 and None not in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon"))
                  and haversine_km((a["lat"], a["lon"]), (b["lat"], b["lon"])) > split_km)
        if g > thr or jumped:
            events.append([])
        events[-1].append(its[i + 1])

    def center(ev):
        pts = [(x["lat"], x["lon"]) for x in ev if x.get("lat") is not None and x.get("lon") is not None]
        return (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts)) if pts else None

    if gap_hours is None:
        merged = [events[0]]
        for ev in events[1:]:
            prev = merged[-1]
            ca, cb = center(prev), center(ev)
            if ca and cb and haversine_km(ca, cb) < merge_km and ev[0]["ts"] - prev[-1]["ts"] < merge_hours * 3600:
                prev.extend(ev)
            else:
                merged.append(ev)
        events = merged
    out = []
    for n, ev in enumerate(events, start=1):
        places = [x["place"] for x in ev if x.get("place")]
        place = max(set(places), key=places.count) if places else None
        start = datetime.fromtimestamp(ev[0]["ts"])
        name = start.strftime("%Y-%m-%d") + (f"_{place.replace(' ', '-')}" if place else "")
        out.append({"id": n, "start": start.isoformat(timespec="seconds"),
                    "end": datetime.fromtimestamp(ev[-1]["ts"]).isoformat(timespec="seconds"), "n": len(ev),
                    "image_ids": [x["id"] for x in ev], "center": center(ev), "place": place, "name": name,
                    "duration_min": round((ev[-1]["ts"] - ev[0]["ts"]) / 60.0, 1)})
    return out


def suggest_events(cat, ids: list[int] | None = None, **kw) -> list[dict]:
    ids = ids if ids is not None else cat.ids()
    items = []
    for i in ids:
        r = cat.conn.execute("SELECT taken_ts, gps_lat, gps_lon FROM images WHERE id=?", (i,)).fetchone()
        place = cat.conn.execute(
            "SELECT t.path FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id=? AND substr(t.path,1,7)='places|' "
            "ORDER BY length(t.path) DESC LIMIT 1", (i,)).fetchone()
        items.append({"id": i, "ts": r["taken_ts"], "lat": r["gps_lat"], "lon": r["gps_lon"],
                      "place": place[0].split("|")[-1] if place else None})
    return detect_events(items, **kw)


def apply_events(cat, events: list[dict], names: dict[int, str] | None = None, albums: bool = True,
                 tags: bool = True) -> dict:
    """Create `events|<name>` tags (source auto:event, confirmed) and static albums `events/<name>`."""
    made_albums = 0
    used: set[str] = set()
    for ev in events:
        name = (names or {}).get(ev["id"]) or ev["name"]
        base, n = name, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        ev["applied_name"] = name
        if tags:
            cat.add_tags(ev["image_ids"], [f"events|{name}"], source="auto:event", confidence=1.0, confirmed=True)
        if albums:
            cat.albums.create(f"events/{name}", images=ev["image_ids"], exist_ok=True)
            made_albums += 1
    return {"events": len(events), "albums": made_albums}
