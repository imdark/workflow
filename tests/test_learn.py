"""Tests for `wf learn`: detectors, proposals, the ledger and accepting a rule."""

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


class Run:
    """Builds one session's turns."""

    def __init__(self, sid):
        self.sid, self.turns, self.n = sid, [], 0

    def call(self, tool, args, result="ok", error=False):
        cid = f"{self.sid}-{self.n}"
        self.turns.append(Turn(self.sid, len(self.turns), "tool_call", json.dumps(args),
                               tool_name=tool, tool_call_id=cid))
        self.turns.append(Turn(self.sid, len(self.turns), "tool_result", result, tool_call_id=cid,
                               raw={"is_error": True} if error else None))
        self.n += 1
        return self


def _save(store, *runs):
    for run in runs:
        store.ensure_session(SessionMeta(session_id=run.sid))
        store.append_turns(run.sid, run.turns)


def test_error_signature_drops_what_varies():
    a = learn.error_signature("Exit code 1\nls: /Users/x/code/a: No such file or directory")
    b = learn.error_signature("Exit code 2\nls: ~/other/b.txt: No such file or directory")
    assert a == b == "ls: <path>: No such file or directory"
    assert learn.error_signature("Traceback (most recent call last):\n  File x\nKeyError: 'k'") == "KeyError: <q>"
    assert learn.error_signature("<tool_use_error>Error: No such tool available: Grep.</tool_use_error>") \
        == "Error: No such tool available: Grep."


def test_stuck_merges_runs_with_the_same_error():
    scans = []
    for sid in ("a", "b"):
        run = Run(sid)
        for i in range(3):
            run.call("Bash", {"command": f"adb devices {i}"}, "Exit code 127\n(eval):1: command not found: adb", error=True)
        scans.append(learn.scan_session(sid, run.turns))
    stuck = [f for f in learn.detect(scans) if f.kind == "stuck"]
    assert len(stuck) == 1
    assert stuck[0].sessions == ["a", "b"] and "command not found: adb" in stuck[0].summary


def test_recurring_error_needs_three_runs():
    def scan(sid):
        return learn.scan_session(sid, Run(sid).call(
            "Edit", {"file_path": "/x"}, "File has not been read yet.", error=True).turns)
    two = learn.detect([scan("a"), scan("b")])
    three = learn.detect([scan("a"), scan("b"), scan("c")])
    assert not [f for f in two if f.kind == "recurring_error"]
    assert [f.kind for f in three] == ["recurring_error"]


def test_successful_calls_are_not_errors():
    run = Run("a")
    for _ in range(5):
        run.call("Bash", {"command": "ls"}, "Exit code 1\nnope")  # no is_error flag
    assert learn.detect([learn.scan_session("a", run.turns)]) == []


def test_reread_resets_after_a_write():
    run = Run("a")
    run.call("Read", {"file_path": "/f"}).call("Read", {"file_path": "/f"})
    run.call("Edit", {"file_path": "/f"}).call("Read", {"file_path": "/f"})
    assert learn.detect([learn.scan_session("a", run.turns)]) == []
    run.call("Read", {"file_path": "/f"}).call("Read", {"file_path": "/f"})
    found = learn.detect([learn.scan_session("a", run.turns)])
    assert [f.kind for f in found] == ["reread"]


def test_repeated_question_across_runs_and_untrusted_flag():
    a = Run("a").call("mcp__run__ask_user", {"question": "Who is Cosmo, and how old are they?"})
    b = Run("b").call("WebFetch", {"url": "http://x"}).call(
        "mcp__run__ask_user", {"question": "who is Cosmo and how old are they"})
    found = learn.detect([learn.scan_session("a", a.turns), learn.scan_session("b", b.turns)])
    assert [f.kind for f in found] == ["repeated_question"]
    assert found[0].sessions == ["a", "b"] and found[0].untrusted


def test_scan_store_reads_recent_sessions(store):
    _save(store, Run("a").call("Read", {"file_path": "/f"}))
    scans = learn.scan_store(store, days=1)
    assert [s["session_id"] for s in scans] == ["a"]


def _finding():
    return learn.Finding("recurring_error", "Bash failed in 3 runs with: adb not found", 3,
                         ["a", "b", "c"], ["adb devices"], untrusted=True)


