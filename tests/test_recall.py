"""Tests for feeding ladder output back into agent prompts."""

import json
from types import SimpleNamespace

import pytest

from workflow import recall
from workflow.conversation_store import build_store
from workflow.conversation_store.base import SessionMeta, Summary, Turn
from workflow.proxy.sanitize import clean_text


@pytest.fixture
def store(tmp_path):
    s = build_store("sqlite", {"path": str(tmp_path / "conv.db")})
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_legacy_memory(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.memory.ROOT", tmp_path / "memory")


def _facts(store, task, *contents):
    store.save_summary(Summary(
        level="facts", task_key=task, session_id="s1",
        content=json.dumps([{"type": "decision", "content": c, "confidence": 0.9} for c in contents]),
    ))


def test_block_injects_rollup_and_facts(store):
    store.save_summary(Summary(level="rollup", task_key="T-1", content="Goal: ship it"))
    _facts(store, "T-1", "Use sqlite, not redis")

    block = recall.memory_block("T-1", store=store)

    assert block.startswith('<wf-memory task="T-1">')
    assert "Goal: ship it" in block
    assert "(decision) Use sqlite, not redis" in block
    # The proxy strips the block, so the ladder never re-learns it.
    assert clean_text(block) == ""


def test_block_is_stable_and_scoped_to_its_task(store):
    _facts(store, "T-1", "mine")
    _facts(store, "T-2", "someone else's")

    first = recall.memory_block("T-1", store=store)
    assert first == recall.memory_block("T-1", store=store)
    assert "someone else's" not in first


def test_block_dedupes_and_caps_facts(store):
    _facts(store, "T-1", *[f"fact number {i}" for i in range(recall.MAX_FACTS + 5)])
    _facts(store, "T-1", "Fact number 0!")

    block = recall.memory_block("T-1", store=store)

    assert block.count("- (decision)") == recall.MAX_FACTS
    assert "5 more: call search_task_facts" in block


def test_block_falls_back_to_mirrored_summaries(store, tmp_path):
    d = tmp_path / "memory" / "T-1"
    d.mkdir(parents=True)
    (d / "summary.md").write_text("--- earlier ---\nfixed the login bug")

    assert "fixed the login bug" in recall.memory_block("T-1", store=store)


def test_fallback_keeps_whole_sessions(store, tmp_path):
    d = tmp_path / "memory" / "T-1"
    d.mkdir(parents=True)
    old = "--- 1 ---\n" + "old " * recall.MAX_FALLBACK_CHARS
    (d / "summary.md").write_text(old + "\n--- 2 ---\nlatest work")

    block = recall.memory_block("T-1", store=store)

    assert "PREVIOUS SESSIONS:\n--- 2 ---\nlatest work" in block


def test_long_facts_respect_the_char_budget(store):
    _facts(store, "T-1", *[f"{i} " + "z" * 1000 for i in range(10)])

    block = recall.memory_block("T-1", store=store)

    assert block.count("- (decision)") < 10
    assert "more: call search_task_facts" in block


def test_block_is_empty_without_memory_or_task(store):
    assert recall.memory_block("T-1", store=store) == ""
    assert recall.memory_block(None, store=store) == ""


def test_broken_store_never_blocks_the_prompt():
    class Broken:
        def get_summaries(self, **_):
            raise RuntimeError("db locked")

    assert recall.memory_block("T-1", store=Broken()) == ""


def test_search_facts_matches_every_term(store):
    _facts(store, "T-1", "Deploy runs on the Mac agent", "Tests use a fake upstream")

    assert [f["content"] for f in recall.search_facts("mac deploy", "T-1", store=store)] == [
        "Deploy runs on the Mac agent"
    ]
    assert len(recall.search_facts("", "T-1", store=store)) == 2


def test_search_conversations_returns_snippets(store):
    store.ensure_session(SessionMeta(session_id="s1", task_key="T-1"))
    store.append_turns("s1", [Turn(session_id="s1", seq=0, role="user",
                                   content="x " * 400 + "the hostname changed under DHCP " + "y " * 400)])

    hits = recall.search_conversations("hostname", "T-1", store=store, snippet_chars=80)

    assert len(hits) == 1
    assert "hostname" in hits[0]["snippet"]
    assert len(hits[0]["snippet"]) <= 82


def test_build_context_uses_memory_block_not_dead_rag_text(monkeypatch):
    from workflow import ai_context

    monkeypatch.setattr(ai_context, "memory_block", lambda key: f'<wf-memory task="{key}">x</wf-memory>')
    monkeypatch.setattr(ai_context, "get_rag", lambda: None)
    monkeypatch.setattr(ai_context, "get_ai_instructions", lambda issue, repo: [])
    issue = SimpleNamespace(key="T-1", title="t", description="d")

    context = ai_context.build_context(issue)

    assert '<wf-memory task="T-1">x</wf-memory>' in context
    assert "RAG system" not in context
    assert "search_past_conversations" in context
