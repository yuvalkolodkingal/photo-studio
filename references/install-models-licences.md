# Install, optional models and licences

## Contents

- [One-line install](#one-line-install)
- [What the installer does](#what-the-installer-does)
- [Extras (pip optional dependencies)](#extras-pip-optional-dependencies)
- [System tools on Fedora](#system-tools-on-fedora)
- [Optional AI models](#optional-ai-models)
- [GPU notes (RTX 4050, 6 GB)](#gpu-notes-rtx-4050-6-gb)
- [Licences](#licences)
- [Where things live](#where-things-live)

## One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/yuvalkolodkingal/photo-studio/main/install.sh | bash
```

It clones the repository into `~/.claude/skills/photo-studio`, creates `.venv` there, installs the package with the
default extras, links `photo` into `~/.local/bin`, and finishes with `photo status`. It never runs `sudo`; it prints
the Fedora commands for the system packages it found missing. Run it again at any time to update (it is idempotent).

Options (flags or environment variables):

| flag | environment | meaning |
|---|---|---|
| `--dir DIR` | `PHOTO_STUDIO_DIR` | install directory (default `~/.claude/skills/photo-studio`, or the checkout you run it from) |
| `--extras LIST` | `PHOTO_STUDIO_EXTRAS` | comma separated pip extras; default `serve,meta,lens,video,heif,astro,geo`; `""` for none |
| `--no-link` | | do not create `~/.local/bin/photo` |
| `--uninstall` | | remove the `.venv` and the link; keeps code, photos and settings |
| | `PHOTO_STUDIO_PYTHON` | interpreter to use (must be 3.11 or newer) |
| | `PHOTO_STUDIO_REPO`, `PHOTO_STUDIO_BRANCH` | another repository or branch |

```bash
curl -fsSL https://raw.githubusercontent.com/yuvalkolodkingal/photo-studio/main/install.sh | bash -s -- --extras serve,meta,lens
PHOTO_STUDIO_EXTRAS="serve,meta,lens,video,heif,astro,geo,ai" ~/.claude/skills/photo-studio/install.sh
```

Manual alternative:

```bash
git clone https://github.com/yuvalkolodkingal/photo-studio ~/.claude/skills/photo-studio
cd ~/.claude/skills/photo-studio
python3.12 -m venv .venv && .venv/bin/pip install -e ".[serve,meta,lens,video,heif,astro,geo]"
.venv/bin/photo status
```

Python 3.11 or newer is required. On Fedora: `sudo dnf install python3.12` (Claude must not run sudo; tell the user).

If `pip install -e .` ever leaves `photo status` crashing with `AttributeError: module 'photostudio' has no attribute
'__version__'`, a stale `photostudio.egg-info` from an older checkout is confusing the editable install: delete that
folder and re-run the installer (the installer does this itself).

## What the installer does

1. Finds Python 3.11 or newer (`python3.13`, `3.12`, `3.11`, `python3`).
2. Uses the local checkout if run from one, otherwise `git clone --depth 1`, otherwise a tarball download; updates an
   existing clone with `git pull --ff-only`.
3. Creates or reuses `.venv`, upgrades pip, installs the base package, then the extras. If the extras fail together
   it retries them one by one and lists the ones that failed.
4. Symlinks `~/.local/bin/photo` (unless `--no-link`), and links the skill into `~/.claude/skills/photo-studio` when
   installed elsewhere and that path is free.
5. Prints hints for missing system tools and runs `photo status`.

## Extras (pip optional dependencies)

The base install (rawpy, numpy, scipy, OpenCV, Pillow, colour-science, scikit-image, BLAKE3, tifffile, imagehash, PyYAML)
covers decode, develop, edit, export, library and cull basics.

<!-- BEGIN GENERATED: extras-table -->
| extra | packages | notes |
|---|---|---|
| `meta` | pyexiftool>=0.5.6 | PyExifTool wrapper; needs the exiftool binary (`perl-Image-ExifTool`) |
| `lens` | lensfunpy>=1.15 | lensfunpy; the skill ships a patched lensfun DB with the R50 and RF-S kit lenses |
| `video` | av>=12, scenedetect>=0.6 | PyAV + PySceneDetect (shot detection, frame access); ffmpeg binary still required |
| `heif` | pillow-heif>=0.16 | HEIF/AVIF export |
| `jxl` | pillow-jxl-plugin>=1.3 | JPEG XL export |
| `astro` | astroalign>=2.5 | astroalign for star registration |
| `geo` | reverse_geocoder>=1.5 | offline reverse geocoding for places tags |
| `fast` | numba>=0.59, xxhash>=3.4 | numba (fast LUTs) + xxhash |
| `denoise` | bm3d>=4.0 | BM3D: non-commercial licence, personal use only |
| `ai` | onnxruntime>=1.17, rembg>=2.0.50, mediapipe>=0.10, open_clip_torch>=2.24, transformers>=4.40 | onnxruntime, rembg, mediapipe, open_clip, transformers; model weights separate (`photo models download`) |
| `yolo` | ultralytics>=8.1 | Ultralytics YOLO: AGPL-3.0, personal use OK, do not redistribute combined works |
| `faces` | insightface>=0.7.3 | InsightFace; weights are non-commercial research only (manual download) |
| `serve` | fastapi, uvicorn | fastapi + uvicorn for the browser editor (`photo serve`); not part of `all`, the installer adds it by default |
| `dev` | pytest>=8, ruff>=0.4 | pytest, ruff |
| `all` | photostudio[serve,meta,lens,video,heif,astro,geo,fast] | meta, lens, video, heif, astro, geo, fast (does NOT include serve, jxl, denoise, ai, yolo, faces) |
| `everything` | photostudio[all,ai,jxl,denoise,yolo,faces] |  |
<!-- END GENERATED: extras-table -->

Install more later with `.venv/bin/pip install -e ".[ai]"` from the skill directory. The `ai` extra pulls in PyTorch-based
packages and is several gigabytes; install it only when the user wants AI masks, CLIP tags or aesthetic scoring.

## System tools on Fedora

Claude never runs these; print them for the user.

```bash
sudo dnf install perl-Image-ExifTool          # exiftool: full Canon CR3 maker notes and XMP writing
sudo dnf install python3.12                    # if no Python 3.11+ is present
# ffmpeg with all codecs (video) comes from RPM Fusion:
sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
sudo dnf install ffmpeg --allowerasing
sudo dnf install rclone restic                 # backups to cloud or versioned metadata
sudo dnf install hugin enblend                 # optional: full-resolution panoramas (not driven by this skill)
sudo dnf install gcc gcc-c++ python3-devel lensfun-devel   # only if the lens extra must compile
```

`photo status` shows which binaries are found. Without `exiftool` the skill still reads the metadata it needs through
rawpy and Pillow, with fewer maker-note fields (mode auto-detection gets weaker).

## Optional AI models

Nothing is downloaded automatically. Weights are fetched only by an explicit command:

```bash
photo models list                               # registry, install state, licences
photo models download isnet-general-use         # subject / background masks (default matte model)
photo models download depth-anything-v2-small face-landmarker segformer-ade-b0
photo models download all                       # everything in the registry (about 2 GB); ask first
```

Models go to `~/.cache/photostudio/models/`. Each needs its Python packages (`ai` extra: onnxruntime, rembg, mediapipe,
open_clip, transformers; `yolo` extra for the animal mask). Without a model the mask falls back to a simple method and
says so in the render log.

<!-- BEGIN GENERATED: models-table -->
| name | used by | licence | size (MB) | needs python modules |
|---|---|---|---|---|
| `big-lama` | inpaint op: LaMa large-mask inpainting | Apache-2.0 (LaMa; verify LICENSE) | 200 | simple_lama_inpainting |
| `birefnet-general` | ai_subject: best matte quality (hair, fur) | MIT (BiRefNet) | 970 | onnxruntime |
| `birefnet-general-lite` | ai_subject: high-quality matte, hair/foliage (lite) | MIT (BiRefNet) | 224 | onnxruntime |
| `depth-anything-v2-small` | depth mask / background blur: monocular depth (ViT-S, 25M params) | Apache-2.0 | 100 | onnxruntime |
| `depth-anything-v2-small-hf` | depth: Depth Anything V2 Small, transformers/torch build (fp16 on CUDA) | Apache-2.0 | 100 | transformers, torch |
| `face-landmarker` | ai_face/eyes/lips/teeth/skin: 478 face landmarks (MediaPipe) | Apache-2.0 (MediaPipe) | 4 | mediapipe |
| `haar-eye` | ai_eyes fallback: OpenCV Haar eye cascade | BSD (OpenCV) | 1 | cv2 |
| `haar-face` | ai_face fallback: OpenCV Haar frontal-face cascade | BSD (OpenCV) | 1 | cv2 |
| `isnet-general-use` | ai_subject / ai_background: salient-object matte (default) | code MIT (rembg); IS-Net weights Apache-2.0 upstream | 170 | onnxruntime |
| `segformer-ade-b0` | ai_sky: SegFormer-B0 ADE20K (class 2 = sky) | NVIDIA SegFormer licence (research / personal use) (do not redistribute) | 15 | transformers, torch |
| `u2net` | ai_subject: lighter salient-object matte | Apache-2.0 (U^2-Net) | 176 | onnxruntime |
| `yolo11n-seg` | ai_animal: YOLO11 nano instance segmentation (COCO animals) | AGPL-3.0 (Ultralytics): personal use OK, do not redistribute combined works (do not redistribute) | 6 | ultralytics |
<!-- END GENERATED: models-table -->

Library AI that is **not in this registry** (CLIP ViT-L/14, the LAION aesthetic head, InsightFace `buffalo_l`, Florence-2,
Recognize Anything) is looked up under `~/.cache/photostudio/models/` and must be placed there by hand; without them
`photo tag auto --tier clip|ram|caption`, `photo analyze --aesthetic` and `photo tag people` stop with an error naming the missing
package. Say so instead of retrying.

## GPU notes (RTX 4050, 6 GB)

- Colour and tone processing is CPU numpy; the GPU only helps AI models and NVENC video encodes.
- The registry models are small (the largest, BiRefNet, is under 1 GB on disk). Models load on demand; use
  `PHOTOSTUDIO_CPU=1` to force CPU if VRAM runs short. Peak VRAM was not measured here.
- For ONNX on CUDA install `onnxruntime-gpu` **instead of** `onnxruntime` (never both). The `ai` extra installs the CPU
  build; swap it manually if the user wants GPU masks, and verify with `photo status` (the `gpu` line).
- `nvidia-smi` is reported by `photo status`; it was not available where this documentation was written, so GPU paths
  are unverified.

## Licences

The skill bundles **no model weights, no lookup tables from third parties and no film-maker data.** Film names are generic
descriptive labels for emulations, and artist-inspired looks are generic styles. Personal use is the assumed context.

| component | licence | what that means |
|---|---|---|
| photo-studio code | MIT | |
| rawpy / LibRaw | LGPL / CDDL | used as a library |
| InsightFace weights (`faces` extra) | non-commercial research only | personal use only; never bundled; downloaded manually |
| Ultralytics YOLO (`yolo` extra) | AGPL-3.0 | fine for personal use; do not redistribute combined works |
| BM3D (`denoise` extra) | non-commercial | personal use only; default denoiser has no such limit |
| SegFormer ADE20K | research / personal (NVIDIA) | sky masks |
| IS-Net, U2-Net, BiRefNet, Depth Anything V2 Small, MediaPipe, LaMa | Apache-2.0 or MIT (verify each LICENSE before commercial use) | |
| CLIP, LAION aesthetic head, Florence-2, Recognize Anything | see each project | manual download, not in the registry |
| lensfun database | CC BY-SA | a small Canon mirrorless camera and lens patch file ships in `photostudio/data/lensfun/` |

If the user ever wants to sell prints or services using output from the non-commercial components (InsightFace face
grouping, BM3D), flag it. Face recognition and naming people is a privacy-sensitive feature: do not run `tag people`
unless asked, and keep the results local (they live in the catalog and sidecars on this machine).

## Where things live

| what | path |
|---|---|
| skill, code, venv | `~/.claude/skills/photo-studio/` (`.venv/bin/photo`) |
| user presets | `~/.config/photostudio/presets/{modes,art,export,video,picture_styles}/` |
| models and caches | `~/.cache/photostudio/` (models in `models/`, default `develop` output in `develop/`) |
| data (settings clipboard, user LUTs in `luts/`) | `~/.local/share/photostudio/` |
| per library | `<root>/.photostudio/` (catalog.sqlite, backup.yaml, snapshots, imports, manifest) |
| per image | `<image>.ps.json` (+ `.bak`) and `<image>.xmp` next to the file |

Library-AI weights are looked up here (place them by hand; nothing downloads them):

```text
~/.cache/photostudio/models/clip-vit-l-14/ViT-L-14.pt              (or env PHOTOSTUDIO_CLIP_WEIGHTS)
~/.cache/photostudio/models/laion-aesthetic/sa_0_4_vit_l_14_linear.pth
~/.cache/photostudio/models/insightface/models/buffalo_l/
~/.cache/photostudio/models/florence-2-base/
~/.cache/photostudio/models/ram/ram_plus_swin_large_14m.pth
```

Environment variables (all optional):

| variable | effect |
|---|---|
| `XDG_CONFIG_HOME` | base for user presets (`$XDG_CONFIG_HOME/photostudio/presets`) |
| `PHOTOSTUDIO_PRESETS` | extra preset folders, separated by `:`; they override the built-ins |
| `PHOTOSTUDIO_HOME`, `PHOTOSTUDIO_CACHE` | move the data and cache folders |
| `PHOTOSTUDIO_ROOT` | default library root (also used by `photo serve`) |
| `PHOTOSTUDIO_FFMPEG`, `PHOTOSTUDIO_FFPROBE` | use another ffmpeg or ffprobe binary |
| `PHOTOSTUDIO_CPU` | force CPU for ONNX and torch models |
| `PHOTOSTUDIO_LUTS` | extra folder searched for `.cube` and Hald LUTs |
| `PHOTOSTUDIO_CLIP_WEIGHTS`, `PHOTOSTUDIO_TAG_VOCAB` | CLIP weight file; your own tag vocabulary YAML |
| `PHOTOSTUDIO_SRC_CACHE_MB`, `PHOTOSTUDIO_STEP_CACHE_MB` | in-memory render caches (defaults 1500 and 768 MB); lower them on a 16 GB machine if other apps need the RAM |
| `PHOTOSTUDIO_DEBUG` | same as `--debug` |
