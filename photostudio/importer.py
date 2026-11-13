"""Import from an SD card / folder into the library: `<root>/YYYY/YYYY-MM-DD[_event]/` with original filenames,
RAW+JPEG kept together, content-hash duplicate skipping, optional second destination, BLAKE3 verification of
every copy and a `MANIFEST.b3` per destination folder.

Safety rules:
  * the card is only read, never formatted; source files are deleted ONLY with `delete_after_verify=True`, one by one,
    and only after every destination copy re-hashed equal to the source (or an existing identical copy was
    re-verified for a duplicate)
  * copies go to `.<name>.part` and are renamed after verification: no half-written originals
  * `dry_run=True` hashes and plans but writes nothing
  * `safe_to_format` in the report is True only when every file is verified in every destination
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import backup
from .catalog import _default_doc, save_doc, sidecar_file
from .util import ALL_MEDIA, RAW_EXTS, VIDEO_EXTS, dumps, file_hash

SKIP_DIRS = {"thumbnails", "misc", "canonmsc", "private", ".trashes", "system volume information", "$recycle.bin"}
CARD_GLOBS = ["/run/media/{user}/*", "/media/{user}/*", "/media/*", "/mnt/*", "/Volumes/*"]


def slug(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", " ", s.strip())
    return re.sub(r"\s+", "-", s).strip("-.")


def detect_dcim(source: str | Path) -> Path:
    """`/media/SD` or `/media/SD/DCIM` -> the DCIM folder; a plain folder is returned unchanged."""
    p = Path(source).expanduser()
    if p.name.upper() == "DCIM":
        return p
    if p.is_dir():
        for c in sorted(p.iterdir()):
            if c.is_dir() and c.name.upper() == "DCIM":
                return c
    if not p.is_dir():
        raise FileNotFoundError(f"import source not found: {p}")
    return p


def find_cards() -> list[dict]:
    """Mounted volumes that contain a DCIM folder (Linux/macOS mount points)."""
    import glob

    user = os.environ.get("USER", "")
    seen, out = set(), []
    for pat in CARD_GLOBS:
        for m in glob.glob(pat.format(user=user)):
            p = Path(m)
            d = detect_dcim(p)
            if d.name.upper() == "DCIM" and str(d) not in seen:
                seen.add(str(d))
                n = sum(1 for f in d.rglob("*") if f.suffix.lower() in ALL_MEDIA)
                out.append({"mount": str(p), "dcim": str(d), "label": p.name, "media_files": n})
    return out


@dataclass
class Item:
    stem: str
    files: list[Path] = field(default_factory=list)
    primary: Path | None = None
    when: datetime | None = None
    meta: dict = field(default_factory=dict)


def enumerate_items(src: Path, include_video: bool = True) -> list[Item]:
    groups: dict[tuple[str, str], list[Path]] = {}
    exts = ALL_MEDIA if include_video else ALL_MEDIA - VIDEO_EXTS
    for dp, dns, fns in os.walk(src):
        dns[:] = sorted(d for d in dns if not d.startswith(".") and d.lower() not in SKIP_DIRS)
        for fn in sorted(fns):
            if fn.startswith("."):
                continue
            stem, ext = os.path.splitext(fn)
            if ext.lower() in exts:
                groups.setdefault((dp, stem.lower()), []).append(Path(dp) / fn)
    items = []
    for (_, _), files in sorted(groups.items()):
        files.sort(key=lambda p: (p.suffix.lower() not in RAW_EXTS, p.name))
        items.append(Item(stem=files[0].stem, files=files, primary=files[0]))
    return items


def _capture_time(it: Item) -> datetime:
    from .metadata import read_metadata

    for f in it.files:
        try:
            md = read_metadata(f)
            if md.get("datetime"):
                it.meta = md
                return datetime.fromisoformat(md["datetime"])
        except Exception:
            continue
    return datetime.fromtimestamp(min(f.stat().st_mtime for f in it.files))


def _render_stem(template: str | None, it: Item, ph: str, seq: int, event: str) -> str:
    if not template:
        return it.stem
    cam = re.sub(r"[^\w-]+", "-", str(it.meta.get("model") or "")).strip("-")
    d = {"stem": it.stem, "orig": it.stem, "date": it.when.strftime("%Y-%m-%d"), "time": it.when.strftime("%H%M%S"),
         "year": it.when.strftime("%Y"), "seq": seq, "hash7": ph[:7], "camera": cam, "event": event}
    try:
        out = template.format(**d)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"bad rename template '{template}': {exc} (tokens: {', '.join(d)})") from exc
    return re.sub(r"[\\/:*?\"<>|]+", "_", out)


def _copy_part(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("." + dst.name + ".part")
    shutil.copyfile(src, tmp)
    return tmp


def import_media(source: str | Path, dest: str | Path, *, dest2: str | Path | None = None, event: str | None = None,
                 rename: str | None = None, delete_after_verify: bool = False, dry_run: bool = False,
                 sidecars: bool = True, include_video: bool = True, catalog: Any = None, algo: str = "blake3",
                 session: str | None = None, progress: Callable[[int, int, str], None] | None = None,
                 write_report: bool = True) -> dict:
    t0 = time.time()
    src = detect_dcim(source)
    dest = Path(dest).expanduser()
    dest2 = Path(dest2).expanduser() if dest2 else None
    if not src.is_dir():
        raise FileNotFoundError(f"source not found: {src}")
    if src.resolve() == dest.resolve() or (dest2 and src.resolve() == dest2.resolve()):
        raise ValueError("source and destination are the same folder")
    if dest2 and dest2.resolve() == dest.resolve():
        raise ValueError("the two destinations must differ")
    items = enumerate_items(src, include_video)
    session = session or "import-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    ev = slug(event) if event else ""
    # ---- known hashes (duplicate detection): manifests under the destination + the catalog
    known: dict[str, str] = backup.collect_known_hashes(dest)
    if catalog is not None:
        for h, rel in catalog.known_hashes().items():
            known.setdefault(h, str(catalog.root / rel))
    report: dict[str, Any] = {"session": session, "source": str(src), "dest": str(dest), "dest2": str(dest2) if dest2 else None,
                              "dry_run": dry_run, "algo": algo, "files": [], "errors": [], "warnings": [], "folders": []}
    # ---- hash source + plan
    plan: list[dict] = []
    total_bytes = 0
    seen_session: dict[str, str] = {}
    seq = 0
    for n, it in enumerate(items, start=1):
        it.when = _capture_time(it)
        folder_rel = f"{it.when:%Y}/{it.when:%Y-%m-%d}" + (f"_{ev}" if ev else "")
        entries = []
        for f in it.files:
            if progress:
                progress(n, len(items), f"hashing {f.name}")
            h = file_hash(f, algo)
            entries.append({"src": f, "hash": h, "size": f.stat().st_size})
        seq += 1
        stem = _render_stem(rename, it, entries[0]["hash"], seq, ev)
        # collision handling per item: all members share the final stem
        cand, k = stem, 0
        while True:
            clash = False
            for e in entries:
                t = dest / folder_rel / f"{cand}{e['src'].suffix}"
                if t.exists() and e["hash"] not in known and file_hash(t, algo) != e["hash"]:
                    clash = True
            if not clash:
                break
            k += 1
            cand = f"{stem}-{k}"
        for e in entries:
            rel = f"{folder_rel}/{cand}{e['src'].suffix}"
            status, existing = "new", None
            if e["hash"] in seen_session:
                status, existing = "duplicate", seen_session[e["hash"]]
            elif e["hash"] in known:
                status, existing = "duplicate", known[e["hash"]]
            elif (dest / rel).exists() and file_hash(dest / rel, algo) == e["hash"]:
                status, existing = "duplicate", str(dest / rel)
            if status == "new":
                seen_session[e["hash"]] = str(dest / rel)
                total_bytes += e["size"]
            plan.append({"src": str(e["src"]), "rel": rel, "hash": e["hash"], "size": e["size"], "status": status,
                         "existing": existing, "folder": folder_rel, "item": it.stem, "is_primary": e["src"] == it.primary})
    # ---- space check
    for d in [dest] + ([dest2] if dest2 else []):
        probe = d
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        if total_bytes > free * 0.98:
            raise OSError(f"not enough free space in {d}: need {total_bytes / 1e9:.2f} GB, have {free / 1e9:.2f} GB")
    report["bytes_to_copy"] = total_bytes
    # ---- execute
    by_folder: dict[tuple[str, str], dict[str, str]] = {}
    verified_src: set[str] = set()
    imported_primaries: list[str] = []
    for n, p in enumerate(plan, start=1):
        rec = {k: p[k] for k in ("src", "rel", "hash", "size", "status")}
        rec["dest"] = str(dest / p["rel"])
        if dest2:
            rec["dest2"] = str(dest2 / p["rel"])
        if p["status"] == "duplicate":
            rec["existing"] = p["existing"]
            if not dry_run and p["existing"] and Path(p["existing"]).exists() and file_hash(p["existing"], algo) == p["hash"]:
                rec["verified"] = True
                if dest2 is not None:      # the card may only be wiped once BOTH destinations hold a verified copy
                    try:
                        rel_existing = Path(p["existing"]).resolve().relative_to(dest.resolve())
                        t2 = dest2 / rel_existing
                        if not (t2.exists() and file_hash(t2, algo) == p["hash"]):
                            tmp = _copy_part(Path(p["existing"]), t2)
                            if file_hash(tmp, algo) != p["hash"]:
                                tmp.unlink(missing_ok=True)
                                raise IOError("hash mismatch after copy to the second destination")
                            os.replace(tmp, t2)
                            rec["restored_to_dest2"] = True
                            by_folder.setdefault((str(dest2), p["folder"]), {})[t2.name] = p["hash"]
                    except ValueError:
                        rec["verified"] = False
                        report["warnings"].append(f"{Path(p['src']).name}: existing copy is outside {dest}; no second copy made")
                    except Exception as exc:
                        rec["verified"] = False
                        report["errors"].append(f"{Path(p['src']).name}: {type(exc).__name__}: {exc}")
                if rec["verified"]:
                    verified_src.add(p["src"])
            elif dry_run:
                rec["verified"] = None
            else:
                rec["verified"] = False
                report["warnings"].append(f"{Path(p['src']).name}: duplicate hash known but the existing copy could not be "
                                          f"re-verified; it will not be deleted from the card")
            report["files"].append(rec)
            continue
        if progress:
            progress(n, len(plan), f"copying {Path(p['src']).name}")
        if dry_run:
            rec["status"] = "would_copy"
            report["files"].append(rec)
            continue
        try:
            src_f = Path(p["src"])
            ok = {}
            for label, root in (("a", dest), ("b", dest2)):
                if root is None:
                    continue
                target = root / p["rel"]
                from_ = src_f if label == "a" else dest / p["rel"]   # second copy reads the verified first copy
                tmp = _copy_part(from_, target)
                if file_hash(tmp, algo) != p["hash"]:
                    tmp.unlink(missing_ok=True)
                    raise IOError(f"hash mismatch after copy to {root}")
                shutil.copystat(src_f, tmp)
                os.replace(tmp, target)
                ok[label] = True
                by_folder.setdefault((str(root), p["folder"]), {})[target.name] = p["hash"]
            rec["status"] = "copied"
            rec["verified"] = True
            verified_src.add(p["src"])
            if p["is_primary"]:
                imported_primaries.append(str(dest / p["rel"]))
        except Exception as exc:
            rec["status"] = "error"
            rec["verified"] = False
            rec["error"] = f"{type(exc).__name__}: {exc}"
            report["errors"].append(f"{Path(p['src']).name}: {rec['error']}")
        report["files"].append(rec)
    # ---- manifests, sidecar stubs
    report["manifests"] = []
    if not dry_run:
        for (root, folder_rel), ents in by_folder.items():
            report["manifests"].append(str(backup.update_b3(Path(root) / folder_rel, ents)))
        if sidecars:
            for p in plan:
                if p["status"] == "new" and p["is_primary"] and Path(p["src"]).suffix.lower() not in VIDEO_EXTS:
                    for root in [dest] + ([dest2] if dest2 else []):
                        t = root / p["rel"]
                        if t.exists() and not sidecar_file(t).exists():
                            save_doc(t, _default_doc(t.name))
    report["folders"] = sorted({p["folder"] for p in plan})
    # ---- counts / safety
    c = {"found": len(plan), "copied": sum(1 for f in report["files"] if f["status"] == "copied"),
         "would_copy": sum(1 for f in report["files"] if f["status"] == "would_copy"),
         "duplicates": sum(1 for f in report["files"] if f["status"] == "duplicate"),
         "errors": len(report["errors"]), "items": len(items)}
    report["counts"] = c
    report["safe_to_format"] = bool(not dry_run and not report["errors"] and
                                    all(f.get("verified") for f in report["files"]))
    report["note"] = ("All files verified in every destination: it is safe to format the card IN THE CAMERA."
                      if report["safe_to_format"] else
                      "Not safe to format yet." if not dry_run else "Dry run: nothing was written.")
    # ---- delete from card (explicit, verified, per file)
    report["deleted_from_source"] = []
    if delete_after_verify and not dry_run:
        for f in report["files"]:
            if f.get("verified") and f["src"] in verified_src and f["status"] in ("copied", "duplicate"):
                try:
                    os.remove(f["src"])
                    report["deleted_from_source"].append(f["src"])
                except OSError as exc:
                    report["warnings"].append(f"could not delete {f['src']}: {exc}")
    # ---- catalog
    if catalog is not None and not dry_run and imported_primaries:
        try:
            catalog.scan(paths=[str(dest / fr) for fr in report["folders"] if (dest / fr).exists()])
            catalog.set_content_hashes({p["rel"] and str(dest / p["rel"]): p["hash"] for p in plan
                                        if p["status"] == "new" and p["is_primary"] and (dest / p["rel"]).exists()}, algo)
            catalog.set_last_import(imported_primaries)
            report["catalog_total"] = catalog.stats()["images"]
        except Exception as exc:
            report["warnings"].append(f"catalog update failed: {exc}")
    report["seconds"] = round(time.time() - t0, 2)
    if write_report and not dry_run:
        d = backup.meta_dir(dest) / "imports"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{session}.json").write_text(dumps(report), encoding="utf-8")
    return report
