"""Command builders and runner for rclone / restic backups. Builders are pure functions returning argv lists
(no shell, no binary needed, so they are fully testable and `--dry-run` can print them); `run_command` executes
them only when asked and requires the binary.

Safety design (3-2-1; see backup.py):
  * originals   `rclone copy --immutable --checksum`  (never overwrite or delete an original at the destination)
  * sidecars / XMP / catalog snapshots   `rclone sync --checksum --backup-dir <trash>/<date>`  (changed or removed files
    are kept in the backup dir, nothing is destroyed)
  * verification  `rclone check --one-way` (or `cryptcheck` for crypt remotes) and `restic check --read-data-subset`
  * never builds `purge`, `delete`, `--delete-excluded` or `forget --prune` unless explicitly requested
`restic forget` retention flags are standard (`--keep-daily/-weekly/-monthly/-yearly`, `--prune`, `--dry-run`) but were
not verifiable offline: check `restic forget --help` for your version before relying on them.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from . import backup
from .util import ALL_MEDIA, MissingDependency, require_binary


# ================================================================================================ rclone
def _exts_glob() -> str:
    return "*.{" + ",".join(sorted(e.lstrip(".") for e in ALL_MEDIA)) + "}"


def rclone_filters(cfg: dict, kinds: tuple[str, ...]) -> list[str]:
    """Ordered `--filter` rules (first match wins; `--ignore-case` is added by the builders)."""
    rules = ["- /.photostudio/thumbs/**", "- /.photostudio/catalog.sqlite*", "- *.part", "- *.tmp", "- *.bak",
             "- .DS_Store", "- Thumbs.db"]
    for pat in cfg.get("exclude", []):
        p = pat[3:] if pat.startswith("**/") else pat
        if f"- {p}" not in rules:
            rules.append(f"- {p}")
    inc = backup.enabled_kinds(cfg, kinds)
    if "originals" in inc:
        rules += [f"+ {_exts_glob()}", f"+ {backup.MANIFEST_NAME}"]
    if "sidecars" in inc:
        rules.append("+ *.ps.json")
    if "xmp" in inc:
        rules.append("+ *.xmp")
    if "catalog" in inc:
        rules += ["+ /.photostudio/snapshots/**", "+ /.photostudio/albums.json", f"+ /.photostudio/{backup.CONFIG_FILE}",
                  f"+ /.photostudio/{backup.INTEGRITY_FILE}"]
    if "exports" in inc:
        rules += [f"+ /{d}/**" for d in cfg.get("exports_dirs", [])]
    rules.append("- *")
    return rules


def _filter_args(rules: list[str]) -> list[str]:
    out = []
    for r in rules:
        out += ["--filter", r]
    return out


def _stats_flags() -> list[str]:
    return ["--use-json-log", "--stats", "1m", "--stats-one-line", "--stats-log-level", "NOTICE"]


def split_remote(spec: str) -> tuple[str, str]:
    """'b2crypt:photos/2026' -> ('b2crypt', 'photos/2026'); a plain path has an empty remote."""
    if ":" in spec and not spec.startswith("/") and not (len(spec) > 1 and spec[1] == ":" and spec[0].isalpha() and "\\" in spec):
        r, _, p = spec.partition(":")
        return r, p
    return "", spec


def default_backup_dir(remote: str, day: str | None = None) -> str:
    """Sibling `_trash` path on the SAME remote, outside the destination (rclone forbids overlap):
    b2crypt:photos -> b2crypt:_trash/photos/<date>. A remote root has no sibling: set `backup_dir` explicitly."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    name, path = split_remote(remote)
    path = path.strip("/")
    if not path:
        raise ValueError(f"cannot derive a backup dir for the remote root '{remote}': set `backup_dir` in backup.yaml")
    lead = "/" if not name and split_remote(remote)[1].startswith("/") else ""
    base = f"{name}:" if name else lead
    parent, _, leaf = path.rpartition("/")
    return f"{base}{parent + '/' if parent else ''}_trash/{leaf}/{day}"


