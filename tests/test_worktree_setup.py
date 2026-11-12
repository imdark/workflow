"""A new worktree runs the repo's scripts/worktree-setup.sh."""
import subprocess
from pathlib import Path

from workflow import git_utils


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path, setup_script=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("x\n")
    if setup_script is not None:
        (repo / "scripts").mkdir()
        (repo / "scripts" / "worktree-setup.sh").write_text(setup_script)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_new_worktree_runs_setup_script(tmp_path, monkeypatch):
    monkeypatch.setattr(git_utils, "add_claude_trust", lambda path: None)
    repo = _repo(tmp_path, 'echo ran > "$PWD/setup-ran"\n')

    path = git_utils.create_worktree(str(repo), "feature", "main")

    assert (Path(path) / "setup-ran").read_text() == "ran\n"


def test_failing_setup_script_does_not_stop_the_start(tmp_path, monkeypatch):
    monkeypatch.setattr(git_utils, "add_claude_trust", lambda path: None)
    repo = _repo(tmp_path, "echo boom >&2; exit 3\n")

    path = git_utils.create_worktree(str(repo), "feature", "main")

    assert Path(path).is_dir()
    assert git_utils.run_worktree_setup(path) is False


def test_repo_without_setup_script(tmp_path, monkeypatch):
    monkeypatch.setattr(git_utils, "add_claude_trust", lambda path: None)
    repo = _repo(tmp_path)

    path = git_utils.create_worktree(str(repo), "feature", "main")

    assert git_utils.run_worktree_setup(path) is None
