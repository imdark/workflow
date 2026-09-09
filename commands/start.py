import typer
from pathlib import Path

from workflow.config import load_effective_config, is_git_enabled
from workflow.backends import get_backend
from workflow.git_utils import create_branch, set_commit_prefix
from workflow.state import set_current_task
from workflow.projects import get_current_project_repositories


app = typer.Typer(help="Start working on a task")


@app.command()
def start(task_key: str = typer.Argument(...), title: str = typer.Argument(...), description: str = typer.Argument(...)):
    """Start working on a task:
    - Move it to In Progress
    - Create git branch
    - Set commit prefix
    """
    cfg = load_effective_config()
    backend = get_backend(cfg)
    
    issue = backend.get_or_create(task_key)
    
    issue.title = title
    issue.description = description
    
    set_current_task(issue)
    
    if is_git_enabled():
        repos = get_current_project_repositories()
        
        missing_repos = []
        for repo_path in repos:
            if not Path(repo_path).exists():
                missing_repos.append(repo_path)
        
        if missing_repos:
            typer.echo(f"❌ Error: The following repositories do not exist:")
            for repo in missing_repos:
                typer.echo(f"   - {repo}")
            typer.echo("Use 'wf repo-add' to add repositories or update your configuration.")
            return
        
        branch_name = f"{task_key}-{issue.title.replace(' ', '-')}"
        for repo_path in repos:
            create_branch(issue, branch_name, repo_path)
        
        set_commit_prefix(task_key, repos)
        
        typer.echo(f"✅ Now working on task {task_key}")
        typer.echo(f"🌿 Created branch: {branch_name}")
        typer.echo(f"📝 Commit prefix set: {task_key}")
