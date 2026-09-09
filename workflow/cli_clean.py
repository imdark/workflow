#!/usr/bin/env python3
import typer
from workflow.config import load_config, add_repository, get_repositories, is_git_enabled, is_github_enabled, save_config, get_github_integration_status
from workflow.state import set_current_task, get_current_task, get_previous_task
from workflow.git import create_branch, set_commit_prefix, commit_all, push_branch, create_pr, shelve_changes, unshelve_changes, has_uncommitted_changes, get_uncommitted_changes_summary, get_default_branch, get_available_branches, has_origin_remote, has_diff_with_base, checkout_branch
from workflow.ai import launch_ai_session
from workflow.slack import post_pr, post_task_start, post_task_complete, post_message, post_error
from workflow.backends import get_backend
from workflow.terminal import open_terminal_tabs
from rich.console import Console

console = Console(color_system=None)

app = typer.Typer()

@app.command()
def init():
    load_config(interactive=True)

# Create config subcommand group
config_app = typer.Typer(help="Configure workflow settings and repositories", no_args_is_help=True)
app.add_typer(config_app, name="config")

@app.command()
def start(
    task: str = typer.Argument(
        None, 
        autocompletion=autocomplete_active_tasks, 
        help="Task key to start working on"
    ),
    base: str = typer.Option(None, autocompletion=autocomplete_branches, help="Base branch for new branch"),
    takeover: bool = typer.Option(False, help="Take over someone else's task"),
    repo_path: str = typer.Option(None, help="Repository path to work on")
):
    """
    Start working on a task:
    - Move it to In Progress
    - Create git branch
    - Set commit prefix
    """
    cfg = load_config()
    backend = get_backend(cfg)

    if task:
        # Get existing task or create if it doesn't exist
        issue = backend.get_or_create(task)
    else:
        # Let user select interactively
        issue = backend.select_interactively(takeover)

    # Move task to in progress
    backend.move_to_in_progress(issue)

    # Remember current task locally
    set_current_task(issue)

    # Get repository info if git is enabled
    repos = get_repositories()
    if is_git_enabled():
        if not repo_path and repos:
            # If no repo specified and there are repos, use the first one
            repo_path = list(repos.keys())[0]
            typer.echo(f"Using repository: {repo_path}")
        elif not repo_path:
            typer.echo("No repositories configured. Use 'wf repo-add' to add one.")
            return
        
        # Create git branch
        repo_config = repos.get(repo_path, {})
        branch_base = base or repo_config.get("base_branch")
        if not branch_base:
            branch_base = get_default_branch(repo_path)
        create_branch(issue, branch_base, repo_path)

        # Set commit prefix
        set_commit_prefix(issue.key, repo_path if repo_path else None)
        
        # Open terminal tabs for all configured repositories
        if repos:
            open_terminal_tabs(repos, issue.key)
    else:
        if repos and not repo_path:
            typer.echo("Note: Git integration is disabled but repositories are configured.")
        elif not repos:
            typer.echo("No repositories configured. Use 'wf repo-add' to add one.")
        typer.echo("Git integration is disabled. Skipping branch creation and commit prefix setup.")

    typer.echo(f"✅ Now working on task {issue.key}")


@app.command()
def switch(task: str, repo_path: str = typer.Option(None, help="Repository path")):
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
    
    if is_git_enabled():
        set_commit_prefix(issue.key, repo_path if repo_path else None)
        
        # Try to checkout existing branch for this task
        task_branch_prefix = f"{issue.key.lower()}-"
        try:
            available_branches = get_available_branches(repo_path if repo_path else None)
            task_branch = None
            
            if available_branches:
                for branch in available_branches:
                    if branch.startswith(task_branch_prefix):
                        task_branch = branch
                        break
            
            if task_branch:
                if checkout_branch(task_branch, repo_path if repo_path else None):
                    console.print(f"✅ Switched to task branch '{task_branch}'")
                else:
                    console.print(f"⚠️  Failed to checkout task branch '{task_branch}'")
            else:
                console.print(f"ℹ️  No existing branch found for task {issue.key}")
        except Exception as e:
            console.print(f"⚠️  Could not check for task branch: {e}")
    else:
        typer.echo("Git integration is disabled. Skipping commit prefix setup.")


