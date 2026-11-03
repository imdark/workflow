import subprocess
from types import SimpleNamespace

import pytest

from workflow.git_utils import commit_summary, pr_text

AGENT_PROMPT = ("THE TASK - the note item this run was started on:\nNote: Untitled\n\n"
                "claude ... (workflow, job f3dc8e96, cloud)\n\nHOW TO WORK ON IT - ...")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def commit(cwd, name, message):
    (cwd / name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path):
    """A task branch off origin/main, as a task worktree is."""
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    commit(tmp_path, "base", "base")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(tmp_path, "switch", "-c", "task")
    return tmp_path


def issue(description, title="claude … (workflow, job f3dc8e96, cloud) Cl…"):
    return SimpleNamespace(key="PERSONAL-201", title=title, description=description)


def test_agent_title_and_body_win(repo):
    commit(repo, "a", "feat: a")
    title, body = pr_text(issue(AGENT_PROMPT), str(repo), "main",
                          title="Add the thing", body="It adds the thing.")
    assert title == "PERSONAL-201 Add the thing"
    assert body == "It adds the thing."


def test_agent_ticket_without_summary_uses_its_lone_commit(repo):
    commit(repo, "a", "feat(x): hand a capture to agents\n\nPicks agents from a list.\n\n"
                      "Co-Authored-By: Claude <noreply@anthropic.com>")
    title, body = pr_text(issue(AGENT_PROMPT), str(repo), "main")
    assert title == "PERSONAL-201 feat(x): hand a capture to agents"
    assert body == "Picks agents from a list."
    assert "THE TASK" not in body


def test_agent_ticket_with_several_commits_lists_them(repo):
    commit(repo, "a", "feat: first\n\nWhy first.")
    commit(repo, "b", "fix: second")
    title, body = pr_text(issue(AGENT_PROMPT, title="Short item"), str(repo), "main")
    assert title == "PERSONAL-201 Short item"
    assert body == "- feat: first\n\nWhy first.\n\n- fix: second"


def test_a_persons_ticket_keeps_its_title_and_description(repo):
    commit(repo, "a", "feat: a")
    title, body = pr_text(issue("Make the button blue.", title="Blue button"), str(repo), "main")
    assert (title, body) == ("PERSONAL-201 Blue button", "Make the button blue.")


def test_title_is_one_line(repo):
    title, _ = pr_text(issue("", title="two\nlines"), str(repo), "main", title="a\n  b")
    assert title == "PERSONAL-201 a b"


def test_commit_summary_without_the_base_branch(repo):
    assert commit_summary(str(repo), "no-such-branch") == (None, "")
