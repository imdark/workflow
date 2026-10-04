"""Shipping stages: a task is only Done once its work is live.

`wf done` used to mark a task Done as soon as there was nothing left to
open a PR for. That says nothing about whether the change ever reached
users. Instead a task climbs a ladder of shipping stages:

  Committed  the work is on a branch with a PR (or pushed commits)
  Merged     that branch landed on the base branch
  Deployed   a cloud deploy shipped the base branch containing it
  Done       the deploy was verified live

Stages only move forward -- re-running a step never demotes a task. The
markdown backend stores the stage names as statuses directly; Jira and
Linear have no such states, so there every stage short of Done maps to
their review state and Done to their done state.
"""

import subprocess
from typing import Callable, Iterable, List, Optional

COMMITTED = "Committed"
MERGED = "Merged"
DEPLOYED = "Deployed"
DONE = "Done"

STAGES = (COMMITTED, MERGED, DEPLOYED, DONE)

# Statuses a task can sit in while its branch waits to be merged.
AWAITING_MERGE = (COMMITTED, "In Review")


def stage_index(status: Optional[str]) -> int:
    """Position of `status` on the ladder; -1 when it isn't a shipping stage."""
    lowered = (status or "").strip().lower()
    for i, stage in enumerate(STAGES):
        if stage.lower() == lowered:
            return i
    return -1


def current_status(backend, task) -> Optional[str]:
    """`task`'s status: its own `.status`, else looked up on the backend."""
    status = getattr(task, "status", None)
    if status is not None or not hasattr(backend, "list_tasks"):
        return status
    try:
        return next((t.status for t in backend.list_tasks()
                     if t.key == task.key and hasattr(t, "status")), None)
    except Exception:
        return None


def advance(backend, task, stage: str) -> bool:
    """Move `task` up to `stage`; returns False when that would be a step back."""
    if stage not in STAGES:
        raise ValueError(f"'{stage}' is not a shipping stage: {', '.join(STAGES)}")
    current = current_status(backend, task)
    if stage_index(current) >= stage_index(stage):
        return False

    if hasattr(backend, "transition_task"):
        backend.transition_task(task.key, stage)
    elif stage == DONE:
        backend.move_to_done(task)
    else:
        backend.move_to_review(task)
    return True


def git(repo_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True)


def pr_state(repo_path: str, branch: str) -> Optional[str]:
    """GitHub PR state for `branch` (OPEN/MERGED/CLOSED), or None if unknown."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "state", "--jq", ".state"],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_merged(repo_path: str, branch: str, base: str) -> bool:
    """Whether `branch` has landed on `origin/<base>`.

    The PR's state is asked first: a squash or rebase merge leaves the
    branch's own commits out of the base's history. Without a PR, the
    branch counts as merged when its tip is an ancestor of the base.
    """
    state = pr_state(repo_path, branch)
    if state:
        return state == "MERGED"
    if git(repo_path, "rev-parse", "--verify", "--quiet", branch).returncode != 0:
        return False
    return git(repo_path, "merge-base", "--is-ancestor", branch, f"origin/{base}").returncode == 0


def promote_merged(backend, tasks: Iterable, merged: Callable[[object], bool]) -> List:
    """Move every task awaiting merge whose branch `merged` says landed."""
    moved = []
    for task in tasks:
        if task.status in AWAITING_MERGE and merged(task) and advance(backend, task, MERGED):
            moved.append(task)
    return moved


def promote(backend, tasks: Iterable, from_stage: str, to_stage: str) -> List:
    """Move every task sitting at `from_stage` up to `to_stage`."""
    return [task for task in tasks
            if task.status == from_stage and advance(backend, task, to_stage)]
