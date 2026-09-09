import typer
import os
import subprocess
from pathlib import Path
from workflow.config import load_config, save_config
from workflow.projects import get_current_project, get_cwd_repo

app = typer.Typer(help="Explore and store project context for AI")


def get_file_structure(root_path, max_depth=3, exclude_dirs=None):
    """Get a directory tree structure"""
    if exclude_dirs is None:
        exclude_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', 'target', 'dist', 'build', '.next', '.nuxt', 'vendor', 'bin', 'obj'}
    
    structure = []
    
    def walk_dir(path, prefix="", depth=0):
        if depth > max_depth:
            return
        
        try:
            items = sorted(Path(path).iterdir(), key=lambda x: (not x.is_dir(), x.name))
        except PermissionError:
            return
        
        dirs = []
        files = []
        
        for item in items:
            if item.name in exclude_dirs or item.name.startswith('.'):
                continue
            if item.is_dir():
                dirs.append(item)
            else:
                files.append(item)
        
        for i, d in enumerate(dirs):
            is_last = (i == len(dirs) - 1 and len(files) == 0)
            connector = "└── " if is_last else "├── "
            structure.append(f"{prefix}{connector}{d.name}/")
            new_prefix = prefix + ("    " if is_last else "│   ")
            walk_dir(d, new_prefix, depth + 1)
        
        for i, f in enumerate(files):
            if f.suffix in ['.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs', '.java', '.md', '.yaml', '.yml', '.json', '.toml']:
                is_last = (i == len(files) - 1)
                connector = "└── " if is_last else "├── "
                structure.append(f"{prefix}{connector}{f.name}")
    
    walk_dir(root_path)
    return "\n".join(structure)


def get_readme_content(root_path):
    """Get README content"""
    for name in ['README.md', 'README.txt', 'readme.md']:
        readme = Path(root_path) / name
        if readme.exists():
            content = readme.read_text()
            # Limit size
            if len(content) > 5000:
                content = content[:5000] + "\n\n... (truncated)"
            return content
    return None


def get_package_info(root_path):
    """Get package.json, pyproject.toml, or similar info"""
    info = {}
    
    # Python
    pyproject = Path(root_path) / "pyproject.toml"
    if pyproject.exists():
        info["pyproject.toml"] = pyproject.read_text()[:2000]
    
    # Node
    package = Path(root_path) / "package.json"
    if package.exists():
        import json
        try:
            pkg_data = json.loads(package.read_text())
            info["package.json"] = {
                "name": pkg_data.get("name"),
                "version": pkg_data.get("version"),
                "scripts": pkg_data.get("scripts", {}),
                "dependencies": list(pkg_data.get("dependencies", {}).keys())[:20],
            }
        except:
            pass
    
    return info


def get_git_info(root_path):
    """Get git branch and remote info"""
    info = {}
    
    try:
        result = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, cwd=root_path)
        if result.returncode == 0:
            info["current_branch"] = result.stdout.strip()
        
        result = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, cwd=root_path)
        if result.returncode == 0:
            info["origin_url"] = result.stdout.strip()
    except:
        pass
    
    return info


