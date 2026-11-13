# Culling: analyse, group, suggest, then apply

Culling is **explainable and non-destructive**. `photo cull` ranks images and prints a flag (`pick`, `maybe`, `reject`)
with a human-readable reason for each. It writes nothing until you re-run with `--apply`, and nothing is ever deleted:
`--move-rejects` moves files into `<root>/_rejects/`.

## Contents

- [The workflow](#the-workflow)
- [What the scores mean](#what-the-scores-mean)
- [Commands and flags](#commands-and-flags)
- [Grouping bursts and near-duplicates](#grouping-bursts-and-near-duplicates)
- [Applying safely](#applying-safely)
- [Honest limits](#honest-limits)

## The workflow

```bash
photo scan                                  # make sure the catalog knows the files (import does this for you)
photo analyze @last-import                  # sharpness, exposure, eyes, tilt, noise: cached in the catalog
photo group @last-import                    # bursts and near-duplicates
photo cull @last-import                     # SUGGEST: prints score, flag, reasons; writes nothing
photo cull @last-import --target 0.2        # keep about 20 percent (best of each group first)
# review: read the reasons, look at contact sheets of picks and of rejects
photo album create cull_picks --query "flag:pick"       # optional: gather for review
photo cull @last-import --apply --query "flag:none"    # APPLY, only to images you have not flagged by hand
```

Steps for Claude:

1. Run `cull` without `--apply` and read the table. Summarise the counts and the main issue types for the user.
2. Spot-check: look at one or two `reject` frames and at the picks of a burst (`photo contact-sheet` or
   `photo develop IMG --preview 800` and Read the JPEG) before trusting the flags. The user's taste decides ties.
3. Ask or state clearly before `--apply`. Flags the user set by hand are **kept**: the sidecar records who set each flag
   (`flag_source`: `user` or `cull`), and `--apply` only writes where there is no flag or the flag came from an earlier
   cull. The result reports `kept_user_flags`. Add `--overwrite` only when the user explicitly wants their own flags replaced.
4. Never use `--move-rejects` unless asked. It moves the raw, the JPEG and their sidecars to `_rejects/` (recoverable),
   then rescans. The catalog never indexes `_rejects/` (nor `exports/` or hidden folders), so moved frames disappear from
   `ls` until you move them back and run `photo scan`.

## What the scores mean

Each image gets a 0 to 100 score from weighted signals; the weights depend on the genre (`--genre portrait|landscape|
wildlife|sports|macro|night|general`, or detected automatically from the shoot):

- Sharpness: variance of the Laplacian and Tenengrad on a 1024 px preview (companion JPEG or embedded preview, never
  a raw render), the sharpest tiles as a subject proxy, face crops, and the **ratio to the sharpest frame in its
  burst**. Relative sharpness is the main blur test.
- Blur kind: gradient anisotropy says motion blur versus defocus (a heuristic, not validated).
- Exposure: highlight and shadow clipping (per CFA channel on raws, histogram on JPEGs), median lightness.
- Eyes: eye-aspect-ratio from MediaPipe landmarks, with a Haar fallback. A closed-eye frame is only a reject when a
  better frame of the same burst exists; alone it is a warning.
- Horizon tilt beyond 1.5 degrees (Hough lines), noise estimate, and an optional aesthetic score (`--aesthetic`,
  needs the optional CLIP model).

Flags without `--target`: a failed check or a clearly worse burst member becomes `reject`, the best of a group with
score 60 or more becomes `pick`, a score under 30 becomes `reject`, everything else `maybe`. With `--target 0.2` (or
`20`), the best-scored non-failing frames up to that share become `pick`.

`photo analyze --json --show 3` returns the raw metrics (`lap_var`, `sharp_peak`, `motion_anisotropy`, `noise`,
`clip_hi`, `median_lum`, `n_faces`, `ear_min`, `phash`, and so on) for the first N images. The metric names also feed the
filter DSL: `photo ls --query "issue:blurry"` lists images with that issue.

## Commands and flags

```text
photo analyze [images] [--force] [--aesthetic] [--show N]
photo cull    [images] [--target F] [--genre G] [--aesthetic] [--query Q] [--full]
                       [--apply [--overwrite] [--rate] [--label-maybe COLOUR] [--clear-maybe] [--move-rejects]
                                [--rejects-dir DIR] [--dry-run]]
photo group   [images] [--dry-run] [--full]
```

- `images`: paths, globs, `id:N`, `@all`, `@last-import`, `@album:NAME`, `@session:DATE`. With none given the whole
  catalog is used.
- `--query` takes the filter DSL (`rating>=4 AND tag:people EXCEPT flag:reject`, `flag:none`, `issue:blurry`, ...).
- `--apply --dry-run` prints what would change without writing.
- `--apply --overwrite` also replaces flags that were set by hand (and records the new flags as `cull`). Without it a
  manually flagged image is never re-flagged, cleared by `--clear-maybe`, or moved by `--move-rejects`.
- `--apply --rate` also gives **unrated** images 1 to 5 stars from the score (never overwrites an existing rating).
- `--apply --label-maybe yellow` colours the maybes so they are easy to review; `--clear-maybe` removes any flag from
  them.
- `--json` returns `{summary, results[], note}`; each result has `id, name, score, flag, group, reasons`.
- Analysis is cached; `--force` recomputes it. Changing the cull genre or target does not need re-analysis.

## Grouping bursts and near-duplicates

`photo group` stores burst and scene ids: frames within 0.5 s of each other (EXIF time) form a burst, and frames with a
perceptual-hash distance of 10 or less (or CLIP similarity 0.9 or more) form a scene. `cull` uses groups to rank "best of
N similar frames" and to explain rejects such as "near-duplicate: ranked 3/4 in its group; A_sharp.JPG scores 96 vs 47".
Groups are shown as `group` ids in the cull table. Use `photo stack` (see [multiframe.md](multiframe.md)) rather than
culling when the burst is meant to be combined (HDR, focus stack).

## Applying safely

- Suggest first, apply second. Always show the user the counts.
- Manual flags are protected by default (see above); `--query "flag:none"` still limits a run to unflagged frames, and
  `photo flag none IMG` resets a single image (it also forgets the flag's source).
- Flags go to the sidecar and the XMP next to the file, so Lightroom-style tools see them as pick and reject flags.
- To undo an apply, re-run `photo flag none @all --query "flag:reject"` (and the same for picks), or restore the
  moved files from `_rejects/` and run `photo scan`.
- `photo ls --query "flag:pick" --count` reports how many picks exist.

## Honest limits

- Thresholds are starting points tuned on synthetic data. They have **not been validated on real R50 files**; expect to
  tune per shoot (`cull` accepts per-call thresholds in the Python API, not on the CLI).
- Blur kind (motion versus defocus) and the eyes-closed test are heuristics. Without the MediaPipe model the face
  and eye checks use a Haar cascade or are skipped.
- Sharpness is judged on a 1024 px preview, so very fine focus errors on a high-resolution subject can pass.
- A frame that is deliberately dark, soft or high-key (night, motion-blur art, silhouette) gets flagged; those are
  the reasons the flags are suggestions only.
- Aesthetic scores are optional and subjective; do not present them as quality.
