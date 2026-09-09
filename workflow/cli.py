#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import typer
from typing import List
from pathlib import Path
import subprocess
import sys
import time
from workflow.config import load_config, add_repository, get_repositories, is_git_enabled, is_github_enabled, save_config, get_github_integration_status, load_effective_config
from workflow.state import set_current_task, get_current_task, get_previous_task
from workflow.git_utils import create_branch, set_commit_prefix, commit_all, push_branch, create_pr, shelve_changes, unshelve_changes, has_uncommitted_changes, get_uncommitted_changes_summary, get_default_branch, get_available_branches, has_origin_remote, has_diff_with_base, checkout_branch, get_available_branches
from workflow.ai import launch_ai_session
from workflow.slack import post_pr, post_task_start, post_task_complete, post_message, post_error
from workflow.slackdump import SlackDumper, set_slack_token, get_slack_token, dump_to_keychain, get_token_from_keychain, get_browser_cookies, save_slack_cookies, get_slack_cookies
from workflow.teleop_issues import (
    find_new_issue_messages,
    parse_new_issue,
    build_linear_mention_comment,
    resolve_channel_id,
    thread_has_linear_conversion,
    DEFAULT_LINEAR_MENTION,
)
from workflow.browser_client import BrowserClient, is_daemon_running, start_daemon
from workflow.teleop_linear_sync import scan_new_issue_status, convert_new_issue, AlreadyConverted
from workflow.config import set_slack_channel, set_slack_message_template, add_slack_user_mapping, get_slack_config
from workflow.backends import get_backend
from workflow.terminal import open_terminal_tabs
from workflow.aliases import AliasManager
from workflow.hooks import HookManager
from workflow.variables import VariableResolver
from workflow.notifications import get_notification_service
from workflow.projects import add_project, remove_project, list_projects, get_project, set_current_project, get_current_project, migrate_to_project_config
from rich.console import Console
from rich.table import Table

console = Console(color_system=None)

app = typer.Typer()

# Enable argcomplete for shell completions
try:
    import argcomplete
    argcomplete.autocomplete(app)
except ImportError:
    pass

# Create action subcommand group
action_app = typer.Typer(help="Action management", no_args_is_help=True)
app.add_typer(action_app, name="action")

# Create project subcommand group
project_app = typer.Typer(help="Project management")
app.add_typer(project_app, name="project")

@action_app.command("list")
def list_actions():
    """List all available action types"""
    from workflow.actions import get_action_registry, ActionType
    
    registry = get_action_registry()
    
    console.print("🎯 [bold]Available Action Types:[/bold]")
    console.print("")
    
    # Action type descriptions
    action_descriptions = {
        ActionType.NOTIFICATION: "Send desktop notifications with customizable content and response options",
        ActionType.START_TASK: "Start working on a specific task (requires task_key)",
        ActionType.FINISH_TASK: "Complete the current task",
        ActionType.OPEN_AI_CONSOLE: "Launch AI console with current task context",
        ActionType.OPEN_TERMINAL: "Open terminal tabs for configured repositories",
        ActionType.CUSTOM_COMMAND: "Execute custom shell commands with environment variables",
        ActionType.ALIAS: "Create aliases that trigger multiple actions in sequence"
    }
    
    for action_type in ActionType:
        description = action_descriptions.get(action_type, "No description available")
        console.print(f"  [cyan]{action_type.value}[/cyan]")
        console.print(f"    {description}")
        console.print("")
    
    console.print("📋 [bold]Registered Actions:[/bold]")
    console.print("")
    
    actions = registry.list_actions()
    if actions:
        for action in actions:
            console.print(f"  [green]{action['action_id']}[/green] ({action['type']})")
    else:
        console.print("  [dim]No actions currently registered[/dim]")
    
    console.print("")
    console.print("💡 [bold]Example Usage:[/bold]")
    console.print("  wf action list                    # Show this list")
    console.print("  wf action test notification        # Test a notification")
    console.print("  wf monitor action add-github ...   # Add monitor with actions")

@action_app.command("test")
def test_action(
    action_type: str = typer.Argument(..., help="Action type to test"),
    context: str = typer.Option("{}", "--context", help="JSON context for action"),
    title: str = typer.Option("Test Notification", "--title", help="Title for notification tests"),
    message: str = typer.Option("This is a test notification", "--message", help="Message for notification tests")
):
    """Test an action with sample data"""
    from workflow.actions import get_action_registry, ActionType
    import json
    
    try:
        action_type_enum = ActionType(action_type)
    except ValueError:
        console.print(f"❌ Unknown action type: [red]{action_type}[/red]")
        console.print("💡 Available types: " + ", ".join([f"[cyan]{t.value}[/cyan]" for t in ActionType]))
        return
    
    try:
        context_dict = json.loads(context)
    except json.JSONDecodeError:
        console.print("❌ Invalid JSON context")
        return
    
    registry = get_action_registry()
    test_action_id = f"test_{action_type}"
    action = registry.create_action(test_action_id, action_type_enum)
    
    if not action:
        console.print(f"❌ Failed to create action: [red]{action_type}[/red]")
        return
    
    # Add sample context for testing
    if action_type == "notification":
        context_dict.update({
            "title": title,
            "message": message,
            "notification_id": "test_notification"
        })
    elif action_type == "start_task":
        context_dict.update({
            "task_key": "TEST-123",
            "task_title": "Test Task",
            "task_description": "This is a test task"
        })
    elif action_type in ["open_ai_console", "open_terminal"]:
        console.print(f"🧪 Testing [cyan]{action_type}[/cyan] action...")
    
    console.print(f"📋 Context: {json.dumps(context_dict, indent=2)}")
    console.print("")
    
    result = action.execute(context_dict)
    
    if result.get('success', False):
        console.print("✅ [green]Action executed successfully[/green]")
        if result.get('stdout'):
            console.print(f"📤 Output: {result['stdout']}")
    else:
        console.print("❌ [red]Action failed[/red]")
        error = result.get('error') or result.get('stderr')
        if error:
            console.print(f"📤 Error: {error}")
    
    # Cleanup
    if hasattr(registry, 'actions') and test_action_id in registry.actions:
        del registry.actions[test_action_id]

# Project management commands
@project_app.command("add")
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
            project_config["default_repo"] = default_repo
        
        add_project(name, project_config)
        console.print(f"✅ Added project: {name}")
        console.print(f"🔗 Jira: {jira_url}")
        console.print(f"📝 Jira Project: {jira_project}")
        if default_repo:
            console.print(f"📁 Default Repository: {default_repo}")
            
    except Exception as e:
        console.print(f"❌ Error adding project: {e}")


@project_app.command("remove")
def project_remove(name: str = typer.Argument(..., help="Project name")):
    """Remove a project configuration"""
    try:
        remove_project(name)
        console.print(f"✅ Removed project: {name}")
    except Exception as e:
        console.print(f"❌ Error removing project: {e}")


@project_app.command("list")
def project_list():
    """List all configured projects"""
    try:
        projects = list_projects()
        current = get_current_project()
        
        if not projects:
            console.print("No projects configured.")
            return
        
        console.print("Configured projects:")
        for i, (project_name, project_config) in enumerate(projects.items(), 1):
            marker = " (current)" if project_name == current else ""
            jira_project = project_config.get("jira", {}).get("project", "Unknown")
            repos = project_config.get("repositories", {})
            default_repo = project_config.get("default_repo")
            console.print(f"{i}. {project_name}{marker}")
            console.print(f"   Jira Project: {jira_project}")
            console.print(f"   Repositories: {len(repos)}")
            if default_repo:
                console.print(f"   Default Repo: {default_repo}")
            
    except Exception as e:
        console.print(f"❌ Error listing projects: {e}")


@project_app.command("change")
def project_change(name: str = typer.Argument(..., help="Project name")):
    """Change the current active project"""
    try:
        set_current_project(name)
        console.print(f"✅ Switched to project: {name}")
        
        # Clear and repopulate task cache
        cache_file = Path.home() / ".wf" / "task_cache"
        if cache_file.exists():
            cache_file.unlink()
            console.print("🗑️  Cleared task cache")
        
        # Repopulate cache in background
        def update_cache_background():
            try:
                from workflow.backends import get_backend
                from workflow.config import load_effective_config
                cfg = load_effective_config()
                backend = get_backend(cfg)
                tasks = backend.get_assigned_tasks()
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_file, "w") as f:
                    for t in tasks:
                        f.write(f"{t.key}:{t.title}\n")
                console.print(f"📝 Updated task cache ({len(tasks)} tasks)")
            except Exception as cache_err:
                console.print(f"⚠️  Could not update cache: {cache_err}")
        
        import threading
        threading.Thread(target=update_cache_background, daemon=True).start()
        
        # Show project info
        project = get_project(name)
        if project:
            jira_project = project.get("jira", {}).get("project", "Unknown")
            repos = project.get("repositories", {})
            default_repo = project.get("default_repo")
            console.print(f"🔗 Jira Project: {jira_project}")
            console.print(f"📁 Repositories: {len(repos)}")
            if default_repo:
                console.print(f"⭐ Default Repo: {default_repo}")
        
    except Exception as e:
        console.print(f"❌ Error changing project: {e}")


@project_app.command("current")
def project_current():
    """Show the current active project"""
    try:
        current = get_current_project()
        if not current:
            console.print("No current project set.")
            return
        
        console.print(f"Current project: {current}")
        project = get_project(current)
        if project:
            jira_project = project.get("jira", {}).get("project", "Unknown")
            repos = project.get("repositories", {})
            default_repo = project.get("default_repo")
            console.print(f"🔗 Jira Project: {jira_project}")
            console.print(f"📁 Repositories: {len(repos)}")
            if default_repo:
                console.print(f"⭐ Default Repo: {default_repo}")
            if repos:
                for repo_path in repos.keys():
                    base_branch = repos[repo_path].get("base_branch", "main")
                    marker = " (default)" if repo_path == default_repo else ""
                    console.print(f"   - {repo_path} (base: {base_branch}){marker}")
                    
    except Exception as e:
        console.print(f"❌ Error getting current project: {e}")


@project_app.command("migrate")
def project_migrate():
    """Migrate existing single-project config to project-based config"""
    try:
        if migrate_to_project_config():
            current = get_current_project()
            console.print(f"✅ Migrated to project-based configuration")
            console.print(f"🔄 Current project: {current}")
        else:
            console.print("ℹ️  Migration not needed or already completed")
            
    except Exception as e:
        console.print(f"❌ Error migrating configuration: {e}")


@project_app.command("set-default-repo")
def project_set_default_repo(
    repo_path: str = typer.Argument(..., help="Repository path to set as default")
):
    """Set the default repository for the current project"""
    from workflow.projects import set_default_repo, get_current_project
    
    try:
        current_project = get_current_project()
        if not current_project:
            console.print("❌ No current project set. Use 'wf project change <name>' first.")
            return
        
        set_default_repo(current_project, repo_path)
        console.print(f"✅ Set default repository for project '{current_project}':")
        console.print(f"   📁 {repo_path}")
        
    except Exception as e:
        console.print(f"❌ Error setting default repository: {e}")


@project_app.command("add-repo")
def project_add_repo(
    project_name: str = typer.Argument(..., help="Project name"),
    repo_path: str = typer.Argument(..., help="Path to repository"),
    base_branch: str = typer.Option("main", help="Base branch for this repository")
):
    """Add a repository to a specific project"""
    from workflow.projects import get_project
    from workflow.config import load_config, save_config
    from workflow.git_utils import get_available_branches
    
    try:
        # Check if project exists
        project = get_project(project_name)
        if not project:
            console.print(f"❌ Project '{project_name}' not found.")
            console.print(f"💡 Use 'wf project list' to see available projects.")
            return
        
        # Verify the repository path exists
        from pathlib import Path
        if not Path(repo_path).exists():
            console.print(f"❌ Repository path '{repo_path}' does not exist.")
            return
        
        # Verify it's a git repository
        try:
            branches = get_available_branches(repo_path)
            if not branches:
                console.print(f"❌ No git branches found in '{repo_path}'.")
                return
        except Exception as e:
            console.print(f"❌ Error accessing repository: {e}")
            return
        
        # Add repository to project config
        cfg = load_config()
        if "projects" not in cfg or project_name not in cfg["projects"]:
            console.print(f"❌ Project '{project_name}' not found in config.")
            return
        
        if "repositories" not in cfg["projects"][project_name]:
            cfg["projects"][project_name]["repositories"] = {}
        
        cfg["projects"][project_name]["repositories"][repo_path] = {"base_branch": base_branch}
        save_config(cfg)
        
        console.print(f"✅ Added repository to project '{project_name}':")
        console.print(f"   📁 {repo_path} (base: {base_branch})")
        
    except Exception as e:
        console.print(f"❌ Error adding repository: {e}")


@app.command()
def init(
    setup_variables: bool = typer.Option(True, "--setup-variables", help="Set up default variables")
):
    from workflow.config import CFG
    
    if CFG.exists():
        console.print("✅ Workflow configuration already exists at ~/.wf/config.yaml")
        console.print("💡 Use 'wf config show' to view current configuration")
        
        if setup_variables:
            from workflow.config import add_default_variables
            try:
                add_default_variables()
                console.print("🎯 Default variables added successfully!")
                console.print("💡 Use 'wf variables list' to see available variables")
            except Exception as e:
                console.print(f"⚠️  Could not add default variables: {e}")
        return
    
    cfg = load_config(interactive=True)
    console.print("✅ Workflow configuration created successfully!")
    console.print(f"📁 Config saved to: {CFG}")
    console.print("💡 Use 'wf config show' to view your configuration")
    
    if setup_variables:
        from workflow.config import add_default_variables
        try:
            add_default_variables()
            console.print("🎯 Default variables added successfully!")
            console.print("💡 Use 'wf variables list' to see available variables")
        except Exception as e:
            console.print(f"⚠️  Could not add default variables: {e}")

# Create config subcommand group
config_app = typer.Typer(help="Configure workflow settings and repositories", no_args_is_help=True)
app.add_typer(config_app, name="config")

# Create variables subcommand group
variables_app = typer.Typer(help="Manage dynamic variables with caching", no_args_is_help=True)
app.add_typer(variables_app, name="variables")

@config_app.command()
def repo_add(
    path_or_url: str = typer.Argument(..., help="Path to repository or GitHub URL"),
    base_branch: str = typer.Option(None, help="Base branch for this repository (auto-detected if not specified)"),
    clone_dir: str = typer.Option(None, help="Directory to clone into (default: ~/code)")
):
    """Add a repository to the configuration or clone from GitHub URL"""
    from workflow.git_utils import (
        get_available_branches, 
        is_github_url, 
        clone_repository, 
        extract_repo_name_from_url,
        generate_clone_path,
        ensure_code_directory
    )
    from pathlib import Path
    from rich.prompt import Prompt
    from rich.console import Console
    
    console = Console(color_system=None)
    repo_path = path_or_url
    
    # Check if it's a GitHub URL
    if is_github_url(path_or_url):
        console.print(f"🔗 Detected GitHub URL: {path_or_url}")
        
        # Extract repo name and generate clone path
        repo_name = extract_repo_name_from_url(path_or_url)
        
        if clone_dir:
            target_path = Path(clone_dir) / repo_name
        else:
            code_dir = ensure_code_directory()
            target_path = generate_clone_path(repo_name, code_dir)
        
        console.print(f"📁 Will clone to: {target_path}")
        
        # Ask user if they want to clone
        clone_choice = Prompt.ask(
            "Clone this repository?",
            choices=["y", "n"],
            default="y"
        )
        
        if clone_choice.lower() != "y":
            return
        
        try:
            repo_path = clone_repository(path_or_url, str(target_path))
        except Exception as e:
            console.print(f"❌ Failed to clone repository: {e}")
            return
    
    else:
        # It's a local path
        if not Path(path_or_url).exists():
            console.print(f"❌ Error: Path '{path_or_url}' does not exist")
            console.print(f"💡 If you want to clone a repository, provide a GitHub URL instead")
            return
    
    # Check if it's a valid git repository
    try:
        available_branches = get_available_branches(repo_path)
        if not available_branches:
            console.print(f"❌ Error: No branches found in repository at '{repo_path}'")
            return
            
        # Use smart default if no base branch specified
        if not base_branch:
            base_branch = get_default_branch(repo_path)
            console.print(f"🔍 Auto-detected base branch: {base_branch}")
        
        # Check if the base branch exists
        if base_branch not in available_branches:
            console.print(f"⚠️  Warning: Base branch '{base_branch}' not found in repository")
            
            # Only show available branches if standard branches are missing
            standard_branches = ["main", "master", "develop", "dev", "development"]
            if not any(branch in available_branches for branch in standard_branches):
                console.print(f"Available branches: {', '.join(available_branches[:10])}")
                if len(available_branches) > 10:
                    console.print(f"... and {len(available_branches) - 10} more")
            
            use_anyway = Prompt.ask(
                f"Add repository anyway with base branch '{base_branch}'?",
                choices=["y", "n"],
                default="n"
            )
            if use_anyway.lower() != "y":
                return
    except Exception as e:
        console.print(f"❌ Error accessing repository at '{repo_path}': {e}")
        return
    
    add_repository(repo_path, base_branch)
    console.print(f"✅ Added repository {repo_path} with base branch {base_branch}")
    console.print(f"   Available branches: {len(available_branches)} total")

@config_app.command()
def repo_list():
    """List all configured repositories"""
    # First check for project-specific repositories
    from workflow.projects import get_current_project, get_project_repositories
    current_project = get_current_project()
    project_repos = get_project_repositories()
    global_repos = get_repositories()

    # Combine both sources
    all_repos = {}
    all_repos.update(global_repos)
    all_repos.update(project_repos)

    if not all_repos:
        typer.echo("No repositories configured")
        return

    if current_project:
        typer.echo(f"📁 Project: {current_project}")
        typer.echo("")

    for path, config in all_repos.items():
        typer.echo(f"{path} (base: {config.get('base_branch', 'main')})")

@config_app.command()
def set(
    key: str = typer.Argument(..., help="Configuration key (git_enabled, github_enabled, github_token)"),
    value: str = typer.Argument(..., help="Configuration value (true/false or GitHub token)")
):
    """Set configuration values"""
    cfg = load_config()
    
    if key in ["git_enabled", "github_enabled"]:
        if value.lower() in ["true", "yes", "y", "1"]:
            cfg[key] = True
        elif value.lower() in ["false", "no", "n", "0"]:
            cfg[key] = False
        else:
            typer.echo("Error: Value must be true/false, yes/no, y/n, or 1/0")
            return
    elif key == "github_token":
        import os
        cfg["github"] = cfg.get("github", {})
        cfg["github"]["token"] = value
        cfg["github_enabled"] = True
        # Also set environment variable for current session
        os.environ["GITHUB_TOKEN"] = value
        typer.echo("✅ GitHub token saved and GitHub integration enabled")
    else:
        typer.echo("Error: Supported keys are 'git_enabled', 'github_enabled', and 'github_token'")
        return
    
    save_config(cfg)
    if key != "github_token":
        typer.echo(f"✅ Set {key} to {cfg[key]}")

@config_app.command()
def show():
    """Show current configuration"""
    cfg = load_config()
    import os
    typer.echo(f"Git enabled: {cfg.get('git_enabled', True)}")
    typer.echo(f"GitHub enabled: {cfg.get('github_enabled', True)}")
    
    # Show GitHub token status
    github_token_in_config = cfg.get("github", {}).get("token")
    github_token_in_env = "GITHUB_TOKEN" in os.environ
    typer.echo(f"GitHub token in config: {'✅' if github_token_in_config else '❌'}")
    typer.echo(f"GitHub token in environment: {'✅' if github_token_in_env else '❌'}")
    
    from workflow.projects import get_project_repositories
    repos = get_project_repositories()
    typer.echo(f"Repositories: {len(repos)} configured")
    for path, config in repos.items():
        typer.echo(f"  {path} (base: {config.get('base_branch', 'main')})")



@config_app.command()
def repo_discover(
    code_dir: str = typer.Option(None, help="Code directory to search (default: ~/code)")
):
    """Discover git repositories in code directory"""
    from pathlib import Path
    from rich.console import Console
    
    console = Console(color_system=None)
    
    if not code_dir:
        code_dir = str(Path.home() / "code")
    
    code_path = Path(code_dir)
    
    if not code_path.exists():
        console.print(f"❌ Code directory '{str(code_path)}' does not exist")
        console.print(f"💡 Use 'wf repo-add <github-url>' to clone a repository")
        return
    
    # Find all git repositories
    git_repos = []
    for item in code_path.iterdir():
        if item.is_dir():
            git_dir = item / ".git"
            if git_dir.exists():
                git_repos.append(item)
    
    if not git_repos:
        console.print(f"No git repositories found in '{code_path}'")
        console.print(f"💡 Use 'wf repo-add <github-url>' to clone a repository")
        return
    
    console.print(f"Found {len(git_repos)} git repositories in '{code_path}':")
    for repo in git_repos:
        try:
            from workflow.git_utils import get_available_branches
            branches = get_available_branches(str(repo))
            # Determine main branch with priority order
            standard_branches = ["main", "master", "develop", "dev", "development"]
            main_branch = next((branch for branch in standard_branches if branch in branches), branches[0] if branches else "unknown")
            console.print(f"  📁 {repo} (main: {main_branch}, {len(branches)} branches total)")
        except Exception as e:
            console.print(f"  📁 {repo} (error reading: {e})")
    
    console.print(f"\n💡 Use 'wf repo-add <path>' to add any of these repositories")

def autocomplete_active_tasks(ctx, args, incomplete: str):
    """
    Autocomplete only incomplete tasks with status, filtered by current project.
    Also includes recent tasks from state if no project match.
    """
    from workflow.projects import get_current_project, get_project
    from workflow.state import get_current_task, get_previous_task
    
    current_project = get_current_project()
    project_key = None
    if current_project:
        project_config = get_project(current_project)
        if project_config:
            project_key = project_config.get("jira", {}).get("project")
    
    cfg = load_config()
    backend = get_backend(cfg)
    tasks = backend.get_assigned_tasks()
    active_tasks = []
    incomplete_lower = incomplete.lower() if incomplete else ""
    
    current_task = get_current_task()
    previous_task = get_previous_task()
    recent_keys = []
    if current_task and current_task.key:
        recent_keys.append(current_task.key)
    if previous_task and previous_task.key:
        recent_keys.append(previous_task.key)
    
    for t in tasks:
        if project_key and not t.key.upper().startswith(project_key.upper()):
            continue
        try:
            if not backend.is_done(t) and t.key.lower().startswith(incomplete_lower):
                active_tasks.append(t.key)
        except:
            if t.key.lower().startswith(incomplete_lower):
                active_tasks.append(t.key)
    
    for key in recent_keys:
        if key and key.lower().startswith(incomplete_lower) and key not in active_tasks:
            active_tasks.append(key)
    
    return active_tasks

def autocomplete_branches(ctx, args, incomplete: str):
    """
    Autocomplete branch names with fuzzy matching.
    """
    from workflow.git_utils import get_available_branches, fuzzy_match_branches
    
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

@config_app.command()
def repo_branches(
    repo_path: str = typer.Argument(..., help="Path to repository"),
    search: str = typer.Option(None, autocompletion=autocomplete_branches, help="Search/filter branches")
):
    """List available branches in a repository with fuzzy search"""
    from workflow.git_utils import get_available_branches, fuzzy_match_branches
    from pathlib import Path
    
    if not Path(repo_path).exists():
        typer.echo(f"❌ Error: Path '{repo_path}' does not exist")
        return
    
    try:
        branches = get_available_branches(repo_path)
        if not branches:
            typer.echo("No branches found in repository")
            return
        
        if search:
            branches = fuzzy_match_branches(branches, search)
            if not branches:
                typer.echo(f"No branches found matching '{search}'")
                return
        
        # Highlight standard branches
        standard_branches = ["main", "master", "develop", "dev", "development"]
        typer.echo(f"Available branches in {repo_path}:")
        for branch in branches:
            if branch in standard_branches:
                typer.echo(f"  {branch} (standard)")
            else:
                typer.echo(f"  {branch}")
    except Exception as e:
        typer.echo(f"❌ Error accessing repository: {e}")

@config_app.command()
def add_field(
    name: str = typer.Argument(..., help="Name of the custom field (used in --field-name=value)"),
    field_identifier: str = typer.Argument(None, help="Jira field name or ID (auto-detected if not provided)"),
    project_key: str = typer.Option("", help="Project key this field applies to (optional)"),
    default: str = typer.Option("", help="Default value for this field (optional - use {active_sprint}, {active_sprint_id}, or {active_sprint_state} variables)")
):
    """Add a custom field configuration - auto-resolves field names to IDs"""
    from workflow.config import add_custom_field
    from workflow.backends import get_backend
    from workflow.config import load_config
    
    cfg = load_config()
    backend = get_backend(cfg)
    
    # If no field identifier provided, try to use the name as the field name
    if not field_identifier:
        field_identifier = name
        typer.echo(f"🔍 Auto-detecting field for '{name}'...")
    
    # Try to resolve field name to ID
    if not field_identifier.startswith('customfield_'):
        # This looks like a field name, resolve it
        typer.echo(f"🔍 Resolving field '{field_identifier}'...")
        field_info = backend.find_field_by_name(field_identifier)
        
        if not field_info:
            # Try searching for similar fields
            matches = backend.search_fields(field_identifier)
            if matches:
                typer.echo(f"💡 Found similar fields:")
                for name, info in list(matches.items())[:5]:  # Show top 5 matches
                    typer.echo(f"  - {info['name']}: {info['id']}")
                typer.echo(f"💡 Use 'wf config search-fields \"{field_identifier}\"' for more options")
            else:
                typer.echo(f"❌ No field found matching '{field_identifier}'")
                typer.echo(f"💡 Use 'wf config search-fields' to see all available fields")
            raise typer.Exit(1)
        
        field_id = field_info['id']
        resolved_name = field_info['name']
        typer.echo(f"✅ Resolved '{field_identifier}' → {resolved_name} ({field_id})")
    else:
        # This is already a field ID
        field_id = field_identifier
        resolved_name = field_identifier
    
    add_custom_field(name, field_id, project_key, default)
    project_desc = f" for project {project_key}" if project_key else ""
    default_desc = f" with default '{default}'" if default else ""
    typer.echo(f"✅ Added custom field '{name}' with ID '{field_id}'{project_desc}{default_desc}")

@config_app.command()
def search_fields(
    query: str = typer.Argument("", help="Search query for field names (empty = show all)")
):
    """Search Jira fields by name"""
    from workflow.backends import get_backend
    from workflow.config import load_config
    
    cfg = load_config()
    backend = get_backend(cfg)
    
    matches = backend.search_fields(query)
    
    if not matches:
        typer.echo("No fields found.")
        return
    
    typer.echo(f"Found {len(matches)} fields:")
    for name, info in sorted(matches.items()):
        field_type = info.get('schema', {}).get('type', 'unknown')
        custom_marker = " (custom)" if info.get('custom') else ""
        typer.echo(f"  {info['name']}: {info['id']} [{field_type}]{custom_marker}")

@config_app.command()
def discover_fields():
    """Refresh field discovery cache"""
    from workflow.backends import get_backend
    from workflow.config import load_config
    
    cfg = load_config()
    backend = get_backend(cfg)
    
    # Clear cache and refresh
    backend._fields_cache = None
    backend._fields_cache_time = None
    fields = backend._get_fields()
    
    typer.echo(f"✅ Discovered {len(fields)} Jira fields")
    typer.echo("💡 Use 'wf config search-fields <query>' to find specific fields")

