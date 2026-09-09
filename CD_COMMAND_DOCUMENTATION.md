# wf cd Command Documentation

## Overview

The `wf cd` command allows you to quickly navigate to your configured repository directories with tab completion support.

## Features

- **Tab completion**: Press Tab after `wf cd` to see available repositories
- **Flexible matching**: Match repositories by name or partial name
- **List repositories**: Run `wf cd` without arguments to see all available repositories
- **Shell integration**: Use `wfcd` alias to actually change directories

## Usage

### Basic Commands

```bash
# List all available repositories
wf cd

# Change to a specific repository (outputs the path)
wf cd myrepo

# Change directory using the wrapper function
wfcd myrepo

# Partial name matching works too
wfcd oth  # Will match other-repo
```

### Tab Completion

1. Type `wf cd ` (with space after cd)
2. Press Tab to see available repositories
3. Type part of repository name and press Tab for auto-completion

## Installation

The completion and wrapper function are automatically installed when you run:

```bash
./install_completion.sh
```

Or manually add this to your `~/.zshrc`:

```bash
# wf cd wrapper function
wf_cd() {
    local repo_path=$(wf cd "$1" 2>/dev/null)
    if [[ -n "$repo_path" && -d "$repo_path" ]]; then
        cd "$repo_path"
        echo "Changed to: $repo_path"
    else
        wf cd "$1"  # Show error/message from wf cd command
    fi
}

# Create alias for wf cd
alias wfcd=wf_cd
```

## Examples

```bash
$ wf cd
Available repositories:
  1. myrepo (~/code/myrepo)
  2. other-repo (~/code/other-repo)

$ wfcd myrepo
Changed to: ~/code/myrepo

$ pwd
~/code/myrepo

$ wfcd deep
Changed to: ~/code/other-repo
```

## Integration with Workflow

The `wf cd` command integrates seamlessly with your existing workflow configuration:

- Uses the same repositories configured with `wf config repo-add`
- Supports both local paths and cloned repositories
- Works with the completion system for smooth navigation

## Troubleshooting

**"No repositories configured" error**
- Run `wf config repo-discover` to find repositories
- Add repositories with `wf config repo-add <path-or-url>`

**Tab completion not working**
- Run `./install_completion.sh` to install completion
- Restart your terminal or run `source ~/.zshrc`

**Directory change not working**
- Use `wfcd` alias instead of `wf cd` directly
- Ensure the wrapper function is loaded in your shell