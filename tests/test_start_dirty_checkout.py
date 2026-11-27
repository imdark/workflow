"""`wf start` on a dirty main checkout, or from an agent's shell: it makes a worktree instead of switching the
checkout's branch, never stops at a prompt, and branches from the base as origin has it now."""

import subprocess

import pytest

from workflow.cli import worktree_for_checkout
from workflow.git_utils import create_branch, fetch_base


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_a_dirty_checkout_run_by_an_agent_gets_a_worktree():
    assert worktree_for_checkout("/r", dirty=True, answer=None, agent=True) is True


def test_a_clean_checkout_run_by_an_agent_gets_a_worktree_unless_told_not_to():
    assert worktree_for_checkout("/r", dirty=False, answer=None, agent=True) is True
    assert worktree_for_checkout("/r", dirty=False, answer=False, agent=True) is False


def test_a_person_on_a_clean_checkout_branches_in_place():
    assert worktree_for_checkout("/r", dirty=False, answer=None, agent=False) is False
    assert worktree_for_checkout("/r", dirty=False, answer=True, agent=False) is True


@pytest.fixture
def origin_and_clone(tmp_path):
    """origin with `main` and `stage`; a clone on `main` that has a local `stage` one commit behind."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=origin)
    git("branch", "stage", cwd=origin)
    clone = tmp_path / "clone"
    git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    git("branch", "stage", "origin/stage", cwd=clone)
    git("checkout", "-q", "stage", cwd=origin)
    git("commit", "-q", "--allow-empty", "-m", "new on stage", cwd=origin)
    return origin, clone


def test_fetch_base_moves_origin_and_the_local_base(origin_and_clone):
    origin, clone = origin_and_clone
    tip = git("rev-parse", "stage", cwd=origin)
    assert fetch_base("stage", str(clone)) is None
    assert git("rev-parse", "origin/stage", cwd=clone) == tip
    assert git("rev-parse", "stage", cwd=clone) == tip


def test_fetch_base_checked_out_locally_still_moves_origin(origin_and_clone):
    origin, clone = origin_and_clone
    git("checkout", "-q", "stage", cwd=clone)           # git refuses stage:stage into the checked-out branch
    tip = git("rev-parse", "stage", cwd=origin)
    assert fetch_base("stage", str(clone)) is None
    assert git("rev-parse", "origin/stage", cwd=clone) == tip


def test_fetch_base_reports_a_missing_branch(origin_and_clone):
    _, clone = origin_and_clone
    assert fetch_base("nope", str(clone))


def test_create_branch_without_a_terminal_refuses_a_dirty_checkout(origin_and_clone, monkeypatch):
    _, clone = origin_and_clone
    (clone / "i18n.json").write_text("{}")
    git("add", "i18n.json", cwd=clone)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    class Issue:
        key = "PERSONAL-1"
        title = "fix"

    with pytest.raises(RuntimeError, match="--worktree"):
        create_branch(Issue(), "origin/stage", str(clone))
    assert git("branch", "--show-current", cwd=clone) == "main"   # the checkout's branch didn't move
