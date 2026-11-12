"""The project a wf command acts on, inside and outside a `wf ai` session.

Precedence: a command's --project, then the session's project (WF_PROJECT, which `wf ai` / the `claude` alias put
in the session's environment), then the project whose repository holds the folder, then the global current project.
A session never switches the global current project as a side effect.
"""
from __future__ import annotations

import pytest
import yaml

from workflow import projects, session


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Two projects: `work` (Linear team PEN, one repo) and `personal` (markdown); a global state; no session."""
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
    return tmp_path


def _state(world) -> dict:
    return yaml.safe_load((world / "state.yaml").read_text()) or {}


def test_without_a_session_the_global_current_project_is_used(world):
    assert projects.session_project() is None
    assert projects.get_current_project() == "personal"
    assert projects.get_effective_config()["task_backend"] == "markdown"


def test_the_sessions_project_wins_over_the_global_current_project(world, monkeypatch):
    monkeypatch.setenv(projects.PROJECT_ENV, "work")
    assert projects.session_project() == "work"
    assert projects.get_current_project() == "work"
    cfg = projects.get_effective_config()
    assert cfg["task_backend"] == "linear" and cfg["linear"]["team"] == "PEN"
    assert projects.get_project_key("work") == "PEN"
    assert _state(world) == {"current_project": "personal"}      # the global state is not touched


def test_the_sessions_project_wins_over_the_folder_and_never_switches_the_global_state(world, monkeypatch):
    (world / "state.yaml").write_text(yaml.dump({"current_project": "work"}))
    monkeypatch.chdir(world / "robots")                            # work's repository
    monkeypatch.setenv(projects.PROJECT_ENV, "personal")           # but the session is personal's
    assert projects.get_project_for_cwd() == "personal"
    assert projects.get_effective_config()["task_backend"] == "markdown"
    assert _state(world) == {"current_project": "work"}


def test_the_folder_wins_over_the_global_current_project_outside_a_session(world, monkeypatch):
    monkeypatch.chdir(world / "robots")
    assert projects.get_project_for_cwd(switch_if_needed=False) == "work"
    assert _state(world) == {"current_project": "personal"}
    assert projects.get_project_for_cwd() == "work"                 # the default switches, as before
    assert _state(world) == {"current_project": "work"}


def test_a_command_level_project_wins_over_the_session(world, monkeypatch):
    monkeypatch.setenv(projects.PROJECT_ENV, "work")
    assert projects.get_effective_config(project="personal")["task_backend"] == "markdown"


def test_an_unknown_session_project_is_ignored(world, monkeypatch):
    monkeypatch.setenv(projects.PROJECT_ENV, "nope")
    assert projects.session_project() is None
    assert projects.get_current_project() == "personal"


def test_wf_ai_pins_the_project_it_starts_the_session_for(world, monkeypatch):
    monkeypatch.setattr("workflow.claude_accounts.apply", lambda env, project=None: None)
    monkeypatch.setattr("workflow.ai_providers.base.apply_proxy_env", lambda env: {})
    env = {}
    applied = session.apply_claude_env(env, "work")                 # wf ai --project work / --here in work's repo
    assert env[projects.PROJECT_ENV] == "work" and applied[projects.PROJECT_ENV] == "work"
    env = {}
    session.apply_claude_env(env, None)                             # plain wf ai: the current project, pinned
    assert env[projects.PROJECT_ENV] == "personal"
    monkeypatch.setenv(projects.PROJECT_ENV, "work")                # a nested session inherits its parent's
    env = {}
    session.apply_claude_env(env, None)
    assert env[projects.PROJECT_ENV] == "work"
