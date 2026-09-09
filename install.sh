#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="work-flow"
PKG_NAME="workflow"
VENV_DIR=".venv"
PYTHON_BIN="python3"

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
# 3. Upgrade pip + install deps
# -----------------------------
echo "⬆️  Installing dependencies from requirements.txt"
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt

# -----------------------------
# 4. Install workflow package (editable)
# -----------------------------
echo "📁 Installing workflow package"
python -m pip install -e .

# -----------------------------
# 5. Create venv-pinned wf launcher
# -----------------------------
echo "🚀 Creating wf launcher"

cat > wf <<'EOF'
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
EOF

chmod +x wf

# -----------------------------
# 6. Create symlink to PATH
# -----------------------------
echo "🔗 Creating symlink to /usr/local/bin/wf"
if sudo ln -sf "$(pwd)/wf" /usr/local/bin/wf; then
  echo "✅ Linked wf → /usr/local/bin/wf"
else
  echo "❌ Failed to create symlink. You may need to:"
  echo "   sudo ln -sf $(pwd)/wf /usr/local/bin/wf"
  echo "   Or add to PATH manually:"
  echo "   export PATH=\"$(pwd):\$PATH\""
fi

# -----------------------------
# 7. Enable shell completion
# -----------------------------
echo "🔧 Setting up shell completion..."

# Detect current shell
CURRENT_SHELL=$(basename "$SHELL")

case "$CURRENT_SHELL" in
  bash)
    echo "📝 Installing bash completion..."
    mkdir -p ~/.bash_completion.d
    source .venv/bin/activate
    # Typer's completion classes are named source_bash/complete_bash (Click's
    # own bash_source/zsh_source names are not registered), and the variable
    # name itself is derived from the program name, which cli.py pins to "wf".
    # sed drops the leading blank line Typer emits.
    if _WF_COMPLETE=source_bash wf | sed '/./,$!d' > ~/.bash_completion.d/wf-completion.bash && \
       [ -s ~/.bash_completion.d/wf-completion.bash ]; then
      echo "✅ Bash completion installed to ~/.bash_completion.d/wf-completion.bash"
    else
      rm -f ~/.bash_completion.d/wf-completion.bash
      echo "⚠️  Could not generate bash completion; skipping"
    fi
    
    # Add to bashrc if not already there
    if ! grep -q "wf-completion.bash" ~/.bashrc 2>/dev/null; then
      echo "" >> ~/.bashrc
      echo "# wf completion" >> ~/.bashrc
      echo "source ~/.bash_completion.d/wf-completion.bash" >> ~/.bashrc
      echo "📝 Added completion to ~/.bashrc"
    fi
    ;;
  zsh)
    echo "📝 Installing zsh completion..."
    mkdir -p ~/.zsh/completion
    source .venv/bin/activate
    # See the note above: source_zsh, and _WF_COMPLETE only works because
    # cli.py pins prog_name="wf". An empty _wf here silently shadows every
    # other completion for `wf` on fpath, so don't leave one behind.
    # sed strips the leading blank line Typer emits: zsh only honors
    # `#compdef` on the very first line of the file.
    if _WF_COMPLETE=source_zsh wf | sed '/./,$!d' > ~/.zsh/completion/_wf && \
       [ -s ~/.zsh/completion/_wf ]; then
      echo "✅ Zsh completion installed to ~/.zsh/completion/_wf"
    else
      rm -f ~/.zsh/completion/_wf
      echo "⚠️  Could not generate zsh completion; skipping"
    fi
    
    # Add to zshrc if not already there
    ZSHRC="$HOME/.zshrc"
    if [ -f "$ZSHRC" ]; then
      if ! grep -q "fpath+=~/.zsh/completion" "$ZSHRC" 2>/dev/null; then
        echo "" >> "$ZSHRC"
        echo "# wf completion" >> "$ZSHRC"
        echo "fpath+=~/.zsh/completion" >> "$ZSHRC"
        echo "autoload -U compinit && compinit" >> "$ZSHRC"
        echo "📝 Added completion to ~/.zshrc"
      fi
    fi
    ;;
  *)
    echo "⚠️  Unsupported shell: $CURRENT_SHELL"
    echo "   Manual completion setup required."
    ;;
esac

# -----------------------------
# 8. Done
# -----------------------------
echo
echo "✅ Installation complete"
echo
echo "Run:"
echo "  wf init"
echo "  wf start PROJ-123"
echo "  wf ai"
echo

