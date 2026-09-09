#!/bin/bash

# Simple test of directory change
echo "🧪 Testing directory change..."
echo ""

REPO_PATH=$(./wf cd myrepo 2>/dev/null)
echo "Repo path: $REPO_PATH"

if [[ -n "$REPO_PATH" && -d "$REPO_PATH" ]]; then
    echo "✅ Repository exists: $REPO_PATH"
    echo "🔧 Testing cd command..."
    
    # Test if we can cd to it
    if cd "$REPO_PATH" 2>/dev/null; then
        echo "✅ cd command successful"
        echo "📍 Current directory: $(pwd)"
        
        # Go back for cleanup
        cd - > /dev/null
    else
        echo "❌ cd command failed"
    fi
else
    echo "❌ Invalid repository path"
fi