"""Tests for reading notes over NotesGraph's MCP endpoint, and for the
"Agent rules" note a Claude run gets on the end of its system prompt.

The MCP endpoint is a real local HTTP server speaking the stateless JSON-RPC
the real one does, so the request shape and the parsing are exercised over
actual requests.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from workflow.deploy.jobs import (AGENT_RULES_MAX_CHARS, CLAUDE_CODE_SYSTEM_PROMPT, Job,
                                  JobClient, claude_code_argv, load_agent_rules,
                                  run_job, with_agent_rules)
from workflow.deploy.notes import NotesReader, reader_from_config, strip_mcp_stamp
from workflow.deploy.registry.notesgraph import NotesGraphError


class FakeMcp:
    """NotesGraph's MCP endpoint, with a few notes in it."""

    def __init__(self, notes=None, search_style="lines", fail_with=None):
        self.notes = notes or {}          # docId -> {"title", "markdown"}
        self.search_style = search_style  # lines | array | glued
        self.fail_with = fail_with        # an HTTP status to answer with
        self.calls = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append((self.path, self.headers.get("Authorization"), body))
                if fake.fail_with:
                    self.send_response(fake.fail_with)
                    self.end_headers()
                    return
                name = body["params"]["name"]
                args = body["params"].get("arguments") or {}
                if name == "keyword_search":
                    hits = [{"docId": doc_id, "title": note["title"]}
                            for doc_id, note in fake.notes.items()
                            if args["query"].casefold() in note["title"].casefold()]
                    if fake.search_style == "array":
                        text = json.dumps(hits)
                    elif fake.search_style == "glued":
                        text = "".join(json.dumps(h) for h in hits)
                    else:
                        text = "\n".join(json.dumps(h) for h in hits)
                    result = {"content": [{"type": "text", "text": text}]}
                elif name == "read_document":
                    note = fake.notes.get(args["docId"])
                    if note is None:
                        result = {"content": [{"type": "text", "text": "no such document"}],
                                  "isError": True}
                    else:
                        result = {"content": [{"type": "text", "text": note["markdown"]}]}
                else:
                    reply = {"jsonrpc": "2.0", "id": body["id"],
                             "error": {"code": -32602, "message": f"Tool not found: {name}"}}
                    self._send(reply)
                    return
                self._send({"jsonrpc": "2.0", "id": body["id"], "result": result})

            def _send(self, payload):
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reader(self):
        return NotesReader(url=self.url, workspace="ws-1", token="pat")

    def job_client(self):
        return JobClient(url=self.url, workspace="ws-1", token="pat")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.server.shutdown()


RULES = "Research notes go under the folder `research`.\nTag people notes #person."
STAMPED = RULES + "\n\n> Edited via MCP by claude-opus-5-5 — 2026-10-07 06:43\n> \n"


def notes():
    return {
        "d-rules": {"title": "Agent rules", "markdown": STAMPED},
        "d-other": {"title": "Agent rules for the robot lab", "markdown": "not these"},
        "d-memory": {"title": "Agent memory", "markdown": "Cosmo: the user's son"},
    }


def test_the_stamp_is_dropped_but_a_quoted_line_in_the_text_is_kept():
    assert strip_mcp_stamp(STAMPED) == RULES
    assert strip_mcp_stamp("> a real quote\n\nmore") == "> a real quote\n\nmore"
    assert strip_mcp_stamp("") == ""


def test_a_tool_call_is_a_bearer_authenticated_json_rpc_post():
    with FakeMcp(notes()) as mcp:
        text = mcp.reader().call("read_document", {"docId": "d-memory"})
    assert text == "Cosmo: the user's son"
    path, auth, body = mcp.calls[0]
    assert path == "/api/workspaces/ws-1/mcp" and auth == "Bearer pat"
    assert body["method"] == "tools/call"
    assert body["params"] == {"name": "read_document", "arguments": {"docId": "d-memory"}}


@pytest.mark.parametrize("style", ["lines", "array", "glued"])
def test_search_hits_parse_however_the_server_lays_them_out(style):
    with FakeMcp(notes(), search_style=style) as mcp:
        hits = mcp.reader().search("agent")
    assert {h["docId"] for h in hits} == {"d-rules", "d-other", "d-memory"}


def test_find_wants_the_exact_title_not_the_best_fuzzy_match():
    with FakeMcp(notes()) as mcp:
        reader = mcp.reader()
        assert reader.find("agent  RULES")["docId"] == "d-rules"
        assert reader.find("Agent") is None


def test_read_strips_the_edit_stamp():
    with FakeMcp(notes()) as mcp:
        assert mcp.reader().read("d-rules") == RULES


