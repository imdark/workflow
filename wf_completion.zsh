# wf completion setup for zsh

_wf_complete_commands() {
    local -a commands
    commands=(
        'init:Initialize workflow configuration'
        'start:Start working on a task'
        'done:Complete current task'
        'switch:Switch to a different task'
        'cd:Change to repository directory'
        'project:Project management commands'
        'branch:Manage git branches'
        'ai:Launch AI session'
        'shelve:Shelve current changes'
        'unshelve:Restore shelved changes'
        'status:Show current task status'
        'config:Configure workflow settings'
        'slack:Slack integration commands'
        'alias:Manage command aliases'
        'hook:Manage command hooks'
        'exec-cmd:Run alias or custom command'
    )
    if typeset -f _describe > /dev/null; then
        _describe 'commands' commands
    else
        reply=($commands)
    fi
}

_wf_complete_repos() {
    local -a repos
    # Get repository names from all projects by parsing wf cd output
    repos=($(wf cd 2>/dev/null | grep -E "^  [0-9]+\." | sed 's/^  [0-9]*\. //' | cut -d' ' -f1 | sort -u))
    if typeset -f _describe > /dev/null; then
        _describe 'repositories' repos
    else
        reply=($repos)
    fi
}

_wf_complete_tasks() {
    local cache_file="$HOME/.wf/task_cache"
    
    # Update cache if needed
    if [[ ! -f "$cache_file" ]] || [[ $(find "$cache_file" -mtime +1 2>/dev/null) ]]; then
        mkdir -p "$HOME/.wf"
        wf get-tasks 2>/dev/null > "$cache_file" || return 1
    fi
    
    local -a task_list
    while IFS=: read -r key description; do
        # Filter out epics and only show stories/tasks
        # Epics typically have numbers ending in 60 or specific keywords
        if [[ "$key" != *"60" ]] && \
           [[ "$description" != *"BLOCKER"* ]] && \
           [[ "$description" != *"bulkedit"* ]] && \
           [[ "$description" != *"Team"* ]] && \
           ! $(echo "$description" | grep -q -i "migrate\|initiative\|infrastructure"); then
            # Format as "KEY - Description" for better readability in completion
            # Truncate description if too long for display
            local short_desc="$description"
            if [[ ${#description} -gt 80 ]]; then
                short_desc="${description:0:77}..."
            fi
            task_list+=("$key:$short_desc")
        fi
    done < "$cache_file"
    
    if typeset -f _describe > /dev/null; then
        _describe 'workflow tasks' task_list
    else
        reply=($task_list)
    fi
}

_wf_complete_config_commands() {
    local -a config_commands
    config_commands=(
        'repo-add:Add a repository'
        'repo-list:List configured repositories'
        'repo-discover:Discover repositories'
        'repo-branches:List repository branches'
        'set:Set configuration value'
        'show:Show current configuration'
    )
    _describe 'config command' config_commands
}

_wf_complete_alias_commands() {
    local -a alias_commands
    alias_commands=(
        'add:Add a new alias'
        'add-complex:Add a complex multi-line alias'
        'list:List all aliases'
        'remove:Remove an alias'
        'run:Run an alias'
    )
    _describe 'alias command' alias_commands
}

_wf_complete_hook_commands() {
    local -a hook_commands
    hook_commands=(
        'add-hook:Add a new hook'
        'list-hooks:List all hooks'
        'remove-hook:Remove a hook'
        'run-hook:Run a hook manually'
        'test-trigger:Test hook triggers'
    )
    _describe 'hook command' hook_commands
}

_wf_complete_project_commands() {
    local -a project_commands
    project_commands=(
        'add:Add a new project'
        'remove:Remove a project'
        'list:List all projects'
        'change:Change current project'
        'current:Show current project'
        'migrate:Migrate existing config to projects'
    )
    _describe 'project command' project_commands
}

_wf_complete_slack_commands() {
    local -a slack_commands
    slack_commands=(
        'setup:Set up Slack integration'
        'test:Test Slack connection'
        'notify:Send notification to Slack'
        'channel:Set Slack channel'
        'template:Set message template'
        'add-user:Add user mapping'
    )
    _describe 'slack command' slack_commands
}

_wf() {
    local curcontext="$curcontext" state line
    typeset -A opt_args

    _arguments -C \
        '1: :->command' \
        '*: :->args' && return 0

    case $state in
        command)
            _wf_complete_commands
            ;;
        args)
            case $line[1] in
                cd)
                    _wf_complete_repos
                    ;;
                start|switch)
                    _wf_complete_tasks
                    ;;
                config)
                    _wf_complete_config_commands
                    ;;
                project)
                    _wf_complete_project_commands
                    ;;
                alias)
                    _wf_complete_alias_commands
                    ;;
                hook)
                    _wf_complete_hook_commands
                    ;;
                slack)
                    _wf_complete_slack_commands
                    ;;
                exec-cmd)
                    # Try to complete with aliases
                    local -a aliases
                    aliases=($(wf alias list 2>/dev/null | grep -E "^[a-zA-Z]" | awk '{print $1}' | sed 's/:$//'))
                    _describe 'aliases' aliases
                    ;;
            esac
            ;;
    esac
}

# Register completion (only if compdef is available)
if [[ -n "$ZSH_VERSION" ]] && type compdef > /dev/null 2>&1; then
    compdef _wf wf wfcd
fi

# Also register with compctl for broader compatibility
compctl -K _wf_complete wf wfcd 2>/dev/null || true