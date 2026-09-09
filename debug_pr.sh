#!/bin/bash

# Debug script for PR detection issue
echo "🔍 Debugging PR detection for proj-123 branch..."

# Check current directory and git status
echo "📁 Current directory: $(pwd)"
echo "🌿 Current branch: $(git branch --show-current)"

# Check if GitHub CLI is available and authenticated
echo "🔧 GitHub CLI version: $(gh --version 2>/dev/null || echo 'Not installed')"
echo "🔑 GitHub CLI auth status:"
gh auth status 2>/dev/null || echo "Not authenticated"

# Test basic gh pr list
echo "📋 Testing 'gh pr list' command:"
gh pr list 2>&1 | head -10

# Test gh pr list with current branch
CURRENT_BRANCH=$(git branch --show-current)
echo "📋 Testing 'gh pr list --head $CURRENT_BRANCH':"
gh pr list --head "$CURRENT_BRANCH" 2>&1

# Test with JSON output
echo "📋 Testing 'gh pr list --json number,title,url':"
gh pr list --json number,title,url 2>&1

# Test with specific branch
echo "📋 Testing 'gh pr list --head $CURRENT_BRANCH --json':"
gh pr list --head "$CURRENT_BRANCH" --json number,title,url 2>&1

# Check git remotes
echo "🔗 Git remotes:"
git remote -v

# Try to find any PRs with PROJ-123 in the title
echo "🔍 Searching for PROJ-123 in PRs:"
gh pr list --search "PROJ-123" 2>&1