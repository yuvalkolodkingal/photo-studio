#!/usr/bin/env bash
# photo-studio installer: works piped from curl and from a local checkout. Idempotent. Never runs sudo.
#
#   curl -fsSL https://raw.githubusercontent.com/yuvalkolodkingal/photo-studio/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/yuvalkolodkingal/photo-studio/main/install.sh | bash -s -- --extras serve,meta
#   ./install.sh [--dir DIR] [--extras LIST] [--no-link] [--uninstall] [--help]
#
# Environment (flags win over environment):
#   PHOTO_STUDIO_DIR      install directory (default: $HOME/.claude/skills/photo-studio, or the checkout when run from one)
#   PHOTO_STUDIO_EXTRAS   comma separated pip extras (default: serve,meta,lens,video,heif,astro,geo; "" = none)
#   PHOTO_STUDIO_REPO     repository URL (default: https://github.com/yuvalkolodkingal/photo-studio)
#   PHOTO_STUDIO_BRANCH   branch (default: main)
#   PHOTO_STUDIO_PYTHON   python interpreter to use (default: first of python3.13, 3.12, 3.11, python3 that is >= 3.11)
set -euo pipefail

REPO_URL="${PHOTO_STUDIO_REPO:-https://github.com/yuvalkolodkingal/photo-studio}"
BRANCH="${PHOTO_STUDIO_BRANCH:-main}"
DEFAULT_EXTRAS="serve,meta,lens,video,heif,astro,geo"
EXTRAS="${PHOTO_STUDIO_EXTRAS-$DEFAULT_EXTRAS}"
DIR="${PHOTO_STUDIO_DIR:-}"
LINK=1
UNINSTALL=0
BIN_DIR="${HOME}/.local/bin"

say()  { printf '%s\n' "$*"; }
info() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
photo-studio installer (idempotent, never runs sudo)

  curl -fsSL https://raw.githubusercontent.com/yuvalkolodkingal/photo-studio/main/install.sh | bash
  curl -fsSL https://raw.githubusercontent.com/yuvalkolodkingal/photo-studio/main/install.sh | bash -s -- --extras serve,meta
  ./install.sh [--dir DIR] [--extras LIST] [--no-link] [--uninstall]

Options:
  --dir DIR       install directory (default: ~/.claude/skills/photo-studio, or the checkout you run it from)
  --extras LIST   pip extras, comma separated; "" for none (default: serve,meta,lens,video,heif,astro,geo)
  --no-link       do not symlink `photo` into ~/.local/bin
  --uninstall     remove the .venv and the ~/.local/bin/photo link (keeps the code, your photos and your settings)
  -h, --help      this help

Environment: PHOTO_STUDIO_DIR, PHOTO_STUDIO_EXTRAS, PHOTO_STUDIO_REPO, PHOTO_STUDIO_BRANCH, PHOTO_STUDIO_PYTHON
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)       [ $# -ge 2 ] || die "--dir needs a value"; DIR="$2"; shift 2 ;;
    --dir=*)     DIR="${1#*=}"; shift ;;
    --extras)    [ $# -ge 2 ] || die "--extras needs a value"; EXTRAS="$2"; shift 2 ;;
    --extras=*)  EXTRAS="${1#*=}"; shift ;;
    --no-link)   LINK=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown option: $1 (try --help)" ;;
  esac
done

case "$EXTRAS" in *[!a-zA-Z0-9_,-]*) die "--extras may only contain letters, digits, commas, '-' and '_'" ;; esac

# ---------------------------------------------------------------- where are we running from?
CHECKOUT=""
src="${BASH_SOURCE[0]:-}"
if [ -n "$src" ] && [ -f "$src" ]; then
  here="$(cd "$(dirname "$src")" && pwd -P)"
  if [ -f "$here/pyproject.toml" ] && [ -f "$here/SKILL.md" ] && [ -d "$here/photostudio" ]; then
    CHECKOUT="$here"
  fi
fi

if [ -z "$DIR" ]; then
  if [ -n "$CHECKOUT" ]; then DIR="$CHECKOUT"; else DIR="$HOME/.claude/skills/photo-studio"; fi
