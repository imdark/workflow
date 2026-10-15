"""Tests for proposing, promoting and exporting skill drafts."""

import json

import pytest

from workflow import skill_drafts
from workflow.conversation_store import build_store
from workflow.conversation_store.base import SessionMeta, Summary, Turn

SKILL = {"name": "Deploy Prod", "description": "ship to prod", "use_when": "a release",
         "not_when": "a hotfix", "steps": ["build", "push"], "verify": "health is green",
         "evidence": "two releases"}


@pytest.fixture
def store(tmp_path):
    s = build_store("sqlite", {"path": str(tmp_path / "conv.db")})
    yield s
    s.close()


@pytest.fixture
def config_file(monkeypatch):
    cfg = {}
    monkeypatch.setattr("workflow.config.load_config", lambda: cfg)
    monkeypatch.setattr("workflow.config.save_config", lambda c: cfg.update(c))
    return cfg


def _sessions(store, n):
    for i in range(n):
        store.save_summary(Summary(level="session", session_id=f"s{i}", task_key="T-1", content=f"did {i}"))


def test_propose_writes_drafts_and_skips_known(store, tmp_path, monkeypatch):
    _sessions(store, 2)
    prompts = []
    monkeypatch.setattr("workflow.ladder._run_llm",
                        lambda prompt, cfg: prompts.append(prompt) or json.dumps([SKILL, {**SKILL, "name": "no steps", "steps": []}]))

    drafts = skill_drafts.propose_skills(store, {}, directory=tmp_path)

    assert [d["name"] for d in drafts] == ["deploy-prod"]
    fields, body = skill_drafts.parse_frontmatter((tmp_path / "deploy-prod.md").read_text())
    assert (fields["status"], fields["version"]) == ("draft", "1")
    assert "## When not to use\na hotfix" in body and "1. build\n2. push" in body

    assert skill_drafts.propose_skills(store, {}, directory=tmp_path) == []
    assert "- deploy-prod" in prompts[-1]


def test_propose_needs_two_sessions(store, tmp_path, monkeypatch):
    _sessions(store, 1)
    monkeypatch.setattr("workflow.ladder._run_llm", lambda *a: pytest.fail("should not call the model"))
    assert skill_drafts.propose_skills(store, {}, directory=tmp_path) == []


def test_promote_and_export(tmp_path, config_file):
    (tmp_path / "deploy-prod.md").write_text(skill_drafts.render_skill({**SKILL, "name": "deploy-prod"}))

    skill_drafts.promote("deploy-prod", project="web", directory=tmp_path)

    assert "## Steps" in config_file["ai_skills"]["projects"]["web"]["deploy-prod"]
    fields, _ = skill_drafts.parse_frontmatter((tmp_path / "deploy-prod.md").read_text())
    assert (fields["status"], fields["version"]) == ("active", "2")

    out = skill_drafts.export_skill_md("deploy-prod", tmp_path / "skills", directory=tmp_path)
    assert out.read_text().startswith("---\nname: deploy-prod\ndescription: ship to prod\n---")

    with pytest.raises(FileNotFoundError):
        skill_drafts.promote("missing", directory=tmp_path)


def test_skill_usage_counts_loads_per_session(store):
    store.ensure_session(SessionMeta(session_id="s1"))
    call = json.dumps({"skill": "plugin:deploy-prod"})
    store.append_turns("s1", [Turn(session_id="s1", seq=0, role="tool_call", content=call, tool_name="Skill")])

    assert skill_drafts.skill_usage(store)["deploy-prod"] == 1
