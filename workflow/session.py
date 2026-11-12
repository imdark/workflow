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


def configured_repos(project: Optional[str] = None) -> dict:
    """Every repository wf knows about: global ones plus the project's
    (the current one unless `project` names another)."""
    from workflow.config import get_repositories
    from workflow.projects import get_project_repositories

    repos = {}
    repos.update(get_repositories())
    repos.update(get_project_repositories(project))
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
    """`<key>-<title>`, with anything git refuses in a branch name dashed.

    Beyond the characters, git (`git check-ref-format`) refuses `//`, `..`,
    a path part that starts with `.` or ends with `.lock`, and a trailing
    `.`; a URL or a version number in the title hits those. Only those
    patterns are rewritten, so a title that already made a valid name keeps
    making the same one (deploy finds merged task branches by this name).
    """
    name = f"{issue.key.lower()}-{issue.title.replace(' ', '-')}"
    name = re.sub(r"[^A-Za-z0-9._/-]+", "-", name)
    name = re.sub(r"\.{2,}", ".", name)
    parts = []
    for part in name.split("/"):
        part = part.lstrip(".")
        if part.endswith(".lock"):
            part = part[: -len(".lock")] + "-lock"
        part = re.sub(r"-{2,}", "-", part).strip("-.")
        if part:
            parts.append(part)
    return "/".join(parts)


class BaseBranchNotConfigured(RuntimeError):
    """The repo has no base_branch set, so there's no branch to work from."""


#: git config key, per branch, that holds the base a task branch was started from (`wf start --base`), so its PR
#: goes into that branch without --base being repeated at `wf done` and without touching the repo's config.
BASE_KEY = "wf-base"


def normalize_base(base: Optional[str]) -> Optional[str]:
    """`origin/x` -> `x`: a base is named as the branch on the remote, which is what the PR is opened against."""
    if not base:
        return None
    base = base.strip()
    return base[len("origin/"):] if base.startswith("origin/") else base


def remember_base(repo_path: str, base: str) -> None:
    """Record `base` as the checked-out branch's own base (git config branch.<name>.wf-base)."""
    from workflow.git_utils import current_branch, get_repo

    branch = current_branch(repo_path)
    if branch and base:
        get_repo(repo_path).git.config(f"branch.{branch}.{BASE_KEY}", normalize_base(base))


def recorded_base(repo_path: str) -> Optional[str]:
    """The base recorded on the checked-out branch by `wf start --base`, else None."""
    from workflow.git_utils import current_branch, get_repo

    branch = current_branch(repo_path)
    if not branch:
        return None
    try:
        return get_repo(repo_path).git.config("--get", f"branch.{branch}.{BASE_KEY}").strip() or None
    except Exception:
        return None


def base_branch_for(repo_path: str, repos: dict, base: Optional[str] = None) -> str:
    """The branch task work in `repo_path` starts from and its PR goes into.

    In order: `base` (a --base flag), the base recorded on the branch when it was started with one, then the
    repo's `base_branch` setting -- it differs by repo (one PRs into main, another into stage), so it's never
    guessed. A task's worktree isn't in the config itself, so its main checkout's setting is used.
    """
    from workflow.git_utils import main_checkout

    if base:
        return normalize_base(base)
    recorded = recorded_base(repo_path)
    if recorded:
        return recorded
    configured = (repos.get(repo_path) or {}).get("base_branch")
    checkout = main_checkout(repo_path)
    if not configured and checkout and checkout != repo_path:
        configured = (repos.get(checkout) or {}).get("base_branch")
    if not configured:
        raise BaseBranchNotConfigured(
            f"No base_branch is set for {checkout or repo_path}. Set the branch "
            f"its PRs go into: wf config repo-add {checkout or repo_path} --base-branch <branch>"
        )
    return configured


def start_in_worktree(issue, repo_path: str, base_branch: str) -> str:
    """Put the task's branch in a worktree beside the repo; returns its path.

    An existing worktree for the branch is reused, so a task picked up
    again carries on where it was left.
    """
    from workflow.git_utils import base_ref, create_worktree, set_commit_prefix

    # from the base as origin has it: a trunk nobody checks out locally exists only as origin/<name>
    worktree_path = create_worktree(repo_path, task_branch_name(issue), base_ref(base_branch, repo_path))
    set_commit_prefix(issue.key, worktree_path)
    return worktree_path


def apply_claude_env(env: Optional[dict] = None, project: Optional[str] = None) -> dict:
    """Point a Claude launch at the project's account, the capture proxy, and the project itself.

    Sets CLAUDE_CONFIG_DIR, the proxy base URL and WF_PROJECT in `env` (default: this process's environment). The
    project is the current one unless `project` names another; WF_PROJECT pins it for every wf command the session
    runs (projects.session_project), so `wf task create` / `wf start` / `wf done` inside the session act on the
    project the session was started for, not on whatever the global current project is by then. Returns what it
    applied, for logging.
    """
    from workflow import claude_accounts
    from workflow.ai_providers.base import apply_proxy_env
    from workflow.projects import PROJECT_ENV, get_current_project

    if env is None:
        env = os.environ
    applied = dict(apply_proxy_env(env) or {})
    account_dir = claude_accounts.apply(env, project)
    if account_dir:
        applied[claude_accounts.ENV_VAR] = account_dir
    pinned = project or get_current_project()
    if pinned:
        env[PROJECT_ENV] = pinned
        applied[PROJECT_ENV] = pinned
    return applied