@app.command()
def explore(
    path: str = typer.Option(None, "--path", "-p", help="Path to explore (default: current directory)"),
    name: str = typer.Option("explore", "--name", "-n", help="Name for the exploration skill"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing exploration"),
):
    """Explore a codebase and store context as an AI skill"""
    from rich.console import Console
    from rich.progress import Progress
    
    console = Console()
    
    # Determine path
    if path:
        root_path = Path(path).resolve()
    else:
        # Try cwd repo first, then cwd
        cwd_repo = get_cwd_repo()
        if cwd_repo:
            root_path = Path(cwd_repo)
        else:
            root_path = Path.cwd()
    
    if not root_path.exists():
        console.print(f"[red]Error: Path does not exist: {root_path}[/red]")
        return
    
    console.print(f"[cyan]Exploring: {root_path}[/cyan]")
    
    # Check if skill already exists
    cfg = load_config()
    ai_skills = cfg.get("ai_skills", {})
    
    # Determine scope (repo-level if we have a cwd repo)
    scope = "repo"
    scope_key = str(root_path)
    cwd_repo = get_cwd_repo()
    if cwd_repo and Path(cwd_repo).resolve() == root_path.resolve():
        scope_key = cwd_repo
        current_project = get_current_project()
        if current_project:
            # Also store at project level
            scope = "project"
    
    if "repos" not in ai_skills:
        ai_skills["repos"] = {}
    if "projects" not in ai_skills:
        ai_skills["projects"] = {}
    
    # Check if already exists
    if not force:
        if scope == "repo" and scope_key in ai_skills.get("repos", {}):
            if name in ai_skills["repos"][scope_key]:
                console.print(f"[yellow]Skill '{name}' already exists. Use --force to overwrite.[/yellow]")
                return
        if scope == "project" and current_project in ai_skills.get("projects", {}):
            if name in ai_skills["projects"][current_project]:
                console.print(f"[yellow]Skill '{name}' already exists. Use --force to overwrite.[/yellow]")
                return
    
    # Gather exploration data
    exploration = []
    exploration.append(f"# Exploration of {root_path.name}")
    exploration.append("")
    
    # Git info
    git_info = get_git_info(root_path)
    if git_info:
        exploration.append("## Git Information")
        for k, v in git_info.items():
            exploration.append(f"- {k}: {v}")
        exploration.append("")
    
    # File structure
    exploration.append("## Project Structure")
    structure = get_file_structure(root_path, max_depth=3)
    exploration.append(f"```\n{structure}\n```")
    exploration.append("")
    
    # README
    readme = get_readme_content(root_path)
    if readme:
        exploration.append("## README")
        exploration.append(readme)
        exploration.append("")
    
    # Package info
    package_info = get_package_info(root_path)
    if package_info:
        exploration.append("## Dependencies")
        for filename, info in package_info.items():
            exploration.append(f"### {filename}")
            if isinstance(info, dict):
                for k, v in info.items():
                    exploration.append(f"- {k}: {v}")
            else:
                exploration.append(info)
            exploration.append("")
    
    # Combine into skill content
    skill_content = "\n".join(exploration)
    
    # Save to config
    if scope == "repo":
        if scope_key not in ai_skills["repos"]:
            ai_skills["repos"][scope_key] = {}
        ai_skills["repos"][scope_key][name] = skill_content
        console.print(f"[green]Saved exploration as repo skill: {name}[/green]")
    
    if scope == "project":
        if current_project not in ai_skills["projects"]:
            ai_skills["projects"][current_project] = {}
        ai_skills["projects"][current_project][name] = skill_content
        console.print(f"[green]Saved exploration as project skill: {name}[/green]")
    
    cfg["ai_skills"] = ai_skills
    save_config(cfg)
    
    console.print(f"[green]Exploration complete! The skill '{name}' will be available to the AI.[/green]")


@app.command()
def list():
    """List stored explorations"""
    from rich.console import Console
    from rich.table import Table
    
    console = Console()
    cfg = load_config()
    ai_skills = cfg.get("ai_skills", {})
    
    table = Table(title="Stored Explorations")
    table.add_column("Scope")
    table.add_column("Path/Project")
    table.add_column("Skill Name")
    
    # Repo skills
    for repo_path, skills in ai_skills.get("repos", {}).items():
        for skill_name in skills.keys():
            table.add_row("repo", repo_path, skill_name)
    
    # Project skills
    for project, skills in ai_skills.get("projects", {}).items():
        for skill_name in skills.keys():
            table.add_row("project", project, skill_name)
    
    console.print(table)


@app.command()
def delete(
    scope: str = typer.Argument(..., help="Scope: repo or project"),
    path_or_project: str = typer.Argument(..., help="Path or project name"),
    name: str = typer.Argument(..., help="Skill name to delete"),
):
    """Delete a stored exploration"""
    from rich.console import Console
    
    console = Console()
    cfg = load_config()
    ai_skills = cfg.get("ai_skills", {})
    
    if scope == "repo":
        if path_or_project in ai_skills.get("repos", {}):
            if name in ai_skills["repos"][path_or_project]:
                del ai_skills["repos"][path_or_project][name]
                console.print(f"[green]Deleted exploration: {name}[/green]")
            else:
                console.print(f"[red]Skill '{name}' not found[/red]")
        else:
            console.print(f"[red]Path '{path_or_project}' not found[/red]")
    elif scope == "project":
        if path_or_project in ai_skills.get("projects", {}):
            if name in ai_skills["projects"][path_or_project]:
                del ai_skills["projects"][path_or_project][name]
                console.print(f"[green]Deleted exploration: {name}[/green]")
            else:
                console.print(f"[red]Skill '{name}' not found[/red]")
        else:
            console.print(f"[red]Project '{path_or_project}' not found[/red]")
    else:
        console.print(f"[red]Invalid scope: {scope}[/red]")
        return
    
    cfg["ai_skills"] = ai_skills
    save_config(cfg)
