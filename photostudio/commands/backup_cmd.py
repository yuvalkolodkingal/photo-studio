"""Backup commands: init, run, verify, status, manifest, snapshot.

3-2-1 in practice: `photo backup init` writes `.photostudio/backup.yaml`; add a local drive (verified Python mirror),
an off-site rclone remote (originals `copy --immutable`, sidecars `sync --backup-dir`) and optionally restic for
versioned sidecar/catalog snapshots. `--dry-run` prints the exact commands without needing rclone/restic installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .library_cmd import find_root, open_catalog


def _prog(args):
    if getattr(args, "quiet", False) or getattr(args, "json", False) or not sys.stderr.isatty():
        return None
    return lambda a, b: print(f"\r{a}/{b}", end="", file=sys.stderr, flush=True)


def cmd_init(args):
    from .. import backup

    root = find_root(args)
    p = backup.init_config(root, force=args.force)
    return {"config": str(p), "next": "edit the destinations, then: photo backup run --dry-run"}


def cmd_run(args):
    from .. import backup_tools as bt

    root = find_root(args)
    if args.plan:
        return {"plan": bt.plan_backup(root, args.dest, native_dry_run=args.native_dry_run)}
    return bt.run_backup(root, args.dest, dry_run=args.dry_run, native_dry_run=args.native_dry_run,
                         snapshot=not args.no_snapshot, verify=not args.no_verify, progress=_prog(args),
                         update_manifest=not args.no_manifest)


def cmd_verify(args):
    from .. import backup
    from .. import backup_tools as bt

    root = find_root(args)
    if args.dest:
        return bt.verify_destination(root, args.dest, dry_run=args.dry_run, subset=args.subset, sample=args.restore_sample)
    kinds = tuple(k.strip() for k in args.kinds.split(","))
    res = backup.manifest_verify(root, kinds=kinds, sample=None if args.full else args.sample, progress=_prog(args))
    if not args.verbose:
        res = {k: (v if not isinstance(v, list) or len(v) <= 50 else v[:50] + [f"... {len(v) - 50} more"]) for k, v in res.items()}
    return res


def cmd_status(args):
    from .. import backup

    return backup.status(find_root(args))


def cmd_manifest(args):
    from .. import backup

    root = find_root(args)
    if args.b3:
        folder = Path(args.b3).expanduser()
        if args.check:
            mans = sorted(folder.rglob(backup.MANIFEST_NAME))
            if not mans:
                raise FileNotFoundError(f"no {backup.MANIFEST_NAME} below {folder}")
            out = [{"manifest": str(m), **{k: (v if k == "clean" else v) for k, v in backup.verify_b3(m).items()}} for m in mans]
            return {"clean": all(o["clean"] for o in out), "manifests": out}
        return backup.create_b3_manifest(folder, recursive=True)
    return backup.manifest_update(root, kinds=("originals", "sidecars", "xmp"), forget_missing=args.forget_missing, progress=_prog(args))


def cmd_snapshot(args):
    from .. import backup

    with open_catalog(args) as cat:
        return {"snapshot": str(backup.snapshot_catalog(cat, keep=args.keep))}


def cmd_restore_test(args):
    from .. import backup

    root = find_root(args)
    return backup.restore_verify(root, args.path, sample=args.sample)


def register(subparsers) -> None:
    try:
        from ..cli import _common_parser

        common = _common_parser(False)
    except Exception:  # noqa: BLE001
        common = None
    b = subparsers.add_parser("backup", help="3-2-1 backup: init run verify status manifest snapshot restore-test")
    g = b.add_subparsers(dest="bcmd", required=True, metavar="<action>")

    def sub(name, fn, help_):
        p = g.add_parser(name, help=help_, description=help_, parents=[common] if common else [])
        p.set_defaults(func=fn)
        return p

    p = sub("init", cmd_init, "write a commented .photostudio/backup.yaml")
    p.add_argument("--force", action="store_true")
    p = sub("run", cmd_run, "back up to every configured destination (originals are never overwritten or deleted)")
    p.add_argument("--dest", help="only this destination")
    p.add_argument("--dry-run", action="store_true", help="print what would run; executes nothing, needs no binaries")
    p.add_argument("--native-dry-run", action="store_true", help="add rclone/restic's own --dry-run to the commands")
    p.add_argument("--plan", action="store_true", help="only list the commands")
    p.add_argument("--no-snapshot", action="store_true", help="skip the catalog snapshot")
    p.add_argument("--no-verify", action="store_true", help="skip the post-copy rclone check")
    p.add_argument("--no-manifest", action="store_true", help="skip updating the integrity manifest")
    p = sub("verify", cmd_verify, "bit-rot check against the manifest; with --dest, verify that backup instead")
    p.add_argument("--dest")
    p.add_argument("--full", action="store_true", help="hash every file (default: --sample share)")
    p.add_argument("--sample", type=float, default=0.1)
    p.add_argument("--kinds", default="originals,sidecars,xmp")
    p.add_argument("--subset", default="10%", help="restic --read-data-subset")
    p.add_argument("--restore-sample", type=int, default=10)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    sub("status", cmd_status, "3-2-1 status: copies, media, off-site, freshness, warnings")
    p = sub("manifest", cmd_manifest, "update the integrity manifest; --b3 FOLDER writes b3sum-compatible MANIFEST.b3 files")
    p.add_argument("--b3", metavar="FOLDER")
    p.add_argument("--check", action="store_true", help="with --b3: verify existing MANIFEST.b3 files")
    p.add_argument("--forget-missing", action="store_true")
    p = sub("snapshot", cmd_snapshot, "dated copy of the catalog database (VACUUM INTO)")
    p.add_argument("--keep", type=int, default=10)
    p = sub("restore-test", cmd_restore_test, "prove a local backup is restorable (presence + random hash sample)")
    p.add_argument("path")
    p.add_argument("--sample", type=int, default=10)
