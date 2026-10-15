"""Tests for backfilling the conversation store from Claude Code transcripts."""

import json

import pytest

from workflow.conversation_store import build_store
from workflow.mem_import import import_transcripts, parse_transcript, task_key_from_branch, transcript_files


@pytest.fixture
def store(tmp_path):
    s = build_store("sqlite", {"path": str(tmp_path / "conv.db")})
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_projects(monkeypatch):
    monkeypatch.setattr("workflow.projects.list_projects", lambda: {"web": {"repositories": {"/src/web": {}}}})


def _write(path, *records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _record(kind, content, **extra):
    return {"type": kind, "sessionId": "abc", "timestamp": "2026-10-01T10:00:00Z",
            "cwd": "/src/web/app", "gitBranch": "personal-113-ideas-worth",
            "message": {"role": kind, "content": content, **({"model": "m"} if kind == "assistant" else {})},
            **extra}


def test_task_key_from_branch():
    assert task_key_from_branch("personal-113-ideas-worth-taking") == "PERSONAL-113"
    assert task_key_from_branch("feat/ABC-7-thing") == "ABC-7"
    assert task_key_from_branch("main") is None
    assert task_key_from_branch(None) is None


def test_parse_reads_meta_and_skips_sidechains_and_noise(tmp_path):
    path = tmp_path / "p" / "abc.jsonl"
    _write(path,
           {"type": "summary", "summary": "x"},
           _record("user", "fix the login bug"),
           _record("assistant", [{"type": "text", "text": "on it"}]),
           _record("user", "subagent chatter", isSidechain=True))

    meta, turns = parse_transcript(path)

    assert (meta.session_id, meta.task_key, meta.project, meta.model) == ("abc", "PERSONAL-113", "web", "m")
    assert [t.content for t in turns] == ["fix the login bug", "on it"]


def test_import_is_idempotent_and_filters_by_task(tmp_path, store):
    _write(tmp_path / "projects" / "d" / "abc.jsonl",
           _record("user", "fix the login bug"), _record("assistant", "done"))
    files = list(transcript_files([tmp_path / "projects"]))

    assert import_transcripts(store, files, task_key="OTHER-1") == []
    assert import_transcripts(store, files) == [("abc", "PERSONAL-113", 2)]
    assert import_transcripts(store, files) == []
    assert [s.task_key for s in store.list_sessions()] == ["PERSONAL-113"]