@app.command()
def done(reviewers: str = typer.Option(None, help="Comma-separated list of reviewers"), repo_path: str = typer.Option(None, help="Repository path")):
    cfg = load_config()
    backend = get_backend(cfg)
    issue = get_current_task()
    
    if not issue:
        typer.echo("No current task set. Use 'wf start' to begin a task.")
        return
    
    repos = get_repositories()
    
    if is_git_enabled():
        if not repos:
            typer.echo("No repositories configured. Use 'wf repo-add' to add one.")
            pr = None
        else:
            # If no specific repo_path provided, iterate through all configured repos
            if not repo_path:
                repo_paths = list(repos.keys())
            else:
                repo_paths = [repo_path]
            
            pr = None
            pr_created = False
            for current_repo_path in repo_paths:
                typer.echo(f"Processing repository: {current_repo_path}")
                
                # Get repository info for PR base branch
                base_branch = repos.get(current_repo_path, {}).get("base_branch")
                
                # Use smart default if no base branch configured
                if not base_branch:
                    base_branch = get_default_branch(current_repo_path)
                
                # Check if there are any differences with base branch or for specified repo
                if has_diff_with_base(current_repo_path, base_branch) or repo_path:
                    try:
                        pr = create_pr(issue, reviewers, current_repo_path, base_branch)
                        if pr and pr.get("url") and not pr.get("url").startswith("existing"):
                            backend.move_to_review(issue)
                            typer.echo(f"✅ Created PR for {current_repo_path}")
                            pr_created = True
                        elif pr and pr.get("url") == "existing":
                            typer.echo(f"⚠️  PR already exists for {current_repo_path}")
                    except Exception as e:
                        typer.echo(f"❌ Failed to create PR for {current_repo_path}: {e}")
                else:
                    typer.echo(f"No differences with base branch '{base_branch}' in {current_repo_path}. Skipping PR creation.")
            
            if not is_github_enabled():
                issues = get_github_integration_status()
                typer.echo("⚠️  GitHub integration is not properly set up:")
                for issue_item in issues:
                    typer.echo(f"   • {issue_item}")
                typer.echo("\n💡 To fix this:")
                if "GITHUB_TOKEN environment variable not set" in issues:
                    typer.echo("   1. Complete a GitHub personal access token at https://github.com/settings/tokens")
                    typer.echo("   2. Run: wf config set github_token YOUR_TOKEN")
                if "GitHub integration is disabled in config" in issues:
                    typer.echo("   Run: wf config set github_enabled true")
                typer.echo("Skipping PR creation.")
    
    # Move task to appropriate final state
    if pr_created:
        backend.move_to_review(issue)
        typer.echo(f"📋 Task {issue.key} moved to In Review")
    else:
        backend.move_to_done(issue)
        typer.echo(f"✅ Task {issue.key} marked as Done")


@app.command()
def shelve(repo_path: str = typer.Option(None, help="Repository path")):
    """Shelve current changes using git stash"""
    if not is_git_enabled():
        typer.echo("Git integration is disabled.")
        return
    
    if not has_uncommitted_changes(repo_path):
        typer.echo("No changes to shelve.")
        return
    
    changes_summary = get_uncommitted_changes_summary(repo_path)
    typer.echo(f"Shelving {changes_summary} changes...")
    
    if shelve_changes(repo_path):
        typer.echo("Changes shelved successfully.")
    else:
        typer.echo("Failed to shelve changes.")


@app.command()
def unshelve(repo_path: str = typer.Option(None, help="Repository path")):
    """Unshelve previously shelved changes"""
    if not is_git_enabled():
        typer.echo("Git integration is disabled.")
        return
    
    from workflow.state import has_shelved_changes, get_shelved_branch, clear_shelved_changes
    
    if not has_shelved_changes(repo_path):
        typer.echo("No shelved changes found.")
        return
    
    shelved_branch = get_shelved_branch(repo_path)
    typer.echo(f"Restoring changes shelved from branch '{shelved_branch}'...")
    
    if unshelve_changes(repo_path):
        typer.echo("Changes restored successfully.")
    else:
        typer.echo("Failed to restore changes.")


@app.command("get-tasks", hidden=True)
def get_tasks():
    """Internal command to get tasks for completion caching"""
    cfg = load_config()
    backend = get_backend(cfg)
    tasks = backend.get_assigned_tasks()
    for task in tasks:
        typer.echo(f"{task.key}:{task.title}")


@app.command()
def ai():
    launch_ai_session(get_current_task())


def autocomplete_active_tasks(ctx, args, incomplete: str):
    """
    Autocomplete only incomplete tasks with status, filtered by current project.
    """
    from workflow.projects import get_current_project, get_project
    
    current_project = get_current_project()
    project_key = None
    if current_project:
        project_config = get_project(current_project)
        if project_config:
            project_key = (project_config.get("jira", {}).get("project")
                           or project_config.get("linear", {}).get("team"))
    
    cfg = load_config()
    backend = get_backend(cfg)
    tasks = backend.get_assigned_tasks()
    active_tasks = []
    for t in tasks:
        if project_key and not t.key.upper().startswith(project_key.upper()):
            continue
        if not backend.is_done(t) and t.key.startswith(incomplete):
            active_tasks.append(t.key)
    return active_tasks


def autocomplete_branches(ctx, args, incomplete: str):
    """
    Autocomplete branch names with fuzzy matching.
    """
    from workflow.git import get_available_branches, fuzzy_match_branches
    
    # Try to get repo_path from context or use current directory
    repo_path = None
    if hasattr(ctx, 'params') and 'repo_path' in ctx.params:
        repo_path = ctx.params['repo_path']
    
    try:
        available_branches = get_available_branches(repo_path)
        matched_branches = fuzzy_match_branches(available_branches, incomplete)
        return matched_branches
    except:
        return []


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    app()