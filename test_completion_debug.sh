#!/bin/bash

echo "🧪 Testing wf tab completion..."
echo ""

# Test basic command completion
echo "📋 Testing basic command completion:"
echo "Type: wf <Tab>"
echo "Expected: Should show init, start, done, switch, cd, branch, ai, shelve, unshelve, status, config, slack, alias, hook, exec-cmd"
echo ""

# Test task completion
echo "🎯 Testing task completion:"
echo "Type: wf start <Tab>"
echo "Expected: Should show JIRA tasks with titles"
echo "Example: PROJ-123 - example ticket"
echo ""

# Test repository completion
echo "📁 Testing repository completion:"
echo "Type: wf cd <Tab>"
echo "Expected: Should show configured repositories"
echo ""

# Test subcommand completion
echo "🔧 Testing subcommand completion:"
echo "Type: wf config <Tab>"
echo "Expected: Should show repo-add, repo-list, repo-discover, repo-branches, set, show"
echo ""

# Interactive test
echo "🎮 Interactive test mode:"
echo "1. Run: source ./wf_completion.zsh"
echo "2. Try: wf <Tab> (should show commands)"
echo "3. Try: wf start <Tab> (should show tasks with titles)"
echo "4. Try: wf cd <Tab> (should show repositories)"
echo ""

# Check completion registration
echo "🔍 Checking completion registration:"
if [[ -n "$ZSH_VERSION" ]]; then
    echo "Running in ZSH"
    source ./wf_completion.zsh 2>/dev/null
    echo "✅ Completion sourced"
    
    # Check if _wf function exists
    if type _wf > /dev/null 2>&1; then
        echo "✅ _wf completion function exists"
    else
        echo "❌ _wf completion function not found"
    fi
    
    # Check if _wf_complete_tasks exists
    if type _wf_complete_tasks > /dev/null 2>&1; then
        echo "✅ _wf_complete_tasks function exists"
    else
        echo "❌ _wf_complete_tasks function not found"
    fi
else
    echo "Not running in ZSH - completion may not work properly"
fi

echo ""
echo "📝 Current tasks from JIRA (first 5):"
wf get-tasks 2>/dev/null | head -5