import yaml
from pathlib import Path
from typing import Dict, Any, Optional
try:
    import typer
except ImportError:
    typer = None

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

KEYCHAIN_SERVICE = "workflow.jira"

def get_jira_token_from_keychain():
    """Retrieve Jira API token from macOS Keychain"""
    if not KEYRING_AVAILABLE:
        return None
    try:
        return keyring.get_password(KEYCHAIN_SERVICE, "api_token")
    except Exception:
        return None

def save_jira_token_to_keychain(token: str):
    """Store Jira API token in macOS Keychain"""
    if not KEYRING_AVAILABLE:
        return False
    try:
        keyring.set_password(KEYCHAIN_SERVICE, "api_token", token)
        return True
    except Exception:
        return False

def delete_jira_token_from_keychain():
    """Remove Jira API token from macOS Keychain"""
    if not KEYRING_AVAILABLE:
        return False
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, "api_token")
        return True
    except Exception:
        return False

def migrate_jira_token_to_keychain():
    """Migrate existing Jira token from config to Keychain if present"""
    if not KEYRING_AVAILABLE:
        return False, "Keyring not available"

    cfg = load_config()
    jira_config = cfg.get("jira", {})
    token = jira_config.get("token")

    if not token:
        return False, "No token found in config"

    if save_jira_token_to_keychain(token):
        # Remove token from config after successful migration
        if "jira" in cfg:
            if "token" in cfg["jira"]:
                del cfg["jira"]["token"]
                save_config(cfg)
        return True, "Token migrated to Keychain successfully"
    else:
        return False, "Failed to save token to Keychain"


def get_jira_config():
    """Get Jira configuration with token from Keychain if available"""
    cfg = load_config()
    jira_config = cfg.get("jira", {}).copy()

    # Try to get token from Keychain
    if KEYRING_AVAILABLE:
        keychain_token = get_jira_token_from_keychain()
        if keychain_token:
            jira_config["token"] = keychain_token
            return jira_config

    # Fall back to config file token
    return jira_config


CFG = Path.home() / ".wf" / "config.yaml"

def load_config(interactive=False):
    if CFG.exists():
        return yaml.safe_load(CFG.read_text())

    if not interactive:
        raise RuntimeError("Run wf init first")

    enable_git = input("Enable git integration? (y/n): ").lower().startswith('y')
    enable_github = input("Enable GitHub integration? (y/n): ").lower().startswith('y') if enable_git else False
    enable_slack = input("Enable Slack integration? (y/n): ").lower().startswith('y')
    slack_webhook = input("Slack webhook URL (leave empty to skip): ").strip() if enable_slack else None
    
    cfg = {
        "backend": "jira",
        "jira": {
            "url": input("Jira URL: "),
            "email": input("Jira Email: "),
            "token": input("Jira Token (get one at https://id.atlassian.com/manage-profile/security/api-tokens): "),
            "project": input("Jira Project Key (found in your Jira project URL, e.g., 'PROJ' from https://your-domain.atlassian.net/browse/PROJ): "),
        },
        "repositories": {},
        "git_enabled": enable_git,
        "github_enabled": enable_github,
        "slack_webhook": slack_webhook,
        "slack": {
            "default_channel": None,
            "message_templates": {
                "task_start": "🚀 Starting work on {task_key}: {task_title}",
                "task_complete": "✅ Completed {task_key}: {task_title}{pr_section}{reviewers_section}",
                "pr_ready": "🔗 PR ready for review: {task_key} - {pr_url}{reviewers_section}"
            },
            "user_mapping": {}  # GitHub email -> Slack username mapping
        },
        "ai": {"provider": "claude"},
    }
    CFG.write_text(yaml.dump(cfg))
    return cfg

def save_config(cfg):
    CFG.write_text(yaml.dump(cfg))

def add_repository(path, base_branch="main"):
    cfg = load_config()
    if "repositories" not in cfg:
        cfg["repositories"] = {}
    cfg["repositories"][path] = {"base_branch": base_branch}
    save_config(cfg)

def get_repositories():
    cfg = load_config()
    return cfg.get("repositories", {})

def is_git_enabled():
    cfg = load_config()
    return cfg.get("git_enabled", True)

def is_github_enabled():
    import os
    cfg = load_config()
    # Check if GitHub integration is enabled and either token is in env or stored in config
    github_enabled = cfg.get("github_enabled", True)
    has_token = "GITHUB_TOKEN" in os.environ or cfg.get("github", {}).get("token")
    return github_enabled and has_token

def get_github_integration_status():
    """Get detailed status of GitHub integration setup"""
    import os
    cfg = load_config()
    
    issues = []
    if not cfg.get("github_enabled", True):
        issues.append("GitHub integration is disabled in config")
    
    if "GITHUB_TOKEN" not in os.environ:
        issues.append("GITHUB_TOKEN environment variable not set")
    
    return issues

def set_github_token(token: str):
    """Save GitHub token to config"""
    import os
    cfg = load_config()
    if "github" not in cfg:
        cfg["github"] = {}
    cfg["github"]["token"] = token

def set_slack_channel(channel: str):
    """Set default Slack channel"""
    cfg = load_config()
    if "slack" not in cfg:
        cfg["slack"] = {}
    cfg["slack"]["default_channel"] = channel
    save_config(cfg)

