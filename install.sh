#!/usr/bin/env bash
# First-time setup: a working venv and the `wf` launcher, then `wf install`
# does the rest (dependencies, /usr/local/bin/wf, completion, the claude
# alias, Claude skills). Any flag given here goes to `wf install`, e.g.
#   ./install.sh --system     # claude alias machine wide (asks for sudo)
# After this, `wf install` alone keeps a checkout set up.
set -euo pipefail

PROJECT_NAME="work-flow"
PKG_NAME="workflow"
VENV_DIR=".venv"
PYTHON_BIN="python3"

cd "$(dirname "${BASH_SOURCE[0]}")"
echo "🔧 Installing $PROJECT_NAME (macOS)"

# -----------------------------
# 1. Sanity checks
# -----------------------------
if ! command -v $PYTHON_BIN >/dev/null; then
  echo "❌ python3 not found."
  echo "Install it with: brew install python"
  exit 1
fi

if [ ! -d "$PKG_NAME" ]; then
  echo "❌ Expected Python package directory '$PKG_NAME/' not found"
  exit 1
fi

if [ ! -f "requirements.txt" ]; then
  echo "❌ requirements.txt not found"
  exit 1
fi

# -----------------------------
# 2. Create (or reuse) virtual environment
# -----------------------------
# A venv can go stale if it was created on a different machine/checkout path
# or its Python was since upgraded/removed (its interpreter symlinks and
# script shebangs are absolute paths baked in at creation time). Reuse the
# existing venv only if it still actually works; otherwise rebuild it.
if [ -d "$VENV_DIR" ] && ! "$VENV_DIR/bin/python" -c '' >/dev/null 2>&1; then
  echo "⚠️  Existing $VENV_DIR is broken (stale interpreter), recreating it"
  rm -rf "$VENV_DIR"
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "📦 Creating virtual environment"
  $PYTHON_BIN -m venv "$VENV_DIR"
else
  echo "📦 Reusing existing virtual environment at $VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# Ensure pip is available inside the venv (some python3 builds, e.g. the
# python.org/Homebrew "framework" build without ensurepip, create venvs
# missing pip entirely).
if ! python -m pip --version >/dev/null 2>&1; then
  echo "📦 pip not found in venv, bootstrapping via ensurepip"
  if ! python -m ensurepip --upgrade >/dev/null 2>&1; then
    echo "❌ Failed to bootstrap pip in the virtual environment."
    echo "   Try installing a Python with pip support, e.g.: brew install python"
    exit 1
  fi
fi

# -----------------------------
# 3. Install deps so `wf` can start (wf install repeats this; it's quick)
# -----------------------------
echo "⬆️  Installing dependencies from requirements.txt"
python -m pip install -q --upgrade pip wheel
python -m pip install -q -r requirements.txt
python -m pip install -q -e .

# -----------------------------
# 4. The venv-pinned wf launcher (tracked in git; recreated if missing)
# -----------------------------
if [ ! -x wf ]; then
  echo "🚀 Creating wf launcher"
  cat > wf <<'LAUNCHER'
#!/usr/bin/env bash
set -e
# Resolve the real path of this script, following symlinks (e.g. when
# invoked via /usr/local/bin/wf -> <install dir>/wf), so we can find the
# venv relative to the actual install directory rather than the symlink's.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
# Source the virtual environment from the original install directory
source "$SCRIPT_DIR/.venv/bin/activate"
exec python -m workflow.cli "$@"
LAUNCHER
  chmod +x wf
fi

# -----------------------------
# 5. Everything else
# -----------------------------
exec ./wf install "$@"
