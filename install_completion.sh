#!/bin/bash

# Install completion for wf command
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPLETION_SCRIPT="$SCRIPT_DIR/_wf"

if [[ ! -f "$COMPLETION_SCRIPT" ]]; then
    echo "Completion script not found at $COMPLETION_SCRIPT"
    exit 1
fi

# Add to .zshrc if not already present
ZSHRC="$HOME/.zshrc"
COMPLETION_LINE="fpath=('$SCRIPT_DIR' \$fpath)"
COMPINIT_LINE="autoload -U compinit && compinit"
CD_FUNCTION='
# wf cd wrapper function
wf_cd() {
    local repo_path=$(wf cd "$1" 2>/dev/null)
    if [[ -n "$repo_path" && -d "$repo_path" ]]; then
        cd "$repo_path"
        echo "Changed to: $repo_path"
    else
        wf cd "$1"  # Show error/message from wf cd command
    fi
}

# Create alias for wf cd
alias wfcd=wf_cd

# Manual tab completion for wf commands
_wf_manual_complete() {
    local commands repos
    commands="init start done switch cd branch ai shelve unshelve status config slack alias hook"
    repos=($(wf config show 2>/dev/null | grep -E "^  /" | awk "{print \$1}" | xargs -I {} basename {}))
    
    case "$words[2]" in
        cd)
            reply=($repos)
            ;;
        *)
            reply=(${=commands})
            ;;
    esac
}

compctl -K _wf_manual_complete wf wfcd 2>/dev/null || true'

if ! grep -q "_wf" "$ZSHRC" 2>/dev/null; then
    echo "" >> "$ZSHRC"
    echo "# wf completion" >> "$ZSHRC"
    echo "# Initialize zsh completion system" >> "$ZSHRC"
    echo "$COMPINIT_LINE" >> "$ZSHRC"
    echo "$COMPLETION_LINE" >> "$ZSHRC"
    echo "$CD_FUNCTION" >> "$ZSHRC"
    echo "wf completion installed to .zshrc"
else
    # Check if cd function exists, add if not
    if ! grep -q "wf_cd()" "$ZSHRC" 2>/dev/null; then
        echo "" >> "$ZSHRC"
        echo "# wf cd wrapper function" >> "$ZSHRC"
        echo "$CD_FUNCTION" >> "$ZSHRC"
        echo "wf cd wrapper function added to .zshrc"
    else
        echo "wf completion and cd function already configured in .zshrc"
    fi
fi

# Copy wrapper script to PATH
WRAPPER_SCRIPT="$SCRIPT_DIR/wfcd"
if [[ -f "$WRAPPER_SCRIPT" ]]; then
    if [[ -f "/usr/local/bin/wfcd" ]]; then
        echo "wfcd wrapper already exists in /usr/local/bin"
    else
        sudo cp "$WRAPPER_SCRIPT" /usr/local/bin/wfcd 2>/dev/null || cp "$WRAPPER_SCRIPT" "$HOME/.local/bin/wfcd" 2>/dev/null || echo "Wrapper script available at $WRAPPER_SCRIPT"
    fi
fi

# Ensure completion is loaded
echo ""
echo "🔧 Installing completion..."

# Source the completion file to make it available immediately
if [[ -f "$SCRIPT_DIR/wf_completion.zsh" ]]; then
    # Initialize completion first
    autoload -U compinit && compinit
    # Then load our completion
    source "$SCRIPT_DIR/wf_completion.zsh"
    echo "✅ Completion loaded in current session"
else
    echo "⚠️  Warning: wf_completion.zsh not found in $SCRIPT_DIR"
fi

# Update .zshrc if needed
if ! grep -q "# wf completion" "$ZSHRC" 2>/dev/null; then
    echo "" >> "$ZSHRC"
    echo "# wf completion" >> "$ZSHRC"
    echo "# Initialize zsh completion system" >> "$ZSHRC"
    echo "$COMPINIT_LINE" >> "$ZSHRC"
    echo "$COMPLETION_LINE" >> "$ZSHRC"
    echo "$CD_FUNCTION" >> "$ZSHRC"
    echo "✅ Completion configuration added to .zshrc"
else
    echo "✅ Completion already configured in .zshrc"
fi

echo ""
echo "🎉 Installation complete!"
echo "📋 Available completion features:"
echo "   • Tab completion for all wf commands"
echo "   • Task completion for 'wf start' and 'wf switch'"
echo "   • Repository completion for 'wf cd'"
echo "   • Command completion for subcommands (config, alias, hook, slack)"
echo ""
echo "💡 Usage:"
echo "   Type 'wf ' and press Tab to see commands"
echo "   Type 'wf start ' and press Tab to see tasks"
echo "   Type 'wf cd ' and press Tab to see repositories"
echo ""
echo "🔄 If completion doesn't work: source ~/.zshrc"