@config_app.command()
def list_fields():
    """List all configured custom fields"""
    from workflow.config import get_custom_fields
    
    fields = get_custom_fields()
    if not fields:
        typer.echo("No custom fields configured.")
        return
    
    typer.echo("Configured custom fields:")
    for name, config in fields.items():
        project_info = f" (project: {config['project_key']})" if config.get('project_key') else ""
        default_info = f" (default: {config['default_value']})" if config.get('default_value') else ""
        typer.echo(f"  {name}: {config['field_id']}{project_info}{default_info}")

@config_app.command()
def set_default(
    field_name: str = typer.Argument(..., help="Name of the custom field"),
    value: str = typer.Argument(..., help="Default value to set")
):
    """Set default value for a custom field"""
    from workflow.config import set_field_default
    
    try:
        set_field_default(field_name, value)
        typer.echo(f"✅ Set default value for '{field_name}' to '{value}'")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")

@config_app.command()
def field_values(
    field_name: str = typer.Argument(..., help="Name of configured field")
):
    """Show all possible values for a custom field"""
    from workflow.config import get_custom_fields
    from workflow.backends import get_backend
    from workflow.config import load_config
    
    cfg = load_config()
    backend = get_backend(cfg)
    
    # Get the field configuration
    custom_fields = get_custom_fields()
    if field_name not in custom_fields:
        typer.echo(f"❌ Field '{field_name}' not configured. Use 'wf config list-fields' to see configured fields.")
        return
    
    field_config = custom_fields[field_name]
    field_id = field_config['field_id']
    
    # Get field metadata from Jira
    field_metadata = backend.get_field_metadata(field_id)
    
    if not field_metadata:
        typer.echo(f"❌ Could not fetch metadata for field '{field_name}' ({field_id})")
        return
    
    typer.echo(f"Field information for '{field_name}':")
    typer.echo(f"  ID: {field_id}")
    typer.echo(f"  Type: {field_metadata.get('schema', {}).get('type', 'unknown')}")
    
    # Show available options if it's an option field
    allowed_values = field_metadata.get('allowedValues', [])
    if allowed_values:
        typer.echo(f"  Available values ({len(allowed_values)}):")
        for i, option in enumerate(allowed_values, 1):
            option_value = option.get('value', str(option))
            option_id = option.get('id', 'N/A')
            typer.echo(f"    {i}. {option_value} (ID: {option_id})")
    else:
        typer.echo("  This field accepts any text value")
    
    # Show current default if set
    current_default = field_config.get('default_value')
    if current_default:
        typer.echo(f"  Current default: '{current_default}'")
    else:
        typer.echo("  No default currently set")

@config_app.command()
def field_options(
    field_name: str = typer.Argument(..., help="Name of configured field")
):
    """Show available options for a custom field"""
    from workflow.config import get_custom_fields
    from workflow.backends import get_backend
    from workflow.config import load_config
    
    cfg = load_config()
    backend = get_backend(cfg)
    
    # Get the field configuration
    custom_fields = get_custom_fields()
    if field_name not in custom_fields:
        typer.echo(f"❌ Field '{field_name}' not configured. Use 'wf config list-fields' to see configured fields.")
        return
    
    field_config = custom_fields[field_name]
    field_id = field_config['field_id']
    
    # Get available options
    options = backend.get_field_options(field_id)
    
    if not options:
        typer.echo(f"Field '{field_name}' has no predefined options (may be a text field)")
        return
    
    typer.echo(f"Available options for '{field_name}':")
    for option in options:
        option_value = option.get('value', str(option))
        typer.echo(f"  - {option_value}")

@config_app.command()
def test_field(
    field_name: str = typer.Argument(..., help="Name of configured field"),
    test_value: str = typer.Argument(..., help="Value to test")
):
    """Test a field value without creating a ticket"""
    from workflow.config import get_custom_fields
    from workflow.backends import get_backend
    from workflow.config import load_config
    
    cfg = load_config()
    backend = get_backend(cfg)
    
    # Get the field configuration
    custom_fields = get_custom_fields()
    if field_name not in custom_fields:
        typer.echo(f"❌ Field '{field_name}' not configured. Use 'wf config list-fields' to see configured fields.")
        return
    
    field_config = custom_fields[field_name]
    field_id = field_config['field_id']
    
    # Get field info for type processing
    fields_dict = backend._get_fields()
    
    # Find the field by ID - case insensitive lookup
    field_info = None
    for info in fields_dict.values():
        if info['id'].lower() == field_id.lower():
            field_info = info
            break
    
    if not field_info:
        typer.echo(f"❌ Could not find field info for '{field_name}' ({field_id})")
        return
    
    field_schema = field_info.get('schema', {})
    field_type = field_schema.get('type', 'unknown')
    
    typer.echo(f"🧪 Testing field '{field_name}' ({field_id})")
    typer.echo(f"   Type: {field_type}")
    typer.echo(f"   Testing value: '{test_value}'")
    
    # Process the value
    try:
        processed_value = backend._process_field_value(test_value, field_type, field_schema)
        typer.echo(f"✅ Processed value: {processed_value}")
        
        # If it's an option field, try to get available options
        if field_type == 'option':
            options = backend.get_field_options(field_id)
            if options:
                typer.echo(f"💡 Available options for this field:")
                for option in options[:10]:  # Show first 10 options
                    option_val = option.get('value', str(option))
                    marker = " ← TESTED" if option_val == str(test_value) else ""
                    typer.echo(f"   - {option_val}{marker}")
            else:
                typer.echo("ℹ️  This option field doesn't have predefined choices")
                
    except Exception as e:
        typer.echo(f"❌ Error processing value: {e}")

@config_app.command()
def clear_default(
    field_name: str = typer.Argument(..., help="Name of the custom field to remove")
):
    """Clear default value for a custom field"""
    from workflow.config import clear_field_default
    
    try:
        clear_field_default(field_name)
        typer.echo(f"✅ Cleared default value for '{field_name}'")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")

@config_app.command()
def remove_field(
    name: str = typer.Argument(..., help="Name of the custom field to remove")
):
    """Remove a custom field configuration"""
    from workflow.config import load_config, save_config
    
    cfg = load_config()
    if "custom_fields" not in cfg or name not in cfg["custom_fields"]:
        typer.echo(f"❌ Custom field '{name}' not found.")
        return
    
    del cfg["custom_fields"][name]
    save_config(cfg)
    typer.echo(f"✅ Removed custom field '{name}'")

@config_app.command()
def auto_assign_sprint(
    enabled: bool = typer.Argument(..., help="Enable or disable automatic sprint assignment")
):
    """Configure automatic sprint assignment for new tasks"""
    from workflow.config import load_config, save_config
    
    cfg = load_config()
    cfg["auto_assign_sprint"] = enabled
    save_config(cfg)
    
    if enabled:
        console.print("✅ Automatic sprint assignment enabled")
        console.print("💡 New tasks will be automatically assigned to the current active sprint")
        console.print("⚠️  If you get permission errors, try: wf config auto-assign-sprint false")
    else:
        console.print("❌ Automatic sprint assignment disabled")
        console.print("💡 New tasks will not be automatically assigned to sprints")

@config_app.command()
def list_boards():
    """List all Jira boards/teams you have access to"""
    from workflow.config import load_config
    from workflow.backends import get_backend
    from rich.table import Table
    
    try:
        cfg = load_effective_config()
        backend = get_backend(cfg)
        
        console.print("🔍 Fetching Jira boards...")
        
        # Get all boards using the Jira API directly
        jira_url = backend.client._options['server'].rstrip('/')
        boards_url = f"{jira_url}/rest/agile/1.0/board"
        
        # Get project key from config
        project_key = cfg.get("jira", {}).get("project", "DEV")
        params = {"projectKeyOrId": project_key}
        
        response = backend.client._session.get(boards_url, params=params)
        
        if response.status_code != 200:
            console.print(f"❌ Could not fetch boards for project {project_key}")
            return
        
        response_data = response.json()
        if not response_data:
            console.print("❌ Invalid response when fetching boards")
            return
            
        boards = response_data.get("values", [])
        if not boards:
            console.print(f"⚠️  No boards found for project {project_key}")
            return
        
        # Create a table to display boards
        table = Table(title=f"Jira Boards for Project {project_key}")
        table.add_column("Board ID", style="cyan", no_wrap=True)
        table.add_column("Board Name", style="green")
        table.add_column("Board Type", style="magenta")
        table.add_column("Sprint Support", style="yellow")
        table.add_column("Active Sprints", style="blue")
        
        active_sprints_info = {}
        
        # Check each board for sprint support and active sprints
        for board in boards:
            board_id = board["id"]
            board_name = board.get("name", f"Board {board_id}")
            board_type = board.get("type", "Unknown")
            
            try:
                # Check if board supports sprints
                sprints_url = f"{jira_url}/rest/agile/1.0/board/{board_id}/sprint"
                sprints_params = {"state": "active", "maxResults": 1}  # Just check if there are any active sprints
                sprints_response = backend.client._session.get(sprints_url, params=sprints_params)
                
                if sprints_response.status_code == 200:
                    sprints_data = sprints_response.json()
                    if sprints_data:
                        active_sprints = sprints_data.get("values", [])
                        sprint_count = len(active_sprints)
                        sprint_support = "✅ Yes"
                        
                        # Store info about active sprints if any
                        if sprint_count > 0:
                            active_sprints_info[board_id] = {
                                'count': sprint_count,
                                'sprints': active_sprints[:3]  # Show first 3 sprints
                            }
                    else:
                        sprint_support = "✅ Yes"
                        sprint_count = 0
                else:
                    sprint_support = "❌ No"
                    sprint_count = 0
                    
            except Exception:
                sprint_support = "❌ No"
                sprint_count = 0
            
            # Highlight your preferred board if configured
            current_prefs = cfg.get("sprint_preferences", {})
            preferred_board_id = current_prefs.get("board_id")
            preferred_board_name = current_prefs.get("board_name", "")
            
            row_name = board_name
            if (preferred_board_id and preferred_board_id == board_id) or \
               (preferred_board_name and preferred_board_name.lower() in board_name.lower()):
                row_name = f"🎯 {board_name}"
            
            table.add_row(
                str(board_id),
                row_name,
                board_type,
                sprint_support,
                str(sprint_count) if sprint_count > 0 else "None"
            )
        
        console.print(table)
        
        # Show active sprint details for boards with sprints
        if active_sprints_info:
            console.print("\n📋 Active Sprints:")
            for board_id, info in active_sprints_info.items():
                board = next((b for b in boards if b["id"] == board_id), None)
                if board:
                    console.print(f"\n🏃 Board: {board.get('name')} (ID: {board_id})")
                    for sprint in info['sprints']:
                        console.print(f"   • {sprint['name']} (ID: {sprint['id']})")
                    if info['count'] > len(info['sprints']):
                        console.print(f"   ... and {info['count'] - len(info['sprints'])} more")
        
        # Show current configuration
        current_prefs = cfg.get("sprint_preferences", {})
        if current_prefs:
            console.print(f"\n⚙️  Current Configuration:")
            if current_prefs.get("board_id"):
                console.print(f"   Board ID: {current_prefs['board_id']}")
            if current_prefs.get("board_name"):
                console.print(f"   Board Name: {current_prefs['board_name']}")
            if current_prefs.get("sprint_keywords"):
                console.print(f"   Keywords: {', '.join(current_prefs['sprint_keywords'])}")
        else:
            console.print(f"\n💡 To configure a specific board:")
            console.print(f"   wf config sprint-preferences --board-id <ID>")
            console.print(f"   wf config sprint-preferences --board-name \"Board Name\"")
            console.print(f"   wf config sprint-preferences --keywords \"core,team,sprint\"")
        
    except Exception as e:
        console.print(f"❌ Error fetching boards: {e}")
        console.print("💡 Check your Jira configuration and permissions")

@config_app.command()
def config_migrate_jira_token():
    """Migrate Jira API token from config to macOS Keychain"""
    from workflow.config import migrate_jira_token_to_keychain
    
    success, message = migrate_jira_token_to_keychain()
    if success:
        console.print(f"✅ {message}")
    else:
        console.print(f"❌ {message}")


@config_app.command()
def config_set_jira_token(token: str = typer.Argument(..., help="Jira API token")):
    """Store Jira API token in macOS Keychain"""
    from workflow.config import save_jira_token_to_keychain
    
    if save_jira_token_to_keychain(token):
        console.print("✅ Jira token saved to macOS Keychain")
    else:
        console.print("❌ Failed to save token to Keychain")


@config_app.command()
def config_delete_jira_token():
    """Remove Jira API token from macOS Keychain"""
    from workflow.config import delete_jira_token_from_keychain
    
    if delete_jira_token_from_keychain():
        console.print("✅ Jira token removed from macOS Keychain")
    else:
        console.print("❌ Failed to remove token from Keychain (may not exist)")


@config_app.command()
def sprint_preferences(
    board_name: str = typer.Option("", help="Preferred board name or ID"),
    keywords: str = typer.Option("", help="Comma-separated keywords to prioritize in sprint/board names"),
    board_id: int = typer.Option(None, help="Specific board ID (takes precedence over board_name)"),
    clear: bool = typer.Option(False, "--clear", help="Clear all sprint preferences")
):
    """Configure sprint selection preferences"""
    from workflow.config import load_config, save_config
    
    cfg = load_config()
    
    if clear:
        if "sprint_preferences" in cfg:
            del cfg["sprint_preferences"]
        save_config(cfg)
        console.print("✅ Cleared all sprint preferences")
        return
    
    if "sprint_preferences" not in cfg:
        cfg["sprint_preferences"] = {}
    
    prefs = cfg["sprint_preferences"]
    
    if board_id:
        prefs["board_id"] = board_id
        console.print(f"✅ Set specific board ID to: {board_id}")
        console.print("💡 This will only check the specified board for sprints")
    elif board_name:
        prefs["board_name"] = board_name
        console.print(f"✅ Set preferred board name to: {board_name}")
    
    if keywords:
        keyword_list = [k.strip() for k in keywords.split(",") if k.strip()]
        prefs["sprint_keywords"] = keyword_list
        console.print(f"✅ Set sprint keywords to: {', '.join(keyword_list)}")
    
    if not board_name and not keywords and not board_id:
        console.print("Current sprint preferences:")
        if prefs:
            console.print(f"  Board ID: {prefs.get('board_id', 'None')}")
            console.print(f"  Board name: {prefs.get('board_name', 'None')}")
            console.print(f"  Keywords: {', '.join(prefs.get('sprint_keywords', []))}")
        else:
            console.print("  No preferences configured")
            console.print("💡 Tip: Use --board-id to target a specific board directly")
        return
    
    save_config(cfg)
    console.print("💡 Sprint selection will now use your preferences")
    if board_id:
        console.print("⚡ Using specific board ID for faster sprint detection")
    else:
        console.print("🔍 Will search boards and prioritize your preferences")

@app.command()
def branch(
    repo_path: str = typer.Option(None, help="Repository path"),
    checkout: str = typer.Option(None, autocompletion=autocomplete_branches, help="Branch to checkout")
):
    """Branch management with fuzzy selection"""
    from workflow.git_utils import get_available_branches, fuzzy_match_branches, checkout_branch
    from rich.console import Console
    from rich.prompt import Prompt
    from workflow.projects import get_project_repositories

    console = Console(color_system=None)

    # Check both global repositories AND project-specific repositories
    global_repos = get_repositories()
    project_repos = get_project_repositories()

    # Merge both sources
    repos = {}
    repos.update(global_repos)
    repos.update(project_repos)

    if not repos and not repo_path:
        typer.echo("No repositories configured. Use 'wf repo-add' to add one.")
        return
    
    if not repo_path and len(repos) == 1:
        repo_path = list(repos.keys())[0]
    elif not repo_path:
        typer.echo("Multiple repositories configured. Please specify --repo-path")
        return
    
    if checkout:
        # Direct checkout with specified branch
        if checkout_branch(checkout, repo_path):
            console.print(f"✅ Switched to branch '{checkout}'")
        else:
            console.print(f"❌ Failed to checkout branch '{checkout}'")
        return
    
    # Interactive branch selection
    available_branches = get_available_branches(repo_path)
    if not available_branches:
        console.print("No branches found in repository")
        return
    
    current_branch = None
    try:
        from git import Repo
        repo = Repo(repo_path)
        current_branch = repo.active_branch.name
    except:
        pass
    
    console.print(f"Current branch: {current_branch or 'unknown'}")
    console.print("\nAvailable branches:")
    
    # Highlight standard branches and current branch
    standard_branches = ["main", "master", "develop", "dev", "development"]
    for branch in available_branches:
        marker = ""
        if branch == current_branch:
            marker = " → (current)"
        elif branch in standard_branches:
            marker = " (standard)"
        console.print(f"  {branch}{marker}")
    
    while True:
        query = Prompt.ask(
            f"Type branch name or part of name to checkout (or 'cancel' to abort)",
            default=""
        ).strip()
        
        if query.lower() == 'cancel':
            return
        
        if not query:
            continue
        
        # Get fuzzy matched branches
        matched_branches = fuzzy_match_branches(available_branches, query, limit=5)
        
        if not matched_branches:
            console.print(f"❌ No branches found matching '{query}'")
            continue
        
        if len(matched_branches) == 1:
            # Single match, checkout it
            selected_branch = matched_branches[0]
            if selected_branch == current_branch:
                console.print(f"Already on branch '{selected_branch}'")
            elif checkout_branch(selected_branch, repo_path):
                console.print(f"✅ Switched to branch '{selected_branch}'")
                return
            else:
                console.print(f"❌ Failed to checkout branch '{selected_branch}'")
        else:
            # Multiple matches, show them
            console.print(f"Found {len(matched_branches)} branches matching '{query}':")
            for i, branch in enumerate(matched_branches, 1):
                marker = " (current)" if branch == current_branch else ""
                console.print(f"  {i}. {branch}{marker}")
            
            choice = Prompt.ask(
                f"Select branch (1-{len(matched_branches)}) or type more to refine search",
                choices=[str(i) for i in range(1, len(matched_branches) + 1)] + ["more"]
            )
            
            if choice == "more":
                # Continue loop with same query but show more results
                more_matches = fuzzy_match_branches(available_branches, query, limit=15)
                if len(more_matches) > len(matched_branches):
                    console.print(f"All matches for '{query}':")
                    for i, branch in enumerate(more_matches, 1):
                        marker = " (current)" if branch == current_branch else ""
                        console.print(f"  {i}. {branch}{marker}")
                    
                    choice = Prompt.ask(
                        f"Select branch (1-{len(more_matches)})",
                        choices=[str(i) for i in range(1, len(more_matches) + 1)]
                    )
                    
                    selected_index = int(choice) - 1
                    selected_branch = more_matches[selected_index]
                    if selected_branch == current_branch:
                        console.print(f"Already on branch '{selected_branch}'")
                    elif checkout_branch(selected_branch, repo_path):
                        console.print(f"✅ Switched to branch '{selected_branch}'")
                        return
                    else:
                        console.print(f"❌ Failed to checkout branch '{selected_branch}'")
            else:
                selected_index = int(choice) - 1
                selected_branch = matched_branches[selected_index]
                if selected_branch == current_branch:
                    console.print(f"Already on branch '{selected_branch}'")
                elif checkout_branch(selected_branch, repo_path):
                    console.print(f"✅ Switched to branch '{selected_branch}'")
                    return
                else:
                    console.print(f"❌ Failed to checkout branch '{selected_branch}'")



@app.command()
def create(
    title: str = typer.Argument(..., help="Task title"),
    description: str = typer.Argument("", help="Task description"),
    issue_type: str = typer.Option("Task", help="Issue type (Task, Bug, Story, Epic)"),
):
    """
    Create a task without starting work on it:
    - Create the issue in Jira
    - Do NOT move to In Progress
    - Do NOT create git branch
    - Do NOT set as current task
    """
    cfg = load_effective_config()
    backend = get_backend(cfg)
    
    typer.echo(f"Creating task...")
    
    issue = backend.create_issue(title, description, issue_type)
    
    if issue:
        typer.echo(f"✅ Created task {issue.key}: {issue.title}")
        jira_url = cfg.get("jira", {}).get("url", "")
        if jira_url:
            jira_link = f"{jira_url.rstrip('/')}/browse/{issue.key}"
            typer.echo(f"🔗 View in Jira: {jira_link}")
    else:
        typer.echo(f"❌ Failed to create task")


instruct_app = typer.Typer(help="Manage instructions for the LLM", no_args_is_help=True)
app.add_typer(instruct_app, name="instruct")


@instruct_app.command("global")
def instruct_global(
    instruction: str = typer.Argument(None, help="Instruction text to pass to LLM"),
    clear: bool = typer.Option(False, "--clear", help="Clear global/cross-project instructions"),
    edit: bool = typer.Option(False, "--edit", help="Edit instructions in default editor"),
):
    """Set global instructions passed to LLM for all sessions across all projects"""
    cfg = load_config()
    
    if clear:
        if "ai_instructions" in cfg:
            if "global" in cfg["ai_instructions"]:
                del cfg["ai_instructions"]["global"]
                save_config(cfg)
                typer.echo("✅ Cleared global instructions")
            else:
                typer.echo("ℹ️  No global instructions to clear")
        else:
            typer.echo("ℹ️  No global instructions to clear")
        return
    
    if edit:
        current = cfg.get("ai_instructions", {}).get("global", "")
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(current)
            temp_path = f.name
        subprocess.run([os.environ.get('EDITOR', 'vi'), temp_path])
        with open(temp_path, 'r') as f:
            new_content = f.read().strip()
        os.unlink(temp_path)
        if not new_content:
            typer.echo("ℹ️  Empty instructions, clearing...")
            if "ai_instructions" in cfg:
                cfg["ai_instructions"].pop("global", None)
        else:
            if "ai_instructions" not in cfg:
                cfg["ai_instructions"] = {}
            cfg["ai_instructions"]["global"] = new_content
        save_config(cfg)
        typer.echo("✅ Updated global instructions")
        return
    
    if instruction is None:
        typer.echo("❌ Please provide an instruction, --edit, or --clear")
        return
    
    if "ai_instructions" not in cfg:
        cfg["ai_instructions"] = {}
    
    existing = cfg["ai_instructions"].get("global", "")
    if existing:
        cfg["ai_instructions"]["global"] = f"{existing}\n{instruction}"
    else:
        cfg["ai_instructions"]["global"] = instruction
    save_config(cfg)
    typer.echo(f"✅ Set global/cross-project instructions")
    typer.echo(f"📝 Instruction: {instruction}")


@instruct_app.command("project")
def instruct_project(
    project_name: str = typer.Argument(None, help="Project name (uses current project if not provided)"),
    instruction: str = typer.Argument(None, help="Instruction text to pass to LLM for the project"),
    clear: bool = typer.Option(False, "--clear", help="Clear project-level instructions"),
    edit: bool = typer.Option(False, "--edit", help="Edit instructions in default editor"),
):
    """Set instructions passed to LLM for a project (defaults to current project)"""
    from workflow.projects import get_current_project, list_projects
    
    cfg = load_config()
    
    # Determine which project to use
    project_key = project_name
    
    if not project_key:
        # Use current project
        project_key = get_current_project()
        if not project_key:
            # No current project, show available projects
            projects = list_projects()
            if projects:
                typer.echo("❌ No current project set. Available projects:")
                for p in projects:
                    typer.echo(f"  - {p}")
                typer.echo("\n💡 Use 'wf project change <name>' or specify project directly:")
                typer.echo("   wf instruct project <project-name> \"instruction\"")
                return
            else:
                typer.echo("❌ No projects configured.")
                typer.echo("💡 Use 'wf project add' to add a project.")
                return
    
    if clear:
        if "ai_instructions" in cfg and "projects" in cfg["ai_instructions"]:
            if project_key in cfg["ai_instructions"]["projects"]:
                del cfg["ai_instructions"]["projects"][project_key]
                save_config(cfg)
                typer.echo(f"✅ Cleared instructions for project: {project_key}")
            else:
                typer.echo(f"ℹ️  No instructions found for project: {project_key}")
        else:
            typer.echo(f"ℹ️  No instructions found for project: {project_key}")
        return
    
    if edit:
        current = cfg.get("ai_instructions", {}).get("projects", {}).get(project_key, "")
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(current)
            temp_path = f.name
        subprocess.run([os.environ.get('EDITOR', 'vi'), temp_path])
        with open(temp_path, 'r') as f:
            new_content = f.read().strip()
        os.unlink(temp_path)
        if not new_content:
            typer.echo("ℹ️  Empty instructions, clearing...")
            if "ai_instructions" in cfg and "projects" in cfg["ai_instructions"]:
                cfg["ai_instructions"]["projects"].pop(project_key, None)
        else:
            if "ai_instructions" not in cfg:
                cfg["ai_instructions"] = {}
            if "projects" not in cfg["ai_instructions"]:
                cfg["ai_instructions"]["projects"] = {}
            cfg["ai_instructions"]["projects"][project_key] = new_content
        save_config(cfg)
        typer.echo(f"✅ Updated instructions for project: {project_key}")
        return
    
    if instruction is None:
        instruction = typer.prompt(f"Enter instruction for project {project_key}")
        if not instruction:
            typer.echo("❌ No instruction provided.")
            return
    
    if "ai_instructions" not in cfg:
        cfg["ai_instructions"] = {}
    if "projects" not in cfg["ai_instructions"]:
        cfg["ai_instructions"]["projects"] = {}
    
    existing = cfg["ai_instructions"]["projects"].get(project_key, "")
    if existing:
        cfg["ai_instructions"]["projects"][project_key] = f"{existing}\n{instruction}"
    else:
        cfg["ai_instructions"]["projects"][project_key] = instruction
    save_config(cfg)
    typer.echo(f"✅ Set instructions for project: {project_key}")
    typer.echo(f"📝 Instruction: {instruction}")


@instruct_app.command("repo")
def instruct_repo(
    key: str = typer.Argument(..., help="Repository path (e.g., /Users/user/code/myrepo)"),
    instruction: str = typer.Argument(None, help="Instruction text to pass to LLM for this repo"),
    clear: bool = typer.Option(False, "--clear", help="Clear repo-level instructions"),
    edit: bool = typer.Option(False, "--edit", help="Edit instructions in default editor"),
):
    """Set instructions passed to LLM for a specific repository"""
    cfg = load_config()
    
    if clear:
        if "ai_instructions" in cfg and "repos" in cfg["ai_instructions"]:
            if key in cfg["ai_instructions"]["repos"]:
                del cfg["ai_instructions"]["repos"][key]
                save_config(cfg)
                typer.echo(f"✅ Cleared instructions for repo: {key}")
            else:
                typer.echo(f"ℹ️  No instructions found for repo: {key}")
        else:
            typer.echo(f"ℹ️  No instructions found for repo: {key}")
        return
    
    if edit:
        current = cfg.get("ai_instructions", {}).get("repos", {}).get(key, "")
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(current)
            temp_path = f.name
        subprocess.run([os.environ.get('EDITOR', 'vi'), temp_path])
        with open(temp_path, 'r') as f:
            new_content = f.read().strip()
        os.unlink(temp_path)
        if not new_content:
            typer.echo("ℹ️  Empty instructions, clearing...")
            if "ai_instructions" in cfg and "repos" in cfg["ai_instructions"]:
                cfg["ai_instructions"]["repos"].pop(key, None)
        else:
            if "ai_instructions" not in cfg:
                cfg["ai_instructions"] = {}
            if "repos" not in cfg["ai_instructions"]:
                cfg["ai_instructions"]["repos"] = {}
            cfg["ai_instructions"]["repos"][key] = new_content
        save_config(cfg)
        typer.echo(f"✅ Updated instructions for repo: {key}")
        return
    
    if instruction is None:
        typer.echo("❌ Please provide an instruction, --edit, or --clear")
        return
    
    if "ai_instructions" not in cfg:
        cfg["ai_instructions"] = {}
    if "repos" not in cfg["ai_instructions"]:
        cfg["ai_instructions"]["repos"] = {}
    
    existing = cfg["ai_instructions"]["repos"].get(key, "")
    if existing:
        cfg["ai_instructions"]["repos"][key] = f"{existing}\n{instruction}"
    else:
        cfg["ai_instructions"]["repos"][key] = instruction
    save_config(cfg)
    typer.echo(f"✅ Set instructions for repo: {key}")
    typer.echo(f"📝 Instruction: {instruction}")


