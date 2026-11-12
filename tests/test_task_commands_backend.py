"""`wf task show / update / transition / comment` act in the task's project's own backend.

They used to open the markdown store whatever the project: a PAN-1133 that `wf task create` made in Linear read
"not found", and an update or a comment never reached Linear. They now resolve the project as `task create` does
(--project, else the session's WF_PROJECT, else the folder's, else the current one), and the Linear backend answers
the same methods the markdown one does.
"""
from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner

from tests.test_linear_backend import ISSUE_NODE, FakeClient, make_backend
from workflow import cli, projects

runner = CliRunner()


class FakeTask:
    def __init__(self, key, title="Fleet e2e flows", url=None, file_path=None):
        self.key, self.title, self.url, self.file_path = key, title, url, file_path
        self.status, self.issue_type, self.priority = "Committed", "Task", "Medium"
        self.assignee = self.reporter = self.due_date = None
        self.labels, self.components, self.fix_versions = [], [], []
        self.created_date = self.updated_date = "2026-11-12"
        self.description = "why"


class FakeBackend:
    """Records what each command asked of which store. Linear looks one issue up; markdown scans its files."""

    def __init__(self, kind):
        self.kind, self.updated, self.moved, self.comments = kind, [], [], []
        if kind == "linear":
            self.get_task = lambda key: (FakeTask("PEN-7", url="https://linear.app/x/issue/PEN-7")
                                         if key.upper() == "PEN-7" else None)

    def list_tasks(self, project_name=None, status=None):
        return [FakeTask("PERSONAL-1", file_path="/tmp/PERSONAL-1.md")] if self.kind == "markdown" else []

    def update_task(self, key, updates):
        if key.upper() == "BOOM-1":
            raise RuntimeError("Issue 'BOOM-1' does not exist or you do not have permission to see it.")
        self.updated.append((key, updates))
        return True

    def get_available_transitions(self, key):
        return ["Todo", "In Progress", "Done"]

    def transition_task(self, key, status):
        self.moved.append((key, status))
        return True

    def add_comment(self, key, text, author=None):
        self.comments.append((key, text))
        return {"id": "c-1", "text": text, "created": "2026-11-12T10:00:00Z"}

    def get_comments(self, key):
        return [{"id": "c-1", "author": "Michael", "text": t, "created": "2026-11-12T10:00:00Z"}
                for k, t in self.comments if k == key]


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
    stores = {"linear": FakeBackend("linear"), "markdown": FakeBackend("markdown")}
    used = []

    def get_backend(cfg, force_type=None):
        kind = force_type or cfg.get("task_backend")
        used.append(kind)
        return stores[kind]

    monkeypatch.setattr(cli, "get_backend", get_backend)
    return type("W", (), {"root": tmp_path, "robots": robots, "linear": stores["linear"],
                          "markdown": stores["markdown"], "used": used})()


def _wf(*args):
    return runner.invoke(cli.task_app, list(args))


def test_show_finds_a_linear_task_in_its_projects_backend(world):
    r = _wf("show", "PEN-7", "--project", "work")
    assert r.exit_code == 0, r.output
    assert world.used == ["linear"]
    assert "Task: PEN-7" in r.output and "Fleet e2e flows" in r.output
    assert "URL: https://linear.app/x/issue/PEN-7" in r.output and "File:" not in r.output


def test_without_project_the_session_then_the_folder_then_the_current_one_decide(world, monkeypatch):
    r = _wf("show", "PEN-7")                                        # nothing says work: the current project
    assert world.used[-1] == "markdown" and "not found in personal (markdown)" in r.output
    monkeypatch.setenv(projects.PROJECT_ENV, "work")                # the session wf ai started
    assert _wf("show", "PEN-7").exit_code == 0 and world.used[-1] == "linear"
    monkeypatch.delenv(projects.PROJECT_ENV)
    monkeypatch.chdir(world.robots)                                 # a work repository
    assert "Task: PEN-7" in _wf("show", "PEN-7").output and world.used[-1] == "linear"


