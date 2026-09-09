#!/bin/zsh

# Simplified test for wf completion
echo "🧪 Testing WF completion in ZSH"
echo ""

# Initialize zsh completion first
autoload -U compinit && compinit

# Load completion
source ./wf_completion.zsh

echo "✅ ZSH completion initialized and WF completion loaded"
echo ""

# Test basic completion
echo "📋 Commands available:"
echo "  init - Initialize workflow configuration"
echo "  start - Start working on a task"
echo "  done - Complete current task"
echo "  switch - Switch to a different task"
echo "  cd - Change to repository directory"
echo "  branch - Manage git branches"
echo "  ai - Launch AI session"
echo "  shelve - Shelve current changes"
echo "  unshelve - Restore shelved changes"
echo "  status - Show current task status"
echo "  config - Configure workflow settings"
echo "  slack - Slack integration commands"
echo "  alias - Manage command aliases"
echo "  hook - Manage command hooks"
echo "  exec-cmd - Run alias or custom command"

echo ""
echo "🎯 Testing task cache creation:"
mkdir -p ~/.wf
wf get-tasks 2>/dev/null > ~/.wf/task_cache && echo "✅ Task cache created" || echo "❌ Task cache failed"

echo ""
echo "📁 Available repositories:"
wf config show 2>/dev/null | grep -E "^  /" | awk '{print $1}' | xargs -I {} basename {}

echo ""
echo "💡 Now test tab completion interactively:"
echo "  Type: wf <Tab>"
echo "  Type: wf start <Tab>"
echo "  Type: wf cd <Tab>"
echo ""
echo "🔄 If completion doesn't work, run: source ~/.zshrc"

# Keep shell open for interactive testing
echo ""
echo "🎮 Interactive shell ready - test completion now"
zsh