@instruct_app.command("task")
def instruct_task(
    key: str = typer.Argument(..., help="Task key (e.g., PROJ-123)"),
    instruction: str = typer.Argument(None, help="Instruction text to pass to LLM for this task"),
    clear: bool = typer.Option(False, "--clear", help="Clear task-level instructions"),
    edit: bool = typer.Option(False, "--edit", help="Edit instructions in default editor"),
):
    """Set instructions passed to LLM for a specific task"""
    cfg = load_config()
    
    if clear:
        if "ai_instructions" in cfg and "tasks" in cfg["ai_instructions"]:
            if key in cfg["ai_instructions"]["tasks"]:
                del cfg["ai_instructions"]["tasks"][key]
                save_config(cfg)
                typer.echo(f"✅ Cleared instructions for task: {key}")
            else:
                typer.echo(f"ℹ️  No instructions found for task: {key}")
        else:
            typer.echo(f"ℹ️  No instructions found for task: {key}")
        return
    
    if edit:
        current = cfg.get("ai_instructions", {}).get("tasks", {}).get(key, "")
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(current)
            temp_path = f.name
        subprocess.run([os.environ.get('EDITOR', 'vi'), temp_path])
        with open(temp_path, 'r') as f:
            new_content = f.read().strip()
        os.unlink(temp_path)
        if not new_content:
            typer.echo("ℹ️  Empty instructions, clearing...")
            if "ai_instructions" in cfg and "tasks" in cfg["ai_instructions"]:
                cfg["ai_instructions"]["tasks"].pop(key, None)
        else:
            if "ai_instructions" not in cfg:
                cfg["ai_instructions"] = {}
            if "tasks" not in cfg["ai_instructions"]:
                cfg["ai_instructions"]["tasks"] = {}
            cfg["ai_instructions"]["tasks"][key] = new_content
        save_config(cfg)
        typer.echo(f"✅ Updated instructions for task: {key}")
        return
    
    if instruction is None:
        typer.echo("❌ Please provide an instruction, --edit, or --clear")
        return
    
    if "ai_instructions" not in cfg:
        cfg["ai_instructions"] = {}
    if "tasks" not in cfg["ai_instructions"]:
        cfg["ai_instructions"]["tasks"] = {}
    
    existing = cfg["ai_instructions"]["tasks"].get(key, "")
    if existing:
        cfg["ai_instructions"]["tasks"][key] = f"{existing}\n{instruction}"
    else:
        cfg["ai_instructions"]["tasks"][key] = instruction
    save_config(cfg)
    typer.echo(f"✅ Set instructions for task: {key}")
    typer.echo(f"📝 Instruction: {instruction}")


@instruct_app.command("list")
def instruct_list():
    """List all configured LLM instructions"""
    cfg = load_config()
    ai_instructions = cfg.get("ai_instructions", {})
    from workflow.projects import get_current_project
    current_project = get_current_project()
    
    if not ai_instructions:
        typer.echo("No LLM instructions configured.")
        typer.echo("\nUsage:")
        typer.echo("  wf instruct global \"Your instructions here\"  # Cross-project instructions")
        typer.echo("  wf instruct project \"instructions\"          # Current project-specific")
        typer.echo("  wf instruct repo /path/to/repo \"instructions\" # Repo-specific")
        typer.echo("  wf instruct task PROJ-123 \"instructions\"      # Task-specific")
        return
    
    typer.echo("📋 Configured LLM Instructions:")
    typer.echo("")
    
    if "global" in ai_instructions:
        typer.echo("🌍 Global (cross-project):")
        typer.echo(f"   {ai_instructions['global']}")
        typer.echo("")
    
    if "projects" in ai_instructions and ai_instructions["projects"]:
        typer.echo("📦 Project-level:")
        for project_key, instruction in ai_instructions["projects"].items():
            marker = " (current)" if project_key == current_project else ""
            typer.echo(f"   {project_key}{marker}:")
            typer.echo(f"      {instruction}")
        typer.echo("")
    
    if "repos" in ai_instructions and ai_instructions["repos"]:
        typer.echo("📁 Repository-level:")
        for repo_path, instruction in ai_instructions["repos"].items():
            repo_name = repo_path.split("/")[-1]
            typer.echo(f"   {repo_name} ({repo_path}):")
            typer.echo(f"      {instruction}")
        typer.echo("")
    
    if "tasks" in ai_instructions and ai_instructions["tasks"]:
        typer.echo("🔑 Task-level:")
        for task_key, instruction in ai_instructions["tasks"].items():
            typer.echo(f"   {task_key}:")
            typer.echo(f"      {instruction}")


skill_app = typer.Typer(help="Manage skills for the LLM", no_args_is_help=True)
app.add_typer(skill_app, name="skill")


@skill_app.command("global")
def skill_global(
    skill_name: str = typer.Argument(None, help="Skill name"),
    skill_content: str = typer.Argument(None, help="Skill content/prompt"),
    clear: bool = typer.Option(False, "--clear", help="Clear all global skills"),
    list_skills: bool = typer.Option(False, "--list", help="List global skills"),
):
    """Manage global skills passed to LLM for all sessions across all projects"""
    cfg = load_config()
    
    if clear:
        if "ai_skills" in cfg and "global" in cfg["ai_skills"]:
            del cfg["ai_skills"]["global"]
            save_config(cfg)
            typer.echo("✅ Cleared all global skills")
        else:
            typer.echo("ℹ️  No global skills to clear")
        return
    
    if list_skills:
        if "ai_skills" in cfg and "global" in cfg["ai_skills"]:
            typer.echo("🌍 Global skills:")
            for name, content in cfg["ai_skills"]["global"].items():
                typer.echo(f"  • {name}")
                typer.echo(f"    {content[:100]}{'...' if len(content) > 100 else ''}")
        else:
            typer.echo("ℹ️  No global skills configured")
        return
    
    if skill_name is None or skill_content is None:
        typer.echo("❌ Please provide skill name and content, or use --list or --clear")
        return
    
    if "ai_skills" not in cfg:
        cfg["ai_skills"] = {}
    if "global" not in cfg["ai_skills"]:
        cfg["ai_skills"]["global"] = {}
    
    cfg["ai_skills"]["global"][skill_name] = skill_content
    save_config(cfg)
    typer.echo(f"✅ Set global skill: {skill_name}")


@skill_app.command("project")
def skill_project(
    project_name: str = typer.Argument(None, help="Project name (uses current project if not provided)"),
    skill_name: str = typer.Argument(None, help="Skill name"),
    skill_content: str = typer.Argument(None, help="Skill content/prompt"),
    clear: bool = typer.Option(False, "--clear", help="Clear project-level skills"),
    list_skills: bool = typer.Option(False, "--list", help="List project skills"),
):
    """Manage skills passed to LLM for a project (defaults to current project)"""
    from workflow.projects import get_current_project, list_projects
    
    cfg = load_config()
    
    project_key = project_name
    
    if not project_key:
        project_key = get_current_project()
        if not project_key:
            projects = list_projects()
            if projects:
                typer.echo("❌ No current project set. Available projects:")
                for p in projects:
                    typer.echo(f"  - {p}")
                typer.echo("\n💡 Use 'wf project change <name>' or specify project directly:")
                typer.echo("   wf skill project <project-name> <skill-name> <skill-content>")
                return
            else:
                typer.echo("❌ No projects configured.")
                typer.echo("💡 Use 'wf project add' to add a project.")
                return
    
    if clear:
        if "ai_skills" in cfg and "projects" in cfg["ai_skills"] and project_key in cfg["ai_skills"]["projects"]:
            del cfg["ai_skills"]["projects"][project_key]
            save_config(cfg)
            typer.echo(f"✅ Cleared skills for project: {project_key}")
        else:
            typer.echo(f"ℹ️  No skills found for project: {project_key}")
        return
    
    if list_skills:
        if "ai_skills" in cfg and "projects" in cfg["ai_skills"] and project_key in cfg["ai_skills"]["projects"]:
            typer.echo(f"📦 Project '{project_key}' skills:")
            for name, content in cfg["ai_skills"]["projects"][project_key].items():
                typer.echo(f"  • {name}")
                typer.echo(f"    {content[:100]}{'...' if len(content) > 100 else ''}")
        else:
            typer.echo(f"ℹ️  No skills configured for project: {project_key}")
        return
    
    if skill_name is None or skill_content is None:
        typer.echo("❌ Please provide skill name and content, or use --list or --clear")
        return
    
    if "ai_skills" not in cfg:
        cfg["ai_skills"] = {}
    if "projects" not in cfg["ai_skills"]:
        cfg["ai_skills"]["projects"] = {}
    if project_key not in cfg["ai_skills"]["projects"]:
        cfg["ai_skills"]["projects"][project_key] = {}
    
    cfg["ai_skills"]["projects"][project_key][skill_name] = skill_content
    save_config(cfg)
    typer.echo(f"✅ Set skill '{skill_name}' for project: {project_key}")


@skill_app.command("repo")
def skill_repo(
    key: str = typer.Argument(None, help="Repository path (e.g., /Users/user/code/myrepo)"),
    skill_name: str = typer.Argument(None, help="Skill name"),
    skill_content: str = typer.Argument(None, help="Skill content/prompt"),
    clear: bool = typer.Option(False, "--clear", help="Clear repo-level skills"),
    list_skills: bool = typer.Option(False, "--list", help="List repo skills"),
):
    """Manage skills passed to LLM for a specific repository"""
    cfg = load_config()
    
    if key is None:
        typer.echo("❌ Please provide repository path")
        return
    
    if clear:
        if "ai_skills" in cfg and "repos" in cfg["ai_skills"] and key in cfg["ai_skills"]["repos"]:
            del cfg["ai_skills"]["repos"][key]
            save_config(cfg)
            typer.echo(f"✅ Cleared skills for repo: {key}")
        else:
            typer.echo(f"ℹ️  No skills found for repo: {key}")
        return
    
    if list_skills:
        if "ai_skills" in cfg and "repos" in cfg["ai_skills"] and key in cfg["ai_skills"]["repos"]:
            typer.echo(f"📁 Repo '{key}' skills:")
            for name, content in cfg["ai_skills"]["repos"][key].items():
                typer.echo(f"  • {name}")
                typer.echo(f"    {content[:100]}{'...' if len(content) > 100 else ''}")
        else:
            typer.echo(f"ℹ️  No skills configured for repo: {key}")
        return
    
    if skill_name is None or skill_content is None:
        typer.echo("❌ Please provide skill name and content, or use --list or --clear")
        return
    
    if "ai_skills" not in cfg:
        cfg["ai_skills"] = {}
    if "repos" not in cfg["ai_skills"]:
        cfg["ai_skills"]["repos"] = {}
    if key not in cfg["ai_skills"]["repos"]:
        cfg["ai_skills"]["repos"][key] = {}
    
    cfg["ai_skills"]["repos"][key][skill_name] = skill_content
    save_config(cfg)
    typer.echo(f"✅ Set skill '{skill_name}' for repo: {key}")


@skill_app.command("task")
def skill_task(
    key: str = typer.Argument(None, help="Task key (e.g., PROJ-123)"),
    skill_name: str = typer.Argument(None, help="Skill name"),
    skill_content: str = typer.Argument(None, help="Skill content/prompt"),
    clear: bool = typer.Option(False, "--clear", help="Clear task-level skills"),
    list_skills: bool = typer.Option(False, "--list", help="List task skills"),
):
    """Manage skills passed to LLM for a specific task"""
    cfg = load_config()
    
    if key is None:
        typer.echo("❌ Please provide task key")
        return
    
    if clear:
        if "ai_skills" in cfg and "tasks" in cfg["ai_skills"] and key in cfg["ai_skills"]["tasks"]:
            del cfg["ai_skills"]["tasks"][key]
            save_config(cfg)
            typer.echo(f"✅ Cleared skills for task: {key}")
        else:
            typer.echo(f"ℹ️  No skills found for task: {key}")
        return
    
    if list_skills:
        if "ai_skills" in cfg and "tasks" in cfg["ai_skills"] and key in cfg["ai_skills"]["tasks"]:
            typer.echo(f"🔑 Task '{key}' skills:")
            for name, content in cfg["ai_skills"]["tasks"][key].items():
                typer.echo(f"  • {name}")
                typer.echo(f"    {content[:100]}{'...' if len(content) > 100 else ''}")
        else:
            typer.echo(f"ℹ️  No skills configured for task: {key}")
        return
    
    if skill_name is None or skill_content is None:
        typer.echo("❌ Please provide skill name and content, or use --list or --clear")
        return
    
    if "ai_skills" not in cfg:
        cfg["ai_skills"] = {}
    if "tasks" not in cfg["ai_skills"]:
        cfg["ai_skills"]["tasks"] = {}
    if key not in cfg["ai_skills"]["tasks"]:
        cfg["ai_skills"]["tasks"][key] = {}
    
    cfg["ai_skills"]["tasks"][key][skill_name] = skill_content
    save_config(cfg)
    typer.echo(f"✅ Set skill '{skill_name}' for task: {key}")


@skill_app.command("list")
def skill_list():
    """List all configured LLM skills"""
    cfg = load_config()
    ai_skills = cfg.get("ai_skills", {})
    from workflow.projects import get_current_project
    current_project = get_current_project()
    
    if not ai_skills:
        typer.echo("No LLM skills configured.")
        typer.echo("\nUsage:")
        typer.echo("  wf skill global <name> <content>              # Add global skill")
        typer.echo("  wf skill global --list                        # List global skills")
        typer.echo("  wf skill project <name> <content>             # Add project skill")
        typer.echo("  wf skill repo /path/to/repo <name> <content>  # Add repo skill")
        typer.echo("  wf skill task PROJ-123 <name> <content>       # Add task skill")
        return
    
    typer.echo("📋 Configured LLM Skills:")
    typer.echo("")
    
    if "global" in ai_skills and ai_skills["global"]:
        typer.echo("🌍 Global (cross-project):")
        for name, content in ai_skills["global"].items():
            typer.echo(f"   • {name}")
            typer.echo(f"     {content[:80]}{'...' if len(content) > 80 else ''}")
        typer.echo("")
    
    if "projects" in ai_skills and ai_skills["projects"]:
        typer.echo("📦 Project-level:")
        for project_key, skills in ai_skills["projects"].items():
            marker = " (current)" if project_key == current_project else ""
            typer.echo(f"   {project_key}{marker}:")
            for name, content in skills.items():
                typer.echo(f"     • {name}")
                typer.echo(f"       {content[:80]}{'...' if len(content) > 80 else ''}")
        typer.echo("")
    
    if "repos" in ai_skills and ai_skills["repos"]:
        typer.echo("📁 Repository-level:")
        for repo_path, skills in ai_skills["repos"].items():
            repo_name = repo_path.split("/")[-1]
            typer.echo(f"   {repo_name} ({repo_path}):")
            for name, content in skills.items():
                typer.echo(f"     • {name}")
                typer.echo(f"       {content[:80]}{'...' if len(content) > 80 else ''}")
        typer.echo("")
    
    if "tasks" in ai_skills and ai_skills["tasks"]:
        typer.echo("🔑 Task-level:")
        for task_key, skills in ai_skills["tasks"].items():
            typer.echo(f"   {task_key}:")
            for name, content in skills.items():
                typer.echo(f"     • {name}")
                typer.echo(f"       {content[:80]}{'...' if len(content) > 80 else ''}")


@app.command()
def start(
    task: str = typer.Argument(
        None, 
        autocompletion=autocomplete_active_tasks, 
        help="Task key to start working on"
    ),
    base: str = typer.Option(None, autocompletion=autocomplete_branches, help="Base branch for new branch"),
    takeover: bool = typer.Option(False, help="Take over someone else's task"),
    repo_path: str = typer.Option(None, help="Repository path to work on"),
    # Dynamic custom fields will be handled via kwargs
):
    """
    Start working on a task:
    - Move it to In Progress
    - Create git branch
    - Set commit prefix
    - Apply custom field values if specified via --field-name=value
    """
    cfg = load_effective_config()
    backend = get_backend(cfg)
    
     # Parse custom field values from command line
    import sys
    custom_fields = {}
    configured_fields = cfg.get('custom_fields', {})
    
    # NOTE: Custom field values should only be set via defaults in configuration
    # CLI field values are disabled to avoid confusion with defaults
    # Users should override defaults by editing the field value, not via CLI
    
    # Disabled CLI parsing for now to focus on defaults
    # for arg in sys.argv:
    #     if arg.startswith('--') and '=' in arg:
    #         field_name, field_value = arg[2:].split('=', 1)
    #         # Check if this is a configured custom field (case-insensitive)
    #         for cfg_field_name in configured_fields.keys():
    #             if cfg_field_name.lower() == field_name.lower():
    #                 custom_fields[cfg_field_name] = field_value
    #                 typer.echo(f"🔧 Found custom field: {cfg_field_name} = {field_value}")
    #                 break

    if task:
        # Get existing task or create if it doesn't exist
        try:
            issue = backend.get_or_create(task)
        except KeyError as e:
            typer.echo(f"❌ Configuration error: {e}")
            typer.echo("💡 Please check your Jira configuration with 'wf config show'")
            raise typer.Exit(1)
        except Exception as e:
            typer.echo(f"❌ Error processing task '{task}': {e}")
            raise typer.Exit(1)
    else:
        # Let user select interactively
        issue = backend.select_interactively(takeover)

    # Move task to in progress with custom fields
    if issue:
        backend.move_to_in_progress(issue, custom_fields=custom_fields)
    else:
        typer.echo("❌ No issue selected. Exiting.")
        raise typer.Exit(1)

    # Remember current task locally
    set_current_task(issue)

    # Get repository info if git is enabled
    # Check both global repositories AND project-specific repositories
    from workflow.projects import get_project_repositories, get_default_repo
    global_repos = get_repositories()
    project_repos = get_project_repositories()
    
    # Merge both sources
    repos = {}
    repos.update(global_repos)
    repos.update(project_repos)
    
    if is_git_enabled():
        if not repo_path and repos:
            # First, check if cwd is in one of the repos
            from workflow.projects import get_cwd_repo
            cwd_repo = get_cwd_repo()
            if cwd_repo and cwd_repo in repos:
                repo_path = cwd_repo
                typer.echo(f"📁 Using repository (cwd match): {repo_path}")
            # Second, try to use the default repo if configured
            elif default_repo := get_default_repo():
                if default_repo in repos:
                    repo_path = default_repo
                    typer.echo(f"📁 Using default repository: {repo_path}")
            else:
                # Filter out repos that don't exist on disk
                valid_repos = {k: v for k, v in repos.items() if Path(k).exists()}
                if valid_repos:
                    repo_path = list(valid_repos.keys())[0]
                    typer.echo(f"📁 Using repository: {repo_path}")
                    if len(valid_repos) > 1:
                        typer.echo(f"💡 Tip: Set a default repo with 'wf project set-default-repo <repo-path>'")
                else:
                    typer.echo("No valid repositories found on disk.")
        elif not repo_path:
            typer.echo("No repositories configured. Use 'wf config repo-add' to add one or disable git integration with 'wf config set git_enabled false'.")
            return
        
        # Check if AI is currently active in this repository
        from workflow.pid_manager import is_ai_active, get_ai_session_info, get_active_ai_sessions
        from workflow.git_utils import create_worktree
        
        ai_active = False
        ai_session_info = None
        worktree_path = None
        use_worktree = False
        same_issue_active = False
        any_ai_active = False
        
        if repo_path and Path(repo_path).exists():
            # First check if the same issue is being worked on by another AI (in any repo)
            active_sessions = get_active_ai_sessions()
            for session in active_sessions:
                if session.get('task_key') == issue.key:
                    same_issue_active = True
                    typer.echo(f"⚠️  Another AI is already working on issue {issue.key}")
                    typer.echo(f"   Task: {session.get('task_key')}")
                    typer.echo(f"   Branch: {session.get('branch_name', 'unknown')}")
                    typer.echo(f"   Repo: {session.get('repo_path', 'unknown')}")
                    typer.echo(f"   Started: {session.get('started_at', 'unknown')}")
                    from rich.prompt import Confirm
                    use_worktree = Confirm.ask(
                        f"Another AI is already working on {issue.key}. Create a worktree to work on it simultaneously?",
                        default=True
                    )
                    break
            
            # If same issue not active, check if any AI is active in any repo
            if not same_issue_active:
                any_ai_active = bool(active_sessions)  # Use the sessions we already fetched
                ai_active = is_ai_active(repo_path)
                if ai_active or any_ai_active:
                    if ai_active:
                        ai_session_info = get_ai_session_info(repo_path)
                        typer.echo(f"🤖 AI session detected in this repository")
                        typer.echo(f"   Task: {ai_session_info.get('task_key', 'unknown')}")
                        typer.echo(f"   Branch: {ai_session_info.get('branch_name', 'unknown')}")
                        typer.echo(f"   Started: {ai_session_info.get('started_at', 'unknown')}")
                    else:
                        # AI is active in a different repo
                        typer.echo(f"🤖 AI session detected in another repository")
                    
                    # Ask user if they want to use a worktree
                    from rich.prompt import Confirm
                    use_worktree = Confirm.ask(
                        "An AI agent is currently working. Create a worktree for your new branch?",
                        default=True
                    )
        
        # Create git branch
        repo_config = repos.get(repo_path, {})
        branch_base = base or repo_config.get("base_branch")
        if not branch_base:
            branch_base = get_default_branch(repo_path)
        
        # Check for uncommitted changes on non-default branch (someone is working)
        has_changes = has_uncommitted_changes(repo_path) if repo_path else False
        current_branch = None
        if has_changes and repo_path:
            from workflow.git_utils import get_repo
            repo = get_repo(repo_path)
            current_branch = repo.active_branch.name
            default_branches = ["main", "master", "develop", "dev"]
            is_on_default = any(current_branch.lower() == b.lower() for b in default_branches) or current_branch == branch_base
            
            if not is_on_default and not use_worktree:
                typer.echo(f"⚠️  You have uncommitted changes on branch '{current_branch}'")
                from rich.prompt import Confirm
                use_worktree = Confirm.ask(
                    f"You're currently on '{current_branch}' with uncommitted changes. Create a worktree instead?",
                    default=True
                )
        
        try:
            if use_worktree and (ai_active or same_issue_active or any_ai_active or (has_changes and current_branch and current_branch != branch_base)):
                # Generate branch name for the new task
                branch_name = f"{issue.key.lower()}-{issue.title.replace(' ', '-')}"
                
                # Create worktree for the new branch
                worktree_path = create_worktree(repo_path, branch_name, branch_base)
                typer.echo(f"✅ Created worktree at: {worktree_path}")
                
                # Update repo_path to the worktree for subsequent operations
                working_repo_path = worktree_path
                
                # Set commit prefix in the worktree
                set_commit_prefix(issue.key, working_repo_path)
            else:
                # Normal branch creation in the main repository
                # Skip uncommitted changes check if AI is active or we're using worktree for changes
                create_branch(issue, branch_base, repo_path, skip_uncommitted_check=ai_active or same_issue_active or any_ai_active or use_worktree)
                # Set commit prefix only if branch creation succeeded
                set_commit_prefix(issue.key, repo_path if repo_path else None)
                working_repo_path = repo_path
        except RuntimeError as e:
            if "cancelled" in str(e).lower():
                typer.echo("❌ Branch creation cancelled. Task is still set to in progress.")
                typer.echo(f"💡 You can manually create a branch later with: git checkout -b {issue.key}")
            else:
                typer.echo(f"❌ Failed to create branch: {e}")
                typer.echo("💡 Task is still set to in progress.")
            return
    else:
        if repos and not repo_path:
            typer.echo("Note: Git integration is disabled but repositories are configured.")
        elif not repos:
            typer.echo("No repositories configured. Use 'wf repo-add' to add one.")
        typer.echo("Git integration is disabled. Skipping branch creation and commit prefix setup.")

    typer.echo(f"✅ Now working on task {issue.key}")

    # Change to default repo directory if available and running in a terminal
    if is_git_enabled() and repo_path and sys.stdin.isatty():
        import os
        
        # Use worktree path if one was created, otherwise use repo_path
        target_path = worktree_path if worktree_path else repo_path
        
        if Path(target_path).exists():
            try:
                # Use AppleScript to change directory in Terminal
                expanded_path = str(Path(target_path).expanduser())
                applescript = f'''
                tell application "Terminal"
                    set currentTab to (selected tab of window 1)
                    do script "cd '{expanded_path}'" in currentTab
                end tell
                '''
                
                temp_file = "/tmp/wf_start_cd.applescript"
                with open(temp_file, "w") as f:
                    f.write(applescript)
                
                os.system(f"osascript '{temp_file}' 2>/dev/null &")
                if worktree_path:
                    typer.echo(f"📂 Changed to worktree: {worktree_path}")
                else:
                    typer.echo(f"📂 Changed to: {repo_path}")
            except Exception as e:
                typer.echo(f"⚠️  Could not change to directory {target_path}: {e}")
    elif is_git_enabled() and repo_path:
        if worktree_path:
            typer.echo(f"📂 Working in worktree: {worktree_path}")
        else:
            typer.echo(f"📂 Working in: {repo_path}")

def autocomplete_in_progress_tasks(ctx, args, incomplete: str):
    """
    Autocomplete tasks assigned to user that are in progress.
    """
    cfg = load_config()
    backend = get_backend(cfg)
    tasks = backend.get_assigned_tasks()
    in_progress_tasks = []
    for t in tasks:
        # Check if task is in progress (not done)
        try:
            if not backend.is_done(t) and t.key.startswith(incomplete):
                in_progress_tasks.append(t.key)
        except:
            # If we can't check status, include it anyway
            if t.key.startswith(incomplete):
                in_progress_tasks.append(t.key)
    return in_progress_tasks