def set_slack_message_template(message_type: str, template: str):
    """Set Slack message template"""
    cfg = load_config()
    if "slack" not in cfg:
        cfg["slack"] = {}
    if "message_templates" not in cfg["slack"]:
        cfg["slack"]["message_templates"] = {}
    cfg["slack"]["message_templates"][message_type] = template
    save_config(cfg)

def add_slack_user_mapping(github_email: str, slack_username: str):
    """Add GitHub email to Slack username mapping"""
    cfg = load_config()
    if "slack" not in cfg:
        cfg["slack"] = {}
    if "user_mapping" not in cfg["slack"]:
        cfg["slack"]["user_mapping"] = {}
    cfg["slack"]["user_mapping"][github_email] = slack_username
    save_config(cfg)

def get_slack_config():
    """Get Slack configuration"""
    cfg = load_config()
    return cfg.get("slack", {})

def add_custom_field(name: str, field_id: str, project_key: str = "", default_value: str = "", value_mapping: dict = {}):
    """Add a custom field configuration"""
    cfg = load_config()
    if "custom_fields" not in cfg:
        cfg["custom_fields"] = {}
    
    field_config = {
        "field_id": field_id,
        "project_key": project_key
    }
    
    # Add default value if provided
    if default_value:
        field_config["default_value"] = default_value
    
    # Add value mapping if provided (for translating user-friendly values to Jira values)
    if value_mapping:
        field_config["value_mapping"] = value_mapping
    
    cfg["custom_fields"][name] = field_config
    save_config(cfg)

def set_field_default(name: str, default_value: str):
    """Set default value for an existing custom field"""
    cfg = load_config()
    if "custom_fields" not in cfg or name not in cfg["custom_fields"]:
        raise Exception(f"Custom field '{name}' not found. Use 'wf config add-field' first.")
    
    cfg["custom_fields"][name]["default_value"] = default_value
    save_config(cfg)

def clear_field_default(name: str):
    """Clear default value for a custom field"""
    cfg = load_config()
    if "custom_fields" not in cfg or name not in cfg["custom_fields"]:
        raise Exception(f"Custom field '{name}' not found.")
    
    if "default_value" in cfg["custom_fields"][name]:
        del cfg["custom_fields"][name]["default_value"]
    save_config(cfg)

def get_field_value_mapping(name: str, user_value: str):
    """Get the Jira value for a user-friendly default value"""
    cfg = load_config()
    custom_fields = cfg.get("custom_fields", {})
    
    if name in custom_fields:
        field_config = custom_fields[name]
        value_mapping = field_config.get("value_mapping", {})
        
        # Check if the user value has a mapping
        if user_value in value_mapping:
            return value_mapping[user_value]
    
    # Return the original value if no mapping found
    return user_value

def get_custom_fields():
    """Get custom fields configuration"""
    cfg = load_config()
    return cfg.get("custom_fields", {})

def add_default_variables():
    """Add default variable configurations"""
    cfg = load_config()
    
    if "variables" not in cfg:
        cfg["variables"] = {}
    
    variables = cfg["variables"]
    
    # Add default variables if they don't exist
    default_vars = {
        "active_sprint": {
            "type": "jira_sprint",
            "field": "name",
            "cache": {"enabled": True, "ttl": 300}
        },
        "active_sprint_id": {
            "type": "jira_sprint", 
            "field": "id",
            "cache": {"enabled": True, "ttl": 300}
        },
        "active_sprint_state": {
            "type": "jira_sprint",
            "field": "state", 
            "cache": {"enabled": True, "ttl": 300}
        },
        "current_time": {
            "value": "{current_time}",  # This will be resolved by builtin variable
            "cache": {"enabled": False}
        },
        "current_date": {
            "value": "{current_date}",  # This will be resolved by builtin variable
            "cache": {"enabled": False}
        },
        "username": {
            "value": "{username}",  # This will be resolved by builtin variable
            "cache": {"enabled": False}
        }
    }
    
    for var_name, var_config in default_vars.items():
        if var_name not in variables:
            variables[var_name] = var_config
            try:
                import typer
                typer.echo(f"✅ Added default variable: {var_name}")
            except ImportError:
                print(f"✅ Added default variable: {var_name}")
    
    save_config(cfg)
    return cfg


def load_effective_config() -> Dict[str, Any]:
    """
    Get the effective configuration, merging project config with global config.
    This is a wrapper around the projects module for backward compatibility.
    """
    try:
        from .projects import get_effective_config, migrate_to_project_config
        
        # Check if we need to migrate
        cfg = load_config()
        if "projects" not in cfg or not cfg["projects"]:
            # Try auto-migration
            if migrate_to_project_config():
                # Migration successful, get the new effective config
                return get_effective_config()
        
        return get_effective_config()
    except ImportError:
        # Projects module not available, return regular config
        return load_config()


def get_project_repositories() -> Dict[str, Any]:
    """
    Get repositories from current project or global config.
    This is a wrapper around the projects module for backward compatibility.
    """
    try:
        from .projects import get_project_repositories
        return get_project_repositories()
    except ImportError:
        # Projects module not available, return regular repositories
        return get_repositories()
