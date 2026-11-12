"""`wf task create` creates the task in the project's own backend, under that backend's key.

A Linear project's task is a Linear issue in its team (PEN-7), a markdown project's a file keyed by the project's
name. The project comes from --project, else the session's (WF_PROJECT), else the folder's, else the current one.
"""
from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner

from workflow import cli, projects
from workflow.backends.base import Issue

runner = CliRunner()


class FakeBackend:
    """Records what the command asked of the backend; the key prefix tells which store it went to."""

    def __init__(self, cfg, templates=False):
        self.cfg, self.created, self.updated = cfg, [], []
        if templates:                         # only the markdown backend has templates and update_task
            self.create_from_template = lambda template, title, project=None: Issue(f"{project}-T", title, "")
            self.update_task = self._update_task

    def create_issue(self, summary, description, issue_type="Task", project_key=None):
        self.created.append({"summary": summary, "description": description, "issue_type": issue_type,
                             "project_key": project_key})
        return Issue(f"{(project_key or 'TASK').upper()}-7", summary, description)

    def _update_task(self, key, updates):
        self.updated.append((key, updates))
        return True


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr("workflow.config.CFG", cfg_path)
    robots = tmp_path / "robots"
    robots.mkdir()
    (tmp_path / "elsewhere").mkdir()
    cfg_path.write_text(yaml.dump({
        "task_backend": "markdown",
        "projects": {
            "work": {"name": "PEN", "task_backend": "linear", "linear": {"team": "PEN"},
                     "repositories": {str(robots): {"base_branch": "main"}}},
            "personal": {"name": "PERSONAL", "task_backend": "markdown", "repositories": {}},
        },
    }))
    state = tmp_path / "state.yaml"
    monkeypatch.setattr("workflow.state.STATE", state)
    state.write_text(yaml.dump({"current_project": "personal"}))
    monkeypatch.delenv(projects.PROJECT_ENV, raising=False)
    monkeypatch.chdir(tmp_path / "elsewhere")
    made = []

    def get_backend(cfg, force_type=None):
        b = FakeBackend(cfg, templates=(force_type or cfg.get("task_backend")) == "markdown")
        made.append((force_type or cfg.get("task_backend"), b))
        return b

    monkeypatch.setattr(cli, "get_backend", get_backend)
    return type("W", (), {"root": tmp_path, "made": made})()


def _create(*args):
    return runner.invoke(cli.task_app, ["create", "E2E harness", "-d", "why", *args])


def test_project_option_creates_in_that_projects_backend_under_its_team_key(world):
    r = _create("--project", "work")
    assert r.exit_code == 0, r.output
    kind, backend = world.made[-1]
    assert kind == "linear" and backend.cfg["linear"]["team"] == "PEN"
    assert backend.created == [{"summary": "E2E harness", "description": "why", "issue_type": "Task",
                                "project_key": "PEN"}]
    assert "Created task: PEN-7" in r.output and "Project: work (linear)" in r.output


def test_the_sessions_project_is_used_in_any_folder(world, monkeypatch):
    monkeypatch.setenv(projects.PROJECT_ENV, "work")               # what wf ai / claude put in the session
    r = _create()
    assert r.exit_code == 0, r.output
    assert world.made[-1][0] == "linear" and world.made[-1][1].created[0]["project_key"] == "PEN"
    assert "PEN-7" in r.output


def test_the_folders_project_is_used_over_the_current_one(world, monkeypatch):
    monkeypatch.chdir(world.root / "robots")
    r = _create()
    assert r.exit_code == 0, r.output
    assert world.made[-1][0] == "linear" and "PEN-7" in r.output


def test_a_markdown_project_keeps_its_name_as_the_key_and_takes_labels(world):
    r = _create("--project", "personal", "--labels", "a, b", "--priority", "High")
    assert r.exit_code == 0, r.output
    kind, backend = world.made[-1]
    assert kind == "markdown" and backend.created[0]["project_key"] == "personal"
    assert backend.updated == [("PERSONAL-7", {"labels": ["a", "b"], "priority": "High"})]


def test_labels_on_a_linear_project_are_said_to_be_unsupported_not_dropped_silently(world):
    r = _create("--project", "work", "--labels", "x")
    assert r.exit_code == 0 and "labels and priority are set on markdown tasks only" in r.output


def test_an_unknown_project_is_refused_before_anything_is_created(world):
    r = _create("--project", "nope")
    assert r.exit_code == 1 and "No project named 'nope'" in r.output and world.made == []


def test_a_template_on_a_linear_project_is_refused(world):
    r = _create("--project", "work", "--template", "bug")
    assert r.exit_code == 1 and "Templates exist for markdown projects only" in r.output
    r = _create("--project", "personal", "--template", "bug")
    assert r.exit_code == 0 and "personal-T" in r.output
