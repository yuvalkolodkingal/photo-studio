# Astro stacking: research notes and design

`photo astro` stacks untracked night-sky frames the way Sequator does (load frames, align the stars, freeze the ground,
reduce light pollution, press Start), but its internals follow the methods used by Siril, DeepSkyStacker and PixInsight.
This page records which methods were considered, which were chosen, and why.

The design was driven by a real, difficult data set:

- 242 frames from a Canon EOS R50 (1.6 s, ISO 100, f/6.3, 64 mm, RF-S 18-150 mm) of Lyra from a light-polluted city;
- no tripod: the camera rested on something and was nudged twice;
- trees in the lower part of the frame;
- only Vega clearly visible in a single frame.

Siril's standard registration failed on it. It found one star per frame, and later matched about 2000 noise "stars"
in mini-stacks. Every choice below fixes a failure seen on that set.

## 1. What the existing tools do

| tool | registration | integration | finishing |
|---|---|---|---|
| Sequator (Windows, closed) | star detection plus local motion; "freeze ground" from a painted sky mask | average or "select best pixels" | light-pollution reduction, star enhancement, HDR, auto brightness |
| DeepSkyStacker | star triangles; bilinear or bisquared transforms | average, median, kappa-sigma, auto-adaptive weighted, entropy | basic levels |
| Siril | global star alignment (triangle matching), homography or shift; 2-pass; drizzle | sigma, winsorized, linear-fit and generalized-ESD rejection; noise weighting | background extraction, photometric (Gaia) colour calibration, GHS / asinh |
| PixInsight (WBPP) | StarAlignment (polygon matching, thin splines) | ImageIntegration with ESD / linear-fit, PSF-signal weights, local normalisation | DBE / ABE, SPCC, arcsinh and GHS stretches |
| astroalign (Python) | asterism (triangle invariants) plus RANSAC similarity | none | none |

The common ground:

1. Calibrate (darks, flats, bias).
2. Detect stars and match asterisms to get a geometric transform.
3. Integrate with per-pixel outlier rejection and noise weights.
4. Model and subtract the background.
5. Calibrate colour from stars.
6. Apply a non-linear, colour-preserving stretch.

## 2. Registration

**Star matching** (`--align stars`) is the default whenever the median frame has 12 or more *reliable* stars, meaning
at least 12 σ in the registration preview. The reliability filter matters: a plain k·MAD detector happily returns
hundreds of noise peaks on a city sky, which is exactly how Siril failed on the test set. Matching uses astroalign
when installed, otherwise rotation-grid translation voting followed by ICP (`multiframe.vote_align`). A RANSAC
homography is then fitted to every matched star. A similarity transform is not enough for wide-field lenses, where
projection and residual distortion bend star positions toward the corners.

**Mini-stacks plus ECC** (`--align ecc`) is for star-poor skies. One or two stars cannot constrain rotation or
perspective, and correlation-based methods lock onto static foreground. The engine does four things:

1. It measures frame-to-frame motion by tracking the brightest star: a local window around the predicted position,
   a global re-acquire after a nudge, and a flux-consistency check. When no single star dominates, it uses phase
   correlation of star previews instead, with a Hanning window and the ground masked.
2. It stacks short runs of frames (8 by default) after translation-aligning them. This raises the SNR by about
   √8, so faint stars appear and give ECC something to correlate with.
3. It registers each mini-stack to the reference mini-stack with *coarse-to-fine* ECC: translation, then Euclidean,
   then homography. Each step must raise the correlation and stay within a few pixels of the coarser solution, and
   the scale must stay at 1. Unguarded ECC homographies wandered into wrong local minima (scale 1.015, tens of pixels
   off) on sparse fields during development.
4. If a mini-stack still has too few stars for ECC, it falls back to the star track (translation only) instead of
   dropping frames.

**Static structure is the enemy.** Horizon edges and tree silhouettes correlate perfectly at zero shift and pull the
alignment toward "no motion". The sky mask used for registration is therefore hard and *eroded* by about 1.5% of the
frame away from the horizon. The softer mask is only used for compositing.

**Camera nudges** are detected from the motion track. A step counts as a nudge when it departs from the median drift
by more than `max(--nudge-px, 12 × median jitter)`. The adaptive part matters: on the hand-rested test set, ordinary
frame-to-frame jitter had a median of 1.6 px and often reached 10 px, while real nudges were 30–55 px. A fixed 4 px
threshold flagged nearly every frame. Steady runs shorter than 3 frames are treated as "camera moving" and dropped.
Every other frame is registered on its own, so jitter costs nothing.

**Lens correction** (`--lens`) runs lensfun (with the bundled RF-S data) before registration. Doing it first makes
the frame-to-frame transform closer to a pure homography.

Not implemented, but candidates for later:

- thin-plate-spline (local) registration, as in PixInsight, for very wide lenses;
- drizzle integration, which needs undersampled, dithered data (a hand-held sequence is naturally dithered);
- per-frame plate solving.

