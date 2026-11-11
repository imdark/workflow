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
                               # As the proxy records an Anthropic tool_result block.
                               raw={"type": "tool_result", **({"is_error": True} if error else {})}))
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


# --- from the review of PR #51 --------------------------------------------------

def test_a_reworded_rejected_change_is_not_proposed_again(monkeypatch):
    ledger = [{"id": "L-abc123", "target": "agent_rules", "status": "rejected",
               "change": "Use grep via Bash; the Grep tool is unavailable"}]
    again = {"id": "L-zzz999", "target": "agent_rules",
             "change": "Use grep via Bash, the Grep tool is not available"}
    other = {"id": "L-yyy888", "target": "agent_rules", "change": "Run tests with pytest -q"}
    assert learn.merge(ledger, [again, other]) == [other]

    # And the model is told what was already proposed.
    prompts = []
    monkeypatch.setattr("workflow.ladder._run_llm", lambda prompt, cfg: prompts.append(prompt) or "[]")
    learn.propose([learn.Finding("stuck", "x", 3, ["a"])], ledger=ledger)
    assert "ALREADY PROPOSED" in prompts[0] and "[rejected] agent_rules: Use grep via Bash" in prompts[0]


def test_rejecting_an_accepted_rule_takes_it_out_of_agent_rules(tmp_path, monkeypatch):
    path = tmp_path / "proposals.json"
    learn.save_ledger([{"id": "L-abc123", "target": "agent_rules", "change": "Use grep via Bash",
                        "status": "accepted"}], path)
    rules = "* mine\n\n## Learned\n\n* Use grep via Bash (L-abc123, learned 2026-11-01)\n* other (L-def456, learned 2026-11-01)\n"
    reader = FakeReader({"Agent rules": ("rules", rules)})
    monkeypatch.setattr(learn, "_reader", lambda cfg: reader)
    assert learn.reject_proposal("L-abc123", {}, echo=lambda *a: None, ledger_path=path) == 0
    assert learn.load_ledger(path)[0]["status"] == "rejected"
    tool, args = reader.calls[0]
    assert tool == "update_document" and "L-abc123" not in args["content"]
    assert "* mine" in args["content"] and "L-def456" in args["content"]


def test_reading_a_file_in_parts_is_not_a_reread():
    run = Run("a")
    for offset in (0, 2000, 4000):
        run.call("Read", {"file_path": "/big.py", "offset": offset, "limit": 2000})
    assert learn.detect([learn.scan_session("a", run.turns)]) == []


@pytest.mark.parametrize("write", [
    ("Bash", {"command": "sed -i '' s/a/b/ /f.py"}),
    ("Bash", {"command": "black f.py"}),
    ("MultiEdit", {"file_path": "/f.py", "edits": []}),
])
def test_a_write_by_other_means_resets_the_reread_count(write):
    run = Run("a")
    run.call("Read", {"file_path": "/f.py"}).call("Read", {"file_path": "/f.py"})
    run.call(*write)
    run.call("Read", {"file_path": "/f.py"}).call("Read", {"file_path": "/f.py"})
    assert learn.detect([learn.scan_session("a", run.turns)]) == []


def test_errors_from_runs_with_no_error_flag_are_seen():
    """OpenAI chat and Responses results carry no is_error."""
    turns = []
    for i in range(3):
        turns += [Turn("a", len(turns), "tool_call", json.dumps({"command": "adb devices"}),
                       tool_name="shell", tool_call_id=f"c{i}"),
                  Turn("a", len(turns) + 1, "tool_result", "zsh: command not found: adb",
                       tool_call_id=f"c{i}", raw={"type": "function_call_output", "call_id": f"c{i}"})]
    assert [f.kind for f in learn.detect([learn.scan_session("a", turns)])] == ["stuck"]
    ok = Turn("a", 0, "tool_result", "3 errors found in the log", raw={"type": "function_call_output"})
    assert not learn._is_error(ok)