def rclone_copy_originals(src: str | Path, dest: str, cfg: dict, d: dict | None = None, native_dry_run: bool = False) -> list[str]:
    d = d or {}
    cmd = ["rclone", "copy", str(src), dest, "--immutable", "--checksum", "--transfers", str(d.get("transfers", 16)),
           "--checkers", str(d.get("checkers", 8)), "--ignore-case", *_stats_flags()]
    if d.get("fast_list", True):
        cmd.append("--fast-list")
    cmd += _filter_args(rclone_filters(cfg, ("originals",)))
    cmd += [str(x) for x in d.get("flags", [])]
    if native_dry_run:
        cmd.append("--dry-run")
    return cmd


def rclone_sync_meta(src: str | Path, dest: str, cfg: dict, d: dict | None = None, backup_dir: str | None = None,
                     native_dry_run: bool = False, stamp: str | None = None) -> list[str]:
    """Sidecars + XMP + catalog snapshots: mirror, but keep every replaced/removed file in `backup_dir`."""
    d = d or {}
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    bd = backup_dir or d.get("backup_dir") or default_backup_dir(dest, stamp[:4] + "-" + stamp[4:6] + "-" + stamp[6:8])
    cmd = ["rclone", "sync", str(src), dest, "--checksum", "--backup-dir", bd, "--suffix", f".{stamp}",
           "--suffix-keep-extension", "--transfers", str(d.get("transfers", 8)), "--checkers", str(d.get("checkers", 8)),
           "--ignore-case", *_stats_flags()]
    cmd += _filter_args(rclone_filters(cfg, ("sidecars", "xmp", "catalog", "exports")))
    cmd += [str(x) for x in d.get("flags", [])]
    if native_dry_run:
        cmd.append("--dry-run")
    return cmd


def rclone_check(src: str | Path, dest: str, cfg: dict, d: dict | None = None, crypt: bool | None = None,
                 kinds: tuple[str, ...] = ("originals", "sidecars", "xmp")) -> list[str]:
    """`rclone check --one-way` (hash compare where the backend supports it) or `cryptcheck` for crypt remotes
    (crypt remotes only expose presence/size to plain `check`)."""
    d = d or {}
    crypt = bool(d.get("crypt")) if crypt is None else crypt
    cmd = ["rclone", "cryptcheck" if crypt else "check", str(src), dest, "--one-way", "--checkers", str(d.get("checkers", 8)),
           "--ignore-case", *_stats_flags()]
    cmd += _filter_args(rclone_filters(cfg, kinds))
    return cmd


def rclone_restore_sample(dest: str, rel: str, out_dir: str | Path) -> list[str]:
    """Download one file from the backup (used by restore verification)."""
    return ["rclone", "copyto", f"{dest.rstrip('/')}/{rel}", str(Path(out_dir) / Path(rel).name), "--checksum"]


# ================================================================================================ restic
def _restic_base(d: dict) -> list[str]:
    cmd = ["restic", "-r", str(d["repo"])]
    if d.get("password_file"):
        cmd += ["--password-file", str(Path(d["password_file"]).expanduser())]
    return cmd


def restic_init(d: dict) -> list[str]:
    return _restic_base(d) + ["init"]


def restic_backup(d: dict, files_from: str | Path | None = None, paths: list[str] | None = None,
                  excludes: list[str] | None = None, native_dry_run: bool = False) -> list[str]:
    """Versioned snapshot of sidecars + catalog (a `--files-from` list built by `restic_file_list`) or of paths."""
    cmd = _restic_base(d) + ["backup", "--tag", "photostudio", "--one-file-system"]
    if files_from:
        cmd += ["--files-from", str(files_from)]
    cmd += list(paths or [])
    for e in excludes or []:
        cmd += ["--exclude", e]
    if native_dry_run:
        cmd.append("--dry-run")
    return cmd


def restic_check(d: dict, subset: str = "10%") -> list[str]:
    """Structure check plus a content read of a subset (`--read-data-subset`, e.g. 10% or 1/10)."""
    return _restic_base(d) + ["check", f"--read-data-subset={subset}"]


def restic_snapshots(d: dict) -> list[str]:
    return _restic_base(d) + ["snapshots", "--json"]