def test_propose_keeps_only_valid_cited_changes(monkeypatch):
    f = _finding()
    prompts = []
    reply = json.dumps([
        {"target": "agent_rules", "change": "adb is at ~/Library/Android/sdk/platform-tools/adb",
         "why": "runs could not find adb", "evidence": [f.id]},
        {"target": "agent_rules", "change": "no evidence", "evidence": []},
        {"target": "code", "change": "rewrite the app", "evidence": [f.id]},
    ])
    monkeypatch.setattr("workflow.ladder._run_llm", lambda p, c: prompts.append(p) or reply)
    out = learn.propose([f], rules="* existing rule", config={})
    assert [p["change"] for p in out] == ["adb is at ~/Library/Android/sdk/platform-tools/adb"]
    assert out[0]["untrusted"] and out[0]["sessions"] == ["a", "b", "c"]
    assert out[0]["id"].startswith("L-") and out[0]["status"] == "pending"
    assert "* existing rule" in prompts[0] and f.id in prompts[0]


def test_merge_skips_known_ids_including_rejected():
    p = {"id": learn.proposal_id("agent_rules", "Do X"), "change": "Do X"}
    ledger = [{**p, "status": "rejected"}]
    again = {"id": learn.proposal_id("agent_rules", "do x."), "change": "do x."}
    assert learn.merge(ledger, [again]) == []


def test_add_learned_rule_appends_to_section_once():
    entry = {"id": "L-abc123", "change": "Use grep via Bash", "decided": "2026-11-01"}
    text = learn.add_learned_rule("## Where things go\n\n* a\n", entry)
    assert text.endswith("## Learned\n\n* Use grep via Bash (L-abc123, learned 2026-11-01)\n")
    assert learn.add_learned_rule(text, entry) == text
    second = learn.add_learned_rule(text + "\n## Leave alone\n\n* b\n",
                                    {"id": "L-def456", "change": "Second", "decided": "2026-11-02"})
    learned = second.split("## Learned")[1].split("## Leave alone")[0]
    assert "L-abc123" in learned and "L-def456" in learned
    assert second.rstrip().endswith("* b")


class FakeReader:
    def __init__(self, notes):
        self.notes = notes  # title -> (docId, text)
        self.calls = []

    def find(self, title):
        return {"docId": self.notes[title][0], "title": title} if title in self.notes else None

    def read(self, doc_id):
        return next(t for d, t in self.notes.values() if d == doc_id)

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "create_document":
            return json.dumps({"docId": "new-doc"})
        return "{}"


def test_accept_rule_updates_agent_rules_and_note(tmp_path, monkeypatch):
    path = tmp_path / "proposals.json"
    entry = {"id": "L-abc123", "target": "agent_rules", "change": "Use grep via Bash",
             "why": "", "evidence": [], "status": "pending"}
    learn.save_ledger([entry], path)
    reader = FakeReader({"Agent rules": ("rules", "* a rule\n")})
    monkeypatch.setattr(learn, "_reader", lambda cfg: reader)
    assert learn.accept_proposal("abc123", {}, echo=lambda *a: None, ledger_path=path) == 0
    assert learn.load_ledger(path)[0]["status"] == "accepted"
    tool, args = reader.calls[0]
    assert tool == "update_document" and args["docId"] == "rules"
    assert "* Use grep via Bash (L-abc123" in args["content"]
    # The proposals note did not exist: created, then linked under Agent rules.
    assert [c[0] for c in reader.calls[1:]] == ["create_document", "link_document"]
    assert "Nothing pending." in reader.calls[1][1]["content"]


def test_reject_is_recorded(tmp_path, monkeypatch):
    path = tmp_path / "proposals.json"
    learn.save_ledger([{"id": "L-abc123", "target": "memory", "change": "x", "status": "pending"}], path)
    monkeypatch.setattr(learn, "_reader", lambda cfg: None)
    assert learn.reject_proposal("L-abc123", {}, echo=lambda *a: None, ledger_path=path) == 0
    assert learn.load_ledger(path)[0]["status"] == "rejected"
    assert learn.reject_proposal("L-nope", {}, echo=lambda *a: None, ledger_path=path) == 1


def test_render_note_flags_untrusted():
    note = learn.render_note([{"id": "L-1", "target": "agent_rules", "change": "Do X", "why": "w",
                               "evidence": ["seen"], "untrusted": True, "status": "pending"}])
    assert "- [ ] **L-1** · Agent rules: Do X ⚠️" in note and "  - Seen: seen" in note


def test_render_note_escapes_tags_outside_code():
    note = learn.render_note([{"id": "L-1", "target": "agent_rules", "change": "Set `X=<serial>`",
                               "evidence": ["ls: <path>: missing"], "status": "pending"}])
    assert "`X=<serial>`" in note and "ls: &lt;path>: missing" in note