@app.command()
def switch(
    task: str = typer.Argument(
        None, 
        autocompletion=autocomplete_active_tasks, 
        help="Task key to switch to"
    ), 
    repo_path: str = typer.Option(None, help="Repository path")
):
    import sys
    cfg = load_config()
    backend = get_backend(cfg)
    if task is None:
        # Check if we're in a terminal environment
        if sys.stdin.isatty():
            issue = backend.select_interactively()
            if not issue:
                return
        else:
            # Non-interactive: show available tasks
            typer.echo("Available tasks:")
            typer.echo("Use 'wf switch TASK-KEY' to switch to a specific task")
            typer.echo("Use 'wf switch -' to switch to previous task")
            tasks = backend.get_assigned_tasks()
            for i, t in enumerate(tasks[:10], 1):  # Show first 10 tasks
                status = getattr(t, 'status', 'Unknown')
                title = getattr(t, 'title', getattr(t, 'summary', 'No title'))
                typer.echo(f"{i:2d}. {t.key} [{status}] {title}")
            return
    elif task == "-":
        issue = get_previous_task()
        if not issue:
            typer.echo("No previous task found")
            return
    else:
        issue = backend.get(task)
    set_current_task(issue)
    
    # Get repositories from all configured sources
    from workflow.projects import get_project_repositories, get_default_repo
    global_repos = get_repositories()
    project_repos = get_project_repositories()
    
    # Merge both sources
    repos = {}
    repos.update(global_repos)
    repos.update(project_repos)
    
    if is_git_enabled():
        set_commit_prefix(issue.key, repo_path if repo_path else None)
        
        # Determine which repo to use
        if not repo_path and repos:
            # First, try to use the default repo if configured
            default_repo = get_default_repo()
            if default_repo and default_repo in repos:
                repo_path = default_repo
            else:
                # Filter out repos that don't exist on disk
                valid_repos = {k: v for k, v in repos.items() if Path(k).exists()}
                if valid_repos:
                    repo_path = list(valid_repos.keys())[0]
        
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
        
        # Open terminal tabs for the selected/default repository only
        if repo_path:
            console.print(f"📁 Opening terminal in {repo_path}...")
            open_terminal_tabs({repo_path: repos.get(repo_path, {})}, issue.key)
        elif repos:
            open_terminal_tabs(repos, issue.key)
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

    # Check both global repositories AND project-specific repositories
    from workflow.projects import get_project_repositories
    global_repos = get_repositories()
    project_repos = get_project_repositories()

    # Merge both sources
    repos = {}
    repos.update(global_repos)
    repos.update(project_repos)

    if is_git_enabled():
        if not repos:
            typer.echo("No repositories configured. Use 'wf repo-add' to add one.")
            pr = None
        else:
            # If no specific repo_path provided, detect current git worktree/repo
            if not repo_path:
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True
                )
                current_repo_path = result.stdout.strip() if result.returncode == 0 else None
                
                if current_repo_path:
                    current_repo_path = current_repo_path.rstrip("/")
                
                # Use current path if it's in repos OR if it exists on disk (e.g., worktree)
                if current_repo_path and (current_repo_path in repos or Path(current_repo_path).exists()):
                    repo_paths = [current_repo_path]
                else:
                    repo_paths = list(repos.keys())
            else:
                repo_paths = [repo_path]
            
            pr = None
            pr_created = False
            
            # Early check for existing PRs across all repositories
            from workflow.git_utils import check_existing_prs_for_issue
            existing_prs = check_existing_prs_for_issue(issue, repo_paths)
            if existing_prs:
                typer.echo(f"🔍 Found existing PRs in {len(existing_prs)} repositories: {list(existing_prs.keys())}")
                pr_created = True
            
            for current_repo_path in repo_paths:
                typer.echo(f"Processing repository: {current_repo_path}")
                
                # Skip if PR already exists for this repository
                if current_repo_path in existing_prs:
                    typer.echo(f"⚠️  PR already exists for {current_repo_path}, skipping...")
                    pr_created = True  # Count existing PR as successful for task status
                    continue
                
                # Get repository info for PR base branch
                base_branch = repos.get(current_repo_path, {}).get("base_branch")
                
                # Use smart default if no base branch configured
                if not base_branch:
                    base_branch = get_default_branch(current_repo_path)
                
                # Check if there are any differences with base branch or for specified repo
                has_diff = has_diff_with_base(current_repo_path, base_branch)
                
                # Check if branch has unpushed commits - use git command directly to get current worktree branch
                from workflow.git_utils import get_repo
                repo = get_repo(current_repo_path)
                
                branch_result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True
                )
                current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
                
                if not current_branch:
                    current_branch = repo.active_branch.name
                
                has_unpushed = False
                commits_ahead = 0
                try:
                    remote_branch = f"origin/{current_branch}"
                    # Check if branch exists on remote first
                    repo.git.fetch("origin", current_branch)
                    commits_ahead = len(list(repo.iter_commits(f"{remote_branch}..{current_branch}")))
                    has_unpushed = commits_ahead > 0
                except Exception:
                    # Branch doesn't exist on remote - check for local commits not yet pushed
                    try:
                        base_remote = f"origin/{base_branch}"
                        commits_ahead = len(list(repo.iter_commits(f"{base_remote}..{current_branch}")))
                        has_unpushed = commits_ahead > 0
                    except:
                        has_unpushed = False
                
                if has_diff:
                    try:
                        pr = create_pr(issue, reviewers, current_repo_path, base_branch)
                        if pr and pr.get("url") and not pr.get("url").startswith("existing"):
                            typer.echo(f"✅ Created PR for {current_repo_path}")
                            pr_created = True
                        elif pr and pr.get("url") == "existing":
                            typer.echo(f"⚠️  PR already exists for {current_repo_path}")
                            pr_created = True
                    except Exception as e:
                        typer.echo(f"❌ Failed to create PR for {current_repo_path}: {e}")
                elif has_unpushed:
                    typer.echo(f"Branch '{current_branch}' has {commits_ahead} unpushed commit(s).")
                    push = typer.confirm(f"Push commits and create PR for {current_repo_path}?", default=True)
                    if push:
                        try:
                            from workflow.git_utils import push_branch
                            push_branch(current_repo_path)
                            typer.echo(f"✅ Pushed branch '{current_branch}'")
                            pr = create_pr(issue, reviewers, current_repo_path, base_branch)
                            if pr and pr.get("url") and not pr.get("url").startswith("existing"):
                                typer.echo(f"✅ Created PR for {current_repo_path}")
                                pr_created = True
                            elif pr and pr.get("url") == "existing":
                                typer.echo(f"⚠️  PR already exists for {current_repo_path}")
                                pr_created = True
                        except Exception as e:
                            typer.echo(f"❌ Failed to push or create PR: {e}")
                    else:
                        typer.echo("Skipping PR creation.")
                else:
                    typer.echo(f"No differences with base branch '{base_branch}' in {current_repo_path}. Skipping PR creation.")
                    
                    if not has_unpushed:
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
        typer.echo(f"📋 Task {issue.key} moved to Code Review")
    else:
        backend.move_to_done(issue)
        typer.echo(f"✅ Task {issue.key} marked as Done")
    
    # Post PR to Slack if created
    if pr and pr.get("url") and not pr.get("url").startswith("existing"):
        post_pr(cfg, issue, pr)
    
    # Post completion to Slack if configured
    if cfg.get("slack_webhook"):
        completion_pr = pr if (pr and pr.get("url")) else None
        if post_task_complete(cfg, issue, completion_pr):
            typer.echo("📢 Posted task completion to Slack")
    else:
        # Ask user if they want to configure Slack
        configure_slack = typer.confirm("📢 Slack is not configured. Would you like to configure it now to post task completion?", default=False)
        if configure_slack:
            webhook_url = typer.prompt("Enter your Slack webhook URL", hide_input=True)
            if webhook_url.strip():
                cfg["slack_webhook"] = webhook_url.strip()
                save_config(cfg)
                typer.echo("✅ Slack webhook configured successfully!")
                
                # Post the completion notification now that it's configured
                completion_pr = pr if (pr and pr.get("url")) else None
                if post_task_complete(cfg, issue, completion_pr):
                    typer.echo("📢 Posted task completion to Slack")
            else:
                typer.echo("❌ Invalid webhook URL. Slack notification skipped.")

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
    
    from workflow.state import has_shelved_changes, get_shelved_branch
    
    if not has_shelved_changes(repo_path):
        typer.echo("No shelved changes found.")
        return
    
    shelved_branch = get_shelved_branch(repo_path)
    typer.echo(f"Restoring changes shelved from branch '{shelved_branch}'...")
    
    if unshelve_changes(repo_path):
        typer.echo("Changes restored successfully.")
    else:
        typer.echo("Failed to restore changes.")



@app.command()
def ai(
    prompt: str = typer.Option(None, "--prompt", help="Custom prompt to use instead of current task context")
):
    # Use configurable AI provider
    print("DEBUG: Starting ai command", flush=True)
    from workflow.ai import get_ai_provider
    from workflow.memory import save_session, save_summary, save_meta
    from workflow.pid_manager import create_ai_pid_file, remove_ai_pid_file
    from workflow.projects import get_default_repo
    from workflow.ai_context import build_context
    from pathlib import Path
    import os
    
    print("DEBUG: Imports done", flush=True)
    
    # Create PID file to track this AI session
    pid_file = None
    repo_path = None
    issue = None
    
    # First check if we're in a worktree (use current directory)
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=os.getcwd()
        )
        if result.returncode == 0:
            repo_path = result.stdout.strip()
            print(f"DEBUG: Using current worktree: {repo_path}", flush=True)
    except:
        pass
    
    # If not in a worktree, try to get the default repo
    if not repo_path:
        default_repo = get_default_repo()
        print(f"DEBUG: default_repo: {default_repo}", flush=True)
        if default_repo:
            repo_path = default_repo
    
    if prompt:
        # Use custom prompt
        context = prompt
        print("🤖 Starting AI session with custom prompt...")
    else:
        # Use current task
        print("DEBUG: Getting current task...", flush=True)
        issue = get_current_task()
        print(f"DEBUG: Got issue: {issue}", flush=True)
        if not issue:
            typer.echo("No current task set. Use 'wf start' to begin a task or use --prompt.")
            return
        context = build_context(issue, repo_path=repo_path)
        print("🤖 Starting AI session...")
    
    print("DEBUG: Creating PID file...", flush=True)
    if issue:
        try:
            if repo_path:
                # Get current branch name
                try:
                    import subprocess
                    result = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True,
                        text=True,
                        cwd=repo_path
                    )
                    branch_name = result.stdout.strip() if result.returncode == 0 else "unknown"
                except:
                    branch_name = "unknown"
                
                pid_file = create_ai_pid_file(repo_path, issue.key, branch_name)
                print(f"📝 AI session tracked (PID file created)")
        except Exception as e:
            print(f"⚠️  Could not create PID file: {e}")
    
    print("DEBUG: Getting provider...", flush=True)
    provider = get_ai_provider()
    print(f"DEBUG: Got provider: {provider.name}", flush=True)
    print(f"Using AI provider: {provider.name}")
    
    try:
        print("DEBUG: Running provider.run...", flush=True)
        transcript = provider.run(context)
        print(f"DEBUG: provider.run returned, transcript length: {len(transcript) if transcript else 0}", flush=True)
        print(f"🔄 AI session completed, transcript length: {len(transcript) if transcript else 0}")
        
        # Try to save only if there's a task issue
        if issue:
            try:
                print("DEBUG: Saving session...", flush=True)
                save_session(issue, transcript)
                print("✅ Session saved")
                save_meta(issue, provider.name)
                print("✅ Meta saved")
                print("🤖 Summary will be generated in background...")
            except Exception as e:
                print(f"❌ Save failed: {e}")
                import traceback
                traceback.print_exc()
    finally:
        # Clean up PID file
        if repo_path:
            try:
                remove_ai_pid_file(repo_path)
            except:
                pass


@app.command()
def ai_mcp_config():
    """Show MCP configuration for AI clients (Claude Desktop, Cursor, etc.)"""
    import json
    from pathlib import Path
    
    wf_path = Path(__file__).parent.parent / "mcp.py"
    server_path = str(wf_path.resolve())
    
    mcp_config = {
        "mcpServers": {
            "wf-tools": {
                "command": "python",
                "args": [server_path]
            }
        }
    }
    
    typer.echo("📋 MCP Configuration for wf-tools")
    typer.echo("=" * 50)
    typer.echo("")
    typer.echo("Add the following to your Claude Desktop config:")
    typer.echo(f"(~/.config/Claude/claude_desktop_config.json on Linux)")
    typer.echo(f"(~/Library/Application Support/Claude/claude_desktop_config.json on macOS)")
    typer.echo("")
    typer.echo(json.dumps(mcp_config, indent=2))
    typer.echo("")
    typer.echo("Available MCP Tools:")
    typer.echo("-" * 30)
    
    tools = [
        ("get_current_task_info", "Get the current active task"),
        ("list_configured_projects", "List all configured projects"),
        ("get_current_project", "Get the current active project"),
        ("list_git_branches", "List available git branches"),
        ("get_current_git_branch", "Get the current git branch"),
        ("list_assigned_tasks", "List Jira tasks assigned to you"),
        ("list_hooks", "List all configured hooks"),
        ("add_hook", "Add a new hook"),
        ("remove_hook", "Remove a hook"),
        ("execute_hook", "Execute a specific hook"),
        ("list_actions", "List all registered actions"),
        ("get_action_types", "Get available action types"),
        ("test_notification", "Send a test notification"),
        ("get_wf_commands", "Get list of available wf commands"),
        ("get_config_value", "Get a configuration value"),
        ("set_config_value", "Set a configuration value"),
    ]
    
    for tool_name, tool_desc in tools:
        typer.echo(f"  • {tool_name}: {tool_desc}")
    
    typer.echo("")
    typer.echo("💡 After adding the config, restart Claude Desktop.")
    typer.echo("💡 The wf-tools will appear in Claude's available tools.")


