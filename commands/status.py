import typer
from git import Repo

from workflow.config import load_effective_config
from workflow.backends import get_backend
from workflow.state import get_current_task
from workflow.git_utils import check_existing_prs_for_issue, has_uncommitted_changes, get_uncommitted_changes_summary
from workflow.projects import get_current_project_repositories


app = typer.Typer(help="Show current task status")


@app.command()
def status():
    """Show current task status and PR information"""
    cfg = load_effective_config()
    backend = get_backend(cfg)
    issue = get_current_task()
    
    if not issue:
        typer.echo("❌ No current task set.")
        typer.echo("💡 Use 'wf start <task-key>' to begin working on a task.")
        return
    
    typer.echo("📋 Current Task Status")
    typer.echo("=" * 50)
    typer.echo(f"🔑 Task Key: {issue.key}")
    
    jira_url = cfg.get("jira", {}).get("url", "")
    if jira_url:
        jira_link = f"{jira_url.rstrip('/')}/browse/{issue.key}"
        typer.echo(f"🔗 Jira: {jira_link}")
    
    typer.echo(f"📝 Title: {issue.title if hasattr(issue, 'title') else getattr(issue, 'summary', 'No title')}")
    
    is_done = backend.is_done(issue) if hasattr(backend, 'is_done') else False
    if is_done:
        typer.echo("✅ State: Completed")
    else:
        typer.echo("🔄 State: In Progress")
    
    repos = get_current_project_repositories()
    if repos:
        typer.echo(f"\n📁 Repository Status")
        typer.echo("-" * 30)
        
        repo_paths = list(repos.keys())
        existing_prs = check_existing_prs_for_issue(issue, repo_paths)
        
        pr_found = False
        for repo_path in repo_paths:
            repo_name = repo_path.split("/")[-1]
            typer.echo(f"\n📂 {repo_name} ({repo_path})")
            
            if repo_path in existing_prs:
                pr_info = existing_prs[repo_path]
                typer.echo(f"  🔗 PR: {pr_info['url']}")
                if pr_info.get('title'):
                    typer.echo(f"  📝 PR Title: {pr_info['title']}")
                if pr_info.get('state'):
                    typer.echo(f"  📊 PR State: {pr_info['state']}")
                pr_found = True
            else:
                typer.echo("  ❌ No PR found")
            
            try:
                repo = Repo(repo_path)
                current_branch = repo.active_branch.name
                typer.echo(f"  🌿 Branch: {current_branch}")
                
                if has_uncommitted_changes(repo_path):
                    changes_summary = get_uncommitted_changes_summary(repo_path)
                    typer.echo(f"  📝 Uncommitted changes: {changes_summary}")
                else:
                    typer.echo("  ✅ Working directory clean")
            except Exception as e:
                typer.echo(f"  ⚠️  Could not check git status: {str(e)}")
        
        if not pr_found:
            typer.echo(f"\n💡 No PRs found for {issue.key}. Use 'wf done' to create PRs.")
    else:
        typer.echo(f"\n⚠️  No repositories configured. Use 'wf repo-add' to add repositories.")
    
    typer.echo(f"\n🚀 Quick Actions:")
    typer.echo(f"  wf done               # Complete task and create PRs")
    typer.echo(f"  wf ai                 # Start AI session")
