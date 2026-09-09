#!/bin/bash

echo "🔧 Fixing wf completion in .zshrc"

ZSHRC="$HOME/.zshrc"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Remove old manual completion
echo "🗑️  Removing old manual completion..."
sed -i '' '/_wf_manual_complete/,/compctl -K _wf_manual_complete wf wfcd/d' "$ZSHRC"

# Add proper completion loading
echo "➕ Adding wf completion source..."
if ! grep -q "source.*wf_completion" "$ZSHRC" 2>/dev/null; then
    echo "" >> "$ZSHRC"
    echo "# Load wf completion with JIRA titles" >> "$ZSHRC"
    echo "if [[ -f '$SCRIPT_DIR/wf_completion.zsh' ]]; then" >> "$ZSHRC"
    echo "  # Initialize completion system" >> "$ZSHRC"
    echo "  autoload -U compinit && compinit" >> "$ZSHRC"
    echo "  # Load wf completion" >> "$ZSHRC"
    echo "  source '$SCRIPT_DIR/wf_completion.zsh'" >> "$ZSHRC"
    echo "  compdef _wf wf wfcd" >> "$ZSHRC"
    echo "fi" >> "$ZSHRC"
    echo "✅ Added wf completion to .zshrc"
else
    echo "✅ wf completion already configured"
fi

echo ""
echo "🔄 Reload your shell:"
echo "   source ~/.zshrc"
echo ""
echo "🎮 Then test:"
echo "   wf start <Tab>"