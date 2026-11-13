# Library: catalog, search, ratings, tags, albums, events, settings copy

The library is a folder tree of originals plus a SQLite catalog at `<root>/.photostudio/`. **Sidecars
(`<image>.ps.json`) and XMP files next to the images are the source of truth**; the catalog is a cache that can be
rebuilt at any time. Commands act on the library under `--root` (default: the current directory, or the nearest folder
that has a catalog).

## Contents

- [Selecting images](#selecting-images)
- [Scan, list, search](#scan-list-search)
- [Filter DSL](#filter-dsl)
- [Rating, flags, colour labels](#rating-flags-colour-labels)
- [Tags](#tags)
- [Albums, smart albums, gallery, contact sheets](#albums-smart-albums-gallery-contact-sheets)
- [Events](#events)
- [Copy and paste settings, batch consistency](#copy-and-paste-settings-batch-consistency)
- [Versions and snapshots](#versions-and-snapshots)
- [Catalog maintenance](#catalog-maintenance)

## Selecting images

Every command that acts on images understands the same selectors, through one shared resolver: the library commands
(`rate`, `flag`, `label`, `tag ...`, `album create/add/remove`, `analyze`, `cull`, `group`, `events`, `thumbs`,
`catalog rewrite-sidecars`) and the editing commands (`develop`, `edit`, `render`, `export`, `reset`, `copy-settings`,
`paste-settings`, `info`, `contact-sheet`, `stack`, `lut bake --file`, the inputs of `video ...`). `images` can be any mix of:

| selector | meaning |
|---|---|
| `path`, glob | a file or a shell glob (`2026/*/IMG_00*.CR3`); needs no catalog for the editing commands |
| `id:N` | catalog id (from `photo ls`) |
| `@all` | every image in the catalog |
| `@last-import` | files of the most recent `photo import` |
| `@album:NAME` | members of an album (smart albums too); `@album:id:3` addresses an album by its number |
| `@session:YYYY-MM-DD` | images taken on that day |
| `@tag:a|b` | images with that tag (or descendants) |
| a folder | every image under that folder (indexed on first use) |
| `--query Q` | filter in the DSL below; with explicit selectors it narrows them, alone it selects (`id`, `rating`, `tag`, ...) |

```bash
photo edit @album:Best --set exposure.ev=0.2
photo export @last-import --preset web --out /tmp/web
photo develop --query "rating>=4 AND tag:people" --art portra_400 --merge --version film --no-render
photo versions list id:12                  # commands for one image (versions, copy-settings, lut bake --file) need exactly one match
```

Selectors resolve against the catalog at `--root` (or the nearest folder that has one); the first use of a folder indexes it.
A selector that matches nothing is an error, not a silent no-op. `photo ls` and `photo search` also take
`--sel/-s SELECTOR` (repeatable); `photo ls @last-import` and `photo ls id:7` work too, because `ls` recognises a selector in
its optional text argument. `ls --query "id>10"` filters by id.

The editing commands skip video files in a selection. Videos are not catalogued, so `video` inputs use selectors only for stills
(`video timelapse @album:Trip`, `video startrails @session:2026-06-14`, `video lut --from-image id:7`).

## Scan, list, search

```bash
photo scan                          # index the library incrementally; reconciles sidecars and XMP (also after `import`)
photo scan --hash                   # also hash file contents
photo ls --query "rating>=4 AND flag:pick" --sort taken_at --order desc --limit 50
photo ls --count --query "camera:R50 AND iso>3200"
photo ls --tag people --since 2026-06 --until 2026-06-30
photo ls --sel @album:Best --full          # every field
photo search sunset                         # full text: names, tags, notes, captions, places, camera, lens
photo stats                                 # counts: ratings, flags, labels, tags, albums, date range
photo info IMG.CR3                          # metadata, detected mode, sidecar summary
```

Sorts: `taken_at, rating, name, added, imported, iso, focal, aperture, camera, id, path, sharpness, aesthetic, burst, size`.
Use `--json` for structured output. Output has `id`, `file`, `rating`, `flag`, `label`, `taken`, `tags`.

## Filter DSL

Used by `ls`, `search`, `--query` on bulk commands and in smart albums. Words are combined with `AND` (also implicit),
`OR`, `EXCEPT` (and not), `NOT`, parentheses.

| field | examples |
|---|---|
| `rating` (alias `stars`) | `rating>=4`, `rating:5` |
| `flag` | `flag:pick`, `flag:reject`, `flag:none` |
| `label` | `label:red` |
| `tag` | `tag:people`, `tag:people|noa` (prefix match includes descendants), `tag:a,b` (all of) |
| `camera`, `lens` | `camera:R50`, `lens:RF-S` |
| `iso`, `focal`, `focal35`, `aperture`, `shutter` | `iso>3200`, `aperture<=2.8`, `shutter<1/250`, `focal35>=200` |
| `taken` (alias `date`) | `taken:[2026-06-01;2026-06-30]`, `taken:2026-06`, `year:2026` |
| `album` | `album:Best` |
| `person`, `place` | `person:Noa`, `place:Lisbon` (set by `tag people` and `tag places`) |
| `issue` | `issue:blurry` (also soft, eyes_closed, overexposed, underexposed, tilted, noisy, duplicate) |
| `mode` (alias `genre`) | `mode:portrait` |
| `has_edits`, `kind`, `folder`, `filename`, `width`, `height`, `size`, `faces`, `sharpness`, `aesthetic`, `burst`, `scene` | `has_edits:true`, `kind:raw` |
| `text` | `text:sunset` (full text), a bare word is also full text |

Example: `rating>=4 AND tag:people|noa EXCEPT flag:reject`.

## Rating, flags, colour labels

```bash
photo rate 4 id:12 id:15                      # 0-5, written to the sidecar and XMP
photo rate 0 @all --query "flag:reject"
photo flag pick @last-import --query "rating>=4"
photo flag reject 2026/*/IMG_00*.CR3
photo flag none @all                          # clear flags
photo label red @album:Edit                   # red yellow green blue purple, or none
```

Rating and label changes are immediate. Do not mass-rate or mass-flag without telling the user which selection you
used (`photo ls --count --query ...` first).

## Tags

Tags are hierarchical, separated by `|`: `people|noa`, `places|portugal|lisbon`.

```bash
photo tag add "people|noa,events|birthday" id:1 id:5
photo tag add "places|portugal" @album:Trip
photo tag remove "events|birthday" id:1            # --recursive to include descendants
photo tag list --text                              # indented tree with counts (--tree, --prefix, --confirmed)
photo tag rename "people|noa" "Noa"                # leaf rename everywhere, --merge to merge on clash
photo tag move "places|lisbon" "places|portugal"   # '-' = top level
photo tag merge "kids" "people|kids"
photo tag delete "events|old"                      # --keep-children
photo tag auto --tier metadata @last-import        # camera, date, ISO, orientation, genre, season, time of day (always works)
photo tag auto --tier clip,ram,caption @last-import --min-prob 0.3 --confirm-above 0.8   # needs optional models
photo tag confirm --tags "scenery" @last-import    # accept suggestions; --reject drops them
photo tag places @last-import                      # GPS to places|country|region|city (offline)
photo tag people --detect @all                     # faces: detect, then --cluster, then --name 3 "Noa", then --match
```

- The `metadata` tier needs no models. `clip`, `ram`, `caption` (and `--aesthetic` in `analyze`) need optional ML
  packages and weights that are **not in the `photo models` registry** and must be placed under
  `~/.cache/photostudio/models/` by hand (CLIP ViT-L/14, LAION aesthetic head, Recognize Anything, Florence-2).
  Without them the command stops with an error that names the missing package. Suggested AI tags are unconfirmed until `tag confirm` or
  `--confirm-above`.
- `tag people` needs InsightFace (the weights are non-commercial research only, fetched manually). It is for
  personal use; see [install-models-licences.md](install-models-licences.md). Do not run it unasked, and never put
  names of real people into tags without the user's say.
- `tag places` needs the offline `geo` extra.
- Tags are stored in sidecar and XMP (`dc:subject` and `lr:hierarchicalSubject`), so other tools read them.

## Albums, smart albums, gallery, contact sheets

```bash
photo album create "Trip 2026" id:1 id:5 @last-import      # static album ("a/b" makes a hierarchy)
photo album add "Trip 2026" --query "rating>=4"
photo album remove "Trip 2026" id:5
photo album smart "Best of" --rule "rating>=4 AND tag:people"     # live rule; DSL or JSON
photo album smart "Rule" --rule '{"all":[{"field":"rating","op":">=","value":4},{"field":"flag","op":"=","value":"pick"}]}'
photo album list --tree
photo album show "Best of" --limit 20
photo album delete "Old"                                  # never deletes the images; --recursive for children

photo album export "Trip 2026" /tmp/trip --preset web --zip /tmp/trip.zip            # rendered JPEGs + zip
photo album export "Trip 2026" /tmp/trip_orig --mode hardlink --no-raw               # copy/symlink/hardlink originals
photo album gallery "Trip 2026" /tmp/gallery --title "Trip" --web-size 1600          # static HTML, open index.html
photo album contact-sheet "Trip 2026" /tmp/trip_sheet.pdf --cols 5 --page A4 --title "Trip"
photo contact-sheet IMG_A.JPG IMG_B.JPG --out /tmp/sheet.jpg --columns 4 --cell 400    # from files, also PDF/PNG
```

- `album export` copies originals by default (`--no-originals` to skip, `--no-raw`/`--no-jpg`, `--sidecars` to include
  sidecars) and/or renders with an export preset (`--preset`). `--dry-run` shows what it would do.
- The gallery is a self-contained folder (grid + lightbox, no server, no network). Add `--originals` to copy and link
  the originals and `--filenames` to show names. Smart albums refresh when you re-run `scan` or any bulk command.

## Events

`photo events` splits the timeline into events using adaptive time gaps and GPS (`--gap-hours N` for a fixed gap,
`--merge-km` to merge nearby places). Without `--apply` it only prints. `--apply` creates event albums and tags
(`--no-albums`, `--no-tags`) and `--name 2="Birthday"` names event number 2.

```bash
photo events @last-import                    # preview
photo events @last-import --apply --name 1="Lisbon trip"
```

## Copy and paste settings, batch consistency

```bash
photo copy-settings IMG_0042.CR3 --ops exposure,white_balance,hsl_mixer   # to the settings clipboard (subset or all)
photo paste-settings IMG_0043.CR3 IMG_0044.CR3 --mode merge --ops hsl_mixer
photo paste-settings IMG_0043.CR3 IMG_0044.CR3 --from IMG_0042.CR3 --mode merge    # copy straight from an image, no clipboard
```

Paste modes: `replace` (default: whole stack, also copies the mode and art labels), `merge` (per-op update by op and id),
`append`. Every paste saves an `auto:before-paste` snapshot first. Notes for consistent sets:

- Develop every frame of a series with the **same mode and art** (`photo develop *.CR3 --mode events --art portra_400`),
  then fix the exceptions.
- Copy only what is meant to be shared. `--ops hsl_mixer,color_balance,tone_map,vibrance` shares the look. Do **not**
  paste `exposure`, `white_balance` temperature or masked entries (their coordinates are image-specific) across
  frames with different lighting.
- **There is no automatic exposure or white-balance matching command for stills** (video has `--auto exposure|wb`).
  To match a set: render previews, compare with `photo scopes` or `photo analyze`, then set `exposure.ev` per frame or
  use `white_balance.picker=[x,y,w,h]` (a normalised rectangle on a neutral patch) or `white_balance.auto=grayworld`.
- Re-check one or two frames from the middle of the batch at full size after a paste.

## Versions and snapshots

```bash
photo versions list IMG.CR3
photo versions new IMG.CR3 bw --from default         # virtual copy; becomes active unless --no-switch
photo versions switch IMG.CR3 default                # the active version is what render, export and the web editor use
photo versions rename IMG.CR3 bw "bw_contrast"
photo versions delete IMG.CR3 bw_contrast
photo versions snapshot IMG.CR3 "client_v1"          # named history point of the active version
photo versions snapshots IMG.CR3                     # includes automatic auto:* snapshots
photo versions restore IMG.CR3 client_v1             # restore into the active version (--version V to target another)
```

Automatic snapshots are taken before `edit`, `develop`, `reset`, `paste-settings` and `restore`. The web editor shows the same
versions and snapshots.

## Catalog maintenance

```bash
photo catalog rebuild                         # drop and rebuild the DB from sidecars + XMP (--drop-analysis, --hash)
photo catalog rewrite-sidecars @all           # re-write sidecar + XMP from the catalog
photo catalog export /tmp/cat_dump --formats json,csv,tags,albums --xmp    # open formats (also --sqlite)
photo catalog import /tmp/cat_dump            # re-apply ratings/tags/albums by relative path
photo thumbs --sizes 256,1024                 # rebuild cached thumbnails (keyed by edit recipe)
photo thumbs --prune                          # delete thumbnails of recipes that no longer exist
```

If the catalog looks wrong (missing images, stale ratings), `photo catalog rebuild` is safe. Edits made by the CLI or
the web editor are in sidecars, so nothing is lost.
