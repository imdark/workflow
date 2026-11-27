"""A BLOCKED task waits for a person: neither `wf start` nor an agent job
starts it again.

Runs set wrong-repo and spec-less tasks BLOCKED and stopped, and the next
dispatch sent the same task straight back, to stop at the same place.
"""

import json

import pytest
from typer.testing import CliRunner

from workflow import session
from workflow.backends.base import Issue
from workflow.deploy.jobs import Job, start_job_task


def blocked_issue(key="PERSONAL-9", title="Allow gh pr list"):
    issue = Issue(key, title, "")
    issue.status = "Blocked"
    return issue


@pytest.mark.parametrize("item, blocked", [
    ("BLOCKED **Allow gh pr list**", True),
    ("- [ ] BLOCKED Allow gh pr list", True),
    ("\n  [ ] BLOCKED: wrong repo", True),
    ("COMMITTED **Make wf-tools load**", False),
    ("Allow gh pr list\nBLOCKED (2026-11-16): goes in robots_realtime", False),
    ("Unblock BLOCKEDness checks", False),
    ("", False),
])
def test_a_note_item_is_blocked_when_its_text_leads_with_the_keyword(item, blocked):
    assert session.is_blocked(item=item) is blocked


def test_a_ticket_is_blocked_by_its_status():
    assert session.is_blocked(blocked_issue())
    assert not session.is_blocked(Issue("PERSONAL-1", "x", ""))


class Backend:
    def __init__(self, issue):
        self.issue, self.created, self.in_progress = issue, [], []

    def get(self, key):
        return self.issue

    def get_or_create(self, key):
        return self.issue

    def create_issue(self, title, description, kind):
        self.created.append(title)
        return self.issue

    def move_to_in_progress(self, issue, custom_fields=None):
        self.in_progress.append(issue.key)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr("workflow.projects.repo_for_notesgraph_project", lambda name, pid="": None)
    monkeypatch.setattr("workflow.config.load_effective_config", lambda project=None: {})
    monkeypatch.setattr("workflow.config.is_git_enabled", lambda: False)


def job(title):
    return Job(id="j1", agent_id="a", agent_name="agent", instructions="do it",
               context=f"Note: Proposed harness changes\n\n{title}", title=title)


def test_a_job_on_a_blocked_item_fails_before_making_a_ticket(setup, monkeypatch, tmp_path):
    backend = Backend(Issue("PERSONAL-1", "x", ""))
    monkeypatch.setattr("workflow.backends.get_backend", lambda cfg, force_type=None: backend)

    with pytest.raises(session.TaskBlocked, match="is BLOCKED"):
        start_job_task(job("BLOCKED **Allow gh pr list**"), tmp_path, tmp_path)
    assert backend.created == [] and backend.in_progress == []


def test_a_reclaimed_job_whose_ticket_is_blocked_is_not_moved(setup, monkeypatch, tmp_path):
    backend = Backend(blocked_issue())
    monkeypatch.setattr("workflow.backends.get_backend", lambda cfg, force_type=None: backend)
    (tmp_path / "task.json").write_text(json.dumps({"key": "PERSONAL-9"}))

    with pytest.raises(session.TaskBlocked, match="PERSONAL-9 is BLOCKED"):
        start_job_task(job("Allow gh pr list"), tmp_path, tmp_path)
    assert backend.in_progress == []


def test_a_job_on_an_open_item_still_starts(setup, monkeypatch, tmp_path):
    backend = Backend(Issue("PERSONAL-1", "Findings", ""))
    monkeypatch.setattr("workflow.backends.get_backend", lambda cfg, force_type=None: backend)

    issue, workdir, _ = start_job_task(job("Findings"), tmp_path, tmp_path)
    assert issue.key == "PERSONAL-1" and backend.in_progress == ["PERSONAL-1"]


def cli_with(monkeypatch, backend):
    from workflow import cli

    monkeypatch.setattr(cli, "get_backend", lambda cfg: backend)
    monkeypatch.setattr(cli, "load_effective_config", lambda: {})
    monkeypatch.setattr(cli, "is_git_enabled", lambda: False)
    monkeypatch.setattr(cli, "set_current_task", lambda issue: None)
    return cli


def test_wf_start_refuses_a_blocked_task_without_moving_it(monkeypatch):
    backend = Backend(blocked_issue())
    cli = cli_with(monkeypatch, backend)

    result = CliRunner().invoke(cli.app, ["start", "PERSONAL-9"])

    assert result.exit_code == 1
    assert "Not starting PERSONAL-9" in result.output and "--force" in result.output
    assert backend.in_progress == []


def test_wf_start_force_starts_a_blocked_task(monkeypatch):
    backend = Backend(blocked_issue())
    cli = cli_with(monkeypatch, backend)

    CliRunner().invoke(cli.app, ["start", "PERSONAL-9", "--force"])

    assert backend.in_progress == ["PERSONAL-9"]