def test_update_and_a_comment_reach_linear(world):
    r = _wf("update", "PEN-7", "--title", "Fleet e2e: policy execution, rig launcher", "--project", "work")
    assert r.exit_code == 0, r.output
    assert world.linear.updated == [("PEN-7", {"title": "Fleet e2e: policy execution, rig launcher"})]
    r = _wf("comment", "add", "PEN-7", "Split 2026-11-12", "--project", "work")
    assert r.exit_code == 0 and "Added comment #c-1 to task PEN-7" in r.output, r.output
    assert world.linear.comments == [("PEN-7", "Split 2026-11-12")] and world.markdown.comments == []
    assert "Split 2026-11-12" in _wf("comment", "list", "PEN-7", "--project", "work").output


def test_transition_names_the_state_in_any_case(world):
    r = _wf("transition", "PEN-7", "done", "--project", "work")
    assert r.exit_code == 0, r.output
    assert world.linear.moved == [("PEN-7", "done")]
    r = _wf("transition", "PEN-7", "Shipped", "--project", "work")
    assert "Invalid transition. Available: Todo, In Progress, Done" in r.output and len(world.linear.moved) == 1


def test_a_markdown_project_keeps_its_store(world):
    r = _wf("show", "PERSONAL-1", "--project", "personal")
    assert r.exit_code == 0 and world.used == ["markdown"], r.output
    assert "Task: PERSONAL-1" in r.output and "File: /tmp/PERSONAL-1.md" in r.output


def test_an_unknown_project_and_a_backend_error_say_so(world):
    r = _wf("show", "PEN-7", "--project", "nope")
    assert r.exit_code == 1 and "No project named 'nope'" in r.output
    r = _wf("update", "BOOM-1", "--title", "x", "--project", "work")
    assert r.exit_code == 1 and "does not exist or you do not have permission" in r.output, r.output


# ---------------------------------------------------------------- the Linear backend's side of those commands

def _linear(monkeypatch, extra=None):
    """A backend over a FakeClient that knows ENG-123 (ISSUE_NODE) and answers the mutations these methods send."""
    def handler(document, variables):
        if "issueUpdate" in document:
            return {"issueUpdate": {"success": True, "issue": ISSUE_NODE}}
        if "commentCreate" in document:
            return {"commentCreate": {"success": True, "comment": {"id": "cm-1", "body": variables["input"]["body"],
                                                                   "createdAt": "2026-11-12T10:00:00.000Z"}}}
        if "comments(" in document:
            return {"issue": {"comments": {"nodes": [
                {"id": "cm-2", "body": "second", "createdAt": "2026-11-12T11:00:00.000Z", "user": {"name": "b"}},
                {"id": "cm-1", "body": "first", "createdAt": "2026-11-12T10:00:00.000Z",
                 "user": {"name": "mk", "displayName": "Michael Kris"}}]}}}
        if "issue(id:" in document:
            return {"issue": ISSUE_NODE if variables.get("id") in ("ENG-123", "issue-uuid") else None}
        if extra:
            return extra(document, variables)
        raise AssertionError(f"unexpected Linear call: {document.strip()[:100]}")
    client = FakeClient(handler=handler)
    return make_backend(monkeypatch, client), client


def _sent(client, root):
    return [v for d, v in client.calls if root in d]


def test_linear_get_task_and_a_missing_issue(monkeypatch):
    backend, _ = _linear(monkeypatch)
    task = backend.get_task("ENG-123")
    assert task.key == "ENG-123" and task.title == "Fix the servo preflight" and task.url.endswith("preflight")
    assert backend.get_task("ENG-999") is None


def test_linear_update_task_maps_the_markdown_vocabulary(monkeypatch):
    backend, client = _linear(monkeypatch)
    assert backend.update_task("ENG-123", {"title": "New title", "priority": "urgent"}) is True
    (sent,) = _sent(client, "issueUpdate")
    assert sent == {"id": "issue-uuid", "input": {"title": "New title", "priority": 1}}
    with pytest.raises(ValueError, match="unknown priority"):
        backend.update_task("ENG-123", {"priority": "whenever"})
    assert backend.update_task("ENG-123", {}) is False and len(_sent(client, "issueUpdate")) == 1


def test_linear_transition_task_by_state_name(monkeypatch):
    backend, client = _linear(monkeypatch)
    assert backend.transition_task("ENG-123", "in review") is True
    assert _sent(client, "issueUpdate")[-1] == {"id": "issue-uuid", "input": {"stateId": "s-review"}}
    assert backend.transition_task("ENG-123", "Shipped") is False
    assert len(_sent(client, "issueUpdate")) == 1
