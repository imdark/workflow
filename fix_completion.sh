#!/bin/bash

echo "🔧 Adding compinit to .zshrc for wf completion..."

ZSHRC="$HOME/.zshrc"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if compinit is already configured for wf
if ! grep -q "# wf completion" "$ZSHRC" 2>/dev/null; then
    echo "" >> "$ZSHRC"
    echo "# wf completion" >> "$ZSHRC"
    echo "if [[ -f '$SCRIPT_DIR/wf_completion.zsh' ]]; then" >> "$ZSHRC"
    echo "  # Initialize completion system" >> "$ZSHRC"
    echo "  autoload -U compinit && compinit" >> "$ZSHRC"
    echo "  # Load wf completion" >> "$ZSHRC"
    echo "  source '$SCRIPT_DIR/wf_completion.zsh'" >> "$ZSHRC"
    echo "fi" >> "$ZSHRC"
    echo "✅ Added wf completion to .zshrc"
else
    echo "✅ wf completion already configured in .zshrc"
fi

echo ""
echo "🔄 Reload your shell: source ~/.zshrc"
echo "🎮 Then test: wf start <Tab>"