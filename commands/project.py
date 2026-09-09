import typer
import keyring

from workflow.projects import add_project, remove_project, list_projects, get_project, set_current_project, get_current_project, migrate_to_project_config


app = typer.Typer(help="Project management")


@app.command("add")
def project_add(
    name: str = typer.Argument(..., help="Project name"),
    jira_url: str = typer.Option(..., help="Jira URL"),
    jira_email: str = typer.Option(..., help="Jira email"),
    jira_token: str = typer.Option(..., help="Jira API token"),
    jira_project: str = typer.Option(..., help="Jira project key"),
    default_repo: str = typer.Option(None, help="Default repository path")
):
    """Add a new project configuration"""
    try:
        project_keychain_service = f"workflow.jira.{name}"
        try:
            keyring.set_password(project_keychain_service, "api_token", jira_token)
        except Exception:
            typer.echo("⚠️  Warning: Could not store token in Keychain, storing in config instead")

        project_config = {
            "name": jira_project,
            "jira": {
                "url": jira_url,
                "email": jira_email,
                "token": jira_token,
                "project": jira_project
            },
            "repositories": {},
            "git_enabled": True,
            "github_enabled": False
        }
        
        if default_repo:
            project_config["repositories"][default_repo] = {"base_branch": "main"}
        
        add_project(name, project_config)
        typer.echo(f"✅ Added project: {name}")
        typer.echo(f"🔗 Jira: {jira_url}")
        typer.echo(f"📝 Jira Project: {jira_project}")
        if default_repo:
            typer.echo(f"📁 Default Repository: {default_repo}")
            
    except Exception as e:
        typer.echo(f"❌ Error adding project: {e}")


@app.command("remove")
def project_remove(name: str = typer.Argument(..., help="Project name")):
    """Remove a project configuration"""
    try:
        remove_project(name)
        typer.echo(f"✅ Removed project: {name}")
    except Exception as e:
        typer.echo(f"❌ Error removing project: {e}")


@app.command("list")
def project_list():
    """List all configured projects"""
    try:
        projects = list_projects()
        current = get_current_project()
        
        if not projects:
            typer.echo("No projects configured.")
            return
        
        typer.echo("Configured projects:")
        for i, (project_name, project_config) in enumerate(projects.items(), 1):
            marker = " (current)" if project_name == current else ""
            jira_project = project_config.get("jira", {}).get("project", "Unknown")
            repos = project_config.get("repositories", {})
            typer.echo(f"{i}. {project_name}{marker}")
            typer.echo(f"   Jira Project: {jira_project}")
            typer.echo(f"   Repositories: {len(repos)}")
            
    except Exception as e:
        typer.echo(f"❌ Error listing projects: {e}")


@app.command("change")
def project_change(name: str = typer.Argument(..., help="Project name")):
    """Change the current active project"""
    try:
        set_current_project(name)
        typer.echo(f"✅ Switched to project: {name}")
        
        project = get_project(name)
        if project:
            jira_project = project.get("jira", {}).get("project", "Unknown")
            repos = project.get("repositories", {})
            typer.echo(f"🔗 Jira Project: {jira_project}")
            typer.echo(f"📁 Repositories: {len(repos)}")
            
    except Exception as e:
        typer.echo(f"❌ Error changing project: {e}")


@app.command("current")
def project_current():
    """Show the current active project"""
    try:
        current = get_current_project()
        if not current:
            typer.echo("No current project set.")
            return
        
        typer.echo(f"Current project: {current}")
        project = get_project(current)
        if project:
            jira_project = project.get("jira", {}).get("project", "Unknown")
            repos = project.get("repositories", {})
            typer.echo(f"🔗 Jira Project: {jira_project}")
            typer.echo(f"📁 Repositories: {len(repos)}")
            if repos:
                for repo_path in repos.keys():
                    base_branch = repos[repo_path].get("base_branch", "main")
                    typer.echo(f"   - {repo_path} (base: {base_branch})")
                    
    except Exception as e:
        typer.echo(f"❌ Error getting current project: {e}")


@app.command("migrate")
def project_migrate():
    """Migrate existing single-project config to project-based config"""
    try:
        if migrate_to_project_config():
            current = get_current_project()
            typer.echo(f"✅ Migrated to project-based configuration")
            typer.echo(f"🔄 Current project: {current}")
        else:
            typer.echo("ℹ️  Migration not needed or already completed")
            
    except Exception as e:
        typer.echo(f"❌ Error migrating configuration: {e}")
