"""Per-project Claude Code accounts.

Claude Pro/Max logins are OAuth credentials created by `/login`, not API
keys, so no environment variable swaps them. What does swap them is the
config directory: Claude Code keys its macOS Keychain entry to
`CLAUDE_CONFIG_DIR`, so a session pointed at a different directory reads a
different credential. Two accounts can stay logged in at once and switching
costs nothing -- no re-auth, no `/logout`.

This module maps each workflow project to a config directory:

    projects:
      work:
        claude_config_dir: ~/.claude-work
      personal:
        claude_config_dir: ~/.claude-personal

What a config directory holds, and therefore what stops being shared
between accounts: the login, `settings.json`, session history, and plugins.
A repo's own `.claude/settings.json` is read from the repo and keeps
applying to both.
"""

import os
from pathlib import Path
from typing import Optional

ENV_VAR = "CLAUDE_CONFIG_DIR"
CONFIG_KEY = "claude_config_dir"

# Where a project's directory goes when `wf auth setup` is left to choose.
DEFAULT_DIR_TEMPLATE = "~/.claude-{project}"


def config_dir_for_project(project_name: Optional[str] = None, cfg=None) -> Optional[str]:
    """The Claude config directory configured for a project, if any.

    Returns an expanded absolute path, or None when the project has no
    directory set -- in which case Claude Code's default ~/.claude applies
    and nothing is switched.
    """
    if cfg is None:
        from workflow.config import load_config
        cfg = load_config()

    if project_name is None:
        from workflow.projects import get_current_project
        project_name = get_current_project()
    if not project_name:
        return None

    project = (cfg.get("projects") or {}).get(project_name) or {}
    raw = project.get(CONFIG_KEY)
    return str(Path(raw).expanduser()) if raw else None


def set_config_dir(project_name: str, directory: Optional[str]) -> str:
    """Point a project at a config directory. Passing None clears it."""
    from workflow.config import load_config, save_config

    cfg = load_config()
    projects = cfg.get("projects") or {}
    if project_name not in projects:
        raise ValueError(f"Project '{project_name}' not found")

    if directory is None:
        projects[project_name].pop(CONFIG_KEY, None)
        save_config(cfg)
        return ""

    resolved = str(Path(directory).expanduser())
    projects[project_name][CONFIG_KEY] = resolved
    save_config(cfg)
    return resolved


def default_dir_for(project_name: str) -> str:
    return str(Path(DEFAULT_DIR_TEMPLATE.format(project=project_name.lower())).expanduser())


def is_logged_in(config_dir: str) -> bool:
    """Whether a config directory looks like it has been through `/login`.

    On macOS the credential itself lives in the Keychain, so its presence
    cannot be checked without prompting for Keychain access. The directory
    having been initialized at all is the honest signal available, so treat
    this as "set up", not as "has a valid, unexpired login".
    """
    path = Path(config_dir).expanduser()
    if (path / ".credentials.json").exists():
        return True
    # The macOS case: no credentials file, but Claude Code has written its
    # own state into the directory.
    return any((path / name).exists() for name in ("settings.json", "history.jsonl", ".claude.json"))


def apply(env=None, project_name: Optional[str] = None) -> Optional[str]:
    """Set CLAUDE_CONFIG_DIR for the active project.

    Returns the directory applied, or None when the project has none
    configured or the caller already set the variable explicitly.
    """
    if env is None:
        env = os.environ

    if env.get(ENV_VAR):
        return None  # respect an explicit choice

    directory = config_dir_for_project(project_name)
    if not directory:
        return None

    Path(directory).mkdir(parents=True, exist_ok=True)
    env[ENV_VAR] = directory
    return directory


def shell_export(project_name: Optional[str] = None) -> Optional[str]:
    """The export line that switches a shell to a project's account."""
    directory = config_dir_for_project(project_name)
    return f"export {ENV_VAR}={directory}" if directory else None