@app.command()
def ai_mcp_status():
    """Check if MCP tools are available"""
    import subprocess
    try:
        result = subprocess.run(
            ["python", "-c", "from workflow.mcp import mcp; print('OK')"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            typer.echo("✅ MCP server module is installed and working")
            
            from workflow.mcp import mcp
            typer.echo(f"   Server name: {mcp.name}")
        else:
            typer.echo("❌ MCP server module has issues")
            typer.echo(f"   Error: {result.stderr}")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")

@app.command()
def summarize(
    task: str = typer.Argument(None, help="Task key to summarize (defaults to current task)"),
    transcript: str = typer.Option(None, "--transcript", help="Path to transcript file"),
):
    """Generate summary from AI session transcript"""
    import subprocess
    from pathlib import Path
    from workflow.state import get_current_task
    from workflow.memory import save_summary, _dir
    
    # Determine task key
    task_key = None
    if task:
        task_key = task.upper()
    else:
        issue = get_current_task()
        if issue:
            task_key = issue.key
    
    if not task_key:
        typer.echo("❌ No task specified and no current task set")
        return
    
    # Create a simple object to pass to save_summary
    class SimpleIssue:
        def __init__(self, key):
            self.key = key
    
    issue = SimpleIssue(task_key)
    
    # Determine task directory
    task_dir = Path.home() / ".wf" / "tasks" / task_key.lower()
    
    # Find transcript file
    transcript_file = None
    if transcript:
        transcript_file = Path(transcript)
    else:
        # Find latest transcript
        transcripts = list(task_dir.glob("transcript_*.txt"))
        if transcripts:
            transcripts.sort(reverse=True)
            transcript_file = transcripts[0]
    
    if not transcript_file or not transcript_file.exists():
        typer.echo(f"❌ No transcript file found for {task_key}")
        return
    
    typer.echo(f"📄 Using transcript: {transcript_file}")
    
    # Read transcript
    transcript_content = transcript_file.read_text()
    
    # Generate summary
    typer.echo("🤖 Generating summary...")
    summary_prompt = f"Summarize durable decisions from this session:\n\n{transcript_content[:50000]}"
    result = subprocess.run(
        ["claude", "--print", summary_prompt],
        capture_output=True,
        text=True,
        timeout=120
    )
    
    if result.returncode == 0 and result.stdout:
        save_summary(issue, result.stdout)
        typer.echo(f"✅ Summary saved to memory for {issue.key}")
        typer.echo(f"\n--- Summary ---\n{result.stdout}")
    else:
        typer.echo(f"❌ Failed to generate summary: {result.stderr}")

@app.command()
def regenerate_context():
    """Regenerate project context file"""
    from workflow.rag import get_rag
    rag = get_rag()
    rag.rebuild_index()

@app.command()
def rag_info():
    """Show RAG information and available categories"""
    from workflow.rag import get_rag
    rag = get_rag()
    
@app.command()
def search_context(query: str):
    """Search project context using RAG"""
    from workflow.rag import get_rag
    rag = get_rag()
    
    print(f"🔍 Searching for: {query}")
    results = rag.retrieve_context(query, max_chunks=10)
    
    if not results:
        print("No relevant context found.")
        return
    
    print(f"📋 Found {len(results)} relevant chunks:")
    for i, chunk in enumerate(results, 1):
        print(f"\n{i}. {chunk['title']} (Relevance: {chunk.get('similarity_score', 0):.2f})")
        print(f"   Category: {chunk['category']}")
        print(f"   Content: {chunk['content'][:200]}{'...' if len(chunk['content']) > 200 else ''}")
        
@app.command()
def list_categories():
    """List all available context categories"""
    from workflow.rag import get_rag
    rag = get_rag()
    
    categories = rag.list_categories()
    print("📂 Available Context Categories:")
    
    for category in categories:
        chunks = rag.get_chunks_by_category(category)
        print(f"\n{category.upper()}:")
        print(f"  ({len(chunks)} chunks)")
        for chunk in chunks[:5]:  # Show first 5 chunks
            print(f"    - {chunk['title']}")
        if len(chunks) > 5:
            print(f"    ... and {len(chunks) - 5} more")

@app.command("get-tasks", hidden=True)
def get_tasks():
    """Internal command to get tasks for completion caching"""
    cfg = load_config()
    backend = get_backend(cfg)
    tasks = backend.get_assigned_tasks()
    for task in tasks:
        # Clean up title and ensure proper format
        title = task.title.strip()
        typer.echo(f"{task.key}:{title}")


# Create Slack subcommand group
slack_app = typer.Typer(help="Slack integration commands", no_args_is_help=True)

@slack_app.command()
def message(
    text: str = typer.Argument(..., help="Message to post to Slack"),
    channel: str = typer.Option(None, help="Slack channel to post to")
):
    """Post a custom message to Slack"""
    cfg = load_config()
    if not cfg.get("slack_webhook"):
        typer.echo("❌ Slack webhook not configured. Run 'wf init' to configure.")
        return
    
    if post_message(cfg, text, channel):
        typer.echo("✅ Message posted to Slack")
    else:
        typer.echo("❌ Failed to post message to Slack")

@slack_app.command()
def notify():
    """Post current task status to Slack"""
    cfg = load_config()
    issue = get_current_task()
    
    if not cfg.get("slack_webhook"):
        typer.echo("❌ Slack webhook not configured. Run 'wf init' to configure.")
        return
    
    if not issue:
        typer.echo("❌ No current task set. Use 'wf start' to begin a task.")
        return
    
    message = f"📋 Currently working on {issue.key}: {issue.title}"
    if post_message(cfg, message):
        typer.echo("✅ Status posted to Slack")
    else:
        typer.echo("❌ Failed to post status to Slack")

@slack_app.command("config")
def slack_config_cmd():
    """Show current Slack configuration"""
    cfg = load_config()
    slack_cfg = get_slack_config()
    
    typer.echo("📢 Slack Configuration:")
    typer.echo(f"  Webhook: {'✅ Configured' if cfg.get('slack_webhook') else '❌ Not configured'}")
    typer.echo(f"  Default Channel: {slack_cfg.get('default_channel', 'Not set')}")
    
    templates = slack_cfg.get('message_templates', {})
    typer.echo("\n📝 Message Templates:")
    for msg_type, template in templates.items():
        typer.echo(f"  {msg_type}: {template}")
    
    user_mapping = slack_cfg.get('user_mapping', {})
    typer.echo(f"\n👥 User Mappings ({len(user_mapping)}):")
    for github_email, slack_user in list(user_mapping.items())[:5]:  # Show first 5
        typer.echo(f"  {github_email} -> {slack_user}")
    if len(user_mapping) > 5:
        typer.echo(f"  ... and {len(user_mapping) - 5} more")

@slack_app.command("set-channel")
def set_channel_cmd(
    channel: str = typer.Argument(..., help="Slack channel (e.g., '#general', 'C1234567890')")
):
    """Set default Slack channel"""
    set_slack_channel(channel)
    typer.echo(f"✅ Default Slack channel set to: {channel}")

@slack_app.command("set-template")
def set_template_cmd(
    message_type: str = typer.Argument(..., help="Message type (task_start, task_complete, pr_ready)"),
    template: str = typer.Argument(..., help="Message template. Variables: {task_key}, {task_title}, {pr_url}, {reviewers_section}")
):
    """Set Slack message template"""
    valid_types = ["task_start", "task_complete", "pr_ready"]
    if message_type not in valid_types:
        typer.echo(f"❌ Invalid message type. Must be one of: {', '.join(valid_types)}")
        return
    
    set_slack_message_template(message_type, template)
    typer.echo(f"✅ Template for '{message_type}' set to: {template}")
    
    # Show available variables
    typer.echo("\n📝 Available variables:")
    typer.echo("  {task_key} - Task identifier (e.g., TASK-123)")
    typer.echo("  {task_title} - Task title/summary")
    typer.echo("  {pr_url} - Pull Request URL")
    typer.echo("  {pr_section} - PR section with link")
    typer.echo("  {reviewers_section} - Tagged reviewers section")

@slack_app.command("add-user")
def add_user_cmd(
    github_email: str = typer.Argument(..., help="GitHub user email"),
    slack_username: str = typer.Argument(..., help="Slack username (without @)")
):
    """Add GitHub email to Slack username mapping"""
    add_slack_user_mapping(github_email, slack_username)
    typer.echo(f"✅ Added mapping: {github_email} -> @{slack_username}")

@slack_app.command("dump-init")
def slack_dump_init(
    token: str = typer.Option(None, "--token", help="Slack API token (xoxp-... or xoxb-...)")
):
    """Configure Slack API token for dump functionality"""
    if not token:
        typer.echo("❌ Token required. Get one at: https://api.slack.com/authentication/token-types")
        typer.echo("   User token (xoxp-): Full access to read messages")
        typer.echo("   Bot token (xoxb-): Limited to public channels")
        return

    if dump_to_keychain(token):
        set_slack_token(token)
        typer.echo("✅ Slack API token saved securely to Keychain")
    else:
        set_slack_token(token)
        typer.echo("✅ Slack API token saved to config (not Keychain)")

@slack_app.command("channels")
def slack_channels():
    """List all accessible Slack channels"""
    cookies = get_browser_cookies()
    if not cookies:
        cookies = get_slack_cookies()
    
    token = get_token_from_keychain() or get_slack_token()
    
    if not cookies and not token:
        typer.echo("❌ Not connected to Slack browser")
        typer.echo("   Make sure browser daemon is running and Slack is open:")
        typer.echo("   wf browser open https://app.slack.com")
        return

    try:
        dumper = SlackDumper(cookies=cookies, token=token)
        channels = dumper.get_channels()
        
        if not channels:
            typer.echo("No channels found.")
            return

        table = Table(title="Slack Channels")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Type", style="magenta")

        for ch in channels:
            ch_type = "Private" if ch.get("is_private") else "Public"
            table.add_row(ch.get("id", ""), f"#{ch.get('name', '')}", ch_type)

        console.print(table)
        typer.echo(f"\nTotal: {len(channels)} channels")

    except Exception as e:
        typer.echo(f"❌ Error: {e}")

@slack_app.command("dump")
def slack_dump(
    channel: str = typer.Argument(..., help="Channel name (without #) or channel ID"),
    output: str = typer.Option(".", "--output", "-o", help="Output directory"),
    include_threads: bool = typer.Option(True, "--include-threads/--no-threads", help="Include thread replies"),
    include_files: bool = typer.Option(True, "--include-files/--no-files", help="Include file downloads"),
    days: int = typer.Option(30, "--days", "-d", help="Export messages from the last N days")
):
    """Dump messages from a Slack channel"""
    cookies = get_browser_cookies()
    if not cookies:
        cookies = get_slack_cookies()
    
    token = get_token_from_keychain() or get_slack_token()
    
    if not cookies and not token:
        typer.echo("❌ Not connected to Slack browser")
        typer.echo("   Make sure browser daemon is running and Slack is open:")
        typer.echo("   wf browser open https://app.slack.com")
        return

    try:
        dumper = SlackDumper(cookies=cookies, token=token)
        
        channel_id = channel
        if not channel.startswith("C") and not channel.startswith("G"):
            channels = dumper.get_channels()
            matching = [c for c in channels if c.get("name") == channel]
            if not matching:
                typer.echo(f"❌ Channel '{channel}' not found")
                return
            channel_id = matching[0]["id"]

        output_dir = Path(output).expanduser()
        oldest = str((datetime.now() - timedelta(days=days)).timestamp())

        def progress(msg):
            typer.echo(f"  {msg}")

        typer.echo(f"📥 Dumping #{channel} (last {days} days)...")
        
        result = dumper.dump_channel(
            channel_id,
            output_dir,
            include_threads=include_threads,
            include_files=include_files,
            oldest=oldest,
            progress_callback=progress
        )

        typer.echo(f"\n✅ Export complete!")
        typer.echo(f"   Messages: {len(result.get('messages', []))}")
        typer.echo(f"   Threads: {len(result.get('threads', {}))}")
        typer.echo(f"   Files: {len(result.get('files', []))}")

    except Exception as e:
        typer.echo(f"❌ Error: {e}")

@slack_app.command("dump-all")
def slack_dump_all(
    output: str = typer.Option("./slack-export", "--output", "-o", help="Output directory"),
    channels: str = typer.Option(None, "--channels", help="Comma-separated channel names (default: all)"),
    include_threads: bool = typer.Option(True, "--include-threads/--no-threads", help="Include thread replies"),
    include_files: bool = typer.Option(True, "--include-files/--no-files", help="Include file downloads"),
    days: int = typer.Option(30, "--days", "-d", help="Export messages from the last N days")
):
    """Dump messages from all accessible Slack channels"""
    cookies = get_browser_cookies()
    if not cookies:
        cookies = get_slack_cookies()
    
    token = get_token_from_keychain() or get_slack_token()
    
    if not cookies and not token:
        typer.echo("❌ Not connected to Slack browser")
        typer.echo("   Make sure browser daemon is running and Slack is open:")
        typer.echo("   wf browser open https://app.slack.com")
        return

    try:
        dumper = SlackDumper(cookies=cookies, token=token)
        output_dir = Path(output).expanduser()
        oldest = str((datetime.now() - timedelta(days=days)).timestamp())

        channel_filter = [c.strip() for c in channels.split(",")] if channels else None

        def progress(msg):
            typer.echo(msg)

        typer.echo(f"📥 Dumping all channels (last {days} days)...")
        
        results = dumper.dump_all_channels(
            output_dir,
            channel_filter=channel_filter,
            include_threads=include_threads,
            include_files=include_files,
            oldest=oldest,
            progress_callback=progress
        )

        total_messages = sum(len(r.get('messages', [])) for r in results)
        total_threads = sum(len(r.get('threads', {})) for r in results)
        total_files = sum(len(r.get('files', [])) for r in results)

        typer.echo(f"\n✅ Export complete!")
        typer.echo(f"   Channels: {len(results)}")
        typer.echo(f"   Messages: {total_messages}")
        typer.echo(f"   Threads: {total_threads}")
        typer.echo(f"   Files: {total_files}")
        typer.echo(f"   Output: {output_dir}")

    except Exception as e:
        typer.echo(f"❌ Error: {e}")

@slack_app.command("teleop-scan")
def teleop_scan_cmd(
    channel: str = typer.Option("teleop", "--channel", help="Channel name (without #) or channel ID"),
    days: int = typer.Option(30, "--days", "-d", help="Look back N days"),
    permalinks: bool = typer.Option(True, "--permalinks/--no-permalinks", help="Fetch permalinks for each match"),
):
    """Scan a channel for 'NEW ISSUE' posts from the Tele-op Issue Slack workflow"""
    cookies = get_browser_cookies()
    if not cookies:
        cookies = get_slack_cookies()

    token = get_token_from_keychain() or get_slack_token()

    if not cookies and not token:
        typer.echo("❌ Not connected to Slack")
        typer.echo("   wf slack dump-init --token <xoxp-or-xoxb-token>")
        typer.echo("   or: wf browser open https://app.slack.com")
        return

    try:
        dumper = SlackDumper(cookies=cookies, token=token)
        channel_id = resolve_channel_id(dumper, channel)
        oldest = str((datetime.now() - timedelta(days=days)).timestamp())

        typer.echo(f"🔎 Scanning #{channel.lstrip('#')} for Tele-op Issue 'NEW ISSUE' posts...")
        found = find_new_issue_messages(dumper, channel_id, oldest=oldest, with_permalinks=permalinks)

        if not found:
            typer.echo("No matching messages found.")
            return

        table = Table(title="Tele-op NEW ISSUE reports")
        table.add_column("ts", style="cyan")
        table.add_column("Title", style="green")
        table.add_column("Linear?", style="yellow")
        table.add_column("Link", style="magenta")

        for item in found:
            parsed = parse_new_issue(item.text)
            converted = "✅ already converted" if item.already_converted else "—"
            table.add_row(item.ts, parsed["title"], converted, item.permalink or "")

        console.print(table)
        pending = [i for i in found if not i.already_converted]
        typer.echo(f"\nTotal: {len(found)} matching message(s), {len(pending)} not yet converted")
        typer.echo("Convert one with: wf slack teleop-convert <ts> --team <TEAM> --project <PROJECT>")

    except Exception as e:
        typer.echo(f"❌ Error: {e}")


@slack_app.command("teleop-convert")
def teleop_convert_cmd(
    ts: str = typer.Argument(..., help="Timestamp of the NEW ISSUE message (from teleop-scan)"),
    team: str = typer.Option(..., "--team", help="Linear team key/name, e.g. PAN"),
    project: str = typer.Option(..., "--project", help="Linear project name, e.g. Teleop"),
    channel: str = typer.Option("teleop", "--channel", help="Channel name (without #) or channel ID"),
    labels: str = typer.Option("bug,teleop", "--labels", help="Comma-separated Linear labels"),
    priority: str = typer.Option("High", "--priority", help="Linear priority, e.g. High"),
    status: str = typer.Option("Triage", "--status", help="Linear workflow status, e.g. Triage"),
    title: str = typer.Option(None, "--title", help="Override the parsed title"),
    description: str = typer.Option(None, "--description", help="Override description; one bullet per line"),
    mention: str = typer.Option(None, "--mention", help="Slack mention text for the Linear app (default: auto-detect, falls back to @Linear)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the reply instead of posting it"),
    force: bool = typer.Option(False, "--force", help="Post even if the thread already looks converted"),
):
    """Reply in-thread on a Tele-op 'NEW ISSUE' message, @-mentioning the Linear
    Slack app so it files the issue (no Linear API key needed)."""
    cookies = get_browser_cookies()
    if not cookies:
        cookies = get_slack_cookies()

    token = get_token_from_keychain() or get_slack_token()

    if not cookies and not token:
        typer.echo("❌ Not connected to Slack")
        typer.echo("   wf slack dump-init --token <xoxp-or-xoxb-token>")
        typer.echo("   or: wf browser open https://app.slack.com")
        return

    try:
        dumper = SlackDumper(cookies=cookies, token=token)
        channel_id = resolve_channel_id(dumper, channel)

        msg = dumper.get_message(channel_id, ts)
        if not msg:
            typer.echo(f"❌ No message found at ts={ts} in #{channel.lstrip('#')}")
            return

        if not force and thread_has_linear_conversion(dumper, channel_id, ts):
            typer.echo(f"⚠️  Thread {ts} already looks converted (a Linear reply/link was found). Skipping.")
            typer.echo("   Use --force to post anyway.")
            return

        parsed = parse_new_issue(msg.get("text", ""))
        final_title = title or parsed["title"]
        final_description_lines = description.splitlines() if description else parsed["description_lines"]
        label_list = [l.strip() for l in labels.split(",") if l.strip()]

        permalink = dumper.get_permalink(channel_id, ts)
        if permalink and not any("Source:" in line for line in final_description_lines):
            final_description_lines = final_description_lines + [f"Source: {permalink}"]

        linear_mention = mention
        if not linear_mention:
            bot_id = dumper.find_bot_user_id("Linear")
            linear_mention = f"<@{bot_id}>" if bot_id else DEFAULT_LINEAR_MENTION

        comment = build_linear_mention_comment(
            team=team,
            project=project,
            title=final_title,
            description_lines=final_description_lines,
            labels=label_list,
            priority=priority,
            status=status,
            linear_mention=linear_mention,
        )

        if dry_run:
            typer.echo("--- Reply that would be posted ---")
            typer.echo(comment)
            return

        dumper.post_message(channel_id, comment, thread_ts=ts)
        typer.echo(f"✅ Posted reply in thread {ts} mentioning {linear_mention}")

    except Exception as e:
        typer.echo(f"❌ Error: {e}")


@slack_app.command("connect")
def slack_connect():
    """Connect to Slack browser for dump operations"""
    typer.echo("🌐 Opening Slack...")
    typer.echo("   Run: wf browser open https://app.slack.com")
    typer.echo("")
    typer.echo("Once Slack is open and logged in, you can run:")
    typer.echo("   wf slack channels   # List available channels")
    typer.echo("   wf slack dump <channel>   # Dump a channel")


def _discover_slack_team_id(client: BrowserClient) -> str:
    client.open("https://app.slack.com/client")
    time.sleep(3)
    r = client.eval("() => location.href")
    m = re.search(r"/client/(T[A-Z0-9]+)", r.get("result") or "")
    if not m:
        raise RuntimeError(
            "Could not auto-detect your Slack workspace id from the browser. "
            "Pass --slack-team-id explicitly (found in any app.slack.com URL, e.g. .../client/T0AML4ADUJY/...)."
        )
    return m.group(1)


@slack_app.command("teleop-linear-sync")
def teleop_linear_sync_cmd(
    channel_id: str = typer.Option(..., "--channel-id", help="Slack channel ID, e.g. C0BJ2PD955H"),
    channel_name: str = typer.Option("teleop", "--channel-name", help="Channel name without # (used in the search query)"),
    slack_team_id: str = typer.Option(None, "--slack-team-id", help="Slack workspace ID, e.g. T0AML4ADUJY (auto-detected if omitted)"),
    team: str = typer.Option("PAN", "--team", help="Linear team key"),
    project: str = typer.Option("Teleop", "--project", help="Linear project name"),
    labels: str = typer.Option("bug,teleop", "--labels", help="Comma-separated Linear labels"),
    priority: str = typer.Option("Medium", "--priority", help="Linear priority"),
    status: str = typer.Option("Triage", "--status", help="Linear workflow status"),
    limit: int = typer.Option(None, "--limit", help="Convert at most N pending issues this run"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Scan and report only; post nothing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt before posting"),
    chrome_debug_port: int = typer.Option(9222, "--chrome-debug-port", help="Port Chrome's --remote-debugging-port is listening on"),
):
    """Scan a channel for 'NEW ISSUE' posts from the Tele-op Issue Slack workflow,
    and @-mention the Linear app in-thread for every one not already converted
    (detected via the team's checkmark-reaction convention).

    Requires a real, already-logged-in Chrome window with remote debugging on,
    since Slack's Web API rejects browser cookies for reading/posting messages:

        /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
            --remote-debugging-port=9222 --remote-allow-origins=*

    then log into Slack in that window before running this command.
    """
    if not is_daemon_running():
        typer.echo("Starting browser daemon...")
        if not start_daemon(chrome_debug_port=chrome_debug_port):
            typer.echo(f"❌ Could not start the browser daemon / connect to Chrome on port {chrome_debug_port}.")
            typer.echo("   Make sure Chrome is running with --remote-debugging-port=9222 and you're logged into Slack.")
            raise typer.Exit(1)

    client = BrowserClient()

    try:
        team_id = slack_team_id or _discover_slack_team_id(client)
    except Exception as e:
        typer.echo(f"❌ {e}")
        raise typer.Exit(1)

    typer.echo(f"🔎 Scanning #{channel_name} ({channel_id}) for Tele-op Issue 'NEW ISSUE' posts...")
    try:
        found = scan_new_issue_status(client, team_id, channel_id, channel_name)
    except Exception as e:
        typer.echo(f"❌ Scan failed: {e}")
        raise typer.Exit(1)

    pending = [i for i in found if not i.already_converted]

    table = Table(title="Tele-op NEW ISSUE reports")
    table.add_column("when", style="cyan")
    table.add_column("Linear?", style="yellow")
    table.add_column("Title", style="green")
    for item in found:
        status_label = "✅ in Linear" if item.already_converted else "— pending"
        table.add_row(item.when, status_label, item.title or "(untitled)")
    console.print(table)
    typer.echo(f"\nTotal: {len(found)}, already in Linear: {len(found) - len(pending)}, pending: {len(pending)}")

    if dry_run:
        return
    if not pending:
        typer.echo("Nothing to convert.")
        return

    to_convert = pending[:limit] if limit else pending

    if not yes:
        confirmed = typer.confirm(
            f"Post the @Linear conversion reply for {len(to_convert)} pending issue(s) in #{channel_name}?"
        )
        if not confirmed:
            typer.echo("Cancelled.")
            return

    label_list = [l.strip() for l in labels.split(",") if l.strip()]
    succeeded, already_done, failed = [], [], []
    for i, item in enumerate(to_convert, 1):
        typer.echo(f"[{i}/{len(to_convert)}] Converting: {item.title[:60]}...")
        try:
            convert_new_issue(
                client, team_id, channel_id, item.ts, item.title or "(see thread for details)",
                team=team, project=project, labels=label_list, priority=priority, status=status,
            )
            succeeded.append(item)
            typer.echo("   ✅ sent")
        except AlreadyConverted:
            already_done.append(item)
            typer.echo("   ⏭️  already has a Linear reply, skipped (no checkmark yet -- add one to speed up future scans)")
        except Exception as e:
            failed.append((item, str(e)))
            typer.echo(f"   ❌ {e}")
        time.sleep(1.0)

    typer.echo(f"\nDone: {len(succeeded)}/{len(to_convert)} converted, {len(already_done)} already done, {len(failed)} failed.")
    if failed:
        typer.echo(f"⚠️  {len(failed)} failed and were skipped:")
        for item, err in failed:
            typer.echo(f"   - {item.ts} ({item.title[:40]}): {err}")


# Add Slack subcommand to main app
app.add_typer(slack_app, name="slack")

# Create alias subcommand group
alias_app = typer.Typer(help="Manage command aliases", no_args_is_help=True)

@alias_app.command("add")
def add_alias(
    name: str = typer.Argument(..., help="Alias name"),
    command: str = typer.Argument(..., help="Command to run (supports %REPO_ROOT%, %PWD%, %HOME% variables)"),
    repo_path: str = typer.Option(None, "--repo", help="Repository-specific command"),
    file_pattern: str = typer.Option(None, "--file", help="File pattern-specific command")
):
    """Add a new alias with variable substitution support
    
    Available variables:
    - %REPO_ROOT%: Git repository root directory
    - %PWD%: Current working directory  
    - %HOME%: User's home directory
    """
    alias_manager = AliasManager()
    
    context = {}
    
    if repo_path:
        context['repositories'] = {
            repo_path: {'command': command}
        }
        # Default command for other contexts
        base_command = typer.prompt("Base command for other contexts", default=command)
        alias_manager.add_alias(name, base_command, context)
    elif file_pattern:
        context['files'] = {
            file_pattern: {'command': command}
        }
        # Default command for other contexts
        base_command = typer.prompt("Base command for other contexts", default=command)
        alias_manager.add_alias(name, base_command, context)
    else:
        alias_manager.add_alias(name, command, context)
    
    typer.echo(f"✅ Added alias '{name}': {command}")

@alias_app.command()
def add_complex(
    name: str = typer.Argument(..., help="Alias name")
):
    """Add a complex alias (multi-line bash script)"""
    alias_manager = AliasManager()
    
    typer.echo(f"📝 Creating complex alias '{name}'...")
    typer.echo("Enter the command(s) (press Ctrl+D or type 'EOF' on a new line to finish):")
    
    lines = []
    try:
        while True:
            line = typer.prompt("", default="", show_default=False)
            if line.strip() == "EOF":
                break
            lines.append(line)
    except (KeyboardInterrupt, EOFError):
        pass
    
    if not lines:
        typer.echo("❌ No command provided. Alias creation cancelled.")
        return
    
    command = "\n".join(lines)
    alias_manager.add_alias(name, command)
    
    typer.echo(f"✅ Added complex alias '{name}':")
    typer.echo(f"   {command.replace(chr(10), ' && ')}")

@alias_app.command()
def list_aliases():
    """List all aliases"""
    alias_manager = AliasManager()
    aliases = alias_manager.list_aliases()
    
    if not aliases:
        typer.echo("No aliases configured.")
        return
    
    typer.echo("📝 Configured aliases:")
    typer.echo("   Variables: %REPO_ROOT%, %PWD%, %HOME%")
    for name, config in aliases.items():
        command = config['command']
        # Highlight if variables are used
        has_vars = any(var in command for var in ['%REPO_ROOT%', '%PWD%', '%HOME%'])
        var_indicator = " 🔄" if has_vars else ""
        typer.echo(f"  {name}: {command}{var_indicator}")
        
        if config.get('context'):
            context = config['context']
            if 'repositories' in context:
                typer.echo(f"    📁 Repository-specific:")
                for repo_path, repo_config in context['repositories'].items():
                    repo_cmd = repo_config.get('command', config['command'])
                    repo_has_vars = any(var in repo_cmd for var in ['%REPO_ROOT%', '%PWD%', '%HOME%'])
                    repo_var_indicator = " 🔄" if repo_has_vars else ""
                    typer.echo(f"      {repo_path}: {repo_cmd}{repo_var_indicator}")
            if 'files' in context:
                typer.echo(f"    📄 File-specific:")
                for pattern, file_config in context['files'].items():
                    file_cmd = file_config.get('command', config['command'])
                    file_has_vars = any(var in file_cmd for var in ['%REPO_ROOT%', '%PWD%', '%HOME%'])
                    file_var_indicator = " 🔄" if file_has_vars else ""
                    typer.echo(f"      {pattern}: {file_cmd}{file_var_indicator}")
    
    if any(any(var in config['command'] for var in ['%REPO_ROOT%', '%PWD%', '%HOME%']) 
           for config in aliases.values()):
        typer.echo("\n🔄 = Uses variable substitution")

@alias_app.command()
def remove_alias(
    name: str = typer.Argument(..., help="Alias name to remove")
):
    """Remove an alias"""
    alias_manager = AliasManager()
    
    if alias_manager.remove_alias(name):
        typer.echo(f"✅ Removed alias '{name}'")
    else:
        typer.echo(f"❌ Alias '{name}' not found")

@alias_app.command()
def edit():
    """Open alias configuration file in default EDITOR"""
    import os
    from pathlib import Path
    
    config_path = Path.home() / ".wf" / "aliases.yaml"
    
    # Ensure config file exists
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        # Create empty config file
        with open(config_path, 'w') as f:
            f.write("# Workflow Aliases Configuration\n# Add your aliases here\n")
        typer.echo(f"✅ Created new alias configuration file at: {config_path}")
    
    # Get editor preference
    editor = os.environ.get('EDITOR')
    if not editor:
        # Try common editors as fallback
        for fallback_editor in ['code', 'vim', 'nano', 'vi']:
            try:
                subprocess.run(['which', fallback_editor], check=True, capture_output=True)
                editor = fallback_editor
                break
            except subprocess.CalledProcessError:
                continue
        
        if not editor:
            typer.echo("❌ No editor found. Please set the EDITOR environment variable.")
            typer.echo("   Example: export EDITOR=code")
            raise typer.Exit(1)
        
        typer.echo(f"ℹ️  EDITOR not set, using: {editor}")
    
    try:
        typer.echo(f"📝 Opening alias configuration: {config_path}")
        subprocess.run([editor, str(config_path)], check=True)
        typer.echo("✅ Alias configuration updated")
    except subprocess.CalledProcessError as e:
        typer.echo(f"❌ Failed to open editor: {e}")
        raise typer.Exit(1)
    except FileNotFoundError:
        typer.echo(f"❌ Editor '{editor}' not found")
        typer.echo("💡 Please set EDITOR to a valid editor or install one")
        raise typer.Exit(1)

@alias_app.command()
def run_alias(
    name: str = typer.Argument(..., help="Alias to run"),
    args: List[str] = typer.Argument(None, help="Additional arguments to pass to command")
):
    """Run an alias"""
    from pathlib import Path
    alias_manager = AliasManager()
    result = alias_manager.execute_alias(name, args, str(Path.cwd()))
    if result != 0:
        raise typer.Exit(result)

# Add alias subcommand to main app
app.add_typer(alias_app, name="alias")

# Create hook subcommand group
hook_app = typer.Typer(help="Manage command hooks and automation", no_args_is_help=True)

@hook_app.command()
def add_hook(
    name: str = typer.Argument(..., help="Hook name"),
    trigger: str = typer.Argument(..., help="Command that triggers this hook"),
    action: str = typer.Argument(..., help="Action to execute when triggered"),
    success_hook: str = typer.Option(None, "--success", help="Hook to run on success"),
    fail_hook: str = typer.Option(None, "--fail", help="Hook to run on failure"),
    output_var: str = typer.Option(None, "--output-var", help="Environment variable for output"),
    condition: str = typer.Option(None, "--condition", help="Condition to execute"),
    pass_output: bool = typer.Option(True, "--pass-output/--no-pass-output", help="Pass output to next command")
):
    """Add a new hook"""
    hook_manager = HookManager()
    
    if hook_manager.add_hook(
        name, trigger, action, success_hook, fail_hook,
        output_var, condition, pass_output
    ):
        typer.echo(f"✅ Added hook '{name}'")
        typer.echo(f"   Trigger: {trigger}")
        typer.echo(f"   Action: {action}")
        if success_hook:
            typer.echo(f"   On success: {success_hook}")
        if fail_hook:
            typer.echo(f"   On failure: {fail_hook}")
        if condition:
            typer.echo(f"   Condition: {condition}")
    else:
        typer.echo(f"❌ Failed to add hook '{name}'")

@hook_app.command()
def list_hooks():
    """List all hooks"""
    hook_manager = HookManager()
    hooks = hook_manager.list_hooks()
    
    if not hooks:
        typer.echo("No hooks configured.")
        return
    
    typer.echo("🔗 Configured hooks:")
    for name, config in hooks.items():
        typer.echo(f"  {name}:")
        typer.echo(f"    Trigger: {config['trigger']}")
        typer.echo(f"    Action: {config['action']}")
        
        if config.get('condition'):
            typer.echo(f"    Condition: {config['condition']}")
        if config.get('success_hook'):
            typer.echo(f"    Success → {config['success_hook']}")
        if config.get('fail_hook'):
            typer.echo(f"    Failure → {config['fail_hook']}")
        if config.get('pass_output'):
            typer.echo(f"    Output var: {config['output_var']}")

@hook_app.command()
def remove_hook(
    name: str = typer.Argument(..., help="Hook name to remove")
):
    """Remove a hook"""
    hook_manager = HookManager()
    
    if hook_manager.remove_hook(name):
        typer.echo(f"✅ Removed hook '{name}'")
    else:
        typer.echo(f"❌ Hook '{name}' not found")

@hook_app.command()
def run_hook(
    name: str = typer.Argument(..., help="Hook to run")
):
    """Run a hook manually"""
    hook_manager = HookManager()
    result = hook_manager.execute_hook(name)
    
    typer.echo(f"\n🔗 Hook Chain Results:")
    for step in result['hook_chain']:
        status = "✅" if step['success'] else "❌"
        typer.echo(f"  {status} {step['hook']}: {step['command']}")
        
        if step.get('stdout') and step['success']:
            # Show only first line of output to keep it clean
            output_line = step['stdout'].split('\n')[0]
            if len(output_line) > 80:
                output_line = output_line[:77] + "..."
            typer.echo(f"     Output: {output_line}")
        
        if not step['success'] and step.get('stderr'):
            error_line = step['stderr'].split('\n')[0]
            if len(error_line) > 80:
                error_line = error_line[:77] + "..."
            typer.echo(f"     Error: {error_line}")
    
    if result.get('chained_to'):
        typer.echo(f"   🔄 Chained to: {result['chained_to']}")
    
    if not result['success']:
        raise typer.Exit(1)

@hook_app.command()
def test_trigger(
    command: str = typer.Argument(..., help="Command to test hooks for")
):
    """Show which hooks would be triggered by a command"""
    hook_manager = HookManager()
    mock_result = {'success': True, 'stdout': 'test output', 'stderr': ''}
    
    triggered = hook_manager.find_triggered_hooks(command, mock_result)
    
    if not triggered:
        typer.echo(f"No hooks would be triggered by: {command}")
        return
    
    typer.echo(f"🔗 Hooks triggered by: {command}")
    for hook_name in triggered:
        hook = hook_manager.get_hook(hook_name)
        typer.echo(f"  {hook_name}: {hook['action']}")

# Add hook subcommand to main app
app.add_typer(hook_app, name="hook")


@app.command()
def cd(
    repo_name: str = typer.Argument(None, help="Repository name (tab completion available)")
):
    """Change directory to a configured repository folder from any project"""
    import os
    from pathlib import Path
    import subprocess
    
    # Get all repositories from all projects
    from workflow.projects import list_projects, get_current_project
    all_projects = list_projects()
    
    if not all_projects:
        typer.echo("❌ No projects configured.")
        return
    
    # Collect all repositories from all projects
    all_repos = {}
    for project_name, project_config in all_projects.items():
        repos = project_config.get("repositories", {})
        for repo_path, repo_config in repos.items():
            all_repos[repo_path] = {
                **repo_config,
                "project": project_name
            }
    
    if not all_repos:
        typer.echo("❌ No repositories configured in any project.")
        return
    
    # If no repo_name provided, list available repositories
    if not repo_name:
        current_project = get_current_project()
        
        if current_project:
            typer.echo(f"📁 Repositories from all projects (current: {current_project}):")
        else:
            typer.echo("📁 Available repositories from all projects:")
        
        # Group repositories by project
        repos_by_project = {}
        for repo_path, repo_config in all_repos.items():
            project_name = repo_config["project"]
            if project_name not in repos_by_project:
                repos_by_project[project_name] = []
            repos_by_project[project_name].append((repo_path, repo_config))
        
        # Display by project, with current project first
        project_order = [current_project] if current_project else []
        project_order += [p for p in repos_by_project.keys() if p != current_project]
        
        counter = 1
        for project_name in project_order:
            if project_name == current_project:
                typer.echo(f"\n🔸 {project_name} (current):")
            else:
                typer.echo(f"\n📁 {project_name}:")
            
            for repo_path, repo_config in repos_by_project[project_name]:
                repo_display_name = Path(repo_path).name
                base_branch = repo_config.get("base_branch", "main")
                typer.echo(f"  {counter}. {repo_display_name} ({repo_path}) [base: {base_branch}]")
                counter += 1
        return
    
    # Find matching repository by name across all projects
    matching_repo = None
    matching_project = None
    for repo_path, repo_config in all_repos.items():
        repo_display_name = Path(repo_path).name
        if repo_name == repo_display_name:
            matching_repo = repo_path
            matching_project = repo_config["project"]
            break
        # Also allow partial matching on repository name only
        if repo_name.lower() in repo_display_name.lower():
            if matching_repo is None:  # Take first match
                matching_repo = repo_path
                matching_project = repo_config["project"]
    
    if not matching_repo:
        typer.echo(f"❌ No repository found matching '{repo_name}'")
        typer.echo("Available repositories from all projects:")
        
        # Group repositories by project for error message
        repos_by_project = {}
        for repo_path, repo_config in all_repos.items():
            project_name = repo_config["project"]
            if project_name not in repos_by_project:
                repos_by_project[project_name] = []
            repos_by_project[project_name].append((repo_path, repo_config))
        
        for project_name, repo_list in repos_by_project.items():
            marker = " (current)" if project_name == get_current_project() else ""
            typer.echo(f"\n📁 {project_name}{marker}:")
            for repo_path, repo_config in repo_list:
                repo_display_name = Path(repo_path).name
                base_branch = repo_config.get("base_branch", "main")
                typer.echo(f"  - {repo_display_name} ({repo_path}) [base: {base_branch}]")
        return
    
    # Check if repository exists
    if not Path(matching_repo).exists():
        typer.echo(f"❌ Repository path does not exist: {matching_repo}")
        return
    
    # Use subprocess to change directory in parent shell using same approach as wf start
    repo_path = str(Path(matching_repo).expanduser())
    
    # Get shell commands from hooks BEFORE creating AppleScript
    from workflow.hooks import HookManager
    hook_manager = HookManager()
    cd_context = {
        'repo_path': repo_path,
        'repo_name': Path(repo_path).name,
        'project': matching_project
    }
    shell_commands = hook_manager.get_shell_commands('wf cd', cd_context)
    
    # Build the command string
    cmd_parts = [f"cd '{repo_path}'"]
    if shell_commands:
        cmd_parts.extend(shell_commands)
    else:
        cmd_parts.append("clear")
        cmd_parts.append(f"echo 'Changed to: {repo_path}'")
    
    shell_command = " && ".join(cmd_parts)
    
    # Create AppleScript to change directory in current terminal tab
    applescript = f"""
    tell application "Terminal"
        set currentTab to (selected tab of window 1)
        do script "{shell_command}" in currentTab
    end tell
    """
    
    # Write to temp file and execute
    temp_file = "/tmp/wf_cd.applescript"
    with open(temp_file, "w") as f:
        f.write(applescript)
    
    # Execute AppleScript like wf start does
    os.system(f"osascript '{temp_file}' 2>/dev/null &")
    
    # Show success message with project information
    current_project = get_current_project()
    if matching_project == current_project:
        typer.echo(f"📁 Changed to: {repo_path} (project: {matching_project})")
    else:
        typer.echo(f"📁 Changed to: {repo_path} (project: {matching_project}) - not current project")

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
    
    if not backend:
        typer.echo("❌ Jira backend not available.")
        typer.echo("💡 Run 'wf init' to set up Jira integration.")
        return
    
    # Task information
    typer.echo("📋 Current Task Status")
    typer.echo("=" * 50)
    typer.echo(f"🔑 Task Key: {issue.key}")

    # Check which backend is being used
    from workflow.backends.markdown import MarkdownBackend
    is_markdown_backend = isinstance(backend, MarkdownBackend)

    if is_markdown_backend:
        # Show markdown file path for local tasks
        from pathlib import Path
        task_file = Path.home() / '.wf' / 'tasks' / f"{issue.key.lower()}.md"
        # Try to find the actual file
        for project_dir in (Path.home() / '.wf' / 'tasks').iterdir():
            if project_dir.is_dir():
                potential_file = project_dir / f"{issue.key.lower()}.md"
                if potential_file.exists():
                    task_file = potential_file
                    break
        typer.echo(f"📝 Local Task: {task_file}")
    else:
        # Get Jira URL from config and create task link
        if cfg.get("jira"):
            jira_url = cfg.get("jira", {}).get("url", "")
            if jira_url:
                jira_link = f"{jira_url.rstrip('/')}/browse/{issue.key}"
                typer.echo(f"🔗 Jira: {jira_link}")
        else:
            typer.echo("ℹ️ Jira URL not configured")
    
    # Try multiple possible attribute names for title
    title = getattr(issue, 'summary', None) or getattr(issue, 'title', None) or str(issue)
    typer.echo(f"📝 Title: {title}")
    
    # Check if task is done or in review
    try:
        if backend:
            is_done = backend.is_done(issue)
            is_in_review = backend.is_in_review(issue) if hasattr(backend, 'is_in_review') else False
        else:
            is_done = False
            is_in_review = False
    except (AttributeError, Exception):
        is_done = False
        is_in_review = False
    
    if is_done:
        typer.echo("✅ State: Completed")
    elif is_in_review:
        typer.echo("👀 State: In Review")
    else:
        typer.echo("🔄 State: In Progress")
    
    # Repository and PR information
    from workflow.projects import get_current_project_repositories
    repos = get_current_project_repositories()
    if repos:
        typer.echo(f"\n📁 Repository Status")
        typer.echo("-" * 30)
        
        from workflow.git_utils import check_existing_prs_for_issue, has_uncommitted_changes, get_uncommitted_changes_summary
        
        repo_paths = list(repos.keys())
        existing_prs = check_existing_prs_for_issue(issue, repo_paths)
        
        pr_found = False
        for repo_path in repo_paths:
            repo_name = repos[repo_path].get("name", repo_path.split("/")[-1])
            typer.echo(f"\n📂 {repo_name} ({repo_path})")
            
            # PR status
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
            
            # Git status
            try:
                from git import Repo
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
        typer.echo(f"\n⚠️  No repositories configured. Use 'wf config repo-add' to add repositories.")
    

    
    typer.echo(f"\n🚀 Quick Actions:")
    typer.echo(f"  wf done               # Complete task and create PRs")
    typer.echo(f"  wf ai                 # Start AI session")
    typer.echo(f"  wf slack notify       # Post status to Slack")
    typer.echo(f"  wf alias add <name>   # Add new alias")
    typer.echo(f"  wf exec-cmd <command> # Run alias or custom command")
    typer.echo(f"  wf hook add-hook <name> # Add command hook")
    typer.echo(f"  wf hook list-hooks     # List all hooks")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def exec_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Alias or command to run"),
    trigger_hooks: bool = typer.Option(True, "--trigger-hooks/--no-trigger-hooks", help="Trigger hooks after command execution")
):
    """Run an alias or custom command"""
    from pathlib import Path
    alias_manager = AliasManager()
    hook_manager = HookManager()
    
    # Get all remaining arguments
    args = ctx.args if ctx.args else []
    
    command = None
    command_result = None
    
    # Check if this is an alias first
    if alias_manager.get_alias(name):
        # Execute alias and capture result
        exit_code = alias_manager.execute_alias(name, args, str(Path.cwd()))
        command_result = {
            'success': exit_code == 0,
            'return_code': exit_code,
            'stdout': '',
            'stderr': '',
            'command': f"alias: {name}"
        }
        command = f"alias: {name}"
    else:
        # If not an alias, treat as a raw command
        command = f"{name} {' '.join(args)}" if args else name
        try:
            typer.echo(f"🚀 Running: {command}")
            result = subprocess.run(command, shell=True, capture_output=True, text=True, check=False)
            
            command_result = {
                'success': result.returncode == 0,
                'return_code': result.returncode,
                'stdout': result.stdout.strip(),
                'stderr': result.stderr.strip(),
                'command': command
            }
            
            if result.returncode == 0:
                typer.echo("✅ Command completed successfully")
            else:
                typer.echo(f"❌ Command failed with exit code {result.returncode}")
                if result.stderr.strip():
                    typer.echo(f"Error: {result.stderr.strip()}")
            
        except Exception as e:
            typer.echo(f"❌ Error executing command: {e}")
            command_result = {
                'success': False,
                'return_code': -1,
                'stdout': '',
                'stderr': str(e),
                'command': command
            }
    
    # Trigger hooks if enabled and we have a command
    if trigger_hooks and command and command_result:
        triggered_results = hook_manager.execute_triggered_hooks(command, command_result)
        
        if triggered_results:
            typer.echo(f"\n🔗 {len(triggered_results)} hook chain(s) executed:")
            for i, hook_result in enumerate(triggered_results, 1):
                main_success = hook_result.get('success', False)
                chain_steps = len(hook_result.get('hook_chain', []))
                main_hook = hook_result.get('main_hook', 'unknown')
                typer.echo(f"  {i}. {main_hook}: {'✅' if main_success else '❌'} ({chain_steps} steps)")
                
                # Show brief output for each step
                for step in hook_result['hook_chain']:
                    status = "✅" if step['success'] else "❌"
                    step_name = step['hook']
                    if step.get('stdout') and step['success']:
                        output = step['stdout'].split('\n')[0]
                        if len(output) > 50:
                            output = output[:47] + "..."
                        typer.echo(f"     {status} {step_name}: {output}")
    
    # Exit with appropriate code
    if command_result and not command_result['success']:
        raise typer.Exit(command_result.get('return_code', 1))


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """Main workflow CLI"""
    # If no subcommand is provided, show status
    if ctx.invoked_subcommand is None:
        status()


# Variables management commands

@variables_app.command("add")
def add_variable(
    name: str = typer.Argument(..., help="Variable name"),
    command: str = typer.Option(None, help="Command to run to get value"),
    value: str = typer.Option(None, help="Static value for the variable"),
    var_type: str = typer.Option(None, help="Variable type (e.g., jira_sprint, jira_field)"),
    cache_enabled: bool = typer.Option(True, help="Enable caching for this variable"),
    cache_ttl: int = typer.Option(300, help="Cache TTL in seconds (default: 300)"),
):
    """Add a new variable configuration"""
    cfg = load_config()
    
    config = {}
    if command:
        config["command"] = command
    if value:
        config["value"] = value
    if var_type:
        config["type"] = var_type
    
    config["cache"] = {
        "enabled": cache_enabled,
        "ttl": cache_ttl
    }
    
    resolver = VariableResolver(cfg)
    resolver.add_variable(name, config)
    
    console.print(f"✅ Added variable: {name}")
    if command:
        console.print(f"   Command: {command}")
    if value:
        console.print(f"   Value: {value}")
    if var_type:
        console.print(f"   Type: {var_type}")
    console.print(f"   Cache: {'enabled' if cache_enabled else 'disabled'} ({cache_ttl}s TTL)")

@variables_app.command("remove")
def remove_variable(
    name: str = typer.Argument(..., help="Variable name to remove")
):
    """Remove a variable configuration"""
    cfg = load_config()
    resolver = VariableResolver(cfg)
    resolver.remove_variable(name)

@variables_app.command("list")
def list_variables():
    """List all configured variables with their current values"""
    cfg = load_config()
    resolver = VariableResolver(cfg)
    
    variables = resolver.list_variables()
    
    if not variables:
        console.print("No variables configured")
        return
    
    table = Table(title="Configured Variables")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Type", style="magenta")
    table.add_column("Config", style="green")
    table.add_column("Current Value", style="yellow")
    
    for name, info in variables.items():
        var_config = info["config"]
        current_value = info["current_value"]
        
        # Determine type and config display
        var_type = "Static"
        config_display = ""
        
        if "command" in var_config:
            var_type = "Command"
            config_display = var_config["command"]
        elif "type" in var_config:
            var_type = var_config["type"]
            config_display = f"type: {var_config['type']}"
        elif "value" in var_config:
            config_display = var_config["value"]
        
        # Cache info
        cache_config = var_config.get("cache", {})
        cache_info = f"cache: {'on' if cache_config.get('enabled', True) else 'off'} ({cache_config.get('ttl', 300)}s)"
        
        table.add_row(
            name,
            var_type,
            f"{config_display}\n{cache_info}",
            str(current_value) if current_value is not None else "null"
        )
    
    console.print(table)

@variables_app.command("test")
def test_variable(
    name: str = typer.Argument(..., help="Variable name to test"),
    clear_cache: bool = typer.Option(False, "--clear-cache", help="Clear cache before testing")
):
    """Test a variable and show its value"""
    cfg = load_config()
    resolver = VariableResolver(cfg)
    
    if clear_cache:
        resolver.clear_cache(name)
        console.print(f"🗑️  Cleared cache for {name}")
    
    console.print(f"🧪 Testing variable: {name}")
    value = resolver.get_variable_value(name)
    console.print(f"📋 Result: {value}")

@variables_app.command("resolve")
def resolve_text(
    text: str = typer.Argument(..., help="Text to resolve variables in")
):
    """Resolve variables in a text string"""
    cfg = load_config()
    resolver = VariableResolver(cfg)
    
    console.print(f"📝 Original: {text}")
    resolved = resolver.resolve_variables(text)
    console.print(f"✅ Resolved: {resolved}")

@variables_app.command("cache")
def cache_commands(
    action: str = typer.Argument(..., help="Action: clear, clear [variable-name]"),
    name: str = typer.Argument(None, help="Variable name (for 'clear' action)"),
):
    """Manage variable caches"""
    cfg = load_config()
    resolver = VariableResolver(cfg)
    
    if action == "clear":
        if name:
            resolver.clear_cache(name)
        else:
            resolver.clear_cache()  # Clear all
    else:
        console.print(f"❌ Unknown action: {action}")
        console.print("Available actions: clear, clear [variable-name]")
        raise typer.Exit(1)


# Add a catch-all command handler for aliases
@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def default_command_handler(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Command name")
):
    """Handle undefined commands as potential aliases"""
    from pathlib import Path
    alias_manager = AliasManager()
    
    # Check if this is an alias
    if alias_manager.get_alias(name):
        # Get all remaining arguments
        args = ctx.args if ctx.args else []
        result = alias_manager.execute_alias(name, args, str(Path.cwd()))
        if result != 0:
            raise typer.Exit(result)
        return
    
    # If not an alias, show help
    typer.echo(f"❌ Unknown command: {name}")
    typer.echo("Use 'wf --help' for available commands or 'wf alias list-aliases' to see aliases.")
    raise typer.Exit(1)


# Create monitor subcommand group
monitor_app = typer.Typer(help="Monitor filesystem, GitHub PRs, and APIs", no_args_is_help=True)

# Create add subcommand group
add_app = typer.Typer(help="Add new monitors", no_args_is_help=True)
monitor_app.add_typer(add_app, name="add")

@add_app.command("file")
def add_file_monitor(
    path: str = typer.Argument(..., help="Path to monitor"),
    name: str = typer.Option(None, "--name", help="Monitor name (optional - if not specified, monitor is ephemeral)"),
    action: str = typer.Option(None, "--action", help="Command to execute when changes are detected"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start monitoring immediately (default: yes)"),
    duration: int = typer.Option(None, "--duration", help="Monitoring duration in seconds"),
    interval: int = typer.Option(2, "--interval", help="Check interval in seconds"),
    background: bool = typer.Option(False, "--background", help="Run monitor in background"),
):
    """Add a file system monitor and start monitoring"""
    from workflow.monitor import get_monitor
    
    monitor = get_monitor()
    if name:
        # Named monitor - add to persistent configuration
        if monitor.add_file_monitor(name, path, action=action):
            if start:
                monitor.start_monitoring(duration=duration, interval=interval)
            else:
                typer.echo(f"💡 Monitor '{name}' added. Use 'wf monitor start' to begin monitoring.")
    else:
        # Ephemeral monitor - create temporary monitor
        from workflow.monitor import FileMonitor
        from rich.console import Console
        console = Console(color_system=None)
        
        config = {'action': action}
        temp_monitor = FileMonitor(path, config)
        
        if start:
            monitor._run_ephemeral_monitor(temp_monitor, "ephemeral-file", duration=duration, interval=interval, background=background)
        else:
            typer.echo(f"💡 Ephemeral file monitor created for '{path}'. Use --start to begin monitoring.")

@add_app.command("github")
def add_github_monitor(
    pr_identifier: str = typer.Argument(..., help="PR identifier (owner/repo#123)"),
    name: str = typer.Option(None, "--name", help="Monitor name (optional - if not specified, monitor is ephemeral)"),
    action: str = typer.Option(None, "--action", help="Command to execute when changes are detected"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start monitoring immediately (default: yes)"),
    duration: int = typer.Option(None, "--duration", help="Monitoring duration in seconds"),
    interval: int = typer.Option(2, "--interval", help="Check interval in seconds"),
    background: bool = typer.Option(False, "--background", help="Run monitor in background"),
):
    """Add a GitHub PR monitor and start monitoring"""
    from workflow.monitor import get_monitor
    
    # Parse PR identifier
    if '#' in pr_identifier and '/' in pr_identifier:
        repo_part, pr_part = pr_identifier.split('#')
        if '/' in repo_part:
            owner, repo = repo_part.split('/', 1)
            try:
                pr_number = int(pr_part)
                monitor = get_monitor()
                if name:
                    # Named monitor - add to persistent configuration
                    if monitor.add_github_monitor(name, owner, repo, pr_number, action=action):
                        if start:
                            monitor.start_monitoring(duration=duration, interval=interval)
                        else:
                            typer.echo(f"💡 Monitor '{name}' added. Use 'wf monitor start' to begin monitoring.")
                else:
                    # Ephemeral monitor - create temporary monitor
                    from workflow.monitor import GitHubPRMonitor
                    from rich.console import Console
                    console = Console(color_system=None)
                    
                    config = {'action': action, 'github_token': monitor.wf_config.get("github", {}).get("token")}
                    temp_monitor = GitHubPRMonitor(owner, repo, pr_number, config)
                    
                    if start:
                        monitor._run_ephemeral_monitor(temp_monitor, "ephemeral-github", duration=duration, interval=interval, background=background)
                    else:
                        typer.echo(f"💡 Ephemeral GitHub PR monitor created for {owner}/{repo}#{pr_number}. Use --start to begin monitoring.")
                return
            except ValueError:
                typer.echo(f"❌ Invalid PR number: {pr_part}")
        else:
            typer.echo(f"❌ Invalid repo format: {repo_part}")
    else:
        typer.echo("❌ Invalid PR identifier format. Use: owner/repo#123")
        return

@add_app.command("github-comment")
def add_github_comment_monitor(
    pr_identifier: str = typer.Argument(..., help="PR identifier (owner/repo#123)"),
    name: str = typer.Option(None, "--name", help="Monitor name (optional - if not specified, monitor is ephemeral)"),
    action: str = typer.Option(None, "--action", help="Command to execute when changes are detected (legacy)"),
    notify: bool = typer.Option(False, "--notify", help="Add notification action"),
    ai_console: bool = typer.Option(False, "--ai-console", help="Open AI console on new comments"),
    terminal: bool = typer.Option(False, "--terminal", help="Open terminal tabs on new comments"),
    notification_title: str = typer.Option("💬 New Comment by {{comment_author}}", "--notification-title", help="Notification title template"),
    notification_message: str = typer.Option("{{comment_body}}", "--notification-message", help="Notification message template"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start monitoring immediately (default: yes)"),
    duration: int = typer.Option(None, "--duration", help="Monitoring duration in seconds"),
    interval: int = typer.Option(2, "--interval", help="Check interval in seconds"),
    background: bool = typer.Option(False, "--background", help="Run monitor in background")
):
    """Add a GitHub PR comment monitor and start monitoring"""
    from workflow.monitor import get_monitor
    
    # Parse PR identifier
    if '#' in pr_identifier and '/' in pr_identifier:
        repo_part, pr_part = pr_identifier.split('#')
        if '/' in repo_part:
            owner, repo = repo_part.split('/', 1)
            try:
                pr_number = int(pr_part)
                monitor = get_monitor()
                
                # Build actions configuration
                actions = []
                
                if notify:
                    actions.append({
                        "id": f"{name}_notification" if name else "comment_notification",
                        "type": "notification",
                        "title": notification_title,
                        "message": notification_message,
                        "response_options": ["View Comment", "Reply", "Open AI Console"] if ai_console else ["View Comment", "Reply"]
                    })
                
                if ai_console:
                    actions.append({
                        "id": f"{name}_ai_console" if name else "comment_ai_console",
                        "type": "open_ai_console"
                    })
                
                if terminal:
                    actions.append({
                        "id": f"{name}_terminal" if name else "comment_terminal",
                        "type": "open_terminal"
                    })
                
                if name:
                    if actions:
                        # Use action framework
                        monitor_config = {
                            "name": name,
                            "type": "github_comment",
                            "owner": owner,
                            "repo": repo,
                            "pr_number": pr_number,
                            "actions": actions
                        }
                        
                        if monitor.add_monitor_from_config(monitor_config):
                            typer.echo(f"✅ GitHub PR comment monitor '{name}' added with {len(actions)} actions")
                            if start:
                                typer.echo(f"🔍 Starting monitoring for {name}")
                                monitor.start_monitoring(duration=duration, interval=interval)
                            else:
                                typer.echo(f"💡 Monitor '{name}' added. Use 'wf monitor start' to begin monitoring.")
                        else:
                            typer.echo(f"❌ Failed to add monitor '{name}'")
                    elif action:
                        # Fallback to legacy action
                        if monitor.add_github_comment_monitor(name, owner, repo, pr_number, action=action):
                            if start:
                                monitor.start_monitoring(duration=duration, interval=interval)
                            else:
                                typer.echo(f"💡 Monitor '{name}' added. Use 'wf monitor start' to begin monitoring.")
                    else:
                        typer.echo("❌ No actions specified. Use --notify, --ai-console, --terminal, or --action")
                else:
                    # Ephemeral monitor
                    if actions:
                        # Create temporary monitor with actions
                        from workflow.monitor import GitHubPRCommentMonitor
                        console = Console(color_system=None)
                        
                        config = {
                            'actions': actions,
                            'github_token': monitor.wf_config.get("github", {}).get("token")
                        }
                        temp_monitor = GitHubPRCommentMonitor(owner, repo, pr_number, config)
                        
                        typer.echo(f"🚀 Ephemeral GitHub PR comment monitor created for {owner}/{repo}#{pr_number} with {len(actions)} actions")
                        if start:
                            monitor._run_ephemeral_monitor(temp_monitor, "ephemeral-github-comment", duration=duration, interval=interval, background=background)
                        else:
                            typer.echo(f"💡 Ephemeral monitor created. Use --start to begin monitoring.")
                    elif action:
                        # Legacy ephemeral monitor
                        from workflow.monitor import GitHubPRCommentMonitor
                        console = Console(color_system=None)
                        
                        config = {'action': action, 'github_token': monitor.wf_config.get("github", {}).get("token")}
                        temp_monitor = GitHubPRCommentMonitor(owner, repo, pr_number, config)
                        
                        if start:
                            monitor._run_ephemeral_monitor(temp_monitor, "ephemeral-github-comment", duration=duration, interval=interval, background=background)
                        else:
                            typer.echo(f"💡 Ephemeral GitHub PR comment monitor created for {owner}/{repo}#{pr_number}. Use --start to begin monitoring.")
                    else:
                        typer.echo("❌ No actions specified. Use --notify, --ai-console, --terminal, or --action")
                return
            except ValueError:
                typer.echo(f"❌ Invalid PR number: {pr_part}")
        else:
            typer.echo(f"❌ Invalid repo format: {repo_part}")
    else:
        typer.echo("❌ Invalid PR identifier format. Use: owner/repo#123")
        return

@add_app.command("api")
def add_api_monitor(
    url: str = typer.Argument(..., help="API URL to monitor"),
    name: str = typer.Option(None, "--name", help="Monitor name (optional - if not specified, monitor is ephemeral)"),
    action: str = typer.Option(None, "--action", help="Command to execute when changes are detected"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start monitoring immediately (default: yes)"),
    duration: int = typer.Option(None, "--duration", help="Monitoring duration in seconds"),
    interval: int = typer.Option(2, "--interval", help="Check interval in seconds"),
    background: bool = typer.Option(False, "--background", help="Run monitor in background"),
):
    """Add an API monitor and start monitoring"""
    from workflow.monitor import get_monitor
    
    monitor = get_monitor()
    if name:
        # Named monitor - add to persistent configuration
        if monitor.add_api_monitor(name, url, action=action):
            if start:
                monitor.start_monitoring(duration=duration, interval=interval)
            else:
                typer.echo(f"💡 Monitor '{name}' added. Use 'wf monitor start' to begin monitoring.")
    else:
        # Ephemeral monitor - create temporary monitor
        from workflow.monitor import APIMonitor
        from rich.console import Console
        console = Console(color_system=None)
        
        config = {'action': action}
        temp_monitor = APIMonitor("ephemeral-api", url, config)
        
        if start:
            monitor._run_ephemeral_monitor(temp_monitor, "ephemeral-api", duration=duration, interval=interval, background=background)
        else:
            typer.echo(f"💡 Ephemeral API monitor created for '{url}'. Use --start to begin monitoring.")

@add_app.command("github-comments")
def add_github_comments_action(
    pr_identifier: str = typer.Argument(..., help="PR identifier (owner/repo#123)"),
    limit: int = typer.Option(10, "--limit", help="Maximum number of comments to fetch"),
    since: int = typer.Option(None, "--since", help="Fetch comments since this comment ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON for one-time execution"),
    notify: bool = typer.Option(False, "--notify", help="Send notification when new comments are detected"),
    name: str = typer.Option(None, "--name", help="Monitor name (optional - if not specified, action runs once)"),
    start: bool = typer.Option(True, "--start/--no-start", help="Execute action immediately (default: yes)"),
    duration: int = typer.Option(None, "--duration", help="Repeat duration in seconds (for monitoring mode)"),
    interval: int = typer.Option(30, "--interval", help="Check interval in seconds (for monitoring mode)"),
    background: bool = typer.Option(False, "--background", help="Run in background (for monitoring mode)")
):
    """Monitor GitHub PR comments using action-based monitoring"""
    from workflow.github_comment_action import lookup_github_comments
    from workflow.monitor import get_monitor
    
    # Parse PR identifier
    if '#' not in pr_identifier or '/' not in pr_identifier:
        typer.echo("❌ Invalid PR identifier format. Use: owner/repo#123")
        return
    
    repo_part, pr_part = pr_identifier.split('#')
    if '/' not in repo_part:
        typer.echo("❌ Invalid PR identifier format. Use: owner/repo#123")
        return
    
    owner, repo = repo_part.split('/', 1)
    
    try:
        pr_number = int(pr_part)
    except ValueError:
        typer.echo("❌ Invalid PR number")
        return
    
    monitor = get_monitor()
    
    # Build actions configuration
    actions = []
    
    if notify:
        actions.append({
            "id": f"{'ephemeral' if not name else name}_notification",
            "type": "notification",
            "title": "💬 New Comments on {{owner}}/{{repo}}#{{pr_number}}",
            "message": "{{count}} new comment(s) detected - Latest by {{comments.0.author}}: {{comments.0.body}}"
        })
    
    if name:
        # Named persistent monitor
        monitor_config = {
            "name": name,
            "type": "github_comment_action",
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "limit": limit,
            "since": since,
            "json_output": json_output
        }
        
        if actions:
            monitor_config["actions"] = actions
        
        if monitor.add_monitor_from_config(monitor_config):
            typer.echo(f"✅ GitHub comments monitor '{name}' added")
            if start:
                typer.echo(f"🔍 Starting monitoring for {name}")
                monitor.start_monitoring(duration=duration, interval=interval)
            else:
                typer.echo(f"💡 Monitor '{name}' added. Use 'wf monitor start' to begin monitoring.")
        else:
            typer.echo(f"❌ Failed to add monitor '{name}'")
    else:
        # Ephemeral monitor - runs indefinitely until Ctrl+C
        monitor_config = {
            "type": "github_comment_action",
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "limit": limit,
            "since": since,
            "json_output": json_output
        }
        
        if actions:
            monitor_config["actions"] = actions
        
        if start:
            # Create temporary monitor for immediate execution
            from workflow.monitor import ActionBasedMonitor
            
            typer.echo(f"🚀 Starting ephemeral GitHub comments monitor for {owner}/{repo}#{pr_number}...")
            typer.echo("💡 Press Ctrl+C to stop monitoring")
            
            action_config = {
                'owner': owner,
                'repo': repo,
                'pr_number': pr_number,
                'limit': limit,
                'since': since
            }
            
            temp_monitor = ActionBasedMonitor(
                name="ephemeral_github_comments",
                action_type='github_comment_lookup',
                action_config=action_config,
                monitor_config=monitor_config
            )
            
            # Add actions to the monitor if configured
            if actions:
                temp_monitor.monitor_config = {**monitor_config, 'actions': actions}
            
            # Run ephemeral monitor indefinitely
            monitor._run_ephemeral_monitor(temp_monitor, "ephemeral-github-comments", duration=None, interval=interval, background=background)
        else:
            typer.echo("💡 Use --start to begin ephemeral monitoring immediately")

@add_app.command("action")
def add_action_monitor(
    action_command: str = typer.Argument(..., help="Action command to monitor (e.g., 'github-comments owner/repo#123')"),
    name: str = typer.Option(None, "--name", help="Monitor name (optional - if not specified, monitor is ephemeral)"),
    action: str = typer.Option(None, "--action", help="Command to execute when changes are detected"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start monitoring immediately (default: yes)"),
    duration: int = typer.Option(None, "--duration", help="Monitoring duration in seconds"),
    interval: int = typer.Option(30, "--interval", help="Check interval in seconds"),
    background: bool = typer.Option(False, "--background", help="Run monitor in background"),
):
    """Add a generic action monitor and start monitoring"""
    from workflow.monitor import get_monitor, CLIActionMonitor
    from rich.console import Console
    console = Console(color_system=None)
    
    monitor = get_monitor()
    
    if name:
        # Named monitor - add to persistent configuration
        if monitor.add_cli_action_monitor(name, action_command, action=action):
            if start:
                monitor.start_monitoring(duration=duration, interval=interval)
            else:
                console.print(f"💡 Monitor '{name}' added. Use 'wf monitor start' to begin monitoring.")
    else:
        # Ephemeral monitor - create temporary monitor
        config = {}
        if action:
            # Check if action is a known action type
            from workflow.actions import ActionType
            try:
                action_type = ActionType(action)
                # This is an action type, create proper action config
                if action == 'notification' and 'github-comments' in action_command:
                    # Special handling for GitHub comment notifications
                    # Extract GitHub URL from the command
                    import re
                    github_url = ""
                    try:
                        match = re.search(r'github-comments\s+([^#\s]+)#(\d+)', action_command)
                        if match:
                            owner_repo = match.group(1)
                            pr_number = match.group(2)
                            github_url = f"https://github.com/{owner_repo}/pull/{pr_number}"
                    except:
                        pass
                    
                    config = {
                        'actions': [{
                            'id': f'ephemeral_{action}',
                            'type': action,
                            'title': 'New GitHub Comment',
                            'message': '${comment_author} commented: ${comment_body}',
                            'url': github_url
                        }]
                    }
                else:
                    # Generic action config
                    config = {
                        'actions': [{
                            'id': f'ephemeral_{action}',
                            'type': action,
                            'title': 'CLI Action Change Detected',
                            'message': f'Command: {action_command}'
                        }]
                    }
                console.print(f"🎯 Created action config: {config}")
            except ValueError:
                # Not an action type, treat as legacy shell command
                config = {'action': action}
                console.print(f"🔧 Created legacy action config: {config}")
        
        temp_monitor = CLIActionMonitor("ephemeral_action", action_command, config)
        
        if start:
            monitor._run_ephemeral_monitor(temp_monitor, "ephemeral-action", duration=duration, interval=interval, background=background)
        else:
            console.print(f"💡 Ephemeral action monitor created for 'wf {action_command}'. Use --start to begin monitoring.")

@monitor_app.command("remove")
def remove_monitor(
    name: str = typer.Argument(..., help="Monitor name to remove")
):
    """Remove a monitor"""
    from workflow.monitor import get_monitor
    
    monitor = get_monitor()
    monitor.remove_monitor(name)

@monitor_app.command("list")
def list_monitors():
    """List all configured monitors"""
    from workflow.monitor import get_monitor
    
    monitor = get_monitor()
    monitor.list_monitors()

@monitor_app.command("logs")
def show_monitor_logs(
    lines: int = typer.Option(50, help="Number of log lines to show"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    clear: bool = typer.Option(False, "--clear", help="Clear monitor logs")
):
    """Show monitor logs"""
    from pathlib import Path
    
    log_file = Path.home() / '.wf' / 'logs' / 'monitor.log'
    
    if clear:
        if log_file.exists():
            log_file.unlink()
            typer.echo("✅ Monitor logs cleared")
        else:
            typer.echo("No monitor logs to clear")
        return
    
    if not log_file.exists():
        typer.echo("No monitor logs found. Start monitoring first.")
        return
    
    try:
        if follow:
            import subprocess
            typer.echo(f"Following monitor logs: {log_file}")
            typer.echo("Press Ctrl+C to stop following")
            subprocess.run(['tail', '-f', str(log_file)], check=False)
        else:
            with open(log_file, 'r') as f:
                all_lines = f.readlines()
                if all_lines:
                    # Show last N lines
                    recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                    typer.echo(f"Monitor logs ({len(recent_lines)} of {len(all_lines)} lines):")
                    typer.echo("-" * 80)
                    for line in recent_lines:
                        # Remove timestamp and logger name for cleaner output
                        if ' - workflow_monitor - INFO - [' in line:
                            log_content = line.split(' - workflow_monitor - INFO - ', 1)[1]
                            typer.echo(f"📋 {log_content.strip()}")
                        elif ' - workflow_monitor - ERROR - [' in line:
                            log_content = line.split(' - workflow_monitor - ERROR - ', 1)[1]
                            typer.echo(f"❌ {log_content.strip()}")
                        else:
                            typer.echo(line.strip())
                else:
                    typer.echo("Monitor log file is empty.")
    except Exception as e:
        typer.echo(f"Error reading logs: {e}")

@monitor_app.command("env-vars")
def show_env_vars():
    """Show available environment variables for actions"""
    typer.echo("📋 Available environment variables for monitor actions:")
    typer.echo("")
    typer.echo("🔧 General Variables:")
    typer.echo("  MONITOR_NAME        - Name of the monitor that detected the change")
    typer.echo("  MONITOR_CHANGE_TYPE - Type of change (created, modified, deleted, new_commit, data_change, error)")
    typer.echo("  MONITOR_TIMESTAMP    - ISO timestamp of when the change was detected")
    typer.echo("  MONITOR_LOG_MESSAGE - Log message for the change")
    typer.echo("")
    typer.echo("📁 File Monitor Variables:")
    typer.echo("  MONITOR_FILE_PATH    - Path of the file that changed")
    typer.echo("  MONITOR_FILE_SIZE    - Size of the file in bytes")
    typer.echo("")
    typer.echo("🔧 GitHub Monitor Variables:")
    typer.echo("  MONITOR_COMMIT_AUTHOR - Author of the commit")
    typer.echo("  MONITOR_COMMIT_MESSAGE - Full commit message")
    typer.echo("  MONITOR_COMMIT_SHA    - Commit SHA (short)")
    typer.echo("")
    typer.echo("🌐 API Monitor Variables:")
    typer.echo("  MONITOR_API_NAME     - Name of the API monitor")
    typer.echo("  MONITOR_API_DATA     - JSON data that changed (as string)")
    typer.echo("")
    typer.echo("🌐 API Monitor Variables:")
    typer.echo("  MONITOR_API_NAME     - Name of the API monitor")
    typer.echo("  MONITOR_API_DATA     - JSON data that changed (as string)")
    typer.echo("")
    typer.echo("⚡ CLI Action Monitor Variables:")
    typer.echo("  MONITOR_CLI_COMMAND  - The CLI command that was executed")
    typer.echo("  MONITOR_CLI_OUTPUT   - Full output from the CLI command")
    typer.echo("  MONITOR_CLI_TIMESTAMP - Timestamp when the CLI command was executed")
    typer.echo("")
    typer.echo("  📋 GitHub Comments Specific:")
    typer.echo("  MONITOR_GITHUB_COMMENTS - Full GitHub comments output")
    typer.echo("  MONITOR_COMMENT_COUNT   - Number of comments found")
    typer.echo("")
    typer.echo("  📋 Status Command Specific:")
    typer.echo("  MONITOR_STATUS_OUTPUT - Full status command output")
    typer.echo("  MONITOR_TASK_KEY      - Current task key (if available)")
    typer.echo("")
    typer.echo("❌ Error Variables:")
    typer.echo("  MONITOR_ERROR_MESSAGE - Error message details")
    typer.echo("")
    typer.echo("💡 Example usage in actions:")
    typer.echo('  echo "File $MONITOR_FILE_PATH was $MONITOR_CHANGE_TYPE ($MONITOR_FILE_SIZE bytes)"')
    typer.echo('  curl -X POST https://hooks.slack.com/YOUR_WEBHOOK -d \'{"text":"$MONITOR_LOG_MESSAGE"}\'')
    typer.echo('  git commit -am "Auto-commit: $MONITOR_COMMIT_MESSAGE by $MONITOR_COMMIT_AUTHOR"')

@monitor_app.command("start")
def start_monitoring(
    duration: int = typer.Option(None, help="Duration in seconds (optional - runs indefinitely if not set)"),
    interval: int = typer.Option(30, help="Check interval in seconds"),
    list_only: bool = typer.Option(False, "--list-only", help="Show configured monitors and exit")
):
    """Start monitoring all configured monitors"""
    from workflow.monitor import get_monitor
    
    monitor = get_monitor()
    
    # If list-only, just show monitors and exit
    if list_only:
        monitor.list_monitors()
        return
    
    if not monitor.monitors:
        typer.echo("❌ No monitors configured. Use 'wf monitor add-*' commands to add monitors.")
        typer.echo("💡 Tip: Use 'wf monitor add-file /path/to/dir' to quickly add and start monitoring")
        return
    
    if duration:
        typer.echo(f"🔍 Starting monitoring for {duration} seconds (interval: {interval}s)")
    else:
        typer.echo(f"🔍 Starting monitoring indefinitely (interval: {interval}s)")
        typer.echo("💡 Press Ctrl+C to stop monitoring")
    
    monitor.start_monitoring(duration, interval)

# Create action subcommand group
action_app = typer.Typer(help="Action management for monitors", no_args_is_help=True)
monitor_app.add_typer(action_app, name="action")

@action_app.command("add-github")
def add_github_with_actions(
    pr_identifier: str = typer.Argument(..., help="PR identifier (owner/repo#123)"),
    name: str = typer.Option(..., "--name", help="Monitor name"),
    notify: bool = typer.Option(False, "--notify", help="Add notification action"),
    ai_console: bool = typer.Option(False, "--ai-console", help="Open AI console on changes"),
    terminal: bool = typer.Option(False, "--terminal", help="Open terminal tabs on changes"),
    notification_title: str = typer.Option("PR Update: {{commit_author}} pushed", "--notification-title", help="Notification title template"),
    notification_message: str = typer.Option("{{commit_message}}", "--notification-message", help="Notification message template"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start monitoring immediately"),
    interval: int = typer.Option(30, "--interval", help="Check interval in seconds")
):
    """Add GitHub PR monitor with action-based configuration"""
    from workflow.monitor import get_monitor
    import json
    
    # Parse PR identifier
    if '#' not in pr_identifier or '/' not in pr_identifier:
        typer.echo("❌ Invalid PR identifier format. Use: owner/repo#123")
        return
    
    repo_part, pr_part = pr_identifier.split('#')
    if '/' not in repo_part:
        typer.echo("❌ Invalid PR identifier format. Use: owner/repo#123")
        return
    
    owner, repo = repo_part.split('/', 1)
    try:
        pr_number = int(pr_part)
    except ValueError:
        typer.echo("❌ Invalid PR number")
        return
    
    # Build actions configuration
    actions = []
    
    if notify:
        actions.append({
            "id": f"{name}_notification",
            "type": "notification",
            "title": notification_title,
            "message": notification_message,
            "response_options": ["View PR", "Open AI Console"] if ai_console else ["View PR"]
        })
    
    if ai_console:
        actions.append({
            "id": f"{name}_ai_console",
            "type": "open_ai_console"
        })
    
    if terminal:
        actions.append({
            "id": f"{name}_terminal",
            "type": "open_terminal"
        })
    
    if not actions:
        typer.echo("❌ No actions specified. Use --notify, --ai-console, or --terminal")
        return
    
    # Create monitor configuration
    monitor_config = {
        "name": name,
        "type": "github_pr",
        "owner": owner,
        "repo": repo,
        "pr_number": pr_number,
        "actions": actions
    }
    
    # Add monitor
    monitor = get_monitor()
    if hasattr(monitor, 'add_monitor_from_config'):
        if monitor.add_monitor_from_config(monitor_config):
            typer.echo(f"✅ GitHub PR monitor '{name}' added with {len(actions)} actions")
            
            if start:
                typer.echo(f"🔍 Starting monitoring for {name}")
                monitor.start_monitoring(duration=None, interval=interval)
            else:
                typer.echo(f"💡 Monitor '{name}' added. Use 'wf monitor start' to begin monitoring.")
        else:
            typer.echo(f"❌ Failed to add monitor '{name}'")
    else:
        typer.echo("❌ Monitor configuration from config not supported. Use JSON configuration method.")
        typer.echo(f"💡 Save this configuration to ~/.wf/monitors/{name}.json:")
        typer.echo(json.dumps(monitor_config, indent=2))

@action_app.command("list")
def list_actions():
    """List available action types and registered actions"""
    from workflow.actions import get_action_registry, ActionType
    
    registry = get_action_registry()
    
    typer.echo("🎯 Available Action Types:")
    typer.echo("")
    
    for action_type in ActionType:
        typer.echo(f"  {action_type.value}")
    
    typer.echo("")
    typer.echo("📋 Registered Actions:")
    typer.echo("")
    
    actions = registry.list_actions()
    if actions:
        for action in actions:
            typer.echo(f"  {action['action_id']} ({action['type']})")
    else:
        typer.echo("  No actions registered")
    
    typer.echo("")
    typer.echo("💡 Example usage:")
    typer.echo("  wf monitor action add-github anomalyco/opencode#123 --name my_pr --notify --ai-console")

@action_app.command("test")
def test_action(
    action_type: str = typer.Argument(..., help="Action type to test"),
    context: str = typer.Option("{}", "--context", help="JSON context for action")
):
    """Test an action with sample data"""
    from workflow.actions import get_action_registry, ActionType
    import json
    
    try:
        action_type_enum = ActionType(action_type)
    except ValueError:
        typer.echo(f"❌ Unknown action type: {action_type}")
        typer.echo("💡 Available types: " + ", ".join([t.value for t in ActionType]))
        return
    
    try:
        context_dict = json.loads(context)
    except json.JSONDecodeError:
        typer.echo("❌ Invalid JSON context")
        return
    
    registry = get_action_registry()
    test_action_id = f"test_{action_type}"
    action = registry.create_action(test_action_id, action_type_enum)
    
    if not action:
        typer.echo(f"❌ Failed to create action: {action_type}")
        return
    
    # Add sample context for testing
    if action_type == "notification":
        context_dict.update({
            "title": "Test Notification",
            "message": "This is a test notification from the action framework"
        })
    elif action_type == "start_task":
        context_dict.update({
            "task_key": "TEST-123",
            "task_title": "Test Task"
        })
    
    typer.echo(f"🧪 Testing action: {action_type}")
    typer.echo(f"📋 Context: {json.dumps(context_dict, indent=2)}")
    typer.echo("")
    
    result = action.execute(context_dict)
    
    if result.get('success', False):
        typer.echo("✅ Action executed successfully")
        if result.get('stdout'):
            typer.echo(f"📤 Output: {result['stdout']}")
    else:
        typer.echo("❌ Action failed")
        error = result.get('error') or result.get('stderr')
        if error:
            typer.echo(f"📤 Error: {error}")
    
    # Cleanup
    if hasattr(registry, 'actions') and test_action_id in registry.actions:
        del registry.actions[test_action_id]

@monitor_app.command("status")
def check_watchdog_status():
    """Check if watchdog monitoring is currently running"""
    import subprocess
    import sys
    from pathlib import Path
    
    # Get the path to the watchdog status script
    script_path = Path(__file__).parent.parent / "wf_watchdog_status.py"
    
    if script_path.exists():
        try:
            # Run the status checker
            result = subprocess.run([sys.executable, str(script_path)], 
                                  capture_output=False, text=True)
        except KeyboardInterrupt:
            console.print("\n🛑 Status check cancelled")
        except Exception as e:
            console.print(f"❌ Error checking status: {e}")
    else:
        console.print("❌ Watchdog status script not found")
        console.print(f"Expected at: {script_path}")

@monitor_app.command("watchdog-status")
def check_watchdog_status_alt():
    """Alias for 'status' command"""
    check_watchdog_status()

# Add monitor subcommand to main app
app.add_typer(monitor_app, name="monitor")

@app.command()
def desktop_notify(
    title: str = typer.Argument("Test Notification", help="Notification title"),
    message: str = typer.Argument("This is a test notification from workflow", help="Notification message"),
    interactive: bool = typer.Option(True, help="Show interactive buttons")
):
    """Send a desktop notification with inline response support"""
    def handle_response(response: str):
        typer.echo(f"📝 Notification response: {response}")
        
        if response == "Open Workflow":
            typer.echo("🚀 Opening workflow...")
            # Could add more sophisticated handling here
        elif response == "View Details":
            current_task = get_current_task()
            if current_task:
                typer.echo(f"📋 Current task: {current_task.key} - {current_task.title}")
            else:
                typer.echo("📋 No current task set")
        elif response == "Dismiss":
            typer.echo("👋 Notification dismissed")
    
    if interactive:
        response_options = ["Open Workflow", "View Details", "Dismiss"]
        send_notification(title, message, response_options, handle_response)
        typer.echo(f"✅ Interactive notification sent: {title}")
    else:
        send_notification(title, message)
        typer.echo(f"✅ Basic notification sent: {title}")


@app.command("github-comments")
def get_github_comments(
    pr_identifier: str = typer.Argument(..., help="PR identifier (owner/repo#123)"),
    limit: int = typer.Option(10, "--limit", help="Maximum number of comments to fetch"),
    since: int = typer.Option(None, "--since", help="Fetch comments since this comment ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON")
):
    """Lookup GitHub PR comments directly without monitoring"""
    from workflow.github_comment_action import lookup_github_comments
    
    # Parse PR identifier
    if '#' not in pr_identifier or '/' not in pr_identifier:
        typer.echo("❌ Invalid PR identifier format. Use: owner/repo#123")
        return
    
    repo_part, pr_part = pr_identifier.split('#')
    if '/' not in repo_part:
        typer.echo("❌ Invalid PR identifier format. Use: owner/repo#123")
        return
    
    owner, repo = repo_part.split('/', 1)
    
    try:
        pr_number = int(pr_part)
    except ValueError:
        typer.echo("❌ Invalid PR number")
        return
    
    # Lookup comments
    result = lookup_github_comments(owner, repo, pr_number, limit, since)
    
    if json_output:
        import json
        typer.echo(json.dumps(result, indent=2))
    else:
        if result['success']:
            typer.echo(f"💬 Found {result['count']} comments for {owner}/{repo}#{pr_number}:")
            typer.echo("-" * 60)
            
            for i, comment in enumerate(result['comments'], 1):
                typer.echo(f"{i}. [cyan]@{comment['author']}[/cyan] on {comment['created_at']}")
                body_preview = comment['body'][:150] + '...' if len(comment['body']) > 150 else comment['body']
                typer.echo(f"   {body_preview}")
                typer.echo(f"   🔗 {comment['url']}")
                typer.echo("")
        else:
            typer.echo(f"❌ Error: {result['error']}")


# Task management CLI commands

def get_markdown_backend():
    """Get the Markdown backend instance"""
    from workflow.backends import get_markdown_backend as _get_md_backend
    cfg = load_config()
    return _get_md_backend(cfg)


def autocomplete_tasks(ctx, args, incomplete: str):
    """Autocomplete task keys"""
    try:
        backend = get_markdown_backend()
        tasks = backend.list_tasks()
        return [t.key for t in tasks if t.key.startswith(incomplete.upper())]
    except:
        return []


# Create task subcommand group
task_app = typer.Typer(help="Task management with Markdown backend", no_args_is_help=True)
app.add_typer(task_app, name="task")


@task_app.command("create")
def task_create(
    title: str = typer.Argument(..., help="Task title"),
    description: str = typer.Option("", "--description", "-d", help="Task description"),
    task_type: str = typer.Option("Task", "--type", "-t", help="Task type (Task, Story, Bug, etc.)"),
    priority: str = typer.Option("Medium", "--priority", "-p", help="Priority (Low, Medium, High, Critical)"),
    labels: str = typer.Option(None, "--labels", "-l", help="Comma-separated labels"),
    template: str = typer.Option(None, "--template", help="Use a template (bug, feature, task)"),
    project: str = typer.Option(None, "--project", help="Project name (defaults to current project)")
):
    """Create a new task"""
    backend = get_markdown_backend()
    
    # Use template if specified
    if template:
        issue = backend.create_from_template(template, title, project)
        if issue:
            typer.echo(f"✅ Created task: {issue.key} (from template: {template})")
            typer.echo(f"   Title: {title}")
            return
        else:
            typer.echo(f"❌ Template '{template}' not found")
            return
    
    # Create task with custom fields
    labels_list = [l.strip() for l in labels.split(",") if l.strip()] if labels else []
    
    from workflow.projects import get_current_project
    current_project = project or get_current_project()
    
    issue = backend.create_issue(
        summary=title,
        description=description,
        issue_type=task_type,
        project_key=current_project
    )
    
    # Update with additional fields
    if labels_list or priority != "Medium":
        backend.update_task(issue.key, {
            "labels": labels_list,
            "priority": priority
        })
    
    typer.echo(f"✅ Created task: {issue.key}")
    typer.echo(f"   Title: {title}")
    typer.echo(f"   Type: {task_type}")
    typer.echo(f"   Priority: {priority}")


@task_app.command("list")
def task_list(
    status: str = typer.Option(None, "--status", "-s", help="Filter by status"),
    project: str = typer.Option(None, "--project", "-p", help="Project name"),
    all_projects: bool = typer.Option(False, "--all", "-a", help="Show tasks from all projects"),
    on: str = typer.Option(None, "--on", help="Backend to use (jira or markdown)")
):
    """List tasks with optional filtering"""
    from collections import defaultdict
    from workflow.projects import get_effective_config, get_current_project
    cfg = get_effective_config()
    backend_type = on or cfg.get('task_backend', 'jira')
    
    status_order = {'To Do': 0, 'Open': 0, 'In Progress': 1, 'In Review': 2, 'Verification': 3, 'Blocked': 4, 'Done': 5, 'Closed': 5, 'Resolved': 5}
    
    def sort_key(task):
        status_idx = status_order.get(task.status, 99)
        return (status_idx, task.key)
    
    if backend_type == 'jira':
        current_proj = get_current_project()
        if current_proj:
            typer.echo(f"📋 Pulling tasks from Jira based on current project: {current_proj}")
        else:
            typer.echo("📋 Pulling tasks from Jira (no project configured)")
        from workflow.backends import get_backend
        backend = get_backend(cfg, force_type='jira')
        if not backend:
            typer.echo("❌ Failed to connect to Jira. Please check your configuration.")
            return
        
        tasks = backend.list_tasks(project_name=project, status=status)
        
        if not tasks:
            if project:
                typer.echo(f"No tasks found in project '{project}'.")
            elif status:
                typer.echo(f"No tasks found with status '{status}'.")
            else:
                typer.echo("No tasks found.")
            return
        
        # For Jira, group by project key extracted from issue key (e.g., "PROJ" from "PROJ-123")
        from collections import defaultdict
        tasks_by_project = defaultdict(list)
        for task in tasks:
            proj = task.key.split('-')[0] if '-' in task.key else 'Unknown'
            tasks_by_project[proj].append(task)
        
        for proj, proj_tasks in sorted(tasks_by_project.items()):
            jira_url = cfg.get("jira", {}).get("url", "")
            
            typer.echo(f"\n📁 Project: {proj}")
            typer.echo("-" * 60)
            
            for task in sorted(proj_tasks, key=sort_key):
                status_emoji = {
                    "To Do": "⬜",
                    "In Progress": "🔄",
                    "In Review": "👀",
                    "Done": "✅"
                }.get(task.status, "⬜")
                
                typer.echo(f"  {status_emoji} {task.key}: {task.title}")
                if jira_url:
                    typer.echo(f"     Link: {jira_url.rstrip('/')}/browse/{task.key}")
                typer.echo(f"     Status: {task.status} | Type: {task.issue_type} | Priority: {task.priority}")
                if task.labels:
                    typer.echo(f"     Labels: {', '.join(task.labels)}")
                typer.echo("")
    else:
        # Markdown backend
        from workflow.backends import get_markdown_backend as _get_md_backend
        backend = _get_md_backend(cfg)
        
        if all_projects:
            from workflow.projects import get_current_project
            projects = backend.get_projects()
            tasks = []
            for proj in projects:
                tasks.extend(backend.list_tasks(project_name=proj))
        else:
            tasks = backend.list_tasks(project_name=project, status=status)
        
        if not tasks:
            if all_projects:
                typer.echo("No tasks found in any project.")
            elif project:
                typer.echo(f"No tasks found in project '{project}'.")
            elif status:
                typer.echo(f"No tasks found with status '{status}'.")
            else:
                typer.echo("No tasks found.")
            return
        
        # Group by project
        tasks_by_project = defaultdict(list)
        for task in tasks:
            proj = task.file_path.parent.name
            tasks_by_project[proj].append(task)
        
        for proj, proj_tasks in sorted(tasks_by_project.items()):
            typer.echo(f"\n📁 Project: {proj}")
            typer.echo("-" * 60)
            
            for task in sorted(proj_tasks, key=sort_key):
                status_emoji = {
                    "To Do": "⬜",
                    "In Progress": "🔄",
                    "In Review": "👀",
                    "Done": "✅"
                }.get(task.status, "⬜")
                
                typer.echo(f"  {status_emoji} {task.key}: {task.title}")
                typer.echo(f"     Status: {task.status} | Type: {task.issue_type} | Priority: {task.priority}")
                if task.labels:
                    typer.echo(f"     Labels: {', '.join(task.labels)}")
                typer.echo("")


@task_app.command("show")
def task_show(
    key: str = typer.Argument(..., autocompletion=autocomplete_tasks, help="Task key")
):
    """Show task details"""
    backend = get_markdown_backend()
    
    try:
        # Get full task data
        tasks = backend.list_tasks()
        task = None
        for t in tasks:
            if t.key.lower() == key.lower():
                task = t
                break
        
        if not task:
            typer.echo(f"❌ Task '{key}' not found")
            return
        
        typer.echo(f"\n📋 Task: {task.key}")
        typer.echo("=" * 60)
        typer.echo(f"Title: {task.title}")
        typer.echo(f"Status: {task.status}")
        typer.echo(f"Type: {task.issue_type}")
        typer.echo(f"Priority: {task.priority}")
        
        if task.assignee:
            typer.echo(f"Assignee: {task.assignee}")
        if task.reporter:
            typer.echo(f"Reporter: {task.reporter}")
        if task.labels:
            typer.echo(f"Labels: {', '.join(task.labels)}")
        if task.components:
            typer.echo(f"Components: {', '.join(task.components)}")
        if task.fix_versions:
            typer.echo(f"Fix Versions: {', '.join(task.fix_versions)}")
        if task.due_date:
            typer.echo(f"Due Date: {task.due_date}")
        
        typer.echo(f"Created: {task.created_date}")
        typer.echo(f"Updated: {task.updated_date}")
        typer.echo(f"File: {task.file_path}")
        
        if task.description:
            typer.echo(f"\nDescription:")
            typer.echo("-" * 60)
            typer.echo(task.description)
        
        # Show available transitions
        transitions = backend.get_available_transitions(task.key)
        if transitions:
            typer.echo(f"\nAvailable Transitions: {', '.join(transitions)}")
    
    except Exception as e:
        typer.echo(f"❌ Error: {e}")


@task_app.command("update")
def task_update(
    key: str = typer.Argument(..., autocompletion=autocomplete_tasks, help="Task key"),
    title: str = typer.Option(None, "--title", help="Update title"),
    description: str = typer.Option(None, "--description", "-d", help="Update description"),
    priority: str = typer.Option(None, "--priority", "-p", help="Update priority"),
    add_labels: str = typer.Option(None, "--add-labels", help="Add comma-separated labels"),
    remove_labels: str = typer.Option(None, "--remove-labels", help="Remove comma-separated labels")
):
    """Update a task"""
    backend = get_markdown_backend()
    
    updates = {}
    
    if title:
        updates["title"] = title
    if description:
        updates["description"] = description
    if priority:
        updates["priority"] = priority
    
    # Handle label updates
    if add_labels or remove_labels:
        # Get current task
        tasks = backend.list_tasks()
        task = None
        for t in tasks:
            if t.key.lower() == key.lower():
                task = t
                break
        
        if task:
            current_labels = set(task.labels)
            
            if add_labels:
                current_labels.update([l.strip() for l in add_labels.split(",") if l.strip()])
            
            if remove_labels:
                current_labels -= set([l.strip() for l in remove_labels.split(",") if l.strip()])
            
            updates["labels"] = list(current_labels)
    
    if updates:
        if backend.update_task(key, updates):
            typer.echo(f"✅ Updated task: {key}")
        else:
            typer.echo(f"❌ Task '{key}' not found")
    else:
        typer.echo("⚠️  No updates specified")


@task_app.command("transition")
def task_transition(
    key: str = typer.Argument(..., autocompletion=autocomplete_tasks, help="Task key"),
    status: str = typer.Argument(..., help="New status")
):
    """Transition a task to a new status"""
    backend = get_markdown_backend()
    
    # Check available transitions first
    transitions = backend.get_available_transitions(key)
    
    if transitions and status not in transitions:
        typer.echo(f"❌ Invalid transition. Available: {', '.join(transitions)}")
        return
    
    if backend.transition_task(key, status):
        typer.echo(f"✅ Transitioned {key} to: {status}")
    else:
        typer.echo(f"❌ Task '{key}' not found")


@task_app.command("delete")
def task_delete(
    key: str = typer.Argument(..., autocompletion=autocomplete_tasks, help="Task key"),
    force: bool = typer.Option(False, "--force", help="Skip confirmation")
):
    """Delete a task"""
    backend = get_markdown_backend()
    
    if not force:
        confirm = typer.confirm(f"Are you sure you want to delete {key}?")
        if not confirm:
            typer.echo("Cancelled")
            return
    
    if backend.delete_task(key):
        typer.echo(f"✅ Deleted task: {key}")
    else:
        typer.echo(f"❌ Task '{key}' not found")


@task_app.command("project")
def task_project(
    action: str = typer.Argument(..., help="Action: list, create, switch"),
    name: str = typer.Argument(None, help="Project name (for create/switch)")
):
    """Manage task projects"""
    backend = get_markdown_backend()
    
    if action == "list":
        projects = backend.get_projects()
        from workflow.projects import get_current_project
        current = get_current_project()
        
        if not projects:
            typer.echo("No task projects found.")
            return
        
        typer.echo("📁 Task Projects:")
        for proj in sorted(projects):
            marker = " → (current)" if proj.lower() == (current or "").lower() else ""
            typer.echo(f"  • {proj}{marker}")
    
    elif action == "create":
        if not name:
            typer.echo("❌ Project name required")
            return
        
        backend.create_project(name)
        typer.echo(f"✅ Created project: {name}")
    
    elif action == "switch":
        if not name:
            typer.echo("❌ Project name required")
            return
        
        from workflow.projects import set_current_project, get_project
        if get_project(name):
            set_current_project(name)
            typer.echo(f"✅ Switched to project: {name}")
        else:
            # Allow switching to a markdown-only project
            projects = backend.get_projects()
            if name.lower() in [p.lower() for p in projects]:
                typer.echo(f"✅ Switched to markdown project: {name}")
            else:
                typer.echo(f"❌ Project '{name}' not found")
    
    else:
        typer.echo(f"❌ Unknown action: {action}")
        typer.echo("Available actions: list, create, switch")


@task_app.command("template")
def task_template(
    action: str = typer.Argument(..., help="Action: list, add, show"),
    name: str = typer.Argument(None, help="Template name"),
    task_type: str = typer.Option("Task", "--type", help="Default task type"),
    priority: str = typer.Option("Medium", "--priority", help="Default priority"),
    labels: str = typer.Option(None, "--labels", help="Comma-separated default labels"),
    description: str = typer.Option(None, "--description", "-d", help="Description template")
):
    """Manage task templates"""
    backend = get_markdown_backend()
    
    if action == "list":
        templates = backend.get_task_templates()
        typer.echo("📝 Task Templates:")
        for tmpl_name, tmpl_data in templates.items():
            typer.echo(f"\n  {tmpl_name}:")
            typer.echo(f"    Type: {tmpl_data.get('type', 'Task')}")
            typer.echo(f"    Priority: {tmpl_data.get('priority', 'Medium')}")
            if tmpl_data.get('labels'):
                typer.echo(f"    Labels: {', '.join(tmpl_data['labels'])}")
    
    elif action == "add":
        if not name:
            typer.echo("❌ Template name required")
            return
        
        labels_list = [l.strip() for l in labels.split(",") if l.strip()] if labels else []
        
        template = {
            "type": task_type,
            "priority": priority,
            "labels": labels_list,
            "description_template": description or "## Description\n\n## Notes\n"
        }
        
        backend.save_task_template(name, template)
        typer.echo(f"✅ Added template: {name}")
    
    elif action == "show":
        if not name:
            typer.echo("❌ Template name required")
            return
        
        templates = backend.get_task_templates()
        if name in templates:
            tmpl = templates[name]
            typer.echo(f"\n📝 Template: {name}")
            typer.echo(f"Type: {tmpl.get('type', 'Task')}")
            typer.echo(f"Priority: {tmpl.get('priority', 'Medium')}")
            if tmpl.get('labels'):
                typer.echo(f"Labels: {', '.join(tmpl['labels'])}")
            if tmpl.get('description_template'):
                typer.echo(f"\nDescription Template:")
                typer.echo(tmpl['description_template'])
        else:
            typer.echo(f"❌ Template '{name}' not found")
    
    else:
        typer.echo(f"❌ Unknown action: {action}")
        typer.echo("Available actions: list, add, show")


@task_app.command("workflow")
def task_workflow(
    action: str = typer.Argument(..., help="Action: show, set-transition"),
    from_status: str = typer.Argument(None, help="From status (for set-transition)"),
    to_status: str = typer.Argument(None, help="To status (for set-transition)")
):
    """Manage task workflow"""
    backend = get_markdown_backend()
    
    if action == "show":
        workflow = backend.get_workflow()
        typer.echo("🔄 Workflow Transitions:")
        for status, transitions in workflow.items():
            typer.echo(f"  {status} → {', '.join(transitions)}")
    
    elif action == "set-transition":
        if not from_status or not to_status:
            typer.echo("❌ Both from and to status required")
            return
        
        workflow = backend.get_workflow()
        if from_status not in workflow:
            workflow[from_status] = []
        
        if to_status not in workflow[from_status]:
            workflow[from_status].append(to_status)
        
        backend.save_workflow(workflow)
        typer.echo(f"✅ Added transition: {from_status} → {to_status}")
    
    else:
        typer.echo(f"❌ Unknown action: {action}")
        typer.echo("Available actions: show, set-transition")


@task_app.command("edit")
def task_edit(
    key: str = typer.Argument(..., autocompletion=autocomplete_tasks, help="Task key")
):
    """Open task in default editor"""
    import os
    import subprocess
    
    backend = get_markdown_backend()
    
    # Find task file
    task_file = None
    for project_dir in backend.base_dir.iterdir():
        if not project_dir.is_dir():
            continue
        potential_file = project_dir / f"{key.lower()}.md"
        if potential_file.exists():
            task_file = potential_file
            break
    
    if not task_file:
        typer.echo(f"❌ Task '{key}' not found")
        return
    
    # Get editor
    editor = os.environ.get('EDITOR', 'code')
    
    try:
        subprocess.run([editor, str(task_file)], check=True)
        typer.echo(f"✅ Opened {key} in {editor}")
    except Exception as e:
        typer.echo(f"❌ Error opening editor: {e}")


@task_app.command("backend")
def task_backend(
    backend_type: str = typer.Argument(None, help="Backend to use (jira, markdown, or 'show' to see current)")
):
    """Switch between Jira and Markdown backends"""
    from workflow.config import load_config, save_config

    cfg = load_config()
    # Only use 'task_backend' config key
    current_backend = cfg.get('task_backend', 'jira')

    if backend_type is None or backend_type == "show":
        typer.echo(f"Current backend: {current_backend}")
        typer.echo(f"Available backends: jira, markdown")
        typer.echo(f"\n💡 To switch backends:")
        typer.echo(f"   wf task backend markdown")
        typer.echo(f"   wf task backend jira")
        return

    if backend_type not in ['jira', 'markdown']:
        typer.echo(f"❌ Unknown backend: {backend_type}")
        typer.echo(f"Available backends: jira, markdown")
        return

    # Use 'task_backend' key for consistency with get_backend
    cfg['task_backend'] = backend_type
    save_config(cfg)
    
    typer.echo(f"✅ Switched to {backend_type} backend")
    
    if backend_type == 'markdown':
        typer.echo(f"\n💡 Markdown backend stores tasks in: ~/.wf/tasks/")
        typer.echo(f"   Use 'wf task list' to see your tasks")
        typer.echo(f"   Use 'wf task create \"My Task\"' to create a task")


task_comment_app = typer.Typer(help="Task comment management", no_args_is_help=True)
task_app.add_typer(task_comment_app, name="comment")


def _get_task_backend():
    """Get the current task backend"""
    from workflow.config import load_effective_config
    cfg = load_effective_config()
    return get_backend(cfg)


@task_comment_app.command("list")
def task_comment_list(
    key: str = typer.Argument(..., help="Task key")
):
    """List comments on a task"""
    backend = _get_task_backend()
    
    if not hasattr(backend, 'get_comments'):
        typer.echo("❌ Comments are not supported by this backend")
        return
    
    comments = backend.get_comments(key)
    
    if not comments:
        typer.echo(f"No comments found on task {key}")
        return
    
    typer.echo(f"\n💬 Comments on {key}")
    typer.echo("=" * 60)
    
    for comment in comments:
        comment_id = comment.get('id', '?')
        author = comment.get('author', 'unknown')
        text = comment.get('text', '')
        created = comment.get('created', '')
        
        typer.echo(f"\n#{comment_id} - {author} ({created[:10]})")
        typer.echo(f"   {text}")


@task_comment_app.command("add")
def task_comment_add(
    key: str = typer.Argument(..., help="Task key"),
    text: str = typer.Argument(..., help="Comment text")
):
    """Add a comment to a task"""
    backend = _get_task_backend()
    
    if not hasattr(backend, 'add_comment'):
        typer.echo("❌ Comments are not supported by this backend")
        return
    
    import os
    author = os.environ.get('USER', 'unknown')
    
    result = backend.add_comment(key, text, author)
    
    if result:
        typer.echo(f"✅ Added comment #{result['id']} to task {key}")
    else:
        typer.echo(f"❌ Failed to add comment to task {key}")


@task_comment_app.command("edit")
def task_comment_edit(
    key: str = typer.Argument(..., help="Task key"),
    comment_id: int = typer.Argument(..., help="Comment ID to edit"),
    text: str = typer.Argument(..., help="New comment text")
):
    """Edit a comment on a task"""
    backend = _get_task_backend()
    
    if not hasattr(backend, 'update_comment'):
        typer.echo("❌ Comments are not supported by this backend")
        return
    
    result = backend.update_comment(key, comment_id, text)
    
    if result:
        typer.echo(f"✅ Updated comment #{comment_id} on task {key}")
    else:
        typer.echo(f"❌ Failed to update comment #{comment_id} on task {key}")


@task_comment_app.command("delete")
def task_comment_delete(
    key: str = typer.Argument(..., help="Task key"),
    comment_id: int = typer.Argument(..., help="Comment ID to delete")
):
    """Delete a comment from a task"""
    backend = _get_task_backend()
    
    if not hasattr(backend, 'delete_comment'):
        typer.echo("❌ Comments are not supported by this backend")
        return
    
    result = backend.delete_comment(key, comment_id)
    
    if result:
        typer.echo(f"✅ Deleted comment #{comment_id} from task {key}")
    else:
        typer.echo(f"❌ Failed to delete comment #{comment_id} from task {key}")


browser_app = typer.Typer(help="Browser automation commands", no_args_is_help=True)
app.add_typer(browser_app, name="browser")

doc_app = typer.Typer(help="Documentation management via Confluence API", no_args_is_help=True)
app.add_typer(doc_app, name="doc")

explore_app = typer.Typer(help="Explore and store project context for AI", no_args_is_help=True)
app.add_typer(explore_app, name="explore")

from commands.explore import explore as explore_cmd, list as explore_list, delete as explore_delete

@explore_app.command("explore", help="Explore a codebase and store context as an AI skill")
def explore_cmd_wrapper(
    path: str = typer.Option(None, "--path", "-p", help="Path to explore (default: current directory)"),
    name: str = typer.Option("explore", "--name", "-n", help="Name for the exploration skill"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing exploration"),
):
    explore_cmd(path=path, name=name, force=force)

@explore_app.command("list", help="List stored explorations")
def explore_list_wrapper():
    explore_list()

@explore_app.command("delete", help="Delete a stored exploration")
def explore_delete_wrapper(
    scope: str = typer.Argument(..., help="Scope: repo or project"),
    path_or_project: str = typer.Argument(..., help="Path or project name"),
    name: str = typer.Argument(..., help="Skill name to delete"),
):
    explore_delete(scope=scope, path_or_project=path_or_project, name=name)

# Shortcut: wf explore (without subcommand) runs explore on cwd
@explore_app.command("default", help="Explore current directory")
def explore_default(
    path: str = typer.Option(None, "--path", "-p", help="Path to explore (default: current directory)"),
    name: str = typer.Option("explore", "--name", "-n", help="Name for the exploration skill"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing exploration"),
):
    explore_cmd(path=path, name=name, force=force)


@doc_app.command("search")
def doc_search(
    query: str = typer.Argument(..., help="Search query"),
    space: str = typer.Option(None, "--space", "-s", help="Limit to space key"),
):
    """Search documentation pages"""
    from workflow.confluence import get_confluence_client
    
    try:
        client = get_confluence_client()
        results = client.search_pages(query, space)
        
        if not results:
            typer.echo("No results found.")
            return
        
        typer.echo(f"Found {len(results)} page(s):\n")
        for r in results:
            title = r.get("title", "Untitled")
            # Use webui link from result
            webui = r.get("_links", {}).get("webui", "")
            if webui:
                url = client.base_url + webui
            else:
                content = r.get("content", {})
                page_id = content.get("id", "") if content else r.get("id", "")
                base = client.base_url.rstrip("/")
                # Prefer the space the page actually lives in; fall back to a
                # space-less page link, which Confluence redirects correctly.
                space_key = (
                    (content.get("space", {}) or {}).get("key")
                    if content else None
                ) or (r.get("space", {}) or {}).get("key") or space
                if space_key:
                    import urllib.parse
                    encoded_title = urllib.parse.quote(title, safe='')
                    url = f"{base}/spaces/{space_key}/pages/{page_id}/{encoded_title}"
                else:
                    url = f"{base}/pages/{page_id}"
            typer.echo(f"  • {title}")
            typer.echo(f"    {url}")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")
        raise typer.Exit(1)


@doc_app.command("list")
def doc_list(
    space: str = typer.Argument(..., help="Space key"),
    limit: int = typer.Option(25, "--limit", "-l", help="Max results"),
):
    """List pages in a space"""
    from workflow.confluence import get_confluence_client
    
    try:
        client = get_confluence_client()
        results = client.search(f"space.key='{space}' AND type=page", limit)
        
        if not results:
            typer.echo(f"No pages found in space {space}")
            return
        
        typer.echo(f"Pages in {space}:\n")
        for r in results:
            title = r.get("title", "Untitled")
            content = r.get("content", {})
            page_id = content.get("id", "") if content else r.get("id", "")
            base = client.base_url.rstrip("/")
            import urllib.parse
            encoded_title = urllib.parse.quote(title, safe='')
            url = base + "/pages/" + str(page_id) + "/" + encoded_title
            typer.echo(f"  • {title}")
            typer.echo(f"    {url}")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")
        raise typer.Exit(1)


@doc_app.command("view")
def doc_view(
    page_id: str = typer.Argument(..., help="Page ID or title (space:title)"),
):
    """View a documentation page"""
    from workflow.confluence import get_confluence_client
    
    try:
        client = get_confluence_client()
        
        # Check if it's "space:title" format
        if ":" in page_id and not page_id.isdigit():
            space_key, title = page_id.split(":", 1)
            page = client.get_page_by_title(space_key, title)
            if not page:
                typer.echo(f"❌ Page '{title}' not found in space {space_key}")
                raise typer.Exit(1)
            page_id = page["id"]
        
        page = client.get_page(page_id)
        
        title = page.get("title", "Untitled")
        content = page.get("body", {}).get("storage", {}).get("value", "")
        version = page.get("version", {}).get("number", 1)
        
        typer.echo(f"\n{'='*60}")
        typer.echo(f"{title}")
        typer.echo(f"{'='*60}")
        typer.echo(f"Page ID: {page_id}")
        typer.echo(f"Version: {version}")
        typer.echo(f"\n{content}\n")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")
        raise typer.Exit(1)


@doc_app.command("create")
def doc_create(
    title: str = typer.Argument(..., help="Page title"),
    space: str = typer.Option(..., "--space", "-s", help="Space key"),
    parent: str = typer.Option(None, "--parent", "-p", help="Parent page ID"),
    content: str = typer.Option("", "--content", "-c", help="Page content (HTML)"),
):
    """Create a new documentation page"""
    from workflow.confluence import get_confluence_client
    
    try:
        client = get_confluence_client()
        result = client.create_page(space, title, content, parent)
        
        page_id = result.get("id")
        base = client.base_url.rstrip("/")
        import urllib.parse
        encoded_title = urllib.parse.quote(title, safe='')
        url = base + "/pages/" + str(page_id) + "/" + encoded_title
        
        typer.echo(f"✅ Created page: {title}")
        typer.echo(f"   {url}")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")
        raise typer.Exit(1)


@doc_app.command("edit")
def doc_edit(
    page_id: str = typer.Argument(..., help="Page ID or title (space:title)"),
    content: str = typer.Option(..., "--content", "-c", help="New content (HTML)"),
    title: str = typer.Option(None, "--title", "-t", help="New title"),
    minor: bool = typer.Option(False, "--minor", help="Minor edit (don't notify)"),
):
    """Edit a documentation page"""
    from workflow.confluence import get_confluence_client
    
    try:
        client = get_confluence_client()
        
        # Check if it's "space:title" format
        if ":" in page_id and not page_id.isdigit():
            space_key, title_hint = page_id.split(":", 1)
            page = client.get_page_by_title(space_key, title_hint)
            if not page:
                typer.echo(f"❌ Page '{title_hint}' not found in space {space_key}")
                raise typer.Exit(1)
            page_id = page["id"]
        
        result = client.update_page(page_id, title, content, minor)
        
        typer.echo(f"✅ Updated page")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")
        raise typer.Exit(1)


@doc_app.command("comment")
def doc_comment(
    page_id: str = typer.Argument(..., help="Page ID or title (space:title)"),
    comment: str = typer.Argument(..., help="Comment content (HTML)"),
):
    """Add a comment to a page"""
    from workflow.confluence import get_confluence_client
    
    try:
        client = get_confluence_client()
        
        # Check if it's "space:title" format
        if ":" in page_id and not page_id.isdigit():
            space_key, title_hint = page_id.split(":", 1)
            page = client.get_page_by_title(space_key, title_hint)
            if not page:
                typer.echo(f"❌ Page '{title_hint}' not found in space {space_key}")
                raise typer.Exit(1)
            page_id = page["id"]
        
        result = client.add_comment(page_id, comment)
        
        typer.echo(f"✅ Added comment to page")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")
        raise typer.Exit(1)


@doc_app.command("children")
def doc_children(
    page_id: str = typer.Argument(..., help="Page ID or title (space:title)"),
):
    """List child pages"""
    from workflow.confluence import get_confluence_client
    
    try:
        client = get_confluence_client()
        
        # Check if it's "space:title" format
        if ":" in page_id and not page_id.isdigit():
            space_key, title_hint = page_id.split(":", 1)
            page = client.get_page_by_title(space_key, title_hint)
            if not page:
                typer.echo(f"❌ Page '{title_hint}' not found in space {space_key}")
                raise typer.Exit(1)
            page_id = page["id"]
        
        children = client.get_child_pages(page_id)
        
        if not children:
            typer.echo("No child pages")
            return
        
        typer.echo(f"Child pages:\n")
        for c in children:
            title = c.get("title", "Untitled")
            child_id = c.get("id", "")
            base = client.base_url.rstrip("/")
            import urllib.parse
            encoded_title = urllib.parse.quote(title, safe='')
            url = base + "/pages/" + str(child_id) + "/" + encoded_title
            typer.echo(f"  • {title}")
            typer.echo(f"    {url}")
    except Exception as e:
        typer.echo(f"❌ Error: {e}")
        raise typer.Exit(1)


@browser_app.command("connect")
def browser_connect():
    """Start the browser screenshot daemon"""
    from workflow.browser_daemon import start_daemon, is_daemon_running, load_daemon_state
    
    if is_daemon_running():
        state = load_daemon_state()
        typer.echo(f"✅ Daemon already running on port {state.get('port')}")
        return
    
    import sys
    import os
    
    daemon_script = os.path.join(os.path.dirname(__file__), "browser_daemon.py")
    
    subprocess.Popen(
        [sys.executable, daemon_script, "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    
    time.sleep(2)
    
    state = load_daemon_state()
    if state:
        typer.echo(f"✅ Daemon started with PID: {state.get('pid')}")
        typer.echo(f"📡 Listening on port: {state.get('port')}")
    else:
        typer.echo("❌ Failed to start daemon")


@browser_app.command("close")
def browser_close():
    """Close the browser page and stop the daemon"""
    import json
    import urllib.request
    import os
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/close",
            method="DELETE"
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())
    except:
        pass
    
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-t", "-i", ":18765"],
            capture_output=True,
            text=True
        )
        if result.stdout:
            for pid in result.stdout.strip().split("\n"):
                if pid:
                    try:
                        os.kill(int(pid), 9)
                    except:
                        pass
    except:
        pass
    
    try:
        subprocess.run(
            ["pkill", "-f", "chrome-devtools-mcp"],
            capture_output=True
        )
    except:
        pass
    
    try:
        subprocess.run(
            ["pkill", "-f", "chrome-devtools-mcp/chrome-profile"],
            capture_output=True
        )
    except:
        pass
    
    state_file = os.path.expanduser("~/.wf/browser/state")
    try:
        if os.path.exists(state_file):
            os.remove(state_file)
    except:
        pass
    
    typer.echo("✅ Daemon stopped")


@browser_app.command("status")
def browser_status():
    """Check daemon status"""
    from workflow.browser_daemon import get_status
    
    status = get_status()
    if status.get("running"):
        typer.echo(f"✅ Daemon running on port {status.get('port')}")
    else:
        typer.echo("❌ Daemon not running")


@browser_app.command("open")
def browser_open(url: str = typer.Argument(..., help="URL to open")):
    """Open a URL using the daemon server"""
    import json
    import urllib.request
    import socket
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    daemon_running = False
    try:
        sock.connect(("127.0.0.1", 18765))
        sock.close()
        daemon_running = True
    except:
        pass
    
    if not daemon_running:
        import subprocess
        import time
        
        typer.echo("Starting daemon...")
        subprocess.Popen(
            ["python", "-m", "workflow.browser_daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        time.sleep(1)
        
        for _ in range(10):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18765/health", timeout=2):
                    break
            except:
                time.sleep(0.5)
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/open",
            data=json.dumps({"url": url}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            if result.get("status") == "ok":
                typer.echo(f"✅ Opened: {url}")
            else:
                typer.echo(f"Response: {result}")
    except Exception as e:
        typer.echo(f"❌ Failed to open URL: {e}")
        raise typer.Exit(1)


@browser_app.command("screenshot")
def browser_screenshot(
    output: str = typer.Option("screenshot.png", "--output", "-o", help="Output file path"),
    wait: int = typer.Option(5, "--wait", "-w", help="Seconds to wait after taking screenshot"),
):
    """Take a screenshot using the daemon server"""
    import json
    import urllib.request
    import socket
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    daemon_running = False
    try:
        sock.connect(("127.0.0.1", 18765))
        sock.close()
        daemon_running = True
    except:
        pass
    
    if not daemon_running:
        import subprocess
        import time
        
        typer.echo("Starting daemon...")
        subprocess.Popen(
            ["python", "-m", "workflow.browser_daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        time.sleep(1)
        
        for _ in range(10):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18765/health", timeout=2):
                    break
            except:
                time.sleep(0.5)
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/screenshot",
            data=json.dumps({"output": output, "wait": wait}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=90) as response:
            result = json.loads(response.read().decode())
            if result.get("saved_path"):
                typer.echo(f"📸 Screenshot saved to: {result.get('saved_path')}")
            else:
                typer.echo(f"Response: {result}")
    except Exception as e:
        typer.echo(f"❌ Failed to take screenshot: {e}")
        raise typer.Exit(1)


@browser_app.command("pages")
def browser_pages():
    """List all open pages"""
    import json
    import urllib.request
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/pages",
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            pages = result.get("pages", [])
            if pages:
                for i, page in enumerate(pages, 1):
                    typer.echo(f"{i}. {page.get('title', 'Unknown')} - {page.get('url', 'Unknown')}")
            else:
                typer.echo("No pages open")
    except Exception as e:
        typer.echo(f"❌ Failed to list pages: {e}")
        raise typer.Exit(1)


@browser_app.command("snapshot")
def browser_snapshot():
    """Take a snapshot of the page"""
    import json
    import urllib.request
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/snapshot",
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            typer.echo("Snapshot taken")
    except Exception as e:
        typer.echo(f"❌ Failed to take snapshot: {e}")
        raise typer.Exit(1)


@browser_app.command("click")
def browser_click(uid: str = typer.Argument(..., help="Element uid to click")):
    """Click an element by uid"""
    import json
    import urllib.request
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/click",
            data=json.dumps({"uid": uid}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            typer.echo("✅ Clicked")
    except Exception as e:
        typer.echo(f"❌ Failed to click: {e}")
        raise typer.Exit(1)


@browser_app.command("fill")
def browser_fill(
    uid: str = typer.Argument(..., help="Element uid to fill"),
    value: str = typer.Argument(..., help="Value to fill"),
):
    """Fill an input element"""
    import json
    import urllib.request
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/fill",
            data=json.dumps({"uid": uid, "value": value}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            typer.echo("✅ Filled")
    except Exception as e:
        typer.echo(f"❌ Failed to fill: {e}")
        raise typer.Exit(1)


@browser_app.command("press")
def browser_press(key: str = typer.Argument(..., help="Key to press (e.g., Enter, Escape, Control+C)")):
    """Press a key"""
    import json
    import urllib.request
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/press",
            data=json.dumps({"key": key}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            typer.echo(f"✅ Pressed: {key}")
    except Exception as e:
        typer.echo(f"❌ Failed to press key: {e}")
        raise typer.Exit(1)


@browser_app.command("eval")
def browser_eval(
    script: str = typer.Argument(None, help="JavaScript to evaluate"),
    file: str = typer.Option(None, "--file", "-f", help="Read script from file"),
):
    """Evaluate JavaScript"""
    import json
    import urllib.request
    from pathlib import Path
    
    # Get script from file or argument
    if file:
        script = Path(file).read_text()
    elif not script:
        typer.echo("Error: either provide script as argument or use --file")
        raise typer.Exit(1)
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/eval",
            data=json.dumps({"script": script}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            if result.get("status") == "error":
                typer.echo(f"❌ Error: {result.get('error', 'Unknown error')}")
                raise typer.Exit(1)
            typer.echo(result.get("result", ""))
    except urllib.error.HTTPError as e:
        try:
            error_body = json.loads(e.read().decode())
            typer.echo(f"❌ Error: {error_body.get('error', e.reason)}")
        except:
            typer.echo(f"❌ Failed to eval: {e}")
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"❌ Failed to eval: {e}")
        raise typer.Exit(1)


@browser_app.command("wait")
def browser_wait(
    text: str = typer.Argument(..., help="Text to wait for"),
    timeout: int = typer.Option(30000, "--timeout", "-t", help="Timeout in milliseconds"),
):
    """Wait for text to appear"""
    import json
    import urllib.request
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/wait",
            data=json.dumps({"text": text, "timeout": timeout}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout/1000 + 10) as response:
            result = json.loads(response.read().decode())
            typer.echo("✅ Text found")
    except Exception as e:
        typer.echo(f"❌ Failed to wait: {e}")
        raise typer.Exit(1)


@browser_app.command("cookies")
def browser_cookies(
    domain: str = typer.Option("", "--domain", help="Filter cookies by domain")
):
    """List cookies for the current page"""
    import json
    import urllib.request
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/cookies",
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            cookies = result.get("cookies", [])
            
            if not cookies:
                typer.echo("No cookies found")
                return
            
            if domain:
                cookies = [c for c in cookies if domain.lower() in c.get('domain', '').lower()]
                if not cookies:
                    typer.echo(f"No cookies found for domain: {domain}")
                    return
            
            console = Console(color_system=None, width=200, soft_wrap=True)
            
            for c in cookies:
                console.print(f"\n[cyan]Name:[/cyan] {c.get('name', '')}")
                console.print(f"[magenta]Domain:[/magenta] {c.get('domain', '')}")
                console.print(f"[blue]Path:[/blue] {c.get('path', '')}")
                value = c.get('value', '')
                console.print("[green]Value:[/green] " + value)
                console.print(f"[yellow]HttpOnly:[/yellow] {'✓' if c.get('httpOnly') else '✗'}")
                console.print(f"[red]Secure:[/red] {'✓' if c.get('secure') else '✗'}")
                console.print("─" * 80)
            
            console.print(f"\nTotal: {len(cookies)} cookies")
            
    except Exception as e:
        typer.echo(f"❌ Failed to get cookies: {e}")
        raise typer.Exit(1)


@browser_app.command("capture")
def browser_capture(
    output: str = typer.Option("screenshot.png", "--output", "-o", help="Output file path"),
    wait: int = typer.Option(60, "--wait", "-w", help="Seconds to wait after capture"),
):
    """Trigger screenshot via daemon API"""
    from workflow.browser_daemon import trigger_screenshot, get_status, load_daemon_state
    
    state = load_daemon_state()
    if not state or not state.get("running"):
        typer.echo("❌ Daemon not running. Use 'wf browser connect' first.")
        raise typer.Exit(1)
    
    typer.echo(f"📸 Capturing screenshot...")
    result = trigger_screenshot(output=output, wait=wait)
    
    if result.get("saved_path"):
        typer.echo(f"✅ Screenshot saved to: {result.get('saved_path')}")
    else:
        typer.echo("⚠️  Screenshot may not have been saved")
        typer.echo(f"\nOutput: {result.get('output', '')[:200]}")


@app.command()
def plan():
    """Launch the TUI task board"""
    from workflow.tui import TaskBoardApp
    tui_app = TaskBoardApp()
    tui_app.run()


if __name__ == "__main__":
    import sys
    from pathlib import Path
    
    # Check if this might be an alias first
    if len(sys.argv) > 1:
        command = sys.argv[1]
        alias_manager = AliasManager()
        
        # Check if this is an alias
        if alias_manager.get_alias(command):
            # Get remaining arguments
            args = sys.argv[2:] if len(sys.argv) > 2 else []
            result = alias_manager.execute_alias(command, args, str(Path.cwd()))
            sys.exit(result)
    
    # If not an alias, run normal CLI
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    app()
