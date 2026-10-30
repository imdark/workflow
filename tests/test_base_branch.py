"""Which branch a task's PR goes into: the configured base, found from a task
worktree too, else the remote's default branch, not the first of main/master."""

import subprocess

import pytest

from workflow.git_utils import get_default_branch, main_checkout, remote_default_branch
from workflow.session import base_branch_for


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A clone of an origin whose default is `stage`, which also has `main`,
    and a task worktree of it."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
    git("branch", "stage", cwd=seed)
    origin = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(seed), str(origin), cwd=tmp_path)
    git("symbolic-ref", "HEAD", "refs/heads/stage", cwd=origin)
    clone = tmp_path / "clone"
    git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    worktree = tmp_path / "clone-task-1"
    git("worktree", "add", "-q", "-b", "task-1", str(worktree), "origin/stage", cwd=clone)
    return clone, worktree


def test_remote_default_is_the_remotes_head(repo):
    clone, worktree = repo
    assert remote_default_branch(str(clone)) == "stage"
    assert remote_default_branch(str(worktree)) == "stage"


def test_default_branch_prefers_the_remote_over_main(repo):
    clone, _ = repo
    assert get_default_branch(str(clone)) == "stage"


def test_main_checkout_of_a_worktree(repo):
    clone, worktree = repo
    assert main_checkout(str(worktree)) == str(clone.resolve())
    assert main_checkout(str(clone)) == str(clone.resolve())


def test_worktree_uses_its_checkouts_configured_base(repo):
    clone, worktree = repo
    repos = {str(clone.resolve()): {"base_branch": "release"}}
    assert base_branch_for(str(worktree), repos) == "release"


def test_unconfigured_worktree_uses_the_remote_default(repo):
    _, worktree = repo
    assert base_branch_for(str(worktree), {}) == "stage"


def test_an_explicit_base_wins(repo):
    clone, worktree = repo
    repos = {str(clone.resolve()): {"base_branch": "release"}}
    assert base_branch_for(str(worktree), repos, base="hotfix") == "hotfix"
