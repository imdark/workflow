"""Tests for mining sessions and proposing harness changes."""

import json

import pytest

from workflow import learn
from workflow.conversation_store import build_store
from workflow.conversation_store.base import SessionMeta, Turn


@pytest.fixture
def store(tmp_path):
    s = build_store("sqlite", {"path": str(tmp_path / "conv.db")})
    yield s
    s.close()


def _session(store, sid, turns):
    store.ensure_session(SessionMeta(session_id=sid, task_key="T-1"))
    store.append_turns(sid, [Turn(session_id=sid, seq=i, role=r, content=c, tool_name=tool, tool_call_id=cid)
                        for i, (r, c, tool, cid) in enumerate(turns)])


def _failing(n):
    out = []
    for i in range(n):
        out.append(("tool_call", "ls /nope", "Bash", f"c{i}"))
        out.append(("tool_result", "ls: No such file or directory", None, f"c{i}"))
    return out


def test_scan_finds_failing_loop_and_repeated_question(store):
    _session(store, "s1", [("user", "where is the config file", None, None)] + _failing(3))
    _session(store, "s2", [("user", "Where is the config file", None, None)])
    found = learn.scan(store)
    assert [(l["kind"], l["failed"]) for l in found["loops"]] == [("failing-loop", 3)]
    assert found["repeated_questions"][0]["sessions"] == ["s1", "s2"]


def test_two_repeats_is_not_a_loop(store):
    _session(store, "s1", _failing(2))
    assert not learn.has_findings(learn.scan(store))


def test_propose_filters_and_flags_untrusted(store, monkeypatch):
    _session(store, "s1", [("tool_call", "http://x", "WebFetch", "w")] + _failing(3))
    reply = json.dumps([
        {"target": "agent_rules", "change": "Config lives in ~/.wf", "why": "w", "evidence": "s1 loop"},
        {"target": "bogus", "change": "x"},
        {"target": "skill", "change": ""},
    ])
    monkeypatch.setattr("workflow.ladder._run_llm", lambda prompt, cfg: reply)
    result = learn.propose_changes(store, {})
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["untrusted"] is True
    assert "review closely" in learn.render_proposals(result["proposals"], result["found"])


def test_no_findings_skips_model(store, monkeypatch):
    monkeypatch.setattr("workflow.ladder._run_llm", lambda *a: pytest.fail("no model call"))
    assert learn.propose_changes(store, {})["proposals"] == []
