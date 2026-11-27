"""wf done and a task's existing PR: only the CURRENT branch's open PR counts (a task can have several branches, and
a merge PR whose title quotes the task's branch name made `wf done` skip a new branch without pushing it), and a
branch that already has its PR still gets its new commits pushed."""
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from workflow import git_utils

ISSUE = SimpleNamespace(key="PAN-1228", title="onboard xdof")


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """origin has `trunk` and the pushed branch `pan-1228-old`; the clone is on the new branch `pan-1228-new`."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    git("init", "-q", "--bare", "-b", "trunk", str(origin), cwd=tmp_path)
    git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    git("checkout", "-q", "-b", "trunk", cwd=clone)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=clone)
    git("push", "-q", "origin", "trunk", cwd=clone)
    git("checkout", "-q", "-b", "pan-1228-old", cwd=clone)
    git("commit", "-q", "--allow-empty", "-m", "old work", cwd=clone)
    git("push", "-q", "origin", "pan-1228-old", cwd=clone)
    git("checkout", "-q", "-b", "pan-1228-new", "trunk", cwd=clone)
    git("commit", "-q", "--allow-empty", "-m", "new work", cwd=clone)
    return clone.resolve()


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A `gh` on PATH answering `pr list` from a list of open PRs, filtered by --head as the real one is."""
    bindir, prs_file = tmp_path / "bin", tmp_path / "prs.json"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(f"""#!{sys.executable}
import json, sys
a = sys.argv[1:]
prs = json.load(open({str(prs_file)!r}))
if a[:2] == ["pr", "list"] and "--head" in a:
    prs = [p for p in prs if p["headRefName"] == a[a.index("--head") + 1]]
print(json.dumps(prs))
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def set_prs(*prs):
        prs_file.write_text(json.dumps([{"number": n, "title": t, "url": f"https://x/pull/{n}", "headRefName": h}
                                        for n, t, h in prs]))
    set_prs()
    return set_prs


def test_another_branchs_pr_naming_the_task_is_not_this_branchs(repo, gh):
    # the 11-18 case: an open merge PR whose title quotes the task's other branch
    gh((2423, "Merge ci-flakes into pan-1228-old: the onboarding", "pan-1228-old"),
       (2404, "PAN-1228 xDOF onboarding", "pan-1228-old"))
    assert git_utils.check_existing_prs_for_issue(ISSUE, [str(repo)]) == {}


def test_this_branchs_pr_naming_the_task_is_found(repo, gh):
    gh((2423, "Merge ci-flakes into pan-1228-old", "pan-1228-old"),
       (2426, "PAN-1228 Fix six trunk CI flakes", "pan-1228-new"))
    found = git_utils.check_existing_prs_for_issue(ISSUE, [str(repo)])
    assert list(found) == [str(repo)] and found[str(repo)]["number"] == 2426


def test_this_branchs_pr_for_another_task_is_not_this_tasks(repo, gh):
    gh((9, "PAN-9 something else", "pan-1228-new"))
    assert git_utils.check_existing_prs_for_issue(ISSUE, [str(repo)]) == {}


def test_unpushed_commits_of_a_branch_never_pushed_count_from_the_base(repo):
    assert git_utils.unpushed_commits(str(repo), "pan-1228-new", "trunk") == 1


def test_unpushed_commits_of_a_pushed_branch_count_from_its_origin_copy(repo):
    git("push", "-q", "origin", "pan-1228-new", cwd=repo)
    assert git_utils.unpushed_commits(str(repo), "pan-1228-new", "trunk") == 0
    git("commit", "-q", "--allow-empty", "-m", "more", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "and more", cwd=repo)
    assert git_utils.unpushed_commits(str(repo), "pan-1228-new", "trunk") == 2


def test_unpushed_commits_without_a_base_is_zero_never_an_error(repo):
    assert git_utils.unpushed_commits(str(repo), "pan-1228-new", None) == 0
