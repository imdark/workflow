#!/bin/bash

# Test script for wf cd functionality

echo "🧪 Testing wf cd command functionality..."
echo ""

# Test 1: List repositories
echo "📋 Test 1: List repositories"
./wf cd
echo ""

# Test 2: cd to specific repo
echo "📁 Test 2: cd to 'flow' repository"
REPO_PATH=$(./wf cd myrepo)
echo "Returned path: $REPO_PATH"

if [[ -d "$REPO_PATH" ]]; then
    echo "✅ Repository path exists: $REPO_PATH"
else
    echo "❌ Repository path does not exist: $REPO_PATH"
fi
echo ""

# Test 3: Test with invalid repo
echo "❌ Test 3: Test with invalid repository"
./wf cd nonexistent-repo
echo ""

# Test 4: Test partial matching
echo "🔍 Test 4: Test partial matching"
./wf cd deep
echo ""

echo "🎉 wf cd command testing completed!"