import typer
from pathlib import Path

from workflow.config import load_config, save_config


app = typer.Typer(help="Repository management")


@app.command("add")
def repo_add(path: str = typer.Argument(...), base_branch: str = typer.Option(None, help="Base branch name")):
    """Add a new repository to workflow configuration"""
    cfg = load_config()
    
    if not Path(path).exists():
        typer.echo(f"❌ Error: Path '{path}' does not exist.")
        return
    
    repos = cfg.get("repositories", {})
    if path in repos:
        typer.echo(f"❌ Error: Repository '{path}' already exists in configuration.")
        return
    
    repos[path] = {}
    if base_branch:
        repos[path]["base_branch"] = base_branch
    
    save_config(cfg)
    
    typer.echo(f"✅ Added repository: {path}")
    if base_branch:
        typer.echo(f"📝 Base branch: {base_branch}")


@app.command("remove")
def repo_remove(path: str = typer.Argument(...)):
    """Remove a repository from workflow configuration"""
    cfg = load_config()
    repos = cfg.get("repositories", {})
    
    if path not in repos:
        typer.echo(f"❌ Error: Repository '{path}' not found in configuration.")
        return
    
    del repos[path]
    save_config(cfg)
    typer.echo(f"✅ Removed repository: {path}")


@app.command("list")
def repo_list():
    """List all configured repositories"""
    cfg = load_config()
    repos = cfg.get("repositories", {})
    
    if not repos:
        typer.echo("No repositories configured.")
        return
    
    typer.echo("Configured repositories:")
    for i, (repo_path, repo_config) in enumerate(repos.items(), 1):
        base_branch = repo_config.get("base_branch", "default")
        typer.echo(f"{i}. {repo_path} (base: {base_branch})")
