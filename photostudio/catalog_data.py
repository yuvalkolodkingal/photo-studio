"""Catalog mixin: cached analysis, CLIP embeddings, burst/scene groups, content hashes, import tracking and the
thumbnail cache. Split from catalog.py to keep modules small; `Catalog` inherits this class and provides `conn`,
`_lock`, `root`, `thumb_dir`, `_row`, `ids`, `rel`, `warnings`."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import params_hash


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class CatalogDataMixin:
    # -- analysis / embeddings / groups / hashes -----------------------------------------------------
    _ANALYSIS_COLS = {"lap_var": "lap_var", "tenengrad": "tenengrad", "sharp_face": "sharp_face",
                      "sharp_subject": "sharp_subject", "sharp_peak": "sharp_peak", "face_ratio": "face_ratio",
                      "motion_aniso": "motion_anisotropy", "blur_type": "blur_type", "n_faces": "n_faces",
                      "ear_min": "ear_min", "clip_hi": "clip_hi", "clip_lo": "clip_lo", "mean_lum": "mean_lum",
                      "median_lum": "median_lum", "dr_p1_p99": "dr_p1_p99", "tilt_deg": "tilt_deg", "noise": "noise",
                      "aesthetic": "aesthetic", "phash": "phash", "dhash": "dhash", "analyzed_v": "analyzed_v"}

    def set_analysis(self, image: Any, a: dict, clip_emb=None) -> None:
        import numpy as np

        r = self._row(image)
        vals = {c: a.get(k) for c, k in self._ANALYSIS_COLS.items()}
        vals["eyes_closed"] = None if a.get("eyes_closed") is None else int(bool(a["eyes_closed"]))
        vals["under"] = int(bool(a.get("under")))
        vals["over"] = int(bool(a.get("over")))
        vals["issues"] = ",".join(a.get("issues", []))
        vals["analyzed_at"] = now_iso()
        vals["src_mtime"] = r["file_mtime"]
        data = {k: v for k, v in a.items() if k != "clip_emb"}
        vals["data"] = json.dumps(data, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
        if clip_emb is not None:
            vals["clip_emb"] = np.asarray(clip_emb, np.float16).tobytes()
        with self._lock, self.conn:
            old = self.conn.execute("SELECT clip_emb FROM analysis WHERE image_id=?", (r["id"],)).fetchone()
            if old is not None and "clip_emb" not in vals:
                vals["clip_emb"] = old[0]
            cols = ["image_id", *vals]
            self.conn.execute(f"INSERT OR REPLACE INTO analysis({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                              [r["id"], *vals.values()])

    def set_embedding(self, image: Any, emb) -> None:
        import numpy as np

        i = self._ids(image)[0]
        with self._lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO analysis(image_id) VALUES(?)", (i,))
            self.conn.execute("UPDATE analysis SET clip_emb=? WHERE image_id=?", (np.asarray(emb, np.float16).tobytes(), i))

    def get_analysis(self, image: Any, fresh_only: bool = False) -> dict | None:
        i = self.find(image)
        if i is None:
            return None
        r = self.conn.execute("SELECT a.data, a.src_mtime, i.file_mtime FROM analysis a JOIN images i ON i.id=a.image_id "
                              "WHERE a.image_id=?", (i,)).fetchone()
        if r is None or not r["data"]:
            return None
        if fresh_only and abs((r["src_mtime"] or 0) - (r["file_mtime"] or 0)) > 1e-3:
            return None
        return json.loads(r["data"])

    def get_embeddings(self, ids: list[int]):
        import numpy as np

        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in self.conn.execute(f"SELECT image_id, clip_emb FROM analysis WHERE image_id IN ({q}) AND clip_emb IS NOT NULL", chunk):
                out[r[0]] = np.frombuffer(r[1], np.float16).astype(np.float32)
        return out

    def set_groups(self, mapping: dict[int, dict]) -> None:
        """mapping: image_id -> {burst_id, scene_id, burst_rank}."""
        with self._lock, self.conn:
            for i, g in mapping.items():
                self.conn.execute("UPDATE images SET burst_id=?, scene_id=?, burst_rank=? WHERE id=?",
                                  (g.get("burst_id"), g.get("scene_id"), g.get("burst_rank"), int(i)))

    def set_genre(self, mapping: dict[int, str]) -> None:
        with self._lock, self.conn:
            for i, g in mapping.items():
                self.conn.execute("UPDATE images SET genre=? WHERE id=?", (g, int(i)))

    def known_hashes(self) -> dict[str, str]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT content_hash, primary_path FROM images WHERE content_hash IS NOT NULL")}

    def set_content_hashes(self, mapping: dict[str, str], algo: str = "blake3") -> int:
        n = 0
        with self._lock, self.conn:
            for p, h in mapping.items():
                n += self.conn.execute("UPDATE images SET content_hash=?, hash_algo=? WHERE primary_path=?",
                                       (h, algo, self.rel(p))).rowcount
        return n

    def set_last_import(self, paths: list[str | Path], stamp: str | None = None) -> None:
        rels = [self.rel(p) for p in paths]
        self.set_meta("last_import", json.dumps(rels))
        self.set_meta("last_import_at", stamp or now_iso())
        stamp = stamp or now_iso()
        with self._lock, self.conn:
            for r in rels:
                self.conn.execute("UPDATE images SET imported_at=? WHERE primary_path=? OR raw_path=? OR jpg_path=?",
                                  (stamp, r, r, r))
        self.conn.commit()

    def last_import_ids(self) -> list[int]:
        raw = self.get_meta("last_import")
        out = []
        for rel in json.loads(raw) if raw else []:
            i = self.find(rel)
            if i is not None:
                out.append(i)
        return sorted(set(out))

    def stats(self) -> dict:
        c = self.conn
        one = lambda q: c.execute(q).fetchone()[0]  # noqa: E731
        return {
            "root": str(self.root), "images": one("SELECT COUNT(*) FROM images"),
            "raw_jpg_pairs": one("SELECT COUNT(*) FROM images WHERE kind='raw+jpg'"),
            "raw_only": one("SELECT COUNT(*) FROM images WHERE kind='raw'"),
            "ratings": {str(r[0]): r[1] for r in c.execute("SELECT rating, COUNT(*) FROM images GROUP BY rating")},
            "flags": {str(r[0]): r[1] for r in c.execute("SELECT coalesce(flag,'none'), COUNT(*) FROM images GROUP BY 1")},
            "labels": {str(r[0]): r[1] for r in c.execute("SELECT label, COUNT(*) FROM images WHERE label IS NOT NULL GROUP BY 1")},
            "with_edits": one("SELECT COUNT(*) FROM images WHERE has_edits=1"),
            "analyzed": one("SELECT COUNT(*) FROM analysis"), "tags": one("SELECT COUNT(*) FROM tags"),
            "albums": one("SELECT COUNT(*) FROM collections"), "faces": one("SELECT COUNT(*) FROM faces"),
            "date_range": [c.execute("SELECT MIN(taken_at) FROM images").fetchone()[0],
                           c.execute("SELECT MAX(taken_at) FROM images").fetchone()[0]],
            "last_scan": self.get_meta("last_scan"), "fts5": self.fts_enabled,
        }

    # -- thumbnails -------------------------------------------------------------------------------
    THUMB_LEVELS = (160, 480, 1080, 2048)

    def thumb(self, image: Any, size: int = 480, force: bool = False) -> Path:
        """JPEG thumbnail (long edge `size`) from the companion JPEG / embedded preview, rendered through the edit
        engine when the image has edits. Cached on disk keyed by (uid, size, ops hash, source stamp)."""
        import numpy as np
        from PIL import Image

        from .util import to_uint8

        r = self._row(image)
        primary = self.root / r["primary_path"]
        ops = r["ops_hash"] or ""
        stamp = params_hash([r["file_mtime"], r["file_size"], r["jpg_mtime"]])[:8]
        d = self.thumb_dir / r["uid"][:2]
        out = d / f"{r['uid']}_{int(size)}_{ops or 'orig'}_{stamp}.jpg"
        if out.exists() and not force:
            return out
        d.mkdir(parents=True, exist_ok=True)
        img = None
        if r["has_edits"]:
            try:
                from .pipeline import engine

                img = np.asarray(engine.render_file(str(primary), long_edge=int(size)), np.float32)
            except Exception as exc:
                self.warnings.append(f"edit render failed for {primary.name}, using preview: {exc}")
                img = None
        if img is None:
            from .decode import preview_display

            img = preview_display(primary, long_edge=int(size))
        Image.fromarray(to_uint8(img)).save(out, "JPEG", quality=86, optimize=True)
        for old in d.glob(f"{r['uid']}_{int(size)}_*.jpg"):
            if old != out:
                old.unlink(missing_ok=True)
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO thumbs(image_id, level, path, ops_hash) VALUES(?,?,?,?)",
                              (r["id"], int(size), str(out), ops))
        return out

    def thumbs(self, ids: list[int] | None = None, sizes: tuple[int, ...] = (160, 480), progress=None, force=False) -> dict:
        ids = ids if ids is not None else self.ids()
        done = failed = 0
        errs = []
        for n, i in enumerate(ids):
            for s in sizes:
                try:
                    self.thumb(i, s, force=force)
                    done += 1
                except Exception as exc:
                    failed += 1
                    errs.append(f"{i}: {exc}")
            if progress:
                progress(n + 1, len(ids))
        return {"made": done, "failed": failed, "errors": errs[:20]}

    def prune_thumbs(self) -> int:
        """Delete thumbnails not referenced by the current (uid, size, ops hash, stamp) of any image."""
        keep = set()
        for r in self.conn.execute("SELECT t.path FROM thumbs t JOIN images i ON i.id = t.image_id"):
            keep.add(r[0])
        n = 0
        if self.thumb_dir.exists():
            for f in self.thumb_dir.rglob("*.jpg"):
                if str(f) not in keep:
                    f.unlink(missing_ok=True)
                    n += 1
        return n
