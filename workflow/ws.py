#!/usr/bin/env python3
import typer
from workflow.cli import start, switch
from workflow.terminal import open_terminal_tabs
from workflow.config import get_repositories

# Create workspace app
ws_app = typer.Typer(help="Workspace management commands", no_args_is_help=True)

@ws_app.command()
def start(
    task: str = typer.Argument(
        None, 
        autocompletion=lambda ctx, args, incomplete: [],  # We'll use the main autocomplete
        help="Task key to start working on"
    ),
    base: str = typer.Option(None, help="Base branch for new branch"),
    takeover: bool = typer.Option(False, help="Take over someone else's task"),
    repo_path: str = typer.Option(None, help="Repository path to work on")
):
    """Start working on a task and open terminal tabs"""
    # Use the main start command which now includes terminal tab opening
    from workflow.cli import start as main_start
    # We need to call this directly to get the proper behavior
    from workflow.cli import start as start_command
    
    # Call the start command logic
    cfg = __import__('workflow.config', fromlist=['load_config']).load_config()
    backend = __import__('workflow.backends', fromlist=['get_backend']).get_backend(cfg)
    from workflow.state import set_current_task
    from workflow.git import create_branch, set_commit_prefix, get_default_branch

    if task:
        issue = backend.get_or_create(task)
    else:
        issue = backend.select_interactively(takeover)

    backend.move_to_in_progress(issue)
    set_current_task(issue)

    repos = get_repositories()
    from workflow.config import is_git_enabled
    if is_git_enabled():
        if not repo_path and repos:
            repo_path = list(repos.keys())[0]
            typer.echo(f"Using repository: {repo_path}")
        elif not repo_path:
            typer.echo("No repositories configured. Use 'wf config repo-add' to add one.")
            return
        
        repo_config = repos.get(repo_path, {})
        branch_base = base or repo_config.get("base_branch")
        if not branch_base:
            branch_base = get_default_branch(repo_path)
        create_branch(issue, branch_base, repo_path)
        set_commit_prefix(issue.key, repo_path if repo_path else None)

    typer.echo(f"✅ Now working on task {issue.key}")
    
    # Open terminal tabs for all repositories
    open_terminal_tabs(repos, issue.key)

@ws_app.command()
def switch(task: str, repo_path: str = typer.Option(None, help="Repository path")):
    """Switch to a task and open terminal tabs"""
    from workflow.cli import switch as main_switch
    from workflow.state import get_previous_task, set_current_task
    from workflow.config import load_config
    from workflow.backends import get_backend
    from workflow.git import set_commit_prefix
    
    cfg = load_config()
    backend = get_backend(cfg)
    
    if task == "-":
        issue = get_previous_task()
        if not issue:
            typer.echo("No previous task found")
            return
    else:
        issue = backend.get(task)
    set_current_task(issue)
    
    from workflow.config import is_git_enabled
    if is_git_enabled():
        set_commit_prefix(issue.key, repo_path if repo_path else None)
    else:
        typer.echo("Git integration is disabled. Skipping commit prefix setup.")
    
    # Open terminal tabs for all repositories
    repos = get_repositories()
    open_terminal_tabs(repos, issue.key)

if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    ws_app()