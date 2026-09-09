"""
Project management for workflow
"""

import os
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any
from .config import load_config, save_config
from .state import _load, _save


def get_project_for_cwd(switch_if_needed: bool = True, verbose: bool = False) -> Optional[str]:
    """
    Get the appropriate project based on the current working directory.
    
    Logic:
    1. If cwd is in current project's repos -> use current project
    2. If not, check if cwd is in any other project's repos -> switch to that project
    3. If not found in any project -> use current active project
    
    Args:
        switch_if_needed: If True, automatically switch to the matching project
        verbose: If True, print messages about project switching
        
    Returns:
        The project name to use
    """
    cwd = os.getcwd()
    current_project = get_current_project()
    all_projects = list_projects()
    
    if not all_projects:
        return current_project
    
    def cwd_in_project_repos(project_config: Dict[str, Any]) -> bool:
        """Check if cwd is in this project's repositories"""
        repos = project_config.get("repositories", {})
        for repo_path in repos:
            if cwd.startswith(repo_path) or Path(cwd).resolve() == Path(repo_path).resolve():
                return True
        return False
    
    # Check if cwd is in current project's repos
    if current_project and current_project in all_projects:
        current_config = all_projects[current_project]
        if cwd_in_project_repos(current_config):
            if verbose:
                print(f"Using current project: {current_project}")
            return current_project
    
    # Check all other projects for a match
    for project_name, project_config in all_projects.items():
        if project_name == current_project:
            continue
        if cwd_in_project_repos(project_config):
            if switch_if_needed:
                if verbose:
                    print(f"Switching to project: {project_name} (cwd matches repository)")
                set_current_project(project_name)
            return project_name
    
    # No match found, return current project
    if verbose and current_project:
        print(f"No project matches cwd, using current project: {current_project}")
    return current_project


def add_project(name: str, config: Dict[str, Any]) -> None:
    """Add a new project configuration"""
    cfg = load_config()
    
    if "projects" not in cfg:
        cfg["projects"] = {}
    
    if name in cfg["projects"]:
        raise ValueError(f"Project '{name}' already exists")
    
    cfg["projects"][name] = config
    save_config(cfg)


def remove_project(name: str) -> None:
    """Remove a project configuration"""
    cfg = load_config()
    
    if "projects" not in cfg or name not in cfg["projects"]:
        raise ValueError(f"Project '{name}' not found")
    
    del cfg["projects"][name]
    
    # If removing the current project, clear it
    current = get_current_project()
    if current == name:
        set_current_project(None)
    
    save_config(cfg)


def list_projects() -> Dict[str, Dict[str, Any]]:
    """List all projects"""
    cfg = load_config()
    return cfg.get("projects", {})


def get_project(name: str) -> Optional[Dict[str, Any]]:
    """Get a specific project configuration"""
    projects = list_projects()
    return projects.get(name)


def set_current_project(name: Optional[str]) -> None:
    """Set the current active project"""
    state = _load()
    if name is None:
        state.pop("current_project", None)
    else:
        # Verify project exists
        if get_project(name) is None:
            raise ValueError(f"Project '{name}' not found")
        state["current_project"] = name
    _save(state)


def get_current_project() -> Optional[str]:
    """Get the current active project name"""
    state = _load()
    return state.get("current_project")


def get_current_project_key() -> Optional[str]:
    """Get the Jira project key for the current project"""
    config = get_current_project_config()
    if config:
        return config.get("jira", {}).get("project")
    return None


def get_current_project_repositories() -> Dict[str, Any]:
    """Get the repositories for the current project (with auto-switch based on cwd)"""
    return get_effective_config(auto_switch=True).get("repositories", {})


def get_current_project_default_repo() -> Optional[str]:
    """Get the default repository for the current project"""
    config = get_current_project_config()
    if config:
        return config.get("default_repo")
    return None


def get_cwd_repo() -> Optional[str]:
    """
    Get the repository that matches the current working directory.
    Returns the repo path if cwd is inside a configured repo, None otherwise.
    Prefers the most specific match (longest path match).
    """
    cwd = os.getcwd()
    repos = get_current_project_repositories()
    
    best_match = None
    best_match_len = 0
    
    for repo_path in repos:
        # Check if cwd starts with repo_path or is exactly equal
        if cwd.startswith(repo_path) or Path(cwd).resolve() == Path(repo_path).resolve():
            # Prefer longer matches (more specific)
            if len(repo_path) > best_match_len:
                best_match = repo_path
                best_match_len = len(repo_path)
    
    return best_match