@pytest.mark.parametrize("item", [
    {"target": "agent_rules", "change": {"text": "x"}, "evidence": ["F-?"]},
    {"target": "agent_rules", "change": "x", "evidence": [["F-?"]]},
    {"target": "agent_rules", "change": "x", "evidence": {"id": "F-?"}},
    {"target": "agent_rules", "change": "x", "why": ["no"], "evidence": "F-?"},
])
def test_odd_model_output_is_skipped_not_a_crash(monkeypatch, item):
    finding = learn.Finding("stuck", "x", 3, ["a"])
    item = json.loads(json.dumps(item).replace("F-?", finding.id))
    good = {"target": "memory", "change": "y", "evidence": [finding.id]}
    monkeypatch.setattr("workflow.ladder._run_llm", lambda prompt, cfg: json.dumps([item, good]))
    out = learn.propose([finding])
    assert out[-1]["change"] == "y"
    # A bare id string is taken as one id, not its characters.
    if isinstance(item["evidence"], str):
        assert out[0]["change"] == "x" and out[0]["why"] == ""


@pytest.mark.parametrize("args", [
    {"questions": ["Which branch?"]},
    {"question": {"text": "Which branch?"}},
    {"questions": [None]},
])
def test_odd_question_shapes_do_not_stop_the_scan(args):
    run = Run("a").call("AskUserQuestion", args)
    run.call("Bash", {"command": ["not", "a", "string"]}, "Error: boom", error=True)
    learn.scan_session("a", run.turns)


def test_one_bad_transcript_does_not_stop_the_others(store, monkeypatch):
    _save(store, Run("a").call("Bash", {"command": "ls"}), Run("b").call("Bash", {"command": "ls"}))
    real = learn.scan_session
    monkeypatch.setattr(learn, "scan_session",
                        lambda sid, turns: (_ for _ in ()).throw(ValueError()) if sid == "a" else real(sid, turns))
    assert [s["session_id"] for s in learn.scan_store(store)] == ["b"]


def test_learned_goes_under_the_real_heading_only():
    rules = "* a\n\n## Tools\n\n### Learned shortcuts\n\n* s\n\n## Leave alone\n\n* b\n"
    out = learn.add_learned_rule(rules, {"id": "L-abc123", "change": "Do X", "decided": "2026-11-01"})
    assert "### Learned shortcuts\n\n* s\n\n## Leave alone" in out
    assert out.rstrip().endswith("## Learned\n\n* Do X (L-abc123, learned 2026-11-01)")


def test_a_stuck_error_is_not_also_reported_as_recurring():
    scans = []
    for sid in ("a", "b", "c"):
        run = Run(sid)
        for _ in range(3):
            run.call("Bash", {"command": "adb devices"}, "command not found: adb", error=True)
        scans.append(learn.scan_session(sid, run.turns))
    assert [f.kind for f in learn.detect(scans)] == ["stuck"]


# --- scoped rules: "## For project/tag/agent: …" -------------------------------

def test_propose_keeps_a_valid_scope_and_drops_a_bad_one(monkeypatch):
    f = _finding()
    prompts = []
    reply = json.dumps([
        {"target": "agent_rules", "change": "adb is not on PATH; use scripts/mobile-emulator.sh",
         "evidence": [f.id], "scope": {"kind": "tag", "value": "#mobile"}},
        {"target": "agent_rules", "change": "Use rg", "evidence": [f.id],
         "scope": {"kind": "folder", "value": "x"}},
        {"target": "agent_rules", "change": "Use pytest -q", "evidence": [f.id], "scope": None},
        {"target": "memory", "change": "Cosmo is 6", "evidence": [f.id],
         "scope": {"kind": "agent", "value": "Researcher"}},
    ])
    monkeypatch.setattr("workflow.ladder._run_llm", lambda p, c: prompts.append(p) or reply)
    out = learn.propose([f], config={})
    assert out[0]["scope"] == {"kind": "tag", "value": "mobile"}
    assert all("scope" not in p for p in out[1:])
    assert '"scope"' in prompts[0] and "For tag: X" in prompts[0]


def test_parse_scope_wants_a_known_kind_and_one_value():
    assert learn.parse_scope({"kind": "Project", "value": " notes  graph "}) == \
        {"kind": "project", "value": "notes graph"}
    assert learn.parse_scope({"kind": "tag", "value": "a, b"}) is None
    assert learn.parse_scope({"kind": "tag", "value": ""}) is None
    assert learn.parse_scope("tag:mobile") is None


def scoped(kind, value, pid="L-abc123", change="Do X"):
    return {"id": pid, "change": change, "decided": "2026-11-01",
            "scope": {"kind": kind, "value": value}}


