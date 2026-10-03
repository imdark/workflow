"""Starting work on a task: the steps `wf start`, `wf ai` and agent jobs share.

`wf start` picks a repository, names a branch after the task and, when the
checkout is busy, puts that branch in a worktree beside the repo. `wf ai`
then launches the model as the project's Claude account and through the
capture proxy. A remote agent job (see `deploy.jobs`) does the same steps
headlessly, so it lands on the same branch names, worktrees, account and
task memory as work started from the terminal.
"""

import os
import re
from pathlib import Path
from typing import Optional


def configured_repos() -> dict:
    """Every repository wf knows about: global ones plus the project's."""
    from workflow.config import get_repositories
    from workflow.projects import get_project_repositories

    repos = {}
    repos.update(get_repositories())
    repos.update(get_project_repositories())
    return repos


def resolve_repo(repos: dict) -> tuple:
    """The repository to work in, and how it was chosen.

    Returns (path, how) with `how` one of "cwd match", "default" or
    "first"; (None, "") when nothing fits. The current directory wins, then
    the project's default repo, then the first configured repo on disk.
    """
    from workflow.projects import get_cwd_repo, get_default_repo

    cwd_repo = get_cwd_repo()
    if cwd_repo and cwd_repo in repos:
        return cwd_repo, "cwd match"
    default_repo = get_default_repo()
    if default_repo:
        return (default_repo, "default") if default_repo in repos else (None, "")
    on_disk = [path for path in repos if Path(path).exists()]
    return (on_disk[0], "first") if on_disk else (None, "")


def task_branch_name(issue) -> str:
    """`<key>-<title>`, with anything git refuses in a branch name dashed."""
    name = f"{issue.key.lower()}-{issue.title.replace(' ', '-')}"
    name = re.sub(r"[^A-Za-z0-9._/-]+", "-", name)
    return re.sub(r"-{2,}", "-", name).strip("-./")


def base_branch_for(repo_path: str, repos: dict, base: Optional[str] = None) -> str:
    """The branch new task work starts from in `repo_path`."""
    from workflow.git_utils import get_default_branch

    return base or (repos.get(repo_path) or {}).get("base_branch") or get_default_branch(repo_path)


def start_in_worktree(issue, repo_path: str, base_branch: str) -> str:
    """Put the task's branch in a worktree beside the repo; returns its path.

    An existing worktree for the branch is reused, so a task picked up
    again carries on where it was left.
    """
    from workflow.git_utils import create_worktree, set_commit_prefix

    worktree_path = create_worktree(repo_path, task_branch_name(issue), base_branch)
    set_commit_prefix(issue.key, worktree_path)
    return worktree_path


def apply_claude_env(env: Optional[dict] = None) -> dict:
    """Point a Claude launch at the project's account and the capture proxy.

    Sets CLAUDE_CONFIG_DIR and the proxy base URL in `env` (default: this
    process's environment). Returns what it applied, for logging.
    """
    from workflow import claude_accounts
    from workflow.ai_providers.base import apply_proxy_env

    if env is None:
        env = os.environ
    applied = dict(apply_proxy_env(env) or {})
    account_dir = claude_accounts.apply(env)
    if account_dir:
        applied[claude_accounts.ENV_VAR] = account_dir
    return applied