def get_current_project_config() -> Optional[Dict[str, Any]]:
    """Get the current active project configuration"""
    current = get_current_project()
    if current:
        return get_project(current)
    return None


def migrate_to_project_config() -> bool:
    """Migrate existing single-project config to project-based config"""
    cfg = load_config()
    
    # Skip if already has projects
    if "projects" in cfg and cfg["projects"]:
        return False
    
    # Check if this looks like a single-project config
    jira_config = cfg.get("jira")
    if not jira_config or not jira_config.get("project"):
        return False
    
    # Create project from existing config
    project_name = jira_config["project"].lower()
    repos = cfg.get("repositories", {})
    project_config = {
        "name": jira_config["project"],
        "jira": jira_config.copy(),
        "repositories": repos,
        "git_enabled": cfg.get("git_enabled", True),
        "github_enabled": cfg.get("github_enabled", False),
        "custom_fields": cfg.get("custom_fields", {}),
        "variables": cfg.get("variables", {}),
    }
    
    # If there's only one repo, set it as default
    if len(repos) == 1:
        project_config["default_repo"] = list(repos.keys())[0]
    
    # Add slack config if present
    if "slack" in cfg:
        project_config["slack"] = cfg["slack"]
    if "slack_webhook" in cfg:
        project_config["slack_webhook"] = cfg["slack_webhook"]
    
    # Initialize projects section
    cfg["projects"] = {project_name: project_config}
    
    # Set as current project
    set_current_project(project_name)
    
    # Remove project-specific fields from root config
    keys_to_remove = ["jira", "repositories", "custom_fields", "variables", "slack_webhook"]
    for key in keys_to_remove:
        cfg.pop(key, None)
    
    save_config(cfg)
    return True


def get_effective_config(auto_switch: bool = True) -> Dict[str, Any]:
    """
    Get the effective configuration, merging project config with global config.
    Project config takes precedence over global config.
    
    Args:
        auto_switch: If True, automatically switch to the project that matches 
                     the current working directory
    """
    cfg = load_config()
    
    # Check if we need to migrate first
    if "projects" not in cfg or not cfg["projects"]:
        return cfg
    
    # Get the appropriate project based on cwd
    if auto_switch:
        current_project = get_project_for_cwd(switch_if_needed=True, verbose=False)
    else:
        current_project = get_current_project()
    
    if not current_project:
        # No current project, return global config as-is
        return cfg
    
    project_config = get_project(current_project)
    if not project_config:
        return cfg
    
    # Start with global config, then overlay project config
    effective = cfg.copy()
    
    # Remove any project-specific keys from global config
    project_specific_keys = [
        "repositories", "custom_fields", "variables",
        "slack_webhook", "slack", "ai", "default_repo", "backend"
    ]
    for key in project_specific_keys:
        effective.pop(key, None)
    
    # Overlay project config
    effective.update(project_config)
    
    # For Jira config, merge global and project configs (project overrides global)
    if 'jira' in project_config:
        global_jira = cfg.get('jira', {})
        project_jira = project_config.get('jira', {})
        effective['jira'] = {**global_jira, **project_jira}
    
    return effective


def get_project_repositories(project_name: Optional[str] = None) -> Dict[str, Any]:
    """Get repositories for a specific project or current project"""
    if project_name:
        project = get_project(project_name)
        return project.get("repositories", {}) if project else {}
    else:
        effective_config = get_effective_config()
        return effective_config.get("repositories", {})


def set_default_repo(project_name: str, repo_path: str) -> None:
    """Set the default repository for a project"""
    cfg = load_config()
    
    if "projects" not in cfg or project_name not in cfg["projects"]:
        raise ValueError(f"Project '{project_name}' not found")
    
    # Verify the repository exists in the project
    project = cfg["projects"][project_name]
    repos = project.get("repositories", {})
    
    if repo_path not in repos:
        raise ValueError(f"Repository '{repo_path}' not found in project '{project_name}'")
    
    project["default_repo"] = repo_path
    save_config(cfg)


def get_default_repo(project_name: Optional[str] = None) -> Optional[str]:
    """Get the default repository for a project"""
    if project_name:
        project = get_project(project_name)
        if project:
            return project.get("default_repo")
        return None
    else:
        effective_config = get_effective_config()
        return effective_config.get("default_repo")