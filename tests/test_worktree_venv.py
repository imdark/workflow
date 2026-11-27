"""A new worktree gets the main checkout's `.venv`, without looking dirty.

Runs in fresh workflow and robots_realtime worktrees had no `.venv` and
borrowed the main checkout's python with a hand-set PYTHONPATH.
"""

import subprocess
from pathlib import Path

from workflow.git_utils import create_worktree, link_venv


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def repo_with_venv(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    return repo


def test_a_new_worktree_links_the_checkouts_venv_and_stays_clean(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.git_utils.add_claude_trust", lambda path: None)
    repo = repo_with_venv(tmp_path)

    worktree = Path(create_worktree(str(repo), "t-1-x", "main"))

    assert (worktree / ".venv").is_symlink()
    assert (worktree / ".venv").resolve() == (repo / ".venv").resolve()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=worktree,
                            capture_output=True, text=True).stdout
    assert status == ""
    # Once is enough: the exclude line isn't added again, and an existing link is left alone.
    assert link_venv(str(worktree)) is None
    assert (repo / ".git" / "info" / "exclude").read_text().count("/.venv") == 1


def test_no_link_without_a_venv_or_in_the_main_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.git_utils.add_claude_trust", lambda path: None)
    repo = repo_with_venv(tmp_path)
    assert link_venv(str(repo)) is None

    (repo / ".venv" / "bin" / "python").unlink()
    worktree = Path(create_worktree(str(repo), "t-2-x", "main"))
    assert not (worktree / ".venv").exists()
