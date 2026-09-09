#!/bin/zsh

# Minimal test for wf completion
echo "🧪 Testing WF completion in ZSH"
echo ""

# Load completion
source ./wf_completion.zsh

# Initialize zsh completion
if [[ -f ~/.zcompdump ]]; then
    rm ~/.zcompdump
fi
autoload -U compinit && compinit -i

echo "✅ ZSH and WF completion loaded"
echo ""

# Test task completion directly
echo "🎯 Testing task completion function:"
_wf_complete_tasks
echo ""

# Test basic completion  
echo "📋 Testing basic command completion:"
_wf_complete_commands
echo ""

echo "📁 Testing repository completion:"
_wf_complete_repos
echo ""

echo "💡 Now try these commands in this shell:"
echo "  wf <Tab>"
echo "  wf start <Tab>" 
echo "  wf cd <Tab>"

# Keep shell open for testing
echo ""
echo "Press Ctrl+C to exit or type commands to test completion"
zsh