def test_a_scoped_rule_goes_last_in_its_own_section():
    rules = ("* a\n\n## For tag: mobile\n\n* m1\n* m2\n\n### Details\n\n* d\n\n"
             "## For project: notes-graph\n\n* p1\n\n## Learned\n\n* l1\n")
    out = learn.add_learned_rule(rules, scoped("tag", "Mobile"))
    assert "* m2\n* Do X (L-abc123, learned 2026-11-01)\n\n### Details" in out
    out = learn.add_learned_rule(out, scoped("project", "Notes Graph", "L-def456", "Use rg"))
    assert "* p1\n* Use rg (L-def456, learned 2026-11-01)\n\n## Learned" in out
    assert out.endswith("* l1\n")
    assert learn.add_learned_rule(out, scoped("tag", "mobile")) == out


def test_a_scoped_rule_with_no_section_gets_one_at_the_end():
    rules = "* a\n\n## For tag: mobile, android\n\n* m1\n\n## Learned\n\n* l1\n"
    out = learn.add_learned_rule(rules, scoped("tag", "android"))
    # Not into the shared mobile/android section: the rule was for android only.
    assert out == rules.rstrip("\n") + "\n\n## For tag: android\n\n* Do X (L-abc123, learned 2026-11-01)\n"
    out = learn.add_learned_rule(rules, scoped("agent", "Researcher"))
    assert out.endswith("## For agent: Researcher\n\n* Do X (L-abc123, learned 2026-11-01)\n")


def test_a_scoped_rule_skips_nested_and_fenced_lookalikes():
    rules = ("## For project: wf\n\n### For tag: mobile\n\n* nested\n\n"
             "```\n## For tag: mobile\n```\n")
    out = learn.add_learned_rule(rules, scoped("tag", "mobile"))
    assert out.endswith("```\n\n## For tag: mobile\n\n* Do X (L-abc123, learned 2026-11-01)\n")


def test_an_empty_scoped_section_gets_the_rule_under_its_heading():
    out = learn.add_learned_rule("## For agent: Researcher\n\n## Learned\n", scoped("agent", "researcher"))
    assert out == "## For agent: Researcher\n* Do X (L-abc123, learned 2026-11-01)\n\n## Learned\n"


def test_a_scoped_rule_is_removed_from_wherever_it_is():
    rules = "* a\n\n## For tag: mobile\n\n* m1\n* Do X (L-abc123, learned 2026-11-01)\n\n## Learned\n\n* l1\n"
    out = learn.remove_learned_rule(rules, "L-abc123")
    assert "L-abc123" not in out and "* m1" in out and "* l1" in out


def test_accepting_a_scoped_rule_files_it_under_its_heading(tmp_path, monkeypatch):
    path = tmp_path / "proposals.json"
    learn.save_ledger([{"id": "L-abc123", "target": "agent_rules", "change": "adb is not on PATH",
                        "evidence": [], "status": "pending",
                        "scope": {"kind": "tag", "value": "mobile"}}], path)
    reader = FakeReader({"Agent rules": ("rules", "* a rule\n")})
    monkeypatch.setattr(learn, "_reader", lambda cfg: reader)
    said = []
    assert learn.accept_proposal("L-abc123", {}, echo=said.append, ledger_path=path) == 0
    content = reader.calls[0][1]["content"]
    assert "## For tag: mobile\n\n* adb is not on PATH (L-abc123, learned" in content
    assert "## Learned" not in content
    assert any("under For tag: mobile" in s for s in said)
    assert learn.load_ledger(path)[0]["scope"] == {"kind": "tag", "value": "mobile"}


def test_render_note_shows_the_scope():
    note = learn.render_note([
        {"id": "L-1", "target": "agent_rules", "change": "Do X", "status": "pending",
         "scope": {"kind": "project", "value": "notes-graph"}},
        {"id": "L-2", "target": "agent_rules", "change": "Do Y", "status": "accepted",
         "decided": "2026-11-02", "scope": {"kind": "tag", "value": "mobile"}},
    ])
    assert "- [ ] **L-1** · Agent rules (For project: notes-graph): Do X" in note
    assert "- [X] L-2 · accepted 2026-11-02 (For tag: mobile): Do Y" in note
