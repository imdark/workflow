"""A PR's base branch is found on origin, not only among local branches; and without a terminal a missing one is an
error that names the fix, never a prompt (wf done hung on `Type branch name...` from an agent's shell)."""
import subprocess

import pytest

from workflow import git_utils


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path):
    """origin has `main` and `jannik/trunk`; the clone checked out `main` only, then branched a task off
    origin/jannik/trunk without creating a local `jannik/trunk`."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
    git("branch", "jannik/trunk", cwd=seed)
    git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    git("push", "-q", str(origin), "main", "jannik/trunk", cwd=seed)
    clone = tmp_path / "clone"
    git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    git("checkout", "-q", "-b", "task-1", "origin/jannik/trunk", cwd=clone)
    return clone.resolve()


def test_a_base_that_exists_only_on_origin_counts(clone):
    assert "jannik/trunk" not in git_utils.get_available_branches(str(clone))
    assert git_utils.branch_exists("jannik/trunk", str(clone))
    assert git_utils.branch_exists("main", str(clone))
    assert not git_utils.branch_exists("nope", str(clone))
    assert git_utils.select_base_branch("jannik/trunk", str(clone)) == "jannik/trunk"


def test_without_a_terminal_a_missing_base_is_an_error_that_names_the_fix(clone, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def no_prompt(*a, **k):
        raise AssertionError("prompted without a terminal")

    monkeypatch.setattr("rich.prompt.Prompt.ask", no_prompt)
    with pytest.raises(RuntimeError, match="origin/nope.*--base"):
        git_utils.select_base_branch("nope", str(clone))
