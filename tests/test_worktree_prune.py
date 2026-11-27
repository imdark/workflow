"""wf prune: task worktrees go once their branch is pushed and has a PR up."""

import subprocess
from pathlib import Path

import pytest

from workflow import worktree_prune


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.pid_manager.STATE_DIR", tmp_path / "state")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    main = tmp_path / "main"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / "a.txt").write_text("a\n")
    (main / ".gitignore").write_text("node_modules/\n")
    git(main, "add", ".")
    git(main, "commit", "-qm", "init")
    git(main, "remote", "add", "origin", str(origin))
    git(main, "push", "-q", "origin", "main")
    monkeypatch.chdir(tmp_path)
    return main.resolve()


def worktree(repo, branch, push=True, path=None):
    from workflow.git_utils import get_worktree_path
    path = path or Path(get_worktree_path(str(repo), branch))
    git(repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
    (path / "work.txt").write_text(f"{branch}\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", branch)
    if push:
        git(path, "push", "-q", "origin", branch)
    return path.resolve()


def has_pr(_repo, branch):
    return "OPEN"


def test_a_pushed_worktree_with_a_pr_is_removed_and_its_branch_kept(repo):
    path = worktree(repo, "task-1")
    (path / "node_modules").mkdir()
    (path / "node_modules" / "big.js").write_text("ignored\n")

    results = worktree_prune.prune([str(repo)], pr_state=has_pr)

    assert (str(path), "task-1", None) in results
    assert not path.exists()
    assert "task-1" in git(repo, "branch", "--list", "task-1")


@pytest.mark.parametrize("setup, reason", [
    (lambda p: (p / "new.txt").write_text("n\n"), "uncommitted changes"),
    (lambda p: (git(p, "commit", "-q", "--allow-empty", "-m", "later")), "unpushed commits"),
])
def test_a_worktree_with_work_not_on_origin_stays(repo, setup, reason):
    path = worktree(repo, "task-2")
    setup(path)
    results = worktree_prune.prune([str(repo)], pr_state=has_pr)
    assert (str(path), "task-2", reason) in results
    assert path.exists()


def test_a_branch_never_pushed_stays(repo):
    path = worktree(repo, "task-3", push=False)
    assert worktree_prune.prune_task_worktree(str(path), pr_state=has_pr) == "branch not pushed"
    assert path.exists()


@pytest.mark.parametrize("state, reason", [(None, "no PR"), ("CLOSED", "PR closed")])
def test_a_branch_without_an_open_or_merged_pr_stays(repo, state, reason):
    path = worktree(repo, "task-4")
    assert worktree_prune.prune_task_worktree(str(path), pr_state=lambda *_: state) == reason
    assert path.exists()


def test_a_merged_pr_counts(repo):
    path = worktree(repo, "task-5")
    assert worktree_prune.prune_task_worktree(str(path), pr_state=lambda *_: "MERGED") is None
    assert not path.exists()


def test_a_worktree_an_ai_session_runs_in_stays(repo):
    from workflow.pid_manager import create_ai_pid_file
    path = worktree(repo, "task-6")
    create_ai_pid_file(str(path), "T-6", "task-6")
    assert worktree_prune.prune_task_worktree(str(path), pr_state=has_pr) == "AI session running"


def test_the_worktree_you_are_in_stays(repo, monkeypatch):
    path = worktree(repo, "task-7")
    monkeypatch.chdir(path)
    assert worktree_prune.prune_task_worktree(str(path), pr_state=has_pr) == "current directory"


def test_a_hand_made_worktree_stays_unless_all_is_given(repo):
    path = worktree(repo, "feature/x", path=repo.parent / "my-scratch")
    assert worktree_prune.prune_task_worktree(str(path), pr_state=has_pr) == "not made by wf start (--all includes it)"
    results = worktree_prune.prune([str(repo)], pr_state=has_pr, any_worktree=True)
    assert (str(path), "feature/x", None) in results
    assert not path.exists()


def test_a_branch_with_a_slash_in_its_wf_path_is_removed(repo):
    path = worktree(repo, "pan-1/a.b")
    assert path.name == "main-pan-1_a_b"
    assert worktree_prune.prune_task_worktree(str(path), pr_state=has_pr) is None


def test_the_main_checkout_is_never_removed_and_dry_run_removes_nothing(repo):
    path = worktree(repo, "task-8")
    results = worktree_prune.prune([str(repo), str(path)], dry_run=True, pr_state=has_pr)
    assert (str(repo), "main", "main checkout") in results
    assert (str(path), "task-8", None) in results
    assert len(results) == 2          # the worktree, also configured, isn't looked at twice
    assert path.exists()
