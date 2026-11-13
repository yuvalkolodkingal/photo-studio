# Web editor (`photo serve`)

A Lightroom-style editor in the browser for the same library the CLI uses. It runs on this computer only (binds to
`127.0.0.1`, rejects other Host headers, works offline). It reads and writes the **same `<image>.ps.json` sidecars** as
the CLI, so a person can adjust sliders by hand while Claude edits with `photo edit`, in either direction.

## Contents

- [Starting and stopping](#starting-and-stopping)
- [Views and panels](#views-and-panels)
- [Shortcuts](#shortcuts)
- [Hand-off between Claude and the person](#hand-off-between-claude-and-the-person)
- [Versions, snapshots, history](#versions-snapshots-history)
- [HTTP API](#http-api)
- [Limits](#limits)

## Starting and stopping

```bash
photo serve ~/Pictures/Library --open            # open in the default browser
photo serve ~/Pictures/Library --port 8765       # default port 8765; the next free port is used if busy
photo serve ~/Pictures/Library --strict-port     # fail instead of picking another port
photo serve ~/Pictures/Library --no-catalog      # plain folder listing instead of the SQLite catalog
```

The root defaults to `--root`, `$PHOTOSTUDIO_ROOT`, the nearest folder with a catalog, else the current directory. The
server prints its URL (for example `http://127.0.0.1:8765/`) and runs until Ctrl+C.

How Claude should run it: start it in the background (for example with `run_in_background`), read the printed URL
from its output, tell the user the address, and **leave it running** while they work. Do not kill a server you did not
start. If another process holds the port the server picks the next free one (read the printed URL, not the default).
Opening a browser (`--open`) only makes sense when the user is at the machine.

## Views and panels

- **Library**: folder tree, albums and tags, a filter bar (stars, flags, colour labels, text and filter-DSL query,
  edited-only, sort and order), thumbnails, and four layouts: Grid, Compare (two photos), Survey (selected photos),
  Cull (best-of-burst view). **Run cull** scores sharpness, exposure and faces and groups bursts; it only
  **suggests** picks and rejects, and nothing is changed until the person applies the suggestions. Ratings, flags and
  labels are written to sidecar and XMP. Photos can be dragged onto albums. Export from the top bar.
- **Develop**: the image with zoom (Fit, Fill, 1:1, 2:1), before/after (After, Split, Before), undo and redo, a live
  histogram and scopes, and a filmstrip. The left panel has Presets (mode, art, user presets, with hover previews and variants,
  plus "Auto-detect mode"), Versions, Snapshots, History and Copy settings (Copy, Paste, "Sync to selected"). The
  right panels: Basic, Tone curve, HSL, Colour grading, Detail, Lens, Effects, Geometry (crop with aspect presets and
  horizon), Retouch (spot heal and clone), Black and white, Art, Masking (linear, radial, brush, and AI masks with an
  overlay) and **All ops** (every registered op with its params). Edits autosave to the sidecar.

The sliders are generated from the same op registry as the CLI, so every `--set op.param=value` has a slider or a field in
"All ops".

## Shortcuts

Everywhere: `?` list, `G` library, `D` or `Enter` open in Develop, `0`-`5` rating, `P` `X` `U` pick, reject, clear flag,
`6`-`9` red, yellow, green, blue label, `Ctrl+E` export, `Ctrl+Shift+C` and `Ctrl+Shift+V` copy and paste settings.

Library: arrows move, Shift or Ctrl click selects, `Ctrl+A` all, `G` `C` `N` `B` for grid, compare, survey, cull.

Develop: Left and Right previous and next photo, `Ctrl+Z` and `Ctrl+Shift+Z` undo and redo, hold `\` to see the
original, `Y` split view, `Z` or double click toggles fit and 100 percent, wheel zooms, `C` crop (`Enter` or `Esc` to
leave, `X` flips orientation), `Q` spot heal or clone (`Delete` removes a spot), `W` white-balance picker, `O` mask
overlay, `J` clipping overlay, `Tab` hides panels, double-click a slider label to reset it, hold `Alt` while brushing to erase.

## Hand-off between Claude and the person

The recommended workflow for a creative edit:

1. Claude makes a first pass with the CLI (mode, art, tweaks), reading its own renders (see SKILL.md).
2. Claude starts `photo serve ROOT` (or confirms it is running) and tells the person which image and which version to open.
3. The person fine-tunes by hand: crop, spot removal, brush masks, slider taste. These are saved to the sidecar.
4. Claude reads the result back with `photo edit IMG` (no edits shows the stack) or `photo versions list IMG`, renders and
   critiques, or exports.

Both sides stay in sync:

- If Claude edits a sidecar while the image is open in the editor, the editor detects the change (external-change
  watcher) and reloads that image.
- If both changed the same image, the server answers a save with a **409 conflict** and shows the newer on-disk
  stack; the person chooses. The CLI does not lock sidecars, so avoid editing the image the person is currently
  dragging sliders on. Use a separate version (`--version claude_v2`) when in doubt, so you never overwrite a hand edit.
- Every change that replaces a stack takes an automatic snapshot first (`auto:before-develop`, `auto:before-edit`,
  `auto:before-paste`, and so on), so hand edits can be restored with `photo versions restore IMG auto:...` or in the
  Snapshots panel.
- The person's rating, flag, label and tags changed in the library view are written to the same sidecar and XMP, and show up
  in `photo ls` after a `photo scan` (or immediately in the catalog the server uses).

## Versions, snapshots, history

Versions (virtual copies) and snapshots are the same objects as `photo versions ...`. The editor's History is a
per-session undo list (up to 200 steps) kept in the server's memory; it is not the same as snapshots, and it is lost when
the server stops. Name a snapshot (`photo versions snapshot IMG "client_ok"`) before handing an image to the person.

## HTTP API

The UI talks to a JSON API under `/api/` on the same port, for example `GET /api/info`, `GET /api/library/images`,
`GET /api/image?p=REL/PATH`, `PUT /api/image/stack`, `GET /api/render?...`, `GET /api/changes?since=N&wait=SECONDS`
(long-poll event log, events with `external: true` are CLI or Claude writes), `POST /api/watch`. It is meant for the
front end; prefer the CLI for scripted work. All paths (`p`) are relative to the served root.

## Limits

- Local only, single user. There is no login, because nothing but this computer can reach it.
- The server renders previews with the same engine as the CLI; heavy looks (slot C raster art) are slow in the editor too.
  The AI masks use the same models and the same non-AI fallbacks as the CLI (see [masks-and-retouch.md](masks-and-retouch.md)).
- Verified by the docs builder: server start, strict-port failure, Host-header rejection, `/api/info`, library and image
  endpoints, and the external-change event after a CLI edit (`external: true`). **Not verified:** the browser UI itself
  (clicking through panels, tools and the export dialog); the shortcut and panel lists above are read from the front-end
  source.