def restic_forget(d: dict, keep_daily: int = 7, keep_weekly: int = 4, keep_monthly: int = 12, keep_yearly: int = 3,
                  prune: bool = False, apply: bool = False) -> list[str]:
    """Retention. Runs as `--dry-run` unless `apply=True`; `--prune` (which deletes data) only when also requested."""
    cmd = _restic_base(d) + ["forget", "--tag", "photostudio", "--keep-daily", str(keep_daily), "--keep-weekly",
                             str(keep_weekly), "--keep-monthly", str(keep_monthly), "--keep-yearly", str(keep_yearly)]
    if prune and apply:
        cmd.append("--prune")
    if not apply:
        cmd.append("--dry-run")
    return cmd


def restic_file_list(root: str | Path, cfg: dict, kinds=("sidecars", "xmp", "catalog"), write_to: Path | None = None) -> list[str]:
    files = [str(p) for _, _, p in backup.iter_files(root, cfg, kinds)]
    if write_to is not None:
        write_to.write_text("\n".join(files) + ("\n" if files else ""), encoding="utf-8")
    return files


# ================================================================================================ runner
def shell_quote(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def parse_rclone_log(text: str) -> dict:
    """Summarise `--use-json-log` output: last stats object, error messages."""
    stats, errors, last_msgs = {}, [], []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if "stats" in j:
            stats = j["stats"]
        if j.get("level") in ("error", "critical"):
            errors.append(j.get("msg", ""))
        last_msgs.append(j.get("msg", ""))
    out = {"errors": errors[:20]}
    for k in ("bytes", "totalBytes", "transfers", "totalTransfers", "checks", "totalChecks", "errors", "deletes"):
        if k in stats:
            out["stats_" + k if k == "errors" else k] = stats[k]
    return out


def run_command(argv: list[str], dry_run: bool = False, env: dict | None = None, timeout: float | None = None) -> dict:
    res: dict[str, Any] = {"argv": argv, "command": shell_quote(argv), "dry_run": dry_run}
    if dry_run:
        res["executed"] = False
        return res
    require_binary(argv[0], "backup")
    cp = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=timeout)
    res.update({"executed": True, "returncode": cp.returncode, "ok": cp.returncode == 0,
                "stdout": cp.stdout[-3000:], "stderr": cp.stderr[-3000:]})
    if argv[0] == "rclone":
        res["summary"] = parse_rclone_log(cp.stderr)
    return res


# ================================================================================================ orchestration
def _dest_commands(root: Path, cfg: dict, d: dict, native_dry_run: bool) -> list[tuple[str, list[str]]]:
    if d["type"] == "rclone":
        dest = d["remote"]
        return [("originals", rclone_copy_originals(root, dest, cfg, d, native_dry_run)),
                ("meta", rclone_sync_meta(root, dest, cfg, d, d.get("backup_dir"), native_dry_run))]
    if d["type"] == "restic":
        lst = root / ".photostudio" / "restic-files.txt"
        return [("snapshot", restic_backup(d, files_from=lst, native_dry_run=native_dry_run))]
    return []


def plan_backup(root: str | Path, dest_name: str | None = None, native_dry_run: bool = False) -> list[dict]:
    """The exact commands a `run` would execute (no side effects)."""
    root = Path(root).expanduser()
    cfg = backup.load_config(root)
    out = []
    for d in backup.get_destination(cfg, dest_name):
        if d["type"] == "local":
            out.append({"dest": d["name"], "type": "local", "action": f"verified Python mirror ({d.get('mode', 'copy')}) -> {d['path']}",
                        "commands": []})
        else:
            out.append({"dest": d["name"], "type": d["type"],
                        "commands": [{"step": s, "command": shell_quote(c)} for s, c in _dest_commands(root, cfg, d, native_dry_run)]})
    return out