## 3. Integration

The choice was between a full per-pixel stack in memory (median, generalized ESD, linear-fit clipping) and a streaming
method. A 24 MP RGB frame is 288 MB as float32, so 242 frames would need 70 GB. The existing `multiframe._Store`
memmap also lives in `/tmp`, which is RAM-backed tmpfs on Fedora.

The engine uses **two streaming passes** over a process pool:

1. **Pass 1** accumulates the weighted Σx and Σx² per pixel, giving a mean and σ per pixel. It also accumulates the
   unaligned ground for "freeze ground".
2. **Pass 2** applies **kappa-sigma rejection** (a pixel is rejected in all channels together, so no colour fringes
   appear) or **winsorization** (clip to mean ± κσ) against the pass-1 statistics, and accumulates the result.
   - Weights are 1/σ²_noise per frame, measured on the star preview.
   - Two **independent half stacks** (alternate frames) are accumulated at the same time.

Warped frames are cached on disk as float16 between the passes when there is room, so every raw file is decoded only
once. float16 keeps about 3 significant digits, which is well below the per-frame noise.

The trade-off is that a single rejection iteration against a mean inflated by outliers is less strict than iterated
clipping. In practice satellite and plane trails are many σ out and still get rejected (see `tests/test_astro.py`).
`--reject max` gives a lighten / star-trail composite, and `--reject mean` gives a plain average.

The **worker count** is chosen from the number of cores and a RAM budget: about 7 full-frame planes per worker, plus
the raw decoder's overhead, against 60% of available memory by default. Accumulators are written to the work
directory, not sent back through pipes. Use `--workdir` on a real disk.

## 4. Finishing (`photostudio.astro.post`)

- **Light pollution:** star-clipped block medians on a grid, with outlier cells replaced by the median of their
  neighbours (so nebula and galaxy cores survive), then smoothed and subtracted per channel. This is an automatic
  version of DBE/ABE. Ground pixels do not influence the model.
- **Colour:**
  1. Neutralise the background.
  2. Run aperture photometry on unsaturated stars, subtracting the local background.
  3. Scale the channels so that the median star is white.

  This is a catalogue-free approximation of photometric colour calibration. SPCC against Gaia would be more correct
  but needs a plate solve and a catalogue download.
- **Stretch:** a colour-preserving arcsinh stretch of luminance.
  - **Auto brightness** solves for the strength that puts the sky median at `--brightness` (0.10 by default).
  - The black point sits 2 σ below the sky by default, so a faint natural noise floor stays visible instead of an
    artificial inky black.
  - **HDR** lowers the strength, keeps bright cores, adds a soft shoulder, and fades saturated cores to white instead
    of showing a false hue.
- **Enhance stars:** a screen-style lift through a soft star mask.
- **Noise reduction:** non-local means on luminance plus Gaussian smoothing on chroma, protected inside stars. NLM
  runs at 8 bits, the only depth every OpenCV build exposes, and the sub-8-bit residual is added back, so precision
  is kept and the quantisation acts as a dither.
- **Split-half validation** (`--validate`): a candidate counts as a real source only if it is above 6 σ in the full
  stack *and* above 3 σ at the same spot in *both* independent half stacks. The half test is also applied to random
  positions to report its false-alarm rate. This is how the original Lyra project separated 106 real faint stars from
  speckle.

## 5. GUI

Sequator's layout works because it maps to the workflow: inputs on the left, the picture in the middle, composition
switches on the right, and one Start button. `photo astro gui` copies that layout as a local web page. The project
already ships FastAPI for `photo serve`, Tk is often missing from Linux Python builds, and a browser page needs no
extra toolkit.

- File picking is server-side, limited to home, `/run/media`, `/media` and `/mnt`, plus `PHOTOSTUDIO_ASTRO_ROOTS`.
- The base frame is a click in the list.
- "Freeze ground: Horizon" draws a draggable line on the base frame.
- Progress and the log stream from the engine.
- After the stack, the finishing sliders re-render a 1600 px preview from the linear result in about a second.
  "Save full resolution" writes the 16-bit file plus the linear 32-bit TIFF.
- It binds to 127.0.0.1 only and reuses the editor's Host and Origin guards.

## 6. Verification

- **Synthetic tests** (`tests/test_astro.py`): star-rich and star-poor fields with a nudge. Every frame transform is
  checked against the truth to within 0.6 px, and noise must fall by about √N. They also cover moving-frame dropping,
  adaptive nudge thresholds, trail rejection, the maximal-rectangle crop, gradient removal, auto brightness, split-half
  validation, the CLI round trip and the GUI end to end.
- **Real data:** 40 Siril-converted FITS frames from the R50 Lyra set. All 40 were registered, the nudge before frame
  5 was found, the trees stayed sharp, and the run took 3 minutes on 3 workers within a 9 GB budget.
- **Not verified yet:** a full 242-frame run straight from CR3, `--lens` on real files, flats, and AI sky masks.