fi
case "$DIR" in "~"*) DIR="$HOME${DIR#"~"}" ;; esac
mkdir -p "$(dirname "$DIR")"
DIR="$(cd "$(dirname "$DIR")" && pwd -P)/$(basename "$DIR")"
VENV="$DIR/.venv"
LINK_PATH="$BIN_DIR/photo"

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
  info "Uninstalling from $DIR"
  if [ -L "$LINK_PATH" ] && [ "$(readlink "$LINK_PATH")" = "$VENV/bin/photo" ]; then
    rm -f "$LINK_PATH"; say "removed $LINK_PATH"
  fi
  if [ -d "$VENV" ]; then rm -rf "$VENV"; say "removed $VENV"; else say "no virtual environment at $VENV"; fi
  say "kept: $DIR (code), ~/.config/photostudio (your presets), ~/.cache/photostudio (models, thumbnails) and all photos."
  say "to remove the code as well: rm -rf '$DIR'"
  exit 0
fi

# ---------------------------------------------------------------- python >= 3.11
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }
PY=""
if [ -n "${PHOTO_STUDIO_PYTHON:-}" ]; then
  py_ok "$PHOTO_STUDIO_PYTHON" || die "PHOTO_STUDIO_PYTHON=$PHOTO_STUDIO_PYTHON is missing or older than Python 3.11"
  PY="$PHOTO_STUDIO_PYTHON"
else
  for cand in python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then PY="$cand"; break; fi
  done
fi
if [ -z "$PY" ]; then
  cat >&2 <<'EOF'
error: Python 3.11 or newer is required and was not found.
On Fedora:   sudo dnf install python3.12
Then re-run this installer (or set PHOTO_STUDIO_PYTHON=/usr/bin/python3.12).
EOF
  exit 1
fi
"$PY" -c 'import venv, ensurepip' >/dev/null 2>&1 || die "the venv/ensurepip module is missing for $PY (Fedora: sudo dnf install python3-pip python3-devel)"
info "Using $("$PY" --version 2>&1) ($(command -v "$PY"))"

# ---------------------------------------------------------------- get the code
fetch_tarball() {
  command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "need git, curl or wget to download the code"
  command -v tar >/dev/null 2>&1 || die "tar is required to unpack the download"
  url="${REPO_URL%.git}/archive/refs/heads/$BRANCH.tar.gz"
  mkdir -p "$DIR"
  info "Downloading $url"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" | tar -xz --strip-components=1 -C "$DIR"
  else
    wget -qO- "$url" | tar -xz --strip-components=1 -C "$DIR"
  fi
}

if [ -n "$CHECKOUT" ] && [ "$DIR" = "$CHECKOUT" ]; then
  info "Using the local checkout at $DIR"
elif [ -d "$DIR/.git" ]; then
  command -v git >/dev/null 2>&1 || die "$DIR is a git checkout but git is not installed"
  info "Updating $DIR (git pull --ff-only)"
  git -C "$DIR" pull --ff-only || warn "could not fast-forward $DIR (local changes?); continuing with the current code"
elif [ -f "$DIR/pyproject.toml" ] && [ -d "$DIR/photostudio" ] && ! command -v git >/dev/null 2>&1; then
  info "Refreshing $DIR from the tarball (git not found)"
  fetch_tarball
elif [ -f "$DIR/pyproject.toml" ] && [ -d "$DIR/photostudio" ]; then
  info "Using the existing copy at $DIR (not a git checkout, left unchanged)"
elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  die "$DIR exists and is not empty and is not photo-studio; choose another place with --dir"
elif command -v git >/dev/null 2>&1; then
  info "Cloning $REPO_URL into $DIR"
  if ! GIT_TERMINAL_PROMPT=0 git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DIR"; then
    die "git clone failed. Check the URL ($REPO_URL), your network, and that the repository is public."
  fi
else
  fetch_tarball
fi
[ -f "$DIR/pyproject.toml" ] || die "no pyproject.toml in $DIR; the download did not work"

# ---------------------------------------------------------------- virtual environment
if [ -x "$VENV/bin/python" ] && py_ok "$VENV/bin/python"; then
  info "Reusing the virtual environment at $VENV"
else
  if [ -e "$VENV" ]; then warn "replacing a broken or old virtual environment at $VENV"; rm -rf "$VENV"; fi
  info "Creating $VENV"
  "$PY" -m venv "$VENV"