def run_backup(root: str | Path, dest_name: str | None = None, dry_run: bool = False, native_dry_run: bool = False,
               snapshot: bool = True, verify: bool = True, kinds=None, progress=None, update_manifest: bool = True) -> dict:
    """Run (or with dry_run, only describe) the backup profile in `<root>/.photostudio/backup.yaml`."""
    root = Path(root).expanduser()
    cfg = backup.load_config(root)
    errs = backup.validate_config(cfg)
    if errs:
        raise ValueError("backup.yaml: " + "; ".join(errs))
    dests = backup.get_destination(cfg, dest_name)
    if not dests:
        raise ValueError(f"no destinations configured: run `photo backup init` and edit {backup.config_path(root)}")
    res: dict[str, Any] = {"root": str(root), "dry_run": dry_run, "results": [], "snapshot": None}
    if update_manifest and not dry_run:
        # baseline for bit-rot detection: new originals get hashed now, unchanged (size+mtime) ones are NOT re-read
        res["manifest"] = backup.manifest_update(root, cfg, kinds=("originals", "sidecars", "xmp"))
        res["manifest"] = {k: res["manifest"][k] for k in ("files", "added", "modified", "touched", "unchanged")}
    if snapshot:
        if dry_run:
            res["snapshot"] = "would write a dated VACUUM INTO copy of the catalog to .photostudio/snapshots/"
        elif (root / ".photostudio" / "catalog.sqlite").exists():
            from .catalog import Catalog

            with Catalog(root) as cat:
                res["snapshot"] = str(backup.snapshot_catalog(cat))
    for d in dests:
        r: dict[str, Any] = {"dest": d["name"], "type": d["type"]}
        try:
            if d["type"] == "local":
                dp = Path(d["path"]).expanduser()
                if not dp.exists() and not dp.parent.exists():
                    raise FileNotFoundError(f"destination '{d['name']}' is not available ({dp}): drive not mounted?")
                m = backup.mirror(root, dp, cfg, kinds, mode=d.get("mode", "copy"), checksum=bool(d.get("checksum")),
                                  dry_run=dry_run, progress=(lambda a, b: progress(a, b)) if progress else None)
                r.update({k: m[k] for k in ("copied", "updated", "skipped", "trashed", "bytes", "conflicts", "errors", "ok")})
                if dry_run:
                    r["plan"] = m["plan"][:200]
                r["ok"] = m["ok"]
            else:
                cmds = _dest_commands(root, cfg, d, native_dry_run)
                r["commands"] = []
                ok = True
                if d["type"] == "restic" and not dry_run:
                    restic_file_list(root, cfg, write_to=root / ".photostudio" / "restic-files.txt")
                for step, argv in cmds:
                    out = run_command(argv, dry_run=dry_run)
                    r["commands"].append({"step": step, **{k: out[k] for k in out if k not in ("argv",)}})
                    ok = ok and (dry_run or out.get("ok", False))
                if verify and not dry_run and ok and d["type"] == "rclone":
                    chk = run_command(rclone_check(root, d["remote"], cfg, d), dry_run=False)
                    r["commands"].append({"step": "verify", **{k: chk[k] for k in chk if k != "argv"}})
                    ok = ok and chk.get("ok", False)
                r["ok"] = ok
        except MissingDependency as exc:
            r.update({"ok": False, "error": str(exc)})
        except Exception as exc:
            r.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        if not dry_run:
            backup.record_run(root, d["name"], d["type"], bool(r.get("ok")), {k: r[k] for k in r if k in ("copied", "updated", "skipped", "error")})
        res["results"].append(r)
    res["ok"] = all(r.get("ok", dry_run) for r in res["results"])
    return res


def verify_destination(root: str | Path, dest_name: str, dry_run: bool = False, subset: str = "10%", sample: int = 10) -> dict:
    """Verify one destination: local -> restore sample + presence; rclone -> check/cryptcheck; restic -> check subset."""
    root = Path(root).expanduser()
    cfg = backup.load_config(root)
    d = backup.get_destination(cfg, dest_name)[0]
    if d["type"] == "local":
        return {"dest": dest_name, **backup.restore_verify(root, d["path"], sample=sample)}
    if d["type"] == "rclone":
        out = run_command(rclone_check(root, d["remote"], cfg, d), dry_run=dry_run)
        return {"dest": dest_name, **out}
    out = run_command(restic_check(d, subset), dry_run=dry_run)
    return {"dest": dest_name, **out}
