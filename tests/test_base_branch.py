"""Which branch a task works from and PRs into: the repo's base_branch setting,
found from a task worktree too, and never guessed when it isn't set."""

import subprocess

import pytest

from workflow.git_utils import main_checkout
from workflow.session import BaseBranchNotConfigured, base_branch_for


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A checkout with `main` and `stage`, and a task worktree of it."""
    clone = tmp_path / "clone"
    clone.mkdir()
    git("init", "-q", "-b", "main", cwd=clone)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=clone)
    git("branch", "stage", cwd=clone)
    worktree = tmp_path / "clone-task-1"
    git("worktree", "add", "-q", "-b", "task-1", str(worktree), "stage", cwd=clone)
    return clone.resolve(), worktree.resolve()


def test_main_checkout_of_a_worktree(repo):
    clone, worktree = repo
    assert main_checkout(str(worktree)) == str(clone)
    assert main_checkout(str(clone)) == str(clone)


def test_checkout_uses_its_setting(repo):
    clone, _ = repo
    assert base_branch_for(str(clone), {str(clone): {"base_branch": "stage"}}) == "stage"


def test_worktree_uses_its_checkouts_setting(repo):
    clone, worktree = repo
    assert base_branch_for(str(worktree), {str(clone): {"base_branch": "stage"}}) == "stage"


def test_unset_is_an_error_naming_the_checkout(repo):
    clone, worktree = repo
    # Not main, though the repo has one: the base is never guessed.
    with pytest.raises(BaseBranchNotConfigured, match=str(clone)):
        base_branch_for(str(worktree), {str(clone): {}})


def test_an_explicit_base_wins(repo):
    clone, worktree = repo
    repos = {str(clone): {"base_branch": "stage"}}
    assert base_branch_for(str(worktree), repos, base="hotfix") == "hotfix"