fi
PIP=(env PYTHONWARNINGS=ignore "$VENV/bin/python" -m pip --disable-pip-version-check -q)
"${PIP[@]}" install --quiet --upgrade pip setuptools wheel || warn "could not upgrade pip (offline?); continuing"

# a stale egg-info from an older checkout can make `import photostudio` resolve as an empty namespace package
rm -rf "$DIR/photostudio.egg-info"

info "Installing photostudio (base dependencies first; this downloads a few hundred MB the first time)"
( cd "$DIR" && "${PIP[@]}" install -e . ) || die "base install failed; see the pip output above"

if [ -n "$EXTRAS" ]; then
  info "Installing extras: $EXTRAS"
  if ! ( cd "$DIR" && "${PIP[@]}" install -e ".[$EXTRAS]" ); then
    warn "some extras failed together; retrying one by one so a single failure does not block the rest"
    IFS=',' read -r -a list <<<"$EXTRAS"
    failed=()
    for x in "${list[@]}"; do
      [ -n "$x" ] || continue
      if ! ( cd "$DIR" && "${PIP[@]}" install -e ".[$x]" ) >/dev/null 2>&1; then failed+=("$x"); fi
    done
    if [ "${#failed[@]}" -gt 0 ]; then
      warn "extras that could not be installed: ${failed[*]}"
      say "  lens needs a compiler and headers on some systems: sudo dnf install gcc gcc-c++ python3-devel lensfun-devel"
      say "  retry later with: $VENV/bin/pip install -e '$DIR[<extra>]'"
    fi
  fi
fi

# ---------------------------------------------------------------- command link
if [ "$LINK" = 1 ]; then
  mkdir -p "$BIN_DIR"
  if [ -e "$LINK_PATH" ] && [ ! -L "$LINK_PATH" ]; then
    warn "$LINK_PATH exists and is not a symlink; leaving it alone"
  else
    ln -sf "$VENV/bin/photo" "$LINK_PATH"
    info "Linked $LINK_PATH -> $VENV/bin/photo"
    case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "    add $BIN_DIR to your PATH to use plain 'photo' (the skill itself uses the full venv path)" ;; esac
  fi
fi

# ---------------------------------------------------------------- Claude skill location
SKILL_LINK="$HOME/.claude/skills/photo-studio"
if [ "$DIR" != "$SKILL_LINK" ]; then
  if [ ! -e "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    mkdir -p "$(dirname "$SKILL_LINK")"
    ln -s "$DIR" "$SKILL_LINK"
    info "Linked the skill: $SKILL_LINK -> $DIR"
  else
    say "note: $SKILL_LINK already exists; to use this copy as the Claude skill: ln -sfn '$DIR' '$SKILL_LINK'"
  fi
fi

# ---------------------------------------------------------------- system package hints (never runs sudo)
missing=()
command -v exiftool >/dev/null 2>&1 || missing+=("exiftool")
command -v ffmpeg  >/dev/null 2>&1 || missing+=("ffmpeg")
if [ "${#missing[@]}" -gt 0 ]; then
  say ""
  say "Optional system tools not found: ${missing[*]}"
  for m in "${missing[@]}"; do
    case "$m" in
      exiftool) say "  exiftool (full Canon CR3 metadata):  sudo dnf install perl-Image-ExifTool" ;;
      ffmpeg)   say "  ffmpeg (video; Fedora's ffmpeg-free lacks some codecs, use RPM Fusion):"
                say "      sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-\$(rpm -E %fedora).noarch.rpm"
                say "      sudo dnf install ffmpeg --allowerasing" ;;
    esac
  done
  say "  also useful for backups: sudo dnf install rclone restic"
fi

# ---------------------------------------------------------------- finish
say ""
info "Health check: photo status"
"$VENV/bin/photo" status || warn "photo status reported a problem; see troubleshooting in references/troubleshooting.md"
say ""
cat <<EOF
Done. photo-studio is installed in $DIR

Next steps:
  $VENV/bin/photo status                      # what is available (binaries, GPU, models, presets)
  $VENV/bin/photo serve ~/Pictures --open     # the local web editor in your browser
  restart Claude Code so it picks up the skill, then ask it to import, cull or develop your photos

Update later:      re-run the same install command (it is idempotent)
Uninstall:         $DIR/install.sh --uninstall
EOF
