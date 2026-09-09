import typer
import json

from workflow.config import load_config, save_config


app = typer.Typer(help="Configuration management")


@app.command("list")
def config_list():
    """List all workflow configuration"""
    cfg = load_config()
    
    typer.echo("Workflow Configuration:")
    for key, value in cfg.items():
        if key == "repositories":
            typer.echo(f"{key}:")
            for repo_path, repo_config in value.items():
                base_branch = repo_config.get("base_branch", "default")
                typer.echo(f"  {repo_path} (base: {base_branch})")
        else:
            typer.echo(f"{key}: {value}")


@app.command("set")
def set_config(key: str = typer.Argument(...), value: str = typer.Argument(...)):
    """Set a configuration value"""
    cfg = load_config()
    
    valid_keys = [
        "task_backend", "jira", "github", "git_enabled", "github_enabled",
        "github_token", "repositories", "ai", "ai.provider"
    ]
    
    if key not in valid_keys:
        typer.echo(f"❌ Error: Invalid key '{key}'. Valid keys: {', '.join(valid_keys)}")
        typer.echo("Supported keys are:")
        for valid_key in valid_keys:
            typer.echo(f"  - {valid_key}")
        return
    
    if value.lower() in ["true", "yes", "y", "1"]:
        cfg[key] = True
    elif value.lower() in ["false", "no", "n", "0"]:
        cfg[key] = False
    else:
        if key == "repositories":
            try:
                repos = json.loads(value)
                cfg[key] = repos
            except json.JSONDecodeError:
                typer.echo(f"❌ Error: Invalid JSON for repositories. Must be a dictionary.")
                return
        elif key == "ai.provider":
            if "ai" not in cfg:
                cfg["ai"] = {}
            cfg["ai"]["provider"] = value
        else:
            cfg[key] = value
    
    save_config(cfg)
    typer.echo(f"✅ Set {key} to {cfg[key]}")


@app.command("get")
def get_config():
    """Get current configuration"""
    cfg = load_config()
    
    typer.echo("Current Configuration:")
    for key, value in cfg.items():
        typer.echo(f"{key}: {value}")


@app.command("migrate-jira-token")
def config_migrate_jira_token():
    """Migrate Jira API token from config to macOS Keychain"""
    from workflow.config import migrate_jira_token_to_keychain
    
    success, message = migrate_jira_token_to_keychain()
    if success:
        typer.echo(f"✅ {message}")
    else:
        typer.echo(f"❌ {message}")


@app.command("set-jira-token")
def config_set_jira_token(token: str = typer.Argument(..., help="Jira API token")):
    """Store Jira API token in macOS Keychain"""
    from workflow.config import save_jira_token_to_keychain
    
    if save_jira_token_to_keychain(token):
        typer.echo("✅ Jira token saved to macOS Keychain")
    else:
        typer.echo("❌ Failed to save token to Keychain")


@app.command("delete-jira-token")
def config_delete_jira_token():
    """Remove Jira API token from macOS Keychain"""
    from workflow.config import delete_jira_token_from_keychain
    
    if delete_jira_token_from_keychain():
        typer.echo("✅ Jira token removed from Keychain")
    else:
        typer.echo("❌ Failed to remove token from Keychain (may not exist)")
