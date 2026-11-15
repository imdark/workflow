"""A task names the repo it belongs in (`#repo:<name>`), and both ways of
starting it -- `wf start` and an agent job -- go there or refuse.

Tasks for the notes-graph app kept being started in the workflow repo; each
run saw it was the wrong repo, set the task BLOCKED and stopped, and the next
start landed in the same wrong checkout.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from workflow import session
from workflow.backends.base import Issue
from workflow.deploy.jobs import Job, job_home, start_job_task


@pytest.mark.parametrize("text, name", [
    ("Capture cold start #repo:notes-graph", "notes-graph"),
    ("Under: Proposed harness changes\nProperties: repo:notes-graph\n\nCapture", "notes-graph"),
    ("Capture cold start\n- Needs repo: notes-graph.", "notes-graph"),
    ("Capture cold start #personal", None),
    ("see issue#repo:x, a word that isn't a tag", None),
    ("which repo: this one?", None),
])
def test_the_repo_field_is_read_from_the_tasks_text(text, name):
    assert session.needed_repo(text) == name


def test_the_repo_field_wins_over_the_notesgraph_project(monkeypatch):
    seen = []
    monkeypatch.setattr("workflow.projects.repo_for_notesgraph_project",
                        lambda name, pid="": seen.append(name) or ("personal", f"/code/{name}"))
    item = "Project: personal (projectId: p-1)\n\nCapture cold start #repo:notes-graph"
    assert session.task_home(item) == ("personal", "/code/notes-graph")
    assert seen == ["notes-graph"]


def test_a_repo_that_isnt_configured_is_refused_by_name(monkeypatch):
    monkeypatch.setattr("workflow.projects.repo_for_notesgraph_project", lambda name, pid="": None)
    with pytest.raises(session.RepoNotHere, match="needs the notes-graph repo"):
        session.task_home("Capture cold start #repo:notes-graph")
    # Only a named repo refuses; an unmatched project falls back as before.
    assert session.task_home("Project: misc (projectId: p-2)\n\nTidy up") is None


class Backend:
    def __init__(self, issue):
        self.issue, self.created, self.in_progress = issue, [], []

    def get_or_create(self, key):
        return self.issue

    def create_issue(self, title, description, kind):
        self.created.append(title)
        return self.issue

    def move_to_in_progress(self, issue, custom_fields=None):
        self.in_progress.append(issue.key)


@pytest.fixture
def no_notes_graph(monkeypatch):
    monkeypatch.setattr("workflow.projects.repo_for_notesgraph_project", lambda name, pid="": None)
    monkeypatch.setattr("workflow.config.load_effective_config", lambda project=None: {})
    monkeypatch.setattr("workflow.config.is_git_enabled", lambda: True)


def test_a_job_for_a_repo_this_machine_lacks_fails_before_making_a_ticket(no_notes_graph, monkeypatch, tmp_path):
    backend = Backend(Issue("PERSONAL-1", "Capture cold start", ""))
    monkeypatch.setattr("workflow.backends.get_backend", lambda cfg, force_type=None: backend)
    job = Job(id="j1", agent_id="a", agent_name="agent", instructions="do it",
              context="Capture cold start #repo:notes-graph", title="Capture cold start")

    with pytest.raises(session.RepoNotHere):
        start_job_task(job, tmp_path, tmp_path)
    assert backend.created == [] and backend.in_progress == []
    # Jobs that don't work in a repo (research, profiled) aren't refused for it.
    assert job_home(job) is None


def test_wf_start_refuses_a_task_for_another_repo_without_moving_it(no_notes_graph, monkeypatch):
    from workflow import cli

    backend = Backend(Issue("PERSONAL-2", "Capture hand-off", "Capture hand-off #repo:notes-graph"))
    monkeypatch.setattr(cli, "get_backend", lambda cfg: backend)
    monkeypatch.setattr(cli, "load_effective_config", lambda: {})
    monkeypatch.setattr(cli, "is_git_enabled", lambda: True)
    monkeypatch.setattr(cli, "set_current_task", lambda issue: pytest.fail("must not become current"))

    result = CliRunner().invoke(cli.app, ["start", "PERSONAL-2"])

    assert result.exit_code == 1
    assert "Not starting PERSONAL-2" in result.output and "notes-graph repo" in result.output
    assert backend.in_progress == []


def test_wf_start_goes_to_the_repo_the_task_names(monkeypatch, tmp_path):
    from workflow import cli

    target = tmp_path / "notes-graph"
    target.mkdir()
    backend = Backend(Issue("PERSONAL-3", "Capture", "Capture #repo:notes-graph"))
    monkeypatch.setattr(cli, "get_backend", lambda cfg: backend)
    monkeypatch.setattr(cli, "load_effective_config", lambda: {})
    monkeypatch.setattr(cli, "is_git_enabled", lambda: True)
    monkeypatch.setattr(cli, "set_current_task", lambda issue: None)
    monkeypatch.setattr("workflow.projects.repo_for_notesgraph_project",
                        lambda name, pid="": ("personal", str(target)))
    asked = []
    monkeypatch.setattr("workflow.session.configured_repos",
                        lambda project=None: asked.append(project) or {str(target): {}})
    # Stop at the base-branch step: by then the repo has been chosen.
    monkeypatch.setattr("workflow.session.base_branch_for",
                        lambda repo_path, repos, base=None: (_ for _ in ()).throw(
                            session.BaseBranchNotConfigured(f"chose {repo_path}")))
    monkeypatch.setattr("workflow.pid_manager.get_active_ai_sessions", lambda: [])
    monkeypatch.setattr("workflow.pid_manager.is_ai_active", lambda path: False)

    result = CliRunner().invoke(cli.app, ["start", "PERSONAL-3"])

    assert f"Using repository (the task's own): {target}" in result.output
    assert f"chose {target}" in result.output
    assert asked == ["personal"] and backend.in_progress == ["PERSONAL-3"]
