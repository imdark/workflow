"""Removing task worktrees whose work is safely on GitHub.

`wf start` and agent jobs put each task's branch in a worktree beside the
repo, and nothing took them away again: with node_modules and build output
in each, a busy machine's disk filled up. Once a branch is pushed and its PR
is up, the worktree holds nothing the remote doesn't, so it can go; the
branch itself stays, and `wf start` / `wf switch` recreate the worktree if
the task is picked up again.

A worktree is kept when any of these hold:
- it is a repo's main checkout, or HEAD is detached (no branch to come back to);
- `wf start` didn't make it (not at its path for the branch): a hand-made one
  may hold ignored files (.env, local config) nobody can recreate. `--all`
  drops this check;
- it has uncommitted changes or untracked files (ignored files don't count);
- the branch has commits origin doesn't, or was never pushed;
- no PR, open or merged, has the branch as its head;
- an AI session is running in it, or it contains the current directory.
"""

import os
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Optional


def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=60)


def gh_pr_state(repo_path: str, branch: str) -> Optional[str]:
    """The state (OPEN, MERGED, CLOSED) of the newest PR from `branch`, or None."""
    import json

    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "state", "--limit", "1"],
            cwd=repo_path, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        prs = json.loads(out.stdout or "[]")
    except ValueError:
        return None
    return prs[0].get("state") if prs else None


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def made_by_wf(path: str, branch: str, repo_path: str) -> bool:
    """Whether `path` is where `wf start` puts `branch`'s worktree (git_utils.get_worktree_path,
    "-N" suffix included). Others were made by hand and may hold ignored files nobody can recreate."""
    import re

    from workflow.git_utils import safe_branch_dirname

    repo = Path(repo_path).resolve()
    expected = f"{repo.name}-{safe_branch_dirname(branch)}"
    p = Path(path).resolve()
    return p.parent == repo.parent and re.fullmatch(re.escape(expected) + r"(-\d+)?", p.name) is not None


def keep_reason(worktree: dict, repo_path: str,
                pr_state: Callable[[str, str], Optional[str]] = gh_pr_state,
                any_worktree: bool = False) -> Optional[str]:
    """Why `worktree` (an entry of git_utils.list_worktrees) must stay, or None when it can go."""
    from workflow.pid_manager import is_ai_active

    path = worktree["path"]
    branch = worktree.get("branch")
    if worktree.get("is_main") or Path(path).resolve() == Path(repo_path).resolve():
        return "main checkout"
    if worktree.get("is_bare"):
        return "bare"
    if not branch:
        return "detached HEAD"
    if not any_worktree and not made_by_wf(path, branch, repo_path):
        return "not made by wf start (--all includes it)"
    if not Path(path).exists():
        return "missing on disk (run `git worktree prune`)"
    if _inside(Path(os.getcwd()).resolve(), Path(path).resolve()):
        return "current directory"
    if is_ai_active(path):
        return "AI session running"
    status = _git(path, "status", "--porcelain")
    if status.returncode != 0:
        return "git status failed"
    if status.stdout.strip():
        return "uncommitted changes"
    remote = f"refs/remotes/origin/{branch}"
    if _git(path, "rev-parse", "--verify", "-q", remote).returncode != 0:
        return "branch not pushed"
    ahead = _git(path, "rev-list", "--count", f"{remote}..{branch}")
    if ahead.returncode != 0 or ahead.stdout.strip() != "0":
        return "unpushed commits"
    state = pr_state(repo_path, branch)
    if state not in ("OPEN", "MERGED"):
        return "no PR" if state is None else f"PR {state.lower()}"
    return None


def prune_repo(repo_path: str, dry_run: bool = False,
               pr_state: Callable[[str, str], Optional[str]] = gh_pr_state,
               only: Optional[str] = None, any_worktree: bool = False) -> list:
    """Remove `repo_path`'s prunable worktrees (just `only`, when given). Only
    ones `wf start` made, unless `any_worktree`.

    Returns (path, branch, reason) per worktree looked at; reason is None for
    one removed (or, with dry_run, that would be).
    """
    from workflow.git_utils import list_worktrees, main_checkout

    main = main_checkout(repo_path) or repo_path
    results = []
    for wt in list_worktrees(main):
        if only and Path(wt["path"]).resolve() != Path(only).resolve():
            continue
        reason = keep_reason(wt, main, pr_state, any_worktree)
        if reason is None and not dry_run:
            # Not --force: git refuses if anything changed since the check.
            removed = _git(main, "worktree", "remove", wt["path"])
            if removed.returncode != 0:
                reason = f"git worktree remove failed: {removed.stderr.strip()[:200]}"
        results.append((wt["path"], wt.get("branch"), reason))
    return results


def prune_task_worktree(worktree_path: str,
                        pr_state: Callable[[str, str], Optional[str]] = gh_pr_state) -> Optional[str]:
    """Remove one task worktree if it can go. Returns why it stayed, or None once removed."""
    from workflow.git_utils import main_checkout

    main = main_checkout(worktree_path)
    if not main:
        return "not a git worktree"
    results = prune_repo(main, pr_state=pr_state, only=worktree_path)
    return results[0][2] if results else "not a worktree of its repo"


def all_repo_paths() -> list:
    """Every configured repository, global and in any project, that exists on disk."""
    from workflow.config import get_repositories
    from workflow.projects import list_projects

    paths = list(get_repositories())
    for project in list_projects().values():
        paths.extend((project or {}).get("repositories", {}))
    seen, out = set(), []
    for p in paths:
        key = str(Path(p).expanduser().resolve())
        if key not in seen and Path(key).is_dir():
            seen.add(key)
            out.append(key)
    return out


def prune(repo_paths: Optional[Iterable[str]] = None, dry_run: bool = False,
          pr_state: Callable[[str, str], Optional[str]] = gh_pr_state,
          any_worktree: bool = False) -> list:
    """prune_repo over every configured repository (or `repo_paths`)."""
    from workflow.git_utils import main_checkout

    results, seen = [], set()
    for repo in (repo_paths if repo_paths is not None else all_repo_paths()):
        main = main_checkout(repo)
        if not main or main in seen:      # not a repo, or one already done via another of its checkouts
            continue
        seen.add(main)
        results.extend(prune_repo(main, dry_run=dry_run, pr_state=pr_state, any_worktree=any_worktree))
    return results
