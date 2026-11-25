"""`wf task create` creates the task in the project's own backend, under that backend's key.

A Linear project's task is a Linear issue in its team (PEN-7), a markdown project's a file keyed by the project's
name. The project comes from --project, else the session's (WF_PROJECT), else the folder's, else the current one.
"""
from __future__ import annotations

import json
from datetime import date

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
    for name in ("NOTESGRAPH_WORKSPACE", "NOTESGRAPH_URL", "NOTESGRAPH_TOKEN"):   # never the real workspace
        monkeypatch.delenv(name, raising=False)
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


class FakeNotes:
    """A NotesGraph workspace of notes keyed by title, enough for upsert_note.

    With `journals`, it has the server's ensure_journal, which makes a missing
    day with the app's outline; without it, the tool is unknown, as on a
    server from before it.
    """

    def __init__(self, notes=None, journals=False, outline="## Work\n\n- [ ] \n\n## Inbox\n\n- [ ] \n"):
        self.notes = dict(notes or {})
        self.calls = []
        self.journals, self.outline = journals, outline

    def find(self, title):
        return {"docId": title, "title": title} if title in self.notes else None

    def read(self, doc_id):
        return self.notes[doc_id]

    def call(self, tool, args):
        self.calls.append(tool)
        if tool == "ensure_journal":
            if not self.journals:
                from workflow.deploy.notes import NotesGraphError
                raise NotesGraphError("Unknown tool: ensure_journal")
            created = args["date"] not in self.notes
            if created:
                self.notes[args["date"]] = self.outline
            return json.dumps({"docId": args["date"], "title": args["date"], "created": created})
        if tool == "create_document":
            self.notes[args["title"]] = args["content"]
            return json.dumps({"docId": args["title"]})
        if tool == "update_document":
            self.notes[args["docId"]] = args["content"]
            return "{}"
        from workflow.deploy.notes import NotesGraphError
        raise NotesGraphError(f"Unknown tool: {tool}")


class FakeBlockNotes(FakeNotes):
    """FakeNotes with list_blocks and update_block over the to-do lines of its
    notes, each block named <doc>:<line number>, its trail the heading above."""

    def call(self, tool, args):
        if tool == "list_blocks":
            self.calls.append(tool)
            tasks, trail = [], None
            for n, text in enumerate(self.notes[args["docId"]].split("\n")):
                if text.startswith("#"):
                    trail = text.lstrip("#").strip()
                elif text.startswith("- [ ]"):
                    tasks.append({"blockId": f"{args['docId']}:{n}", "text": text[5:].strip(), "trail": trail})
            return json.dumps({"tasks": tasks})
        if tool == "update_block":
            self.calls.append(tool)
            doc, n = args["blockId"].rsplit(":", 1)
            lines = self.notes[doc].split("\n")
            lines[int(n)] = f"- [ ] {args['text']}"
            self.notes[doc] = "\n".join(lines)
            return "{}"
        return super().call(tool, args)


@pytest.fixture
def notes(monkeypatch):
    fake = FakeNotes({date.today().isoformat(): "## Work\n\n- [ ] \n\n## workflow\n\n- [ ] older\n\n### todos:\n"})
    monkeypatch.setattr("workflow.cli_notes.reader_from_env", lambda: fake)
    return fake


def test_a_task_made_in_a_wf_ai_session_is_listed_in_todays_journal_under_its_repo(world, notes, monkeypatch):
    workflow_repo = world.root / "workflow"
    workflow_repo.mkdir()
    cfg = yaml.safe_load((world.root / "config.yaml").read_text())
    cfg["projects"]["personal"]["repositories"] = {str(workflow_repo): {}}
    (world.root / "config.yaml").write_text(yaml.dump(cfg))
    monkeypatch.chdir(workflow_repo)
    monkeypatch.setenv(projects.PROJECT_ENV, "personal")
    r = _create()
    assert r.exit_code == 0, r.output
    assert notes.notes[date.today().isoformat()] == (
        "## Work\n\n- [ ] \n\n## workflow\n\n- [ ] older\n- [ ] PERSONAL-7 E2E harness\n\n### todos:\n")
    assert "Journal: under 'workflow'" in r.output


def test_outside_a_session_the_journal_is_left_alone_unless_asked(world, notes):
    assert _create("--project", "personal").exit_code == 0
    assert notes.calls == []
    r = _create("--project", "personal", "--journal")
    assert r.exit_code == 0, r.output
    assert "- [ ] PERSONAL-7 E2E harness" in notes.notes[date.today().isoformat()]


def test_a_session_task_with_no_reachable_notesgraph_is_still_created(world, monkeypatch):
    monkeypatch.setenv(projects.PROJECT_ENV, "personal")
    r = _create()
    assert r.exit_code == 0 and "Created task: PERSONAL-7" in r.output
    assert "Not added to today's journal" in r.output


def test_a_template_on_a_linear_project_is_refused(world):
    r = _create("--project", "work", "--template", "bug")
    assert r.exit_code == 1 and "Templates exist for markdown projects only" in r.output
    r = _create("--project", "personal", "--template", "bug")
    assert r.exit_code == 0 and "personal-T" in r.output


