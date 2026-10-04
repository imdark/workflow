"""Shipping stages: a task reaches Done only through Committed, Merged, Deployed.

Runs against fake backends and a throwaway git repo; nothing is deployed
and no PR is looked up (`gh` is stubbed out).
"""

import subprocess
from dataclasses import dataclass

import pytest

from workflow import ship
from workflow.deploy import cloud


@dataclass
class Task:
    key: str
    title: str
    status: str


class MarkdownLike:
    """Stores any status string, like the markdown backend."""

    def __init__(self, *tasks):
        self.tasks = {t.key: t for t in tasks}
        self.moves = []

    def list_tasks(self):
        return [Task(t.key, t.title, t.status) for t in self.tasks.values()]

    def transition_task(self, key, status):
        self.moves.append((key, status))
        self.tasks[key].status = status


class TrackerLike:
    """Jira/Linear: only review and done states, no arbitrary statuses."""

    def __init__(self):
        self.moves = []

    def move_to_review(self, issue):
        self.moves.append((issue.key, "review"))

    def move_to_done(self, issue):
        self.moves.append((issue.key, "done"))


def test_stages_run_committed_merged_deployed_done():
    assert ship.STAGES == ("Committed", "Merged", "Deployed", "Done")
    assert ship.stage_index("deployed") == 2
    assert ship.stage_index("In Progress") == -1


def test_advance_never_steps_back():
    backend = MarkdownLike(Task("P-1", "t", "Deployed"))
    assert not ship.advance(backend, backend.tasks["P-1"], ship.COMMITTED)
    assert ship.advance(backend, backend.tasks["P-1"], ship.DONE)
    assert backend.moves == [("P-1", "Done")]


def test_advance_looks_up_status_when_the_issue_has_none():
    @dataclass
    class Issue:
        key: str

    backend = MarkdownLike(Task("P-1", "t", "Merged"))
    assert not ship.advance(backend, Issue("P-1"), ship.COMMITTED)
    assert backend.moves == []


def test_advance_maps_stages_onto_trackers_without_them():
    backend = TrackerLike()
    issue = Task("J-1", "t", "In Progress")
    ship.advance(backend, issue, ship.MERGED)
    ship.advance(backend, issue, ship.DONE)
    assert backend.moves == [("J-1", "review"), ("J-1", "done")]


def test_rejects_non_stages():
    with pytest.raises(ValueError):
        ship.advance(MarkdownLike(), Task("P-1", "t", "To Do"), "Shipped")


def test_promote_merged_only_moves_landed_tasks_awaiting_merge():
    backend = MarkdownLike(Task("P-1", "a", "Committed"), Task("P-2", "b", "In Review"),
                           Task("P-3", "c", "Committed"), Task("P-4", "d", "In Progress"))
    landed = {"P-1", "P-2", "P-4"}
    moved = ship.promote_merged(backend, backend.list_tasks(), lambda t: t.key in landed)
    assert [t.key for t in moved] == ["P-1", "P-2"]
    assert backend.tasks["P-3"].status == "Committed"
    assert backend.tasks["P-4"].status == "In Progress"


def test_promote_moves_only_the_given_stage():
    backend = MarkdownLike(Task("P-1", "a", "Merged"), Task("P-2", "b", "Committed"))
    moved = ship.promote(backend, backend.list_tasks(), ship.MERGED, ship.DEPLOYED)
    assert [t.key for t in moved] == ["P-1"]
    assert backend.tasks["P-2"].status == "Committed"


# ── git-backed checks ────────────────────────────────────────────────────────

def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """An origin with `main`, and a clone of it on `main`."""
    monkeypatch.setattr(ship, "pr_state", lambda repo, branch: None)
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    git(tmp_path, "init", "-q", "-b", "main", str(origin))
    git(origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "root")
    git(origin, "config", "receive.denyCurrentBranch", "updateInstead")
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, clone


def commit(repo, message):
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", message)


def test_is_merged_follows_the_base_branch(repos):
    origin, clone = repos
    git(clone, "checkout", "-q", "-b", "p-1-thing")
    commit(clone, "work")
    assert not ship.is_merged(str(clone), "p-1-thing", "main")

    git(clone, "push", "-q", "origin", "p-1-thing:main")
    git(clone, "fetch", "-q", "origin")
    assert ship.is_merged(str(clone), "p-1-thing", "main")
    assert not ship.is_merged(str(clone), "no-such-branch", "main")


def test_pr_state_wins_over_ancestry(repos, monkeypatch):
    _, clone = repos
    monkeypatch.setattr(ship, "pr_state", lambda repo, branch: "MERGED")
    assert ship.is_merged(str(clone), "squashed-away", "main")


def test_sync_checkout_fast_forwards_main(repos):
    origin, clone = repos
    commit(origin, "merged later")
    target = cloud.CloudTarget(repo=str(clone), command="true")
    assert cloud.sync_checkout(target) is None
    head = subprocess.run(["git", "-C", str(clone), "log", "-1", "--format=%s"],
                          capture_output=True, text=True).stdout.strip()
    assert head == "merged later"


def test_sync_checkout_refuses_other_branches_and_dirty_trees(repos):
    _, clone = repos
    target = cloud.CloudTarget(repo=str(clone), command="true")

    git(clone, "checkout", "-q", "-b", "feature")
    assert "is on 'feature'" in cloud.sync_checkout(target)

    git(clone, "checkout", "-q", "main")
    (clone / "f.txt").write_text("x")
    git(clone, "add", "f.txt")
    assert "uncommitted changes" in cloud.sync_checkout(target)


def test_target_needs_repo_and_command():
    assert cloud.target_from({}) is None
    assert cloud.target_from({"cloud_deploy": {"repo": "/r"}}) is None
    target = cloud.target_from({"cloud_deploy": {"repo": "/r", "command": "deploy",
                                                 "verify_url": "https://x/"}})
    assert (target.branch, target.verify_url) == ("main", "https://x/")
