#!/bin/zsh

# wf cd function that actually changes directory
wf_cd_actual() {
    local repo_path
    repo_path=$(wf cd "$1" 2>/dev/null)
    
    if [[ -n "$repo_path" && -d "$repo_path" ]]; then
        # Actually change directory in current shell
        cd "$repo_path"
        echo "Changed to: $repo_path"
        return 0
    else
        # Show error from wf cd command
        wf cd "$1"
        return 1
    fi
}

# Create alias
alias wfcd='wf_cd_actual'

# Also override wf cd if we want direct behavior
alias wf='wf_cd_actual'