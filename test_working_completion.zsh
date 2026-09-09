#!/bin/zsh

# Complete working completion test
echo "🧪 Testing WF tab completion"
echo ""

# Load zsh completion system
autoload -U compinit && compinit

# Load our completion
source ./wf_completion.zsh

# Register completion manually
compdef _wf wf wfcd

echo "✅ Completion system loaded"
echo ""

# Test if functions exist
echo "🔍 Checking completion functions:"
if type _wf > /dev/null 2>&1; then
    echo "✅ _wf function exists"
else
    echo "❌ _wf function missing"
fi

if type _wf_complete_tasks > /dev/null 2>&1; then
    echo "✅ _wf_complete_tasks function exists"
else
    echo "❌ _wf_complete_tasks function missing"
fi

echo ""

# Test task completion function
echo "🎯 Testing task completion function:"
setopt local_options bash_rematch
local -a task_list
local cache_file="$HOME/.wf/task_cache"

# Create cache
mkdir -p "$HOME/.wf"
wf get-tasks 2>/dev/null > "$cache_file"

# Parse tasks
while IFS=: read -r key description; do
    if [[ "$key" != *"60" ]] && \
       [[ "$description" != *"BLOCKER"* ]] && \
       [[ "$description" != *"bulkedit"* ]] && \
       [[ "$description" != *"Team"* ]] && \
       ! $(echo "$description" | grep -q -i "migrate\|initiative\|infrastructure"); then
        local short_desc="$description"
        if [[ ${#description} -gt 80 ]]; then
            short_desc="${description:0:77}..."
        fi
        task_list+=("$key:$short_desc")
    fi
done < "$cache_file"

echo "Found ${#task_list[@]} tasks:"
for task in "${task_list[@]:0:3}"; do
    echo "  $task"
done

echo ""
echo "🎮 Interactive test - type these commands and press Tab:"
echo "  wf <Tab>"
echo "  wf start <Tab>" 
echo "  wf cd <Tab>"
echo ""
echo "Press Ctrl+C to exit"

# Keep shell open
exec zsh