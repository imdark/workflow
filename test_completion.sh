#!/bin/bash

# Simple test for wf cd completion
echo "🧪 Testing wf cd tab completion manually..."
echo ""

# Show what should be completed for 'wf cd '
echo "📋 Available repositories for 'wf cd ':"
wf config show 2>/dev/null | grep -E "^  /" | awk '{print $1}' | xargs -I {} basename {}

echo ""
echo "📝 Available commands for 'wf ':"
echo "init start done switch cd branch ai shelve unshelve status config slack alias hook"

echo ""
echo "✅ Manual completion test complete!"
echo ""
echo "💡 To test tab completion in your shell:"
echo "1. Run: source ~/.zshrc"
echo "2. Type: wf cd "
echo "3. Press Tab to see repositories"