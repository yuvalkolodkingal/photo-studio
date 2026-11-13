# Import from the card and 3-2-1 backup

Import copies a card or folder into a dated library tree, verifies every copy by hash, and tells you when it is safe to
format the card. Backup keeps the library on at least three copies, on two kinds of media, one off-site. Neither ever
deletes your originals; the only command that removes anything from the card is `import --delete-after-verify`, and it
needs your explicit say.

## Contents

- [Import](#import)
- [After the import](#after-the-import)
- [Backup configuration](#backup-configuration)
- [Backup commands](#backup-commands)
- [Rules for Claude](#rules-for-claude)
- [Verified and unverified](#verified-and-unverified)

## Import

```bash
photo --root ~/Pictures/Library import /run/media/$USER/EOS_DIGITAL --dry-run       # plan, writes nothing
photo --root ~/Pictures/Library import /run/media/$USER/EOS_DIGITAL --event "wedding"
photo --root ~/Pictures/Library import /run/media/$USER/EOS_DIGITAL --dest2 /mnt/backup/Library   # second copy
```

- The source can be the card mount, its `DCIM` folder or any folder; given a mount, the `DCIM` folder inside it is
  used. On Fedora a card mounts under `/run/media/$USER/<label>`.
- Files land in `<root>/YYYY/YYYY-MM-DD[_event]/` using the **capture date** from EXIF, with original file names.
  RAW+JPEG pairs stay together. Videos are included unless `--no-video`.
- **Use `--root LIBRARY` to name the library.** (`--dest` also works for the copy, but see the note below.) Without
  either, the current directory is the library.
- Every file is copied to `.name.part`, re-read and hash-verified (BLAKE3), then renamed. A `MANIFEST.b3` is written
  per destination folder, and `--dest2` is copied from the verified first copy and verified again.
- Duplicates (same content hash, anywhere in the library) are skipped, so re-running an import is safe.
- A sidecar stub `<file>.ps.json` is created for each image unless `--no-sidecars`.
- `--rename PATTERN` renames files. Tokens: `{orig}` or `{stem}`, `{date}` (YYYY-MM-DD), `{time}` (HHMMSS), `{year}`,
  `{seq}` (also `{seq:04d}`), `{hash7}`, `{camera}`, `{event}`. Example: `--rename "{date}_{time}_{orig}"`.
- `--full` includes the per-file list in the report. The report is also saved in
  `<root>/.photostudio/imports/<session>.json`.
- The result has `counts` (found, copied, duplicates, errors), `safe_to_format` and a `note`. **Only tell the user the
  card is safe to format when `safe_to_format` is true** and errors is empty, and say to format it in the camera.
- `--delete-after-verify` removes each source file only after every destination copy re-hashed equal to the source.
  Never add it unless the user asks for it.

`photo import SRC --dest OTHER_DIR` indexes the catalog of `OTHER_DIR` (relative paths included), whatever `--root` says, so
`photo --root OTHER_DIR ls @last-import` works straight away and `catalog_total` in the report is the number of images there.

## After the import

```bash
photo --root LIB ls --last-import --count                  # how many arrived
photo --root LIB analyze @last-import                      # then cull, see culling.md
photo --root LIB tag auto --tier metadata @last-import     # camera, date, ISO, orientation tags
photo --root LIB backup run                                # get the second and third copies
```

If `catalog_total` looks too low (files copied by hand, a card with nested folders), run `photo --root LIB scan`.

## Backup configuration

`photo --root LIB backup init` writes a commented `LIB/.photostudio/backup.yaml`. Edit the destinations, then
`photo backup run --dry-run`. The shipped template:

```yaml
version: 1
working: {media: ssd}
include: {originals: true, sidecars: true, xmp: true, catalog: true, exports: false}
exclude: ["**/.photostudio/thumbs/**", "**/*.part", "**/*.tmp", "**/*.bak"]
destinations:
  - name: external-hdd          # local drive: verified pure-Python mirror
    type: local
    path: /run/media/YOU/BACKUP/Photos
    media: hdd
    offsite: false
    mode: copy                  # copy: never deletes | sync: removed files go to _trash/<date>, never deleted
  - name: b2-crypt              # off-site via rclone (needs the rclone binary and a configured remote)
    type: rclone
    remote: "b2crypt:photos"
    backup_dir: "b2crypt:_trash/photos"
    media: cloud
    offsite: true
    crypt: true                 # verify with `rclone cryptcheck`
    transfers: 16
  - name: restic-meta           # versioned snapshots of sidecars + catalog (needs restic)
    type: restic
    repo: "rclone:b2:my-bucket/restic"
    password_file: ~/.config/photostudio/restic.pass
    what: [sidecars, xmp, catalog]
    media: cloud
    offsite: true
verify: {sample: 0.1, full_every_days: 90}
```

Three destination types: `local` (built-in copier, verified, no extra binaries), `rclone` (originals with
`copy --immutable`, sidecars with `sync --backup-dir` so older versions are kept), `restic` (versioned metadata).
Originals are copied immutably: an existing original is never overwritten or deleted by a backup.

The 3-2-1 rule is checked by `backup status`: three verified copies (working plus two), at least two media kinds,
at least one off-site. Do not edit the config for the user's cloud account details; show them the block and let
them fill in remotes and passwords. Never write secrets or password contents anywhere.

## Backup commands

```bash
photo --root LIB backup status                       # copies, media, off-site, freshness, warnings
photo --root LIB backup run --dry-run                # what would be copied; needs no binaries
photo --root LIB backup run --plan --dest b2-crypt   # prints the exact rclone/restic commands only
photo --root LIB backup run                          # all destinations (--dest NAME for one; --no-verify, --no-snapshot, --no-manifest)
photo --root LIB backup verify                       # bit-rot check of the working copy against the manifest (sampled)
photo --root LIB backup verify --full                # hash everything
photo --root LIB backup verify --dest external-hdd   # check that backup instead
photo --root LIB backup manifest                     # refresh the integrity manifest (--b3 FOLDER writes b3sum-compatible MANIFEST.b3)
photo --root LIB backup snapshot --keep 14           # dated copy of the catalog database
photo --root LIB backup restore-test /run/media/$USER/BACKUP/Photos --sample 20   # restore a sample to a temp dir and re-hash
```

Use `--native-dry-run` with `run` to also pass rclone's or restic's own dry-run flag. `backup run` also takes a dated
catalog snapshot and refreshes the manifest unless told not to.

A backup you have never restored from is a hope. After the first run, and every few months, run `restore-test` and
`verify --full` on the external copy, and tell the user the result in plain words.

## Rules for Claude

- Run `backup status` before and after; report warnings verbatim (for example "no off-site copy yet").
- Dry-run first. Do not run `backup run` against an unmounted or unconfigured destination; `status` shows
  `available: False` for a drive that is not mounted.
- Never use `mode: sync` or any delete behaviour without being asked. Even then, removed files go to `_trash/<date>`.
- Never install or run rclone or restic with sudo; print the Fedora commands (`sudo dnf install rclone restic`) for the
  user to run.
- Do not claim off-site safety unless an `offsite: true` destination has `ever_ok: True` and a recent `last_run`.

## Verified and unverified

Verified on synthetic data: import (dry run, real copy, `--dest2`, `--event`, `--rename`, duplicate skipping,
manifests), `backup init/run/status/verify/manifest/snapshot/restore-test` against a local destination, and the
`rclone` plan output. **Not run for real:** rclone and restic transfers (no binaries or remotes here), card removal
detection, `--delete-after-verify` on a real card.
