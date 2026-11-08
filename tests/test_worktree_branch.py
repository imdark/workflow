"""`wf done` and `wf start` from outside a task's worktree.

`wf done --repo-path <worktree>` run from the main checkout must push and
open the PR for the worktree's branch, not the main checkout's; `wf start`
in an agent's shell (no terminal) must not abort on the worktree question.
"""

import subprocess

import pytest

from workflow.cli import ask_worktree
from workflow.git_utils import current_branch


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def checkout_and_worktree(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    git("init", "-b", "main", cwd=main)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "init", cwd=main)
    worktree = tmp_path / "repo-task"
    git("worktree", "add", "-b", "task-1-fix", str(worktree), cwd=main)
    return main, worktree


def test_branch_is_the_worktrees_not_the_cwds(checkout_and_worktree, monkeypatch):
    main, worktree = checkout_and_worktree
    # Run from the main checkout, as `wf done --repo-path <worktree>` was.
    monkeypatch.chdir(main)
    assert current_branch(str(worktree)) == "task-1-fix"
    assert current_branch() == "main"


def test_no_branch_when_detached_or_not_a_repo(checkout_and_worktree, tmp_path):
    main, _ = checkout_and_worktree
    git("checkout", "--detach", cwd=main)
    assert current_branch(str(main)) is None
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    assert current_branch(str(elsewhere)) is None


def test_worktree_question_answered_up_front():
    assert ask_worktree("Worktree?", True, interactive=True) is True
    assert ask_worktree("Worktree?", False, interactive=False) is False


def test_worktree_question_without_a_terminal_takes_the_default():
    # Asking here would read EOF and abort `wf start`.
    assert ask_worktree("Worktree?", None, interactive=False) is True