def test_a_tool_error_is_a_notesgraph_error():
    with FakeMcp(notes()) as mcp:
        with pytest.raises(NotesGraphError, match="no such document"):
            mcp.reader().read("d-missing")
        with pytest.raises(NotesGraphError, match="Tool not found"):
            mcp.reader().call("list_boards")


def test_an_http_failure_is_a_notesgraph_error():
    with FakeMcp(notes(), fail_with=401) as mcp:
        with pytest.raises(NotesGraphError, match="401"):
            mcp.reader().search("x")


def test_no_token_is_said_plainly(monkeypatch):
    monkeypatch.setattr("workflow.deploy.notes.load_token", lambda: None)
    with pytest.raises(NotesGraphError, match="notes-login"):
        NotesReader(url="http://127.0.0.1:1", workspace="ws-1").search("x")


def test_the_reader_comes_from_the_deploy_config(monkeypatch):
    monkeypatch.setattr("workflow.deploy.notes.load_token", lambda: "pat")
    reader = reader_from_config({"deploy": {"notesgraph": {"url": "https://ng.example/",
                                                          "workspace": "ws-9"}}})
    assert reader.endpoint == "https://ng.example/api/workspaces/ws-9/mcp"
    with pytest.raises(NotesGraphError, match="workspace"):
        reader_from_config({})


# --- the "Agent rules" note ---------------------------------------------------

def test_the_rules_note_is_read_by_its_exact_title():
    with FakeMcp(notes()) as mcp:
        assert load_agent_rules(mcp.job_client()) == RULES


def test_no_rules_note_means_no_rules():
    with FakeMcp({"d-memory": notes()["d-memory"]}) as mcp:
        assert load_agent_rules(mcp.job_client()) == ""


def test_a_server_without_the_endpoint_does_not_fail_the_run():
    with FakeMcp(notes(), fail_with=404) as mcp:
        assert load_agent_rules(mcp.job_client()) == ""
    gone = JobClient(url="http://127.0.0.1:9", workspace="ws-1", token="pat")
    assert load_agent_rules(gone) == ""


def test_an_overlong_rules_note_is_cut_not_sent_whole():
    long_note = {"d-rules": {"title": "Agent rules", "markdown": "x" * (AGENT_RULES_MAX_CHARS + 500)}}
    with FakeMcp(long_note) as mcp:
        rules = load_agent_rules(mcp.job_client())
    assert len(rules) < AGENT_RULES_MAX_CHARS + 100 and rules.endswith("[cut: the note goes on]")


def test_rules_go_on_the_end_of_the_system_prompt():
    assert with_agent_rules("Be helpful.\n", "") == "Be helpful.\n"
    prompt = with_agent_rules("Be helpful.\n", RULES)
    assert prompt.startswith("Be helpful.\n\n")
    assert '"Agent rules"' in prompt and prompt.rstrip().endswith("#person.")


def job(**kw):
    return Job.from_payload({"id": "job-1234abcd", "agentId": "a", "agentName": "shows",
                             "instructions": "Find a show", "context": "", "model": "claude-code", **kw})


def test_claude_code_gets_the_rules_after_the_built_in_prompt(tmp_path):
    client = JobClient(url="https://app.notesgraph.com", workspace="ws-1", token="pat")
    argv, _ = claude_code_argv(job(), "go", tmp_path, client, rules=RULES)
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert prompt.startswith(CLAUDE_CODE_SYSTEM_PROMPT) and prompt.rstrip().endswith("#person.")


def test_the_rules_follow_the_servers_prompt_too(tmp_path):
    """The server's profile replaces the built-in prompt; the user's note
    still applies on top of whatever the server sends."""
    client = JobClient(url="https://app.notesgraph.com", workspace="ws-1", token="pat")
    profile = {"systemPrompt": "Be the agent the server says."}
    argv, _ = claude_code_argv(job(profile=profile), "go", tmp_path, client, rules=RULES)
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert prompt.startswith("Be the agent the server says.\n\n")
    assert prompt.rstrip().endswith("#person.")


def test_run_job_reads_the_note_once_and_hands_it_to_the_agent(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr("workflow.deploy.jobs._watch",
                        lambda *a: {"result": "", "steps": 1, "session": None})
    plain = tmp_path / "plain"
    plain.mkdir()
    with FakeMcp(notes()) as mcp:
        run_job(job(), config={}, use_tmux=False, client=mcp.job_client(), cwd=plain)
        reads = [c for c in mcp.calls if c[2]["params"]["name"] == "read_document"]
    assert len(reads) == 1
    spec = json.loads((tmp_path / "jobs" / "job-1234abcd" / "cmd.json").read_text())
    prompt = spec["argv"][spec["argv"].index("--append-system-prompt") + 1]
    assert prompt.startswith(CLAUDE_CODE_SYSTEM_PROMPT) and RULES in prompt