@pytest.fixture
def two_repos(world):
    """personal owns notes-graph and workflow, like the real config: one wf project, two journal headings."""
    for name in ("notes-graph", "workflow"):
        (world.root / name).mkdir()
    cfg = yaml.safe_load((world.root / "config.yaml").read_text())
    cfg["projects"]["personal"]["repositories"] = {str(world.root / "notes-graph"): {},
                                                   str(world.root / "workflow"): {}}
    (world.root / "config.yaml").write_text(yaml.dump(cfg))
    return world


def test_repo_files_the_task_under_that_repos_heading_and_tags_it_to_start_there(two_repos, notes, monkeypatch):
    # Filed from a notes-graph session, for the workflow repo of the same project.
    monkeypatch.chdir(two_repos.root / "notes-graph")
    monkeypatch.setenv(projects.PROJECT_ENV, "personal")
    r = _create("--repo", "workflow")
    assert r.exit_code == 0, r.output
    day = notes.notes[date.today().isoformat()]
    assert "## workflow\n\n- [ ] older\n- [ ] PERSONAL-7 E2E harness #repo:workflow\n" in day
    assert "Journal: under 'workflow'" in r.output
    created = two_repos.made[-1][1].created[0]
    assert created["description"] == "why\n\n#repo:workflow"     # so `wf start` goes to that repo too


def test_repo_alone_picks_the_project_that_owns_it(two_repos, notes, monkeypatch):
    monkeypatch.chdir(two_repos.root / "robots")                  # a work-project folder
    r = _create("--repo", "workflow", "--journal")
    assert r.exit_code == 0, r.output
    assert two_repos.made[-1][0] == "markdown" and "PERSONAL-7" in r.output


def test_a_repo_that_isnt_configured_or_isnt_the_projects_is_refused_before_anything_is_created(two_repos):
    r = _create("--repo", "nope")
    assert r.exit_code == 1 and "No configured repo named 'nope'" in r.output
    r = _create("--repo", "workflow", "--project", "work")
    assert r.exit_code == 1 and "'workflow' belongs to project 'personal', not 'work'" in r.output
    assert two_repos.made == []


def test_a_day_with_no_journal_gets_its_page_made_first(world, monkeypatch):
    fake = FakeNotes(journals=True, outline="## Work\n\n- [ ] \n\n## workflow\n\n- [ ] \n\n## Inbox\n\n- [ ] \n")
    monkeypatch.setattr("workflow.cli_notes.reader_from_env", lambda: fake)
    workflow_repo = world.root / "workflow"
    workflow_repo.mkdir()
    cfg = yaml.safe_load((world.root / "config.yaml").read_text())
    cfg["projects"]["personal"]["repositories"] = {str(workflow_repo): {}}
    (world.root / "config.yaml").write_text(yaml.dump(cfg))

    r = _create("--project", "personal", "--journal")

    assert r.exit_code == 0, r.output
    # Made by ensure_journal with the day's outline, not as a bare page holding the line.
    assert fake.calls[:2] == ["ensure_journal", "list_blocks"] and "create_document" not in fake.calls
    assert fake.notes[date.today().isoformat()] == (
        "## Work\n\n- [ ] \n\n## workflow\n\n- [ ] PERSONAL-7 E2E harness\n\n## Inbox\n\n- [ ] \n")


# A journal page as read_document renders it: each todos: list is a database block, shown as a table.
JOURNAL_WITH_LISTS = ("## Work\n\n- [ ] \n\n### todos:\n|Status|Deadline|Scheduled|\n|---|---|---|\n\n"
                      "## workflow\n\n- [ ] \n\n### todos:\n|Status|Deadline|Scheduled|\n|---|---|---|\n")


def _workflow_world(world):
    workflow_repo = world.root / "workflow"
    workflow_repo.mkdir()
    cfg = yaml.safe_load((world.root / "config.yaml").read_text())
    cfg["projects"]["personal"]["repositories"] = {str(workflow_repo): {}}
    (world.root / "config.yaml").write_text(yaml.dump(cfg))


def test_a_journal_with_todos_lists_gets_the_headings_empty_item_filled_not_rewritten(world, monkeypatch):
    fake = FakeBlockNotes({date.today().isoformat(): JOURNAL_WITH_LISTS}, journals=True)
    monkeypatch.setattr("workflow.cli_notes.reader_from_env", lambda: fake)
    _workflow_world(world)

    r = _create("--project", "personal", "--journal")

    assert r.exit_code == 0, r.output
    assert "update_document" not in fake.calls and fake.calls.count("update_block") == 1
    assert fake.notes[date.today().isoformat()] == JOURNAL_WITH_LISTS.replace(
        "## workflow\n\n- [ ] \n", "## workflow\n\n- [ ] PERSONAL-7 E2E harness\n")
    assert "Journal: under 'workflow'" in r.output


def test_a_journal_with_todos_lists_and_no_empty_item_is_left_alone(world, monkeypatch):
    full = JOURNAL_WITH_LISTS.replace("## workflow\n\n- [ ] \n", "## workflow\n\n- [ ] older\n")
    fake = FakeBlockNotes({date.today().isoformat(): full}, journals=True)
    monkeypatch.setattr("workflow.cli_notes.reader_from_env", lambda: fake)
    _workflow_world(world)

    r = _create("--project", "personal", "--journal")

    assert r.exit_code == 0 and "Created task: PERSONAL-7" in r.output
    assert fake.notes[date.today().isoformat()] == full
    assert "update_document" not in fake.calls and "update_block" not in fake.calls
    assert "Not added to today's journal" in r.output and "no empty to-do under 'workflow'" in r.output
