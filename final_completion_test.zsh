#!/bin/zsh

# Direct completion test
echo "🧪 Direct completion test"
echo ""

# First, install completion permanently
./install_completion.sh

echo ""
echo "✅ Installing completion permanently..."

# Source .zshrc to load completion
source ~/.zshrc

# Also source our completion file
source ./wf_completion.zsh

echo ""
echo "🎯 Testing completion in current shell:"

# Test basic completion
echo "Testing 'wf' command completion:"
compadd -a commands init start done switch cd branch ai shelve unshelve status config slack alias hook exec-cmd

# Test task completion
echo ""
echo "Testing 'wf start' task completion:"
mkdir -p ~/.wf
wf get-tasks > ~/.wf/task_cache

# Show first few tasks
echo "Available tasks:"
head -5 ~/.wf/task_cache | while IFS=: read -r key desc; do
    echo "  $key - $desc"
done

echo ""
echo "💡 Try these commands now:"
echo "  wf <Tab>"
echo "  wf start <Tab>"
echo ""
echo "If Tab doesn't work, restart your terminal"